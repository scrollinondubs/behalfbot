// Browser autopilot relay (plan: drafts/behalfbot/chrome-autopilot in
// new-jaxity). One AutopilotDO named "relay" holds every pairing code and
// session, so a code resolves without knowing its install. index.ts wraps
// AutopilotCore the way InstallDO wraps InstallCore; this file stays plain TS
// so the node tests can drive it.
//
//   admin  POST /installs/:id/autopilot/code   -> createCode     code XXX-XXX, single use, 10 min
//   admin  POST /autopilot/sessions {customer}   -> createCustomerCode  same, for a customer with no install
//   admin  GET  /autopilot/sessions?customer=    -> customerView   that customer's sessions and journal
//   ext    POST /autopilot/pair {code}          -> pair           session token, allowlist
//   ext    GET  /autopilot/ws?token=            -> accept         hibernatable socket
//   admin  POST /autopilot/:session/cmd         -> forward        RelayCommand out, reply back
//   admin  GET  /autopilot/:session/cmd/:id     -> poll           for replies that outlast one request
//   admin  POST /autopilot/:session/secret      -> takeSecret     one-shot, raw value, then gone
//
// Secret rule: a save_secret reply's value goes into ap_secret for at most 5
// minutes and leaves only through takeSecret. It is never in a cmd reply,
// the journal or a log line. Every other reply passes the redactor first.

import { Redactor } from './redact.ts'
import { ulid } from './ids.ts'
import { runbook, type Runbook } from './runbooks.ts'
import type { SqlLike } from './types.ts'

export const CODE_TTL_MS = 10 * 60 * 1000
// Check-on-read is exact; the sweep runs every 30 s, so a value never sits
// in storage longer than 5 minutes.
export const SECRET_TTL_MS = 4.5 * 60 * 1000
const SWEEP_MS = 30_000
// A paired session whose socket never opens is dropped after this.
export const UNCONNECTED_TTL_MS = 10 * 60 * 1000
export const MAX_WAIT_MS = 25_000
export const DEFAULT_WAIT_MS = 15_000
// Failed pairing attempts allowed per minute across the whole relay.
export const PAIR_FAILS_PER_MIN = 20

const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTVWXYZ23456789'
export const SESSION_RE = /^aps_[0-9A-HJKMNP-TV-Z]{26}$/
// Owner id for an autopilot-only customer: someone already installed who
// needs a browser session, with no install channel, homework or alarms. It
// sits in the install_id column of ap_code and ap_session (the deployed
// tables keep their names) and can never pass INSTALL_ID_RE.
export const CUSTOMER_ID_RE = /^apcu_[0-9A-HJKMNP-TV-Z]{26}$/
const CUSTOMER_JOURNAL_KEEP = 1000

// A ref from the snapshot is a backendDOMNodeId. CDP wants an int32 and
// rejects "2378", so accept the forms a caller copies out of "[2378]".
export function toRef(v: unknown): number | null {
  let n: number
  if (typeof v === 'number') n = v
  else if (typeof v === 'string' && /^\s*\[?\s*\d{1,10}\s*\]?\s*$/.test(v)) n = Number(v.replace(/[^\d]/g, ''))
  else return null
  return Number.isInteger(n) && n > 0 && n <= 0x7fffffff ? n : null
}

export const RELAY_OPS = ['cdp', 'scroll', 'save_secret', 'human_step', 'note', 'screenshot', 'clear', 'challenge'] as const

// A screenshot reply's base64 JPEG. Matches MAX_SCREENSHOT_B64 in
// chrome-ext/policy.js and stays well under the 2 MB SQLite row cap.
export const MAX_SCREENSHOT_B64 = 1_200_000
export const SCREENSHOT_LABEL = 'Took a screenshot of the tab'
export type RelayOp = (typeof RELAY_OPS)[number]

// The extension enforces the same list (chrome-ext/policy.js); this copy
// refuses early so a bad command never reaches the browser.
export const RELAY_METHODS = [
  'Accessibility.getFullAXTree',
  'DOM.getBoxModel',
  'DOM.scrollIntoViewIfNeeded',
  'DOM.focus',
  'Input.dispatchMouseEvent',
  'Input.insertText',
  'Input.dispatchKeyEvent',
  'Page.navigate',
] as const

// The one message the driver, the DO and the extension share. The DO fills
// in id; the extension owns the tab, so there is no tabId here.
export interface RelayCommand {
  id: string
  op: RelayOp
  method?: (typeof RELAY_METHODS)[number]
  params?: Record<string, unknown>
  ref?: number // backendDOMNodeId, for save_secret and clear
  dy?: number // for scroll
  label: string // the side panel and journal line, in plain words; '' for helper calls
  secretTarget?: string // save_secret only: the vault item name
}

export interface RelayReply {
  ok: boolean
  result?: unknown
  error?: string
}

export interface WsLike {
  send(data: string): void
  close(code?: number, reason?: string): void
}

export interface AutopilotDeps {
  sql: SqlLike
  now(): number
  setAlarm(at: number | null): Promise<void>
  getAlarm(): Promise<number | null>
  accept(ws: WsLike, sessionId: string): void
  sockets(sessionId: string): WsLike[]
  attachment(ws: WsLike): { session_id: string } | null
  // Writes one line into that install's journal (InstallDO.autopilotNote).
  journal(installId: string, line: string): Promise<void>
}

export interface Session {
  session_id: string
  install_id: string
  allow: string[]
  task: string
  start_url: string
  created_at: number
  connected_at: number | null
  tab_url: string | null
}

export const AUTOPILOT_DDL = [
  `CREATE TABLE IF NOT EXISTS ap_code (
    code TEXT PRIMARY KEY, install_id TEXT NOT NULL, runbook TEXT NOT NULL, expires_at INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS ap_session (
    session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, install_id TEXT NOT NULL,
    runbook TEXT NOT NULL, created_at INTEGER NOT NULL, connected_at INTEGER, tab_url TEXT)`,
  // One row per command. body is NULL until the extension answers; the row
  // goes once the driver has the answer.
  `CREATE TABLE IF NOT EXISTS ap_cmd (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, op TEXT NOT NULL, label TEXT NOT NULL,
    method TEXT, secret_target TEXT, body TEXT, created_at INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS ap_secret (
    session_id TEXT NOT NULL, item TEXT NOT NULL, value TEXT NOT NULL, expires_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, item))`,
  `CREATE TABLE IF NOT EXISTS ap_pair_fail (minute INTEGER PRIMARY KEY, n INTEGER NOT NULL)`,
  // Autopilot-only customers and their journal. An install's lines go to its
  // InstallDO journal instead.
  `CREATE TABLE IF NOT EXISTS ap_customer (
    customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS ap_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, line TEXT NOT NULL, at INTEGER NOT NULL)`,
]

export type Out = { status: number; body: Record<string, unknown> }

const enc = new TextEncoder()

async function sha256Hex(s: string): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(s)))
  return [...d].map((b) => b.toString(16).padStart(2, '0')).join('')
}

function b64url(bytes: Uint8Array): string {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function newCode(): string {
  const r = crypto.getRandomValues(new Uint8Array(6))
  let s = ''
  for (let i = 0; i < 6; i++) s += CODE_ALPHABET[r[i] % CODE_ALPHABET.length]
  return `${s.slice(0, 3)}-${s.slice(3)}`
}

export function normalizeCode(raw: unknown): string | null {
  const s = String(raw ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '')
  if (s.length !== 6) return null
  for (const ch of s) if (!CODE_ALPHABET.includes(ch)) return null
  return `${s.slice(0, 3)}-${s.slice(3)}`
}

// Validates what the driver sent. Returns the error text or null.
export function commandError(c: Record<string, unknown>): string | null {
  if (!RELAY_OPS.includes(c.op as RelayOp)) return `op must be one of ${RELAY_OPS.join(', ')}`
  if (typeof c.label !== 'string' || c.label.length > 300) return 'label must be a string up to 300 chars'
  if (c.op === 'cdp') {
    if (!RELAY_METHODS.includes(c.method as (typeof RELAY_METHODS)[number])) return 'method not allowed'
    if (c.params !== undefined && (typeof c.params !== 'object' || c.params === null || Array.isArray(c.params))) return 'params must be an object'
    const p = c.params as Record<string, unknown> | undefined
    if (p && p.backendNodeId !== undefined && toRef(p.backendNodeId) === null) return 'params.backendNodeId must be a positive integer ref'
  }
  if (c.op === 'save_secret') {
    if (toRef(c.ref) === null) return 'save_secret needs ref'
    if (typeof c.secretTarget !== 'string' || !c.secretTarget.trim() || c.secretTarget.length > 200) return 'save_secret needs secretTarget'
  }
  if (c.op === 'clear' && toRef(c.ref) === null) return 'clear needs ref'
  if (c.op === 'scroll' && !Number.isInteger(c.dy)) return 'scroll needs integer dy'
  if (c.op === 'human_step' && !c.label) return 'human_step needs a label'
  return null
}

type Waiter = (body: Record<string, unknown>) => void

export class AutopilotCore {
  private waiters = new Map<string, Waiter>()
  private d: AutopilotDeps

  constructor(d: AutopilotDeps) {
    this.d = d
    for (const ddl of AUTOPILOT_DDL) d.sql.exec(ddl)
  }

  private rows<T>(q: string, ...b: unknown[]): T[] {
    return this.d.sql.exec(q, ...b).toArray() as T[]
  }

  private async scheduleSweep(): Promise<void> {
    const at = this.d.now() + SWEEP_MS
    const cur = await this.d.getAlarm()
    if (cur === null || cur > at) await this.d.setAlarm(at)
  }

  private sessionRow(sessionId: string): (Omit<Session, 'allow' | 'task' | 'start_url'> & { runbook: string }) | null {
    const [r] = this.rows<Omit<Session, 'allow' | 'task' | 'start_url'> & { runbook: string }>(
      'SELECT session_id, install_id, runbook, created_at, connected_at, tab_url FROM ap_session WHERE session_id = ?',
      sessionId,
    )
    return r ?? null
  }

  private view(r: { session_id: string; install_id: string; runbook: string; created_at: number; connected_at: number | null; tab_url: string | null }): Session {
    const rb = runbook(r.runbook) as Runbook
    return { ...r, allow: rb.allow, task: rb.task, start_url: rb.start_url }
  }

  private async journal(ownerId: string, line: string): Promise<void> {
    const text = `autopilot: ${this.redactor().redact(line)}`
    if (CUSTOMER_ID_RE.test(ownerId)) {
      this.d.sql.exec('INSERT INTO ap_journal (owner_id, line, at) VALUES (?, ?, ?)', ownerId, text.slice(0, 500), Math.floor(this.d.now() / 1000))
      this.d.sql.exec(
        'DELETE FROM ap_journal WHERE owner_id = ? AND seq <= (SELECT seq FROM ap_journal WHERE owner_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?)',
        ownerId,
        ownerId,
        CUSTOMER_JOURNAL_KEEP,
      )
      return
    }
    try {
      await this.d.journal(ownerId, text)
    } catch {
      // The journal is the record, but a journal hiccup must not wedge the
      // browser session.
    }
  }

  private redactor(sessionId?: string): Redactor {
    const r = new Redactor()
    const rows = sessionId
      ? this.rows<{ value: string }>('SELECT value FROM ap_secret WHERE session_id = ?', sessionId)
      : this.rows<{ value: string }>('SELECT value FROM ap_secret')
    for (const s of rows) r.add(s.value)
    return r
  }

  // ---- admin: pairing code ----------------------------------------------

  async createCode(installId: string, runbookId: string, devRunbooks: boolean): Promise<Out> {
    const rb = runbook(runbookId)
    if (!rb || (rb.dev_only && !devRunbooks)) return { status: 400, body: { error: `unknown runbook ${runbookId}` } }
    const now = this.d.now()
    let code = newCode()
    while (this.rows('SELECT 1 FROM ap_code WHERE code = ?', code).length) code = newCode()
    this.d.sql.exec('INSERT INTO ap_code (code, install_id, runbook, expires_at) VALUES (?, ?, ?, ?)', code, installId, rb.id, now + CODE_TTL_MS)
    await this.scheduleSweep()
    await this.journal(installId, `pairing code issued for "${rb.task}", sites: ${rb.allow.join(', ')}`)
    return { status: 201, body: { code, expires_at: Math.floor((now + CODE_TTL_MS) / 1000), allow: rb.allow, task: rb.task } }
  }

  // ---- admin: autopilot-only customers ----------------------------------

  private customerRow(rawName: unknown): { customer_id: string; name: string } | null {
    const key = nameKey(rawName)
    if (!key) return null
    const [r] = this.rows<{ customer_id: string; name: string }>('SELECT customer_id, name FROM ap_customer WHERE name_key = ?', key)
    return r ?? null
  }

  // Same code as createCode, owned by a named customer instead of an install.
  // The relay has no Discord client, so this path cannot post anywhere.
  async createCustomerCode(rawName: unknown, runbookId: string, devRunbooks: boolean): Promise<Out> {
    const key = nameKey(rawName)
    if (!key) return { status: 400, body: { error: 'customer must be a name, 1 to 100 characters' } }
    const rb = runbook(runbookId)
    if (!rb || (rb.dev_only && !devRunbooks)) return { status: 400, body: { error: `unknown runbook ${runbookId}` } }
    let c = this.customerRow(rawName)
    if (!c) {
      c = { customer_id: `apcu_${ulid(this.d.now())}`, name: String(rawName).trim().replace(/\s+/g, ' ') }
      this.d.sql.exec('INSERT INTO ap_customer (customer_id, name, name_key, created_at) VALUES (?, ?, ?, ?)', c.customer_id, c.name, key, this.d.now())
    }
    const r = await this.createCode(c.customer_id, rb.id, devRunbooks)
    return { status: r.status, body: { customer_id: c.customer_id, customer: c.name, ...r.body } }
  }

  customerView(rawName: unknown): Out {
    const c = this.customerRow(rawName)
    if (!c) return { status: 404, body: { error: 'no autopilot customer by that name' } }
    const journal = this.rows<{ line: string; at: number }>('SELECT line, at FROM ap_journal WHERE owner_id = ? ORDER BY seq', c.customer_id)
    return { status: 200, body: { customer_id: c.customer_id, customer: c.name, sessions: this.listSessions(c.customer_id).body.sessions, journal } }
  }

  // ---- extension: pair and connect --------------------------------------

  async pair(rawCode: unknown): Promise<Out> {
    const now = this.d.now()
    const minute = Math.floor(now / 60_000)
    const [f] = this.rows<{ n: number }>('SELECT n FROM ap_pair_fail WHERE minute = ?', minute)
    if (f && f.n >= PAIR_FAILS_PER_MIN) return { status: 429, body: { error: 'too many attempts, wait a minute' } }

    const code = normalizeCode(rawCode)
    const [row] = code
      ? this.rows<{ install_id: string; runbook: string; expires_at: number }>('SELECT install_id, runbook, expires_at FROM ap_code WHERE code = ?', code)
      : []
    // Single use: the code is gone whether or not it was still valid.
    if (code) this.d.sql.exec('DELETE FROM ap_code WHERE code = ?', code)
    const rb = row ? runbook(row.runbook) : null
    if (!row || row.expires_at < now || !rb) {
      this.d.sql.exec(
        'INSERT INTO ap_pair_fail (minute, n) VALUES (?, 1) ON CONFLICT(minute) DO UPDATE SET n = n + 1',
        minute,
      )
      this.d.sql.exec('DELETE FROM ap_pair_fail WHERE minute < ?', minute - 5)
      return { status: 404, body: { error: 'code not found or expired' } }
    }
    const token = b64url(crypto.getRandomValues(new Uint8Array(32)))
    const sessionId = `aps_${ulid(now)}`
    this.d.sql.exec(
      'INSERT INTO ap_session (session_id, token_hash, install_id, runbook, created_at) VALUES (?, ?, ?, ?, ?)',
      sessionId,
      await sha256Hex(token),
      row.install_id,
      rb.id,
      now,
    )
    await this.scheduleSweep()
    await this.journal(row.install_id, `browser paired (session ${sessionId})`)
    return { status: 200, body: { token, session: sessionId, allow: rb.allow, task: rb.task, start_url: rb.start_url } }
  }

  // Called for GET /autopilot/ws before the upgrade. One socket per session.
  async sessionForToken(token: string | null): Promise<Session | null> {
    if (!token) return null
    const [r] = this.rows<{ session_id: string }>('SELECT session_id FROM ap_session WHERE token_hash = ?', await sha256Hex(token))
    if (!r) return null
    if (this.d.sockets(r.session_id).length > 0) return null
    const s = this.sessionRow(r.session_id)
    return s ? this.view(s) : null
  }

  async accept(ws: WsLike, s: Session): Promise<void> {
    this.d.accept(ws, s.session_id)
    this.d.sql.exec('UPDATE ap_session SET connected_at = ? WHERE session_id = ?', this.d.now(), s.session_id)
    await this.journal(s.install_id, `browser connected (session ${s.session_id})`)
  }

  // ---- socket events -----------------------------------------------------

  async onMessage(ws: WsLike, data: string | ArrayBuffer): Promise<void> {
    const att = this.d.attachment(ws)
    if (!att) return ws.close(1008, 'no session')
    const s = this.sessionRow(att.session_id)
    if (!s) return ws.close(1008, 'session ended')
    let m: Record<string, unknown>
    try {
      m = JSON.parse(typeof data === 'string' ? data : new TextDecoder().decode(data))
    } catch {
      return
    }
    if (m.type === 'hello' || m.type === 'tab') {
      const url = typeof m.tab_url === 'string' ? m.tab_url.slice(0, 500) : null
      this.d.sql.exec('UPDATE ap_session SET tab_url = ? WHERE session_id = ?', url, s.session_id)
      return
    }
    if (m.type === 'event' && typeof m.text === 'string') {
      // Things Chelsea did in the browser: Done pressed, Cancel on the bar.
      await this.journal(s.install_id, m.text.slice(0, 300))
      return
    }
    if (m.type !== 'reply' || typeof m.id !== 'string') return
    const [cmd] = this.rows<{ op: string; method: string | null; label: string; secret_target: string | null; body: string | null }>(
      'SELECT op, method, label, secret_target, body FROM ap_cmd WHERE id = ? AND session_id = ?',
      m.id,
      s.session_id,
    )
    if (!cmd || cmd.body !== null) return

    let body: Record<string, unknown>
    let image: string | null = null
    if (m.secret === true) {
      // Only a pending save_secret may carry a value, and the value stops here.
      const value = typeof m.value === 'string' ? m.value : ''
      if (cmd.op !== 'save_secret' || !cmd.secret_target || !value.trim()) {
        body = { ok: false, error: 'field was empty or the command was not save_secret' }
        await this.journal(s.install_id, `secret for vault item "${cmd.secret_target ?? '?'}" not captured`)
      } else {
        this.d.sql.exec(
          'INSERT INTO ap_secret (session_id, item, value, expires_at) VALUES (?, ?, ?, ?) ON CONFLICT(session_id, item) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at',
          s.session_id,
          cmd.secret_target,
          value.trim(),
          this.d.now() + SECRET_TTL_MS,
        )
        await this.scheduleSweep()
        body = { ok: true, result: 'captured' }
        await this.journal(s.install_id, `secret captured for vault item "${cmd.secret_target}" (held 5 min, one fetch)`)
      }
    } else if (cmd.op === 'save_secret') {
      body = { ok: false, error: typeof m.error === 'string' ? m.error.slice(0, 300) : 'save_secret failed' }
    } else if (cmd.op === 'screenshot' && m.ok === true) {
      const shot = screenshotResult(m.result)
      body = shot ? { ok: true, result: shot.meta } : { ok: false, error: 'screenshot reply was not a JPEG within the size limit' }
      if (shot) image = shot.data
    } else {
      body = { ok: m.ok === true }
      if (m.ok === true) body.result = m.result ?? null
      else body.error = typeof m.error === 'string' ? m.error.slice(0, 300) : 'failed'
    }
    if (body.ok !== true && cmd.op !== 'challenge') {
      await this.journal(s.install_id, `${cmd.label || cmd.method || cmd.op}: failed (${String(body.error)})`)
    }
    // Belt and braces: a snapshot taken while the key is on screen must not
    // carry it to the driver.
    // The image bytes skip the text redactor: base64 is full of 64-character
    // runs its token patterns would cut into. Pixels are masked in the
    // extension before capture leaves the browser.
    let text = this.redactor(s.session_id).redact(JSON.stringify(body))
    if (image !== null) {
      const b = JSON.parse(text)
      b.result = { ...b.result, data: image }
      text = JSON.stringify(b)
    }
    this.d.sql.exec('UPDATE ap_cmd SET body = ? WHERE id = ?', text, m.id)
    const w = this.waiters.get(m.id)
    if (w) w(JSON.parse(text))
  }

  async onClose(ws: WsLike): Promise<void> {
    const att = this.d.attachment(ws)
    if (att) await this.end(att.session_id, 'browser disconnected')
  }

  // Deletes the session, its token, any held secret and pending commands.
  async end(sessionId: string, why: string): Promise<Out> {
    const s = this.sessionRow(sessionId)
    if (!s) return { status: 404, body: { error: 'no such session' } }
    this.d.sql.exec('DELETE FROM ap_session WHERE session_id = ?', sessionId)
    this.d.sql.exec('DELETE FROM ap_secret WHERE session_id = ?', sessionId)
    const pending = this.rows<{ id: string }>('SELECT id FROM ap_cmd WHERE session_id = ?', sessionId)
    this.d.sql.exec('DELETE FROM ap_cmd WHERE session_id = ?', sessionId)
    for (const p of pending) this.waiters.get(p.id)?.({ ok: false, error: 'session ended' })
    for (const ws of this.d.sockets(sessionId)) {
      try {
        ws.close(1000, why.slice(0, 100))
      } catch {
        // already closing
      }
    }
    await this.journal(s.install_id, `${why} (session ${sessionId} ended)`)
    return { status: 200, body: { ended: sessionId } }
  }

  // ---- driver ------------------------------------------------------------

  listSessions(installId: string): Out {
    const rows = this.rows<{ session_id: string; install_id: string; runbook: string; created_at: number; connected_at: number | null; tab_url: string | null }>(
      'SELECT session_id, install_id, runbook, created_at, connected_at, tab_url FROM ap_session WHERE install_id = ? ORDER BY created_at',
      installId,
    )
    const sessions = rows.map((r) => {
      const v = this.view(r)
      return { ...v, connected: this.d.sockets(r.session_id).length > 0, tab_url: r.tab_url ? this.redactor().redact(r.tab_url) : null }
    })
    return { status: 200, body: { sessions } }
  }

  async forward(sessionId: string, raw: Record<string, unknown>, waitMs?: number): Promise<Out> {
    const s = this.sessionRow(sessionId)
    if (!s) return { status: 404, body: { error: 'no such session' } }
    const sockets = this.d.sockets(sessionId)
    if (sockets.length === 0) return { status: 409, body: { error: 'browser not connected' } }
    const err = commandError(raw)
    if (err) return { status: 400, body: { error: err } }
    const cmd: RelayCommand = {
      id: `apc_${ulid(this.d.now())}`,
      op: raw.op as RelayOp,
      label: (raw.label as string).trim(),
    }
    if (cmd.op === 'cdp') {
      cmd.method = raw.method as RelayCommand['method']
      cmd.params = { ...((raw.params as Record<string, unknown>) ?? {}) }
      if (cmd.params.backendNodeId !== undefined) cmd.params.backendNodeId = toRef(cmd.params.backendNodeId)
    }
    if (cmd.op === 'save_secret') {
      cmd.ref = toRef(raw.ref) as number
      cmd.secretTarget = (raw.secretTarget as string).trim()
    }
    if (cmd.op === 'clear') cmd.ref = toRef(raw.ref) as number
    if (cmd.op === 'scroll') cmd.dy = raw.dy as number
    // The journal says a screenshot was taken whatever the driver called it.
    if (cmd.op === 'screenshot') cmd.label = SCREENSHOT_LABEL
    this.d.sql.exec(
      'INSERT INTO ap_cmd (id, session_id, op, method, label, secret_target, body, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)',
      cmd.id,
      sessionId,
      cmd.op,
      cmd.method ?? null,
      cmd.label,
      cmd.secretTarget ?? null,
      this.d.now(),
    )
    if (cmd.label) await this.journal(s.install_id, cmd.op === 'human_step' ? `your turn: ${cmd.label}` : cmd.label)
    sockets[0].send(JSON.stringify(cmd))
    return this.poll(sessionId, cmd.id, waitMs)
  }

  async poll(sessionId: string, id: string, waitMs?: number): Promise<Out> {
    const wait = Math.max(0, Math.min(MAX_WAIT_MS, waitMs ?? DEFAULT_WAIT_MS))
    const take = (): Out | null => {
      const [r] = this.rows<{ body: string | null }>('SELECT body FROM ap_cmd WHERE id = ? AND session_id = ?', id, sessionId)
      if (!r) return { status: 404, body: { error: 'no such command (answered already, or the session ended)' } }
      if (r.body === null) return null
      this.d.sql.exec('DELETE FROM ap_cmd WHERE id = ?', id)
      return { status: 200, body: { id, ...JSON.parse(r.body) } }
    }
    const now = take()
    if (now) return now
    const answered = await new Promise<boolean>((resolve) => {
      const t = setTimeout(() => {
        this.waiters.delete(id)
        resolve(false)
      }, wait)
      this.waiters.set(id, () => {
        clearTimeout(t)
        this.waiters.delete(id)
        resolve(true)
      })
    })
    const [r] = this.rows<{ body: string | null }>('SELECT body FROM ap_cmd WHERE id = ?', id)
    if (!r) return { status: 410, body: { id, ok: false, error: 'session ended' } }
    if (!answered && r.body === null) return { status: 202, body: { id, pending: true } }
    return take() ?? { status: 202, body: { id, pending: true } }
  }

  // The one way a captured value leaves the relay: returned once, then gone.
  async takeSecret(sessionId: string, item: string): Promise<{ value: string } | null> {
    const [r] = this.rows<{ value: string; expires_at: number }>(
      'SELECT value, expires_at FROM ap_secret WHERE session_id = ? AND item = ?',
      sessionId,
      item,
    )
    this.d.sql.exec('DELETE FROM ap_secret WHERE session_id = ? AND item = ?', sessionId, item)
    if (!r || r.expires_at < this.d.now()) return null
    const s = this.sessionRow(sessionId)
    if (s) await this.journal(s.install_id, `secret for vault item "${item}" handed to the vault writer`)
    return { value: r.value }
  }

  // ---- alarm -------------------------------------------------------------

  async onAlarm(): Promise<void> {
    const now = this.d.now()
    this.d.sql.exec('DELETE FROM ap_code WHERE expires_at < ?', now)
    this.d.sql.exec('DELETE FROM ap_secret WHERE expires_at < ?', now)
    const stale = this.rows<{ session_id: string }>(
      'SELECT session_id FROM ap_session WHERE connected_at IS NULL AND created_at < ?',
      now - UNCONNECTED_TTL_MS,
    )
    for (const r of stale) await this.end(r.session_id, 'pairing never connected')
    const left =
      this.rows('SELECT 1 FROM ap_code LIMIT 1').length +
      this.rows('SELECT 1 FROM ap_secret LIMIT 1').length +
      this.rows('SELECT 1 FROM ap_session WHERE connected_at IS NULL LIMIT 1').length
    await this.d.setAlarm(left ? now + SWEEP_MS : null)
  }
}

// Only a JPEG within the size cap passes, with its numbers; any other field
// the extension might add is dropped.
export function screenshotResult(r: unknown): { meta: Record<string, unknown>; data: string } | null {
  if (!r || typeof r !== 'object') return null
  const o = r as Record<string, unknown>
  const data = o.data
  if (o.mime !== 'image/jpeg' || typeof data !== 'string' || data.length === 0 || data.length > MAX_SCREENSHOT_B64) return null
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(data) || !data.startsWith('/9j/')) return null
  const n = (v: unknown) => (Number.isInteger(v) && (v as number) >= 0 && (v as number) <= 100_000 ? (v as number) : null)
  return { meta: { mime: 'image/jpeg', width: n(o.width), height: n(o.height), masked: n(o.masked) }, data }
}

function nameKey(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const k = raw.trim().replace(/\s+/g, ' ').toLowerCase()
  return k.length >= 1 && k.length <= 100 ? k : null
}
