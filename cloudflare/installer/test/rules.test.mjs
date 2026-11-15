import { test } from 'node:test'
import assert from 'node:assert/strict'
import { gcDecision, startDecision, capBreach, allGreen, DAY, HOUR, MINUTE } from '../src/rules.ts'

const T0 = 1_800_000_000

test('gc: nothing before day 11, warn at day 11, close at day 14', () => {
  assert.deepEqual(gcDecision({ now: T0 + 10 * DAY, base: T0, warnedAt: null }), { action: 'none', next: T0 + 11 * DAY })
  const warn = gcDecision({ now: T0 + 11 * DAY, base: T0, warnedAt: null })
  assert.equal(warn.action, 'warn')
  assert.equal(warn.closeAt, T0 + 14 * DAY)
  const warnedAt = T0 + 11 * DAY
  assert.deepEqual(gcDecision({ now: T0 + 13 * DAY, base: T0, warnedAt }), { action: 'none', next: T0 + 14 * DAY })
  assert.deepEqual(gcDecision({ now: T0 + 14 * DAY, base: T0, warnedAt }), { action: 'close' })
})

test('gc: a customer message after the warning resets the clock', () => {
  const warnedAt = T0 + 11 * DAY
  const msg = T0 + 12 * DAY
  // The warning is older than the new base, so it no longer counts.
  assert.deepEqual(gcDecision({ now: T0 + 14 * DAY, base: msg, warnedAt }), { action: 'none', next: msg + 11 * DAY })
  assert.equal(gcDecision({ now: msg + 11 * DAY, base: msg, warnedAt }).action, 'warn')
  assert.equal(gcDecision({ now: msg + 14 * DAY, base: msg, warnedAt: msg + 11 * DAY }).action, 'close')
})

test('gc: never closes without a warning at least 3 days earlier, even if the alarm ran late', () => {
  const late = gcDecision({ now: T0 + 20 * DAY, base: T0, warnedAt: null })
  assert.equal(late.action, 'warn')
  assert.equal(late.closeAt, T0 + 23 * DAY)
  assert.deepEqual(gcDecision({ now: T0 + 22 * DAY, base: T0, warnedAt: T0 + 20 * DAY }), { action: 'none', next: T0 + 23 * DAY })
  assert.deepEqual(gcDecision({ now: T0 + 23 * DAY, base: T0, warnedAt: T0 + 20 * DAY }), { action: 'close' })
})

test('start rule: wait for the slot, start when all green, else recheck every 15 min for 2h then escalate', () => {
  const green = ['green', 'green']
  const mixed = ['green', 'pending']
  assert.deepEqual(startDecision({ now: T0 - 1, scheduledAt: T0, homework: green }), { action: 'wait_slot', next: T0 })
  assert.deepEqual(startDecision({ now: T0, scheduledAt: T0, homework: green }), { action: 'start' })
  assert.deepEqual(startDecision({ now: T0, scheduledAt: T0, homework: mixed }), { action: 'recheck', next: T0 + 15 * MINUTE })
  assert.deepEqual(startDecision({ now: T0 + 105 * MINUTE, scheduledAt: T0, homework: mixed }), { action: 'recheck', next: T0 + 2 * HOUR })
  assert.deepEqual(startDecision({ now: T0 + 2 * HOUR, scheduledAt: T0, homework: mixed }), { action: 'escalate' })
  // Green late in the window still starts.
  assert.deepEqual(startDecision({ now: T0 + 90 * MINUTE, scheduledAt: T0, homework: green }), { action: 'start' })
})

test('start rule: no homework rows is not green', () => {
  assert.equal(allGreen([]), false)
  assert.equal(startDecision({ now: T0, scheduledAt: T0, homework: [] }).action, 'recheck')
})

test('caps: $40 spend or 4h wall clock', () => {
  assert.equal(capBreach({ spendCents: 3999, runStartedAt: T0, now: T0 + HOUR }), null)
  assert.equal(capBreach({ spendCents: 4000, runStartedAt: T0, now: T0 + HOUR }), 'spend')
  assert.equal(capBreach({ spendCents: 0, runStartedAt: T0, now: T0 + 4 * HOUR }), 'wall_clock')
  assert.equal(capBreach({ spendCents: 0, runStartedAt: null, now: T0 + 10 * HOUR }), null)
})
