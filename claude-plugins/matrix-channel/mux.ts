// Single-sync multiplexer. Every Claude Code session with this plugin enabled
// spawns its own server, but they share one bot account and one sync token.
// The first process to take the lock becomes the owner: it runs /sync, persists
// the `since` token and fans raw room events out over a unix socket. Others
// connect as clients and take over if the owner goes away.
//
// Adapted from zekker6/claude-code-channel-matrix (MIT, see LICENSE). Changed:
// flock is loaded from libSystem on macOS (upstream hard-coded libc.so.6, which
// does not exist there), and frames carry raw Matrix events so clients run the
// same parser as the owner.

import { existsSync, readFileSync, writeFileSync, mkdirSync, openSync, closeSync, unlinkSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { createConnection, createServer, type Socket, type Server as NetServer } from 'node:net'
import { dlopen, FFIType } from 'bun:ffi'

// ── Sync token persistence ────────────────────────────

const SYNC_TOKEN_FILE = 'sync-token'

export function loadSyncToken(dir: string): string | null {
  const p = join(dir, SYNC_TOKEN_FILE)
  if (!existsSync(p)) return null
  try {
    return readFileSync(p, 'utf8').trim() || null
  } catch {
    return null
  }
}

export function saveSyncToken(dir: string, token: string): void {
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const p = join(dir, SYNC_TOKEN_FILE)
  writeFileSync(p + '.tmp', token, { mode: 0o600 })
  renameSync(p + '.tmp', p)
}

// ── Lock via flock(2) ─────────────────────────────────
// flock is released by the kernel when the holder dies, so a crashed owner
// never leaves a stale lock behind.

const LOCK_FILE = 'mux.lock'
export const SOCKET_FILE = 'mux.sock'
const LOCK_EX = 2
const LOCK_NB = 4
const LOCK_UN = 8

function libcPath(): string {
  if (process.platform === 'darwin') return '/usr/lib/libSystem.B.dylib'
  if (process.platform === 'linux') return 'libc.so.6'
  throw new Error(`flock not supported on ${process.platform}`)
}

let libc: ReturnType<typeof loadLibc> | null = null
function loadLibc() {
  return dlopen(libcPath(), { flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 } })
}

export interface LockHandle {
  release: () => void
}

export function tryAcquireLock(dir: string): LockHandle | null {
  libc ??= loadLibc()
  const lib = libc
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  let fd: number
  try {
    fd = openSync(join(dir, LOCK_FILE), 'a')
  } catch {
    return null
  }
  if (lib.symbols.flock(fd, LOCK_EX | LOCK_NB) !== 0) {
    closeSync(fd)
    return null
  }
  let released = false
  return {
    release: () => {
      if (released) return
      released = true
      try { lib.symbols.flock(fd, LOCK_UN) } catch {}
      try { closeSync(fd) } catch {}
    },
  }
}

// ── Wire protocol ─────────────────────────────────────

export interface WireEventFrame {
  v: 2
  type: 'event'
  roomId: string
  event: any
}

export interface WireHeartbeatFrame {
  v: 2
  type: 'heartbeat'
  ts: number
}

export type WireFrame = WireEventFrame | WireHeartbeatFrame

export function serializeFrame(frame: WireFrame): string {
  return JSON.stringify(frame)
}

export function deserializeFrame(line: string): WireFrame | null {
  try {
    const obj = JSON.parse(line)
    if (obj?.v !== 2) return null
    if (obj.type === 'event' && typeof obj.roomId === 'string' && obj.event) return obj
    if (obj.type === 'heartbeat') return obj
    return null
  } catch {
    return null
  }
}

/** macOS sun_path is 104 bytes including the NUL; Linux is 108. */
export function socketPathFits(dir: string): boolean {
  const max = process.platform === 'darwin' ? 103 : 107
  return Buffer.byteLength(join(dir, SOCKET_FILE)) <= max
}

// ── Server ────────────────────────────────────────────

const HEARTBEAT_INTERVAL_MS = 30_000

export class MuxServer {
  private server: NetServer | null = null
  private clients = new Set<Socket>()
  private heartbeat: ReturnType<typeof setInterval> | null = null

  constructor(private dir: string) {}

  get socketPath(): string {
    return join(this.dir, SOCKET_FILE)
  }

  get clientCount(): number {
    return this.clients.size
  }

  async start(): Promise<void> {
    try { unlinkSync(this.socketPath) } catch {}
    this.server = createServer(socket => {
      this.clients.add(socket)
      const drop = () => this.clients.delete(socket)
      socket.on('close', drop)
      socket.on('error', drop)
    })
    await new Promise<void>((resolve, reject) => {
      this.server!.once('error', reject)
      this.server!.listen(this.socketPath, () => resolve())
    })
    this.heartbeat = setInterval(() => this.broadcast({ v: 2, type: 'heartbeat', ts: Date.now() }), HEARTBEAT_INTERVAL_MS)
    this.heartbeat.unref()
  }

  broadcast(frame: WireFrame): void {
    const line = serializeFrame(frame) + '\n'
    for (const c of this.clients) {
      try {
        c.write(line)
      } catch {
        this.clients.delete(c)
      }
    }
  }

  async stop(): Promise<void> {
    if (this.heartbeat) clearInterval(this.heartbeat)
    this.heartbeat = null
    for (const c of this.clients) c.destroy()
    this.clients.clear()
    if (this.server) {
      const s = this.server
      this.server = null
      await new Promise<void>(resolve => s.close(() => resolve()))
    }
    try { unlinkSync(this.socketPath) } catch {}
  }
}

// ── Client ────────────────────────────────────────────

export interface MuxClientOptions {
  maxRetries?: number
  retryDelayMs?: number
  heartbeatTimeoutMs?: number
}

export class MuxClient {
  private socket: Socket | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private buffer = ''
  private opts: Required<MuxClientOptions>

  onFrame: ((frame: WireEventFrame) => void) | null = null
  onDisconnect: (() => void) | null = null

  constructor(private dir: string, opts: MuxClientOptions = {}) {
    this.opts = {
      maxRetries: opts.maxRetries ?? 5,
      retryDelayMs: opts.retryDelayMs ?? 500,
      heartbeatTimeoutMs: opts.heartbeatTimeoutMs ?? 90_000,
    }
  }

  get socketPath(): string {
    return join(this.dir, SOCKET_FILE)
  }

  async connect(): Promise<void> {
    for (let attempt = 0; attempt < this.opts.maxRetries; attempt++) {
      try {
        await this.tryConnect()
        return
      } catch {
        if (attempt < this.opts.maxRetries - 1) await new Promise(r => setTimeout(r, this.opts.retryDelayMs))
      }
    }
    throw new Error(`could not connect to multiplexer after ${this.opts.maxRetries} attempts`)
  }

  private tryConnect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(this.socketPath)
      socket.on('connect', () => {
        this.socket = socket
        this.resetTimer()
        socket.on('data', chunk => this.onData(chunk))
        resolve()
      })
      socket.on('error', err => {
        if (this.socket === socket) this.lost()
        else reject(err)
      })
      socket.on('close', () => {
        if (this.socket === socket) this.lost()
      })
    })
  }

  private onData(chunk: Buffer): void {
    this.buffer += chunk.toString()
    const lines = this.buffer.split('\n')
    this.buffer = lines.pop()!
    for (const line of lines) {
      if (!line) continue
      this.resetTimer()
      const frame = deserializeFrame(line)
      if (frame?.type === 'event') this.onFrame?.(frame)
    }
  }

  private resetTimer(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = setTimeout(() => this.lost(), this.opts.heartbeatTimeoutMs)
    this.timer.unref()
  }

  private lost(): void {
    this.disconnect()
    this.onDisconnect?.()
  }

  disconnect(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    if (this.socket) {
      const s = this.socket
      this.socket = null
      s.destroy()
    }
    this.buffer = ''
  }
}
