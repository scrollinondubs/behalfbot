import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ADMIN, CUSTOMER_ID, ENV, STRIPE, makeWorld, createInstall, fakeDiscord } from './helpers.mjs'
import { USER_AGENT, PERM } from '../src/discord.ts'

const site = { token: ENV.INSTALLER_SITE_SECRET }

test('stripe: a repeat POST for the same purchase_id returns 200 with the same ids and no second channel', async () => {
  const w = makeWorld()
  const first = await createInstall(w)
  assert.equal(first.res.status, 201)
  assert.match(first.body.install_id, /^inst_[0-9A-Z]{26}$/)
  assert.ok(first.body.channel_id)
  const again = await createInstall(w)
  assert.equal(again.res.status, 200)
  assert.deepEqual(again.body, first.body)
  assert.equal(w.discord.channelCreates().length, 1)
})

test('stripe: two concurrent POSTs for one purchase create one install and one channel', async () => {
  const w = makeWorld()
  const [a, b] = await Promise.all([createInstall(w), createInstall(w)])
  assert.equal(a.body.install_id, b.body.install_id)
  assert.deepEqual([a.res.status, b.res.status].sort(), [200, 201])
  assert.equal(w.discord.channelCreates().length, 1)
})

test('stripe: a different purchase_id is a different install', async () => {
  const w = makeWorld()
  const a = await createInstall(w)
  const b = await createInstall(w, { ...STRIPE, purchase_id: 43 })
  assert.equal(b.res.status, 201)
  assert.notEqual(a.body.install_id, b.body.install_id)
})

test('admin: idempotent on discord_user_id while the install is open, new install once it is closed', async () => {
  const w = makeWorld()
  const a = await createInstall(w, ADMIN)
  assert.equal(a.res.status, 201)
  const again = await createInstall(w, ADMIN)
  assert.equal(again.res.status, 200)
  assert.equal(again.body.install_id, a.body.install_id)

  await a.core.close('test')
  const fresh = await createInstall(w, ADMIN)
  assert.equal(fresh.res.status, 201)
  assert.notEqual(fresh.body.install_id, a.body.install_id)
})

test('admin with a stripe install open for the same user still gets its own install', async () => {
  const w = makeWorld()
  const s = await createInstall(w)
  const a = await createInstall(w, ADMIN)
  assert.equal(a.res.status, 201)
  assert.notEqual(a.body.install_id, s.body.install_id)
})

test('validation: stripe needs purchase_id, admin needs discord_user_id, ids are strings', async () => {
  const w = makeWorld()
  const bad = [
    { ...STRIPE, purchase_id: null },
    { ...ADMIN, discord_user_id: null },
    { ...ADMIN, discord_user_id: 300000000000000001 },
    { ...STRIPE, source: 'other' },
    { ...STRIPE, scheduled_at: 'tomorrow' },
  ]
  for (const body of bad) assert.equal((await w.request('POST', '/installs', { body, ...site })).status, 400, JSON.stringify(body))
  assert.equal(w.discord.calls.length, 0)
})

test('customer_name is nullable', async () => {
  const w = makeWorld()
  const r = await createInstall(w, { ...STRIPE, customer_name: null })
  assert.equal(r.res.status, 201)
  const create = JSON.parse(w.discord.channelCreates()[0].body)
  assert.equal(create.name, 'testcustomer-install')
})

test('a failed channel create answers 502, and the repeat POST retries it on the same install', async () => {
  const discord = fakeDiscord()
  discord.state.failCreate = 1
  const w = makeWorld({ discord })
  const first = await createInstall(w)
  assert.equal(first.res.status, 502)
  const retry = await createInstall(w)
  assert.equal(retry.res.status, 200)
  assert.equal(retry.body.install_id, first.body.install_id)
  assert.ok(retry.body.channel_id)
})

test('the channel is private: @everyone denied, customer and bot allowed; requests carry a User-Agent', async () => {
  const w = makeWorld()
  const { body } = await createInstall(w)
  const create = w.discord.channelCreates()[0]
  const ow = JSON.parse(create.body).permission_overwrites
  const view = PERM.VIEW_CHANNEL
  const everyone = ow.find((o) => o.id === ENV.DISCORD_GUILD_ID)
  assert.equal(everyone.type, 0)
  assert.ok(BigInt(everyone.deny) & view)
  const cust = ow.find((o) => o.id === CUSTOMER_ID)
  assert.equal(cust.type, 1)
  assert.ok(BigInt(cust.allow) & view)
  for (const c of w.discord.calls) {
    assert.equal(c.headers['user-agent'], USER_AGENT)
    assert.equal(c.headers.authorization, `Bot ${ENV.DISCORD_INSTALLER_BOT_TOKEN}`)
  }
  const welcome = w.discord.posts(body.channel_id)
  assert.equal(welcome.length, 1)
  assert.match(welcome[0], /Hetzner project token/)
})

test('PATCH sets the slot, moves the install to scheduled and arms the alarm at the slot', async () => {
  const w = makeWorld()
  const { body } = await createInstall(w)
  const slot = w.clock.s + 3 * 86400
  const res = await w.request('PATCH', `/installs/${body.install_id}`, { body: { scheduled_at: slot }, ...site })
  assert.equal(res.status, 200)
  assert.equal(w.install(body.install_id).row().status, 'scheduled')
  assert.equal(w.alarms.get(body.install_id), slot * 1000)
  assert.equal((await w.request('PATCH', `/installs/${body.install_id}`, { body: { scheduled_at: 'x' }, ...site })).status, 400)
})

test('GET /installs lists open installs with the contract fields, and drops closed ones', async () => {
  const w = makeWorld()
  const a = await createInstall(w)
  const b = await createInstall(w, ADMIN)
  const slot = w.clock.s + 86400
  await w.request('PATCH', `/installs/${a.body.install_id}`, { body: { scheduled_at: slot }, ...site })

  let list = (await (await w.request('GET', '/installs?status=open', site)).json()).installs
  assert.equal(list.length, 2)
  const row = list.find((r) => r.install_id === a.body.install_id)
  assert.deepEqual(Object.keys(row).sort(), ['channel_id', 'customer_name', 'discord_user_id', 'install_id', 'purchase_id', 'scheduled_at', 'source', 'status'])
  assert.equal(row.status, 'scheduled')
  assert.equal(row.scheduled_at, slot)
  assert.equal(row.channel_id, a.body.channel_id)
  assert.equal(row.purchase_id, 42)

  await b.core.close('test')
  list = (await (await w.request('GET', '/installs?status=open', site)).json()).installs
  assert.deepEqual(list.map((r) => r.install_id), [a.body.install_id])
  const all = (await (await w.request('GET', '/installs?status=all', site)).json()).installs
  assert.equal(all.length, 2)
  const closed = (await (await w.request('GET', '/installs?status=closed', site)).json()).installs
  assert.deepEqual(closed.map((r) => r.install_id), [b.body.install_id])
  assert.equal((await w.request('GET', '/installs?status=bogus', site)).status, 400)
})
