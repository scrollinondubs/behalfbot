import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { generateKeyPairSync } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fingerprintOfBlob, pinnedKnownHostsLine, runOnBox, RefusedError, validateRunRequest } from './ssh.mjs'

const u32 = (n) => {
  const b = Buffer.alloc(4)
  b.writeUInt32BE(n)
  return b
}
function blobOf(raw32) {
  const type = Buffer.from('ssh-ed25519')
  return Buffer.concat([u32(type.length), type, u32(32), raw32]).toString('base64')
}
const GOOD = blobOf(Buffer.alloc(32, 1))
const EVIL = blobOf(Buffer.alloc(32, 2))
const HOST = '203.0.113.10'
// Made up. Not a live credential.
const SECRET = 'vault-pass-Zq81xNw0pLm4'

function hasSshKeygen() {
  try {
    execFileSync('ssh-keygen', ['-?'], { stdio: 'ignore' })
    return true
  } catch (e) {
    return e.status !== undefined && e.status !== null
  }
}

function fakeSpawn(scanOut) {
  const calls = []
  const spawn = (file, args) => {
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    const stdin = []
    const run = () => {
      child.stdout.emit('data', file === 'ssh-keyscan' ? scanOut : 'ok\n')
      child.emit('close', 0)
    }
    child.stdin = {
      on() {},
      end(b) {
        if (b) stdin.push(Buffer.from(b))
        setImmediate(run)
      },
    }
    child.kill = () => {}
    calls.push({ file, args, stdin })
    return child
  }
  return { spawn, calls }
}

const base = () => ({
  host: HOST,
  port: 22,
  user: 'root',
  host_key_fp: fingerprintOfBlob(GOOD),
  private_key: '-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n',
  cmd: 'cat > /root/.vault-pass',
  stdin_b64: Buffer.from(SECRET).toString('base64'),
  timeout_ms: 5000,
})

test('fingerprintOfBlob matches ssh-keygen -lf', { skip: !hasSshKeygen() }, () => {
  const { publicKey } = generateKeyPairSync('ed25519')
  const blob = blobOf(publicKey.export({ format: 'der', type: 'spki' }).subarray(-32))
  const dir = mkdtempSync(join(tmpdir(), 'fp-'))
  try {
    const p = join(dir, 'k.pub')
    writeFileSync(p, `ssh-ed25519 ${blob} test\n`)
    const out = execFileSync('ssh-keygen', ['-lf', p], { encoding: 'utf8' })
    assert.equal(out.split(' ')[1], fingerprintOfBlob(blob))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('host pinning: a box presenting a different key is refused and ssh never runs', async () => {
  const { spawn, calls } = fakeSpawn(`${HOST} ssh-ed25519 ${EVIL}\n`)
  await assert.rejects(runOnBox(base(), { spawn }), (e) => e instanceof RefusedError && e.code === 'host_key_mismatch')
  assert.deepEqual(
    calls.map((c) => c.file),
    ['ssh-keyscan'],
  )
})

test('host pinning: pinnedKnownHostsLine ignores non-ed25519 and comment lines', () => {
  const out = `# ${HOST}:22 SSH-2.0-OpenSSH\n${HOST} ssh-rsa AAAAB3Nza\n${HOST} ssh-ed25519 ${GOOD}\n`
  assert.equal(pinnedKnownHostsLine(out, HOST, fingerprintOfBlob(GOOD)), `${HOST} ssh-ed25519 ${GOOD}\n`)
  assert.throws(() => pinnedKnownHostsLine(`${HOST} ssh-rsa AAAAB3Nza\n`, HOST, fingerprintOfBlob(GOOD)), RefusedError)
})

test('hostnames, other ports and option-looking values are refused before any process starts', () => {
  for (const patch of [{ host: 'evil.example.com' }, { host: '-oProxyCommand=sh' }, { port: 2222 }, { user: 'root -o x' }, { host_key_fp: 'nope' }]) {
    assert.throws(() => validateRunRequest({ ...base(), ...patch }), RefusedError, JSON.stringify(patch))
  }
})

test('matched host: strict host key checking, secret only on stdin, never in argv', async () => {
  const { spawn, calls } = fakeSpawn(`${HOST} ssh-ed25519 ${GOOD}\n`)
  const res = await runOnBox(base(), { spawn })
  assert.equal(res.code, 0)
  assert.equal(res.host_key_fp, fingerprintOfBlob(GOOD))
  const ssh = calls.find((c) => c.file === 'ssh')
  const argv = ssh.args.join(' ')
  assert.match(argv, /StrictHostKeyChecking=yes/)
  assert.match(argv, /GlobalKnownHostsFile=\/dev\/null/)
  assert.ok(!argv.includes('StrictHostKeyChecking=no'))
  assert.ok(!argv.includes(SECRET))
  assert.ok(!argv.includes('PRIVATE KEY'))
  assert.equal(ssh.args.at(-2), `root@${HOST}`)
  assert.equal(ssh.args.at(-3), '--')
  assert.equal(Buffer.concat(ssh.stdin).toString(), SECRET)
})
