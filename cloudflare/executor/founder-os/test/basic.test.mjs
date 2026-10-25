// Skill types, stage materials from VCL, and image attachments.

import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import { startMockVcl } from './mock-vcl.mjs'
import { SKILLS } from './fixtures/content.mjs'
import { parseSkill } from '../content.mjs'
import { prepareSession, executeSession, SessionError, MAX_STAGE_FILE_BYTES, validStageFiles } from '../session.mjs'
import { fetchImage, MAX_IMAGE_BYTES } from '../attachments.mjs'

const SKILL = 'founder-os-basic-review'
const API_KEY = 'sk-ant-test-dedicated-key'
const BLOB_URL = 'https://abc123xyz.public.blob.vercel-storage.com/founder-os/p1/shot-Ab12.png'

const PNG = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(64, 7)])
const JPEG = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(64, 1)])
const WEBP = Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WEBP'), Buffer.alloc(64, 2)])

let vcl, A, tokenA, workRoot

beforeEach(async () => {
  vcl = await startMockVcl()
  A = vcl.addFounder({ stage: 1, name: 'Founder A' })
  tokenA = vcl.mintToken(A, { skill: SKILL })
  workRoot = mkdtempSync(join(tmpdir(), 'fos-basic-test-'))
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
function fakeClaude(envelope) {
  const calls = []
  const spawnImpl = (bin, args, opts) => {
    const files = listFiles(opts.cwd)
    const contents = Object.fromEntries(files.map(f => [f, readFileSync(join(opts.cwd, f))]))
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

// Sends ledger calls to the mock VCL and everything else to `blob`.
function routedFetch(blob) {
  const blobCalls = []
  const fetchImpl = (url, init) => {
    if (String(url).startsWith(vcl.baseUrl)) return fetch(url, init)
    blobCalls.push({ url: String(url), init })
    return blob(String(url), init)
  }
  return { fetchImpl, blobCalls }
}

const serve = (bytes, type = 'image/png') => async () =>
  new Response(bytes, { status: 200, headers: { 'content-type': type } })

function deps(extra = {}) {
  return { workRoot, apiBase: vcl.baseUrl, apiKey: API_KEY, parentEnv: { PATH: '/usr/bin' }, ...extra }
}

// VCL mints a token for one session and so one skill: a body naming another
// skill gets a token minted for it.
function body(extra = {}) {
  const skill = extra.skill ?? SKILL
  const session_token = extra.session_token ?? (skill === SKILL ? tokenA : vcl.mintToken(A, { skill }))
  return { founder_id: A, stage: 1, message: 'Please review my work on: Alpha card', ...extra, skill, session_token }
}

function imageArtifact(meta = {}) {
  return vcl.seed('artifacts', A, {
    stage: 1,
    kind: 'basic_alpha_card',
    version: 1,
    body: 'My work, screenshot attached.',
    meta: { card_id: 'basic-alpha-card', submission_type: 'file', file_name: 'shot.png', content_type: 'image/png', size: PNG.length, blob_url: BLOB_URL, ...meta },
  })
}

// --- skill types --------------------------------------------------------------

test('parseSkill accepts the real type names and the old aliases', () => {
  const cases = {
    'founder-os-stage-1-alpha': ['stage-skill', 'advanced'],
    'founder-os-stage-1-auditor': ['auditor', 'advanced'],
    'founder-os-stage-1-checker': ['auditor', 'advanced'],
    'founder-os-coach-fixture': ['coach', 'advanced'],
    'founder-os-basic-coach': ['basic-coach', 'basic'],
    'founder-os-basic-review': ['basic-review', 'basic'],
  }
  for (const [name, [type, track]] of Object.entries(cases)) {
    const skill = parseSkill(name, SKILLS[name])
    assert.ok(skill, name)
    assert.equal(skill.type, type, name)
    assert.equal(skill.track, track, name)
    assert.equal(skill.text, SKILLS[name], name)
  }
  assert.equal(parseSkill(SKILL, SKILLS[SKILL]).stage, null)
  assert.equal(parseSkill('founder-os-basic-coach', SKILLS[SKILL]), null, 'the frontmatter name must match')
})

test('a basic-skill with an unknown role is not a skill', async () => {
  assert.equal(parseSkill('founder-os-basic-judge', SKILLS['founder-os-basic-judge']), null)
  await assert.rejects(prepareSession(body({ skill: 'founder-os-basic-judge' }), deps()), e => e instanceof SessionError && e.status === 404 && e.code === 'skill_not_found')
})

test('an auditor-skill session gets the auditor context and may record a gate decision', async () => {
  const session = await prepareSession(body({ skill: 'founder-os-stage-1-checker' }), deps())
  assert.ok(Array.isArray(session.context.audits))
  const art = vcl.seed('artifacts', A, { stage: 1, kind: 'audience', version: 1, body: 'x', meta: {} })
  const { spawnImpl, calls } = fakeClaude({
    reply: 'passed',
    ledger_writes: [{ method: 'record_gate_decision', args: { stage: 1, gate_id: 'stage-1-alpha', decision: 'pass', evidence: [{ table: 'artifacts', id: art.id }], rationale: 'ok' } }],
  })
  const outcome = await executeSession(session, deps({ spawnImpl }))
  assert.equal(outcome.status, 'completed')
  assert.deepEqual(outcome.writes.map(w => w.status), ['applied'])
  assert.deepEqual(calls[0].files, ['cards/dolor-card.md', 'gate-stage-1-alpha.md', 'skill/SKILL.md'])
})

test('a coach-skill session runs and gets the coach writes', async () => {
  const { spawnImpl } = fakeClaude({ reply: 'ok', ledger_writes: [{ method: 'add_pain', args: { quote: 'q', job: 'j' } }] })
  const outcome = await executeSession(await prepareSession(body({ skill: 'founder-os-coach-fixture' }), deps()), deps({ spawnImpl }))
  assert.equal(outcome.status, 'completed')
  assert.deepEqual(outcome.writes.map(w => w.status), ['applied'])
})

// --- Basic materials ----------------------------------------------------------

const BASIC_STAGE_1 = [
  'basic/gates/stage-1-ipsum.md',
  'basic/stage-1/alpha-card.md',
  'basic/stage-1/beta-card.md',
  'basic/stage-1/gamma-card.md',
  'skill/SKILL.md',
]

test('a Basic session gets its stage\'s cards with Coach checks and its panel, nothing from core or other stages', async () => {
  const { spawnImpl, calls } = fakeClaude({ reply: 'Keep going.', ledger_writes: [] })
  const outcome = await executeSession(await prepareSession(body({ skill: 'founder-os-basic-coach' }), deps()), deps({ spawnImpl }))
  assert.equal(outcome.status, 'completed')
  assert.deepEqual(calls[0].files, BASIC_STAGE_1)
  assert.match(calls[0].contents['basic/stage-1/alpha-card.md'].toString(), /COACH CHECK basic-alpha-card/)
  const system = calls[0].args[calls[0].args.indexOf('--append-system-prompt') + 1]
  assert.match(system, /\$FOUNDER_OS_DIR, read it as your working directory/)
  assert.deepEqual(readdirSync(workRoot), [])
})

test('a Basic review with no writes completes; one that tries any write is rejected whole', async () => {
  const reply = 'Good start.\n\n```founderos-verdict\n{"card_id": "basic-alpha-card", "verdict": "accepted", "failed": []}\n```'
  let { spawnImpl, calls } = fakeClaude({ reply, ledger_writes: [] })
  let outcome = await executeSession(await prepareSession(body(), deps()), deps({ spawnImpl }))
  assert.equal(outcome.status, 'completed')
  assert.equal(outcome.reply, reply)
  const system = calls[0].args[calls[0].args.indexOf('--append-system-prompt') + 1]
  assert.match(system, /A basic-review may not write anything/)

  ;({ spawnImpl } = fakeClaude({ reply: 'x', ledger_writes: [{ method: 'add_artifact', args: { stage: 1, kind: 'note', body: 'y' } }] }))
  outcome = await executeSession(await prepareSession(body({ session_token: vcl.mintToken(A, { skill: SKILL }) }), deps()), deps({ spawnImpl }))
  assert.equal(outcome.error, 'envelope_rejected')
  assert.equal(vcl.rowsFor('artifacts', A).length, 0)
})

test('the card text VCL appends to the message still reaches the model unchanged', async () => {
  const message = 'Please review my work on: Alpha card\n\n---\nid: basic-alpha-card\n---\n## Coach checks\n- appended'
  const { spawnImpl, calls } = fakeClaude({ reply: 'ok', ledger_writes: [] })
  await executeSession(await prepareSession(body({ message }), deps()), deps({ spawnImpl }))
  assert.ok(calls[0].stdin.endsWith(message))
})

// --- stage files from VCL ----------------------------------------------------

const SHA = 'eab31eda71defb38ff195eaf223ae0d2f3ac7280'
const EDITED = '---\nid: basic-alpha-card\nstage: 1\n---\n## What this is\nEDITED IN VCL\n\n## Coach checks\n- edited check\n'
const vclFiles = (extra = []) => ({
  base_sha: SHA,
  files: [
    { path: 'basic/stage-1/alpha-card.md', text: EDITED },
    { path: 'basic/stage-1/beta-card.md', text: '---\nid: basic-beta-card\n---\nVCL beta\n' },
    { path: 'basic/gates/stage-1-ipsum.md', text: '---\nid: basic-stage-1-ipsum\n---\nVCL gate\n' },
    ...extra,
  ],
})
const BASIC_COACH = parseSkill('founder-os-basic-coach', SKILLS['founder-os-basic-coach'])
const ADVANCED = parseSkill('founder-os-stage-1-alpha', SKILLS['founder-os-stage-1-alpha'])

async function runBasic(extraDeps = {}) {
  const logs = []
  const log = line => logs.push(JSON.parse(line))
  const { spawnImpl, calls } = fakeClaude({ reply: 'ok', ledger_writes: [] })
  // The ledger client, and so its fetchImpl, is made at prepare time.
  const b = body({ skill: 'founder-os-basic-coach' })
  const session = await prepareSession(b, deps(extraDeps))
  const outcome = await executeSession(session, deps({ spawnImpl, log, ...extraDeps }))
  return { outcome, call: calls[0], calls, token: b.session_token, materials: logs.find(l => l.event === 'founder_os_materials') }
}

test('a Basic session writes exactly the stage files VCL serves, edits included', async () => {
  vcl.serveStageContent(vclFiles())
  const { outcome, call, token, materials } = await runBasic()
  assert.equal(outcome.status, 'completed')
  assert.deepEqual(call.files, [
    'basic/gates/stage-1-ipsum.md',
    'basic/stage-1/alpha-card.md',
    'basic/stage-1/beta-card.md',
    'skill/SKILL.md',
  ])
  assert.equal(call.contents['basic/stage-1/alpha-card.md'].toString(), EDITED)
  assert.deepEqual(materials, { event: 'founder_os_materials', session_id: materials.session_id, source: 'vcl', base_sha: SHA, files: 3 })
  const fetches = vcl.requests.filter(r => r.url.startsWith('/content/stage'))
  assert.deepEqual(fetches.map(r => [r.method, r.url, r.auth, r.body]), [['GET', '/content/stage?stage=1', `Bearer ${token}`, '']])
  assert.equal(vcl.sessionResults.length, 1)
  assert.deepEqual(readdirSync(workRoot), [])
})

test('the SKILL.md in the workdir is the text VCL served for the session', async () => {
  const { call } = await runBasic()
  assert.equal(call.contents['skill/SKILL.md'].toString(), SKILLS['founder-os-basic-coach'])
})

test('no stage files from VCL fails the session: there is no copy in the image to fall back to', async () => {
  for (const [serve, reason] of [
    [(req, url, send) => send(404, { error: 'not_found' }), 'not_found'],
    [(req, url, send) => send(500, { error: 'internal_error' }), 'internal_error'],
    [(req, url, send) => send(200, null), 'no_files'],
    [{ base_sha: SHA, files: [] }, 'no_files'],
  ]) {
    vcl.serveStageContent(serve)
    vcl.sessionResults.length = 0
    const { outcome, calls, materials } = await runBasic()
    assert.deepEqual([outcome.status, outcome.error], ['failed', 'content_unavailable'], reason)
    assert.equal(calls.length, 0, `${reason}: claude must not run without cards`)
    assert.equal(materials.reason, reason)
    assert.deepEqual(vcl.sessionResults.map(r => [r.status, r.error]), [['failed', 'content_unavailable']], reason)
  }
  assert.deepEqual(readdirSync(workRoot), [])
})

test('a VCL that hangs fails the session after the timeout, even if the fetch ignores the abort', async () => {
  const hang = (url, init) =>
    String(url).includes('/content/stage') ? new Promise(() => {}) : fetch(url, init)
  const { outcome, calls, materials } = await runBasic({ fetchImpl: hang, contentFetchTimeoutMs: 30 })
  assert.deepEqual([outcome.status, outcome.error], ['failed', 'content_unavailable'])
  assert.equal(calls.length, 0)
  assert.equal(materials.reason, 'timeout')
})

test('one unsafe file rejects the whole set and fails the session', async () => {
  const bad = {
    traversal: { path: 'basic/stage-1/../../../etc/passwd.md', text: 'x' },
    absolute: { path: '/basic/stage-1/x.md', text: 'x' },
    otherStage: { path: 'basic/stage-2/x.md', text: 'x' },
    otherGate: { path: 'basic/gates/stage-2-lorem.md', text: 'x' },
    skillDir: { path: 'skill/SKILL.md', text: 'x' },
    coreDir: { path: 'core/stage-1/x.md', text: 'x' },
    notMarkdown: { path: 'basic/stage-1/x.txt', text: 'x' },
    nested: { path: 'basic/stage-1/sub/x.md', text: 'x' },
    backslash: { path: 'basic\\stage-1\\x.md', text: 'x' },
    duplicate: { path: 'basic/stage-1/alpha-card.md', text: 'again' },
    notText: { path: 'basic/stage-1/x.md', text: 42 },
    oversize: { path: 'basic/stage-1/x.md', text: 'a'.repeat(MAX_STAGE_FILE_BYTES + 1) },
  }
  for (const [name, file] of Object.entries(bad)) {
    assert.ok(validStageFiles(vclFiles([file]), 1, BASIC_COACH).reason, name)
  }
  assert.equal(validStageFiles(vclFiles([{ path: 'basic/stage-1/x.md', text: 'a'.repeat(MAX_STAGE_FILE_BYTES) }]), 1, BASIC_COACH).files.length, 4)
  assert.equal(validStageFiles({ files: [{ path: 'basic/gates/stage-1-ipsum.md', text: 'gate only' }] }, 1, BASIC_COACH).reason, 'no_cards')
  assert.equal(validStageFiles({ files: Array.from({ length: 33 }, (_, i) => ({ path: `basic/stage-1/c${i}.md`, text: 'x' })) }, 1, BASIC_COACH).reason, 'too_many_files')

  vcl.serveStageContent(vclFiles([bad.traversal]))
  const { outcome, calls, materials } = await runBasic()
  assert.deepEqual([outcome.status, outcome.error], ['failed', 'content_unavailable'])
  assert.equal(calls.length, 0)
  assert.equal(materials.reason, 'bad_path')
})

test('an Advanced session gets its core cards and gate from VCL, in the Advanced workdir layout', async () => {
  const { spawnImpl, calls } = fakeClaude({ reply: 'ok', ledger_writes: [] })
  const b = body({ skill: 'founder-os-stage-1-alpha' })
  const outcome = await executeSession(await prepareSession(b, deps()), deps({ spawnImpl, log: () => {} }))
  assert.equal(outcome.status, 'completed')
  assert.deepEqual(calls[0].files, ['cards/dolor-card.md', 'gate-stage-1-alpha.md', 'skill/SKILL.md'])
  assert.deepEqual(vcl.requests.filter(r => r.url.startsWith('/content/stage')).map(r => r.url), ['/content/stage?stage=1'])
})

test('Advanced and Basic paths never cross: each track only takes its own files', () => {
  const advanced = { files: [{ path: 'core/stage-1/x.md', text: 'x' }, { path: 'gates/stage-1-alpha.md', text: 'g' }] }
  assert.deepEqual(validStageFiles(advanced, 1, ADVANCED).files.map(f => f.path), ['cards/x.md', 'gate-stage-1-alpha.md'])
  assert.equal(validStageFiles(advanced, 1, BASIC_COACH).reason, 'bad_path')
  assert.equal(validStageFiles(vclFiles(), 1, ADVANCED).reason, 'bad_path')
  for (const path of ['core/stage-2/x.md', 'gates/stage-2-alpha.md', 'core/stage-1/../x.md', 'gates/x.md', 'cards/x.md']) {
    assert.ok(validStageFiles({ files: [{ path: 'core/stage-1/ok.md', text: 'x' }, { path, text: 'x' }] }, 1, ADVANCED).reason, path)
  }
})

// --- images -------------------------------------------------------------------

test('an image submission is downloaded into the workdir and its path is in the prompt', async () => {
  const art = imageArtifact()
  const { fetchImpl, blobCalls } = routedFetch(serve(PNG))
  const { spawnImpl, calls } = fakeClaude({ reply: 'Nice screenshot.', ledger_writes: [] })
  const d = deps({ spawnImpl, fetchImpl })
  const outcome = await executeSession(await prepareSession(body({ artifact_refs: [art.id] }), d), d)

  assert.equal(outcome.status, 'completed')
  assert.equal(blobCalls.length, 1)
  assert.equal(blobCalls[0].url, BLOB_URL)
  assert.equal(blobCalls[0].init.redirect, 'error')
  assert.equal(blobCalls[0].init.headers, undefined, 'the blob fetch must carry no headers')
  assert.ok(calls[0].files.includes('attachments/ref-1.png'))
  assert.deepEqual(calls[0].contents['attachments/ref-1.png'], PNG)

  const workdir = calls[0].opts.cwd
  const prompt = calls[0].stdin
  const expected = join(workdir, 'attachments', 'ref-1.png')
  assert.ok(prompt.includes(`"attached_image": ${JSON.stringify(expected)}`), 'prompt names the local image path')
  assert.equal(relative(workdir, expected), 'attachments/ref-1.png')
  assert.ok(!prompt.includes(tokenA))
  assert.deepEqual(readdirSync(workRoot), [], 'the image left with the workdir')
})

test('an image that cannot be fetched does not fail the session; the prompt says why', async () => {
  const art = imageArtifact({ blob_url: 'https://evil.example.com/x.png' })
  const { fetchImpl, blobCalls } = routedFetch(serve(PNG))
  const { spawnImpl, calls } = fakeClaude({ reply: 'Please resend the image.', ledger_writes: [] })
  const d = deps({ spawnImpl, fetchImpl })
  const outcome = await executeSession(await prepareSession(body({ artifact_refs: [art.id] }), d), d)
  assert.equal(outcome.status, 'completed')
  assert.equal(blobCalls.length, 0, 'a host off the allowlist is never fetched')
  assert.ok(!calls[0].files.some(f => f.startsWith('attachments/')))
  assert.ok(calls[0].stdin.includes('"attached_image_error": "host_not_allowed"'))
})

test('PDFs, links and text submissions are left alone', async () => {
  const pdf = imageArtifact({ content_type: 'application/pdf', blob_url: BLOB_URL.replace('.png', '.pdf') })
  const link = vcl.seed('artifacts', A, { stage: 1, kind: 'k', version: 1, body: 'b', meta: { submission_type: 'link', link: 'https://example.com' } })
  const { fetchImpl, blobCalls } = routedFetch(serve(PNG))
  const { spawnImpl, calls } = fakeClaude({ reply: 'ok', ledger_writes: [] })
  const d = deps({ spawnImpl, fetchImpl })
  await executeSession(await prepareSession(body({ artifact_refs: [pdf.id, link.id] }), d), d)
  assert.equal(blobCalls.length, 0)
  assert.ok(!calls[0].stdin.includes('attached_image'))
})

test('fetchImage accepts PNG, JPEG and WebP from a Vercel Blob host', async () => {
  for (const [bytes, type] of [[PNG, 'image/png'], [JPEG, 'image/jpeg'], [WEBP, 'image/webp']]) {
    const got = await fetchImage(BLOB_URL, type, { fetchImpl: serve(bytes, `${type}; charset=binary`) })
    assert.deepEqual(got, bytes)
  }
})

test('fetchImage refuses anything outside the narrow path, before or after the request', async () => {
  const never = async () => assert.fail('must not fetch')
  const refusedBeforeFetch = {
    'http://abc.public.blob.vercel-storage.com/x.png': 'not_https',
    'https://abc.public.blob.vercel-storage.com.evil.com/x.png': 'host_not_allowed',
    'https://public.blob.vercel-storage.com/x.png': 'host_not_allowed',
    'https://a.b.public.blob.vercel-storage.com/x.png': 'host_not_allowed',
    'https://evil.com/abc.public.blob.vercel-storage.com/x.png': 'host_not_allowed',
    'https://user:pw@abc.public.blob.vercel-storage.com/x.png': 'bad_url',
    'https://abc.public.blob.vercel-storage.com:8443/x.png': 'bad_url',
    'file:///etc/passwd': 'not_https',
    'not a url': 'bad_url',
  }
  for (const [url, code] of Object.entries(refusedBeforeFetch)) {
    await assert.rejects(fetchImage(url, 'image/png', { fetchImpl: never }), e => e.code === code, url)
  }
  await assert.rejects(fetchImage(BLOB_URL, 'image/gif', { fetchImpl: never }), e => e.code === 'not_an_image')

  const cases = [
    ['redirect', async () => new Response(null, { status: 302, headers: { location: 'https://evil.com/' } })],
    ['fetch_failed', async () => { throw new TypeError('fetch failed: redirect mode is set to error') }],
    ['http_404', async () => new Response('nope', { status: 404 })],
    ['content_type_mismatch', serve(PNG, 'text/html')],
    ['content_type_mismatch', serve(JPEG, 'image/jpeg')],
    ['bad_image', serve(Buffer.from('<html>not a png at all</html>'), 'image/png')],
    ['too_large', async () => new Response(PNG, { headers: { 'content-type': 'image/png', 'content-length': String(MAX_IMAGE_BYTES + 1) } })],
    ['too_large', serve(Buffer.concat([PNG, Buffer.alloc(MAX_IMAGE_BYTES)]))],
  ]
  for (const [code, fetchImpl] of cases) {
    await assert.rejects(fetchImage(BLOB_URL, 'image/png', { fetchImpl }), e => e.code === code, code)
  }
})

test('fetchImage gives up on a slow host', async () => {
  const hang = (url, init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))))
  await assert.rejects(fetchImage(BLOB_URL, 'image/png', { fetchImpl: hang, timeoutMs: 20 }), e => e.code === 'timeout')
  assert.ok(!existsSync(join(workRoot, 'attachments')))
})
