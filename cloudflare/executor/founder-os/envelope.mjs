// The model's side of a session is a JSON envelope, not live API access:
//   { "reply": "<markdown for the founder>", "ledger_writes": [ { "method", "args" } ] }
// The shim validates the whole envelope before applying anything. One bad
// write rejects them all, so a session never half-applies an envelope that
// was trying something it should not.

import { containsFounderIdKey } from './ledger-client.mjs'

export const MAX_WRITES = 25

const COACH_WRITES = ['add_artifact', 'add_pain', 'add_interview', 'add_prfaq_version', 'mark_gate_pending']

// Keyed by the executor's skill type (plugin.mjs loadSkill). A Basic review
// only answers: VCL reads its verdict block and records the card status.
export const WRITES_BY_SKILL_TYPE = {
  'stage-skill': COACH_WRITES,
  coach: COACH_WRITES,
  auditor: ['add_audit', 'record_gate_decision'],
  'basic-coach': COACH_WRITES,
  'basic-review': [],
}

const STAGE_BOUND = new Set(['add_artifact', 'add_prfaq_version', 'mark_gate_pending', 'record_gate_decision'])

export const EXCERPT_CHARS = 1500

// `detail` says which format check failed (no_json, invalid_json, no_reply,
// writes_not_list) and carries the start of the model's output, so a failed
// session can be diagnosed from its VCL row and the container log.
export class EnvelopeError extends Error {
  constructor(code, message, detail = null) {
    super(message)
    this.name = 'EnvelopeError'
    this.code = code
    this.detail = detail
  }
}

function parseError(check, message, rawText) {
  return new EnvelopeError('parse_error', message, { check, message, excerpt: String(rawText || '').slice(0, EXCERPT_CHARS) })
}

function stripFence(text) {
  const fenced = /```(?:json)?\s*\n([\s\S]*?)\n```/.exec(text)
  return fenced ? fenced[1] : text
}

// Every balanced top-level {...} span, in order. The envelope is not always
// the first one: prose before it can hold a brace, and a reply in the skill's
// own format holds only the verdict object.
function jsonObjects(text) {
  const spans = []
  let from = 0
  for (;;) {
    const start = text.indexOf('{', from)
    if (start < 0) return spans
    let depth = 0
    let inString = false
    let escaped = false
    let end = -1
    for (let i = start; i < text.length; i++) {
      const ch = text[i]
      if (inString) {
        if (escaped) escaped = false
        else if (ch === '\\') escaped = true
        else if (ch === '"') inString = false
        continue
      }
      if (ch === '"') inString = true
      else if (ch === '{') depth++
      else if (ch === '}' && --depth === 0) {
        end = i
        break
      }
    }
    if (end < 0) {
      from = start + 1
      continue
    }
    spans.push(text.slice(start, end + 1))
    from = end + 1
  }
}

// Models sometimes put real newlines or tabs inside a JSON string, most often
// in a multi-line reply that ends in a fenced block. JSON forbids them; escape
// them and try again rather than fail the session.
function escapeControlCharsInStrings(text) {
  let out = ''
  let inString = false
  let escaped = false
  for (const ch of text) {
    if (inString) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') inString = false
      else if (ch === '\n') {
        out += '\\n'
        continue
      } else if (ch === '\r') {
        out += '\\r'
        continue
      } else if (ch === '\t') {
        out += '\\t'
        continue
      }
    } else if (ch === '"') inString = true
    out += ch
  }
  return out
}

function tryJson(text) {
  try {
    return { value: JSON.parse(text) }
  } catch (err) {
    try {
      return { value: JSON.parse(escapeControlCharsInStrings(text)) }
    } catch {
      return { error: err }
    }
  }
}

const isObject = v => v !== null && typeof v === 'object' && !Array.isArray(v)

export function parseEnvelope(rawText) {
  const raw = String(rawText || '')
  const stripped = stripFence(raw)
  const candidates = [...new Set([...jsonObjects(stripped), ...jsonObjects(raw)])]
  if (candidates.length === 0) throw parseError('no_json', 'no JSON envelope in the model output', raw)

  let firstError = null
  let parsedAny = false
  for (const candidate of candidates) {
    const { value, error } = tryJson(candidate)
    if (error) {
      firstError ??= error
      continue
    }
    if (!isObject(value)) continue
    parsedAny = true
    if (typeof value.reply !== 'string' || value.reply.trim() === '') continue
    const writes = value.ledger_writes ?? []
    if (!Array.isArray(writes)) throw parseError('writes_not_list', 'ledger_writes must be a list', raw)
    return { reply: value.reply, writes }
  }
  if (!parsedAny && firstError) {
    throw parseError('invalid_json', `envelope is not valid JSON: ${firstError.message}`, raw)
  }
  const also = firstError ? ` (another object was not valid JSON: ${firstError.message})` : ''
  throw parseError('no_reply', `envelope has no reply${also}`, raw)
}

const VERDICT_RE = /```founderos-verdict[^\n]*\n([\s\S]*?)```/g
const VERDICTS = new Set(['accepted', 'needs_work'])

// The last founderos-verdict block, the way VCL reads it (vibecodelisboa
// src/lib/founderos/basic.ts parseVerdict), or null if it would not parse there.
export function findVerdict(text) {
  const last = Array.from(String(text || '').matchAll(VERDICT_RE)).at(-1)
  if (!last) return null
  try {
    const v = JSON.parse(last[1].trim())
    if (!isObject(v) || typeof v.card_id !== 'string' || !VERDICTS.has(v.verdict)) return null
    return { card_id: v.card_id, verdict: v.verdict }
  } catch {
    return null
  }
}

// A Basic review answered in the skill's own format (markdown ending in the
// verdict block) instead of the JSON envelope. The review writes nothing and
// VCL reads the verdict from the reply text, so the whole output is the reply.
// Returns null when there is no verdict VCL could use.
export function reviewFallback(rawText) {
  const verdict = findVerdict(rawText)
  if (!verdict) return null
  return { reply: String(rawText).trim(), writes: [], verdict }
}

// Returns the writes normalised and ready to apply, or throws EnvelopeError.
export function validateWrites(writes, { skillType, currentStage }) {
  const allowed = new Set(WRITES_BY_SKILL_TYPE[skillType] || [])
  if (writes.length > MAX_WRITES) {
    throw new EnvelopeError('envelope_rejected', `more than ${MAX_WRITES} writes`)
  }
  return writes.map((write, i) => {
    const where = `ledger_writes[${i}]`
    if (!write || typeof write !== 'object' || typeof write.method !== 'string') {
      throw new EnvelopeError('envelope_rejected', `${where} has no method`)
    }
    if (!allowed.has(write.method)) {
      throw new EnvelopeError('envelope_rejected', `${where}: ${write.method} is not allowed for a ${skillType}`)
    }
    const args = write.args ?? {}
    if (typeof args !== 'object' || Array.isArray(args)) {
      throw new EnvelopeError('envelope_rejected', `${where}: args must be an object`)
    }
    if (containsFounderIdKey(write)) {
      throw new EnvelopeError('envelope_rejected', `${where} names a founder_id; the session founder is fixed`)
    }
    if (STAGE_BOUND.has(write.method) && args.stage !== currentStage) {
      throw new EnvelopeError('envelope_rejected', `${where}: stage must be the founder's current stage (${currentStage})`)
    }
    const normalised = { ...args }
    if (write.method === 'record_gate_decision') {
      // Sean signs off in the dashboard, never through a session.
      normalised.decided_by = 'claude'
      normalised.sean_signoff = false
    }
    return { method: write.method, args: normalised }
  })
}

const APPLY = {
  add_artifact: (c, a) => c.addArtifact(a),
  add_pain: (c, a) => c.addPain(a),
  add_interview: (c, a) => c.addInterview(a),
  add_prfaq_version: (c, a) => c.addPrfaqVersion(a),
  mark_gate_pending: (c, a) => c.markGatePending(a),
  add_audit: (c, a) => c.addAudit(a),
  record_gate_decision: (c, a) => c.recordGateDecision(a),
}

export async function applyWrites(client, writes) {
  const results = []
  for (const write of writes) {
    try {
      const row = await APPLY[write.method](client, write.args)
      results.push({ method: write.method, status: 'applied', ...(row?.id ? { id: row.id } : {}) })
    } catch (err) {
      if (err.code === 'tenant_mismatch') throw err
      results.push({ method: write.method, status: 'failed', error: err.code || 'error' })
    }
  }
  return results
}
