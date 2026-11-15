import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ENV, CUSTOMER_ID, PIN, DAY_MS, makeWorld, createInstall } from './helpers.mjs'
import { removeKeyCommand } from '../src/install.ts'

const esc = ENV.DISCORD_ESCALATION_CHANNEL_ID
const MIN_MS = 60_000

async function scheduled(w, inMs = DAY_MS) {
  const { body, core } = await createInstall(w)
  const slot = Math.floor((w.clock.ms + inMs) / 1000)
  await core.patchSchedule(slot)
  return { id: body.install_id, channel: body.channel_id, core, slot }
}

async function greenAll(core) {
  for (const h of ['discord_bot', 'claude_plan', 'hetzner_token', 'openai_key', 'agent_gmail']) await core.setHomework(h, 'green')
}

test('start rule: at the slot with homework green, the run starts', async () => {
  const w = makeWorld()
  const { core, id, slot } = await scheduled(w)
  await greenAll(core)
  w.clock.ms = slot * 1000
  await core.onAlarm()
  assert.equal(await w.stores.get(id).get('run_started_at'), slot)
  const notes = core.journalView().body.journal.map((j) => j.body)
  assert.ok(notes.some((b) => /run started/.test(b)))
  // The runbook agent is a TODO, so the stub hands the run to a human.
  assert.equal(core.row().status, 'needs_human')
})

test('start rule: homework not green posts once, rechecks every 15 min, escalates after 2h', async () => {
  const w = makeWorld()
  const { core, id, channel, slot } = await scheduled(w)
  await core.setHomework('discord_bot', 'green')
  w.clock.ms = slot * 1000
  await core.onAlarm()
  assert.equal(core.row().status, 'scheduled')
  assert.equal(w.alarms.get(id), slot * 1000 + 15 * MIN_MS)
  const waitPosts = () => w.discord.posts(channel).filter((p) => /aren't done yet/.test(p))
  assert.equal(waitPosts().length, 1)

  for (let i = 1; i < 8; i++) {
    w.clock.ms = w.alarms.get(id)
    await core.onAlarm()
    assert.equal(core.row().status, 'scheduled', `check ${i}`)
  }
  assert.equal(waitPosts().length, 1, 'no repeat post every 15 min')
  assert.equal(w.alarms.get(id), slot * 1000 + 120 * MIN_MS)

  w.clock.ms = w.alarms.get(id)
  await core.onAlarm()
  assert.equal(core.row().status, 'needs_human')
  const ops = w.discord.posts(esc)
  assert.equal(ops.length, 1)
  assert.match(ops[0], new RegExp(id))
  assert.match(ops[0], /journal: https:\/\/installer\.test\/installs\/inst_/)
  assert.match(ops[0], /attempts: 9/)
})

test('start rule: going green during the recheck window starts the run', async () => {
  const w = makeWorld()
  const { core, id, slot } = await scheduled(w)
  w.clock.ms = slot * 1000
  await core.onAlarm()
  await greenAll(core)
  w.clock.ms = w.alarms.get(id)
  await core.onAlarm()
  assert.ok(await w.stores.get(id).get('run_started_at'))
})

test('caps: spend reaching $40 while running sets needs_human and escalates', async () => {
  const w = makeWorld()
  const { core } = await createInstall(w)
  core.update({ status: 'running' })
  await core.chargeSpend(2500, 3, 'compose up')
  assert.equal(core.row().status, 'running')
  await core.chargeSpend(1500, 4, 'discord session')
  assert.equal(core.row().status, 'needs_human')
  assert.equal(core.row().spend_usd_cents, 4000)
  assert.match(w.discord.posts(esc)[0], /spend cap hit/)
})

test('caps: the wall-clock alarm escalates a run still going after 4h', async () => {
  const w = makeWorld()
  const { core, body } = await createInstall(w)
  core.update({ status: 'running' })
  await w.stores.get(body.install_id).put('run_started_at', w.clock.s)
  w.clock.advance(4 * 3600 * 1000)
  await core.onAlarm()
  assert.equal(core.row().status, 'needs_human')
  assert.match(w.discord.posts(esc)[0], /wall-clock cap/)
})

// GC: the install finishes at F. Discord's message list drives the clock.
async function finished(w, { pinned = true } = {}) {
  const { body, core } = await createInstall(w)
  if (pinned) await core.pinServer(PIN.serverIp, PIN.hostKeyFp)
  await core.finish('done')
  return { id: body.install_id, channel: body.channel_id, core, F: w.clock.ms }
}

test('gc: warns at day 11 and closes at day 14 when the customer stays quiet', async () => {
  const w = makeWorld()
  const { id, channel, core, F } = await finished(w)
  assert.equal(w.alarms.get(id), F + 11 * DAY_MS)

  w.clock.ms = F + 11 * DAY_MS
  await core.onAlarm()
  assert.ok(w.discord.posts(channel).some((p) => /closes <t:\d+:R>/.test(p)))
  assert.equal(core.row().close_warned_at, w.clock.s)
  assert.equal(w.alarms.get(id), F + 14 * DAY_MS)

  w.clock.ms = F + 14 * DAY_MS
  await core.onAlarm()
  assert.equal(core.row().status, 'closed')
  assert.equal(w.alarms.get(id), null)
  assert.ok(w.discord.calls.some((c) => c.method === 'DELETE' && c.path.endsWith(`/channels/${channel}`)))
})

test('gc: a customer message after the warning resets the clock and cancels the close', async () => {
  const w = makeWorld()
  const { id, channel, core, F } = await finished(w)
  w.clock.ms = F + 11 * DAY_MS
  await core.onAlarm()
  assert.ok(core.row().close_warned_at)

  const msgAt = F + 12 * DAY_MS
  w.discord.state.customerMessages = [
    { author: { id: '999999999999999999' }, timestamp: new Date(F + 13 * DAY_MS).toISOString() },
    { author: { id: CUSTOMER_ID }, timestamp: new Date(msgAt).toISOString() },
  ]
  w.clock.ms = F + 14 * DAY_MS
  await core.onAlarm()
  assert.equal(core.row().status, 'done', 'not closed')
  assert.equal(core.row().last_customer_msg_at, msgAt / 1000, 'only the customer counts')
  assert.equal(core.row().close_warned_at, null, 'old warning cleared')
  assert.equal(w.alarms.get(id), msgAt + 11 * DAY_MS)
  assert.ok(!w.discord.calls.some((c) => c.method === 'DELETE'))

  w.clock.ms = msgAt + 11 * DAY_MS
  await core.onAlarm()
  w.clock.ms = msgAt + 14 * DAY_MS
  await core.onAlarm()
  assert.equal(core.row().status, 'closed')
  void channel
})

test('gc: if the channel cannot be read, it does not close and retries in an hour', async () => {
  const w = makeWorld()
  const { id, core, F } = await finished(w)
  w.discord.state.failRead = true
  w.clock.ms = F + 15 * DAY_MS
  await core.onAlarm()
  assert.equal(core.row().status, 'done')
  assert.equal(core.row().close_warned_at, null)
  assert.equal(w.alarms.get(id), w.clock.ms + 3600 * 1000)
})

test('close: removes our key from the box, deletes it from storage, never calls Hetzner', async () => {
  const w = makeWorld()
  const { id, core, F } = await finished(w)
  const store = w.stores.get(id)
  await store.put('hetzner_token', 'h'.repeat(64))
  assert.ok(await store.get('ssh_private_key'))
  w.clock.ms = F + 11 * DAY_MS
  await core.onAlarm()
  w.clock.ms = F + 14 * DAY_MS
  await core.onAlarm()

  assert.equal(w.box.requests.length, 1)
  const req = w.box.requests[0].body
  assert.equal(req.cmd, removeKeyCommand(id))
  assert.equal(req.host, PIN.serverIp)
  assert.equal(req.host_key_fp, PIN.hostKeyFp)
  assert.equal(await store.get('ssh_private_key'), undefined)
  assert.equal(await store.get('hetzner_token'), undefined)
  assert.ok(w.discord.calls.every((c) => !/hetzner/i.test(c.path)))
  // The only box command on close is the authorized_keys edit: no shutdown,
  // no delete, nothing else.
  assert.match(req.cmd, /^sed -i '\/ behalfbot-installer-inst_[0-9A-Z]{26}\$\/d' ~\/\.ssh\/authorized_keys$/)
})

test('close: a failed key removal still deletes our private key and tells ops', async () => {
  const w = makeWorld({ box: { requests: [], send: async () => new Response('{}', { status: 500 }) } })
  const { id, core } = await finished(w)
  await core.close('test')
  assert.equal(await w.stores.get(id).get('ssh_private_key'), undefined)
  assert.equal(core.row().status, 'closed')
  assert.match(w.discord.posts(esc)[0], /could not remove the installer key/)
})

test('close with no server pinned skips the box and still closes', async () => {
  const w = makeWorld()
  const { core } = await finished(w, { pinned: false })
  await core.close('test')
  assert.equal(w.box.requests.length, 0)
  assert.equal(core.row().status, 'closed')
})
