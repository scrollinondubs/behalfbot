import { test } from 'node:test'
import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { ENV, makeWorld, createInstall } from './helpers.mjs'
import { CODE_TTL_MS, SECRET_TTL_MS, PAIR_FAILS_PER_MIN, UNCONNECTED_TTL_MS } from '../src/autopilot.ts'

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

test('socket: needs a live token, one socket per session', async () => {
  const s = await setup()
  const p = await (await s.w.request('POST', '/autopilot/pair', { body: { code: s.code } })).json()
  assert.equal((await ws(s.w, 'not-a-token')).status, 401)
  const first = await ws(s.w, p.token)
  assert.ok(first.ws)
  assert.equal((await ws(s.w, p.token)).status, 401)
  const list = await (await s.w.request('GET', `/installs/${s.id}/autopilot`, admin)).json()
  assert.equal(list.sessions.length, 1)
  assert.equal(list.sessions[0].connected, true)
})

test('cmd: admin only; a method off the list never reaches the browser', async () => {
  const { w, session } = await pairedSession()
  const anon = await w.request('POST', `/autopilot/${session.id}/cmd`, { body: { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' } })
  assert.equal(anon.status, 401)
  for (const bad of [
    { op: 'cdp', method: 'Runtime.evaluate', params: { expression: 'document.cookie' }, label: 'x' },
    { op: 'cdp', method: 'Network.getAllCookies', label: 'x' },
    { op: 'raw', label: 'x' },
  ]) {
    const r = await cmd(w, session.id, bad)
    assert.equal(r.status, 400, JSON.stringify(bad))
  }
  assert.equal(session.ws.commands().length, 0)
})

test('cmd: forwards to the extension, returns its reply, journals the label', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = (c) => ({ ok: true, result: { echoed: c.method } })
  const r = await cmd(w, session.id, { op: 'cdp', method: 'DOM.getDocument', params: {}, label: 'Read the page' })
  assert.equal(r.status, 200)
  assert.deepEqual(r.body.result, { echoed: 'DOM.getDocument' })
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
  const r = await cmd(w, session.id, { op: 'cdp', method: 'DOM.getDocument', label: '' })
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

test('teardown: closing the socket deletes the session, its token and any held secret', async () => {
  const { w, id, session } = await pairedSession()
  session.ws.answer = (c) => (c.op === 'save_secret' ? { ok: true, secret: true, value: fakeGoogleKey() } : null)
  await cmd(w, session.id, { op: 'save_secret', ref: 3, secretTarget: 'k', label: '' })
  const pending = cmd(w, session.id, { op: 'human_step', label: 'Your turn', wait_ms: 5000 })
  await new Promise((r) => setTimeout(r, 5))
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
  assert.match(await journalText(w, id), /browser disconnected/)
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

async function pairedSession() {
  const s = await setup()
  const p = await (await s.w.request('POST', '/autopilot/pair', { body: { code: s.code } })).json()
  const conn = await ws(s.w, p.token)
  return { w: s.w, id: s.id, session: { id: p.session, token: p.token, ws: conn.ws } }
}
