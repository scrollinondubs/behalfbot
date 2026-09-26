// Laya on Cloudflare Containers (behalfbot#214, FounderOS epic
// behalfbot-plugins#22).
//
// One Worker in front of one LayaContainer Durable Object per checkpoint
// ("english", "multilingual"). Each DO owns its own laya-serve container,
// pinned to that checkpoint, and its own async job queue:
//
//   POST /v1/systemone    synchronous passthrough to laya-serve. Pays the cold
//                         start when that checkpoint's container is asleep.
//   POST /v1/jobs         queue a batch, answers 202 + job id at once
//   GET  /v1/jobs/:id     job status, plus results once every item is done
//   GET  /healthz         unauthenticated, never wakes a container
//
// Everything except /healthz needs `Authorization: Bearer <LAYA_API_TOKEN>`.
// The `model` field picks the instance; it defaults to english, the
// checkpoint that separates English pain posts (new-jaxity#603).
//
// DO NOT DEPLOY without Sean's explicit approval (see README.md).

import { Container, getContainer } from '@cloudflare/containers'

interface Env {
  LAYA_CONTAINER: DurableObjectNamespace<LayaContainer>
  // Set with `wrangler secret put LAYA_API_TOKEN`. Minted fresh for this
  // Worker and held by the VCL backend only.
  LAYA_API_TOKEN: string
}

type Checkpoint = 'english' | 'multilingual'

const PORT = 8080
const MODEL_HEADER = 'x-laya-model'
// Questions per laya call. Memory, not latency, sets this: a multilingual
// call with a 12k-char state and 16 questions peaked at 3.72 GiB of the
// 4 GiB standard-1 limit (README.md). 8 keeps a wide margin; callers split
// larger question sets across items.
const MAX_QUESTIONS = 8
// Items per batch job. A pain-tagging pass over one founder's Sales Safari
// log is tens to low hundreds of posts; 500 leaves room without letting one
// job hold an instance for hours.
const MAX_JOB_ITEMS = 500
const MAX_SYNC_BODY_BYTES = 2 * 1024 * 1024
const MAX_JOB_BODY_BYTES = 16 * 1024 * 1024
// A drain pass stops taking new items after this long and reschedules
// itself, so one alarm invocation never runs long enough to be killed.
const DRAIN_SLICE_MS = 60_000
// Transport failures (container would not start, connection dropped) are
// retried; HTTP errors from laya-serve are final and stored as the result.
const MAX_ITEM_ATTEMPTS = 3
const RETRY_DELAY_S = 30
const JOB_TTL_MS = 7 * 24 * 60 * 60 * 1000

type JobRow = {
  id: string
  status: 'queued' | 'running' | 'done'
  total: number
  completed: number
  created_at: number
  updated_at: number
  finished_at: number | null
}

type ItemRow = {
  idx: number
  status_code: number | null
  response: string | null
}

export class LayaContainer extends Container<Env> {
  defaultPort = PORT
  // Scale to zero after 10 idle minutes. Every proxied request, including
  // each item a job drain sends, resets the timer.
  sleepAfter = '10m'
  // Weights are baked into the image and HF_HUB_OFFLINE=1 is set there, so
  // the container has no reason to reach the internet at all.
  enableInternet = false

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env)
    ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS laya_jobs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        total INTEGER NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        finished_at INTEGER
      )`)
    ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS laya_job_items (
        job_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        request TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        status_code INTEGER,
        response TEXT,
        PRIMARY KEY (job_id, idx)
      )`)
  }

  // Requests arrive already authenticated and validated by the Worker.
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url)
    const model = request.headers.get(MODEL_HEADER)
    if (model !== 'english' && model !== 'multilingual') {
      return json({ error: 'missing checkpoint' }, 400)
    }
    // Remembered so a drain run from the alarm, with no request in hand,
    // starts the container on the right checkpoint.
    await this.ctx.storage.put('model', model)

    if (request.method === 'POST' && url.pathname === '/v1/systemone') {
      try {
        return await this.callLaya(await request.text())
      } catch (e) {
        return json({ error: 'container_unavailable', detail: String(e) }, 503)
      }
    }
    if (request.method === 'POST' && url.pathname === '/v1/jobs') {
      return this.submitJob(model, (await request.json()) as unknown[])
    }
    const m = url.pathname.match(/^\/v1\/jobs\/([a-z]+-[0-9a-f-]{36})$/)
    if (request.method === 'GET' && m) {
      return this.readJob(m[1])
    }
    return json({ error: 'not_found' }, 404)
  }

  // Start explicitly instead of letting containerFetch do it: its default
  // port wait is 20 s, and a cold start on the real 1/2 vCPU may need more
  // (4.6 s english / 6.6 s multilingual on local arm64, 47 s under x86
  // emulation). Generous limits cost nothing when the start is fast.
  private async ensureStarted(): Promise<void> {
    const state = await this.getState()
    if (this.ctx.container?.running && state.status === 'healthy') return
    const model = (await this.ctx.storage.get<Checkpoint>('model')) ?? 'english'
    this.envVars = { LAYA_MODEL: model }
    await this.startAndWaitForPorts({
      ports: PORT,
      cancellationOptions: {
        instanceGetTimeoutMS: 60_000,
        portReadyTimeoutMS: 300_000,
      },
    })
  }

  private async callLaya(body: string): Promise<Response> {
    await this.ensureStarted()
    return this.containerFetch(
      new Request('http://container/v1/systemone', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body,
      }),
    )
  }

  private async submitJob(model: Checkpoint, items: unknown[]): Promise<Response> {
    const now = Date.now()
    this.purgeExpired(now)
    const id = `${model}-${crypto.randomUUID()}`
    const sql = this.ctx.storage.sql
    sql.exec(
      `INSERT INTO laya_jobs (id, status, total, created_at, updated_at) VALUES (?, 'queued', ?, ?, ?)`,
      id, items.length, now, now,
    )
    items.forEach((it, idx) => {
      sql.exec(
        `INSERT INTO laya_job_items (job_id, idx, request) VALUES (?, ?, ?)`,
        id, idx, JSON.stringify(it),
      )
    })
    await this.scheduleDrainOnce(0)
    return json({ id, status: 'queued', total: items.length, poll: `/v1/jobs/${id}` }, 202)
  }

  private readJob(id: string): Response {
    const sql = this.ctx.storage.sql
    const job = sql.exec<JobRow>(`SELECT * FROM laya_jobs WHERE id = ?`, id).toArray()[0]
    if (!job) return json({ error: 'not_found' }, 404)
    const out: Record<string, unknown> = {
      id: job.id,
      status: job.status,
      total: job.total,
      completed: job.completed,
      created_at: new Date(job.created_at).toISOString(),
      finished_at: job.finished_at ? new Date(job.finished_at).toISOString() : null,
    }
    if (job.status === 'done') {
      out.results = sql
        .exec<ItemRow>(
          `SELECT idx, status_code, response FROM laya_job_items WHERE job_id = ? ORDER BY idx`,
          id,
        )
        .toArray()
        .map((r) => ({ index: r.idx, status: r.status_code, body: parseOrText(r.response) }))
    }
    return json(out)
  }

  // Scheduled callback. Container.schedule runs it from the DO alarm, so a
  // drain outlives the request that queued it and survives DO eviction: the
  // schedule row is deleted only after the callback returns.
  async drain(): Promise<void> {
    const sql = this.ctx.storage.sql
    const started = Date.now()
    while (Date.now() - started < DRAIN_SLICE_MS) {
      const next = sql
        .exec<{ job_id: string; idx: number; request: string; attempts: number }>(
          `SELECT i.job_id, i.idx, i.request, i.attempts
             FROM laya_job_items i JOIN laya_jobs j ON j.id = i.job_id
            WHERE i.status_code IS NULL
            ORDER BY j.created_at, i.idx
            LIMIT 1`,
        )
        .toArray()[0]
      if (!next) return

      sql.exec(
        `UPDATE laya_jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'`,
        Date.now(), next.job_id,
      )

      let status: number
      let text: string
      try {
        const res = await this.callLaya(next.request)
        status = res.status
        text = await res.text()
      } catch (e) {
        status = 503
        text = JSON.stringify({ error: 'container_unavailable', detail: String(e) })
      }

      if (isStartFailure(status, text) && next.attempts + 1 < MAX_ITEM_ATTEMPTS) {
        sql.exec(
          `UPDATE laya_job_items SET attempts = attempts + 1 WHERE job_id = ? AND idx = ?`,
          next.job_id, next.idx,
        )
        await this.scheduleDrainOnce(RETRY_DELAY_S)
        return
      }

      const now = Date.now()
      sql.exec(
        `UPDATE laya_job_items SET status_code = ?, response = ?, attempts = attempts + 1
          WHERE job_id = ? AND idx = ?`,
        status, text, next.job_id, next.idx,
      )
      sql.exec(
        `UPDATE laya_jobs SET completed = completed + 1, updated_at = ? WHERE id = ?`,
        now, next.job_id,
      )
      sql.exec(
        `UPDATE laya_jobs SET status = 'done', finished_at = ? WHERE id = ? AND completed >= total`,
        now, next.job_id,
      )
    }
    await this.scheduleDrainOnce(0)
  }

  // The alarm runs callbacks one at a time, so an extra row only costs an
  // empty pass, but a burst of submits should not pile them up. The row of
  // the drain currently executing is still present (it is deleted after the
  // callback returns), so allow exactly one more.
  private async scheduleDrainOnce(delaySeconds: number): Promise<void> {
    const pending = this.ctx.storage.sql
      .exec<{ n: number }>(`SELECT COUNT(*) AS n FROM container_schedules WHERE callback = 'drain'`)
      .toArray()[0]
    if ((pending?.n ?? 0) >= 2) return
    await this.schedule(delaySeconds, 'drain')
  }

  private purgeExpired(now: number): void {
    const cutoff = now - JOB_TTL_MS
    const sql = this.ctx.storage.sql
    sql.exec(
      `DELETE FROM laya_job_items WHERE job_id IN
         (SELECT id FROM laya_jobs WHERE status = 'done' AND finished_at < ?)`,
      cutoff,
    )
    sql.exec(`DELETE FROM laya_jobs WHERE status = 'done' AND finished_at < ?`, cutoff)
  }
}

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status })
}

// Transport-level failures: containerFetch turns a failed start into a 5xx
// Response rather than a throw, and callLaya throws from ensureStarted. Both
// are worth a retry. An HTTP error from laya-serve itself (400, 413, 422) is
// the real answer for that item and is stored as-is.
function isStartFailure(status: number, text: string): boolean {
  if (status === 429) return true
  if (status < 500) return false
  return (
    text.startsWith('Failed to start') ||
    text.startsWith('There is no Container instance') ||
    text.includes('"container_unavailable"')
  )
}

function parseOrText(s: string | null): unknown {
  if (s === null) return null
  try {
    return JSON.parse(s)
  } catch {
    return s
  }
}

// Same names laya-serve accepts. Anything else (including no model) goes to
// english; typed-decisions is not baked into this image.
function checkpointFor(model: unknown): Checkpoint | null {
  if (model === 'multilingual' || model === 'convaiinnovations/laya-multilingual') return 'multilingual'
  if (model === 'typed-decisions' || model === 'convaiinnovations/laya-typed-decisions') return null
  return 'english'
}

// Returns an error message, or null when the call is acceptable.
function checkCall(call: unknown, where: string): string | null {
  if (!call || typeof call !== 'object' || Array.isArray(call)) return `${where} must be an object`
  const q = (call as { questions?: unknown }).questions
  if (!q || typeof q !== 'object' || Array.isArray(q)) return `${where} needs a 'questions' object`
  const n = Object.keys(q).length
  if (n === 0) return `${where} has no questions`
  if (n > MAX_QUESTIONS) return `${where} has ${n} questions; the limit is ${MAX_QUESTIONS} per call`
  return null
}

async function readJsonCapped(request: Request, max: number): Promise<unknown | Response> {
  const declared = Number(request.headers.get('content-length') ?? '0')
  if (declared > max) return json({ error: 'request body too large' }, 413)
  const buf = await request.arrayBuffer()
  if (buf.byteLength > max) return json({ error: 'request body too large' }, 413)
  try {
    return JSON.parse(new TextDecoder().decode(buf))
  } catch {
    return json({ error: 'request body must be valid JSON' }, 400)
  }
}

async function tokenMatches(supplied: string, expected: string): Promise<boolean> {
  const enc = new TextEncoder()
  const [a, b] = await Promise.all([
    crypto.subtle.digest('SHA-256', enc.encode(supplied)),
    crypto.subtle.digest('SHA-256', enc.encode(expected)),
  ])
  return crypto.subtle.timingSafeEqual(a, b)
}

function forward(env: Env, model: Checkpoint, path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  headers.set(MODEL_HEADER, model)
  return getContainer(env.LAYA_CONTAINER, model).fetch(
    new Request(`http://laya${path}`, { ...init, headers }),
  )
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url)

    if (request.method === 'GET' && url.pathname === '/healthz') {
      return json({ ok: true })
    }

    // Fail closed if the secret was never set: an empty token must not turn
    // "Bearer " into a valid credential.
    if (!env.LAYA_API_TOKEN) return json({ error: 'unauthorized' }, 401)
    const auth = request.headers.get('authorization') ?? ''
    if (!(await tokenMatches(auth, `Bearer ${env.LAYA_API_TOKEN}`))) {
      return json({ error: 'unauthorized' }, 401)
    }

    if (request.method === 'POST' && url.pathname === '/v1/systemone') {
      const body = await readJsonCapped(request, MAX_SYNC_BODY_BYTES)
      if (body instanceof Response) return body
      const bad = checkCall(body, 'body')
      if (bad) return json({ error: bad }, 400)
      const model = checkpointFor((body as { model?: unknown }).model)
      if (!model) return json({ error: 'checkpoint not served here' }, 400)
      return forward(env, model, '/v1/systemone', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      })
    }

    if (request.method === 'POST' && url.pathname === '/v1/jobs') {
      const body = await readJsonCapped(request, MAX_JOB_BODY_BYTES)
      if (body instanceof Response) return body
      const { model: rawModel, items } = (body ?? {}) as { model?: unknown; items?: unknown }
      if (!Array.isArray(items) || items.length === 0) {
        return json({ error: "body must be an object with a non-empty 'items' array" }, 400)
      }
      if (items.length > MAX_JOB_ITEMS) {
        return json({ error: `too many items (${items.length} > ${MAX_JOB_ITEMS})` }, 413)
      }
      for (let i = 0; i < items.length; i++) {
        const bad = checkCall(items[i], `items[${i}]`)
        if (bad) return json({ error: bad }, 400)
      }
      const model = checkpointFor(rawModel)
      if (!model) return json({ error: 'checkpoint not served here' }, 400)
      // The job's checkpoint wins over any per-item model: one job, one
      // instance.
      const pinned = items.map((it) => ({ ...(it as object), model }))
      return forward(env, model, '/v1/jobs', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(pinned),
      })
    }

    const m = url.pathname.match(/^\/v1\/jobs\/((english|multilingual)-[0-9a-f-]{36})$/)
    if (request.method === 'GET' && m) {
      return forward(env, m[2] as Checkpoint, `/v1/jobs/${m[1]}`)
    }

    return json({ error: 'not_found' }, 404)
  },
}
