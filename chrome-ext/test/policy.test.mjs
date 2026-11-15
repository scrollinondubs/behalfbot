import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ALLOWED_METHODS, compactAX, hostAllowed, normalizeCode, refusal, scrollExpression } from '../policy.js'

const ALLOW = ['console.cloud.google.com']
const TAB = 'https://console.cloud.google.com/apis/library?project=kit-assistant-514520'

test('host allowlist: exact host or a dot-boundary subdomain, https only', () => {
  assert.equal(hostAllowed(TAB, ALLOW), true)
  assert.equal(hostAllowed('https://x.console.cloud.google.com/', ALLOW), true)
  assert.equal(hostAllowed('http://console.cloud.google.com/', ALLOW), false)
  assert.equal(hostAllowed('https://evilconsole.cloud.google.com/', ALLOW), false)
  assert.equal(hostAllowed('https://console.cloud.google.com.evil.example/', ALLOW), false)
  assert.equal(hostAllowed('https://accounts.google.com/', ALLOW), false)
  assert.equal(hostAllowed('chrome://settings', ALLOW), false)
  assert.equal(hostAllowed('not a url', ALLOW), false)
  assert.equal(hostAllowed(TAB, []), false)
})

test('host allowlist: plain http only for a local host the list names outright', () => {
  assert.equal(hostAllowed('http://localhost:8100/', ['localhost']), true)
  assert.equal(hostAllowed('http://localhost:8100/', ALLOW), false)
  assert.equal(hostAllowed('http://evil.localhost/', ['localhost']), false)
})

test('refusal: every op except note and human_step needs the tab on an allowed host', () => {
  const off = 'https://mail.google.com/mail/u/0/'
  for (const cmd of [
    { op: 'cdp', method: 'Accessibility.getFullAXTree', label: '' },
    { op: 'cdp', method: 'Input.dispatchMouseEvent', params: {}, label: 'Click' },
    { op: 'save_secret', ref: 5, secretTarget: 'x', label: '' },
    { op: 'scroll', dy: 300, label: '' },
  ]) {
    assert.equal(refusal(cmd, off, ALLOW), 'tab not allowed', cmd.op)
    assert.equal(refusal(cmd, TAB, ALLOW), null, cmd.op)
  }
  assert.equal(refusal({ op: 'note', label: 'x' }, off, ALLOW), null)
})

test('refusal: methods off the list, Runtime.evaluate from the relay, and unknown ops', () => {
  assert.equal(refusal({ op: 'cdp', method: 'Runtime.evaluate', params: { expression: '1' } }, TAB, ALLOW), 'method not allowed')
  assert.equal(refusal({ op: 'cdp', method: 'Network.getAllCookies' }, TAB, ALLOW), 'method not allowed')
  assert.equal(refusal({ op: 'cdp', method: 'Runtime.callFunctionOn' }, TAB, ALLOW), 'method not allowed')
  assert.equal(refusal({ op: 'eval' }, TAB, ALLOW), 'op not allowed')
  assert.ok(!ALLOWED_METHODS.has('Runtime.evaluate'))
})

test('refusal: Page.navigate only to an allowed host', () => {
  const nav = (url) => ({ op: 'cdp', method: 'Page.navigate', params: { url } })
  assert.equal(refusal(nav('https://console.cloud.google.com/billing'), TAB, ALLOW), null)
  assert.equal(refusal(nav('https://evil.example/'), TAB, ALLOW), 'navigate target not allowed')
  assert.equal(refusal(nav('javascript:alert(1)'), TAB, ALLOW), 'navigate target not allowed')
  assert.equal(refusal({ op: 'cdp', method: 'Page.navigate', params: {} }, TAB, ALLOW), 'navigate target not allowed')
})

test('scroll is a fixed template with an integer only', () => {
  assert.equal(scrollExpression(400), 'window.scrollBy(0, 400)')
  assert.throws(() => scrollExpression('0); fetch("x"'))
  assert.equal(refusal({ op: 'scroll', dy: '1);alert(1' }, TAB, ALLOW), 'bad scroll')
})

test('compactAX: keeps refs, roles and names, drops field values', () => {
  const tree = {
    nodes: [
      { nodeId: '1', role: { value: 'RootWebArea' }, name: { value: 'Credentials' }, childIds: ['2', '3', '4'], backendDOMNodeId: 1 },
      { nodeId: '2', parentId: '1', role: { value: 'button' }, name: { value: 'Create API key' }, backendDOMNodeId: 20 },
      { nodeId: '3', parentId: '1', role: { value: 'textbox' }, name: { value: 'Your API key' }, value: { type: 'string', value: 'SECRET-VALUE-123456' }, backendDOMNodeId: 30 },
      { nodeId: '4', parentId: '1', role: { value: 'generic' }, ignored: true, childIds: [], backendDOMNodeId: 40 },
    ],
  }
  const out = compactAX(tree)
  assert.deepEqual(out.map((r) => r.ref), [1, 20, 30])
  assert.equal(out[2].filled, true)
  assert.ok(!JSON.stringify(out).includes('SECRET-VALUE'))
})

test('compactAX: says when it stopped early', () => {
  const nodes = [{ nodeId: 'r', role: { value: 'RootWebArea' }, name: { value: 'p' }, childIds: [], backendDOMNodeId: 1 }]
  for (let i = 0; i < 10; i++) {
    nodes[0].childIds.push(`b${i}`)
    nodes.push({ nodeId: `b${i}`, parentId: 'r', role: { value: 'button' }, name: { value: `B${i}` }, backendDOMNodeId: 10 + i })
  }
  const out = compactAX({ nodes }, 5)
  assert.equal(out.at(-1).role, 'truncated')
  assert.equal(compactAX({ nodes }).at(-1).role, 'button')
})

test('pairing codes normalise and reject look-alikes', () => {
  assert.equal(normalizeCode(' k7q 4m2 '), 'K7Q-4M2')
  assert.equal(normalizeCode('K7Q-4M'), null)
  assert.equal(normalizeCode('K0Q-4M2'), null)
})

test('refusal: a backendNodeId or save_secret ref must be a positive int32, never a string', () => {
  const at = (backendNodeId) => ({ op: 'cdp', method: 'DOM.focus', params: { backendNodeId }, label: '' })
  assert.equal(refusal(at(2378), TAB, ALLOW), null)
  assert.equal(refusal(at('2378'), TAB, ALLOW), 'bad ref')
  assert.equal(refusal(at(0), TAB, ALLOW), 'bad ref')
  assert.equal(refusal(at(1.5), TAB, ALLOW), 'bad ref')
  assert.equal(refusal(at(2 ** 31), TAB, ALLOW), 'bad ref')
  assert.equal(refusal({ op: 'cdp', method: 'Accessibility.getFullAXTree', params: {} }, TAB, ALLOW), null)
  assert.equal(refusal({ op: 'save_secret', ref: '2378', secretTarget: 'x' }, TAB, ALLOW), 'bad ref')
})
