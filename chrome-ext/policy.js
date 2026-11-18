// Pure policy for the extension: which tabs and which CDP calls a relay
// command may touch. No chrome.* here, so the node tests import exactly the
// code the service worker runs.

// CDP methods a relay command may name with op "cdp". Runtime.evaluate is not
// here on purpose: the extension builds its only two evaluations itself
// (scroll, and the save_secret field read) from fixed templates.
export const ALLOWED_METHODS = new Set([
  'Accessibility.getFullAXTree',
  'DOM.getBoxModel',
  'DOM.scrollIntoViewIfNeeded',
  'DOM.focus',
  'Input.dispatchMouseEvent',
  'Input.insertText',
  'Input.dispatchKeyEvent',
  'Page.navigate',
])

export const OPS = new Set(['cdp', 'scroll', 'save_secret', 'human_step', 'note', 'screenshot', 'clear', 'challenge', 'click', 'type', 'select_option'])
export const MAX_TYPE_CHARS = 2000

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1'])

// An allowlist entry matches its exact host or a subdomain of it on a dot
// boundary. https only, except a local host the allowlist names outright
// (the dev runbook), which may be plain http.
export function hostAllowed(rawUrl, allow) {
  let u
  try {
    u = new URL(rawUrl)
  } catch {
    return false
  }
  const host = u.hostname.toLowerCase()
  if (!Array.isArray(allow) || allow.length === 0) return false
  for (const entry of allow) {
    const a = String(entry).toLowerCase()
    if (!a) continue
    const match = host === a || host.endsWith('.' + a)
    if (!match) continue
    if (u.protocol === 'https:') return true
    if (u.protocol === 'http:' && LOCAL_HOSTS.has(a) && host === a) return true
  }
  return false
}

// Returns null when the command may run against a tab at tabUrl, otherwise
// the refusal text sent back to the relay.
export function refusal(cmd, tabUrl, allow) {
  if (!cmd || typeof cmd !== 'object') return 'bad command'
  if (!OPS.has(cmd.op)) return 'op not allowed'
  if (cmd.op === 'note') return null
  if (cmd.op === 'human_step') return null
  if (!tabUrl || !hostAllowed(tabUrl, allow)) return 'tab not allowed'
  if (cmd.op === 'cdp') {
    if (!ALLOWED_METHODS.has(cmd.method)) return 'method not allowed'
    if (cmd.method === 'Page.navigate') {
      const target = cmd.params && cmd.params.url
      if (typeof target !== 'string' || !hostAllowed(target, allow)) return 'navigate target not allowed'
    }
    // CDP rejects a string backendNodeId; the relay coerces, this refuses
    // anything that still is not an int32 ref.
    const ref = cmd.params && cmd.params.backendNodeId
    if (ref !== undefined && !isRef(ref)) return 'bad ref'
  }
  if (cmd.op === 'scroll') {
    if (!Number.isInteger(cmd.dy) || Math.abs(cmd.dy) > 20000) return 'bad scroll'
  }
  if (cmd.op === 'clear' && !isRef(cmd.ref)) return 'bad ref'
  if ((cmd.op === 'click' || cmd.op === 'type' || cmd.op === 'select_option') && !isRef(cmd.ref)) return 'bad ref'
  if (cmd.op === 'type' && (typeof cmd.text !== 'string' || cmd.text.length > MAX_TYPE_CHARS)) return 'bad text'
  if (cmd.op === 'select_option' && (typeof cmd.text !== 'string' || !cmd.text.trim() || cmd.text.length > 300)) return 'bad option'
  if (cmd.pacing !== undefined && cmd.pacing !== 'off' && cmd.pacing !== 'human') return 'bad pacing'
  if (cmd.op === 'save_secret') {
    if (!isRef(cmd.ref)) return 'bad ref'
    if (typeof cmd.secretTarget !== 'string' || !cmd.secretTarget) return 'missing vault item'
  }
  return null
}

function isRef(v) {
  return Number.isInteger(v) && v > 0 && v <= 0x7fffffff
}

export function scrollExpression(dy) {
  if (!Number.isInteger(dy)) throw new Error('bad scroll')
  return `window.scrollBy(0, ${dy})`
}

// Runs in the page via Runtime.callFunctionOn on the resolved node. Fixed
// text: the relay never supplies code.
export const READ_FIELD_FN =
  'function () { var v = (this.value !== undefined && this.value !== null && this.value !== "") ? this.value : this.textContent; return String(v || "").trim() }'

// Sets a native <select> by option value or visible label, the way a person
// picking from the list would: the value goes through the prototype setter
// (so React and other frameworks see the change), then input and change
// bubble. A ref to one of its <option>s works too. The wanted text arrives as
// a CDP call argument, never spliced into this source.
export const SELECT_OPTION_FN = `function (want) {
  var sel = this instanceof HTMLOptionElement ? this.closest('select') : this
  if (!(sel instanceof HTMLSelectElement)) {
    return { ok: false, error: 'not a native select (it is a ' + String(this.tagName || 'node').toLowerCase() + '); click it, then click the option' }
  }
  var norm = function (s) { return String(s).replace(/\\s+/g, ' ').trim().toLowerCase() }
  var opts = Array.prototype.slice.call(sel.options)
  var w = norm(want)
  var o = opts.find(function (x) { return x.value === want })
    || opts.find(function (x) { return norm(x.label) === w })
    || opts.find(function (x) { return norm(x.value) === w })
    || opts.find(function (x) { return norm(x.label).indexOf(w) === 0 })
  if (!o) return { ok: false, error: 'no option matches', options: opts.slice(0, 40).map(function (x) { return x.label.slice(0, 80) }) }
  if (o.disabled) return { ok: false, error: 'that option is disabled' }
  if (sel.disabled) return { ok: false, error: 'the select is disabled' }
  sel.focus()
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, o.value)
  sel.dispatchEvent(new Event('input', { bubbles: true }))
  sel.dispatchEvent(new Event('change', { bubbles: true }))
  return { ok: sel.value === o.value, value: o.value.slice(0, 80), label: o.label.slice(0, 80) }
}`

// Pacing: timing that reads like a person at the keyboard. rand is
// Math.random in the worker and fixed in tests.
export function actionPause(pacing, rand = Math.random) {
  return pacing === 'off' ? 0 : Math.round(150 + rand() * 450)
}

export function keyGap(pacing, rand = Math.random) {
  if (pacing === 'off') return 0
  const gap = 30 + rand() * 90
  // Now and then a longer hesitation, as between words.
  return Math.round(rand() < 0.06 ? gap + 150 + rand() * 200 : gap)
}

// Where in the element to click: the centre, or with pacing a point a little
// off it, kept inside the box.
export function clickPoint(q, pacing, rand = Math.random) {
  const xs = [q[0], q[2], q[4], q[6]]
  const ys = [q[1], q[3], q[5], q[7]]
  const cx = (xs[0] + xs[1] + xs[2] + xs[3]) / 4
  const cy = (ys[0] + ys[1] + ys[2] + ys[3]) / 4
  if (pacing === 'off') return { x: cx, y: cy }
  const hw = (Math.max(...xs) - Math.min(...xs)) / 2
  const hh = (Math.max(...ys) - Math.min(...ys)) / 2
  const dx = Math.min(hw * 0.3, 8) * (rand() * 2 - 1)
  const dy = Math.min(hh * 0.3, 4) * (rand() * 2 - 1)
  return { x: cx + dx, y: cy + dy }
}

// Mouse path from where the pointer was to the target: a few eased steps.
export function mousePath(from, to, pacing, rand = Math.random) {
  if (pacing === 'off' || !from) return [to]
  const n = 3 + Math.floor(rand() * 4)
  const out = []
  for (let i = 1; i <= n; i++) {
    const t = i / n
    const e = t * t * (3 - 2 * t)
    out.push({ x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e })
  }
  out[out.length - 1] = to
  return out
}

// How typed text goes in: one key event per ordinary character; newlines,
// tabs and anything off the keyboard go through insertText, so Enter never
// submits a form and Tab never moves focus.
export function keyPlan(text, pacing) {
  if (pacing === 'off') return text ? [{ insert: text }] : []
  const out = []
  for (const ch of text) {
    const code = ch.codePointAt(0)
    const key = ch.length === 1 && code >= 0x20 && code !== 0x7f && !(code >= 0x80 && code < 0xa0)
    if (key) out.push({ key: ch })
    else if (out.length && out[out.length - 1].insert !== undefined) out[out.length - 1].insert += ch
    else out.push({ insert: ch })
  }
  return out
}

// Errors worth one retry after a short wait: another extension's frame
// (a password manager, usually) was in the way for a moment.
export function transientError(message) {
  return /Cannot access a chrome-extension:\/\/ URL of different extension/.test(String(message || ''))
}

const TEXT_ROLES = new Set(['textbox', 'searchbox', 'combobox', 'textarea', 'spinbutton'])
const SKIP_ROLES = new Set(['none', 'generic', 'InlineTextBox', 'LineBreak'])

// Turns Accessibility.getFullAXTree into a compact list the driver can read.
// Field values are dropped: whatever sits in an input (a new API key, a
// password) never leaves the browser through a snapshot. The driver reads a
// field only through save_secret, whose value goes to the relay's one-shot
// store and never back to the driver.
export function compactAX(result, limit = 1500) {
  const nodes = (result && result.nodes) || []
  const byId = new Map(nodes.map((n) => [n.nodeId, n]))
  const roots = nodes.filter((n) => !n.parentId || !byId.has(n.parentId))
  const out = []
  const walk = (n, depth) => {
    if (out.length >= limit) return
    const role = n.role && n.role.value
    const name = n.name && typeof n.name.value === 'string' ? n.name.value.trim() : ''
    const keep = !n.ignored && role && !SKIP_ROLES.has(role) && !(role === 'StaticText' && !name) &&
      !((role === 'group' || role === 'paragraph' || role === 'Section' || role === 'div') && !name)
    let d = depth
    if (keep && typeof n.backendDOMNodeId === 'number') {
      const row = { ref: n.backendDOMNodeId, role, name: name.slice(0, 200), depth }
      const props = {}
      for (const p of n.properties || []) {
        if (['focused', 'disabled', 'checked', 'expanded', 'selected', 'required', 'readonly'].includes(p.name)) {
          props[p.name] = p.value && p.value.value
        }
      }
      if (Object.keys(props).length) row.props = props
      if (TEXT_ROLES.has(role)) {
        const has = n.value && n.value.value !== undefined && n.value.value !== ''
        row.filled = Boolean(has)
      }
      out.push(row)
      d = depth + 1
    }
    for (const c of n.childIds || []) {
      const child = byId.get(c)
      if (child) walk(child, d)
    }
  }
  for (const r of roots) walk(r, 0)
  // StaticText that only repeats its parent's name adds nothing.
  const rows = out.filter((row, i) => !(row.role === 'StaticText' && i > 0 && out[i - 1].name === row.name))
  if (out.length >= limit) rows.push({ ref: 0, role: 'truncated', name: `stopped at ${limit} nodes; scroll or narrow the page`, depth: 0 })
  return rows
}

const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTVWXYZ23456789'

export function normalizeCode(raw) {
  const s = String(raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
  if (s.length !== 6) return null
  for (const ch of s) if (!CODE_ALPHABET.includes(ch)) return null
  return `${s.slice(0, 3)}-${s.slice(3)}`
}

// ---- clear (scrollinondubs/new-jaxity#751) -------------------------------

// Select-all, then Backspace, as real key events with the customer's own
// modifier (Cmd on a Mac, Ctrl elsewhere): chrome.runtime.getPlatformInfo().os,
// never the driver host's OS. keyDown rather than rawKeyDown, because Monaco
// ignored a rawKeyDown select-all in the first live session. The commands
// arrays carry the editing command Chrome on a Mac needs alongside the event.
export function clearKeys(os) {
  const mac = os === 'mac'
  const mod = mac ? 4 : 2
  const modKey = mac
    ? { key: 'Meta', code: 'MetaLeft', windowsVirtualKeyCode: 91, location: 1 }
    : { key: 'Control', code: 'ControlLeft', windowsVirtualKeyCode: 17, location: 1 }
  const a = { key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65 }
  const bs = { key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 }
  return [
    { type: 'rawKeyDown', ...modKey, modifiers: mod },
    { type: 'keyDown', ...a, modifiers: mod, commands: ['selectAll'] },
    { type: 'keyUp', ...a, modifiers: mod },
    { type: 'keyUp', ...modKey, modifiers: 0 },
    { type: 'rawKeyDown', ...bs, modifiers: 0, commands: ['deleteBackward'] },
    { type: 'keyUp', ...bs, modifiers: 0 },
  ]
}

// Runs on the resolved ref via Runtime.callFunctionOn. Reports what kind of
// field it is, whether it is empty, and whether focus is inside it. It never
// returns the text itself. Fixed text: the relay never supplies code.
export const FIELD_STATE_FN = `function () {
  var el = this
  var root = el.closest ? el.closest('.monaco-editor, .cm-editor, .CodeMirror') : null
  if (!root && el.querySelector) root = el.querySelector('.monaco-editor, .cm-editor, .CodeMirror')
  var kind = 'other', text = null
  if (root && root.classList.contains('monaco-editor')) {
    kind = 'monaco'
    var v = root.querySelector('.view-lines')
    text = v ? v.textContent : ''
  } else if (root && root.classList.contains('cm-editor')) {
    kind = 'codemirror'
    var c = root.querySelector('.cm-content')
    if (c) { c = c.cloneNode(true); var p = c.querySelectorAll('.cm-placeholder'); for (var i = 0; i < p.length; i++) p[i].remove() }
    text = c ? c.textContent : ''
  } else if (root) {
    kind = 'codemirror'
    var l5 = root.querySelectorAll('.CodeMirror-code .CodeMirror-line')
    text = ''
    for (var j = 0; j < l5.length; j++) text += l5[j].textContent
  } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
    kind = 'input'
    text = el.value
  } else if (el.isContentEditable) {
    kind = 'contenteditable'
    text = el.textContent
  }
  var a = el.ownerDocument.activeElement
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement
  var focused = !!a && (a === el || el.contains(a) || (!!root && root.contains(a)))
  return { kind: kind, empty: text === null ? null : String(text).replace(/[\\s\\u00a0\\u200b]/g, '') === '', focused: focused }
}`

// ---- screenshot masking ----------------------------------------------------

const EDITOR_CLASS = /(^|\s)(monaco-editor|CodeMirror|cm-editor)(\s|$)/
const UNMASKED_INPUT_TYPES = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'range', 'color', 'hidden', 'file'])

function attr(node, name) {
  const a = node.attributes || []
  for (let i = 0; i < a.length; i += 2) if (a[i] === name) return a[i + 1]
  return undefined
}

// Walks DOM.getDocument({depth: -1, pierce: true}) and returns the
// backendNodeIds a screenshot must paint over: every text-entry input and
// textarea (the snapshot never shows field values, so neither does a
// screenshot), and every frame whose source is off the allowed sites (card
// fields, other people's widgets) or whose document is not in the tree.
// Shadow roots and same-process frames are in the tree, so their fields are
// covered one by one.
export function maskNodeIds(root, allow, pageUrl) {
  const out = []
  const walk = (n, inEditor) => {
    if (!n) return
    const name = String(n.nodeName || '').toUpperCase()
    const inside = inEditor || EDITOR_CLASS.test(String(attr(n, 'class') || ''))
    if (name === 'INPUT') {
      const t = String(attr(n, 'type') || 'text').toLowerCase()
      if (!UNMASKED_INPUT_TYPES.has(t)) out.push(n.backendNodeId)
    } else if (name === 'TEXTAREA') {
      // A code editor's hidden input proxy sits over the code it edits, and
      // the code is what a screenshot is for.
      if (!inside) out.push(n.backendNodeId)
    } else if (name === 'IFRAME' || name === 'FRAME' || name === 'EMBED' || name === 'OBJECT') {
      const src = attr(n, 'src') ?? attr(n, 'data')
      let ok = false
      if (src === undefined || src === '' || src === 'about:blank') ok = true
      else {
        try {
          ok = hostAllowed(new URL(src, pageUrl).href, allow)
        } catch {
          ok = false
        }
      }
      // A frame in another process has no contentDocument here, so its
      // fields cannot be found: paint over the whole frame.
      if (!ok || !n.contentDocument) out.push(n.backendNodeId)
    }
    for (const c of n.children || []) walk(c, inside)
    for (const c of n.shadowRoots || []) walk(c, inside)
    if (n.contentDocument) walk(n.contentDocument, false)
  }
  walk(root, false)
  return out.filter((id) => Number.isInteger(id))
}

// Token shapes the relay's redactor knows (cloudflare/installer/src/redact.ts),
// plus AWS access keys. A snapshot gets these redacted as text; a screenshot
// gets the element painted over.
const SECRET_SHAPES = [
  /\bsk-ant-[A-Za-z0-9_-]{16,}/,
  /\bsk-(proj-)?[A-Za-z0-9_-]{20,}/,
  /\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/,
  /\bgithub_pat_[A-Za-z0-9_]{20,}/,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/,
  /\bya29\.[A-Za-z0-9_-]{20,}/,
  /\b1\/\/[A-Za-z0-9_-]{30,}/,
  /\bGOCSPX-[A-Za-z0-9_-]{20,}/,
  /\bAIza[0-9A-Za-z_-]{35}/,
  /\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}\b/,
  /\b[A-Za-z0-9]{64}\b/,
  /\b(AKIA|ASIA)[A-Z0-9]{16}\b/,
  /(^|[^A-Za-z0-9/+])[A-Za-z0-9/+]{40}([^A-Za-z0-9/+=]|$)/,
]

export function looksSecret(text) {
  return typeof text === 'string' && text.length >= 16 && SECRET_SHAPES.some((re) => re.test(text))
}

// AX nodes whose accessible name looks like a credential (a key shown as
// plain text, not in a field), with their parent so a text node that has no
// box of its own is still covered.
export function secretShapeIds(axResult) {
  const nodes = (axResult && axResult.nodes) || []
  const byId = new Map(nodes.map((n) => [n.nodeId, n]))
  const out = []
  for (const n of nodes) {
    const name = n.name && typeof n.name.value === 'string' ? n.name.value : ''
    if (!looksSecret(name)) continue
    if (typeof n.backendDOMNodeId === 'number') out.push(n.backendDOMNodeId)
    const p = n.parentId && byId.get(n.parentId)
    if (p && typeof p.backendDOMNodeId === 'number') out.push(p.backendDOMNodeId)
  }
  return out
}

// A CDP quad (8 numbers, CSS px from the viewport's top left) to a padded
// rectangle in canvas pixels, clipped to the canvas. null when off screen.
export function quadToRect(q, scale, width, height, pad = 3) {
  if (!Array.isArray(q) || q.length < 8) return null
  const xs = [q[0], q[2], q[4], q[6]]
  const ys = [q[1], q[3], q[5], q[7]]
  const x0 = Math.max(0, Math.floor((Math.min(...xs) - pad) * scale))
  const y0 = Math.max(0, Math.floor((Math.min(...ys) - pad) * scale))
  const x1 = Math.min(width, Math.ceil((Math.max(...xs) + pad) * scale))
  const y1 = Math.min(height, Math.ceil((Math.max(...ys) + pad) * scale))
  if (x1 <= x0 || y1 <= y0) return null
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
}

// base64 characters, so a 1.2 MB string is a ~900 KB JPEG. The relay keeps
// a reply in one SQLite row (2 MB cap) and checks the same limit.
export const MAX_SCREENSHOT_B64 = 1_200_000

// ---- bot challenges ----------------------------------------------------------

const CF_IDS = new Set(['challenge-form', 'challenge-running', 'challenge-stage', 'challenge-body-text', 'cf-challenge-running', 'cf-please-wait', 'challenge-success-text'])
const CF_TITLE = /^(just a moment|attention required|verifying you are human|checking your browser|one more step)/i

// Pulls what challenge detection needs out of the same pierced DOM tree:
// element ids, custom element names, and every frame with its source.
export function challengeFacts(root, pageUrl) {
  const ids = []
  const tags = []
  const frames = []
  const walk = (n) => {
    if (!n) return
    const name = String(n.nodeName || '').toUpperCase()
    const id = attr(n, 'id')
    if (id && (CF_IDS.has(id) || id === 'px-captcha')) ids.push(id)
    if (name === 'AWSWAF-CAPTCHA') tags.push('awswaf-captcha')
    if (name === 'IFRAME' || name === 'FRAME') {
      const src = attr(n, 'src')
      if (src) {
        try {
          frames.push({ url: new URL(src, pageUrl).href, backendNodeId: n.backendNodeId })
        } catch {
          // not a URL
        }
      }
    }
    for (const c of n.children || []) walk(c)
    for (const c of n.shadowRoots || []) walk(c)
    if (n.contentDocument) walk(n.contentDocument)
  }
  walk(root)
  return { ids, tags, frames: frames.slice(0, 50) }
}

function frameKind(url) {
  let u
  try {
    u = new URL(url)
  } catch {
    return null
  }
  const h = u.hostname.toLowerCase()
  const on = (d) => h === d || h.endsWith('.' + d)
  const invisible = /(^|[?&#])size=invisible\b/.test(u.search + u.hash)
  if ((on('google.com') || on('recaptcha.net')) && /\/recaptcha\//.test(u.pathname)) {
    if (/\/bframe/.test(u.pathname)) return { kind: 'recaptcha', role: 'challenge' }
    if (/\/anchor/.test(u.pathname)) return { kind: 'recaptcha', role: invisible ? 'passive' : 'checkbox' }
    return null
  }
  if (on('hcaptcha.com')) {
    if (/frame=challenge/.test(u.hash)) return { kind: 'hcaptcha', role: 'challenge' }
    if (/frame=checkbox/.test(u.hash)) return { kind: 'hcaptcha', role: invisible ? 'passive' : 'checkbox' }
    return null
  }
  if (on('challenges.cloudflare.com')) return { kind: 'turnstile', role: 'widget' }
  if (on('captcha-delivery.com')) return { kind: 'datadome', role: 'challenge' }
  if (on('arkoselabs.com') || on('funcaptcha.com')) return { kind: 'arkose', role: 'challenge' }
  if (on('awswaf.com')) return { kind: 'aws-waf', role: 'challenge' }
  return null
}

// facts: { title, ids, tags, frames: [{ url, w, h, onscreen }] } where w/h
// are the frame's on-screen size in CSS px. Returns null, or
// { kind, blocking }. blocking means the driver cannot get past it and the
// person has to: a full-page interstitial, or a visible checkbox or picture
// challenge. An invisible reCAPTCHA badge or a Turnstile widget that may pass
// by itself is reported with blocking false.
export function classifyChallenge(facts) {
  const f = facts || {}
  const ids = new Set(f.ids || [])
  if (CF_TITLE.test(String(f.title || '').trim()) || [...ids].some((i) => CF_IDS.has(i))) return { kind: 'cloudflare', blocking: true }
  if (ids.has('px-captcha')) return { kind: 'perimeterx', blocking: true }
  if ((f.tags || []).includes('awswaf-captcha')) return { kind: 'aws-waf', blocking: true }
  let soft = null
  for (const fr of f.frames || []) {
    const k = frameKind(fr.url)
    if (!k) continue
    const seen = fr.onscreen && fr.w >= 30 && fr.h >= 30
    if (k.role === 'challenge' && seen && fr.h >= 60) return { kind: k.kind, blocking: true }
    if (k.role === 'checkbox' && seen) return { kind: k.kind, blocking: true }
    if (!soft) soft = { kind: k.kind, blocking: false }
  }
  return soft
}

// ---- Done --------------------------------------------------------------------

// Which extension page may send which message to the worker. Done only from
// the side panel, and only for the turn it is showing. A web page cannot get
// here at all (no content scripts, no externally_connectable, no web
// accessible resources); this pins the remaining paths.
export function messageRefusal(msg, sender, extId, panelUrl, turnNonce) {
  if (!sender || sender.id !== extId) return 'not this extension'
  const url = String(sender.url || '')
  if (!url.startsWith(`chrome-extension://${extId}/`)) return 'not an extension page'
  if (!msg || typeof msg !== 'object') return 'bad message'
  if (msg.type === 'done') {
    if (url.split('#')[0].split('?')[0] !== panelUrl) return 'done only from the side panel'
    if (!turnNonce || msg.turn !== turnNonce) return 'no such turn'
  }
  return null
}
