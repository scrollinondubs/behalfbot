// The model's envelope: which format check failed, the Basic review fallback,
// and what a parse failure reports to VCL.

import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startMockVcl } from './mock-vcl.mjs'
import { prepareSession, executeSession, buildPrompts } from '../session.mjs'
import { EXCERPT_CHARS, EnvelopeError, findVerdict, parseEnvelope, reviewFallback } from '../envelope.mjs'

const PLUGIN_DIR = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'plugin')

const VERDICT = '```founderos-verdict\n{"card_id": "basic-find-where-they-talk", "verdict": "needs_work", "failed": ["Each place shows recent conversation between members, not only announcements or sales posts."]}\n```'
// The skill's own example reply: markdown ending in the verdict block, no envelope.
const SKILL_FORMAT = `Your search words are great. "Bookkeeper", "close the books" and "QBO" are insider talk. Can you find a spot where bookkeepers answer each other instead?\n\n${VERDICT}`

function checkOf(fn) {
  try {
    fn()
  } catch (err) {
    assert.ok(err instanceof EnvelopeError)
    assert.equal(err.code, 'parse_error')
    return err.detail
  }
  assert.fail('expected a parse_error')
}

// --- parseEnvelope -------------------------------------------------------------

test('the envelope with the verdict block inside its reply parses, bare or fenced', () => {
  const env = JSON.stringify({ reply: SKILL_FORMAT, ledger_writes: [] })
  assert.deepEqual(parseEnvelope(env), { reply: SKILL_FORMAT, writes: [] })
  assert.deepEqual(parseEnvelope('```json\n' + env + '\n```'), { reply: SKILL_FORMAT, writes: [] })
  assert.deepEqual(parseEnvelope('Here is my answer {as asked}:\n' + env), { reply: SKILL_FORMAT, writes: [] })
})

test('an envelope with real newlines inside the reply string parses', () => {
  const raw = `{"reply": "Nice list.\n\n${VERDICT.replace(/"/g, '\\"')}", "ledger_writes": []}`
  assert.throws(() => JSON.parse(raw))
  const env = parseEnvelope(raw)
  assert.equal(env.reply, `Nice list.\n\n${VERDICT}`)
  assert.deepEqual(findVerdict(env.reply), { card_id: 'basic-find-where-they-talk', verdict: 'needs_work' })
})

test('each format check that fails is named, with the start of the output', () => {
  assert.equal(checkOf(() => parseEnvelope('Just prose, no braces.')).check, 'no_json')
  assert.equal(checkOf(() => parseEnvelope("{\"reply\": 'single quotes'}")).check, 'invalid_json')
  assert.equal(checkOf(() => parseEnvelope(SKILL_FORMAT)).check, 'no_reply')
  assert.equal(checkOf(() => parseEnvelope('{"reply": "   "}')).check, 'no_reply')
  assert.equal(checkOf(() => parseEnvelope('{"reply": "hi", "ledger_writes": {"method": "x"}}')).check, 'writes_not_list')

  const long = 'x'.repeat(EXCERPT_CHARS + 500)
  const detail = checkOf(() => parseEnvelope(long))
  assert.equal(detail.excerpt, long.slice(0, EXCERPT_CHARS))
  assert.equal(checkOf(() => parseEnvelope(SKILL_FORMAT)).excerpt, SKILL_FORMAT)
})

test('reviewFallback takes text with a verdict VCL can read, and nothing else', () => {
  assert.deepEqual(reviewFallback(SKILL_FORMAT), {
    reply: SKILL_FORMAT,
    writes: [],
    verdict: { card_id: 'basic-find-where-they-talk', verdict: 'needs_work' },
  })
  assert.equal(reviewFallback('Good work, no verdict here.'), null)
  assert.equal(reviewFallback('x\n```founderos-verdict\n{"card_id": "c", "verdict": "maybe"}\n```'), null)
  assert.equal(reviewFallback('x\n```founderos-verdict\n{not json}\n```'), null)
  assert.equal(reviewFallback(`{"reply": "Nice "list".\n\n${VERDICT}", "ledger_writes": []}`), null)
})

// --- executeSession ------------------------------------------------------------

let vcl, A, workRoot

beforeEach(async () => {
  vcl = await startMockVcl()
  A = vcl.addFounder({ stage: 1, name: 'Founder A' })
  workRoot = mkdtempSync(join(tmpdir(), 'fos-envelope-test-'))
})

afterEach(async () => {
  await vcl.close()
  rmSync(workRoot, { recursive: true, force: true })
})

// claude's --output-format json wrapper around `text`, exactly as the model wrote it.
function fakeClaude(text) {
  const spawnImpl = () => {
    const child = new EventEmitter()
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.stdin = new PassThrough()
    child.kill = () => {}
    setImmediate(() => {
      child.stdout.end(JSON.stringify({ result: text }))
      child.emit('close', 0)
    })
    return child
  }
  return spawnImpl
}

function run(skill, text) {
  const logs = []
  const deps = {
    pluginDir: PLUGIN_DIR, workRoot, apiBase: vcl.baseUrl, apiKey: 'sk-ant-test-dedicated-key',
    parentEnv: { PATH: '/usr/bin' }, spawnImpl: fakeClaude(text), log: line => logs.push(JSON.parse(line)),
  }
  const body = { founder_id: A, stage: 1, skill, message: 'Please review my work on: Find where they talk', session_token: vcl.mintToken(A) }
  return prepareSession(body, deps)
    .then(session => executeSession(session, deps))
    .then(outcome => ({ outcome, logs, posted: vcl.sessionResults.at(-1) }))
}

test('a Basic review answered in the skill format completes with the whole text as its reply', async () => {
  const { outcome, logs, posted } = await run('founder-os-basic-review', SKILL_FORMAT)
  assert.equal(outcome.status, 'completed')
  assert.equal(outcome.reply, SKILL_FORMAT)
  assert.deepEqual(outcome.writes, [])
  assert.equal(posted.status, 'completed')
  assert.equal(posted.reply, SKILL_FORMAT)
  const fallback = logs.find(l => l.event === 'founder_os_envelope_fallback')
  assert.equal(fallback.check, 'no_reply')
  assert.equal(fallback.verdict, 'needs_work')
})

test('a malformed output fails with parse_error and reports the check and an excerpt to VCL', async () => {
  const text = 'I looked at the three places. Two are busy, one is a vendor forum.'
  const { outcome, logs, posted } = await run('founder-os-basic-review', text)
  assert.equal(outcome.status, 'failed')
  assert.equal(outcome.error, 'parse_error')
  assert.equal(outcome.error_detail.check, 'no_json')
  assert.equal(outcome.error_detail.excerpt, text)

  assert.equal(posted.status, 'failed')
  assert.match(posted.error, /^parse_error: no_json: /)
  assert.ok(posted.error.includes(text))
  assert.equal(posted.error_detail, undefined)

  const logged = logs.find(l => l.event === 'founder_os_envelope_error')
  assert.equal(logged.check, 'no_json')
  assert.equal(logged.excerpt, text)
})

test('the excerpt VCL gets is capped', async () => {
  const text = 'y'.repeat(5000)
  const { posted } = await run('founder-os-basic-review', text)
  assert.ok(posted.error.includes('y'.repeat(EXCERPT_CHARS)))
  assert.ok(!posted.error.includes('y'.repeat(EXCERPT_CHARS + 1)))
})

test('a coach session never falls back: its writes would be lost', async () => {
  const { outcome, posted } = await run('founder-os-basic-coach', SKILL_FORMAT)
  assert.equal(outcome.status, 'failed')
  assert.equal(outcome.error, 'parse_error')
  assert.equal(outcome.error_detail.check, 'no_reply')
  assert.match(posted.error, /^parse_error: no_reply: /)
})

test('the system prompt puts a skill\'s fenced block inside the reply string', async () => {
  const deps = { pluginDir: PLUGIN_DIR, workRoot, apiBase: vcl.baseUrl }
  const session = await prepareSession(
    { founder_id: A, stage: 1, skill: 'founder-os-basic-review', message: 'x', session_token: vcl.mintToken(A) },
    deps,
  )
  assert.match(buildPrompts(session).systemPrompt, /founderos-verdict\), that block goes at the end of the reply string, inside this JSON object/)
})
