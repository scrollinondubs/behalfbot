import { test } from 'node:test'
import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { ENV, makeWorld, createInstall } from './helpers.mjs'
import { CODE_TTL_MS, MAX_SCREENSHOT_B64, RECONNECT_GRACE_MS, SECRET_TTL_MS, STALE_MS, PAIR_FAILS_PER_MIN, UNCONNECTED_TTL_MS, toRef } from '../src/autopilot.ts'

const admin = { token: ENV.INSTALLER_ADMIN_SECRET }

// Built at run time so no key-shaped string sits in the repo.
function fakeGoogleKey() {
  const abc = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'
  return 'AIza' + [...randomBytes(35)].map((b) => abc[b % abc.length]).join('')
}

async function setup(runbook = 'google-places') {
  const w = makeWorld()
  const { body } = await createInstall(w)
  const id = body.install_id
  const res = await w.request('POST', `/installs/${id}/autopilot/code`, { ...admin, body: { runbook } })
  return { w, id, res, code: (await res.json()).code }
}

// helpers.request has no header option; sockets need the upgrade header.
async function ws(w, token) {
  return w.autopilot.connect(new Request(`https://installer.test/autopilot/ws?token=${token}`, { headers: { upgrade: 'websocket' } }))
}

async function journalText(w, id) {
  const r = await w.request('GET', `/installs/${id}/journal`, admin)
  return JSON.stringify((await r.json()).journal)
}

async function cmd(w, session, body) {
  const r = await w.request('POST', `/autopilot/${session}/cmd`, { ...admin, body })
  return { status: r.status, body: await r.json() }
}

test('code: admin only, XXX-XXX, bound to the install and the runbook allowlist', async () => {
  const { w, id, res, code } = await setup()
  assert.equal(res.status, 201)
  assert.match(code, /^[A-Z2-9]{3}-[A-Z2-9]{3}$/)
  const again = await w.request('POST', `/installs/${id}/autopilot/code`, { body: {} })
  assert.equal(again.status, 401)
  const missing = await w.request('POST', `/installs/inst_01JZZZZZZZZZZZZZZZZZZZZZZZ/autopilot/code`, { ...admin, body: {} })
  assert.equal(missing.status, 404)
  const bad = await w.request('POST', `/installs/${id}/autopilot/code`, { ...admin, body: { runbook: 'nope' } })
  assert.equal(bad.status, 400)
  const p = await (await w.request('POST', '/autopilot/pair', { body: { code } })).json()
  assert.deepEqual(p.allow, ['console.cloud.google.com'])
  assert.match(p.session, /^aps_/)
  assert.ok(p.token.length >= 43)
  assert.match(await journalText(w, id), /pairing code issued/)
})

test('pairing: a code works once, even typed lower-case without the dash', async () => {
  const { w, code } = await setup()
  const loose = code.toLowerCase().replace('-', ' ')
  const first = await w.request('POST', '/autopilot/pair', { body: { code: loose } })
  assert.equal(first.status, 200)
  const second = await w.request('POST', '/autopilot/pair', { body: { code } })
  assert.equal(second.status, 404)
})

test('pairing: a code stops working after 10 minutes, and the failed read still deletes it', async () => {
  const { w, code } = await setup()
  w.clock.advance(CODE_TTL_MS + 1)
  const r = await w.request('POST', '/autopilot/pair', { body: { code } })
  assert.equal(r.status, 404)
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_code').length, 0)
})

test('pairing: failed attempts are capped per minute, then reset', async () => {
  const { w, code } = await setup()
  for (let i = 0; i < PAIR_FAILS_PER_MIN; i++) {
    const r = await w.request('POST', '/autopilot/pair', { body: { code: 'ZZZ-ZZZ' } })
    assert.equal(r.status, 404)
  }
  const blocked = await w.request('POST', '/autopilot/pair', { body: { code } })
  assert.equal(blocked.status, 429)
  w.clock.advance(60_000)
  const ok = await w.request('POST', '/autopilot/pair', { body: { code } })
  assert.equal(ok.status, 200)
})

test('socket: needs a live token; the same token again replaces the old socket', async () => {
  const s = await setup()
  const p = await (await s.w.request('POST', '/autopilot/pair', { body: { code: s.code } })).json()
  assert.equal((await ws(s.w, 'not-a-token')).status, 401)
  const first = await ws(s.w, p.token)
  assert.ok(first.ws)
  const second = await ws(s.w, p.token)
  assert.ok(second.ws)
  assert.equal(first.ws.closed.code, 4000)
  const list = await (await s.w.request('GET', `/installs/${s.id}/autopilot`, admin)).json()
  assert.equal(list.sessions.length, 1)
  assert.equal(list.sessions[0].connected, true)
  second.ws.answer = () => ({ ok: true, result: 'via second' })
  first.ws.answer = () => ({ ok: true, result: 'via first' })
  assert.equal((await cmd(s.w, p.session, { op: 'note', label: 'x' })).body.result, 'via second')
})

test('cmd: admin only; a method off the list never reaches the browser', async () => {
  const { w, session } = await pairedSession()
  const anon = await w.request('POST', `/autopilot/${session.id}/cmd`, { body: { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' } })
  assert.equal(anon.status, 401)
  for (const bad of [
    { op: 'cdp', method: 'Runtime.evaluate', params: { expression: 'document.cookie' }, label: 'x' },
    { op: 'cdp', method: 'Network.getAllCookies', label: 'x' },
    { op: 'raw', label: 'x' },
    { op: 'cdp', method: 'DOM.getDocument', label: 'x' },
  ]) {
    const r = await cmd(w, session.id, bad)
    assert.equal(r.status, 400, JSON.stringify(bad))
  }
  assert.equal(session.ws.commands().length, 0)
})

test('cmd: forwards to the extension, returns its reply, journals the label', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = (c) => ({ ok: true, result: { echoed: c.method } })
  const r = await cmd(w, session.id, { op: 'cdp', method: 'DOM.getBoxModel', params: { backendNodeId: 4 }, label: 'Read the page' })
  assert.equal(r.status, 200)
  assert.deepEqual(r.body.result, { echoed: 'DOM.getBoxModel' })
  const sent = session.ws.commands()[0]
  assert.match(sent.id, /^apc_/)
  assert.equal(sent.tabId, undefined)
  assert.match(await journalText(w, id), /autopilot: Read the page/)
})

test('allowlist: a refusal from the extension comes back as an error and is journaled', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = () => ({ ok: false, error: 'tab not allowed' })
  const r = await cmd(w, session.id, { op: 'cdp', method: 'Page.navigate', params: { url: 'https://evil.example/' }, label: 'Open evil' })
  assert.equal(r.body.ok, false)
  assert.equal(r.body.error, 'tab not allowed')
  assert.match(await journalText(w, id), /Open evil: failed \(tab not allowed\)/)
})

test('allowlist: an unlabelled refusal is journaled too', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = () => ({ ok: false, error: 'tab not allowed' })
  await cmd(w, session.id, { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' })
  assert.match(await journalText(w, id), /Accessibility.getFullAXTree: failed \(tab not allowed\)/)
})

test('secret: the value never appears in a cmd reply or the journal, and is fetchable exactly once', async () => {
  const { w, id, session } = await pairedSession()
  const key = fakeGoogleKey()
  const item = 'Behalf.bot - Google Places API key (Kit)'
  session.ws.answer = (c) => {
    if (c.op === 'save_secret') return { ok: true, secret: true, value: key }
    // A snapshot taken while the key is on screen.
    return { ok: true, result: [{ ref: 9, role: 'StaticText', name: `Your API key ${key}` }] }
  }
  const before = await cmd(w, session.id, { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' })
  assert.ok(!JSON.stringify(before.body).includes(key), 'pattern redaction catches it before capture')

  const saved = await cmd(w, session.id, { op: 'save_secret', ref: 9, secretTarget: item, label: 'Save the key to the vault' })
  assert.equal(saved.status, 200)
  assert.equal(saved.body.result, 'captured')
  assert.ok(!JSON.stringify(saved.body).includes(key))

  const after = await cmd(w, session.id, { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' })
  assert.ok(!JSON.stringify(after.body).includes(key))
  assert.match(JSON.stringify(after.body), /\[REDACTED\]/)

  const journal = await journalText(w, id)
  assert.ok(!journal.includes(key))
  assert.ok(!journal.includes(key.slice(4, 30)))
  assert.match(journal, /secret captured for vault item/)

  const anon = await w.request('POST', `/autopilot/${session.id}/secret`, { body: { item } })
  assert.equal(anon.status, 401)
  const take = await w.request('POST', `/autopilot/${session.id}/secret`, { ...admin, body: { item } })
  assert.equal(take.status, 200)
  assert.equal(take.headers.get('cache-control'), 'no-store')
  assert.equal(await take.text(), key)
  const again = await w.request('POST', `/autopilot/${session.id}/secret`, { ...admin, body: { item } })
  assert.equal(again.status, 404)
  assert.ok(!(await journalText(w, id)).includes(key))
})

test('secret: a value sent on a non-save_secret command is dropped', async () => {
  const { w, session } = await pairedSession()
  const key = 'not-a-google-shape-' + randomBytes(12).toString('hex')
  session.ws.answer = () => ({ ok: true, secret: true, value: key })
  const r = await cmd(w, session.id, { op: 'cdp', method: 'DOM.getBoxModel', params: { backendNodeId: 4 }, label: '' })
  assert.equal(r.body.ok, false)
  assert.ok(!JSON.stringify(r.body).includes(key))
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_secret').length, 0)
})

test('secret: gone after the hold time, both on read and from storage', async () => {
  const { w, session } = await pairedSession()
  const item = 'k'
  session.ws.answer = () => ({ ok: true, secret: true, value: fakeGoogleKey() })
  await cmd(w, session.id, { op: 'save_secret', ref: 3, secretTarget: item, label: '' })
  assert.ok(w.autopilot.alarm() !== null && w.autopilot.alarm() <= w.clock.now() + 30_000)
  w.clock.advance(SECRET_TTL_MS + 1)
  await w.autopilot.core.onAlarm()
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_secret').length, 0)
  const take = await w.request('POST', `/autopilot/${session.id}/secret`, { ...admin, body: { item } })
  assert.equal(take.status, 404)
  assert.ok(SECRET_TTL_MS + 30_000 <= 5 * 60 * 1000)
})

test('human step: a slow answer comes back as 202 pending, then by polling', async () => {
  const { w, session } = await pairedSession()
  const r = await cmd(w, session.id, { op: 'human_step', label: 'Link a billing account', wait_ms: 5 })
  assert.equal(r.status, 202)
  assert.equal(r.body.pending, true)
  const sent = session.ws.commands()[0]
  setTimeout(() => w.autopilot.core.onMessage(session.ws, JSON.stringify({ type: 'reply', id: sent.id, ok: true, result: 'done' })), 5)
  const p = await w.request('GET', `/autopilot/${session.id}/cmd/${r.body.id}?wait_ms=1000`, admin)
  assert.equal(p.status, 200)
  assert.equal((await p.json()).result, 'done')
})

test('teardown: a bye from the extension deletes the session, its token and any held secret at once', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = (c) => (c.op === 'save_secret' ? { ok: true, secret: true, value: fakeGoogleKey() } : null)
  await cmd(w, session.id, { op: 'save_secret', ref: 3, secretTarget: 'k', label: '' })
  const pending = cmd(w, session.id, { op: 'human_step', label: 'Your turn', wait_ms: 5000 })
  await new Promise((r) => setTimeout(r, 5))
  await w.autopilot.core.onMessage(session.ws, JSON.stringify({ type: 'bye', text: 'Disconnected by you' }))
  assert.equal(session.ws.closed.code, 1000)
  await w.autopilot.disconnect(session.ws)
  const p = await pending
  assert.equal(p.body.ok, false)
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_session').length, 0)
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_secret').length, 0)
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_cmd').length, 0)
  assert.equal((await ws(w, session.token)).status, 401)
  assert.equal((await cmd(w, session.id, { op: 'note', label: 'x' })).status, 404)
  const list = await (await w.request('GET', `/installs/${id}/autopilot`, admin)).json()
  assert.equal(list.sessions.length, 0)
  assert.match(await journalText(w, id), /Disconnected by you \(session aps_\w+ ended\)/)
})

// #281: the socket died one-sidedly around a human step and every later call
// failed. A drop now keeps the session for the extension to come back to.
test('reconnect: Done pressed across a dropped socket still lands, and the session keeps working', async () => {
  const { w, id, session } = await pairedSession()
  const hs = cmd(w, session.id, { op: 'human_step', label: 'Set Duration to Forever', wait_ms: 5 })
  assert.equal((await hs).status, 202)
  const hsId = (await hs).body.id
  const lost = cmd(w, session.id, { op: 'cdp', method: 'DOM.getBoxModel', params: { backendNodeId: 4 }, label: '', wait_ms: 5 })
  assert.equal((await lost).status, 202)
  await w.autopilot.disconnect(session.ws)

  const mid = (await (await w.request('GET', `/installs/${id}/autopilot`, admin)).json()).sessions[0]
  assert.equal(mid.connected, false)
  assert.equal(mid.reconnecting, true)
  assert.equal(mid.tab_url, null)
  const refused = await cmd(w, session.id, { op: 'note', label: 'x' })
  assert.equal(refused.status, 409)
  assert.match(refused.body.error, /not answering/)

  w.clock.advance(RECONNECT_GRACE_MS - 1000)
  await w.autopilot.core.onAlarm()
  const back = (await ws(w, session.token)).ws
  assert.ok(back, 'same token reconnects inside the grace window')
  // The extension flushes the Done it held, then says hello.
  await w.autopilot.core.onMessage(back, JSON.stringify({ type: 'reply', id: hsId, ok: true, result: 'done' }))
  await w.autopilot.core.onMessage(back, JSON.stringify({ type: 'hello', tab_url: 'https://console.cloud.google.com/x' }))
  const done = await w.request('GET', `/autopilot/${session.id}/cmd/${hsId}?wait_ms=10`, admin)
  assert.equal((await done.json()).result, 'done')
  const failed = await (await w.request('GET', `/autopilot/${session.id}/cmd/${(await lost).body.id}?wait_ms=10`, admin)).json()
  assert.equal(failed.ok, false)
  assert.match(failed.error, /reconnected before answering/)
  assert.equal(back.commands().length, 0, 'nothing but a pending human step is ever re-sent')

  back.answer = (c) => ({ ok: true, result: c.op })
  const after = await cmd(w, session.id, { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' })
  assert.equal(after.status, 200)
  w.clock.advance(RECONNECT_GRACE_MS)
  back.lastPing = w.clock.now()
  await w.autopilot.core.onAlarm()
  const list = (await (await w.request('GET', `/installs/${id}/autopilot`, admin)).json()).sessions
  assert.equal(list.length, 1)
  assert.equal(list[0].connected, true)
  assert.equal(list[0].tab_url, 'https://console.cloud.google.com/x')
  assert.match(await journalText(w, id), /connection dropped.*browser reconnected/s)
})

test('reconnect: a human step still waiting for Done is shown again on the new socket', async () => {
  const { w, session } = await pairedSession()
  const hs = await cmd(w, session.id, { op: 'human_step', label: 'Your turn', wait_ms: 5 })
  await w.autopilot.disconnect(session.ws)
  w.clock.advance(1000)
  const back = (await ws(w, session.token)).ws
  await w.autopilot.core.onMessage(back, JSON.stringify({ type: 'hello', tab_url: 'https://console.cloud.google.com/' }))
  assert.deepEqual(back.commands(), [{ id: hs.body.id, op: 'human_step', label: 'Your turn' }])
})

test('reconnect: a socket that never comes back is ended by the sweep after the grace window', async () => {
  const { w, id, session } = await pairedSession()
  const pending = cmd(w, session.id, { op: 'human_step', label: 'Your turn', wait_ms: 5000 })
  await new Promise((r) => setTimeout(r, 5))
  await w.autopilot.disconnect(session.ws)
  assert.ok(w.autopilot.alarm() !== null, 'the sweep is scheduled')
  w.clock.advance(RECONNECT_GRACE_MS + 1)
  await w.autopilot.core.onAlarm()
  assert.equal((await pending).body.ok, false)
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_session').length, 0)
  assert.equal((await ws(w, session.token)).status, 401)
  assert.equal(w.autopilot.alarm(), null)
  assert.match(await journalText(w, id), /did not come back/)
})

test('reconnect: the late close of a replaced socket changes nothing', async () => {
  const { w, id, session } = await pairedSession()
  const back = (await ws(w, session.token)).ws
  await w.autopilot.disconnect(session.ws)
  const list = (await (await w.request('GET', `/installs/${id}/autopilot`, admin)).json()).sessions
  assert.equal(list[0].connected, true)
  assert.equal(list[0].reconnecting, false)
  back.answer = () => ({ ok: true, result: 'ok' })
  assert.equal((await cmd(w, session.id, { op: 'note', label: 'x' })).status, 200)
})

test('liveness: a socket nothing came over for STALE_MS reads connected:false, and commands are refused', async () => {
  const { w, id, session } = await pairedSession()
  const list = async () => (await (await w.request('GET', `/installs/${id}/autopilot`, admin)).json()).sessions[0]
  assert.equal((await list()).connected, true)
  w.clock.advance(STALE_MS + 1)
  const stale = await list()
  assert.equal(stale.connected, false)
  assert.equal(stale.reconnecting, true)
  assert.equal(stale.tab_url, null)
  assert.equal((await cmd(w, session.id, { op: 'note', label: 'x' })).status, 409)
  assert.equal(session.ws.commands().length, 0)
  // A keepalive ping the runtime answered counts as heard from.
  session.ws.lastPing = w.clock.now()
  assert.equal((await list()).connected, true)
  w.clock.advance(STALE_MS + 1)
  await w.autopilot.core.onMessage(session.ws, JSON.stringify({ type: 'tab', tab_url: 'https://console.cloud.google.com/y' }))
  assert.equal((await list()).connected, true)
})

test('teardown: DELETE ends the session and closes the socket', async () => {
  const { w, session } = await pairedSession()
  const r = await w.request('DELETE', `/autopilot/${session.id}`, admin)
  assert.equal(r.status, 200)
  assert.ok(session.ws.closed)
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_session').length, 0)
})

test('teardown: a pairing whose socket never opens is swept', async () => {
  const s = await setup()
  await s.w.request('POST', '/autopilot/pair', { body: { code: s.code } })
  s.w.clock.advance(UNCONNECTED_TTL_MS + 1)
  await s.w.autopilot.core.onAlarm()
  assert.equal(s.w.autopilot.core['rows']('SELECT * FROM ap_session').length, 0)
})

test('dev runbook: refused unless the Worker allows dev runbooks', async () => {
  const w = makeWorld()
  const { body } = await createInstall(w)
  const r = await w.autopilot.core.createCode(body.install_id, 'local-test', false)
  assert.equal(r.status, 400)
})

test('refs: a ref sent as "2378" or "[2378]" reaches the extension as the number 2378', async () => {
  const { w, session } = await pairedSession()
  session.ws.answer = () => ({ ok: true, result: null })
  for (const ref of [2378, '2378', '[2378]', ' 2378 ']) {
    const r = await cmd(w, session.id, { op: 'cdp', method: 'DOM.focus', params: { backendNodeId: ref }, label: '' })
    assert.equal(r.status, 200, JSON.stringify(ref))
  }
  session.ws.answer = (c) => (c.op === 'save_secret' ? { ok: true, secret: true, value: fakeGoogleKey() } : null)
  await cmd(w, session.id, { op: 'save_secret', ref: '2378', secretTarget: 'k', label: '' })
  const sent = session.ws.commands()
  for (const c of sent.slice(0, 4)) assert.strictEqual(c.params.backendNodeId, 2378)
  assert.strictEqual(sent[4].ref, 2378)
})

test('refs: anything that is not a positive int32 is refused before the browser', async () => {
  const { w, session } = await pairedSession()
  for (const ref of ['12abc', '1.5', 1.5, 0, -3, '', 'abc', 2 ** 31, null, [12], '99999999999']) {
    const r = await cmd(w, session.id, { op: 'cdp', method: 'DOM.focus', params: { backendNodeId: ref }, label: '' })
    assert.equal(r.status, 400, JSON.stringify(ref))
    const s = await cmd(w, session.id, { op: 'save_secret', ref, secretTarget: 'k', label: '' })
    assert.equal(s.status, 400, JSON.stringify(ref))
  }
  assert.equal(session.ws.commands().length, 0)
  assert.equal(toRef('[ 7 ]'), 7)
  assert.equal(toRef(2 ** 31 - 1), 2 ** 31 - 1)
})

// Autopilot-only customers: already installed, they need a browser session
// and nothing else.
async function customerSetup(customer = 'Chelsea') {
  const w = makeWorld()
  const res = await w.request('POST', '/autopilot/sessions', { ...admin, body: { customer } })
  return { w, res, body: await res.json() }
}

async function customerJournal(w, customer) {
  const r = await w.request('GET', `/autopilot/sessions?customer=${encodeURIComponent(customer)}`, admin)
  return { status: r.status, body: await r.json() }
}

test('customer session: no install, no registry row, and not one Discord call', async () => {
  const { w, res, body } = await customerSetup()
  assert.equal(res.status, 201)
  assert.match(body.customer_id, /^apcu_/)
  assert.equal(body.customer, 'Chelsea')
  assert.match(body.code, /^[A-Z2-9]{3}-[A-Z2-9]{3}$/)
  assert.deepEqual(body.allow, ['console.cloud.google.com'])

  const p = await (await w.request('POST', '/autopilot/pair', { body: { code: body.code } })).json()
  assert.match(p.session, /^aps_/)
  const conn = await ws(w, p.token)
  assert.ok(conn.ws)
  conn.ws.answer = () => ({ ok: true, result: [] })
  const r = await cmd(w, p.session, { op: 'cdp', method: 'Accessibility.getFullAXTree', label: 'Read the page' })
  assert.equal(r.status, 200)
  await w.request('DELETE', `/autopilot/${p.session}`, admin)

  assert.equal(w.discord.calls.length, 0)
  assert.equal(w.registry.list('all').length, 0)
  const installs = await w.request('GET', '/installs?status=all', { token: ENV.INSTALLER_SITE_SECRET })
  assert.deepEqual((await installs.json()).installs, [])
  assert.equal(w.alarms.size, 0)
})

test('customer session: the journal lands in the relay and reads back by name', async () => {
  const { w, body } = await customerSetup('  Chelsea   Example ')
  assert.equal(body.customer, 'Chelsea Example')
  const p = await (await w.request('POST', '/autopilot/pair', { body: { code: body.code } })).json()
  const conn = await ws(w, p.token)
  const key = fakeGoogleKey()
  conn.ws.answer = (c) => (c.op === 'save_secret' ? { ok: true, secret: true, value: key } : { ok: true, result: null })
  await cmd(w, p.session, { op: 'note', label: 'Open the credentials page' })
  await cmd(w, p.session, { op: 'save_secret', ref: '9', secretTarget: 'Places key', label: '' })

  const v = await customerJournal(w, 'chelsea example')
  assert.equal(v.status, 200)
  assert.equal(v.body.customer_id, body.customer_id)
  assert.equal(v.body.sessions.length, 1)
  assert.equal(v.body.sessions[0].connected, true)
  const lines = v.body.journal.map((j) => j.line).join('\n')
  assert.match(lines, /pairing code issued/)
  assert.match(lines, /browser paired/)
  assert.match(lines, /browser connected/)
  assert.match(lines, /autopilot: Open the credentials page/)
  assert.match(lines, /secret captured for vault item "Places key"/)
  assert.ok(!lines.includes(key))

  const take = await w.request('POST', `/autopilot/${p.session}/secret`, { ...admin, body: { item: 'Places key' } })
  assert.equal(await take.text(), key)
})

test('customer session: same name reuses the customer; admin only; bad input refused', async () => {
  const { w, body } = await customerSetup()
  const again = await (await w.request('POST', '/autopilot/sessions', { ...admin, body: { customer: 'chelsea' } })).json()
  assert.equal(again.customer_id, body.customer_id)
  assert.notEqual(again.code, body.code)

  assert.equal((await w.request('POST', '/autopilot/sessions', { body: { customer: 'X' } })).status, 401)
  assert.equal((await w.request('GET', '/autopilot/sessions?customer=Chelsea')).status, 401)
  for (const bad of [{}, { customer: '' }, { customer: '   ' }, { customer: 7 }, { customer: 'x'.repeat(101) }, { customer: 'Y', runbook: 'nope' }, { customer: 'Y', runbook: 3 }]) {
    const r = await w.request('POST', '/autopilot/sessions', { ...admin, body: bad })
    assert.equal(r.status, 400, JSON.stringify(bad))
  }
  assert.equal((await customerJournal(w, 'Nobody')).status, 404)
  assert.equal((await w.request('PUT', '/autopilot/sessions', { ...admin, body: {} })).status, 405)
  // A customer id is not an install id, so the install routes never take it.
  assert.equal((await w.request('POST', `/installs/${body.customer_id}/autopilot/code`, { ...admin, body: {} })).status, 404)
  assert.equal(w.discord.calls.length, 0)
})

test('customer session: an install still journals to its own InstallDO, not the relay', async () => {
  const { w, id } = await setup()
  assert.equal(w.autopilot.core['rows']('SELECT * FROM ap_journal').length, 0)
  assert.match(await journalText(w, id), /pairing code issued/)
})

async function pairedSession() {
  const s = await setup()
  const p = await (await s.w.request('POST', '/autopilot/pair', { body: { code: s.code } })).json()
  const conn = await ws(s.w, p.token)
  return { w: s.w, id: s.id, session: { id: p.session, token: p.token, ws: conn.ws } }
}

test('aws-backup-hardening: pairs with the AWS console only, start page on the allow list, no secret step', async () => {
  const { res, code, w } = await setup('aws-backup-hardening')
  assert.equal(res.status, 201)
  const p = await (await w.request('POST', '/autopilot/pair', { body: { code } })).json()
  assert.deepEqual(p.allow, ['console.aws.amazon.com'])
  const { hostAllowed } = await import('../../../chrome-ext/policy.js')
  assert.ok(hostAllowed(p.start_url, p.allow))
  assert.ok(hostAllowed('https://eu-west-1.console.aws.amazon.com/s3/home', p.allow))
  assert.ok(!hostAllowed('https://signin.aws.amazon.com/oauth', p.allow))
  const { runbook } = await import('../src/runbooks.ts')
  assert.ok(!runbook('aws-backup-hardening').steps.some((s) => s.save_secret))
})

test('CRM contact exports: each pairs, starts on its allow list, keeps sign-in and mail off it, and leaves the final export to the person', async () => {
  const { hostAllowed } = await import('../../../chrome-ext/policy.js')
  const { runbook } = await import('../src/runbooks.ts')
  const cases = {
    'linkedin-connections-export': { off: ['https://www.google.com/'] },
    'google-contacts-export': { off: ['https://accounts.google.com/signin', 'https://mail.google.com/mail/u/0/'] },
    'outlook-contacts-export': { off: ['https://login.live.com/', 'https://login.microsoftonline.com/'] },
    'icloud-contacts-export': { off: ['https://idmsa.apple.com/', 'https://p42-contacts.icloud.com/'] },
  }
  for (const [id, { off }] of Object.entries(cases)) {
    const { res, code, w } = await setup(id)
    assert.equal(res.status, 201, id)
    const p = await (await w.request('POST', '/autopilot/pair', { body: { code } })).json()
    assert.ok(hostAllowed(p.start_url, p.allow), `${id} start_url`)
    for (const url of off) assert.ok(!hostAllowed(url, p.allow), `${id} must not allow ${url}`)
    const steps = runbook(id).steps
    assert.ok(!steps.some((s) => s.save_secret), `${id} saves no secret`)
    assert.ok(steps.at(-1).human || id === 'linkedin-connections-export', `${id} ends on a human step`)
  }
  const li = runbook('linkedin-connections-export')
  assert.match(JSON.stringify(li.steps), /larger data archive/)
  assert.ok(li.steps.find((s) => s.id === 'request').human, 'Request archive is pressed by the person')
  assert.match(li.accept, /ONLY Connections\.csv/)
})

// A JPEG-shaped base64 body with long alphanumeric runs, the kind the text
// redactor's 64-character pattern would cut into.
function fakeJpegB64(len = 4000) {
  const abc = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  let s = '/9j/'
  while (s.length < len) s += [...randomBytes(64)].map((b) => abc[b % abc.length]).join('') + '+'
  return s.slice(0, len - (len % 4))
}

test('screenshot: the image passes through byte for byte; the label is fixed whatever the driver sends', async () => {
  const { w, id, session } = await pairedSession()
  const data = fakeJpegB64()
  session.ws.answer = (c) => (c.op === 'screenshot' ? { ok: true, result: { mime: 'image/jpeg', width: 1280, height: 800, masked: 3, data, extra: 'dropped' } } : null)
  const r = await cmd(w, session.id, { op: 'screenshot', label: 'Nothing to see here' })
  assert.equal(r.status, 200)
  assert.equal(r.body.result.data, data)
  assert.deepEqual({ ...r.body.result, data: undefined }, { mime: 'image/jpeg', width: 1280, height: 800, masked: 3, data: undefined })
  assert.equal(session.ws.commands()[0].label, 'Took a screenshot of the tab')
  const j = await journalText(w, id)
  assert.match(j, /Took a screenshot of the tab/)
  assert.ok(!j.includes('Nothing to see here'))
})

test('screenshot: anything but a JPEG within the size cap is refused', async () => {
  const { w, session } = await pairedSession()
  for (const result of [
    { mime: 'image/png', data: fakeJpegB64() },
    { mime: 'image/jpeg', data: 'not base64 at all!' },
    { mime: 'image/jpeg', data: 'iVBORw0KGgo' + 'A'.repeat(100) },
    { mime: 'image/jpeg', data: fakeJpegB64(MAX_SCREENSHOT_B64 + 400) },
    'a string',
  ]) {
    session.ws.answer = () => ({ ok: true, result })
    const r = await cmd(w, session.id, { op: 'screenshot', label: '' })
    assert.equal(r.body.ok, false, JSON.stringify(result).slice(0, 60))
    assert.match(r.body.error, /JPEG/)
  }
})

test('screenshot: the text fields of the reply are still redacted', async () => {
  const { w, session } = await pairedSession()
  const key = fakeGoogleKey()
  session.ws.answer = () => ({ ok: false, error: `boom ${key}` })
  const r = await cmd(w, session.id, { op: 'screenshot', label: '' })
  assert.ok(!JSON.stringify(r.body).includes(key))
})

test('clear and challenge: forwarded with a numeric ref; clear without a ref never reaches the browser', async () => {
  const { w, session } = await pairedSession()
  session.ws.answer = (c) => (c.op === 'clear' ? { ok: true, result: { cleared: true, kind: 'monaco' } } : { ok: true, result: { kind: 'cloudflare', blocking: true } })
  const bad = await cmd(w, session.id, { op: 'clear', label: '' })
  assert.equal(bad.status, 400)
  const c = await cmd(w, session.id, { op: 'clear', ref: '[812]', label: '' })
  assert.deepEqual(c.body.result, { cleared: true, kind: 'monaco' })
  const ch = await cmd(w, session.id, { op: 'challenge', label: '' })
  assert.deepEqual(ch.body.result, { kind: 'cloudflare', blocking: true })
  const sent = session.ws.commands()
  assert.strictEqual(sent[0].ref, 812)
  assert.equal(sent[1].ref, undefined)
})

test('challenge: a refused probe is not journaled; it runs after every click and navigate', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = () => ({ ok: false, error: 'tab not allowed' })
  const r = await cmd(w, session.id, { op: 'challenge', label: '' })
  assert.equal(r.body.ok, false)
  assert.ok(!/challenge: failed/.test(await journalText(w, id)))
})

test('click, type and select_option: forwarded with a numeric ref, text and pacing; bad input never reaches the browser', async () => {
  const { w, session } = await pairedSession()
  session.ws.answer = (c) => ({ ok: true, result: c.op })
  for (const c of [
    { op: 'click', ref: '[12]', label: 'Click button "Save"', pacing: 'off' },
    { op: 'type', ref: 13, text: 'Gold for Silver', clear: true, submit: false, label: 'Type into textbox "Name"' },
    { op: 'select_option', ref: 14, text: 'Forever', label: 'Choose "Forever" in combobox "Duration"', pacing: 'human' },
  ]) assert.equal((await cmd(w, session.id, c)).status, 200, c.op)
  const [click, type, sel] = session.ws.commands()
  assert.deepEqual(click, { id: click.id, op: 'click', label: 'Click button "Save"', ref: 12, pacing: 'off' })
  assert.deepEqual(type, { id: type.id, op: 'type', label: 'Type into textbox "Name"', ref: 13, text: 'Gold for Silver', clear: true })
  assert.deepEqual(sel, { id: sel.id, op: 'select_option', label: 'Choose "Forever" in combobox "Duration"', ref: 14, text: 'Forever', pacing: 'human' })
  for (const bad of [
    { op: 'click', label: '' },
    { op: 'type', ref: 1, label: '' },
    { op: 'type', ref: 1, text: 'x'.repeat(2001), label: '' },
    { op: 'type', ref: 1, text: 'x', clear: 'yes', label: '' },
    { op: 'select_option', ref: 1, text: '  ', label: '' },
    { op: 'select_option', text: 'Once', label: '' },
    { op: 'click', ref: 1, pacing: 'robot', label: '' },
  ]) assert.equal((await cmd(w, session.id, bad)).status, 400, JSON.stringify(bad))
  assert.equal(session.ws.commands().length, 3)
})

test('human step: the 300-char label cap is the error the driver sees', async () => {
  const { w, session } = await pairedSession()
  const r = await cmd(w, session.id, { op: 'human_step', label: 'x'.repeat(301) })
  assert.equal(r.status, 400)
  assert.match(r.body.error, /300 chars/)
})
