// The Sales Safari corpus: what the coach gets in safari/, the open item in
// the prompt, and a VCL without the route.

import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { startMockVcl } from './mock-vcl.mjs'
import {
  prepareSession, executeSession, SessionError, SAFARI_SYSTEM_LINE, MAX_FOCUS_INLINE_BYTES,
  safariFiles, safariFileName, writeWorkdirFiles,
} from '../session.mjs'
import { createFounderOsServer } from '../server.mjs'

const SKILL = 'founder-os-basic-coach'
const API_KEY = 'sk-ant-test-dedicated-key'

let vcl, A, tokenA, workRoot

beforeEach(async () => {
  vcl = await startMockVcl()
  A = vcl.addFounder({ stage: 1, name: 'Founder A' })
  tokenA = vcl.mintToken(A, { skill: SKILL })
  workRoot = mkdtempSync(join(tmpdir(), 'fos-safari-test-'))
})

afterEach(async () => {
  await vcl.close()
  rmSync(workRoot, { recursive: true, force: true })
})

function listFiles(dir) {
  return readdirSync(dir, { recursive: true })
    .filter(p => statSync(join(dir, p)).isFile())
    .sort()
}

// Records what claude would get, including the workdir as it stood at spawn
// time (the session removes it afterwards).
function fakeClaude(envelope = { reply: 'ok', ledger_writes: [] }) {
  const calls = []
  const spawnImpl = (bin, args, opts) => {
    const files = listFiles(opts.cwd)
    const contents = Object.fromEntries(files.map(f => [f, readFileSync(join(opts.cwd, f), 'utf8')]))
    calls.push({ stdin: '', bin, args, opts, files, contents })
    const child = new EventEmitter()
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.stdin = new PassThrough()
    child.stdin.setEncoding('utf8')
    child.stdin.on('data', c => (calls.at(-1).stdin += c))
    child.kill = () => {}
    setImmediate(() => {
      child.stdout.end(JSON.stringify({ result: JSON.stringify(envelope) }))
      child.emit('close', 0)
    })
    return child
  }
  return { spawnImpl, calls }
}

function deps(extra = {}) {
  return { workRoot, apiBase: vcl.baseUrl, apiKey: API_KEY, parentEnv: { PATH: '/usr/bin' }, ...extra }
}

function body(extra = {}) {
  return { founder_id: A, stage: 1, skill: SKILL, message: 'What did I find in the bakery thread?', session_token: tokenA, ...extra }
}

const system = call => call.args[call.args.indexOf('--append-system-prompt') + 1]
const prompt = call => call.stdin

const CORPUS = {
  watering_holes: [
    { id: 'wh-1', url: 'https://reddit.com/r/Baking', title: 'r/Baking', status: 'active' },
    { id: 'wh-2', url: 'https://forum.example/bread', title: 'Bread forum', status: 'parked' },
  ],
  threads: [
    {
      id: 'th-1',
      title: 'Flour prices\nare killing my bakery',
      url: 'https://reddit.com/r/Baking/1',
      watering_hole_id: 'wh-1',
      text: 'Every month the flour bill goes up and I cannot raise prices.',
      notes: [
        { id: 'n-1', kind: 'pain', quote: 'I cannot raise prices', body: 'Margin squeeze', tags: ['cost', 'pricing'] },
        { id: 'n-2', kind: 'worldview', quote: 'regulars would leave', body: '', tags: [] },
      ],
      extra_field_vcl_added: 'ignored',
    },
    { id: 'th-2', title: 'Sourdough starter help', url: 'https://forum.example/bread/2', watering_hole_id: 'wh-2', text: 'My starter died.', notes: [] },
  ],
  ebombs: [
    {
      id: 'eb-1',
      title: 'The flour price calculator',
      status: 'draft',
      draft_markdown: '# Price your loaf\n\nA table of flour costs.',
      sources: [{ note_id: 'n-1', quote: 'I cannot raise prices' }],
    },
  ],
  truncated: false,
}

const FOCUS = { type: 'thread', id: 'th-1' }

// Hub panels always send a focus, so run() does too unless told otherwise.
async function run({ request = { focus: FOCUS }, extraDeps = {} } = {}) {
  const logs = []
  const log = line => logs.push(JSON.parse(line))
  const { spawnImpl, calls } = fakeClaude()
  const session = await prepareSession(body(request), deps(extraDeps))
  const outcome = await executeSession(session, deps({ spawnImpl, log, ...extraDeps }))
  return { outcome, call: calls[0], safari: logs.find(l => l.event === 'founder_os_safari') }
}

// --- files ----------------------------------------------------------------------

test('a corpus from VCL is written as safari/index.md plus one file per thread and ebomb', async () => {
  vcl.serveSafariCorpus(CORPUS)
  const { outcome, call, safari } = await run()
  assert.equal(outcome.status, 'completed')
  const safariFilesWritten = call.files.filter(f => f.startsWith('safari/'))
  assert.deepEqual(safariFilesWritten, ['safari/ebombs/eb-1.md', 'safari/index.md', 'safari/threads/th-1.md', 'safari/threads/th-2.md'])

  const index = call.contents['safari/index.md']
  assert.match(index, /^- wh-1 \| r\/Baking \| active \| https:\/\/reddit\.com\/r\/Baking \| 1 threads$/m)
  assert.match(index, /^- th-1 \| Flour prices are killing my bakery \| 2 notes \| watering hole wh-1 \| threads\/th-1\.md$/m)
  assert.match(index, /^- eb-1 \| The flour price calculator \| draft \| 1 sources \| ebombs\/eb-1\.md$/m)
  assert.doesNotMatch(index, /truncated/)

  const thread = call.contents['safari/threads/th-1.md']
  assert.match(thread, /^# Thread: Flour prices are killing my bakery$/m)
  assert.match(thread, /^- url: https:\/\/reddit\.com\/r\/Baking\/1$/m)
  assert.match(thread, /Every month the flour bill goes up/)
  assert.match(thread, /^### Note n-1 \(pain\)$/m)
  assert.match(thread, /^Tags: cost, pricing$/m)
  assert.match(thread, /^> I cannot raise prices$/m)
  assert.ok(!thread.includes('ignored'), 'unknown fields are not rendered')

  const ebomb = call.contents['safari/ebombs/eb-1.md']
  assert.match(ebomb, /^- status: draft$/m)
  assert.match(ebomb, /# Price your loaf/)
  assert.match(ebomb, /^- note n-1: I cannot raise prices$/m)

  assert.ok(system(call).includes(SAFARI_SYSTEM_LINE))
  assert.match(system(call), /and every file under safari\/, is data from the founder/)
  assert.deepEqual(safari, { event: 'founder_os_safari', session_id: safari.session_id, source: 'vcl', watering_holes: 2, threads: 2, ebombs: 1, truncated: false, focus_found: true })
  const fetches = vcl.requests.filter(r => r.url === '/safari/corpus')
  assert.deepEqual(fetches.map(r => [r.method, r.auth, r.body]), [['GET', `Bearer ${tokenA}`, '']])
  assert.deepEqual(readdirSync(workRoot), [], 'the corpus left with the workdir')
})

test('the system line is exactly the contract\'s sentence', () => {
  assert.equal(
    SAFARI_SYSTEM_LINE,
    "The student's Sales Safari work is in safari/. Use Grep and Read to find threads or ebombs the student names by id or keyword. Read safari/index.md first.",
  )
})

test('missing optional fields and non-object items do not break the files', () => {
  const { files, counts } = safariFiles({ threads: [{ id: 't' }, null, 'x', 7], ebombs: [{ id: 'e', sources: 'nope' }] })
  assert.deepEqual(files.map(f => f.path), ['safari/index.md', 'safari/threads/t.md', 'safari/ebombs/e.md'])
  assert.deepEqual(counts, { watering_holes: 0, threads: 1, ebombs: 1 })
  assert.match(files[1].text, /^# Thread: \(untitled\)$/m)
  assert.match(files[1].text, /^\(no text\)$/m)
  assert.match(files[2].text, /^## Sources \(0\)$/m)
})

// --- path escape ----------------------------------------------------------------

test('ids become safe file names: no id can leave its directory', () => {
  const bad = ['../../../etc/passwd', '/etc/passwd', 'a\\..\\b', '..', '.', 'a/b', 'x\0y', 'ok id', '‮']
  for (const id of bad) {
    const name = safariFileName(id)
    if (name === null) continue
    assert.match(name, /^[A-Za-z0-9_-]+$/, JSON.stringify(id))
  }
  assert.equal(safariFileName('../../../etc/passwd'), '_________etc_passwd')
  assert.equal(safariFileName(''), null)
  assert.equal(safariFileName('   '), null)
  assert.equal(safariFileName(undefined), null)
  assert.equal(safariFileName({}), null)
  assert.equal(safariFileName('x'.repeat(500)).length, 128)

  const { files } = safariFiles({ threads: bad.map(id => ({ id, title: id })), ebombs: [{ id: '../../x' }] })
  for (const f of files) assert.match(f.path, /^safari\/(index|threads\/[A-Za-z0-9_-]+|ebombs\/[A-Za-z0-9_-]+)\.md$/, f.path)
})

test('two ids that sanitize to one name keep the first and drop the second', () => {
  const { files, byId } = safariFiles({ threads: [{ id: 'a/b', text: 'first' }, { id: 'a:b', text: 'second' }, { id: 'a/b', text: 'dup' }] })
  assert.deepEqual(files.map(f => f.path), ['safari/index.md', 'safari/threads/a_b.md'])
  assert.match(files[1].text, /first/)
  assert.ok(byId.thread.has('a/b'))
  assert.ok(!byId.thread.has('a:b'))
})

test('the write guard refuses any path outside the workdir', () => {
  const dir = mkdtempSync(join(workRoot, 'guard-'))
  for (const path of ['../escape.md', 'safari/../../escape.md', '/tmp/escape.md']) {
    assert.throws(() => writeWorkdirFiles(dir, [{ path, text: 'x' }]), e => e instanceof SessionError && e.code === 'bad_material_path', path)
  }
  assert.ok(!existsSync(join(workRoot, 'escape.md')))
})

test('a corpus with traversal ids writes inside safari/ only', async () => {
  vcl.serveSafariCorpus({ threads: [{ id: '../../../../escape', title: 'evil', text: 'x' }], ebombs: [{ id: '../../skill/SKILL', draft_markdown: 'overwrite' }] })
  const { outcome, call } = await run()
  assert.equal(outcome.status, 'completed')
  assert.ok(call.files.includes('safari/threads/____________escape.md'))
  assert.ok(call.files.includes('safari/ebombs/______skill_SKILL.md'))
  assert.ok(!call.contents['skill/SKILL.md'].includes('overwrite'))
  assert.ok(!existsSync(join(workRoot, 'escape.md')))
  assert.ok(!existsSync(join(dirname(workRoot), 'escape.md')))
})

// --- the open item --------------------------------------------------------------

test('a focus thread is inlined in the user prompt, ahead of the message', async () => {
  vcl.serveSafariCorpus(CORPUS)
  const { call, safari } = await run({ request: { focus: { type: 'thread', id: 'th-1' } } })
  const p = prompt(call)
  const open = p.indexOf('# OPEN ITEM (thread th-1)')
  assert.ok(open > p.indexOf('# REFERENCED ARTIFACTS'))
  assert.ok(open < p.indexOf('# FOUNDER MESSAGE'))
  assert.ok(p.includes(call.contents['safari/threads/th-1.md']))
  assert.match(system(call), /REFERENCED ARTIFACTS, OPEN ITEM and FOUNDER MESSAGE/)
  assert.equal(safari.focus_found, true)
})

test('a focus ebomb is inlined too', async () => {
  vcl.serveSafariCorpus(CORPUS)
  const { call } = await run({ request: { focus: { type: 'ebomb', id: 'eb-1' } } })
  assert.ok(prompt(call).includes('# OPEN ITEM (ebomb eb-1)\n' + call.contents['safari/ebombs/eb-1.md']))
})

test('a long focus item is cut in bytes and points at its file', async () => {
  // Three bytes a char: a char cap of the same number would blow the limit.
  for (const text of ['word '.repeat(60000), '\u20ac'.repeat(100000)]) {
    vcl.serveSafariCorpus({ threads: [{ id: 'big', title: 'Big', text }] })
    tokenA = vcl.mintToken(A, { skill: SKILL })
    const { outcome, call } = await run({ request: { focus: { type: 'thread', id: 'big' } } })
    assert.equal(outcome.status, 'completed')
    assert.ok(call.contents['safari/threads/big.md'].includes(text), 'the file keeps the whole text')
    const p = prompt(call)
    assert.ok(p.includes(`[cut at ${MAX_FOCUS_INLINE_BYTES} bytes: Read safari/threads/big.md for the rest]`))
    assert.ok(!p.includes('\uFFFD'), 'no half char at the cut')
    assert.ok(Buffer.byteLength(p, 'utf8') < MAX_FOCUS_INLINE_BYTES + 32 * 1024, `prompt is ${Buffer.byteLength(p, 'utf8')} bytes`)
  }
})

test('the user prompt goes on stdin, so a prompt past the 128 KB argv cap still reaches claude', async () => {
  const text = 'pain '.repeat(40000)
  vcl.serveSafariCorpus({ threads: [{ id: 'big', title: 'Big', text }] })
  tokenA = vcl.mintToken(A, { skill: SKILL })
  const message = 'x'.repeat(20000)
  const { outcome, call } = await run({ request: { message, focus: { type: 'thread', id: 'big' } } })
  assert.equal(outcome.status, 'completed')
  assert.ok(call.stdin.endsWith(message), 'the whole message arrives on stdin')
  assert.ok(Buffer.byteLength(call.stdin, 'utf8') > 128 * 1024)
  assert.ok(!call.args.some(a => a.includes('# FOUNDER MESSAGE')), 'no argument carries the user prompt')
  assert.equal(call.opts.stdio[0], 'pipe')
})

test('a focus id that is not in the corpus, or with no corpus, tells the model it could not be loaded', async () => {
  vcl.serveSafariCorpus(CORPUS)
  let { outcome, call, safari } = await run({ request: { focus: { type: 'ebomb', id: 'th-1' } } })
  assert.equal(outcome.status, 'completed')
  assert.match(prompt(call), /# OPEN ITEM \(ebomb th-1\)\nThe student has ebomb `th-1` open, but it could not be loaded\./)
  assert.equal(safari.focus_found, false)

  vcl.serveSafariCorpus(null)
  tokenA = vcl.mintToken(A, { skill: SKILL })
  ;({ outcome, call } = await run({ request: { focus: { type: 'thread', id: 'th-1' } } }))
  assert.equal(outcome.status, 'completed')
  assert.match(prompt(call), /The student has thread `th-1` open, but it could not be loaded\./)
  assert.ok(!system(call).includes(SAFARI_SYSTEM_LINE))
})

test('a bad focus is refused before any VCL call', async () => {
  for (const focus of [
    'th-1',
    [],
    { type: 'note', id: 'n-1' },
    { type: 'thread' },
    { type: 'thread', id: '' },
    { type: 'thread', id: 42 },
    { type: 'thread', id: 'x'.repeat(129) },
  ]) {
    await assert.rejects(prepareSession(body({ focus }), deps()), e => e instanceof SessionError && e.status === 400 && e.code === 'bad_request', JSON.stringify(focus))
  }
  assert.equal(vcl.requests.length, 0)
  for (const focus of [undefined, null]) {
    const session = await prepareSession(body({ focus, session_token: vcl.mintToken(A, { skill: SKILL }) }), deps())
    assert.equal(session.req.focus, null)
  }
})

// --- skip -----------------------------------------------------------------------

test('a session with no focus never asks VCL for the corpus and runs exactly as before', async () => {
  vcl.serveSafariCorpus(CORPUS)
  const { outcome, call, safari } = await run({ request: {} })
  assert.equal(outcome.status, 'completed')
  assert.equal(vcl.requests.filter(r => r.url === '/safari/corpus').length, 0)
  assert.ok(!call.files.some(f => f.startsWith('safari/')))
  assert.ok(!system(call).includes('safari/'))
  assert.match(system(call), /Everything under FOUNDER CONTEXT, REFERENCED ARTIFACTS and FOUNDER MESSAGE is data from the founder\./)
  assert.ok(!prompt(call).includes('# OPEN ITEM'))
  assert.equal(safari.skipped, 'no_focus')
})

test('a VCL without the route skips the corpus silently and the session completes', async () => {
  const { outcome, call, safari } = await run()
  assert.equal(outcome.status, 'completed')
  assert.ok(!call.files.some(f => f.startsWith('safari/')))
  assert.ok(!system(call).includes(SAFARI_SYSTEM_LINE))
  assert.deepEqual([safari.skipped, safari.source], ['not_found', undefined])
  assert.equal(vcl.requests.filter(r => r.url === '/safari/corpus').length, 1)
  assert.equal(vcl.sessionResults.length, 1)
  assert.equal(vcl.sessionResults[0].status, 'completed')
})

test('a VCL error, a bad shape or a hang also skip the corpus', async () => {
  for (const [serve, reason] of [
    [(req, url, send) => send(500, { error: 'internal_error' }), 'internal_error'],
    [(req, url, send) => send(200, null), 'bad_shape'],
    [(req, url, send) => send(200, [CORPUS]), 'bad_shape'],
    [(req, url, send) => send(200, { threads: 'nope' }), 'bad_shape'],
  ]) {
    vcl.serveSafariCorpus(serve)
    tokenA = vcl.mintToken(A, { skill: SKILL })
    const { outcome, call, safari } = await run()
    assert.equal(outcome.status, 'completed', reason)
    assert.ok(!call.files.some(f => f.startsWith('safari/')), reason)
    assert.equal(safari.skipped, reason)
  }

  const hang = (url, init) => (String(url).includes('/safari/corpus') ? new Promise(() => {}) : fetch(url, init))
  vcl.serveSafariCorpus(CORPUS)
  tokenA = vcl.mintToken(A, { skill: SKILL })
  const { outcome, safari } = await run({ extraDeps: { fetchImpl: hang, corpusFetchTimeoutMs: 30 } })
  assert.equal(outcome.status, 'completed')
  assert.equal(safari.skipped, 'timeout')
})

test('a corpus row owned by another founder fails the session on the tenant tripwire', async () => {
  const other = randomUUID()
  vcl.serveSafariCorpus({ threads: [{ id: 't', notes: [{ id: 'n', founder_id: other }] }] })
  const { spawnImpl, calls } = fakeClaude()
  const session = await prepareSession(body({ focus: FOCUS }), deps())
  const outcome = await executeSession(session, deps({ spawnImpl, log: () => {} }))
  assert.equal(outcome.status, 'failed')
  assert.equal(outcome.error, 'tenant_mismatch')
  assert.equal(calls.length, 0, 'claude never ran')
})

// --- truncated ------------------------------------------------------------------

test('a truncated corpus says so in the index and in each thread that lost its text', async () => {
  vcl.serveSafariCorpus({
    ...CORPUS,
    threads: [{ ...CORPUS.threads[0], text: undefined }, CORPUS.threads[1]],
    truncated: true,
  })
  const { outcome, call, safari } = await run()
  assert.equal(outcome.status, 'completed')
  assert.match(call.contents['safari/index.md'], /The corpus was truncated: the text of the oldest threads was dropped\./)
  const old = call.contents['safari/threads/th-1.md']
  assert.match(old, /^\(no text: the corpus was truncated and older thread text was dropped\)$/m)
  assert.match(old, /^### Note n-1 \(pain\)$/m, 'notes survive the cut')
  assert.match(call.contents['safari/threads/th-2.md'], /My starter died\./)
  assert.equal(safari.truncated, true)
})

// --- the shim -------------------------------------------------------------------

test('shim: a focus in the session request reaches the prompt', async () => {
  vcl.serveSafariCorpus(CORPUS)
  const { spawnImpl, calls } = fakeClaude()
  const { server, settled } = createFounderOsServer(deps({ spawnImpl, log: () => {} }))
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  try {
    const res = await fetch(`http://127.0.0.1:${server.address().port}/session`, {
      method: 'POST',
      body: JSON.stringify(body({ focus: { type: 'ebomb', id: 'eb-1' } })),
    })
    assert.equal(res.status, 202)
    const outcome = await settled()
    assert.equal(outcome.status, 'completed')
    assert.ok(prompt(calls[0]).includes('# OPEN ITEM (ebomb eb-1)'))
  } finally {
    server.close()
  }
})
