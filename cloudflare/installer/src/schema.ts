// Interface 2 (CONTRACT.md), verbatim. Timestamps are unix seconds.
// Per-install state that has no column here (the install's SSH private key,
// the Hetzner token, run clock, GC anchor, request origin) lives in the DO's
// key-value storage instead, so these three tables stay exactly as agreed.

export const INSTALL_DDL = [
  `CREATE TABLE IF NOT EXISTS install (
    install_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    purchase_id INTEGER,
    customer_name TEXT, customer_email TEXT,
    discord_user_id TEXT, channel_id TEXT,
    status TEXT NOT NULL,
    scheduled_at INTEGER,
    server_ip TEXT, host_key_fp TEXT,
    spend_usd_cents INTEGER NOT NULL DEFAULT 0,
    last_customer_msg_at INTEGER,
    close_warned_at INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS homework (
    install_id TEXT, item TEXT,
    state TEXT NOT NULL,
    updated_at INTEGER,
    PRIMARY KEY (install_id, item)
  )`,
  `CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    install_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    exit_code INTEGER,
    cost_usd_cents INTEGER,
    at INTEGER NOT NULL
  )`,
]

// Lives in the singleton InstallRegistry DO, not in any InstallDO. It exists
// so POST /installs can be idempotent across installs and GET /installs can
// list them. The listing columns are a copy each InstallDO pushes after it
// changes; the InstallDO's own install row stays the source of truth.
export const REGISTRY_DDL = [
  `CREATE TABLE IF NOT EXISTS install_index (
    install_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    purchase_id INTEGER,
    discord_user_id TEXT,
    open INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    customer_name TEXT,
    channel_id TEXT,
    status TEXT,
    scheduled_at INTEGER
  )`,
  `CREATE INDEX IF NOT EXISTS install_index_purchase ON install_index (purchase_id)`,
  `CREATE INDEX IF NOT EXISTS install_index_admin_user ON install_index (discord_user_id, source, open)`,
]

export type InstallStatus = 'homework' | 'scheduled' | 'running' | 'needs_human' | 'done' | 'failed' | 'closed'

export interface InstallRow {
  install_id: string
  source: 'stripe' | 'admin'
  purchase_id: number | null
  customer_name: string | null
  customer_email: string | null
  discord_user_id: string | null
  channel_id: string | null
  status: InstallStatus
  scheduled_at: number | null
  server_ip: string | null
  host_key_fp: string | null
  spend_usd_cents: number
  last_customer_msg_at: number | null
  close_warned_at: number | null
  created_at: number
  updated_at: number
}

// GET /installs row (CONTRACT.md Interface 1 addendum).
export type InstallListing = Pick<
  InstallRow,
  'install_id' | 'source' | 'purchase_id' | 'customer_name' | 'discord_user_id' | 'channel_id' | 'status' | 'scheduled_at'
>

export const INSTALL_STATUSES: InstallStatus[] = ['homework', 'scheduled', 'running', 'needs_human', 'done', 'failed', 'closed']

export type JournalKind = 'cmd' | 'result' | 'note' | 'escalation' | 'customer_wait'

export const HOMEWORK_ITEMS = ['discord_bot', 'claude_plan', 'hetzner_token', 'openai_key', 'agent_gmail'] as const

export const HOMEWORK_LABELS: Record<string, string> = {
  discord_bot: 'Discord bot created',
  claude_plan: 'Claude plan active',
  hetzner_token: 'Hetzner project token',
  openai_key: 'OpenAI API key',
  agent_gmail: 'Agent Gmail account',
}
