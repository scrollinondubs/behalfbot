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
  // Shared secret the Mac mini poke authenticates with. Minted fresh for
  // this Worker; not reused from any other system.
  EXECUTOR_TRIGGER_TOKEN: string
  // The five executor secrets (issue #41's set). Set via `wrangler secret
  // put`, passed into the container as env vars below. Nothing else - no
  // Vaultwarden, no Postgres, no SiYuan, no Discord, no OAuth (issue #66
  // packaging boundary).
  DATABASE_URL: string
  DATABASE_AUTH_TOKEN: string
  ENCRYPTION_SECRET: string
  GITHUB_PAT: string
  BEHALFBOT_ANTHROPIC_API_KEY: string
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
    }
  }
}

function unauthorized(): Response {
  return Response.json({ error: 'unauthorized' }, { status: 401 })
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url)

    // Unauthenticated liveness probe for the Worker itself (does not wake
    // the container).
    if (request.method === 'GET' && url.pathname === '/healthz') {
      return Response.json({ ok: true })
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
