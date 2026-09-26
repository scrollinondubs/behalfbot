// Reference mock of the VCL tenant-scoped ledger API
// (docs/founder-os-ledger-api.md). Every rule in the contract's "Tenant rules"
// section is implemented here, so it doubles as an executable spec for
// vibecodelisboa#508. It also records every request it receives, which is how
// the isolation tests prove the executor never even asked for another
// founder's data.

import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'

const TABLES = ['artifacts', 'pains', 'interviews', 'audits', 'prfaq_versions', 'gate_decisions', 'stage_progress']
const AUDIT_TARGETS = new Set(['artifacts', 'pains', 'interviews', 'prfaq_versions'])
const EVIDENCE_TABLES = new Set(['artifacts', 'pains', 'interviews', 'prfaq_versions', 'audits'])

function hasFounderIdKey(v) {
  if (Array.isArray(v)) return v.some(hasFounderIdKey)
  if (v && typeof v === 'object') return Object.entries(v).some(([k, x]) => k === 'founder_id' || hasFounderIdKey(x))
  return false
}

export async function startMockVcl() {
  const now = () => new Date().toISOString()
  const founders = new Map()
  const rows = Object.fromEntries(TABLES.map(t => [t, []]))
  const tokens = new Map()
  const requests = []
  const sessionResults = []

  const state = {
    founders,
    rows,
    requests,
    sessionResults,
    addFounder({ stage = 1, name = 'Test founder' } = {}) {
      const founder_id = randomUUID()
      founders.set(founder_id, {
        founder_id, display_name: name, cohort: 'c1', current_stage: stage, context: {},
        created_at: now(), updated_at: now(),
      })
      for (let s = 0; s <= stage; s++) {
        rows.stage_progress.push({
          id: randomUUID(), founder_id, stage: s, status: s < stage ? 'passed' : 'in_progress',
          started_at: now(), passed_at: s < stage ? now() : null, updated_at: now(),
        })
      }
      return founder_id
    },
    seed(table, founder_id, fields) {
      const row = { id: randomUUID(), founder_id, created_at: now(), ...fields }
      rows[table].push(row)
      return row
    },
    mintToken(founder_id, { ttlMs = 15 * 60 * 1000 } = {}) {
      const token = `tok_${randomUUID().replace(/-/g, '')}`
      tokens.set(token, { founder_id, session_id: randomUUID(), expires_at: new Date(Date.now() + ttlMs).toISOString() })
      return token
    },
    tokenInfo: token => tokens.get(token),
    rowsFor: (table, founder_id) => rows[table].filter(r => r.founder_id === founder_id),
  }

  const server = createServer((req, res) => {
    let raw = ''
    req.on('data', c => (raw += c))
    req.on('end', () => {
      requests.push({ method: req.method, url: req.url, auth: req.headers.authorization || '', body: raw })
      const send = (status, body) => {
        res.writeHead(status, body === undefined ? {} : { 'content-type': 'application/json' })
        res.end(body === undefined ? undefined : JSON.stringify(body))
      }
      const fail = (status, error) => send(status, { error, message: error })

      const token = (req.headers.authorization || '').replace(/^Bearer /, '')
      const info = tokens.get(token)
      if (!info) return fail(401, 'unauthorized')
      if (Date.parse(info.expires_at) <= Date.now() && !req.url.startsWith('/me')) return fail(403, 'token_expired')
      const fid = info.founder_id

      const url = new URL(req.url, 'http://mock')
      if (url.searchParams.has('founder_id')) return fail(400, 'founder_id_not_accepted')
      let body = {}
      if (raw) {
        try {
          body = JSON.parse(raw)
        } catch {
          return fail(400, 'bad_request')
        }
        if (hasFounderIdKey(body)) return fail(400, 'founder_id_not_accepted')
      }
      const mine = table => rows[table].filter(r => r.founder_id === fid)
      const owned = (table, id) => mine(table).find(r => r.id === id)
      const insert = (table, fields) => {
        const row = { id: randomUUID(), founder_id: fid, created_at: now(), ...fields }
        rows[table].push(row)
        return send(201, row)
      }
      const p = url.pathname
      const m = req.method

      if (m === 'GET' && p === '/me') return send(200, info)
      if (m === 'GET' && p === '/founder') return send(200, founders.get(fid))
      if (m === 'GET' && p === '/stage-progress') return send(200, { rows: mine('stage_progress') })
      const gp = /^\/stage-progress\/(\d+)\/gate-pending$/.exec(p)
      if (m === 'POST' && gp) {
        const row = mine('stage_progress').find(r => r.stage === Number(gp[1]))
        if (!row || founders.get(fid).current_stage !== row.stage) return fail(422, 'ledger_error')
        row.status = 'gate_pending'
        return send(200, row)
      }

      if (m === 'GET' && p === '/artifacts') {
        let list = mine('artifacts')
        if (url.searchParams.has('stage')) list = list.filter(r => r.stage === Number(url.searchParams.get('stage')))
        if (url.searchParams.has('kind')) list = list.filter(r => r.kind === url.searchParams.get('kind'))
        return send(200, { rows: list })
      }
      const art = /^\/artifacts\/([^/]+)$/.exec(p)
      if (m === 'GET' && art) {
        const row = owned('artifacts', decodeURIComponent(art[1]))
        return row ? send(200, row) : fail(404, 'not_found')
      }
      if (m === 'POST' && p === '/artifacts') {
        if (!body.kind || !body.body || !Number.isInteger(body.stage)) return fail(422, 'ledger_error')
        const version = mine('artifacts').filter(r => r.kind === body.kind).length + 1
        return insert('artifacts', { stage: body.stage, kind: body.kind, title: body.title ?? null, body: body.body, meta: body.meta ?? {}, version })
      }

      if (m === 'GET' && p === '/pains') return send(200, { rows: mine('pains') })
      if (m === 'POST' && p === '/pains') {
        if (!body.quote) return fail(422, 'ledger_error')
        return insert('pains', { quote: body.quote, job: body.job ?? null, tags: body.tags ?? [] })
      }
      if (m === 'GET' && p === '/interviews') return send(200, { rows: mine('interviews') })
      if (m === 'POST' && p === '/interviews') {
        if (!body.interviewee || !body.notes) return fail(422, 'ledger_error')
        return insert('interviews', { interviewee: body.interviewee, notes: body.notes, commitment: body.commitment ?? 'none', earlyvangelist: !!body.earlyvangelist })
      }

      if (m === 'GET' && p === '/audits') return send(200, { rows: mine('audits') })
      if (m === 'POST' && p === '/audits') {
        if (!AUDIT_TARGETS.has(body.target_table)) return fail(422, 'ledger_error')
        if (!owned(body.target_table, body.target_id)) return fail(404, 'not_found')
        return insert('audits', { target_table: body.target_table, target_id: body.target_id, auditor: body.auditor, check_name: body.check_name, verdict: body.verdict, findings: body.findings ?? null })
      }

      if (m === 'GET' && p === '/prfaq') return send(200, { rows: mine('prfaq_versions') })
      if (m === 'GET' && p === '/prfaq/latest') return send(200, mine('prfaq_versions').at(-1) ?? null)
      if (m === 'POST' && p === '/prfaq') {
        return insert('prfaq_versions', { stage: body.stage, body: body.body, assumptions: body.assumptions ?? [], version: mine('prfaq_versions').length })
      }

      if (m === 'GET' && p === '/gate-decisions') return send(200, { rows: mine('gate_decisions') })
      if (m === 'POST' && p === '/gate-decisions') {
        if (body.sean_signoff === true || body.decided_by === 'claude+sean') return fail(403, 'signoff_not_allowed')
        if (!Array.isArray(body.evidence) || body.evidence.length === 0) return fail(422, 'ledger_error')
        for (const ref of body.evidence) {
          if (!EVIDENCE_TABLES.has(ref?.table) || !owned(ref.table, ref.id)) return fail(404, 'not_found')
        }
        if (body.decision === 'pass' && body.stage >= 3) return fail(422, 'ledger_error')
        return insert('gate_decisions', { stage: body.stage, gate_id: body.gate_id, decision: body.decision, decided_by: body.decided_by, sean_signoff: false, evidence: body.evidence, rationale: body.rationale })
      }

      if (m === 'POST' && p === '/session/result') {
        if (body.session_id !== info.session_id) return fail(403, 'token_founder_mismatch')
        sessionResults.push({ founder_id: fid, ...body })
        tokens.delete(token)
        return send(204)
      }

      return fail(404, 'not_found')
    })
  })

  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  return {
    ...state,
    baseUrl: `http://127.0.0.1:${port}`,
    close: () => new Promise(resolve => server.close(resolve)),
  }
}
