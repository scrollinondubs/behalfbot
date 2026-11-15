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

export const OPS = new Set(['cdp', 'scroll', 'save_secret', 'human_step', 'note'])

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
  }
  if (cmd.op === 'scroll') {
    if (!Number.isInteger(cmd.dy) || Math.abs(cmd.dy) > 20000) return 'bad scroll'
  }
  if (cmd.op === 'save_secret') {
    if (!Number.isInteger(cmd.ref) || cmd.ref <= 0) return 'bad ref'
    if (typeof cmd.secretTarget !== 'string' || !cmd.secretTarget) return 'missing vault item'
  }
  return null
}

export function scrollExpression(dy) {
  if (!Number.isInteger(dy)) throw new Error('bad scroll')
  return `window.scrollBy(0, ${dy})`
}

// Runs in the page via Runtime.callFunctionOn on the resolved node. Fixed
// text: the relay never supplies code.
export const READ_FIELD_FN =
  'function () { var v = (this.value !== undefined && this.value !== null && this.value !== "") ? this.value : this.textContent; return String(v || "").trim() }'

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
