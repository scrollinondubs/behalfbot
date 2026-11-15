// HTTP surface. Pure: index.ts hands in DO stubs, the tests hand in cores.
//
//   GET   /healthz                      unauthenticated
//   POST  /installs                     site bearer   Interface 1
//   GET   /installs?status=open         site bearer   open|all|<status>, default open
//   PATCH /installs/:id                 site bearer   {scheduled_at}
//   GET   /installs/:id/journal         admin bearer
//   POST  /installs/:id/homework        admin bearer  {item, state}  until the key form exists
//   POST  /installs/:id/server          admin bearer  {server_ip, host_key_fp}  until Hetzner create exists
//   POST  /installs/:id/finish          admin bearer  {status: done|failed}  until the runbook agent exists

import { bearerMatches } from './auth.ts'
import { INSTALL_ID_RE } from './ids.ts'
import { parseCreate, parsePatch, type CreatePayload } from './payload.ts'
import { INSTALL_STATUSES, type InstallListing, type InstallStatus } from './schema.ts'
import type { InstallerSecrets, Reply } from './types.ts'

export interface InstallApi {
  init(p: CreatePayload, installId: string, origin: string): Promise<Reply>
  patchSchedule(scheduledAt: number): Promise<Reply>
  setHomework(item: string, state: 'pending' | 'green'): Promise<Reply>
  pinServer(serverIp: string, hostKeyFp: string): Promise<Reply>
  finishByAdmin(status: 'done' | 'failed'): Promise<Reply>
  journalView(): Promise<Reply> | Reply
}

export interface RouterDeps {
  env: Pick<InstallerSecrets, 'INSTALLER_SITE_SECRET' | 'INSTALLER_ADMIN_SECRET'>
  reserve(p: CreatePayload): Promise<{ install_id: string; created: boolean }>
  list(filter: string): Promise<InstallListing[]>
  exists(installId: string): Promise<boolean>
  install(installId: string): InstallApi
}

const MAX_BODY = 16 * 1024

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status })
}

async function readJson(req: Request): Promise<unknown> {
  const text = await req.text()
  if (text.length > MAX_BODY) throw new Error('body too large')
  return JSON.parse(text)
}

export async function handle(req: Request, deps: RouterDeps): Promise<Response> {
  const url = new URL(req.url)
  if (req.method === 'GET' && url.pathname === '/healthz') return json({ ok: true })

  const auth = req.headers.get('authorization')
  const site = () => bearerMatches(auth, deps.env.INSTALLER_SITE_SECRET)
  const admin = () => bearerMatches(auth, deps.env.INSTALLER_ADMIN_SECRET)

  if (url.pathname === '/installs') {
    if (req.method !== 'POST' && req.method !== 'GET') return json({ error: 'method_not_allowed' }, 405)
    if (!(await site())) return json({ error: 'unauthorized' }, 401)
    if (req.method === 'GET') {
      const filter = url.searchParams.get('status') ?? 'open'
      if (filter !== 'open' && filter !== 'all' && !INSTALL_STATUSES.includes(filter as InstallStatus)) {
        return json({ error: `status must be open, all or one of ${INSTALL_STATUSES.join(', ')}` }, 400)
      }
      return json({ installs: await deps.list(filter) })
    }
    let raw: unknown
    try {
      raw = await readJson(req)
    } catch {
      return json({ error: 'invalid JSON body' }, 400)
    }
    const parsed = parseCreate(raw)
    if (!parsed.ok) return json({ error: parsed.error }, 400)
    const { install_id, created } = await deps.reserve(parsed.value)
    const reply = await deps.install(install_id).init(parsed.value, install_id, url.origin)
    if (reply.status >= 300) return json(reply.body, reply.status)
    return json({ install_id: reply.body.install_id, channel_id: reply.body.channel_id }, created ? 201 : 200)
  }

  const m = /^\/installs\/([^/]+)(\/journal|\/homework|\/server|\/finish)?$/.exec(url.pathname)
  if (!m) return json({ error: 'not_found' }, 404)
  const [, id, sub] = m

  const route = `${req.method} ${sub ?? ''}`
  const needsSite = route === 'PATCH '
  const needsAdmin = route === 'GET /journal' || route === 'POST /homework' || route === 'POST /server' || route === 'POST /finish'
  if (!needsSite && !needsAdmin) return json({ error: 'not_found' }, 404)
  if (!(await (needsSite ? site() : admin()))) return json({ error: 'unauthorized' }, 401)
  if (!INSTALL_ID_RE.test(id) || !(await deps.exists(id))) return json({ error: 'not_found' }, 404)
  const inst = deps.install(id)

  if (route === 'GET /journal') {
    const r = await inst.journalView()
    return json(r.body, r.status)
  }

  let raw: unknown
  try {
    raw = await readJson(req)
  } catch {
    return json({ error: 'invalid JSON body' }, 400)
  }
  const body = (raw ?? {}) as Record<string, unknown>

  let r: Reply
  if (route === 'PATCH ') {
    const p = parsePatch(raw)
    if (!p.ok) return json({ error: p.error }, 400)
    r = await inst.patchSchedule(p.scheduled_at)
  } else if (route === 'POST /homework') {
    if (typeof body.item !== 'string' || (body.state !== 'pending' && body.state !== 'green')) {
      return json({ error: 'want {item, state: pending|green}' }, 400)
    }
    r = await inst.setHomework(body.item, body.state)
  } else if (route === 'POST /finish') {
    if (body.status !== 'done' && body.status !== 'failed') return json({ error: 'want {status: done|failed}' }, 400)
    r = await inst.finishByAdmin(body.status)
  } else {
    if (typeof body.server_ip !== 'string' || typeof body.host_key_fp !== 'string') {
      return json({ error: 'want {server_ip, host_key_fp}' }, 400)
    }
    r = await inst.pinServer(body.server_ip, body.host_key_fp)
  }
  return json(r.body, r.status)
}
