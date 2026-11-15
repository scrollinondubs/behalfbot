import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Redactor, REDACTED } from '../src/redact.ts'
import { PIN, ENV, makeWorld, createInstall } from './helpers.mjs'

// Made-up values shaped like real tokens. None of them is a live credential.
const SECRET = 'vault-pass-Zq81xNw0pLm4'

test('redactor: exact values in raw, base64 and URL-encoded form', () => {
  const r = new Redactor().add(`VW_PASSWORD=${SECRET}`)
  const b64 = Buffer.from(SECRET).toString('base64')
  const out = r.redact(`a ${SECRET} b ${b64} c ${encodeURIComponent(SECRET)} d VW_PASSWORD=${SECRET}`)
  assert.ok(!out.includes(SECRET))
  assert.ok(!out.includes(b64))
  assert.match(out, /\[REDACTED\]/)
})

test('redactor: known token shapes without knowing the value', () => {
  const r = new Redactor()
  const samples = [
    'sk-ant-api03-' + 'x'.repeat(40),
    'sk-proj-' + 'y'.repeat(40),
    'ghp_' + 'a'.repeat(36),
    'ya29.' + 'b'.repeat(40),
    'z'.repeat(64),
    'MTAwMDAwMDAwMDAwMDAwMDAx.GaBcDe.' + 'c'.repeat(38),
    '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----',
  ]
  for (const s of samples) assert.equal(r.redact(`x ${s} y`), `x ${REDACTED} y`, s.slice(0, 12))
  assert.equal(r.redact('exit 0, 3 files changed'), 'exit 0, 3 files changed')
})

test('a secret passed via stdin never appears in any journal row, Discord body, or container cmd', async () => {
  const w = makeWorld()
  const { core, body } = await createInstall(w)
  await core.pinServer(PIN.serverIp, PIN.hostKeyFp)

  const stdin = new TextEncoder().encode(`${SECRET}\n`)
  // The fake box echoes stdin on stdout and stderr: the worst case.
  const res = await core.exec(9, 'vault write', 1, 'cat > /root/.vault-pass', { stdin })
  assert.ok(!res.stdout.includes(SECRET) && !res.stderr.includes(SECRET), 'redacted before it is returned')
  assert.match(res.stdout, /\[REDACTED\]/)

  // Later rows that mention it (say a model echoes it into a note) are
  // redacted too, and so is an escalation that quotes it.
  await core.appendJournal('note', `model said: the password is ${SECRET}`)
  await core.escalate(`step failed with output ${SECRET}`, { step: 9, stepName: 'vault write', attempts: 2 })

  const rows = core.journalView().body.journal
  assert.ok(rows.length >= 4)
  const b64 = Buffer.from(SECRET).toString('base64')
  for (const row of rows) {
    assert.ok(!JSON.stringify(row).includes(SECRET), `journal row ${row.seq} leaked`)
    assert.ok(!JSON.stringify(row).includes(b64), `journal row ${row.seq} leaked base64`)
  }

  for (const c of w.discord.calls) assert.ok(!(c.body ?? '').includes(SECRET), `discord ${c.method} ${c.path} leaked`)

  const req = w.box.requests[0].body
  assert.ok(!req.cmd.includes(SECRET), 'never in the command line')
  assert.equal(Buffer.from(req.stdin_b64, 'base64').toString(), `${SECRET}\n`, 'only in stdin')

  // The journal endpoint serves the same rows.
  const view = await w.request('GET', `/installs/${body.install_id}/journal`, { token: ENV.INSTALLER_ADMIN_SECRET })
  assert.ok(!(await view.text()).includes(SECRET))
})

test('a command that carries a secret value is refused before it reaches the box', async () => {
  const w = makeWorld()
  const { core } = await createInstall(w)
  await core.pinServer(PIN.serverIp, PIN.hostKeyFp)
  await core.exec(9, 'vault write', 1, 'cat > /root/.vault-pass', { stdin: new TextEncoder().encode(SECRET) })
  await assert.rejects(core.exec(10, 'oops', 1, `echo ${SECRET} > /tmp/x`), /contains a secret/)
  assert.equal(w.box.requests.length, 1)
  for (const row of core.journalView().body.journal) assert.ok(!row.body.includes(SECRET))
})

test('the install private key never appears in the journal view', async () => {
  const w = makeWorld()
  const { core, body } = await createInstall(w)
  const pem = await w.stores.get(body.install_id).get('ssh_private_key')
  assert.match(pem, /OPENSSH PRIVATE KEY/)
  assert.ok(!JSON.stringify(core.journalView().body).includes(pem.split('\n')[1]))
})
