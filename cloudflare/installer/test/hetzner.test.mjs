import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ENV, makeWorld, createInstall } from './helpers.mjs'
import { fingerprintOfBlob } from '../container/ssh.mjs'

const admin = { token: ENV.INSTALLER_ADMIN_SECRET }
// Made up: 64 alphanumerics shaped like a Hetzner token. Not a credential.
const HZ_TOKEN = 'hz' + 'Q7'.repeat(31)

// Fake Hetzner API: records nothing itself (the shared fetch log does).
function hetznerOk({ failServer = false } = {}) {
  return (method, path, body) => {
    if (method === 'POST' && path === '/v1/ssh_keys') return Response.json({ ssh_key: { id: 7001, name: body.name } }, { status: 201 })
    if (method === 'DELETE' && path === '/v1/ssh_keys/7001') return new Response(null, { status: 204 })
    if (method === 'POST' && path === '/v1/servers') {
      if (failServer) return Response.json({ error: { code: 'resource_limit_exceeded', message: 'nope' } }, { status: 403 })
      return Response.json(
        { server: { id: 9001, name: body.name, public_net: { ipv4: { ip: '198.51.100.7' } } }, action: { id: 1 }, root_password: null },
        { status: 201 },
      )
    }
    return Response.json({ error: { code: 'not_found', message: `${method} ${path}` } }, { status: 404 })
  }
}

async function withToken(w) {
  const c = await createInstall(w)
  const res = await w.request('PUT', `/installs/${c.body.install_id}/hetzner-token`, { body: { token: HZ_TOKEN }, ...admin })
  assert.equal(res.status, 200)
  return c
}

const hz = (w) => w.discord.calls.filter((c) => c.host === 'api.hetzner.cloud')

test('hetzner token: admin only, validated, stored, marks homework green, never journaled', async () => {
  const w = makeWorld()
  const { body, core } = await createInstall(w)
  const path = `/installs/${body.install_id}/hetzner-token`
  assert.equal((await w.request('PUT', path, { body: { token: HZ_TOKEN }, token: ENV.INSTALLER_SITE_SECRET })).status, 401)
  assert.equal((await w.request('PUT', path, { body: { token: 'short' }, ...admin })).status, 400)
  assert.equal((await w.request('PUT', path, { body: { token: HZ_TOKEN }, ...admin })).status, 200)
  assert.equal(await w.stores.get(body.install_id).get('hetzner_token'), HZ_TOKEN)
  assert.ok(core.journalView().body.homework.some((h) => h.item === 'hetzner_token' && h.state === 'green'))
  assert.ok(!JSON.stringify(core.journalView().body).includes(HZ_TOKEN))
  for (const c of w.discord.calls) assert.ok(!(c.body ?? '').includes(HZ_TOKEN))
})

test('create server: cx23 nbg1 ubuntu-24.04 in the customer project, our key injected, host key pinned from cloud-init', async () => {
  const w = makeWorld()
  w.discord.state.hetzner = hetznerOk()
  const { body, core } = await withToken(w)
  const id = body.install_id
  const res = await w.request('POST', `/installs/${id}/hetzner-server`, admin)
  assert.equal(res.status, 201)
  const out = await res.json()
  assert.equal(out.server_ip, '198.51.100.7')

  const calls = hz(w)
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.path}`),
    ['POST /v1/ssh_keys', 'POST /v1/servers', 'DELETE /v1/ssh_keys/7001'],
  )
  for (const c of calls) assert.equal(c.headers.authorization, `Bearer ${HZ_TOKEN}`)

  const pub = await w.stores.get(id).get('ssh_public_key')
  assert.equal(JSON.parse(calls[0].body).public_key, pub)

  const srv = JSON.parse(calls[1].body)
  assert.equal(srv.server_type, 'cx23')
  assert.equal(srv.location, 'nbg1')
  assert.equal(srv.image, 'ubuntu-24.04')
  assert.deepEqual(srv.ssh_keys, [7001])
  assert.match(srv.user_data, /^#cloud-config\n/)
  assert.match(srv.user_data, /ed25519_private: \|\n {4}-----BEGIN OPENSSH PRIVATE KEY-----/)

  // The pinned fingerprint is the cloud-init host key's, computed two ways.
  const hostPub = /ed25519_public: (ssh-ed25519 \S+)/.exec(srv.user_data)[1]
  assert.equal(out.host_key_fp, fingerprintOfBlob(hostPub.split(' ')[1]))
  const row = core.row()
  assert.equal(row.server_ip, '198.51.100.7')
  assert.equal(row.host_key_fp, out.host_key_fp)
  assert.equal(await w.stores.get(id).get('hetzner_server_id'), 9001)

  // The host private key went to Hetzner only: not stored, not journaled.
  // Lines 1-2 of any openssh ed25519 key are the shared header; line 4
  // carries this key's own material.
  const hostPrivLine = srv.user_data.split('\n').filter((l) => /^ {4}[A-Za-z0-9+/=]{40,}$/.test(l))[3].trim()
  assert.ok(![...w.stores.get(id).map.values()].some((v) => String(v).includes(hostPrivLine)))
  assert.ok(!JSON.stringify(core.journalView().body).includes(hostPrivLine))
  assert.ok(!JSON.stringify(core.journalView().body).includes(HZ_TOKEN))

  // A second create is refused rather than making a second box.
  assert.equal((await w.request('POST', `/installs/${id}/hetzner-server`, admin)).status, 409)
  assert.equal(hz(w).length, 3)
})

test('create server: a Hetzner failure pins nothing and removes our key object again', async () => {
  const w = makeWorld()
  w.discord.state.hetzner = hetznerOk({ failServer: true })
  const { body, core } = await withToken(w)
  const res = await w.request('POST', `/installs/${body.install_id}/hetzner-server`, admin)
  assert.equal(res.status, 502)
  assert.match((await res.json()).detail, /resource_limit_exceeded/)
  assert.equal(core.row().server_ip, null)
  assert.deepEqual(
    hz(w).map((c) => `${c.method} ${c.path}`),
    ['POST /v1/ssh_keys', 'POST /v1/servers', 'DELETE /v1/ssh_keys/7001'],
  )
})

test('create server needs a token first, and is admin only', async () => {
  const w = makeWorld()
  w.discord.state.hetzner = hetznerOk()
  const { body } = await createInstall(w)
  assert.equal((await w.request('POST', `/installs/${body.install_id}/hetzner-server`, { token: ENV.INSTALLER_SITE_SECRET })).status, 401)
  assert.equal((await w.request('POST', `/installs/${body.install_id}/hetzner-server`, admin)).status, 409)
  assert.equal(hz(w).length, 0)
})

test('probe runs only fixed checks on the pinned box; close by admin never calls Hetzner', async () => {
  const w = makeWorld()
  w.discord.state.hetzner = hetznerOk()
  const { body, core } = await withToken(w)
  const id = body.install_id
  await w.request('POST', `/installs/${id}/hetzner-server`, admin)

  assert.equal((await w.request('POST', `/installs/${id}/probe`, { body: { check: 'rm -rf x' }, ...admin })).status, 400)
  assert.equal((await w.request('POST', `/installs/${id}/probe`, { body: { check: 'true' }, token: ENV.INSTALLER_SITE_SECRET })).status, 401)
  for (const [check, cmd] of [['true', 'true'], ['uname', 'uname -a']]) {
    const r = await w.request('POST', `/installs/${id}/probe`, { body: { check }, ...admin })
    assert.equal(r.status, 200)
    assert.equal(w.box.requests.at(-1).body.cmd, cmd)
    assert.equal(w.box.requests.at(-1).body.host, '198.51.100.7')
  }
  const kp = await w.request('POST', `/installs/${id}/probe`, { body: { check: 'installer_key_present' }, ...admin })
  assert.equal(kp.status, 200)
  assert.match(w.box.requests.at(-1).body.cmd, /^grep -cF 'AAAAC3NzaC1lZDI1NTE5[^']+' "\$HOME\/\.ssh\/authorized_keys" \|\| true$/)

  const before = hz(w).length
  const closed = await w.request('POST', `/installs/${id}/close`, admin)
  assert.equal(closed.status, 200)
  assert.equal(core.row().status, 'closed')
  assert.equal(hz(w).length, before, 'close makes no Hetzner call')
  assert.equal(await w.stores.get(id).get('hetzner_token'), undefined)
  assert.equal((await w.request('POST', `/installs/${id}/close`, admin)).status, 409)
})
