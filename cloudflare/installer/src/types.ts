// Storage seams. InstallCore and RegistryCore only see these shapes, so the
// node tests can back them with node:sqlite and a Map while production hands
// in ctx.storage.sql and ctx.storage.

export interface SqlLike {
  exec(query: string, ...bindings: unknown[]): { toArray(): unknown[] }
}

export interface KvLike {
  get<T = unknown>(key: string): Promise<T | undefined>
  put(key: string, value: unknown): Promise<void>
  delete(key: string): Promise<boolean>
}

export type FetchLike = (input: Request) => Promise<Response>

// The installer secret set is closed (CONTRACT.md "Installer Worker secret
// set"). Customer secrets are per-install DO storage, never Worker secrets.
export interface InstallerSecrets {
  DISCORD_INSTALLER_BOT_TOKEN: string
  DISCORD_GUILD_ID: string
  DISCORD_ESCALATION_CHANNEL_ID: string
  ANTHROPIC_API_KEY: string
  INSTALLER_SITE_SECRET: string
  INSTALLER_ADMIN_SECRET: string
}

// Handlers return this instead of throwing so results cross the DO RPC
// boundary as plain data.
export interface Reply {
  status: number
  body: Record<string, unknown>
}
