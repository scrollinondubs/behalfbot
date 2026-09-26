// In-container HTTP shim for FounderOS sessions (behalfbot#215).
//
// Runs in FounderOSContainer, a separate Container class and image from the
// Asks executor. The Worker names each instance by founder_id, so one
// instance only ever serves one founder. This shim adds:
//   - a founder binding: the first founder_id an instance sees is the only
//     one it will ever serve, a belt under the Worker's routing
//   - one session in flight per instance (409 otherwise)
//   - synchronous preflight (4xx/503 straight back to VCL), then 202 and the
//     claude run in the background, same no-held-connection rule as Asks
//
// This process has no database or GitHub credential in its env. The only
// secret is BEHALFBOT_ANTHROPIC_API_KEY, and claude's child env is an
// allowlist that carries that key and nothing else of value.

import { createServer } from 'node:http'
import { pathToFileURL } from 'node:url'
import { readPin } from './plugin.mjs'
import { SessionError, executeSession, prepareSession } from './session.mjs'

const MAX_BODY_BYTES = 256 * 1024

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', chunk => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new SessionError(413, 'body_too_large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

export function createFounderOsServer(deps) {
  let boundFounderId = null
  let inFlight = null // { startedAt, sessionId, skill }
  let lastSession = null // { startedAt, endedAt, sessionId, skill, status, error, writes }
  let lastDone = Promise.resolve()

  const server = createServer(async (req, res) => {
    const respond = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json' })
      res.end(JSON.stringify(body))
    }

    if (req.method === 'GET' && req.url === '/healthz') {
      const pin = readPin(deps.pluginDir)
      return respond(200, { ok: true, inFlight: inFlight !== null, plugin: pin.pinned ? { tag: pin.tag, sha: pin.sha } : null })
    }

    if (req.method === 'GET' && req.url === '/status') {
      return respond(200, { inFlight, lastSession })
    }

    if (req.method === 'POST' && req.url === '/session') {
      if (inFlight) return respond(409, { started: false, error: 'session_in_flight', inFlight })
      let session
      try {
        const body = JSON.parse(await readBody(req))
        if (boundFounderId && body?.founder_id !== boundFounderId) {
          throw new SessionError(409, 'instance_bound_to_other_founder')
        }
        // Claim the slot before the first await on the network, so two
        // requests racing through preflight cannot both start.
        if (inFlight) throw new SessionError(409, 'session_in_flight')
        inFlight = { startedAt: new Date().toISOString(), sessionId: null, skill: body?.skill ?? null }
        try {
          session = await prepareSession(body, deps)
        } catch (err) {
          inFlight = null
          throw err
        }
      } catch (err) {
        if (err instanceof SyntaxError) return respond(400, { started: false, error: 'bad_request' })
        const status = err instanceof SessionError ? err.status : 500
        return respond(status, { started: false, error: err.code || 'internal_error', message: err.message })
      }

      boundFounderId = session.req.founderId
      inFlight.sessionId = session.sessionId
      const startedAt = inFlight.startedAt
      lastDone = executeSession(session, deps).then(outcome => {
        lastSession = {
          startedAt,
          endedAt: new Date().toISOString(),
          sessionId: session.sessionId,
          skill: session.skill.name,
          status: outcome.status,
          error: outcome.error ?? null,
          writes: outcome.writes.map(w => ({ method: w.method, status: w.status })),
        }
        inFlight = null
        console.log(JSON.stringify({ event: 'founder_os_session_done', ...lastSession }))
        return outcome
      })
      return respond(202, { started: true, sessionId: session.sessionId, startedAt })
    }

    respond(404, { error: 'not_found' })
  })

  return { server, settled: () => lastDone }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { server } = createFounderOsServer({
    pluginDir: process.env.FOUNDER_OS_PLUGIN_DIR || '/app/founder-os-plugin',
    workRoot: process.env.FOUNDER_OS_WORK_ROOT || '/tmp/founder-os-work',
    apiBase: process.env.FOUNDER_OS_VCL_API_BASE,
    apiKey: process.env.BEHALFBOT_ANTHROPIC_API_KEY,
    model: process.env.FOUNDER_OS_CLAUDE_MODEL || undefined,
    claudeBin: process.env.FOUNDER_OS_CLAUDE_BIN || 'claude',
  })
  server.listen(8080, () => console.log(JSON.stringify({ event: 'listening', port: 8080 })))
}
