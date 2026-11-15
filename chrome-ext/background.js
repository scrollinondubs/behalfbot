// Service worker: one WebSocket out to the relay, one bound tab, and the
// policy check (policy.js) on every command before anything touches the tab.
// The session token lives only in this worker's memory and the socket URL.

import { RELAY_ORIGIN } from './config.js'
import { READ_FIELD_FN, compactAX, hostAllowed, normalizeCode, refusal, scrollExpression } from './policy.js'

const store = chrome.storage.session

let ws = null
let session = null // { allow, task, tabId, startedAt, steps }
let attachedTab = null
let keepalive = null
let turn = null // { id } while a "Your turn" step waits for Done
let queue = Promise.resolve()

// A restarted worker has no socket, so whatever the panel last showed is stale.
store.set({ status: 'off', turn: null, current: null })

async function log(text, kind) {
  const { log = [] } = await store.get('log')
  log.push({ at: Date.now(), text, kind })
  await store.set({ log: log.slice(-300) })
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj))
}

function reply(id, body) {
  send({ type: 'reply', id, ...body })
}

function event(text) {
  send({ type: 'event', text })
}

async function attach() {
  if (attachedTab === session.tabId) return
  await chrome.debugger.attach({ tabId: session.tabId }, '1.3')
  attachedTab = session.tabId
}

async function detach() {
  const t = attachedTab
  attachedTab = null
  if (t !== null) await chrome.debugger.detach({ tabId: t }).catch(() => {})
}

function cdp(method, params) {
  return chrome.debugger.sendCommand({ tabId: session.tabId }, method, params || {})
}

async function run(cmd) {
  if (!session) return reply(cmd.id, { ok: false, error: 'not connected' })
  const tab = await chrome.tabs.get(session.tabId).catch(() => null)
  if (!tab) return reply(cmd.id, { ok: false, error: 'tab closed' })
  const why = refusal(cmd, tab.url, session.allow)
  if (why) {
    // No attach, no read.
    await log(`Refused${cmd.label ? `: ${cmd.label}` : ''} (${why})`, 'err')
    return reply(cmd.id, { ok: false, error: why })
  }
  if (turn && cmd.op !== 'note') return reply(cmd.id, { ok: false, error: 'waiting for the person to press Done' })
  if (cmd.op === 'note') {
    await log(cmd.label, 'ok')
    return reply(cmd.id, { ok: true, result: 'noted' })
  }
  if (cmd.op === 'human_step') {
    // Hand the tab back: detaching removes the debugging bar until Done.
    await detach()
    turn = { id: cmd.id }
    await store.set({ turn: { text: cmd.label }, current: null })
    await log(`Your turn: ${cmd.label}`, 'handoff')
    chrome.windows.update(tab.windowId, { focused: true }).catch(() => {})
    return
  }
  if (cmd.label) await store.set({ current: cmd.label })
  try {
    await attach()
    let result
    if (cmd.op === 'cdp') {
      result = await cdp(cmd.method, cmd.params)
      if (cmd.method === 'Accessibility.getFullAXTree') result = compactAX(result)
    } else if (cmd.op === 'scroll') {
      await cdp('Runtime.evaluate', { expression: scrollExpression(cmd.dy) })
      result = 'scrolled'
    } else if (cmd.op === 'save_secret') {
      const { object } = await cdp('DOM.resolveNode', { backendNodeId: cmd.ref })
      const read = await cdp('Runtime.callFunctionOn', {
        objectId: object.objectId,
        functionDeclaration: READ_FIELD_FN,
        returnByValue: true,
      })
      await cdp('Runtime.releaseObject', { objectId: object.objectId }).catch(() => {})
      const value = read && read.result && typeof read.result.value === 'string' ? read.result.value : ''
      await store.set({ current: null })
      if (!value) {
        await log(`${cmd.label || 'Save to vault'}: field was empty`, 'err')
        return reply(cmd.id, { ok: false, error: 'field empty' })
      }
      // The value goes to the relay's one-shot store, never to the panel.
      session.steps++
      await log(cmd.label || `Sent a value for "${cmd.secretTarget}" to your vault`, 'ok')
      return reply(cmd.id, { ok: true, secret: true, value })
    }
    if (cmd.label) {
      session.steps++
      await log(cmd.label, 'ok')
    }
    await store.set({ current: null })
    reply(cmd.id, { ok: true, result })
  } catch (e) {
    await store.set({ current: null })
    if (cmd.label) await log(`${cmd.label} (failed)`, 'err')
    reply(cmd.id, { ok: false, error: String((e && e.message) || e).slice(0, 300) })
  }
}

async function pair(rawCode) {
  const code = normalizeCode(rawCode)
  if (!code) throw new Error('That code does not look right. It has 6 letters and numbers, like K7Q-4M2.')
  if (ws) await disconnect('Replaced by a new pairing')
  const res = await fetch(`${RELAY_ORIGIN}/autopilot/pair`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code }),
  })
  const j = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(j.error || `pairing failed (${res.status})`)

  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
  let tab = active && active.url && hostAllowed(active.url, j.allow) ? active : null
  if (!tab) {
    if (!hostAllowed(j.start_url, j.allow)) throw new Error('start page is not on the allowed sites')
    tab = await chrome.tabs.create({ url: j.start_url, active: true })
  }
  session = { allow: j.allow, task: j.task, tabId: tab.id, startedAt: Date.now(), steps: 0 }
  await store.set({ status: 'connecting', allow: j.allow, task: j.task, log: [], turn: null, current: null })

  const url = RELAY_ORIGIN.replace(/^http/, 'ws') + '/autopilot/ws?token=' + encodeURIComponent(j.token)
  const sock = new WebSocket(url)
  ws = sock
  sock.onopen = async () => {
    send({ type: 'hello', tab_url: tab.url || tab.pendingUrl || j.start_url })
    // An MV3 worker sleeps after ~30 s idle; socket traffic keeps it awake.
    // The relay answers "ping" without waking its Durable Object.
    keepalive = setInterval(() => sock.readyState === WebSocket.OPEN && sock.send('ping'), 20_000)
    await store.set({ status: 'connected' })
    await log(`Connected. Allowed: ${j.allow.join(', ')}`, 'ok')
  }
  sock.onmessage = (m) => {
    if (m.data === 'pong') return
    let cmd
    try {
      cmd = JSON.parse(m.data)
    } catch {
      return
    }
    queue = queue.then(() => run(cmd)).catch(() => {})
  }
  sock.onclose = () => {
    if (ws === sock) teardown('Relay closed the session')
  }
  return { ok: true }
}

async function teardown(why) {
  const s = session
  ws = null
  session = null
  turn = null
  clearInterval(keepalive)
  keepalive = null
  await detach()
  const patch = { status: 'off', turn: null, current: null, allow: [], task: '' }
  if (s) patch.lastSession = { steps: s.steps, start: s.startedAt, end: Date.now(), why }
  await store.set(patch)
  await log(`Off: ${why}`, 'off')
}

async function disconnect(why) {
  const sock = ws
  if (sock) {
    event(why)
    ws = null
    try {
      sock.close(1000, why.slice(0, 100))
    } catch {
      // already closed
    }
  }
  await teardown(why)
}

async function done() {
  if (!turn) return { ok: false }
  const id = turn.id
  turn = null
  await store.set({ turn: null })
  await log('Done pressed', 'ok')
  event('The person pressed Done on "Your turn"')
  reply(id, { ok: true, result: 'done' })
  return { ok: true }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Only this extension's own pages talk to the worker.
  if (sender.id !== chrome.runtime.id) return
  const p =
    msg.type === 'pair' ? pair(msg.code)
    : msg.type === 'disconnect' ? disconnect('Disconnected by you').then(() => ({ ok: true }))
    : msg.type === 'done' ? done()
    : Promise.resolve({ ok: false, error: 'unknown' })
  p.then(sendResponse, (e) => sendResponse({ ok: false, error: String((e && e.message) || e) }))
  return true
})

chrome.debugger.onDetach.addListener((src, reason) => {
  if (!session || src.tabId !== session.tabId) return
  attachedTab = null
  // Cancel on Chrome's debugging bar ends the session at once.
  if (reason === 'canceled_by_user') disconnect('Cancelled from the Chrome debugging bar')
})

chrome.tabs.onRemoved.addListener((tabId) => {
  if (session && tabId === session.tabId) disconnect('The autopilot tab was closed')
})

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (!session || tabId !== session.tabId || !info.url) return
  if (hostAllowed(info.url, session.allow)) {
    send({ type: 'tab', tab_url: info.url })
    return
  }
  // Off the allowed sites: let go of the tab. Commands are refused until it
  // comes back (policy.refusal checks the URL on every one).
  detach()
  send({ type: 'tab', tab_url: 'outside the allowed sites' })
  log('Tab left the allowed sites, paused', 'err')
})
