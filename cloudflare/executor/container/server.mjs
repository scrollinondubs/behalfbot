// In-container HTTP shim for the Behalf.bot executor (issues #41 / #66).
//
// The Worker forwards trigger requests here (Container defaultPort 8080).
// One tick = one invocation of the vibecodelisboa heartbeat script, which
// does its own queue picking, single-flight DB locking, orphan sweep,
// spend caps, and workdir cleanup. This shim adds only:
//   - process-level single-flight (never two ticks in one container)
//   - 202-and-run-in-background so no caller ever holds a connection
//     open for a 15-20 min job
//   - a status endpoint so the Mac mini heartbeat (and later a Worker
//     cron) can observe the last run without touching the DB
//
// Security posture is inherited from the executor itself and is NOT
// re-implemented here: claude -p runs with --bare, the Read/Glob/Grep
// allowlist, the wide tool denylist, workdir scrub, git
// core.hooksPath=/dev/null, and the dedicated BEHALFBOT_ANTHROPIC_API_KEY
// (see src/lib/contribution-ledger/behalfbot-prompts.ts callClaude()).

import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { preScanPromotedARow } from './prescan-outcome.mjs'

const PORT = 8080
const REPO_ROOT = '/app/vibecodelisboa'
const TICK_SCRIPT = 'scripts/behalfbot-heartbeat.ts'
// Belt-and-braces above the executor's own 25-min CLI timeout and 30-min
// row wallclock cap. If tsx itself wedges, kill it.
const TICK_HARD_TIMEOUT_MS = 35 * 60 * 1000

let inFlight = null // { startedAt, mode } while a tick runs
let lastRun = null // { startedAt, endedAt, exitCode, mode, stdoutTail }

const server = createServer((req, res) => {
  const respond = (status, body) => {
    res.writeHead(status, { 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
  }

  if (req.method === 'GET' && req.url === '/healthz') {
    return respond(200, { ok: true, inFlight: inFlight !== null })
  }

  if (req.method === 'GET' && req.url === '/status') {
    return respond(200, { inFlight, lastRun })
  }

  if (req.method === 'POST' && (req.url === '/run' || req.url === '/run?mode=prescan')) {
    if (inFlight) {
      return respond(409, { started: false, reason: 'tick_in_flight', inFlight })
    }
    const mode = req.url.includes('mode=prescan') ? 'prescan' : 'executor'
    runTick(mode)
    return respond(202, { started: true, mode, startedAt: inFlight.startedAt })
  }

  respond(404, { error: 'not_found' })
})

server.listen(PORT, () => {
  console.log(JSON.stringify({ event: 'listening', port: PORT }))
})
