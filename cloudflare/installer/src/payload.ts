// Interface 1 request validation (CONTRACT.md).

export interface CreatePayload {
  source: 'stripe' | 'admin'
  purchase_id: number | null
  customer_name: string | null
  customer_email: string | null
  discord_user_id: string | null
  discord_username: string | null
  variant: string | null
  scheduled_at: number | null
}

const SNOWFLAKE = /^\d{17,20}$/

function optString(v: unknown, max: number): string | null | undefined {
  if (v === undefined || v === null || v === '') return null
  if (typeof v !== 'string' || v.length > max) return undefined
  return v
}

function optUnix(v: unknown): number | null | undefined {
  if (v === undefined || v === null) return null
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 1_000_000_000 || v > 100_000_000_000) return undefined
  return v
}

export function parseCreate(raw: unknown): { ok: true; value: CreatePayload } | { ok: false; error: string } {
  if (!raw || typeof raw !== 'object') return { ok: false, error: 'body must be a JSON object' }
  const r = raw as Record<string, unknown>
  if (r.source !== 'stripe' && r.source !== 'admin') return { ok: false, error: 'source must be stripe or admin' }
  let purchase_id: number | null = null
  if (r.purchase_id !== undefined && r.purchase_id !== null) {
    if (typeof r.purchase_id !== 'number' || !Number.isInteger(r.purchase_id) || r.purchase_id <= 0) {
      return { ok: false, error: 'purchase_id must be a positive integer or null' }
    }
    purchase_id = r.purchase_id
  }
  const customer_name = optString(r.customer_name, 200)
  const customer_email = optString(r.customer_email, 320)
  const discord_username = optString(r.discord_username, 100)
  const variant = optString(r.variant, 64)
  const discord_user_id = optString(r.discord_user_id, 20)
  const scheduled_at = optUnix(r.scheduled_at)
  if (customer_name === undefined) return { ok: false, error: 'customer_name must be a string' }
  if (customer_email === undefined) return { ok: false, error: 'customer_email must be a string' }
  if (discord_username === undefined) return { ok: false, error: 'discord_username must be a string' }
  if (variant === undefined) return { ok: false, error: 'variant must be a string' }
  if (discord_user_id === undefined || (discord_user_id !== null && !SNOWFLAKE.test(discord_user_id))) {
    return { ok: false, error: 'discord_user_id must be a Discord snowflake' }
  }
  if (scheduled_at === undefined) return { ok: false, error: 'scheduled_at must be unix seconds or null' }
  if (r.source === 'stripe' && purchase_id === null) return { ok: false, error: 'stripe installs need purchase_id' }
  if (r.source === 'admin' && discord_user_id === null) return { ok: false, error: 'admin installs need discord_user_id' }
  return {
    ok: true,
    value: { source: r.source, purchase_id, customer_name, customer_email, discord_user_id, discord_username, variant, scheduled_at },
  }
}

export function parsePatch(raw: unknown): { ok: true; scheduled_at: number } | { ok: false; error: string } {
  if (!raw || typeof raw !== 'object') return { ok: false, error: 'body must be a JSON object' }
  const v = optUnix((raw as Record<string, unknown>).scheduled_at)
  if (v === undefined || v === null) return { ok: false, error: 'scheduled_at must be unix seconds' }
  return { ok: true, scheduled_at: v }
}
