import { test } from 'node:test'
import assert from 'node:assert/strict'
import { checkQueue, gatedTrigger, pipelineUrl } from '../src/queue-gate.ts'

const db = { url: 'libsql://queue-example.turso.io', authToken: 'test-token' }

const tursoSays = hasWork => {
  const calls = []
  const fetchImpl = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) })
    return Response.json({
      baton: null,
      base_url: null,
      results: [
        {
          type: 'ok',
          response: {
            type: 'execute',
            result: {
              cols: [{ name: 'has_work', decltype: null }],
              rows: [[{ type: 'integer', value: hasWork ? '1' : '0' }]],
              affected_row_count: 0,
              last_insert_rowid: null,
            },
          },
        },
        { type: 'ok', response: { type: 'close' } },
      ],
    })
  }
  return { fetchImpl, calls }
}

const fakeContainer = () => {
  const wakes = { count: 0 }
  const wake = async () => {
    wakes.count++
    return Response.json({ started: true }, { status: 202 })
  }
  return { wake, wakes }
}

test('empty queue: answers 200 skipped and never wakes the container', async () => {
  const { fetchImpl, calls } = tursoSays(false)
  const { wake, wakes } = fakeContainer()
  const res = await gatedTrigger(db, 'executor', wake, fetchImpl)
  assert.equal(res.status, 200)
  assert.deepEqual(await res.json(), { ok: true, skipped: 'queue_empty', mode: 'executor' })
  assert.equal(wakes.count, 0)
  assert.equal(calls.length, 1)
})

test('pending rows: wakes the container and returns its answer', async () => {
  const { fetchImpl } = tursoSays(true)
  const { wake, wakes } = fakeContainer()
  const res = await gatedTrigger(db, 'executor', wake, fetchImpl)
  assert.equal(res.status, 202)
  assert.equal(wakes.count, 1)
})

test('processing rows count as work so the timeout sweep still runs', async () => {
  const { fetchImpl, calls } = tursoSays(true)
  const { wake, wakes } = fakeContainer()
  await gatedTrigger(db, 'prescan', wake, fetchImpl)
  assert.equal(wakes.count, 1)
  assert.match(calls[0].body.requests[0].stmt.sql, /status IN \(\?, 'processing'\)/)
})

test('each mode asks about the status its heartbeat script picks', async () => {
  for (const [mode, status] of [['executor', 'pending_execution'], ['prescan', 'pending_plan']]) {
    const { fetchImpl, calls } = tursoSays(false)
    await checkQueue(db, mode, fetchImpl)
    const stmt = calls[0].body.requests[0].stmt
    assert.match(stmt.sql, /FROM behalfbot_queue/)
    assert.deepEqual(stmt.args, [{ type: 'text', value: status }])
    assert.equal(calls[0].url, 'https://queue-example.turso.io/v2/pipeline')
    assert.equal(calls[0].init.headers.authorization, 'Bearer test-token')
  }
})

test('DB error: fails open and wakes the container', async () => {
  const { wake, wakes } = fakeContainer()
  const res = await gatedTrigger(db, 'executor', wake, async () => { throw new Error('network down') })
  assert.equal(res.status, 202)
  assert.equal(wakes.count, 1)
})

test('non-2xx from Turso fails open', async () => {
  const { wake, wakes } = fakeContainer()
  await gatedTrigger(db, 'executor', wake, async () => new Response('nope', { status: 500 }))
  assert.equal(wakes.count, 1)
})

test('a statement error result fails open', async () => {
  const { wake, wakes } = fakeContainer()
  const fetchImpl = async () => Response.json({ results: [{ type: 'error', error: { message: 'no such table' } }] })
  await gatedTrigger(db, 'executor', wake, fetchImpl)
  assert.equal(wakes.count, 1)
})

test('a malformed response fails open', async () => {
  const { wake, wakes } = fakeContainer()
  const fetchImpl = async () => Response.json({ results: [{ type: 'ok', response: { type: 'execute', result: { rows: [] } } }] })
  await gatedTrigger(db, 'executor', wake, fetchImpl)
  assert.equal(wakes.count, 1)
})

test('a slow DB times out and fails open', async () => {
  const { wake, wakes } = fakeContainer()
  const hang = (_url, init) => new Promise((_, reject) => {
    init.signal.addEventListener('abort', () => reject(init.signal.reason))
  })
  const started = Date.now()
  await gatedTrigger(db, 'executor', wake, hang, 50)
  assert.equal(wakes.count, 1)
  assert.ok(Date.now() - started < 2000)
})

test('missing credentials fail open without calling Turso', async () => {
  const { wake, wakes } = fakeContainer()
  let called = false
  await gatedTrigger({ url: undefined, authToken: undefined }, 'executor', wake, async () => { called = true })
  assert.equal(called, false)
  assert.equal(wakes.count, 1)
})

test('pipelineUrl maps libsql and https URLs and rejects file URLs', () => {
  assert.equal(pipelineUrl('libsql://a.turso.io'), 'https://a.turso.io/v2/pipeline')
  assert.equal(pipelineUrl('https://a.turso.io'), 'https://a.turso.io/v2/pipeline')
  assert.equal(pipelineUrl('libsql://a.turso.io?tls=1'), 'https://a.turso.io/v2/pipeline')
  assert.throws(() => pipelineUrl('file:local.db'))
})
