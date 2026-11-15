// Minimal Discord REST client for the installer bot. No gateway: the bot
// reads customer activity by polling the channel when the alarm needs it.

import type { FetchLike } from './types.ts'

const API = 'https://discord.com/api/v10'
// Discord rejects requests without a bot-style User-Agent, and Cloudflare in
// front of discord.com blocks bare runtime defaults.
export const USER_AGENT = 'DiscordBot (https://github.com/scrollinondubs/behalfbot, 0.1) behalfbot-installer'

// Permission bits, https://discord.com/developers/docs/topics/permissions
export const PERM = {
  MANAGE_CHANNELS: 1n << 4n,
  VIEW_CHANNEL: 1n << 10n,
  SEND_MESSAGES: 1n << 11n,
  EMBED_LINKS: 1n << 14n,
  ATTACH_FILES: 1n << 15n,
  READ_MESSAGE_HISTORY: 1n << 16n,
}

const MEMBER_ALLOW = PERM.VIEW_CHANNEL | PERM.SEND_MESSAGES | PERM.READ_MESSAGE_HISTORY | PERM.ATTACH_FILES | PERM.EMBED_LINKS
const BOT_ALLOW = MEMBER_ALLOW | PERM.MANAGE_CHANNELS

const OVERWRITE_ROLE = 0
const OVERWRITE_MEMBER = 1
const GUILD_TEXT = 0

export class DiscordError extends Error {
  readonly status: number
  constructor(status: number, method: string, path: string, detail: string) {
    super(`discord ${method} ${path} -> HTTP ${status}${detail ? `: ${detail}` : ''}`)
    this.status = status
  }
}

export function channelName(base: string): string {
  const slug = base
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80)
  return `${slug || 'customer'}-install`
}

export class DiscordClient {
  private readonly token: string
  private readonly guildId: string
  private readonly fetchImpl: FetchLike
  private botId: string | null = null

  constructor(o: { token: string; guildId: string; fetch: FetchLike }) {
    this.token = o.token
    this.guildId = o.guildId
    this.fetchImpl = o.fetch
  }

  private async req(method: string, path: string, body?: unknown, retried = false): Promise<unknown> {
    const res = await this.fetchImpl(
      new Request(`${API}${path}`, {
        method,
        headers: {
          authorization: `Bot ${this.token}`,
          'user-agent': USER_AGENT,
          ...(body === undefined ? {} : { 'content-type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
    )
    if (res.status === 429 && !retried) {
      const j = (await res.json().catch(() => ({}))) as { retry_after?: number }
      const wait = Math.min(Math.max(Number(j.retry_after ?? 1), 0), 5)
      await new Promise((r) => setTimeout(r, wait * 1000))
      return this.req(method, path, body, true)
    }
    if (!res.ok) {
      const detail = (await res.text().catch(() => '')).slice(0, 200)
      throw new DiscordError(res.status, method, path, detail)
    }
    if (res.status === 204) return null
    return res.json()
  }

  async botUserId(): Promise<string> {
    if (!this.botId) {
      const me = (await this.req('GET', '/users/@me')) as { id: string }
      this.botId = me.id
    }
    return this.botId
  }

  // Private text channel: @everyone (role id = guild id) denied, the bot and
  // the customer allowed. customerUserId may be null for a Stripe purchase
  // whose Discord id is not known yet; allowMember adds them later.
  async createPrivateChannel(name: string, customerUserId: string | null): Promise<string> {
    const botId = await this.botUserId()
    const overwrites = [
      { id: this.guildId, type: OVERWRITE_ROLE, allow: '0', deny: PERM.VIEW_CHANNEL.toString() },
      { id: botId, type: OVERWRITE_MEMBER, allow: BOT_ALLOW.toString(), deny: '0' },
    ]
    if (customerUserId) overwrites.push({ id: customerUserId, type: OVERWRITE_MEMBER, allow: MEMBER_ALLOW.toString(), deny: '0' })
    const ch = (await this.req('POST', `/guilds/${this.guildId}/channels`, {
      name,
      type: GUILD_TEXT,
      permission_overwrites: overwrites,
    })) as { id: string }
    return ch.id
  }

  async allowMember(channelId: string, userId: string): Promise<void> {
    await this.req('PUT', `/channels/${channelId}/permissions/${userId}`, {
      type: OVERWRITE_MEMBER,
      allow: MEMBER_ALLOW.toString(),
      deny: '0',
    })
  }

  async postMessage(channelId: string, content: string, mentionUserIds: string[] = []): Promise<string> {
    const m = (await this.req('POST', `/channels/${channelId}/messages`, {
      content: content.slice(0, 2000),
      allowed_mentions: { parse: [], users: mentionUserIds },
    })) as { id: string }
    return m.id
  }

  // Unix seconds of the newest message by userId among the last 100, or null.
  async latestMessageAt(channelId: string, userId: string): Promise<number | null> {
    const msgs = (await this.req('GET', `/channels/${channelId}/messages?limit=100`)) as {
      author?: { id?: string }
      timestamp?: string
    }[]
    let latest: number | null = null
    for (const m of msgs ?? []) {
      if (m.author?.id !== userId || !m.timestamp) continue
      const t = Math.floor(Date.parse(m.timestamp) / 1000)
      if (Number.isFinite(t) && (latest === null || t > latest)) latest = t
    }
    return latest
  }

  async findMemberByUsername(username: string): Promise<string | null> {
    const q = encodeURIComponent(username)
    const members = (await this.req('GET', `/guilds/${this.guildId}/members/search?query=${q}&limit=10`)) as {
      user?: { id?: string; username?: string }
    }[]
    const want = username.toLowerCase()
    const hit = (members ?? []).find((m) => m.user?.username?.toLowerCase() === want)
    return hit?.user?.id ?? null
  }

  async deleteChannel(channelId: string): Promise<void> {
    await this.req('DELETE', `/channels/${channelId}`)
  }

  // Keeps history but takes the customer out. Not used by GC (Discord caps a
  // guild at 500 channels, so closed installs are deleted), kept for manual
  // use.
  async archiveChannel(channelId: string, customerUserId: string | null): Promise<void> {
    const ch = (await this.req('GET', `/channels/${channelId}`)) as { name?: string }
    await this.req('PATCH', `/channels/${channelId}`, { name: `closed-${ch.name ?? 'install'}`.slice(0, 100) })
    if (customerUserId) {
      await this.req('PUT', `/channels/${channelId}/permissions/${customerUserId}`, {
        type: OVERWRITE_MEMBER,
        allow: '0',
        deny: PERM.VIEW_CHANNEL.toString(),
      })
    }
  }
}
