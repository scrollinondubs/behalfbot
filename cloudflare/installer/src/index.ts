// behalfbot-installer: managed installs (behalfbot#265). Spec:
// CONTRACT.md in the managed-installs plan; this file is the only one that
// imports Cloudflare runtime modules, everything else is plain TS the node
// tests can load.
//
// DO NOT `wrangler deploy` or `wrangler secret put` without Sean's explicit
// approval (README.md).

import { DurableObject } from 'cloudflare:workers'
import { Container, getContainer } from '@cloudflare/containers'
import { InstallCore, type Probe } from './install.ts'
import { RegistryCore } from './registry.ts'
import { handle, type AutopilotApi, type InstallApi } from './router.ts'
import { AutopilotCore, type Out, type Session, type WsLike } from './autopilot.ts'
import type { CreatePayload } from './payload.ts'
import type { InstallListing } from './schema.ts'
import type { InstallerSecrets, Reply } from './types.ts'

interface Env extends InstallerSecrets {
  INSTALL_DO: DurableObjectNamespace<InstallDO>
  INSTALL_REGISTRY: DurableObjectNamespace<InstallRegistry>
  SSH_BOX: DurableObjectNamespace<SshBox>
  AUTOPILOT: DurableObjectNamespace<AutopilotDO>
  // "1" only in local dev: lets the local-test runbook pair.
  AUTOPILOT_DEV_RUNBOOKS?: string
}

// Runs ssh/ssh-keyscan for one install (container/server.mjs). It holds no
// secrets of its own: the install key and any stdin arrive per request from
// that install's DO and are written to a tmpfs file only for the call.
export class SshBox extends Container<Env> {
  defaultPort = 8080
  sleepAfter = '10m'
  // Outbound TCP/22 to the customer's box (SPIKE.md).
  enableInternet = true
}

export class InstallRegistry extends DurableObject<Env> {
  private core: RegistryCore

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env)
    this.core = new RegistryCore({ sql: ctx.storage.sql, now: () => Date.now() })
  }

  reserve(p: CreatePayload): { install_id: string; created: boolean } {
    return this.core.reserve(p)
  }

  exists(installId: string): boolean {
    return this.core.exists(installId)
  }

  sync(listing: InstallListing): void {
    this.core.sync(listing)
  }

  list(filter: string): InstallListing[] {
    return this.core.list(filter)
  }
}

function registry(env: Env) {
  return env.INSTALL_REGISTRY.get(env.INSTALL_REGISTRY.idFromName('registry'))
}

export class InstallDO extends DurableObject<Env> implements InstallApi {
  private core: InstallCore

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env)
    this.core = new InstallCore({
      sql: ctx.storage.sql,
      kv: ctx.storage,
      env,
      fetch: (req) => fetch(req),
      now: () => Date.now(),
      setAlarm: async (at) => {
        if (at === null) await ctx.storage.deleteAlarm()
        else await ctx.storage.setAlarm(at)
      },
      sendToBox: (installId, req) => getContainer(env.SSH_BOX, installId).fetch(req),
      sync: async (listing) => {
        await registry(env).sync(listing)
      },
    })
  }

  init(p: CreatePayload, installId: string, origin: string): Promise<Reply> {
    return this.core.init(p, installId, origin)
  }
  patchSchedule(scheduledAt: number): Promise<Reply> {
    return this.core.patchSchedule(scheduledAt)
  }
  setHomework(item: string, state: 'pending' | 'green'): Promise<Reply> {
    return this.core.setHomework(item, state)
  }
  pinServer(serverIp: string, hostKeyFp: string): Promise<Reply> {
    return this.core.pinServer(serverIp, hostKeyFp)
  }
  finishByAdmin(status: 'done' | 'failed'): Promise<Reply> {
    return this.core.finishByAdmin(status)
  }
  setHetznerToken(token: string): Promise<Reply> {
    return this.core.setHetznerToken(token)
  }
  createServer(): Promise<Reply> {
    return this.core.createServer()
  }
  probe(check: Probe): Promise<Reply> {
    return this.core.probe(check)
  }
  closeByAdmin(): Promise<Reply> {
    return this.core.closeByAdmin()
  }
  journalView(): Reply {
    return this.core.journalView()
  }
  autopilotNote(line: string): Promise<Reply> {
    return this.core.autopilotNote(line)
  }
  async alarm(): Promise<void> {
    await this.core.onAlarm()
  }
}

// Browser autopilot relay (autopilot.ts). One instance, "relay", so a pairing
// code resolves without knowing its install. Sockets are hibernatable and the
// extension's keepalive "ping" is answered without waking the object.
export class AutopilotDO extends DurableObject<Env> implements AutopilotApi {
  private core: AutopilotCore

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env)
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('ping', 'pong'))
    this.core = new AutopilotCore({
      sql: ctx.storage.sql,
      now: () => Date.now(),
      setAlarm: async (at) => {
        if (at === null) await ctx.storage.deleteAlarm()
        else await ctx.storage.setAlarm(at)
      },
      getAlarm: () => ctx.storage.getAlarm(),
      accept: (ws, sessionId, conn, at) => {
        ctx.acceptWebSocket(ws as WebSocket, [sessionId])
        ;(ws as WebSocket).serializeAttachment({ session_id: sessionId, conn, at })
      },
      sockets: (sessionId) => ctx.getWebSockets(sessionId),
      attachment: (ws) => ((ws as WebSocket).deserializeAttachment() as { session_id: string; conn?: string; at?: number } | null) ?? null,
      lastPing: (ws) => ctx.getWebSocketAutoResponseTimestamp(ws as WebSocket)?.getTime() ?? null,
      // Only install owners reach here; the core keeps customer lines itself.
      journal: async (installId, line) => {
        await env.INSTALL_DO.get(env.INSTALL_DO.idFromName(installId)).autopilotNote(line)
      },
    })
  }

  // Only the extension's socket upgrade arrives as a fetch.
  async fetch(req: Request): Promise<Response> {
    if (req.headers.get('upgrade')?.toLowerCase() !== 'websocket') return new Response('expected websocket', { status: 426 })
    const s: Session | null = await this.core.sessionForToken(new URL(req.url).searchParams.get('token'))
    if (!s) return new Response('unauthorized', { status: 401 })
    const pair = new WebSocketPair()
    await this.core.accept(pair[1] as unknown as WsLike, s)
    return new Response(null, { status: 101, webSocket: pair[0] })
  }
  async webSocketMessage(ws: WebSocket, data: string | ArrayBuffer): Promise<void> {
    await this.core.onMessage(ws as unknown as WsLike, data)
  }
  async webSocketClose(ws: WebSocket): Promise<void> {
    await this.core.onClose(ws as unknown as WsLike)
  }
  async webSocketError(ws: WebSocket): Promise<void> {
    await this.core.onClose(ws as unknown as WsLike)
  }
  async alarm(): Promise<void> {
    await this.core.onAlarm()
  }

  createCode(installId: string, runbookId: string): Promise<Out> {
    return this.core.createCode(installId, runbookId, this.env.AUTOPILOT_DEV_RUNBOOKS === '1')
  }
  createCustomerCode(customer: unknown, runbookId: string): Promise<Out> {
    return this.core.createCustomerCode(customer, runbookId, this.env.AUTOPILOT_DEV_RUNBOOKS === '1')
  }
  customerView(customer: string): Out {
    return this.core.customerView(customer)
  }
  pair(code: unknown): Promise<Out> {
    return this.core.pair(code)
  }
  listSessions(installId: string): Out {
    return this.core.listSessions(installId)
  }
  forward(sessionId: string, cmd: Record<string, unknown>, waitMs?: number): Promise<Out> {
    return this.core.forward(sessionId, cmd, waitMs)
  }
  poll(sessionId: string, id: string, waitMs?: number): Promise<Out> {
    return this.core.poll(sessionId, id, waitMs)
  }
  takeSecret(sessionId: string, item: string): Promise<{ value: string } | null> {
    return this.core.takeSecret(sessionId, item)
  }
  end(sessionId: string, why: string): Promise<Out> {
    return this.core.end(sessionId, why)
  }
}

function relay(env: Env) {
  return env.AUTOPILOT.get(env.AUTOPILOT.idFromName('relay'))
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    return handle(req, {
      env,
      reserve: (p) => registry(env).reserve(p),
      exists: (id) => registry(env).exists(id),
      list: (filter) => registry(env).list(filter),
      install: (id) => env.INSTALL_DO.get(env.INSTALL_DO.idFromName(id)) as unknown as InstallApi,
      autopilot: () => relay(env) as unknown as AutopilotApi,
      autopilotSocket: (r) => relay(env).fetch(r),
    })
  },
} satisfies ExportedHandler<Env>
