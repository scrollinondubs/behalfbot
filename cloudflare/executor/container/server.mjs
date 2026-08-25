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
import { readFileSync } from 'node:fs'
import { preScanPromotedARow } from './prescan-outcome.mjs'

const PORT = 8080
const REPO_ROOT = '/app/vibecodelisboa'

// Written by build.sh (issue #173) before it strips vibecodelisboa's .git.
// Absent on a local/dev run that skipped build.sh - healthz still answers,
// just with null identity, rather than crashing the shim.
let buildInfo = { appCommit: null, builtAt: null }
try {
  buildInfo = JSON.parse(readFileSync('/app/build-info.json', 'utf8'))
} catch {
  console.log(JSON.stringify({ event: 'build_info_missing' }))
}

const TICK_SCRIPT = 'scripts/behalfbot-heartbeat.ts'
// Belt-and-braces above the executor's own 25-min CLI timeout and 30-min
// row wallclock cap. If tsx itself wedges, kill it.
const TICK_HARD_TIMEOUT_MS = 35 * 60 * 1000

let inFlight = null // { startedAt, mode } while a tick runs
let lastRun = null // { startedAt, endedAt, exitCode, mode, stdoutTail }

// Restored in #174. #166 deleted this function while adding the chaining it was
// supposed to call, and nothing replaced it, so the shim crashed on the first
// /run with `ReferenceError: runTick is not defined` and took the container
// down with it. The image also could not boot at all (missing module), so the
// two faults masked each other: the container died before the crash could be
// reached, and once it booted, the first request killed it.
function runTick(mode) {
  const startedAt = new Date().toISOString()
  const script = mode === 'prescan' ? 'scripts/behalfbot-prescan-heartbeat.ts' : TICK_SCRIPT
  const child = spawn(`${REPO_ROOT}/node_modules/.bin/tsx`, [script], {
    cwd: REPO_ROOT,
    env: process.env,
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  let tail = ''
  const keepTail = chunk => {
    tail = (tail + chunk.toString()).slice(-4000)
  }
  child.stdout.on('data', keepTail)
  child.stderr.on('data', keepTail)

  const killer = setTimeout(() => {
    try {
      child.kill('SIGKILL')
    } catch {
      // best-effort
    }
  }, TICK_HARD_TIMEOUT_MS)

  inFlight = { startedAt, mode }
  child.on('exit', code => {
    clearTimeout(killer)
    lastRun = {
      startedAt,
      endedAt: new Date().toISOString(),
      exitCode: code,
      mode,
      stdoutTail: tail,
    }
    inFlight = null
    console.log(JSON.stringify({ event: 'tick_done', ...lastRun, stdoutTail: undefined }))

    // The chaining #166 was written for: a pre-scan that promoted a row to
    // pending_execution leaves work only an executor tick will do, and the two
    // heartbeats run in disjoint windows, so without this the ask waits until
    // the small hours. The caller cannot do it - the container answers 409
    // while a tick runs, and it still is one at the moment the pre-scan
    // finishes. This exit event is the first instant a second tick can start.
    //
    // Conservative by construction: only a clean exit chains, and anything
    // unparseable returns false, degrading to "wait for the next poke" rather
    // than firing builds nobody asked for. A clarification never chains - the
    // ask is waiting on a human, and building would spend the budget answering
    // a question nobody has answered.
    if (mode === 'prescan' && code === 0 && preScanPromotedARow(tail)) {
      console.log(JSON.stringify({ event: 'prescan_chained_to_executor', startedAt }))
      runTick('executor')
    }
  })
}

const server = createServer((req, res) => {
  const respond = (status, body) => {
    res.writeHead(status, { 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
  }

  if (req.method === 'GET' && req.url === '/healthz') {
    return respond(200, {
      ok: true,
      inFlight: inFlight !== null,
      appCommit: buildInfo.appCommit,
      builtAt: buildInfo.builtAt,
    })
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
