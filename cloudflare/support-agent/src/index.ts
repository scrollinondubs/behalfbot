// Behalf.bot support-agent Worker stub (issue #66).
//
// Thin trigger in front of the support container. An intake surface (the
// Discord bridge relay, a future web form) POSTs /ask with a bearer token
// and a JSON body; we forward to the container, which runs one claude -p
// answer and responds synchronously. Support answers are minutes, not the
// executor's 15-20 min jobs, so request/response is fine; if a proxy
// timeout ever bites, fall back to the executor's 202+poll pattern.
//
// DO NOT DEPLOY without Sean's explicit approval (see README.md).
//
// TRUST BOUNDARY (issue #66): this Worker's env holds exactly two
// secrets, listed below. It is a SEPARATE Worker from behalfbot-executor
// so the executor's credentials (GITHUB_PAT, Turso, ENCRYPTION_SECRET)
// structurally cannot appear here. Nothing in this env can reach
// Vaultwarden, Postgres, SiYuan, the Discord-bridge identity, or any
// OAuth token of Sean's.

import { Container, getContainer } from '@cloudflare/containers'

interface Env {
  SUPPORT_CONTAINER: DurableObjectNamespace<SupportContainer>
  // Shared secret the intake surface authenticates with. Minted fresh for
  // this Worker; not reused from any other system (and NOT the executor's
  // EXECUTOR_TRIGGER_TOKEN).
  SUPPORT_TRIGGER_TOKEN: string
  // Dedicated Anthropic API key. Billing NEVER touches Sean's Max
  // subscription. This is the ONLY secret the container ever sees.
  BEHALFBOT_ANTHROPIC_API_KEY: string
}

export class SupportContainer extends Container<Env> {
  defaultPort = 8080
  // Support asks run minutes, not tens of minutes; the shim hard-kills at
  // 10 min. 15 idle minutes then scale-to-zero.
  sleepAfter = '15m'

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env)
    // The complete container env. One key. Nothing else crosses the
    // boundary.
    this.envVars = {
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
    if (auth !== `Bearer ${env.SUPPORT_TRIGGER_TOKEN}`) {
      return unauthorized()
    }

    const container = getContainer(env.SUPPORT_CONTAINER, 'singleton')

    if (request.method === 'POST' && url.pathname === '/ask') {
      // Body: { "question": string, "context"?: string } - context is the
      // caller-supplied prior-turn transcript (see README: the container
      // is stateless; continuity is the intake surface's job).
      return container.fetch(
        new Request('http://container/ask', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: request.body,
        })
      )
    }

    if (request.method === 'GET' && url.pathname === '/status') {
      return container.fetch(new Request('http://container/status'))
    }

    return Response.json({ error: 'not_found' }, { status: 404 })
  },
}
