// Cross-install index behind POST /installs idempotency. One singleton DO
// ("registry"). reserve() does its check and insert with no await in between,
// so two concurrent POSTs for the same purchase cannot both get a new id.

import { newInstallId } from './ids.ts'
import { REGISTRY_DDL } from './schema.ts'
import type { SqlLike } from './types.ts'
import type { CreatePayload } from './payload.ts'
import type { InstallListing } from './schema.ts'

export class RegistryCore {
  private readonly sql: SqlLike
  private readonly now: () => number

  constructor(o: { sql: SqlLike; now: () => number }) {
    this.sql = o.sql
    this.now = o.now
    for (const ddl of REGISTRY_DDL) this.sql.exec(ddl)
  }

  // Idempotent on purchase_id (any source), and on discord_user_id for admin
  // installs while one is still open.
  reserve(p: Pick<CreatePayload, 'source' | 'purchase_id' | 'discord_user_id' | 'customer_name' | 'scheduled_at'>): { install_id: string; created: boolean } {
    if (p.purchase_id !== null) {
      const hit = this.sql
        .exec('SELECT install_id FROM install_index WHERE purchase_id = ? ORDER BY created_at LIMIT 1', p.purchase_id)
        .toArray()[0] as { install_id: string } | undefined
      if (hit) return { install_id: hit.install_id, created: false }
    }
    if (p.source === 'admin' && p.discord_user_id) {
      const hit = this.sql
        .exec(
          "SELECT install_id FROM install_index WHERE discord_user_id = ? AND source = 'admin' AND open = 1 ORDER BY created_at LIMIT 1",
          p.discord_user_id,
        )
        .toArray()[0] as { install_id: string } | undefined
      if (hit) return { install_id: hit.install_id, created: false }
    }
    const nowMs = this.now()
    const install_id = newInstallId(nowMs)
    this.sql.exec(
      `INSERT INTO install_index (install_id, source, purchase_id, discord_user_id, open, created_at, customer_name, status, scheduled_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)`,
      install_id,
      p.source,
      p.purchase_id,
      p.discord_user_id,
      Math.floor(nowMs / 1000),
      p.customer_name,
      p.scheduled_at === null ? 'homework' : 'scheduled',
      p.scheduled_at,
    )
    return { install_id, created: true }
  }

  exists(installId: string): boolean {
    return this.sql.exec('SELECT 1 AS x FROM install_index WHERE install_id = ?', installId).toArray().length > 0
  }

  // Called by an InstallDO after its row changes. A closed install frees its
  // admin idempotency key.
  sync(s: InstallListing): void {
    this.sql.exec(
      'UPDATE install_index SET customer_name = ?, discord_user_id = ?, channel_id = ?, status = ?, scheduled_at = ?, open = ? WHERE install_id = ?',
      s.customer_name,
      s.discord_user_id,
      s.channel_id,
      s.status,
      s.scheduled_at,
      s.status === 'closed' ? 0 : 1,
      s.install_id,
    )
  }

  // filter: 'open' (anything not closed), 'all', or one exact status.
  list(filter: string): InstallListing[] {
    const cols = 'install_id, source, purchase_id, customer_name, discord_user_id, channel_id, status, scheduled_at'
    if (filter === 'all') return this.sql.exec(`SELECT ${cols} FROM install_index ORDER BY created_at`).toArray() as InstallListing[]
    if (filter === 'open') return this.sql.exec(`SELECT ${cols} FROM install_index WHERE open = 1 ORDER BY created_at`).toArray() as InstallListing[]
    return this.sql.exec(`SELECT ${cols} FROM install_index WHERE status = ? ORDER BY created_at`, filter).toArray() as InstallListing[]
  }
}
