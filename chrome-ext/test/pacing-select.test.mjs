import { test } from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import {
  OPS,
  SELECT_OPTION_FN,
  actionPause,
  clickPoint,
  keyGap,
  keyPlan,
  mousePath,
  refusal,
  transientError,
} from '../policy.js'

const ALLOW = ['dashboard.stripe.com']
const TAB = 'https://dashboard.stripe.com/coupons/create'

test('click, type and select_option: need an allowed tab and an int ref; text and pacing checked', () => {
  for (const op of ['click', 'type', 'select_option']) assert.ok(OPS.has(op), op)
  assert.equal(refusal({ op: 'click', ref: 5, label: '' }, TAB, ALLOW), null)
  assert.equal(refusal({ op: 'click', ref: '5', label: '' }, TAB, ALLOW), 'bad ref')
  assert.equal(refusal({ op: 'click', ref: 5, label: '' }, 'https://evil.example/', ALLOW), 'tab not allowed')
  assert.equal(refusal({ op: 'type', ref: 5, text: 'abc', label: '' }, TAB, ALLOW), null)
  assert.equal(refusal({ op: 'type', ref: 5, label: '' }, TAB, ALLOW), 'bad text')
  assert.equal(refusal({ op: 'type', ref: 5, text: 'x'.repeat(2001), label: '' }, TAB, ALLOW), 'bad text')
  assert.equal(refusal({ op: 'select_option', ref: 5, text: 'Forever', label: '' }, TAB, ALLOW), null)
  assert.equal(refusal({ op: 'select_option', ref: 5, text: ' ', label: '' }, TAB, ALLOW), 'bad option')
  assert.equal(refusal({ op: 'click', ref: 5, pacing: 'fast', label: '' }, TAB, ALLOW), 'bad pacing')
  assert.equal(refusal({ op: 'click', ref: 5, pacing: 'off', label: '' }, TAB, ALLOW), null)
})

test('pacing off: no pauses, centre clicks, one insertText', () => {
  assert.equal(actionPause('off'), 0)
  assert.equal(keyGap('off'), 0)
  const q = [10, 20, 110, 20, 110, 60, 10, 60]
  assert.deepEqual(clickPoint(q, 'off'), { x: 60, y: 40 })
  assert.deepEqual(mousePath({ x: 0, y: 0 }, { x: 60, y: 40 }, 'off'), [{ x: 60, y: 40 }])
  assert.deepEqual(keyPlan('Gold\nSilver', 'off'), [{ insert: 'Gold\nSilver' }])
})

test('pacing human: pauses in range, a click point near the centre and inside the box, a path that ends on it', () => {
  for (const r of [0, 0.5, 0.999]) {
    const p = actionPause('human', () => r)
    assert.ok(p >= 150 && p <= 600, `pause ${p}`)
  }
  for (const seq of [[0.5, 0.5], [0.01, 0.01], [0.5, 0.0]]) {
    let i = 0
    const g = keyGap('human', () => seq[i++ % seq.length])
    assert.ok(g >= 30 && g <= 470, `gap ${g}`)
  }
  const q = [10, 20, 110, 20, 110, 60, 10, 60]
  for (const r of [0, 1]) {
    const p = clickPoint(q, 'human', () => r)
    assert.ok(p.x >= 10 && p.x <= 110 && p.y >= 20 && p.y <= 60)
    assert.ok(Math.abs(p.x - 60) <= 8 && Math.abs(p.y - 40) <= 4)
  }
  const path = mousePath({ x: 0, y: 0 }, { x: 60, y: 40 }, 'human', () => 0.5)
  assert.ok(path.length >= 3)
  assert.deepEqual(path[path.length - 1], { x: 60, y: 40 })
  assert.deepEqual(mousePath(null, { x: 1, y: 2 }, 'human'), [{ x: 1, y: 2 }])
})

test('keyPlan: one key per ordinary character; newline, tab and emoji go through insertText', () => {
  assert.deepEqual(keyPlan('a b\n\tc€😀', 'human'), [
    { key: 'a' }, { key: ' ' }, { key: 'b' }, { insert: '\n\t' }, { key: 'c' }, { key: '€' }, { insert: '😀' },
  ])
  assert.deepEqual(keyPlan('', 'human'), [])
})

test('transientError: only the other-extension frame error is retried', () => {
  assert.ok(transientError('Cannot access a chrome-extension:// URL of different extension'))
  assert.ok(!transientError('Node does not have a layout object'))
  assert.ok(!transientError(undefined))
})

// The function runs in a page; here it runs against a tiny DOM stand-in, in a
// fresh context, to check matching and the events a framework listens for.
function fakeSelect(options, { tracked = false } = {}) {
  const ctx = vm.createContext({})
  vm.runInContext(`
    class Event { constructor(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles) } }
    class HTMLOptionElement { constructor(value, label, disabled) { this.value = value; this.label = label; this.disabled = !!disabled; this.tagName = 'OPTION' } closest() { return this.parent } }
    class HTMLSelectElement {
      constructor() { this.options = []; this._value = ''; this.events = []; this.tagName = 'SELECT'; this.focused = false }
      get value() { return this._value }
      set value(v) { this._value = v }
      focus() { this.focused = true }
      dispatchEvent(e) { this.events.push(e.type + (e.bubbles ? '/bubbles' : '')) }
    }
    globalThis.HTMLOptionElement = HTMLOptionElement
    globalThis.HTMLSelectElement = HTMLSelectElement
    globalThis.Event = Event
  `, ctx)
  const sel = vm.runInContext('new HTMLSelectElement()', ctx)
  for (const [v, l, d] of options) {
    const o = vm.runInContext('(v, l, d) => new HTMLOptionElement(v, l, d)', ctx)(v, l, d)
    o.parent = sel
    sel.options.push(o)
  }
  if (tracked) {
    // React's value tracker: an own-property setter on the node that hides
    // the change unless the prototype setter is used.
    let own = sel._value
    Object.defineProperty(sel, 'value', { configurable: true, get() { return this._value }, set(v) { own = v } })
    sel.tracked = () => own
  }
  const fn = vm.runInContext(`(${SELECT_OPTION_FN})`, ctx)
  return { sel, run: (want, self = sel) => fn.call(self, want) }
}

const DURATION = [['once', 'Once'], ['repeating', 'Multiple months'], ['forever', 'Forever']]

test('select_option: by label, by value, case and space blind; input and change bubble', () => {
  const { sel, run } = fakeSelect(DURATION)
  assert.deepEqual({ ...run('Forever') }, { ok: true, value: 'forever', label: 'Forever' })
  assert.equal(sel.value, 'forever')
  assert.deepEqual([...sel.events], ['input/bubbles', 'change/bubbles'])
  assert.equal(sel.focused, true)
  assert.equal(run('repeating').value, 'repeating')
  assert.equal(run('  multiple   MONTHS ').value, 'repeating')
  assert.equal(run('Multi').value, 'repeating')
})

test('select_option: goes through the prototype setter, so a framework-tracked select sees the change', () => {
  const { sel, run } = fakeSelect(DURATION, { tracked: true })
  assert.equal(run('Forever').ok, true)
  assert.equal(sel._value, 'forever')
  assert.equal(sel.tracked(), '', 'the own-property setter was bypassed')
})

test('select_option: no match lists the options; disabled refused; an option ref works; not a select refused', () => {
  const { sel, run } = fakeSelect([...DURATION, ['x', 'Retired', true]])
  const miss = run('Weekly')
  assert.equal(miss.ok, false)
  assert.deepEqual([...miss.options], ['Once', 'Multiple months', 'Forever', 'Retired'])
  assert.equal(run('Retired').ok, false)
  assert.equal(run('Once', sel.options[0]).value, 'once')
  const notSelect = run('Once', { tagName: 'DIV' })
  assert.equal(notSelect.ok, false)
  assert.match(notSelect.error, /not a native select/)
})
