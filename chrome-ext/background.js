// Service worker: one WebSocket out to the relay, one bound tab, and the
// policy check (policy.js) on every command before anything touches the tab.
// The session token lives only in this worker's memory and the socket URL.

import { RELAY_ORIGIN } from './config.js'
import {
  FIELD_STATE_FN,
  MAX_SCREENSHOT_B64,
  READ_FIELD_FN,
  challengeFacts,
  classifyChallenge,
  clearKeys,
  compactAX,
  hostAllowed,
  maskNodeIds,
  messageRefusal,
  normalizeCode,
  quadToRect,
  refusal,
  scrollExpression,
  secretShapeIds,
} from './policy.js'

const store = chrome.storage.session

let ws = null
let session = null // { allow, task, tabId, startedAt, steps, secretRefs }
let attachedTab = null
let keepalive = null
let turn = null // { id, nonce } while a "Your turn" step waits for Done
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

async function callOn(ref, fn) {
  const { object } = await cdp('DOM.resolveNode', { backendNodeId: ref })
  try {
    const r = await cdp('Runtime.callFunctionOn', { objectId: object.objectId, functionDeclaration: fn, returnByValue: true })
    return r && r.result ? r.result.value : undefined
  } finally {
    await cdp('Runtime.releaseObject', { objectId: object.objectId }).catch(() => {})
  }
}

async function box(ref) {
  try {
    const { model } = await cdp('DOM.getBoxModel', { backendNodeId: ref })
    return model.border
  } catch {
    return null // not rendered
  }
}

async function clickRef(ref) {
  await cdp('DOM.scrollIntoViewIfNeeded', { backendNodeId: ref }).catch(() => {})
  const q = await box(ref)
  if (!q) return false
  const x = (q[0] + q[2] + q[4] + q[6]) / 4
  const y = (q[1] + q[3] + q[5] + q[7]) / 4
  await cdp('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y })
  await cdp('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
  await cdp('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
  return true
}

// Empties a field so the next insertText replaces rather than appends. Plain
// inputs, contenteditable, Monaco and CodeMirror all take the same real key
// events; the field state check says whether it worked, never what was there.
async function clearField(ref) {
  const state = async () => (await callOn(ref, FIELD_STATE_FN)) || { kind: 'other', empty: null, focused: false }
  await cdp('DOM.focus', { backendNodeId: ref }).catch(() => {})
  let s = await state()
  if (!s.focused) {
    // Code editors often refuse DOM.focus on their wrapper; a click lands.
    await clickRef(ref)
    s = await state()
  }
  if (!s.focused) return { cleared: false, kind: s.kind, error: 'could not put the cursor in that field' }
  if (s.empty === true) return { cleared: true, kind: s.kind }
  const { os } = await chrome.runtime.getPlatformInfo()
  for (let i = 0; i < 2 && (i === 0 || s.empty === false); i++) {
    for (const k of clearKeys(os)) await cdp('Input.dispatchKeyEvent', k)
    // Code editors repaint on the next animation frame.
    for (let t = 0; t < 6; t++) {
      s = await state()
      if (s.empty !== false) break
      await new Promise((r) => setTimeout(r, 80))
    }
  }
  return { cleared: s.empty, kind: s.kind, os }
}

function b64(bytes) {
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(bin)
}

// Screenshot of the bound tab with every field value, off-site frame,
// save_secret field and credential-shaped text painted black in this worker, so nothing about
// the page can undo the masking. Fails closed: if masking cannot be worked
// out, or the page changes underneath, no image leaves.
async function screenshot(tabUrl) {
  const metrics = await cdp('Page.getLayoutMetrics')
  const vp = metrics.cssVisualViewport || metrics.visualViewport
  const vw = Math.max(1, Math.round(vp.clientWidth))
  const vh = Math.max(1, Math.round(vp.clientHeight))
  const { root } = await cdp('DOM.getDocument', { depth: -1, pierce: true })
  const ids = new Set(maskNodeIds(root, session.allow, tabUrl))
  for (const id of secretShapeIds(await cdp('Accessibility.getFullAXTree'))) ids.add(id)
  for (const id of session.secretRefs) ids.add(id)
  if (ids.size > 3000) throw new Error('too many fields to hide on this page, no screenshot taken')
  const quads = []
  for (const id of ids) {
    const q = await box(id)
    if (q) quads.push(q)
  }
  const shot = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  const now = await chrome.tabs.get(session.tabId).catch(() => null)
  if (!now || now.url !== tabUrl || !hostAllowed(now.url, session.allow)) throw new Error('the page changed during the screenshot, nothing sent')

  const raw = Uint8Array.from(atob(shot.data), (c) => c.charCodeAt(0))
  const bmp = await createImageBitmap(new Blob([raw], { type: 'image/png' }))
  let scale = Math.min(1, 1600 / vw)
  for (let attempt = 0; attempt < 6; attempt++) {
    const w = Math.max(1, Math.round(vw * scale))
    const h = Math.max(1, Math.round(vh * scale))
    const canvas = new OffscreenCanvas(w, h)
    const ctx = canvas.getContext('2d')
    ctx.drawImage(bmp, 0, 0, w, h)
    ctx.fillStyle = '#000'
    for (const q of quads) {
      const r = quadToRect(q, scale, w, h)
      if (r) ctx.fillRect(r.x, r.y, r.w, r.h)
    }
    const quality = [0.7, 0.5, 0.35][Math.min(attempt, 2)]
    const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality })
    const data = b64(new Uint8Array(await blob.arrayBuffer()))
    if (data.length <= MAX_SCREENSHOT_B64) {
      bmp.close()
      return { mime: 'image/jpeg', width: w, height: h, masked: quads.length, data }
    }
    if (attempt >= 2) scale *= 0.7
  }
  bmp.close()
  throw new Error('screenshot too large to send')
}

// Looks for a bot check on the bound tab, from the pierced DOM tree, frame
// boxes and the tab title. The answer is a fixed kind; nothing the page
// wrote travels back.
async function challenge(tab) {
  const metrics = await cdp('Page.getLayoutMetrics')
  const vp = metrics.cssVisualViewport || metrics.visualViewport
  const { root } = await cdp('DOM.getDocument', { depth: -1, pierce: true })
  const facts = challengeFacts(root, tab.url)
  const frames = []
  for (const f of facts.frames) {
    const q = await box(f.backendNodeId)
    if (!q) {
      frames.push({ url: f.url, w: 0, h: 0, onscreen: false })
      continue
    }
    const xs = [q[0], q[2], q[4], q[6]]
    const ys = [q[1], q[3], q[5], q[7]]
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys)
    const onscreen = x1 > 0 && y1 > 0 && x0 < vp.clientWidth && y0 < vp.clientHeight
    frames.push({ url: f.url, w: x1 - x0, h: y1 - y0, onscreen })
  }
  return classifyChallenge({ title: tab.title || '', ids: facts.ids, tags: facts.tags, frames })
}

async function run(cmd) {
  if (!session) return reply(cmd.id, { ok: false, error: 'not connected' })
  const tab = await chrome.tabs.get(session.tabId).catch(() => null)
  if (!tab) return reply(cmd.id, { ok: false, error: 'tab closed' })
  const why = refusal(cmd, tab.url, session.allow)
  if (why) {
    // No attach, no read. The bot-check probe after every click and navigate
    // is the driver's housekeeping; its refusals stay out of the panel.
    if (cmd.op === 'challenge') return reply(cmd.id, { ok: false, error: why })
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
    turn = { id: cmd.id, nonce: crypto.randomUUID() }
    await store.set({ turn: { text: cmd.label, nonce: turn.nonce }, current: null })
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
      session.secretRefs.add(cmd.ref)
      return reply(cmd.id, { ok: true, secret: true, value })
    } else if (cmd.op === 'clear') {
      result = await clearField(cmd.ref)
    } else if (cmd.op === 'screenshot') {
      result = await screenshot(tab.url)
      session.steps++
      await log('Took a screenshot of this tab (typed values blacked out)', 'ok')
      await store.set({ current: null })
      return reply(cmd.id, { ok: true, result })
    } else if (cmd.op === 'challenge') {
      result = await challenge(tab)
      if (result && result.blocking) await log('This page is showing a bot check', 'handoff')
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
  session = { allow: j.allow, task: j.task, tabId: tab.id, startedAt: Date.now(), steps: 0, secretRefs: new Set() }
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

const PANEL_URL = chrome.runtime.getURL('sidepanel.html')

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Only this extension's own pages talk to the worker, and Done only from
  // the side panel for the turn it shows (policy.messageRefusal).
  const why = messageRefusal(msg, sender, chrome.runtime.id, PANEL_URL, turn && turn.nonce)
  if (why) {
    sendResponse({ ok: false, error: why })
    return
  }
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
