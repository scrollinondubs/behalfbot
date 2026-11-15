import { test } from 'node:test'
import assert from 'node:assert/strict'
import { bearerMatches } from '../src/auth.ts'
import { ENV, STRIPE, makeWorld, createInstall } from './helpers.mjs'

test('bearerMatches accepts only the exact secret', async () => {
  assert.equal(await bearerMatches('Bearer s3cret-value', 's3cret-value'), true)
  assert.equal(await bearerMatches('Bearer s3cret-valuX', 's3cret-value'), false)
  assert.equal(await bearerMatches('Bearer s3cret-value-longer', 's3cret-value'), false)
  assert.equal(await bearerMatches('s3cret-value', 's3cret-value'), false)
  assert.equal(await bearerMatches('bearer s3cret-value', 's3cret-value'), false)
  assert.equal(await bearerMatches(null, 's3cret-value'), false)
})

test('an unset or empty configured secret matches nothing, not even an empty bearer', async () => {
  assert.equal(await bearerMatches('Bearer ', ''), false)
  assert.equal(await bearerMatches('Bearer anything', ''), false)
  assert.equal(await bearerMatches('Bearer undefined', undefined), false)
})

test('POST /installs: no bearer, wrong bearer and the admin bearer all get 401', async () => {
  const w = makeWorld()
  for (const token of [undefined, 'wrong', ENV.INSTALLER_ADMIN_SECRET]) {
    const res = await w.request('POST', '/installs', { body: STRIPE, token })
    assert.equal(res.status, 401, `token ${token}`)
  }
  assert.equal(w.discord.calls.length, 0, 'no Discord call before auth')
})

test('PATCH /installs/:id needs the site bearer', async () => {
  const w = makeWorld()
  const { body } = await createInstall(w)
  const path = `/installs/${body.install_id}`
  assert.equal((await w.request('PATCH', path, { body: { scheduled_at: 1800000000 } })).status, 401)
  assert.equal((await w.request('PATCH', path, { body: { scheduled_at: 1800000000 }, token: ENV.INSTALLER_ADMIN_SECRET })).status, 401)
  assert.equal((await w.request('PATCH', path, { body: { scheduled_at: 1800000000 }, token: ENV.INSTALLER_SITE_SECRET })).status, 200)
})

test('GET /installs/:id/journal needs the admin bearer; the site bearer cannot read it', async () => {
  const w = makeWorld()
  const { body } = await createInstall(w)
  const path = `/installs/${body.install_id}/journal`
  assert.equal((await w.request('GET', path)).status, 401)
  assert.equal((await w.request('GET', path, { token: ENV.INSTALLER_SITE_SECRET })).status, 401)
  const ok = await w.request('GET', path, { token: ENV.INSTALLER_ADMIN_SECRET })
  assert.equal(ok.status, 200)
  const j = await ok.json()
  assert.equal(j.install.install_id, body.install_id)
  assert.ok(j.journal.length > 0)
})

test('GET /installs (list) needs the site bearer; the admin bearer and no bearer get 401', async () => {
  const w = makeWorld()
  await createInstall(w)
  assert.equal((await w.request('GET', '/installs?status=open')).status, 401)
  assert.equal((await w.request('GET', '/installs?status=open', { token: 'wrong' })).status, 401)
  assert.equal((await w.request('GET', '/installs?status=open', { token: ENV.INSTALLER_ADMIN_SECRET })).status, 401)
  assert.equal((await w.request('GET', '/installs?status=open', { token: ENV.INSTALLER_SITE_SECRET })).status, 200)
})

test('unknown or malformed install ids are 404 after auth, and never reach a DO', async () => {
  const w = makeWorld()
  const t = { token: ENV.INSTALLER_ADMIN_SECRET }
  assert.equal((await w.request('GET', '/installs/inst_01ARZ3NDEKTSV4RRFFQ69G5FAV/journal', t)).status, 404)
  assert.equal((await w.request('GET', '/installs/../journal', t)).status, 404)
  assert.equal(w.stores.size, 0)
})
