// Ported from zekker6/claude-code-channel-matrix mux.test.ts (MIT).
import { describe, test, expect, beforeEach, afterEach } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import {
  loadSyncToken, saveSyncToken, tryAcquireLock, MuxServer, MuxClient, serializeFrame, deserializeFrame,
  socketPathFits, SOCKET_FILE, type WireEventFrame,
} from './mux'

// macOS sockets need short paths; os.tmpdir() under /var/folders is fine but keep it tight.
function shortDir(): string {
  return mkdtempSync('/tmp/mxt-')
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

async function until(cond: () => boolean, ms = 2000): Promise<void> {
  const end = Date.now() + ms
  while (!cond()) {
    if (Date.now() > end) throw new Error('timed out')
    await sleep(10)
  }
}

const frame = (id: string): WireEventFrame => ({
  v: 2, type: 'event', roomId: '!r:x', event: { type: 'm.room.message', event_id: id, sender: '@s:x', content: { msgtype: 'm.text', body: 'hi' } },
})

describe('frames', () => {
  test('round-trip', () => {
    expect(deserializeFrame(serializeFrame(frame('$1')))).toEqual(frame('$1'))
  })
  test('rejects other versions, malformed json, and incomplete frames', () => {
    expect(deserializeFrame(JSON.stringify({ v: 1, type: 'text' }))).toBeNull()
    expect(deserializeFrame('{nope')).toBeNull()
    expect(deserializeFrame(JSON.stringify({ v: 2, type: 'event' }))).toBeNull()
  })
})

describe('sync token persistence', () => {
  let dir: string
  beforeEach(() => { dir = shortDir() })
  afterEach(() => rmSync(dir, { recursive: true, force: true }))
  test('null when absent or empty', () => {
    expect(loadSyncToken(dir)).toBeNull()
    writeFileSync(join(dir, 'sync-token'), '  \n')
    expect(loadSyncToken(dir)).toBeNull()
  })
  test('save, load, overwrite', () => {
    saveSyncToken(dir, 's1')
    expect(loadSyncToken(dir)).toBe('s1')
    saveSyncToken(dir, 's2')
    expect(loadSyncToken(dir)).toBe('s2')
  })
})

describe('tryAcquireLock (portable flock)', () => {
  let dir: string
  beforeEach(() => { dir = shortDir() })
  afterEach(() => rmSync(dir, { recursive: true, force: true }))
  test('acquires when free', () => {
    const l = tryAcquireLock(dir)
    expect(l).not.toBeNull()
    l!.release()
  })
  test('second acquire fails while held, succeeds after release', () => {
    const a = tryAcquireLock(dir)!
    expect(tryAcquireLock(dir)).toBeNull()
    a.release()
    const b = tryAcquireLock(dir)
    expect(b).not.toBeNull()
    b!.release()
  })
  test('lock held by another process blocks us, and is freed when it dies', async () => {
    const script = `import { tryAcquireLock } from '${join(import.meta.dir, 'mux.ts')}'; const l = tryAcquireLock(${JSON.stringify(dir)}); console.log(l ? 'held' : 'busy'); setTimeout(() => {}, 60000)`
    const child = Bun.spawn(['bun', '-e', script], { stdout: 'pipe' })
    const reader = child.stdout.getReader()
    const { value } = await reader.read()
    expect(new TextDecoder().decode(value)).toContain('held')
    expect(tryAcquireLock(dir)).toBeNull()
    child.kill('SIGKILL')
    await child.exited
    const l = tryAcquireLock(dir)
    expect(l).not.toBeNull()
    l!.release()
  })
})

describe('socket path length', () => {
  test('rejects long paths, accepts short', () => {
    expect(socketPathFits('/tmp/' + 'a'.repeat(120))).toBe(false)
    expect(socketPathFits('/tmp/short')).toBe(true)
  })
})

describe('MuxServer + MuxClient', () => {
  let dir: string
  let server: MuxServer
  const clients: MuxClient[] = []
  beforeEach(async () => {
    dir = shortDir()
    server = new MuxServer(dir)
    await server.start()
  })
  afterEach(async () => {
    for (const c of clients.splice(0)) c.disconnect()
    await server.stop()
    rmSync(dir, { recursive: true, force: true })
  })

  test('broadcasts events to every client, filters heartbeats', async () => {
    const got: string[][] = [[], []]
    for (const i of [0, 1]) {
      const c = new MuxClient(dir, { maxRetries: 3, retryDelayMs: 20 })
      c.onFrame = f => got[i]!.push(f.event.event_id)
      await c.connect()
      clients.push(c)
    }
    await until(() => server.clientCount === 2)
    server.broadcast({ v: 2, type: 'heartbeat', ts: 1 })
    server.broadcast(frame('$a'))
    server.broadcast(frame('$b'))
    await until(() => got[0]!.length === 2 && got[1]!.length === 2)
    expect(got).toEqual([['$a', '$b'], ['$a', '$b']])
  })

  test('client sees disconnect when server stops; socket removed', async () => {
    let lost = false
    const c = new MuxClient(dir, { maxRetries: 3, retryDelayMs: 20 })
    c.onDisconnect = () => { lost = true }
    await c.connect()
    clients.push(c)
    await server.stop()
    await until(() => lost)
    expect(existsSync(join(dir, SOCKET_FILE))).toBe(false)
  })

  test('heartbeat timeout triggers disconnect', async () => {
    let lost = false
    const c = new MuxClient(dir, { heartbeatTimeoutMs: 100 })
    c.onDisconnect = () => { lost = true }
    await c.connect()
    clients.push(c)
    await until(() => lost, 1000)
  })

  test('start removes a stale socket file', async () => {
    await server.stop()
    writeFileSync(join(dir, SOCKET_FILE), 'stale')
    server = new MuxServer(dir)
    await server.start()
    const c = new MuxClient(dir, { maxRetries: 2, retryDelayMs: 20 })
    await c.connect()
    clients.push(c)
  })
})

describe('takeover', () => {
  test('client takes the lock and serves after the owner goes away', async () => {
    const dir = shortDir()
    try {
      const ownerLock = tryAcquireLock(dir)!
      const owner = new MuxServer(dir)
      await owner.start()

      let tookOver = false
      let newServer: MuxServer | null = null
      const c = new MuxClient(dir, { maxRetries: 2, retryDelayMs: 20 })
      c.onDisconnect = async () => {
        const l = tryAcquireLock(dir)
        if (!l) return
        newServer = new MuxServer(dir)
        await newServer.start()
        tookOver = true
        l.release()
      }
      await c.connect()
      await owner.stop()
      ownerLock.release()
      await until(() => tookOver)

      const c2 = new MuxClient(dir, { maxRetries: 3, retryDelayMs: 20 })
      const got: string[] = []
      c2.onFrame = f => got.push(f.event.event_id)
      await c2.connect()
      await until(() => newServer!.clientCount === 1)
      newServer!.broadcast(frame('$after'))
      await until(() => got.length === 1)
      c2.disconnect()
      await newServer!.stop()
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  test('connect fails cleanly with no server', async () => {
    const dir = shortDir()
    try {
      await expect(new MuxClient(dir, { maxRetries: 2, retryDelayMs: 10 }).connect()).rejects.toThrow(/after 2 attempts/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})
