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

export class EnvelopeError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'EnvelopeError'
    this.code = code
  }
}

function stripFence(text) {
  const fenced = /```(?:json)?\s*\n([\s\S]*?)\n```/.exec(text)
  return fenced ? fenced[1] : text
}

function firstJsonObject(text) {
  const start = text.indexOf('{')
  if (start < 0) return null
  let depth = 0
  let inString = false
  let escaped = false
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
    else if (ch === '}' && --depth === 0) return text.slice(start, i + 1)
  }
  return null
}

export function parseEnvelope(rawText) {
  const candidate = firstJsonObject(stripFence(String(rawText || '')))
  if (!candidate) throw new EnvelopeError('parse_error', 'no JSON envelope in the model output')
  let parsed
  try {
    parsed = JSON.parse(candidate)
  } catch (err) {
    throw new EnvelopeError('parse_error', `envelope is not valid JSON: ${err.message}`)
  }
  if (typeof parsed.reply !== 'string' || parsed.reply.trim() === '') {
    throw new EnvelopeError('parse_error', 'envelope has no reply')
  }
  const writes = parsed.ledger_writes ?? []
  if (!Array.isArray(writes)) throw new EnvelopeError('parse_error', 'ledger_writes must be a list')
  return { reply: parsed.reply, writes }
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
