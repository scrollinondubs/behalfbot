// Idle gate for /trigger. The Mac mini heartbeats POST /trigger every 15-30
// min, which is shorter than ExecutorContainer's 45m sleepAfter, so waking the
// container blindly kept a 4 GiB standard-1 instance billed ~19 h/day while
// almost every tick found nothing to do. Asking Turso first lets an empty
// queue answer from the Worker and the container stays asleep.
//
// Kept free of @cloudflare/containers imports (like healthz.ts) so node:test
// can load it directly.
//
// Fail open: any doubt about the answer wakes the container exactly as before
// this gate existed. Only a positively parsed "no work" skips.

export type TickMode = 'executor' | 'prescan'

// The status each heartbeat script picks (vibecodelisboa scripts/
// behalfbot-heartbeat.ts and behalfbot-prescan-heartbeat.ts). Both scripts
// first run sweepOrphanedProcessingRows over 'processing' rows, so any
// 'processing' row also means the tick has work.
const PICK_STATUS: Record<TickMode, string> = {
  executor: 'pending_execution',
  prescan: 'pending_plan',
}

const SQL =
  "SELECT EXISTS(SELECT 1 FROM behalfbot_queue WHERE status IN (?, 'processing')) AS has_work"

export const QUEUE_CHECK_TIMEOUT_MS = 3000

export interface QueueDb {
  url: string | undefined
  authToken: string | undefined
}

export type QueueCheck = { hasWork: boolean } | { error: string }

export function pipelineUrl(databaseUrl: string): string {
  // The URL setter refuses to turn a non-special scheme like libsql: into
  // https:, so swap the scheme in the string before parsing.
  const scheme = databaseUrl.slice(0, databaseUrl.indexOf(':') + 1).toLowerCase()
  const http = { 'libsql:': 'https:', 'wss:': 'https:', 'https:': 'https:', 'ws:': 'http:', 'http:': 'http:' }[scheme]
  if (!http) throw new Error(`unsupported DATABASE_URL scheme ${scheme || '(none)'}`)
  const u = new URL(http + databaseUrl.slice(scheme.length))
  return `${u.origin}/v2/pipeline`
}

export async function checkQueue(
  db: QueueDb,
  mode: TickMode,
  fetchImpl: typeof fetch = fetch,
  timeoutMs: number = QUEUE_CHECK_TIMEOUT_MS,
): Promise<QueueCheck> {
  try {
    if (!db.url || !db.authToken) return { error: 'database credentials missing' }
    const res = await fetchImpl(pipelineUrl(db.url), {
      method: 'POST',
      headers: {
        authorization: `Bearer ${db.authToken}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        requests: [
          { type: 'execute', stmt: { sql: SQL, args: [{ type: 'text', value: PICK_STATUS[mode] }] } },
          { type: 'close' },
        ],
      }),
      signal: AbortSignal.timeout(timeoutMs),
    })
    if (!res.ok) return { error: `turso HTTP ${res.status}` }
    const body = (await res.json()) as any
    const first = body?.results?.[0]
    if (first?.type !== 'ok') {
      return { error: `turso query error: ${first?.error?.message ?? 'unexpected result'}` }
    }
    const cell = first?.response?.result?.rows?.[0]?.[0]
    const v = cell && typeof cell === 'object' ? cell.value : undefined
    if (v === '0' || v === 0) return { hasWork: false }
    if (v === '1' || v === 1) return { hasWork: true }
    return { error: 'turso response had no has_work value' }
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) }
  }
}

export async function gatedTrigger(
  db: QueueDb,
  mode: TickMode,
  wake: () => Promise<Response>,
  fetchImpl: typeof fetch = fetch,
  timeoutMs: number = QUEUE_CHECK_TIMEOUT_MS,
): Promise<Response> {
  const check = await checkQueue(db, mode, fetchImpl, timeoutMs)
  if ('error' in check) {
    console.error(JSON.stringify({ event: 'queue_gate_fail_open', mode, error: check.error }))
    return wake()
  }
  if (!check.hasWork) {
    console.log(JSON.stringify({ event: 'queue_gate_skipped', mode }))
    return Response.json({ ok: true, skipped: 'queue_empty', mode })
  }
  return wake()
}
