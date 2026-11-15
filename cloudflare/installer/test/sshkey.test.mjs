import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { generateInstallKey } from '../src/sshkey.ts'

function hasSshKeygen() {
  try {
    execFileSync('ssh-keygen', ['-?'], { stdio: 'ignore' })
    return true
  } catch (e) {
    // ssh-keygen -? exits non-zero but exists; ENOENT has no status.
    return e.status !== undefined && e.status !== null
  }
}

test('each install gets a fresh ed25519 key', async () => {
  const a = await generateInstallKey('inst_A')
  const b = await generateInstallKey('inst_A')
  assert.notEqual(a.publicKey, b.publicKey)
  assert.match(a.publicKey, /^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]+=* behalfbot-installer-inst_A$/)
})

test('ssh-keygen accepts the private key and derives the same public key', { skip: !hasSshKeygen() }, async () => {
  const k = await generateInstallKey('inst_B')
  const dir = mkdtempSync(join(tmpdir(), 'installer-key-'))
  try {
    const p = join(dir, 'id')
    writeFileSync(p, k.privateKeyPem, { mode: 0o600 })
    const derived = execFileSync('ssh-keygen', ['-y', '-f', p], { encoding: 'utf8' }).trim()
    assert.equal(derived, k.publicKey)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
