// SSH runner for one install. Called only by that install's Durable Object
// through the container binding; the Worker never exposes it publicly.
//
// Host pinning: we only have the pinned fingerprint, not the key, so we
// ssh-keyscan the host, hash what it presents, and refuse unless it matches
// the pin. Then we connect with that one key as the only known host and
// StrictHostKeyChecking=yes, so a different key on the real connection also
// fails. The spike's StrictHostKeyChecking=no is deliberately not used.
//
// Secrets: the install key is written to a 0600 file in a 0700 temp dir for
// the call and removed after; stdin is piped to ssh. Neither is ever in argv,
// and nothing here logs a request body.

import { createHash } from 'node:crypto'
import { spawn as nodeSpawn } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/
const IPV6 = /^[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}$/
const FP = /^SHA256:[A-Za-z0-9+/]{43}$/
const USER = /^[a-z_][a-z0-9_-]{0,31}$/
const MAX_OUT = 1024 * 1024
const MAX_TIMEOUT_MS = 30 * 60_000

export class RefusedError extends Error {
  constructor(code, message, extra = {}) {
    super(message)
    this.code = code
    this.extra = extra
  }
}

export function validateRunRequest(b) {
  if (!b || typeof b !== 'object') throw new RefusedError('bad_request', 'body must be an object')
  if (typeof b.host !== 'string' || !(IPV4.test(b.host) || IPV6.test(b.host))) {
    throw new RefusedError('bad_host', 'host must be a literal IP')
  }
  if (b.port !== 22) throw new RefusedError('bad_port', 'port must be 22')
  if (typeof b.host_key_fp !== 'string' || !FP.test(b.host_key_fp)) throw new RefusedError('bad_fp', 'host_key_fp must be SHA256:<base64>')
  if (typeof b.user !== 'string' || !USER.test(b.user)) throw new RefusedError('bad_user', 'bad user')
  if (typeof b.private_key !== 'string' || !b.private_key.includes('OPENSSH PRIVATE KEY')) throw new RefusedError('bad_key', 'missing key')
  if (typeof b.cmd !== 'string' || b.cmd.length === 0 || b.cmd.length > 64 * 1024) throw new RefusedError('bad_cmd', 'bad cmd')
  if (b.stdin_b64 !== null && b.stdin_b64 !== undefined && typeof b.stdin_b64 !== 'string') throw new RefusedError('bad_stdin', 'bad stdin')
  const timeout = Number.isInteger(b.timeout_ms) ? Math.min(Math.max(b.timeout_ms, 1000), MAX_TIMEOUT_MS) : 120_000
  return { ...b, timeout_ms: timeout }
}

// "SHA256:<unpadded base64 of sha256(key blob)>", the same string
// `ssh-keygen -lf` prints.
export function fingerprintOfBlob(blobB64) {
  return 'SHA256:' + createHash('sha256').update(Buffer.from(blobB64, 'base64')).digest('base64').replace(/=+$/, '')
}

// Picks the ed25519 line out of ssh-keyscan output whose fingerprint matches
// the pin. Returns the known_hosts line to trust, or throws.
export function pinnedKnownHostsLine(keyscanOut, host, pinnedFp) {
  const seen = []
  for (const raw of keyscanOut.split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const [, type, blob] = line.split(/\s+/)
    if (type !== 'ssh-ed25519' || !blob) continue
    const fp = fingerprintOfBlob(blob)
    seen.push(fp)
    if (fp === pinnedFp) return `${host} ${type} ${blob}\n`
  }
  throw new RefusedError('host_key_mismatch', 'host key does not match the pin', { seen })
}

export function sshArgs({ user, host, keyPath, knownHostsPath, cmd }) {
  return [
    '-i', keyPath,
    '-o', 'IdentitiesOnly=yes',
    '-o', 'IdentityAgent=none',
    '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', `UserKnownHostsFile=${knownHostsPath}`,
    '-o', 'GlobalKnownHostsFile=/dev/null',
    '-o', 'HostKeyAlgorithms=ssh-ed25519',
    '-o', 'ConnectTimeout=15',
    '-o', 'ServerAliveInterval=30',
    '-o', 'LogLevel=ERROR',
    '-p', '22',
    '--',
    `${user}@${host}`,
    cmd,
  ]
}

export function execCapture(spawnImpl, file, args, { stdin, timeoutMs }) {
  return new Promise((resolve) => {
    const child = spawnImpl(file, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    let done = false
    const finish = (code) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve({ code, stdout: stdout.slice(0, MAX_OUT), stderr: stderr.slice(0, MAX_OUT) })
    }
    const timer = setTimeout(() => {
      stderr += `\n[timed out after ${timeoutMs} ms]`
      child.kill('SIGKILL')
      finish(124)
    }, timeoutMs)
    child.stdout.on('data', (d) => { if (stdout.length < MAX_OUT) stdout += d })
    child.stderr.on('data', (d) => { if (stderr.length < MAX_OUT) stderr += d })
    child.on('error', (e) => { stderr += String(e); finish(127) })
    child.on('close', (code) => finish(code ?? 1))
    child.stdin.on('error', () => {})
    child.stdin.end(stdin ?? undefined)
  })
}

export async function runOnBox(body, { spawn = nodeSpawn, tmpRoot = tmpdir() } = {}) {
  const b = validateRunRequest(body)
  const scan = await execCapture(spawn, 'ssh-keyscan', ['-T', '10', '-t', 'ed25519', '-p', '22', b.host], { timeoutMs: 20_000 })
  const knownLine = pinnedKnownHostsLine(scan.stdout, b.host, b.host_key_fp)
  const dir = await mkdtemp(join(tmpRoot, 'box-'))
  try {
    const keyPath = join(dir, 'id')
    const knownHostsPath = join(dir, 'known_hosts')
    await writeFile(keyPath, b.private_key, { mode: 0o600 })
    await writeFile(knownHostsPath, knownLine, { mode: 0o600 })
    const stdin = b.stdin_b64 ? Buffer.from(b.stdin_b64, 'base64') : null
    const res = await execCapture(spawn, 'ssh', sshArgs({ user: b.user, host: b.host, keyPath, knownHostsPath, cmd: b.cmd }), {
      stdin,
      timeoutMs: b.timeout_ms,
    })
    return { ...res, host_key_fp: b.host_key_fp }
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
}
