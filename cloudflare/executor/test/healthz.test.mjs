import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deepHealthz } from '../src/healthz.ts'

const worker = { appCommit: '1531dcd', builtAt: '2026-10-22T11:20:47Z' }
const containerSays = body => async () => Response.json(body)

test('in sync when the container booted the image the Worker was deployed with', async () => {
  const res = await deepHealthz(worker, containerSays({ ok: true, inFlight: false, appCommit: '1531dcd', builtAt: '2026-10-22T11:20:47Z' }))
  assert.equal(res.status, 200)
  const body = await res.json()
  assert.equal(body.inSync, true)
  assert.deepEqual(body.container, { appCommit: '1531dcd', builtAt: '2026-10-22T11:20:47Z', inFlight: false })
  assert.equal(body.appCommit, '1531dcd')
})

test('partial deploy: new Worker vars over the old image reads as out of sync', async () => {
  const res = await deepHealthz(worker, containerSays({ ok: true, inFlight: true, appCommit: '28c5b24', builtAt: '2026-10-22T09:00:00Z' }))
  const body = await res.json()
  assert.equal(res.status, 200)
  assert.equal(body.inSync, false)
  assert.equal(body.container.appCommit, '28c5b24')
  assert.equal(body.worker.appCommit, '1531dcd')
})

test('container with no build-info is never in sync', async () => {
  const body = await (await deepHealthz(worker, containerSays({ ok: true, inFlight: false, appCommit: null, builtAt: null }))).json()
  assert.equal(body.inSync, false)
  assert.equal(body.container.appCommit, null)
})

test('Worker with no APP_COMMIT var is never in sync, even against a null container', async () => {
  const body = await (await deepHealthz({ appCommit: null, builtAt: null }, containerSays({ appCommit: null }))).json()
  assert.equal(body.inSync, false)
})

test('a container that cannot be reached answers 503 with the reason', async () => {
  const res = await deepHealthz(worker, async () => { throw new Error('container failed to start') })
  assert.equal(res.status, 503)
  const body = await res.json()
  assert.equal(body.ok, false)
  assert.equal(body.container, null)
  assert.equal(body.containerError, 'container failed to start')
  assert.equal(body.inSync, false)
})

test('a non-2xx container answer is an error, not an identity', async () => {
  const res = await deepHealthz(worker, async () => new Response('nope', { status: 500 }))
  assert.equal(res.status, 503)
  assert.match((await res.json()).containerError, /HTTP 500/)
})
