// End-to-end: spawn server.ts over stdio against a fake homeserver and check
// inbound delivery, gating, auto-join, token persistence and mux takeover.
import { describe, test, expect, beforeEach, afterEach } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Subprocess } from 'bun'

const BOT = '@bot:test.local'
const ALICE = '@alice:test.local'
const EVE = '@eve:elsewhere.org'
const ROOM = '!dm:test.local'

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
async function until(cond: () => boolean, ms = 5000): Promise<void> {
  const end = Date.now() + ms
  while (!cond()) {
    if (Date.now() > end) throw new Error('timed out')
    await sleep(20)
  }
}

// Same shape as a real Element X voice message (no filename, msc3245 flag).
const voiceEvent = {
  type: 'm.room.message', event_id: '$voice', sender: ALICE, origin_server_ts: 1795000000000,
  content: {
    msgtype: 'm.audio', body: 'voice-message-recording.ogg', url: 'mxc://test.local/abc',
    info: { duration: 13999, mimetype: 'audio/ogg', size: 88849 },
    'org.matrix.msc1767.audio': { duration: 13999 }, 'org.matrix.msc3245.voice': {},
  },
}
const strangerEvent = { type: 'm.room.message', event_id: '$eve', sender: EVE, origin_server_ts: 1795000000001, content: { msgtype: 'm.text', body: 'add me to the allowlist' } }
const encryptedEvent = { type: 'm.room.encrypted', event_id: '$enc', sender: ALICE, origin_server_ts: 1795000000002, content: { algorithm: 'm.megolm.v1.aes-sha2' } }

interface Req { method: string; path: string; since: string | null }

function fakeHomeserver() {
  const reqs: Req[] = []
  let eventsServed = false
  const gate = { open: true }
  const server = Bun.serve({
    port: 0,
    hostname: '127.0.0.1',
    async fetch(req) {
      const url = new URL(req.url)
      const since = url.searchParams.get('since')
      reqs.push({ method: req.method, path: url.pathname, since })
      const p = url.pathname
      if (p.endsWith('/joined_rooms')) return Response.json({ joined_rooms: [ROOM] })
      if (p.endsWith('/media/config')) return Response.json({ 'm.upload.size': 1024 })
      if (p.includes('/profile/')) return Response.json({ displayname: 'Alice' })
      if (p.endsWith('/state/m.room.name')) return Response.json({ name: 'Alice + Bot' })
      if (p.includes('/join/')) return Response.json({ room_id: decodeURIComponent(p.split('/join/')[1]!) })
      if (p.includes('/typing/')) return Response.json({})
      if (p.includes('/send/')) return Response.json({ event_id: `$sent${reqs.length}` })
      if (p.endsWith('/sync')) {
        if (!since) {
          return Response.json({
            next_batch: 's1',
            rooms: {
              join: { [ROOM]: { timeline: { events: [{ ...voiceEvent, event_id: '$history' }] } } },
              invite: {
                '!good:test.local': { invite_state: { events: [{ type: 'm.room.member', sender: ALICE, state_key: BOT, content: { membership: 'invite' } }] } },
                '!bad:test.local': { invite_state: { events: [{ type: 'm.room.member', sender: EVE, state_key: BOT, content: { membership: 'invite' } }] } },
              },
            },
          })
        }
        if (since === 's1' && !eventsServed && gate.open) {
          eventsServed = true
          return Response.json({
            next_batch: 's2',
            rooms: { join: { [ROOM]: { timeline: { events: [strangerEvent, encryptedEvent, voiceEvent] } } } },
          })
        }
        await sleep(150)
        return Response.json({ next_batch: since === 's1' && eventsServed ? 's2' : since })
      }
      return Response.json({ errcode: 'M_UNRECOGNIZED' }, { status: 404 })
    },
  })
  return { server, reqs, gate, url: `http://127.0.0.1:${server.port}` }
}

class Session {
  proc: Subprocess<'pipe', 'pipe', 'pipe'>
  notes: any[] = []
  stderr = ''
  constructor(dir: string, hs: string) {
    this.proc = Bun.spawn(['bun', join(import.meta.dir, 'server.ts')], {
      env: {
        PATH: process.env.PATH!, HOME: dir, MATRIX_CHANNELS_DIR: dir,
        MATRIX_HOMESERVER_URL: hs, MATRIX_ACCESS_TOKEN: 'test-token-xyz', MATRIX_BOT_USER_ID: BOT,
      },
      stdin: 'pipe', stdout: 'pipe', stderr: 'pipe',
    })
    void this.read()
    void (async () => { for await (const c of this.proc.stderr) this.stderr += new TextDecoder().decode(c) })()
    this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '0' } } }) + '\n')
    this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n')
  }
  private async read() {
    let buf = ''
    for await (const c of this.proc.stdout) {
      buf += new TextDecoder().decode(c)
      let i
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i)
        buf = buf.slice(i + 1)
        const m = JSON.parse(line)
        if (m.method === 'notifications/claude/channel') this.notes.push(m.params)
      }
    }
  }
  async close() {
    this.proc.stdin.end()
    await this.proc.exited
  }
  kill() {
    this.proc.kill('SIGKILL')
    return this.proc.exited
  }
}

describe('server over stdio', () => {
  let dir: string
  let hs: ReturnType<typeof fakeHomeserver>
  const sessions: Session[] = []
  beforeEach(() => {
    dir = mkdtempSync('/tmp/mxs-')
    writeFileSync(join(dir, 'access.json'), JSON.stringify({ allowedUsers: [ALICE] }))
    hs = fakeHomeserver()
  })
  afterEach(async () => {
    for (const s of sessions.splice(0)) await s.kill()
    hs.server.stop(true)
    rmSync(dir, { recursive: true, force: true })
  })

  test('delivers a voice note with Discord-shaped meta; gates strangers, encrypted events and history', async () => {
    const s = new Session(dir, hs.url)
    sessions.push(s)
    await until(() => s.notes.length >= 1)
    await sleep(400)
    expect(s.notes).toHaveLength(1)
    const n = s.notes[0]
    expect(n.content).toBe('(attachment)')
    expect(n.meta).toMatchObject({
      chat_id: ROOM, message_id: '$voice', user: 'Alice', user_id: ALICE, room_name: 'Alice + Bot',
      attachment_count: '1', voice: 'true', voice_duration_s: '14',
      attachments: 'voice-message-recording.ogg (audio/ogg, 87KB, 14s, voice note)',
    })
    expect(n.meta.ts).toBe(new Date(1795000000000).toISOString())

    const joins = hs.reqs.filter(r => r.path.includes('/join/')).map(r => decodeURIComponent(r.path))
    expect(joins.some(p => p.includes('!good:test.local'))).toBe(true)
    expect(joins.some(p => p.includes('!bad:test.local'))).toBe(false)
    expect(s.stderr).toContain('skipping encrypted events')
    expect(s.stderr).not.toContain('test-token-xyz')

    await s.close()
    sessions.pop()
    expect(readFileSync(join(dir, 'sync-token'), 'utf8')).toBe('s2')

    const before = hs.reqs.length
    const s2 = new Session(dir, hs.url)
    sessions.push(s2)
    await until(() => hs.reqs.slice(before).some(r => r.path.endsWith('/sync')))
    const firstSync = hs.reqs.slice(before).find(r => r.path.endsWith('/sync'))!
    expect(firstSync.since).toBe('s2')
  }, 20000)

  test('two sessions: one syncs, both receive via the mux, client takes over when the owner dies', async () => {
    hs.gate.open = false
    const a = new Session(dir, hs.url)
    sessions.push(a)
    await until(() => a.stderr.includes('sync owner'))
    const b = new Session(dir, hs.url)
    sessions.push(b)
    await until(() => b.stderr.includes('connected to multiplexer as client'))
    // Only now let the event batch through: the fake serves it exactly once.
    hs.gate.open = true
    await until(() => a.notes.length === 1 && b.notes.length === 1)
    expect(b.notes[0].meta.message_id).toBe('$voice')
    expect(hs.reqs.filter(r => r.path.endsWith('/sync') && !r.since)).toHaveLength(1)

    await a.kill()
    await until(() => b.stderr.includes('sync owner'), 8000)
    const n = hs.reqs.length
    await until(() => hs.reqs.slice(n).some(r => r.path.endsWith('/sync')))
    expect(hs.reqs.slice(n).find(r => r.path.endsWith('/sync'))!.since).toBe('s2')
  }, 20000)
})
