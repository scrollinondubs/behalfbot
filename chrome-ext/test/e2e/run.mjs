// End-to-end check of the extension in real Chrome: local fixture pages, a
// stub relay, no customer site and no deployed relay. Not part of npm test
// (it needs Chrome, and the fixtures load Monaco and CodeMirror from CDNs).
//
//   (cd chrome-ext && RELAY_ORIGIN=http://127.0.0.1:8797 ./build.sh)
//   (cd cloudflare/installer && npm ci)     # for the ws package
//   node chrome-ext/test/e2e/run.mjs
//
// CHROME overrides the browser path. Chrome 137+ ignores --load-extension, so
// this loads the build over --remote-debugging-pipe with
// Extensions.loadUnpacked. On macOS, Chrome for Testing may be refused
// loopback connections; branded Chrome works.
import { createServer } from 'node:http'
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { spawn } from 'node:child_process'
import { tmpdir } from 'node:os'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const EXT = join(HERE, '../../dist/behalfbot-autopilot')
const { WebSocketServer } = createRequire(join(HERE, '../../../cloudflare/installer/package.json'))('ws')
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' - ' + detail : ''}`) }

// fixtures
createServer((req, res) => {
  const f = req.url.split('?')[0].replace(/^\//, '') || 'index.html'
  let body; try { body = readFileSync(join(HERE, 'fixtures', f)) } catch { res.writeHead(404); return res.end() } res.writeHead(200, { 'content-type': 'text/html' }); res.end(body)
}).listen(8100)

// stub relay
let sock = null
const waiters = new Map()
let n = 0
const relay = createServer((req, res) => {
  if (req.url === '/autopilot/pair') {
    res.writeHead(200, { 'content-type': 'application/json' })
    return res.end(JSON.stringify({ token: 't', session: 'aps_x', allow: ['127.0.0.1'], task: 'e2e', start_url: 'http://127.0.0.1:8100/fields.html' }))
  }
  res.writeHead(404); res.end()
})
const wss = new WebSocketServer({ noServer: true })
relay.on('upgrade', (req, s, head) => wss.handleUpgrade(req, s, head, (ws) => {
  sock = ws
  ws.on('message', (d) => {
    const t = d.toString()
    if (t === 'ping') return ws.send('pong')
    const m = JSON.parse(t)
    if (m.type === 'reply' && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id) }
  })
}))
relay.listen(8797)
function cmd(c, timeout = 20000) {
  const id = `c${++n}`
  sock.send(JSON.stringify({ id, label: '', ...c }))
  return new Promise((resolve) => {
    waiters.set(id, resolve)
    setTimeout(() => { if (waiters.has(id)) { waiters.delete(id); resolve({ timeout: true, id }) } }, timeout)
  })
}

// chromium: branded Chrome over a pipe, extension loaded with
// Extensions.loadUnpacked (Chrome 137+ ignores --load-extension).
const prof = mkdtempSync(join(tmpdir(), 'ap-e2e-'))
const chrome = spawn(CHROME, [
  ...(process.env.DPR ? [`--force-device-scale-factor=${process.env.DPR}`] : []),
  '--headless=new', `--user-data-dir=${prof}`, '--remote-debugging-pipe', '--enable-unsafe-extension-debugging', '--no-first-run', '--no-default-browser-check',
  '--window-size=1280,1000', '--host-resolver-rules=MAP www.google.com 127.0.0.1:9, MAP challenges.cloudflare.com 127.0.0.1:9',
  'about:blank',
], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] })
const out = chrome.stdio[3]
const inp = chrome.stdio[4]
let mid = 0
const pend = new Map()
const listeners = []
let buf = ''
inp.on('data', (d) => {
  buf += d.toString()
  let i
  while ((i = buf.indexOf('\0')) >= 0) {
    const m = JSON.parse(buf.slice(0, i))
    buf = buf.slice(i + 1)
    if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id) }
    for (const l of listeners) l(m)
  }
})
const B = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const id = ++mid
  pend.set(id, (m) => (m.error ? reject(new Error(method + ': ' + m.error.message)) : resolve(m.result)))
  out.write(JSON.stringify({ id, method, params, sessionId }) + '\0')
})
const { id: extId } = await B('Extensions.loadUnpacked', { path: EXT })
console.log('chrome up, extension', extId)
async function targets() { return (await B('Target.getTargets')).targetInfos }
let ext
for (let i = 0; i < 50 && !ext; i++) { ext = (await targets()).find((t) => t.type === 'service_worker' && t.url === `chrome-extension://${extId}/background.js`); if (!ext) await sleep(200) }
if (!ext) { console.log('extension worker not found for', extId); process.exit(1) }
async function open(url) {
  const { targetId } = await B('Target.createTarget', { url })
  const { sessionId } = await B('Target.attachToTarget', { targetId, flatten: true })
  await sleep(800)
  return { targetId, s: sessionId }
}
const ev = async (s, expression) => (await B('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, s)).result.value

// pair from the popup page
const popup = await open(`chrome-extension://${extId}/popup.html`)
const pr = await ev(popup.s, `chrome.runtime.sendMessage({ type: 'pair', code: 'ABC-DEF' })`)
check('pair', pr && pr.ok, JSON.stringify(pr)); if (!pr || !pr.ok) process.exit(1)
for (let i = 0; i < 50 && !sock; i++) await sleep(100)
const page = (await targets()).find((t) => t.type === 'page' && t.url.includes('fields.html'))
const { sessionId: P } = await B('Target.attachToTarget', { targetId: page.targetId, flatten: true })
await B('Runtime.enable', {}, P)
for (let i = 0; i < 100; i++) { if (await ev(P, `!!(window.mon && window.cm6 && window.cm6.state && window.cm5)`)) break; await sleep(200) }
check('editors loaded', await ev(P, `!!(window.mon && window.cm6 && window.cm6.state && window.cm5)`))
await B('DOM.enable', {}, P)
async function ref(sel) {
  const { result } = await B('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(sel)})` }, P)
  return (await B('DOM.describeNode', { objectId: result.objectId }, P)).node.backendNodeId
}

// 1. clear + type
const cases = [
  ['plain input', '#plain', `document.getElementById('plain').value`],
  ['textarea', '#ta', `document.getElementById('ta').value`],
  ['contenteditable', '#ce', `document.getElementById('ce').textContent`],
  ['monaco', '#monaco textarea', `mon.getValue()`],
  ['codemirror 6', '#cm6host .cm-content', `cm6.state.doc.toString()`],
  ['codemirror 5', '.CodeMirror textarea', `cm5.getValue()`],
]
for (const [name, sel, read] of cases) {
  const r = await cmd({ op: 'clear', ref: await ref(sel) })
  const ins = await cmd({ op: 'cdp', method: 'Input.insertText', params: { text: '{"new": 1}' } })
  const v = await ev(P, read)
  check(`clear+type ${name}`, r.ok && r.result.cleared === true && ins.ok && v === '{"new": 1}', `${JSON.stringify(r.result || r.error)} -> ${JSON.stringify(v)}`)
}
// clearing an already empty field is a no-op, and old rawKeyDown behaviour for comparison
await ev(P, `mon.setValue('{"Statement": []}')`)
await cmd({ op: 'cdp', method: 'DOM.focus', params: { backendNodeId: await ref('#monaco textarea') } })
await cmd({ op: 'cdp', method: 'Input.dispatchKeyEvent', params: { type: 'rawKeyDown', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 4, commands: ['selectAll'] } })
await cmd({ op: 'cdp', method: 'Input.dispatchKeyEvent', params: { type: 'keyUp', key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65 } })
await cmd({ op: 'cdp', method: 'Input.insertText', params: { text: 'X' } })
console.log('info: old rawKeyDown select-all on monaco left', JSON.stringify(await ev(P, 'mon.getValue()')))

// 2. screenshot: every field the page shows must be black in the image, at
// whatever zoom, scroll or device scale the tab is in.
const MASKED = ['#plain', '#pw', '#ta', '#akid', 'iframe#inner']
async function maskCheck(label) {
  const shot = await cmd({ op: 'screenshot' }, 30000)
  if (!shot.ok) return check(`screenshot ${label}`, false, shot.error || 'timeout')
  writeFileSync(join(prof, `shot-${label.replace(/\W+/g, '-')}.jpg`), Buffer.from(shot.result.data, 'base64'))
  const pts = await ev(P, `(() => {
    const out = []
    const scale = ${shot.result.width} / window.innerWidth
    for (const sel of ${JSON.stringify(MASKED)}) {
      let r = document.querySelector(sel).getBoundingClientRect()
      if (sel === 'iframe#inner') {
        const f = r
        const i = document.querySelector(sel).contentDocument.getElementById('x').getBoundingClientRect()
        r = { left: f.left + i.left, top: f.top + i.top, width: i.width, height: i.height }
      }
      const x = (r.left + r.width / 2) * scale, y = (r.top + r.height / 2) * scale
      if (y > 2 && y < ${shot.result.height} - 2) out.push({ sel, x, y })
    }
    out.push({ sel: 'background', x: ${shot.result.width} - 4, y: ${shot.result.height} - 4 })
    return out
  })()`)
  const lum = await ev(popup.s, `(async () => {
    const img = new Image()
    img.src = 'data:image/jpeg;base64,${shot.result.data}'
    await img.decode()
    const c = new OffscreenCanvas(img.width, img.height)
    const x = c.getContext('2d')
    x.drawImage(img, 0, 0)
    return ${JSON.stringify(pts)}.map((p) => { const d = x.getImageData(Math.round(p.x), Math.round(p.y), 1, 1).data; return d[0] + d[1] + d[2] })
  })()`)
  const bad = pts.filter((p, i) => (p.sel === 'background' ? lum[i] < 600 : lum[i] > 90)).map((p) => p.sel)
  const fields = pts.filter((p) => p.sel !== 'background').length
  check(`screenshot ${label}: ${fields} visible fields black`, bad.length === 0 && fields >= 3, `${shot.result.width}x${shot.result.height}, masked ${shot.result.masked}${bad.length ? ', NOT masked: ' + bad.join(' ') : ''}`)
  return shot
}
await ev(P, `document.getElementById('plain').value = 'visible-plain-SECRETVALUE'`)
const shot = await maskCheck('default')
const keys = Object.keys(shot.result || {}).sort().join(',')
check('screenshot result has only expected keys', keys === 'data,height,masked,mime,width', keys)
await ev(P, `window.scrollTo(0, 70)`)
await maskCheck('scrolled 70px')
await ev(P, `window.scrollTo(0, 0)`)
const { sessionId: SW } = await B('Target.attachToTarget', { targetId: ext.targetId, flatten: true })
const zoom = (z) => ev(SW, `chrome.tabs.query({}).then((ts) => chrome.tabs.setZoom(ts.find((t) => t.url.includes('fields.html')).id, ${z}))`)
await zoom(1.5)
await sleep(500)
await maskCheck('zoom 150%')
await zoom(0.8)
await sleep(500)
await maskCheck('zoom 80%')
await zoom(1)
await sleep(300)
const other = await open('http://127.0.0.1:8100/index.html')
await B('Target.activateTarget', { targetId: other.targetId })
await sleep(300)
await maskCheck('bound tab in the background')
await B('Target.activateTarget', { targetId: page.targetId })
console.log(`info: screenshots saved in ${prof}`)

// 3. challenge detection
for (const [f, want] of [['cf.html', 'cloudflare/true'], ['recaptcha.html', 'recaptcha/true'], ['invisible.html', 'recaptcha/false'], ['shadow-turnstile.html', 'turnstile/false'], ['index.html', 'null']]) {
  const nav = await cmd({ op: 'cdp', method: 'Page.navigate', params: { url: `http://127.0.0.1:8100/${f}` } })
  await sleep(1200)
  const c = await cmd({ op: 'challenge' })
  const got = c.ok ? (c.result ? `${c.result.kind}/${c.result.blocking}` : 'null') : 'error ' + c.error
  check(`challenge ${f}`, nav.ok && got === want, got)
}
// off-allowlist navigation refused, screenshot refused off the allowlist
const offnav = await cmd({ op: 'cdp', method: 'Page.navigate', params: { url: 'https://example.com/' } })
check('navigate off allowlist refused', !offnav.ok && /not allowed/.test(offnav.error), offnav.error)
const rawShot = await cmd({ op: 'cdp', method: 'Page.captureScreenshot', params: {} })
check('raw Page.captureScreenshot refused', !rawShot.ok && rawShot.error === 'method not allowed', rawShot.error)

// 4. Done only from a trusted click in the side panel
const hs = cmd({ op: 'human_step', label: 'Do the thing, then press Done' }, 60000)
await sleep(500)
const panel = await open(`chrome-extension://${extId}/sidepanel.html`)
await ev(panel.s, `document.getElementById('done').click()`)
await ev(panel.s, `document.getElementById('done').dispatchEvent(new MouseEvent('click', { bubbles: true }))`)
const nonce = await ev(popup.s, `chrome.storage.session.get('turn').then((s) => s.turn && s.turn.nonce)`)
const fromPopup = await ev(popup.s, `chrome.runtime.sendMessage({ type: 'done', turn: ${JSON.stringify(nonce)} })`)
const staleFromPanel = await ev(panel.s, `chrome.runtime.sendMessage({ type: 'done', turn: 'stale' })`)
const noRuntimeOnPage = await ev(P, `typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.sendMessage`)
const early = await Promise.race([hs, sleep(800).then(() => null)])
check('synthetic click, popup message, stale turn and web page cannot press Done', early === null && fromPopup.ok === false && staleFromPanel.ok === false && noRuntimeOnPage, `${JSON.stringify(fromPopup)} ${JSON.stringify(staleFromPanel)} pageNoRuntime=${noRuntimeOnPage}`)
const cmdDuringTurn = await cmd({ op: 'cdp', method: 'Accessibility.getFullAXTree' })
check('commands refused during the turn', !cmdDuringTurn.ok && /waiting/.test(cmdDuringTurn.error), cmdDuringTurn.error)
const btn = await ev(panel.s, `(() => { const r = document.getElementById('done').getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 } })()`)
for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await B('Input.dispatchMouseEvent', { type, x: btn.x, y: btn.y, button: 'left', clickCount: 1 }, panel.s)
const done = await Promise.race([hs, sleep(3000).then(() => null)])
check('trusted click presses Done', done && done.ok && done.result === 'done', JSON.stringify(done))

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} passed`)
chrome.kill()
process.exit(failed.length ? 1 : 0)
