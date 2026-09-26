// Tenant-scoped client for the VCL FounderOS ledger API (behalfbot#215).
//
// Contract: docs/founder-os-ledger-api.md. The client holds one session token
// and nothing else - no database credential, no founder_id of its own choosing.
// VCL derives the tenant from the token. This side adds two tripwires:
//   - it refuses to send a founder_id anywhere in a request, so no caller
//     (and no model output routed through a caller) can name a tenant
//   - once bound, it aborts on any response row whose founder_id differs

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function isUuid(value) {
  return typeof value === 'string' && UUID_RE.test(value)
}

export class LedgerApiError extends Error {
  constructor(status, code, message) {
    super(message || code)
    this.name = 'LedgerApiError'
    this.status = status
    this.code = code
  }
}

export function containsFounderIdKey(value) {
  if (Array.isArray(value)) return value.some(containsFounderIdKey)
  if (value && typeof value === 'object') {
    for (const [key, inner] of Object.entries(value)) {
      if (key === 'founder_id') return true
      if (containsFounderIdKey(inner)) return true
    }
  }
  return false
}

function assertBaseUrl(baseUrl) {
  let parsed
  try {
    parsed = new URL(baseUrl)
  } catch {
    throw new LedgerApiError(0, 'bad_base_url', 'FOUNDER_OS_VCL_API_BASE is not a URL')
  }
  const local = parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost'
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && local)) {
    throw new LedgerApiError(0, 'bad_base_url', 'the ledger API must be https')
  }
  return parsed.href.replace(/\/+$/, '')
}

function query(params) {
  const qs = new URLSearchParams()
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null) qs.set(key, String(value))
  }
  const s = qs.toString()
  return s ? `?${s}` : ''
}

function idSegment(id) {
  if (!isUuid(id)) throw new LedgerApiError(0, 'bad_id', 'row ids are UUIDs')
  return encodeURIComponent(id)
}

export function createLedgerClient({ baseUrl, token, fetchImpl = globalThis.fetch }) {
  const base = assertBaseUrl(baseUrl)
  if (typeof token !== 'string' || token.length === 0) {
    throw new LedgerApiError(0, 'no_token', 'a session token is required')
  }
  let boundFounderId = null

  function checkRows(payload) {
    if (boundFounderId === null || payload === null || payload === undefined) return
    const rows = Array.isArray(payload.rows) ? payload.rows : [payload]
    for (const row of rows) {
      if (row && typeof row === 'object' && 'founder_id' in row && row.founder_id !== boundFounderId) {
        throw new LedgerApiError(0, 'tenant_mismatch', 'the ledger API returned another founder\'s row')
      }
    }
  }

  async function call(method, path, body) {
    if (body !== undefined && containsFounderIdKey(body)) {
      throw new LedgerApiError(0, 'founder_id_not_accepted', 'requests never carry a founder_id')
    }
    const res = await fetchImpl(`${base}${path}`, {
      method,
      headers: {
        authorization: `Bearer ${token}`,
        ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    })
    if (res.status === 204) return null
    const text = await res.text()
    let payload = null
    if (text) {
      try {
        payload = JSON.parse(text)
      } catch {
        throw new LedgerApiError(res.status, 'bad_response', 'the ledger API returned non-JSON')
      }
    }
    if (!res.ok) {
      throw new LedgerApiError(res.status, payload?.error || 'http_error', payload?.message)
    }
    checkRows(payload)
    return payload
  }

  const rows = payload => (payload && Array.isArray(payload.rows) ? payload.rows : [])

  return {
    get boundFounderId() {
      return boundFounderId
    },
    bind(founderId) {
      if (!isUuid(founderId)) throw new LedgerApiError(0, 'bad_id', 'founder_id is a UUID')
      boundFounderId = founderId
    },

    whoami: () => call('GET', '/me'),
    getFounder: () => call('GET', '/founder'),
    listStageProgress: async () => rows(await call('GET', '/stage-progress')),
    markGatePending: ({ stage }) =>
      call('POST', `/stage-progress/${Number.isInteger(stage) ? stage : 'x'}/gate-pending`),

    listArtifacts: async (params = {}) => rows(await call('GET', `/artifacts${query(params)}`)),
    getArtifact: id => call('GET', `/artifacts/${idSegment(id)}`),
    addArtifact: args => call('POST', '/artifacts', args),

    listPains: async (params = {}) => rows(await call('GET', `/pains${query(params)}`)),
    addPain: args => call('POST', '/pains', args),

    listInterviews: async () => rows(await call('GET', '/interviews')),
    addInterview: args => call('POST', '/interviews', args),

    listAudits: async (params = {}) => rows(await call('GET', `/audits${query(params)}`)),
    addAudit: args => call('POST', '/audits', args),

    listPrfaqVersions: async () => rows(await call('GET', '/prfaq')),
    latestPrfaq: () => call('GET', '/prfaq/latest'),
    addPrfaqVersion: args => call('POST', '/prfaq', args),

    listGateDecisions: async (params = {}) => rows(await call('GET', `/gate-decisions${query(params)}`)),
    recordGateDecision: args => call('POST', '/gate-decisions', args),

    postSessionResult: args => call('POST', '/session/result', args),
  }
}
