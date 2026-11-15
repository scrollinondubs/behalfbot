import { test } from 'node:test'
import assert from 'node:assert/strict'
import { assertPinned, HostPinError, SshTransport, isLiteralIp } from '../src/transport.ts'
import { PIN, makeWorld, createInstall, fakeBox } from './helpers.mjs'

const KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----\n'

test('host pinning: only a literal IP plus a SHA256 fingerprint is accepted', () => {
  assert.doesNotThrow(() => assertPinned(PIN))
  assert.doesNotThrow(() => assertPinned({ ...PIN, serverIp: '2001:db8::10' }))
  const bad = [
    { ...PIN, serverIp: 'box.example.com' },
    { ...PIN, serverIp: '-oProxyCommand=sh' },
    { ...PIN, serverIp: '203.0.113.10;reboot' },
    { ...PIN, serverIp: '256.1.1.1' },
    { ...PIN, serverIp: undefined },
    { ...PIN, hostKeyFp: undefined },
    { ...PIN, hostKeyFp: 'MD5:aa:bb' },
    null,
  ]
  for (const p of bad) assert.throws(() => assertPinned(p), HostPinError, JSON.stringify(p))
  assert.equal(isLiteralIp('localhost'), false)
})

test('host pinning: the transport refuses when the box reports a host key that is not the pin', async () => {
  const box = fakeBox({ reportFp: 'SHA256:' + 'B'.repeat(43) })
  const t = new SshTransport({ pin: PIN, user: 'root', privateKeyPem: KEY, send: (req) => box.send('x', req) })
  await assert.rejects(t.run('true'), HostPinError)
})

test('host pinning: a container host_key_mismatch answer is a refusal', async () => {
  const send = async () => Response.json({ error: 'host_key_mismatch', seen: ['SHA256:x'] }, { status: 403 })
  const t = new SshTransport({ pin: PIN, user: 'root', privateKeyPem: KEY, send })
  await assert.rejects(t.run('true'), HostPinError)
})

test('host pinning: every request carries only the pinned host; run() takes no host', async () => {
  const box = fakeBox()
  const t = new SshTransport({ pin: PIN, user: 'root', privateKeyPem: KEY, send: (req) => box.send('x', req) })
  await t.run('uname -a')
  assert.equal(box.requests[0].body.host, PIN.serverIp)
  assert.equal(box.requests[0].body.port, 22)
  assert.equal(box.requests[0].body.host_key_fp, PIN.hostKeyFp)
})

test('install exec refuses an unpinned install and journals why', async () => {
  const w = makeWorld()
  const { core } = await createInstall(w)
  await assert.rejects(core.exec(1, 'probe', 1, 'true'), HostPinError)
  assert.equal(w.box.requests.length, 0)
  assert.ok(core.journalView().body.journal.some((j) => /not pinned/.test(j.body)))
})

test('pinServer validates and refuses re-pinning to a different box', async () => {
  const w = makeWorld()
  const { core } = await createInstall(w)
  assert.equal((await core.pinServer('box.example.com', PIN.hostKeyFp)).status, 400)
  assert.equal((await core.pinServer(PIN.serverIp, PIN.hostKeyFp)).status, 200)
  assert.equal((await core.pinServer('203.0.113.99', PIN.hostKeyFp)).status, 409)
  assert.equal((await core.pinServer(PIN.serverIp, PIN.hostKeyFp)).status, 200)
})
