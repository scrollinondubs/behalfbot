// Test doubles: node:sqlite stands in for DO SQLite (so the real DDL runs),
// a Map for DO key-value storage, and fake fetches for Discord and the SSH
// container. No real tokens anywhere: every value below is made up.

import { DatabaseSync } from 'node:sqlite'
import { InstallCore } from '../src/install.ts'
import { RegistryCore } from '../src/registry.ts'
import { handle } from '../src/router.ts'
import { AutopilotCore } from '../src/autopilot.ts'

export const ENV = {
  DISCORD_INSTALLER_BOT_TOKEN: 'test-bot-token-not-real',
  DISCORD_GUILD_ID: '100000000000000001',
  DISCORD_ESCALATION_CHANNEL_ID: '100000000000000002',
  ANTHROPIC_API_KEY: 'test-anthropic-not-real',
  INSTALLER_SITE_SECRET: 'site-secret-for-tests',
  INSTALLER_ADMIN_SECRET: 'admin-secret-for-tests',
}

export const BOT_ID = '200000000000000001'
export const CUSTOMER_ID = '300000000000000001'
export const PIN = { serverIp: '203.0.113.10', hostKeyFp: 'SHA256:' + 'A'.repeat(43) }
export const DAY_MS = 24 * 3600 * 1000

export function sqlite() {
  const db = new DatabaseSync(':memory:')
  return {
    exec(q, ...b) {
      const rows = db.prepare(q).all(...b.map((v) => (v === undefined ? null : v)))
      return { toArray: () => rows.map((r) => ({ ...r })) }
    },
  }
}

export function kv() {
  const m = new Map()
  return {
    map: m,
    async get(k) { return m.get(k) },
    async put(k, v) { m.set(k, v) },
    async delete(k) { return m.delete(k) },
  }
}

export class Clock {
  constructor(ms = Date.UTC(2026, 10, 16, 12, 0, 0)) { this.ms = ms }
  now = () => this.ms
  advance(ms) { this.ms += ms }
  get s() { return Math.floor(this.ms / 1000) }
}

// Fake Discord. Records every request; customerMessages drives the GC poll.
export function fakeDiscord() {
  const calls = []
  const state = { channelSeq: 0, customerMessages: [], failCreate: 0, failRead: false, hetzner: null }
  const fetch = async (req) => {
    const url = new URL(req.url)
    const body = req.method === 'GET' || req.method === 'DELETE' ? null : await req.text()
    calls.push({ host: url.hostname, method: req.method, path: url.pathname + url.search, body, headers: Object.fromEntries(req.headers) })
    if (url.hostname === 'api.hetzner.cloud' && state.hetzner) return state.hetzner(req.method, url.pathname, body ? JSON.parse(body) : null)
    if (url.hostname !== 'discord.com') throw new Error(`unexpected host ${url.hostname}`)
    const p = url.pathname.replace('/api/v10', '')
    if (req.method === 'GET' && p === '/users/@me') return Response.json({ id: BOT_ID })
    if (req.method === 'POST' && /^\/guilds\/\d+\/channels$/.test(p)) {
      if (state.failCreate > 0) {
        state.failCreate--
        return new Response('upstream sad', { status: 502 })
      }
      state.channelSeq++
      return Response.json({ id: `40000000000000000${state.channelSeq}` })
    }
    if (req.method === 'POST' && /^\/channels\/\d+\/messages$/.test(p)) return Response.json({ id: '500000000000000001' })
    if (req.method === 'GET' && /^\/channels\/\d+\/messages$/.test(p)) {
      if (state.failRead) return new Response('nope', { status: 500 })
      return Response.json(state.customerMessages)
    }
    if (req.method === 'GET' && /^\/guilds\/\d+\/members\/search$/.test(p)) return Response.json([])
    if (req.method === 'DELETE' && /^\/channels\/\d+$/.test(p)) return new Response(null, { status: 204 })
    if (req.method === 'PUT' && /^\/channels\/\d+\/permissions\/\d+$/.test(p)) return new Response(null, { status: 204 })
    return new Response('unhandled', { status: 404 })
  }
  const posts = (channelId) =>
    calls.filter((c) => c.host === 'discord.com' && c.method === 'POST' && c.path.endsWith(`/channels/${channelId}/messages`)).map((c) => JSON.parse(c.body).content)
  const channelCreates = () => calls.filter((c) => c.host === 'discord.com' && c.method === 'POST' && /\/guilds\/\d+\/channels$/.test(c.path))
  return { fetch, calls, state, posts, channelCreates }
}

// Fake SSH container: echoes stdin back on stdout and stderr, the worst case
// for a command that touches a secret.
export function fakeBox({ reportFp } = {}) {
  const requests = []
  const send = async (installId, req) => {
    const body = await req.json()
    requests.push({ installId, body })
    const stdin = body.stdin_b64 ? Buffer.from(body.stdin_b64, 'base64').toString('utf8') : ''
    return Response.json({
      code: 0,
      stdout: `ran: ${body.cmd}\n${stdin}`,
      stderr: stdin ? `echo ${stdin}` : '',
      host_key_fp: reportFp ?? body.host_key_fp,
    })
  }
  return { send, requests }
}

// A whole installer: registry + per-install cores + router, wired the way
// index.ts wires the DOs.
export function makeWorld({ clock = new Clock(), discord = fakeDiscord(), box = fakeBox() } = {}) {
  const alarms = new Map()
  const registry = new RegistryCore({ sql: sqlite(), now: clock.now })
  const installs = new Map()
  const stores = new Map()
  const install = (id) => {
    if (!installs.has(id)) {
      const store = kv()
      stores.set(id, store)
      installs.set(
        id,
        new InstallCore({
          sql: sqlite(),
          kv: store,
          env: ENV,
          fetch: discord.fetch,
          now: clock.now,
          setAlarm: async (at) => { alarms.set(id, at) },
          sendToBox: box.send,
          sync: async (listing) => registry.sync(listing),
        }),
      )
    }
    return installs.get(id)
  }
  const autopilot = fakeRelay({ clock, install })
  const deps = {
    env: ENV,
    reserve: async (p) => registry.reserve(p),
    exists: async (id) => registry.exists(id),
    list: async (f) => registry.list(f),
    install,
    autopilot: () => autopilot.api,
    autopilotSocket: async (req) => autopilot.connect(req),
  }
  const request = (method, path, { body, token } = {}) =>
    handle(
      new Request(`https://installer.test${path}`, {
        method,
        headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
      deps,
    )
  return { clock, discord, box, registry, install, stores, alarms, request, autopilot }
}

// A fake extension socket. `answer` turns each RelayCommand into the reply
// the extension would send; null means stay silent (a human step in flight).
export class FakeSocket {
  constructor() {
    this.sent = []
    this.closed = null
    this.lastPing = null
    this.answer = null
    this.core = null
  }
  send(data) {
    this.sent.push(data)
    if (data === 'pong' || !this.answer) return
    const cmd = JSON.parse(data)
    const reply = this.answer(cmd)
    if (reply) queueMicrotask(() => this.core.onMessage(this, JSON.stringify({ type: 'reply', id: cmd.id, ...reply })))
  }
  close(code, reason) {
    this.closed = { code, reason }
  }
  commands() {
    return this.sent.filter((d) => d !== 'pong').map((d) => JSON.parse(d))
  }
}

// AutopilotCore wired the way AutopilotDO wires it, with sockets held in a
// Map instead of the hibernation API.
export function fakeRelay({ clock, install }) {
  const socks = new Map()
  const att = new WeakMap()
  let alarm = null
  const core = new AutopilotCore({
    sql: sqlite(),
    now: clock.now,
    setAlarm: async (at) => { alarm = at },
    getAlarm: async () => alarm,
    accept: (ws, sessionId, conn, at) => {
      socks.set(sessionId, [...(socks.get(sessionId) ?? []), ws])
      att.set(ws, { session_id: sessionId, conn, at })
    },
    sockets: (sessionId) => socks.get(sessionId) ?? [],
    attachment: (ws) => att.get(ws) ?? null,
    // Tests set ws.lastPing to stand in for the runtime's auto-response time.
    lastPing: (ws) => ws.lastPing ?? null,
    journal: async (installId, line) => { await install(installId).autopilotNote(line) },
  })
  const api = {
    createCode: (id, rb) => core.createCode(id, rb, true),
    createCustomerCode: (c, rb) => core.createCustomerCode(c, rb, true),
    customerView: (c) => core.customerView(c),
    pair: (c) => core.pair(c),
    listSessions: (id) => core.listSessions(id),
    forward: (s, c, w) => core.forward(s, c, w),
    poll: (s, i, w) => core.poll(s, i, w),
    takeSecret: (s, i) => core.takeSecret(s, i),
    end: (s, why) => core.end(s, why),
  }
  // What the runtime does on a socket close: drop it, then tell the core.
  const disconnect = async (ws) => {
    for (const [k, v] of socks) socks.set(k, v.filter((x) => x !== ws))
    await core.onClose(ws)
  }
  // Returns { status, ws } instead of a 101 so tests can hold the socket.
  const connect = async (req) => {
    const s = await core.sessionForToken(new URL(req.url).searchParams.get('token'))
    if (!s) return new Response('unauthorized', { status: 401 })
    const ws = new FakeSocket()
    ws.core = core
    await core.accept(ws, s)
    const res = new Response('switching', { status: 200 })
    res.ws = ws
    return res
  }
  return { core, api, connect, disconnect, alarm: () => alarm }
}

export const STRIPE = {
  source: 'stripe',
  purchase_id: 42,
  customer_name: 'Test Customer',
  customer_email: 'customer@example.test',
  discord_user_id: CUSTOMER_ID,
  discord_username: 'testcustomer',
  variant: 'behalfbot',
  scheduled_at: null,
}

export const ADMIN = { ...STRIPE, source: 'admin', purchase_id: null }

export async function createInstall(world, payload = STRIPE) {
  const res = await world.request('POST', '/installs', { body: payload, token: ENV.INSTALLER_SITE_SECRET })
  const body = await res.json()
  return { res, body, core: world.install(body.install_id) }
}
