// Does a finished pre-scan warrant an immediate build?
//
// Getting this wrong is expensive in both directions: a false positive spends
// tokens on a build nobody asked for, a false negative leaves an ask sitting
// until the next scheduled window - which is the bug this whole change exists
// to fix. So every case here is a real log line, not an invented one.
//
// Run: node container/prescan-outcome.test.mjs

import assert from 'node:assert/strict'
import { test } from 'node:test'
import { preScanPromotedARow } from './prescan-outcome.mjs'

// Verbatim from the Todo Cow run, 2026-08-18 20:51Z - the first pre-scan that
// ever promoted a row on the Cloudflare path.
const READY_TAIL =
  '[behalfbot-prescan] scrubbed 1 claude artifact(s) from scrollinondubs/todo-cow workdir\n' +
  '{"ranAt":1787086154,"swept":[],"inFlight":0,"picked":"tit0psdaxq487swgggfc7trw",' +
  '"outcome":{"kind":"ready","queueRowId":"tit0psdaxq487swgggfc7trw","plan":"Scaffold a fresh Next.js 14 App Router"}}\n'

// Verbatim from an idle tick the same evening.
const IDLE_TAIL =
  '{"ranAt":1787085910,"swept":[],"inFlight":0,"picked":null,"outcome":null,"skippedReason":"no_pending_plan_rows"}'

test('a promoted row chains to a build', () => {
  assert.equal(preScanPromotedARow(READY_TAIL), true)
})

test('an idle tick does not', () => {
  assert.equal(preScanPromotedARow(IDLE_TAIL), false)
})

test('a clarification does not - the ask is waiting on a human', () => {
  // This is the July stall and the first Todo Cow attempt. Building here would
  // spend the budget answering a question nobody has answered yet.
  assert.equal(
    preScanPromotedARow('{"ranAt":1,"picked":"abc","outcome":{"kind":"clarification","question":"which repo?"}}'),
    false
  )
})

test('unparseable output never chains', () => {
  // Fail closed: a change to the summary line degrades to the old behaviour
  // (wait for the next poke) rather than firing builds nobody asked for.
  assert.equal(preScanPromotedARow(''), false)
  assert.equal(preScanPromotedARow(undefined), false)
  assert.equal(preScanPromotedARow(null), false)
  assert.equal(preScanPromotedARow('Error: boom\n    at foo\n'), false)
  assert.equal(preScanPromotedARow('{"ranAt":1,"outcome":{"kind":"rea'), false)
})

test('the word ready in prose does not chain', () => {
  // The plan text itself routinely contains "ready". Only the parsed field counts.
  assert.equal(preScanPromotedARow('the plan is ready to go'), false)
  assert.equal(
    preScanPromotedARow('{"ranAt":1,"outcome":{"kind":"clarification","question":"is it ready?"}}'),
    false
  )
})

test('finds the summary line among surrounding noise', () => {
  assert.equal(preScanPromotedARow('warn: something\n' + READY_TAIL + 'trailing noise\n'), true)
})
