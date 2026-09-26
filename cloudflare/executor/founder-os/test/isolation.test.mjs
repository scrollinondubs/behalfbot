// Tenant isolation for FounderOS sessions (behalfbot#215).
//
// Two founders, A and B, live in one mock VCL. Every test runs a session as A
// and then checks two things: B's rows are untouched, and no request the
// executor sent ever contained B's founder_id or asked for one of B's rows by
// a path that could succeed. The mock implements the contract in
// docs/founder-os-ledger-api.md, so these cases are also the port list for
// vibecodelisboa#508.

import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import { mkdtempSync, rmSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startMockVcl } from './mock-vcl.mjs'
import { createLedgerClient, LedgerApiError } from '../ledger-client.mjs'
import { prepareSession, executeSession, SessionError, DISALLOWED_TOOLS } from '../session.mjs'
import { createFounderOsServer } from '../server.mjs'

const PLUGIN_DIR = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'plugin')
const API_KEY = 'sk-ant-test-dedicated-key'

let vcl, A, B, tokenA, workRoot, bArtifact, bInterview, aArtifact

beforeEach(async () => {
  vcl = await startMockVcl()
  A = vcl.addFounder({ stage: 1, name: 'Founder A' })
  B = vcl.addFounder({ stage: 1, name: 'Founder B' })
  aArtifact = vcl.seed('artifacts', A, { stage: 1, kind: 'audience', version: 1, body: 'A audience', meta: {} })
  bArtifact = vcl.seed('artifacts', B, { stage: 1, kind: 'audience', version: 1, body: 'B SECRET audience', meta: {} })
  bInterview = vcl.seed('interviews', B, { interviewee: 'p1', notes: 'B SECRET notes', commitment: 'none', earlyvangelist: false })
  tokenA = vcl.mintToken(A)
  workRoot = mkdtempSync(join(tmpdir(), 'fos-test-'))
})

afterEach(async () => {
  await vcl.close()
  rmSync(workRoot, { recursive: true, force: true })
})

// A spawn stand-in that records exactly what claude would have been given
// and answers with a canned `claude -p --output-format json` envelope.
function fakeClaude(envelope) {
  const calls = []
  const spawnImpl = (bin, args, opts) => {
    calls.push({ bin, args, opts })
    const child = new EventEmitter()
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => {}
    setImmediate(() => {
      const result = typeof envelope === 'string' ? envelope : JSON.stringify(envelope)
      child.stdout.end(JSON.stringify({ result, usage: { input_tokens: 10, output_tokens: 5 } }))
      child.emit('close', 0)
    })
    return child
  }
  return { spawnImpl, calls }
}

function deps(extra = {}) {
  return {
    pluginDir: PLUGIN_DIR,
    workRoot,
    apiBase: vcl.baseUrl,
    apiKey: API_KEY,
    parentEnv: {
      PATH: '/usr/bin',
      HOME: '/home/executor',
      DATABASE_URL: 'libsql://prod.turso.io',
      DATABASE_AUTH_TOKEN: 'turso-secret',
      GITHUB_PAT: 'ghp_secret',
      ENCRYPTION_SECRET: 'enc-secret',
      RESEND_API_KEY: 're_secret',
      BEHALFBOT_ANTHROPIC_API_KEY: API_KEY,
      FOUNDER_OS_VCL_API_BASE: vcl.baseUrl,
    },
    ...extra,
  }
}

function sessionBody(extra = {}) {
  return { founder_id: A, stage: 1, skill: 'founder-os-stage-1-audience', message: 'Who should I build for?', session_token: tokenA, ...extra }
}

function assertNothingAboutB() {
  for (const r of vcl.requests) {
    const blob = `${r.url} ${r.body}`
    assert.ok(!blob.includes(B), `request leaked founder B's id: ${r.method} ${r.url}`)
    assert.ok(!/"founder_id"/.test(r.body), `request body carried a founder_id: ${r.body}`)
  }
}

function snapshotB() {
  return JSON.stringify(Object.keys(vcl.rows).map(t => vcl.rowsFor(t, B)))
}

// --- the client ---------------------------------------------------------------

test('client for A gets 404 on B\'s artifact, the same answer as a nonexistent id', async () => {
  const client = createLedgerClient({ baseUrl: vcl.baseUrl, token: tokenA })
  client.bind(A)
  await assert.rejects(client.getArtifact(bArtifact.id), e => e.status === 404 && e.code === 'not_found')
  await assert.rejects(client.getArtifact('00000000-0000-4000-8000-000000000000'), e => e.status === 404 && e.code === 'not_found')
  const mine = await client.listArtifacts()
  assert.deepEqual(mine.map(r => r.id), [aArtifact.id])
})

test('client refuses to send a founder_id at any depth, before any request goes out', async () => {
  const client = createLedgerClient({ baseUrl: vcl.baseUrl, token: tokenA })
  const before = vcl.requests.length
  await assert.rejects(client.addArtifact({ stage: 1, kind: 'x', body: 'y', founder_id: B }), e => e.code === 'founder_id_not_accepted')
  await assert.rejects(client.addArtifact({ stage: 1, kind: 'x', body: 'y', meta: { nested: [{ founder_id: B }] } }), e => e.code === 'founder_id_not_accepted')
  assert.equal(vcl.requests.length, before)
})

test('client aborts on a response row owned by another founder (tripwire for a VCL bug)', async () => {
  const leakyFetch = async () => new Response(JSON.stringify({ rows: [{ id: bArtifact.id, founder_id: B }] }), { status: 200 })
  const client = createLedgerClient({ baseUrl: 'https://vcl.example', token: tokenA, fetchImpl: leakyFetch })
  client.bind(A)
  await assert.rejects(client.listArtifacts(), e => e.code === 'tenant_mismatch')
})

test('client refuses a non-https ledger base URL', () => {
  assert.throws(() => createLedgerClient({ baseUrl: 'http://vcl.example', token: tokenA }), e => e.code === 'bad_base_url')
})

test('the mock enforces the contract directly: A\'s token on B\'s rows is 404, a founder_id body is 400', async () => {
  const h = { authorization: `Bearer ${tokenA}`, 'content-type': 'application/json' }
  let r = await fetch(`${vcl.baseUrl}/artifacts/${bArtifact.id}`, { headers: h })
  assert.equal(r.status, 404)
  r = await fetch(`${vcl.baseUrl}/audits`, { method: 'POST', headers: h, body: JSON.stringify({ target_table: 'interviews', target_id: bInterview.id, auditor: 'claude', check_name: 'x', verdict: 'pass' }) })
  assert.equal(r.status, 404)
  r = await fetch(`${vcl.baseUrl}/artifacts`, { method: 'POST', headers: h, body: JSON.stringify({ stage: 1, kind: 'x', body: 'y', founder_id: B }) })
  assert.equal(r.status, 400)
  assert.equal(vcl.rowsFor('audits', B).length, 0)
  assert.equal(vcl.rowsFor('artifacts', B).length, 1)
})

// --- preflight ----------------------------------------------------------------

test('a token for A cannot open a session for B', async () => {
  await assert.rejects(
    prepareSession(sessionBody({ founder_id: B }), deps()),
    e => e instanceof SessionError && e.status === 403 && e.code === 'token_founder_mismatch',
  )
  assert.deepEqual(vcl.requests.map(r => r.url), ['/me'])
})

test('an expired token is refused before any context is read', async () => {
  const expired = vcl.mintToken(A, { ttlMs: -1000 })
  await assert.rejects(prepareSession(sessionBody({ session_token: expired }), deps()), e => e.status === 403 && e.code === 'token_expired')
  assert.deepEqual(vcl.requests.map(r => r.url), ['/me'])
})

test('referencing B\'s artifact from A\'s session fails as not found', async () => {
  await assert.rejects(prepareSession(sessionBody({ artifact_refs: [bArtifact.id] }), deps()), e => e.status === 404 && e.code === 'artifact_not_found')
  assertNothingAboutB()
})

test('unknown or malformed skill names answer 404 without touching VCL', async () => {
  for (const skill of ['founder-os-stage-9-nope', '../../etc/passwd', 'founder-os-stage-1-audience/../x', 'Founder-OS']) {
    await assert.rejects(prepareSession(sessionBody({ skill }), deps()), e => e.status === 404 && e.code === 'skill_not_found')
  }
  assert.equal(vcl.requests.length, 0)
})

test('an unpinned plugin refuses every session with 503', async () => {
  const empty = mkdtempSync(join(tmpdir(), 'fos-unpinned-'))
  try {
    await assert.rejects(prepareSession(sessionBody(), deps({ pluginDir: empty })), e => e.status === 503 && e.code === 'plugin_unpinned')
  } finally {
    rmSync(empty, { recursive: true, force: true })
  }
})

test('a stage mismatch is refused: never coach ahead of the ledger', async () => {
  vcl.founders.get(A).current_stage = 2
  await assert.rejects(prepareSession(sessionBody({ stage: 2 }), deps()), e => e.status === 409 && e.code === 'skill_stage_mismatch')
  await assert.rejects(prepareSession(sessionBody({ stage: 1 }), deps()), e => e.status === 409 && e.code === 'stage_mismatch')
})

// --- full sessions ------------------------------------------------------------

test('happy path: writes land in A\'s ledger only, and the reply reaches VCL', async () => {
  const before = snapshotB()
  const { spawnImpl } = fakeClaude({
    reply: 'Your audience is indie bakers.',
    ledger_writes: [
      { method: 'add_artifact', args: { stage: 1, kind: 'audience', body: 'indie bakers' } },
      { method: 'add_pain', args: { quote: 'flour prices are killing me', job: 'cost' } },
    ],
  })
  const session = await prepareSession(sessionBody({ artifact_refs: [aArtifact.id] }), deps())
  const outcome = await executeSession(session, deps({ spawnImpl }))

  assert.equal(outcome.status, 'completed')
  assert.deepEqual(outcome.writes.map(w => w.status), ['applied', 'applied'])
  assert.equal(vcl.rowsFor('artifacts', A).length, 2)
  assert.equal(vcl.rowsFor('pains', A).length, 1)
  assert.equal(snapshotB(), before)
  assert.equal(vcl.sessionResults.length, 1)
  assert.equal(vcl.sessionResults[0].founder_id, A)
  assert.equal(vcl.sessionResults[0].reply, 'Your audience is indie bakers.')
  assertNothingAboutB()
})

test('a skill that names founder B in a write gets the whole envelope rejected, nothing written anywhere', async () => {
  const before = snapshotB()
  const beforeA = vcl.rowsFor('artifacts', A).length
  const { spawnImpl } = fakeClaude({
    reply: 'done',
    ledger_writes: [
      { method: 'add_artifact', args: { stage: 1, kind: 'audience', body: 'legit' } },
      { method: 'add_artifact', args: { stage: 1, kind: 'audience', body: 'sneaky', founder_id: B } },
    ],
  })
  const outcome = await executeSession(await prepareSession(sessionBody(), deps()), deps({ spawnImpl }))
  assert.equal(outcome.status, 'failed')
  assert.equal(outcome.error, 'envelope_rejected')
  assert.equal(vcl.rowsFor('artifacts', A).length, beforeA)
  assert.equal(snapshotB(), before)
  assertNothingAboutB()
})

test('an auditor that targets B\'s interview by row id is refused by VCL and writes nothing', async () => {
  const before = snapshotB()
  const { spawnImpl } = fakeClaude({
    reply: 'audited',
    ledger_writes: [
      { method: 'add_audit', args: { target_table: 'interviews', target_id: bInterview.id, auditor: 'claude', check_name: 'mom-test', verdict: 'fail' } },
      { method: 'record_gate_decision', args: { stage: 1, gate_id: 'stage-1-audience', decision: 'pass', decided_by: 'claude', evidence: [{ table: 'artifacts', id: bArtifact.id }], rationale: 'x' } },
    ],
  })
  const outcome = await executeSession(await prepareSession(sessionBody({ skill: 'founder-os-stage-1-auditor' }), deps()), deps({ spawnImpl }))
  assert.equal(outcome.status, 'completed')
  assert.deepEqual(outcome.writes.map(w => [w.status, w.error]), [['failed', 'not_found'], ['failed', 'not_found']])
  assert.equal(vcl.rowsFor('audits', A).length, 0)
  assert.equal(vcl.rowsFor('gate_decisions', A).length, 0)
  assert.equal(snapshotB(), before)
})

test('an auditor cannot claim Sean\'s sign-off: the executor forces it off', async () => {
  const { spawnImpl } = fakeClaude({
    reply: 'passed',
    ledger_writes: [{ method: 'record_gate_decision', args: { stage: 1, gate_id: 'stage-1-audience', decision: 'pass', decided_by: 'claude+sean', sean_signoff: true, evidence: [{ table: 'artifacts', id: aArtifact.id }], rationale: 'ok' } }],
  })
  const outcome = await executeSession(await prepareSession(sessionBody({ skill: 'founder-os-stage-1-auditor' }), deps()), deps({ spawnImpl }))
  assert.equal(outcome.writes[0].status, 'applied')
  const [decision] = vcl.rowsFor('gate_decisions', A)
  assert.equal(decision.decided_by, 'claude')
  assert.equal(decision.sean_signoff, false)
})

test('a stage skill cannot record a gate decision, and no write may name another stage', async () => {
  for (const write of [
    { method: 'record_gate_decision', args: { stage: 1, gate_id: 'g', decision: 'pass', evidence: [{ table: 'artifacts', id: aArtifact.id }], rationale: 'x' } },
    { method: 'add_artifact', args: { stage: 4, kind: 'sprint', body: 'skip ahead' } },
    { method: 'export_corrected_labels', args: {} },
    { method: 'delete_founder', args: {} },
  ]) {
    const { spawnImpl } = fakeClaude({ reply: 'x', ledger_writes: [write] })
    const outcome = await executeSession(await prepareSession(sessionBody({ session_token: vcl.mintToken(A) }), deps()), deps({ spawnImpl }))
    assert.equal(outcome.error, 'envelope_rejected', write.method)
  }
  assert.equal(vcl.rowsFor('gate_decisions', A).length, 0)
  assert.equal(vcl.rowsFor('artifacts', A).length, 1)
})

test('claude runs with no credentials, no network tools, and never sees the session token', async () => {
  const { spawnImpl, calls } = fakeClaude({ reply: 'hi', ledger_writes: [] })
  await executeSession(await prepareSession(sessionBody(), deps()), deps({ spawnImpl }))
  assert.equal(calls.length, 1)
  const { args, opts } = calls[0]

  assert.deepEqual(Object.keys(opts.env).sort(), ['ANTHROPIC_API_KEY', 'HOME', 'PATH'])
  assert.equal(opts.env.ANTHROPIC_API_KEY, API_KEY)
  const everything = JSON.stringify({ args, env: opts.env })
  for (const secret of ['turso-secret', 'ghp_secret', 'enc-secret', 're_secret', 'libsql://', tokenA, vcl.baseUrl]) {
    assert.ok(!everything.includes(secret), `claude was given ${secret}`)
  }
  assert.ok(args.includes('--bare'))
  assert.equal(args[args.indexOf('--allowedTools') + 1], 'Read,Glob,Grep')
  const denied = args[args.indexOf('--disallowedTools') + 1].split(',')
  for (const tool of ['Bash', 'Write', 'Edit', 'WebFetch', 'WebSearch', 'Task', 'mcp__*']) assert.ok(denied.includes(tool), tool)
  assert.equal(args[args.indexOf('--disallowedTools') + 1], DISALLOWED_TOOLS)
  assert.equal(opts.cwd, args[args.indexOf('--add-dir') + 1])
  assert.ok(!args.join(' ').includes('B SECRET'), 'B\'s data reached the prompt')
  assert.deepEqual(readdirSync(workRoot), [], 'the session workdir was not cleaned up')
})

// --- the shim -----------------------------------------------------------------

test('shim: one session in flight per instance, and an instance stays bound to its first founder', async () => {
  let release
  const gate = new Promise(r => (release = r))
  const spawnImpl = (bin, args, opts) => {
    const child = new EventEmitter()
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => {}
    gate.then(() => {
      child.stdout.end(JSON.stringify({ result: JSON.stringify({ reply: 'ok', ledger_writes: [] }) }))
      child.emit('close', 0)
    })
    return child
  }
  const { server, settled } = createFounderOsServer(deps({ spawnImpl }))
  await new Promise(r => server.listen(0, '127.0.0.1', r))
  const url = `http://127.0.0.1:${server.address().port}/session`
  const post = body => fetch(url, { method: 'POST', body: JSON.stringify(body) }).then(async r => ({ status: r.status, body: await r.json() }))
  try {
    const first = await post(sessionBody())
    assert.equal(first.status, 202)
    const second = await post(sessionBody({ session_token: vcl.mintToken(A) }))
    assert.equal(second.status, 409)
    assert.equal(second.body.error, 'session_in_flight')
    release()
    await settled()
    const other = await post(sessionBody({ founder_id: B, session_token: vcl.mintToken(B) }))
    assert.equal(other.status, 409)
    assert.equal(other.body.error, 'instance_bound_to_other_founder')
    const bad = await post({ founder_id: A })
    assert.equal(bad.status, 400)
  } finally {
    server.close()
  }
})
