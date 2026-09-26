// Behalf.bot executor Worker stub (issues #41 / #66).
//
// Thin trigger in front of the executor container. The Mac mini heartbeat
// (and later a Worker cron) POSTs /trigger with a bearer token; we forward
// to the singleton container instance, which starts one queue tick and
// answers 202 immediately. No connection is ever held open for the job,
// so the Workers CPU limit is never in play - the 15-20 min work happens
// entirely inside the container.
//
// DO NOT DEPLOY without Sean's explicit approval (see README.md).

import { Container, getContainer } from '@cloudflare/containers'

interface Env {
  EXECUTOR_CONTAINER: DurableObjectNamespace<ExecutorContainer>
  // FounderOS sessions (behalfbot#215). A separate Container class and image
  // so the tenant-facing path never shares a process, env or filesystem with
  // the Asks executor's Turso and GitHub credentials.
  FOUNDER_OS_CONTAINER: DurableObjectNamespace<FounderOSContainer>
  // Bearer VCL's server side uses to start sessions. Distinct from
  // EXECUTOR_TRIGGER_TOKEN so the Mac mini poke cannot start one, and a VCL
  // leak cannot trigger Asks ticks.
  FOUNDER_OS_TRIGGER_TOKEN?: string
  // Plain var: base URL of the tenant-scoped ledger API
  // (docs/founder-os-ledger-api.md). Not a secret.
  FOUNDER_OS_VCL_API_BASE?: string
  // Shared secret the Mac mini poke authenticates with. Minted fresh for
  // this Worker; not reused from any other system.
  EXECUTOR_TRIGGER_TOKEN: string
  // The executor secrets (issue #41's set + RESEND_API_KEY per #73). Set
  // via `wrangler secret put`, passed into the container as env vars
  // below. Nothing else - no Vaultwarden, no Postgres, no SiYuan, no
  // Discord, no OAuth (issue #66 packaging boundary).
  DATABASE_URL: string
  DATABASE_AUTH_TOKEN: string
  ENCRYPTION_SECRET: string
  GITHUB_PAT: string
  BEHALFBOT_ANTHROPIC_API_KEY: string
  // Member notifications. Without it the processor's notify step throws at
  // module load (resend-client constructs at import time) AFTER the PR has
  // shipped, retroactively marking completed jobs failed (#73).
  RESEND_API_KEY: string
  // Build identity (issue #173). Set as plain vars at deploy time by
  // deploy.sh, which reads them out of build-context/build-info.json so
  // the Worker and the container image always report the same commit.
  // Optional on purpose: a hand-rolled `wrangler deploy` that skips
  // deploy.sh leaves them undefined and /healthz answers null, which is
  // what the executor-drift monitor should see. A deploy with no
  // provenance must look like a deploy with no provenance.
  APP_COMMIT?: string
  BUILT_AT?: string
}

export class ExecutorContainer extends Container<Env> {
  defaultPort = 8080
  // Must exceed the executor's 30-min per-ask wallclock cap (and the
  // shim's 35-min hard kill) so the idle reaper can never sleep a
  // container mid-job. Each /trigger resets the timer.
  sleepAfter = '45m'

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env)
    this.envVars = {
      DATABASE_URL: env.DATABASE_URL,
      DATABASE_AUTH_TOKEN: env.DATABASE_AUTH_TOKEN,
      ENCRYPTION_SECRET: env.ENCRYPTION_SECRET,
      GITHUB_PAT: env.GITHUB_PAT,
      BEHALFBOT_ANTHROPIC_API_KEY: env.BEHALFBOT_ANTHROPIC_API_KEY,
      RESEND_API_KEY: env.RESEND_API_KEY,
    }
  }
}

// FounderOS session container. Holds the dedicated Anthropic key and the
// ledger API base URL, and nothing else: no DATABASE_*, no GITHUB_PAT, no
// ENCRYPTION_SECRET. Ledger access is only through the per-session token VCL
// sends with each request, which is scoped to one founder_id.
export class FounderOSContainer extends Container<Env> {
  defaultPort = 8080
  // Above the shim's 10-min session cap so the reaper never sleeps an
  // instance mid-session.
  sleepAfter = '20m'

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env)
    this.envVars = {
      BEHALFBOT_ANTHROPIC_API_KEY: env.BEHALFBOT_ANTHROPIC_API_KEY,
      FOUNDER_OS_VCL_API_BASE: env.FOUNDER_OS_VCL_API_BASE ?? '',
    }
  }
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MAX_SESSION_BODY_BYTES = 256 * 1024

// Each founder gets their own container instance, named by founder_id, so
// one instance's filesystem and process only ever see one tenant.
async function handleFounderOs(request: Request, env: Env, url: URL): Promise<Response> {
  const auth = request.headers.get('authorization') ?? ''
  if (!env.FOUNDER_OS_TRIGGER_TOKEN || auth !== `Bearer ${env.FOUNDER_OS_TRIGGER_TOKEN}`) {
    return unauthorized()
  }

  if (request.method === 'POST' && url.pathname === '/founder-os/sessions') {
    const text = await request.text()
    if (text.length > MAX_SESSION_BODY_BYTES) {
      return Response.json({ error: 'body_too_large' }, { status: 413 })
    }
    let founderId: unknown
    try {
      founderId = (JSON.parse(text) as { founder_id?: unknown }).founder_id
    } catch {
      return Response.json({ error: 'bad_request' }, { status: 400 })
    }
    if (typeof founderId !== 'string' || !UUID_RE.test(founderId)) {
      return Response.json({ error: 'bad_request', message: 'founder_id must be a UUID' }, { status: 400 })
    }
    const container = getContainer(env.FOUNDER_OS_CONTAINER, founderId.toLowerCase())
    return container.fetch(
      new Request('http://container/session', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: text,
      }),
    )
  }

  if (request.method === 'GET' && url.pathname === '/founder-os/status') {
    const founderId = url.searchParams.get('founder_id') ?? ''
    if (!UUID_RE.test(founderId)) {
      return Response.json({ error: 'bad_request', message: 'founder_id must be a UUID' }, { status: 400 })
    }
    return getContainer(env.FOUNDER_OS_CONTAINER, founderId.toLowerCase()).fetch(
      new Request('http://container/status'),
    )
  }

  return Response.json({ error: 'not_found' }, { status: 404 })
}

function unauthorized(): Response {
  return Response.json({ error: 'unauthorized' }, { status: 401 })
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url)

    // Unauthenticated liveness probe for the Worker itself (does not wake
    // the container).
    //
    // It also carries build identity (issue #173). The container answers
    // the same fields on its own /healthz, but nothing routes to that
    // path: this handler returns before any container.fetch, deliberately,
    // so a probe never wakes a standard-1 instance. Reading the vars here
    // keeps the endpoint free and unauthenticated while still making
    // "which app commit is live" answerable, which is the whole point of
    // #173. Deploying without deploy.sh yields nulls rather than a lie.
    if (request.method === 'GET' && url.pathname === '/healthz') {
      return Response.json({
        ok: true,
        appCommit: env.APP_COMMIT ?? null,
        builtAt: env.BUILT_AT ?? null,
      })
    }

    // FounderOS routes carry their own bearer and never reach the Asks
    // singleton below.
    if (url.pathname.startsWith('/founder-os/')) {
      return handleFounderOs(request, env, url)
    }

    const auth = request.headers.get('authorization') ?? ''
    if (auth !== `Bearer ${env.EXECUTOR_TRIGGER_TOKEN}`) {
      return unauthorized()
    }

    // Singleton instance preserves the single-in-flight contract at the
    // instance level; the DB-level optimistic UPDATE remains the
    // authoritative lock (same as on the Mac mini today).
    const container = getContainer(env.EXECUTOR_CONTAINER, 'singleton')

    if (request.method === 'POST' && url.pathname === '/trigger') {
      const mode = url.searchParams.get('mode') === 'prescan' ? '?mode=prescan' : ''
      return container.fetch(new Request(`http://container/run${mode}`, { method: 'POST' }))
    }

    if (request.method === 'GET' && url.pathname === '/status') {
      return container.fetch(new Request('http://container/status'))
    }

    return Response.json({ error: 'not_found' }, { status: 404 })
  },
}
