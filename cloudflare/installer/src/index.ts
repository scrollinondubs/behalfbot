// behalfbot-installer: managed installs (behalfbot#265). Spec:
// CONTRACT.md in the managed-installs plan; this file is the only one that
// imports Cloudflare runtime modules, everything else is plain TS the node
// tests can load.
//
// DO NOT `wrangler deploy` or `wrangler secret put` without Sean's explicit
// approval (README.md).

import { DurableObject } from 'cloudflare:workers'
import { Container, getContainer } from '@cloudflare/containers'
import { InstallCore } from './install.ts'
import { RegistryCore } from './registry.ts'
import { handle, type InstallApi } from './router.ts'
import type { CreatePayload } from './payload.ts'
import type { InstallListing } from './schema.ts'
import type { InstallerSecrets, Reply } from './types.ts'

interface Env extends InstallerSecrets {
  INSTALL_DO: DurableObjectNamespace<InstallDO>
  INSTALL_REGISTRY: DurableObjectNamespace<InstallRegistry>
  SSH_BOX: DurableObjectNamespace<SshBox>
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
  journalView(): Reply {
    return this.core.journalView()
  }
  async alarm(): Promise<void> {
    await this.core.onAlarm()
  }
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    return handle(req, {
      env,
      reserve: (p) => registry(env).reserve(p),
      exists: (id) => registry(env).exists(id),
      list: (filter) => registry(env).list(filter),
      install: (id) => env.INSTALL_DO.get(env.INSTALL_DO.idFromName(id)) as unknown as InstallApi,
    })
  },
} satisfies ExportedHandler<Env>
