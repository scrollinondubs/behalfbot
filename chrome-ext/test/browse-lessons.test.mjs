import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  ALLOWED_METHODS,
  challengeFacts,
  classifyChallenge,
  clearKeys,
  looksSecret,
  maskNodeIds,
  messageRefusal,
  quadToRect,
  refusal,
  secretShapeIds,
} from '../policy.js'

const ALLOW = ['console.aws.amazon.com']
const TAB = 'https://us-east-1.console.aws.amazon.com/iam/home'

test('new ops: screenshot and challenge need an allowed tab, clear needs an int ref', () => {
  const off = 'https://evil.example/'
  for (const op of ['screenshot', 'challenge']) {
    assert.equal(refusal({ op, label: '' }, TAB, ALLOW), null, op)
    assert.equal(refusal({ op, label: '' }, off, ALLOW), 'tab not allowed', op)
  }
  assert.equal(refusal({ op: 'clear', ref: 12, label: '' }, TAB, ALLOW), null)
  assert.equal(refusal({ op: 'clear', ref: '12', label: '' }, TAB, ALLOW), 'bad ref')
  assert.equal(refusal({ op: 'clear', ref: 12, label: '' }, off, ALLOW), 'tab not allowed')
})

test('screenshot and frame-tree methods stay out of the raw cdp list', () => {
  for (const m of ['Page.captureScreenshot', 'DOM.getDocument', 'Page.getLayoutMetrics', 'Runtime.callFunctionOn', 'DOM.resolveNode']) {
    assert.ok(!ALLOWED_METHODS.has(m), m)
    assert.equal(refusal({ op: 'cdp', method: m, params: {} }, TAB, ALLOW), 'method not allowed', m)
  }
})

test('clearKeys: Cmd on a Mac, Ctrl elsewhere; select-all is a keyDown, then Backspace', () => {
  const mac = clearKeys('mac')
  const win = clearKeys('win')
  const sel = (ks) => ks.find((k) => k.code === 'KeyA' && k.type !== 'keyUp')
  assert.equal(sel(mac).type, 'keyDown')
  assert.equal(sel(mac).modifiers, 4)
  assert.deepEqual(sel(mac).commands, ['selectAll'])
  assert.equal(sel(win).modifiers, 2)
  assert.equal(win[0].key, 'Control')
  assert.equal(clearKeys('linux')[0].key, 'Control')
  assert.equal(mac[0].key, 'Meta')
  const iBs = mac.findIndex((k) => k.key === 'Backspace')
  assert.ok(iBs > mac.indexOf(sel(mac)))
  assert.equal(mac.at(-1).type, 'keyUp')
  // Every key that goes down comes back up.
  for (const ks of [mac, win]) {
    const down = ks.filter((k) => k.type !== 'keyUp').map((k) => k.code).sort()
    const up = ks.filter((k) => k.type === 'keyUp').map((k) => k.code).sort()
    assert.deepEqual(down, up)
  }
})

const el = (backendNodeId, nodeName, attrs = [], extra = {}) => ({ backendNodeId, nodeName, attributes: attrs, children: [], ...extra })

test('maskNodeIds: text fields, textareas, off-site and out-of-process frames, inside shadow roots too; not a code editor\'s input proxy', () => {
  const sameFrameDoc = { backendNodeId: 90, nodeName: '#document', children: [el(91, 'INPUT', ['type', 'email'])] }
  const root = {
    backendNodeId: 1,
    nodeName: '#document',
    children: [
      el(2, 'INPUT'),
      el(3, 'INPUT', ['type', 'password']),
      el(4, 'INPUT', ['type', 'checkbox']),
      el(5, 'TEXTAREA'),
      el(6, 'BUTTON'),
      el(7, 'IFRAME', ['src', 'https://js.stripe.com/v3/elements']),
      el(8, 'IFRAME', ['src', '/iam/inner'], { contentDocument: sameFrameDoc }),
      el(9, 'IFRAME', ['src', 'https://other.console.aws.amazon.com/x']),
      el(10, 'DIV', [], { shadowRoots: [{ backendNodeId: 11, nodeName: '#document-fragment', children: [el(12, 'INPUT', ['type', 'TEXT'])] }] }),
      el(13, 'IFRAME', [], { contentDocument: { backendNodeId: 14, nodeName: '#document', children: [] } }),
      el(15, 'INPUT', ['type', 'hidden']),
      el(16, 'DIV', ['class', 'monaco-editor no-user-select'], { children: [el(17, 'TEXTAREA', ['class', 'inputarea']), el(18, 'INPUT')] }),
      el(19, 'DIV', ['class', 'CodeMirror cm-s-default'], { children: [el(20, 'DIV', [], { children: [el(21, 'TEXTAREA')] })] }),
      el(22, 'DIV', ['class', 'not-a-CodeMirror-x'], { children: [el(23, 'TEXTAREA')] }),
    ],
  }
  const ids = maskNodeIds(root, ALLOW, TAB)
  assert.deepEqual(ids.sort((a, b) => a - b), [2, 3, 5, 7, 9, 12, 18, 23, 91])
})

test('secret-shaped text is masked with its parent; ordinary text is not', () => {
  const key = ['AK', 'IA', 'ABCDEFGHIJKLMNOP'].join('')
  assert.ok(looksSecret(key))
  assert.ok(looksSecret(['wJalrXUtnFEMI', 'K7MDENG', 'bPxRfiCYEXAMPLEKEY'].join('/')))
  assert.ok(!looksSecret('Create access key for the backup writer'))
  assert.ok(!looksSecret('arn:aws:s3:::my-backup-bucket/nightly/*'))
  const ax = {
    nodes: [
      { nodeId: 'a', role: { value: 'cell' }, name: { value: key }, backendDOMNodeId: 20, childIds: ['b'] },
      { nodeId: 'b', parentId: 'a', role: { value: 'StaticText' }, name: { value: key }, backendDOMNodeId: 21 },
      { nodeId: 'c', role: { value: 'button' }, name: { value: 'Show secret access key' }, backendDOMNodeId: 22 },
    ],
  }
  assert.deepEqual(secretShapeIds(ax).sort(), [20, 20, 21].sort())
})

test('quadToRect: pads, scales, clips, drops off-screen boxes', () => {
  assert.deepEqual(quadToRect([10, 10, 110, 10, 110, 30, 10, 30], 1, 800, 600, 0), { x: 10, y: 10, w: 100, h: 20 })
  assert.deepEqual(quadToRect([10, 10, 110, 10, 110, 30, 10, 30], 0.5, 800, 600, 0), { x: 5, y: 5, w: 50, h: 10 })
  assert.deepEqual(quadToRect([-50, -50, 20, -50, 20, 20, -50, 20], 1, 800, 600, 3), { x: 0, y: 0, w: 23, h: 23 })
  assert.equal(quadToRect([900, 10, 950, 10, 950, 30, 900, 30], 1, 800, 600, 0), null)
  assert.equal(quadToRect([1, 2, 3], 1, 800, 600), null)
})

test('challenge: Cloudflare interstitial by title or element id', () => {
  assert.deepEqual(classifyChallenge({ title: 'Just a moment...', ids: [], frames: [] }), { kind: 'cloudflare', blocking: true })
  assert.deepEqual(classifyChallenge({ title: 'Sign in', ids: ['challenge-form'], frames: [] }), { kind: 'cloudflare', blocking: true })
  assert.equal(classifyChallenge({ title: 'IAM | Global', ids: [], frames: [] }), null)
})

test('challenge: visible checkbox or picture challenge blocks; invisible badges and passive widgets only report', () => {
  const anchor = 'https://www.google.com/recaptcha/api2/anchor?ar=1&k=x&co=y&hl=en&v=z&size=normal'
  const invisible = 'https://www.google.com/recaptcha/api2/anchor?ar=1&k=x&size=invisible'
  const bframe = 'https://www.google.com/recaptcha/api2/bframe?hl=en&v=z&k=x'
  const hcap = 'https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&id=0&host=x'
  const turnstile = 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/x/0x4AAA/auto/fbE/new/normal/auto/'
  const seen = (url, w = 304, h = 78) => ({ url, w, h, onscreen: true })
  assert.deepEqual(classifyChallenge({ title: 'Sign up', frames: [seen(anchor)] }), { kind: 'recaptcha', blocking: true })
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [seen(invisible, 256, 60)] }), { kind: 'recaptcha', blocking: false })
  // The picture challenge frame sits off screen until it is needed.
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [seen(invisible, 256, 60), { url: bframe, w: 400, h: 580, onscreen: false }] }), { kind: 'recaptcha', blocking: false })
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [seen(invisible, 256, 60), seen(bframe, 400, 580)] }), { kind: 'recaptcha', blocking: true })
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [seen(hcap, 303, 78)] }), { kind: 'hcaptcha', blocking: true })
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [seen(turnstile, 300, 65)] }), { kind: 'turnstile', blocking: false })
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [{ url: anchor, w: 0, h: 0, onscreen: false }] }), { kind: 'recaptcha', blocking: false })
  assert.deepEqual(classifyChallenge({ title: 'x', frames: [seen('https://geo.captcha-delivery.com/captcha/?initialCid=x', 800, 600)] }), { kind: 'datadome', blocking: true })
  assert.deepEqual(classifyChallenge({ title: 'x', ids: ['px-captcha'], frames: [] }), { kind: 'perimeterx', blocking: true })
  assert.deepEqual(classifyChallenge({ title: 'x', tags: ['awswaf-captcha'], frames: [] }), { kind: 'aws-waf', blocking: true })
  assert.equal(classifyChallenge({ title: 'x', frames: [seen('https://www.youtube.com/embed/abc', 560, 315), seen('https://www.google.com/maps/embed?pb=1', 600, 450)] }), null)
  assert.equal(classifyChallenge({ title: 'x', frames: [seen('https://evilgoogle.com/recaptcha/api2/anchor')] }), null)
})

test('challengeFacts: finds ids, the AWS WAF element and frames inside shadow roots', () => {
  const root = {
    nodeName: '#document',
    children: [
      el(2, 'DIV', ['id', 'challenge-stage']),
      el(3, 'AWSWAF-CAPTCHA'),
      el(4, 'DIV', [], { shadowRoots: [{ nodeName: '#document-fragment', children: [el(5, 'IFRAME', ['src', 'https://challenges.cloudflare.com/turnstile/if/x'])] }] }),
      el(6, 'IFRAME'),
    ],
  }
  const f = challengeFacts(root, 'https://example.com/')
  assert.deepEqual(f.ids, ['challenge-stage'])
  assert.deepEqual(f.tags, ['awswaf-captcha'])
  assert.deepEqual(f.frames, [{ url: 'https://challenges.cloudflare.com/turnstile/if/x', backendNodeId: 5 }])
})

test('Done: only from the side panel, only for the turn on screen', () => {
  const ext = 'abcdefghijklmnopabcdefghijklmnop'
  const panel = `chrome-extension://${ext}/sidepanel.html`
  const fromPanel = { id: ext, url: panel }
  assert.equal(messageRefusal({ type: 'done', turn: 'n1' }, fromPanel, ext, panel, 'n1'), null)
  assert.equal(messageRefusal({ type: 'done', turn: 'n0' }, fromPanel, ext, panel, 'n1'), 'no such turn')
  assert.equal(messageRefusal({ type: 'done' }, fromPanel, ext, panel, 'n1'), 'no such turn')
  assert.equal(messageRefusal({ type: 'done', turn: 'n1' }, fromPanel, ext, panel, null), 'no such turn')
  assert.equal(messageRefusal({ type: 'done', turn: 'n1' }, { id: ext, url: `chrome-extension://${ext}/popup.html` }, ext, panel, 'n1'), 'done only from the side panel')
  assert.equal(messageRefusal({ type: 'done', turn: 'n1' }, { id: ext, url: 'https://console.aws.amazon.com/', tab: { id: 3 } }, ext, panel, 'n1'), 'not an extension page')
  assert.equal(messageRefusal({ type: 'done', turn: 'n1' }, { id: 'otherextension', url: panel }, ext, panel, 'n1'), 'not this extension')
  assert.equal(messageRefusal({ type: 'pair', code: 'x' }, { id: ext, url: `chrome-extension://${ext}/popup.html` }, ext, panel, null), null)
})
