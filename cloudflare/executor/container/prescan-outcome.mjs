// Reading the pre-scan's own summary line.
//
// Extracted from server.mjs so it can be unit tested. server.mjs starts an
// HTTP listener on import, so anything defined there is untestable without
// standing up a server - and a predicate that decides whether to spend money
// on a build is not one to leave unverified.

/**
 * Did this pre-scan tick promote a row to pending_execution?
 *
 * The pre-scan prints one JSON line summarising the tick. When it has planned
 * an ask and moved it on, that line carries outcome.kind === 'ready':
 *
 *   {"ranAt":...,"picked":"tit0ps...","outcome":{"kind":"ready",...}}
 *
 * Parsed out of the stdout tail rather than exposed as a separate signal
 * because the tail is already captured, and adding a second channel between
 * these two processes means two things to keep in sync.
 *
 * Deliberately conservative: anything unparseable returns false, so a change
 * to that log line degrades to the old behaviour (wait for the next poke)
 * rather than firing builds nobody asked for.
 */
export function preScanPromotedARow(tail) {
  if (typeof tail !== 'string' || tail === '') return false
  for (const line of tail.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('{')) continue
    try {
      const parsed = JSON.parse(trimmed)
      if (parsed?.outcome?.kind === 'ready') return true
    } catch {
      // not the summary line
    }
  }
  return false
}

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

    // Second hop. A pre-scan that promoted a row leaves work that only an
    // executor tick will do, and until now nothing started it: the pre-scan
    // window is 09:00-18:00 and the executor's is 02:00-06:00, so an ask
    // planned at 09:05 sat until the small hours. On 2026-08-18 the gap was
    // closed by a human poking the endpoint by hand between the two.
    //
    // It cannot be fired by the caller a moment later either - this container
    // is single-flight and answers 409 while a tick runs, which it still is at
    // the point the pre-scan finishes. Here, on the exit event, is the first
    // moment the second tick can actually start, which is why it lives in the
    // shim rather than in the Worker or the web app.
    //
    // Only on a clean exit. A pre-scan that crashed has not promoted anything
    // it can vouch for, whatever it managed to print first.
    if (mode === 'prescan' && code === 0 && preScanPromotedARow(tail)) {
      console.log(JSON.stringify({ event: 'chaining_to_executor', after: 'prescan' }))
      runTick('executor')
    }
  })
}
