// Pure timing and cap rules. All times are unix seconds, matching the
// schema's scheduled_at. The alarm code turns the `next` values into ms.

export const MINUTE = 60
export const HOUR = 60 * MINUTE
export const DAY = 24 * HOUR

// Start rule (hard requirement 6).
export const RECHECK_EVERY = 15 * MINUTE
export const START_GRACE = 2 * HOUR

// GC (hard requirement 7). Warn at day 11, close at day 14, and never close
// sooner than WARN_NOTICE after a warning actually went out.
export const GC_WARN_AFTER = 11 * DAY
export const GC_CLOSE_AFTER = 14 * DAY
export const WARN_NOTICE = GC_CLOSE_AFTER - GC_WARN_AFTER

// Caps (hard requirement 4).
export const SPEND_CAP_CENTS = 4000
export const WALL_CLOCK_CAP = 4 * HOUR

export type HomeworkState = 'pending' | 'green'

export type StartDecision =
  | { action: 'wait_slot'; next: number }
  | { action: 'start' }
  | { action: 'recheck'; next: number }
  | { action: 'escalate' }

export function allGreen(homework: HomeworkState[]): boolean {
  // An install with no homework rows was never seeded; that is not "done".
  return homework.length > 0 && homework.every((s) => s === 'green')
}

export function startDecision(p: { now: number; scheduledAt: number; homework: HomeworkState[] }): StartDecision {
  if (p.now < p.scheduledAt) return { action: 'wait_slot', next: p.scheduledAt }
  if (allGreen(p.homework)) return { action: 'start' }
  if (p.now >= p.scheduledAt + START_GRACE) return { action: 'escalate' }
  return { action: 'recheck', next: Math.min(p.now + RECHECK_EVERY, p.scheduledAt + START_GRACE) }
}

export type GcDecision =
  | { action: 'none'; next: number }
  | { action: 'warn'; closeAt: number; next: number }
  | { action: 'close' }

// base: the later of the customer's last message and the moment the install
// became eligible for GC. A warning older than base is stale (the customer
// has spoken since), so it no longer counts.
export function gcDecision(p: { now: number; base: number; warnedAt: number | null }): GcDecision {
  const warned = p.warnedAt !== null && p.warnedAt >= p.base
  const warnAt = p.base + GC_WARN_AFTER
  const closeAt = warned ? Math.max(p.base + GC_CLOSE_AFTER, (p.warnedAt as number) + WARN_NOTICE) : p.base + GC_CLOSE_AFTER
  if (warned) {
    if (p.now >= closeAt) return { action: 'close' }
    return { action: 'none', next: closeAt }
  }
  if (p.now >= warnAt) {
    const at = Math.max(closeAt, p.now + WARN_NOTICE)
    return { action: 'warn', closeAt: at, next: at }
  }
  return { action: 'none', next: warnAt }
}

export function gcBase(lastCustomerMsgAt: number | null, eligibleSince: number): number {
  return Math.max(lastCustomerMsgAt ?? 0, eligibleSince)
}

export type CapBreach = 'spend' | 'wall_clock' | null

export function capBreach(p: {
  spendCents: number
  runStartedAt: number | null
  now: number
  spendCapCents?: number
  wallClockCap?: number
}): CapBreach {
  if (p.spendCents >= (p.spendCapCents ?? SPEND_CAP_CENTS)) return 'spend'
  if (p.runStartedAt !== null && p.now - p.runStartedAt >= (p.wallClockCap ?? WALL_CLOCK_CAP)) return 'wall_clock'
  return null
}
