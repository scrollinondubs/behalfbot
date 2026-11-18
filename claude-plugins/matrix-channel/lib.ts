// Pure helpers for the Matrix channel: config/access loading, event parsing,
// content builders, chunking, markdown. No network here so it stays testable.
//
// Parts of the parsing and gating logic are adapted from
// zekker6/claude-code-channel-matrix (MIT, see LICENSE).

import { existsSync, readFileSync, chmodSync } from 'node:fs'
import { join, extname } from 'node:path'
import { homedir } from 'node:os'

// ── Config ─────────────────────────────────────────────

export interface Config {
  homeserverUrl: string
  accessToken: string
  botUserId: string
  /** Optional room filter (inbound, outbound and auto-join). null = every joined room. */
  roomIds: string[] | null
  /** Room for permission prompts when no room has been active yet. */
  permissionRoomId: string | null
  /** false = this process never syncs; outbound tools only. */
  inbound: boolean
}

export function channelsDir(): string {
  return (
    process.env.MATRIX_CHANNELS_DIR ??
    join(process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), '.claude'), 'channels', 'matrix')
  )
}

/** Loads <dir>/.env into process.env. Real env wins. */
export function loadEnvFile(dir: string): void {
  const envPath = join(dir, '.env')
  if (!existsSync(envPath)) return
  try { chmodSync(envPath, 0o600) } catch {}
  for (const line of readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/)
    if (!m || line.trim().startsWith('#')) continue
    if (process.env[m[1]!] === undefined) process.env[m[1]!] = m[2]!.replace(/^(['"])(.*)\1$/, '$2')
  }
}

export function loadConfig(env: Record<string, string | undefined> = process.env): Config {
  const missing = ['MATRIX_HOMESERVER_URL', 'MATRIX_ACCESS_TOKEN', 'MATRIX_BOT_USER_ID'].filter(k => !env[k])
  if (missing.length) throw new Error(`missing ${missing.join(', ')}`)
  let homeserverUrl = env.MATRIX_HOMESERVER_URL!.trim().replace(/\/+$/, '')
  if (!/^https?:\/\//.test(homeserverUrl)) homeserverUrl = `https://${homeserverUrl}`
  const rawRooms = env.MATRIX_ROOM_IDS?.trim()
  const roomIds = rawRooms ? rawRooms.split(',').map(s => s.trim()).filter(Boolean) : null
  return {
    homeserverUrl,
    accessToken: env.MATRIX_ACCESS_TOKEN!.trim(),
    botUserId: env.MATRIX_BOT_USER_ID!.trim(),
    roomIds,
    permissionRoomId: env.MATRIX_PERMISSION_ROOM_ID?.trim() || null,
    inbound: !/^(0|false|off|no)$/i.test(env.MATRIX_INBOUND?.trim() ?? ''),
  }
}

// ── Access ─────────────────────────────────────────────

export interface Access {
  allowedUsers: string[]
  /** Emoji to react with on receipt. Empty/absent disables. */
  ackReaction: string | null
  /** Max chars per outbound message before splitting. */
  textChunkLimit: number
  chunkMode: 'length' | 'newline'
}

export const DEFAULT_CHUNK_LIMIT = 16000
export const MAX_CHUNK_LIMIT = 32000

export function defaultAccess(): Access {
  return { allowedUsers: [], ackReaction: null, textChunkLimit: DEFAULT_CHUNK_LIMIT, chunkMode: 'newline' }
}

/** Re-read on every use so /matrix:access edits apply without a restart. */
export function loadAccess(path: string): Access {
  let raw: any
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'))
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
      process.stderr.write(`matrix channel: cannot parse ${path}, treating allowlist as empty\n`)
    }
    return defaultAccess()
  }
  const limit = typeof raw.textChunkLimit === 'number' ? raw.textChunkLimit : DEFAULT_CHUNK_LIMIT
  return {
    allowedUsers: Array.isArray(raw.allowedUsers) ? raw.allowedUsers.filter((u: unknown) => typeof u === 'string') : [],
    ackReaction: typeof raw.ackReaction === 'string' && raw.ackReaction ? raw.ackReaction : null,
    textChunkLimit: Math.max(1, Math.min(limit, MAX_CHUNK_LIMIT)),
    chunkMode: raw.chunkMode === 'length' ? 'length' : 'newline',
  }
}

// ── Gating ─────────────────────────────────────────────

export function roomAllowed(roomId: string, roomIds: string[] | null): boolean {
  return !roomIds || roomIds.includes(roomId)
}

export function shouldForward(
  sender: string,
  roomId: string,
  access: Access,
  botUserId: string,
  roomIds: string[] | null,
): boolean {
  if (sender === botUserId) return false
  if (!access.allowedUsers.includes(sender)) return false
  return roomAllowed(roomId, roomIds)
}

export function shouldAutoJoin(
  inviter: string,
  roomId: string,
  access: Access,
  roomIds: string[] | null,
): boolean {
  return access.allowedUsers.includes(inviter) && roomAllowed(roomId, roomIds)
}

// ── Sync parsing ───────────────────────────────────────

export interface RawRoomEvent {
  roomId: string
  event: any
}

export interface SyncInvite {
  roomId: string
  inviter: string
}

export interface ParsedSync {
  nextBatch: string
  events: RawRoomEvent[]
  invites: SyncInvite[]
  roomNames: Map<string, string>
}

export const SYNC_FILTER = JSON.stringify({
  room: {
    timeline: { types: ['m.room.message', 'm.reaction', 'm.room.encrypted'], limit: 50 },
    state: { types: ['m.room.name', 'm.room.canonical_alias'], lazy_load_members: true },
    ephemeral: { types: [] },
    account_data: { types: [] },
  },
  presence: { types: [] },
  account_data: { types: [] },
})

export function parseSync(data: any): ParsedSync {
  const events: RawRoomEvent[] = []
  const roomNames = new Map<string, string>()
  for (const [roomId, room] of Object.entries<any>(data?.rooms?.join ?? {})) {
    const stateEvents = [...(room.state?.events ?? []), ...(room.timeline?.events ?? [])]
    for (const e of stateEvents) {
      if (e?.type === 'm.room.name' && typeof e.content?.name === 'string' && e.content.name) {
        roomNames.set(roomId, e.content.name)
      }
    }
    for (const event of room.timeline?.events ?? []) {
      if (event && typeof event.event_id === 'string') events.push({ roomId, event })
    }
  }
  const invites: SyncInvite[] = []
  for (const [roomId, room] of Object.entries<any>(data?.rooms?.invite ?? {})) {
    const member = (room.invite_state?.events ?? []).find(
      (e: any) => e.type === 'm.room.member' && e.content?.membership === 'invite',
    )
    invites.push({ roomId, inviter: member?.sender ?? 'unknown' })
  }
  return { nextBatch: data?.next_batch, events, invites, roomNames }
}

// ── Message parsing ────────────────────────────────────

export type AttachmentKind = 'image' | 'audio' | 'video' | 'file'

export interface Attachment {
  kind: AttachmentKind
  name: string
  mimetype: string
  size: number | null
  mxc: string
  /** Element X voice message (org.matrix.msc3245.voice). */
  voice: boolean
  durationMs: number | null
}

export interface InboundMessage {
  roomId: string
  eventId: string
  sender: string
  ts: number
  msgtype: string
  body: string
  attachment: Attachment | null
  /** Event this one quote-replies to. */
  replyTo: string | null
  /** Set when this event is an edit (m.replace) of an earlier one. */
  editOf: string | null
}

export interface InboundReaction {
  roomId: string
  eventId: string
  sender: string
  targetEventId: string
  key: string
}

export type ParsedEvent =
  | { kind: 'message'; msg: InboundMessage }
  | { kind: 'reaction'; reaction: InboundReaction }
  | { kind: 'encrypted'; roomId: string; eventId: string; sender: string }
  | { kind: 'ignored' }

const MEDIA_KINDS: Record<string, AttachmentKind> = {
  'm.image': 'image',
  'm.audio': 'audio',
  'm.video': 'video',
  'm.file': 'file',
}

/** Removes the "> <@user> quoted text" fallback that clients prepend to replies. */
export function stripReplyFallback(body: string): string {
  if (!body.startsWith('> ')) return body
  const lines = body.split('\n')
  let i = 0
  while (i < lines.length && lines[i]!.startsWith('>')) i++
  if (i < lines.length && lines[i] === '') i++
  return lines.slice(i).join('\n')
}

export function parseEvent(roomId: string, ev: any): ParsedEvent {
  if (!ev || typeof ev !== 'object') return { kind: 'ignored' }
  if (ev.type === 'm.room.encrypted') {
    return { kind: 'encrypted', roomId, eventId: ev.event_id, sender: ev.sender }
  }
  if (ev.type === 'm.reaction') {
    const rel = ev.content?.['m.relates_to']
    if (rel?.rel_type !== 'm.annotation' || typeof rel.event_id !== 'string' || typeof rel.key !== 'string') {
      return { kind: 'ignored' }
    }
    return {
      kind: 'reaction',
      reaction: { roomId, eventId: ev.event_id, sender: ev.sender, targetEventId: rel.event_id, key: rel.key },
    }
  }
  if (ev.type !== 'm.room.message' || !ev.content || ev.unsigned?.redacted_because) return { kind: 'ignored' }

  const rel = ev.content['m.relates_to']
  let content = ev.content
  let editOf: string | null = null
  if (rel?.rel_type === 'm.replace' && typeof rel.event_id === 'string') {
    editOf = rel.event_id
    content = ev.content['m.new_content'] ?? ev.content
  }
  const replyTo: string | null =
    typeof rel?.['m.in_reply_to']?.event_id === 'string' && rel?.rel_type !== 'm.thread'
      ? rel['m.in_reply_to'].event_id
      : rel?.rel_type === 'm.thread' && rel?.is_falling_back === false
        ? rel['m.in_reply_to']?.event_id ?? null
        : null

  const msgtype: string = typeof content.msgtype === 'string' ? content.msgtype : 'm.text'
  let body: string = typeof content.body === 'string' ? content.body : ''
  if (replyTo && !editOf) body = stripReplyFallback(body)

  let attachment: Attachment | null = null
  const kind = MEDIA_KINDS[msgtype]
  if (kind) {
    const filename: string | null = typeof content.filename === 'string' ? content.filename : null
    const name = filename ?? (body || kind)
    // MSC2530: body is a caption only when a distinct filename is present.
    const caption = filename && body && body !== filename ? body : ''
    if (typeof content.url === 'string') {
      const duration = content.info?.duration ?? content['org.matrix.msc1767.audio']?.duration
      attachment = {
        kind,
        name,
        mimetype: baseMime(content.info?.mimetype) ?? 'application/octet-stream',
        size: typeof content.info?.size === 'number' ? content.info.size : null,
        mxc: content.url,
        voice: content['org.matrix.msc3245.voice'] !== undefined,
        durationMs: typeof duration === 'number' ? duration : null,
      }
      body = caption
    } else if (content.file) {
      body = `[encrypted ${kind} not supported: ${safeName(name)}]${caption ? ` ${caption}` : ''}`
    } else {
      body = caption
    }
  } else if (msgtype === 'm.emote') {
    body = `* ${body}`
  } else if (msgtype === 'm.location' && typeof content.geo_uri === 'string') {
    body = `${body} (${content.geo_uri})`.trim()
  }

  return {
    kind: 'message',
    msg: {
      roomId,
      eventId: ev.event_id,
      sender: ev.sender,
      ts: typeof ev.origin_server_ts === 'number' ? ev.origin_server_ts : Date.now(),
      msgtype,
      body,
      attachment,
      replyTo,
      editOf,
    },
  }
}

/** "audio/ogg; codecs=opus" -> "audio/ogg" */
export function baseMime(mime: unknown): string | null {
  if (typeof mime !== 'string' || !mime) return null
  return mime.split(';')[0]!.trim().toLowerCase() || null
}

// Uploader-controlled names land inside notification meta and newline-joined
// tool output; strip delimiter characters so they cannot forge structure.
export function safeName(name: string): string {
  return name.replace(/[\[\]\r\n;]/g, '_')
}

export function describeAttachment(att: Attachment): string {
  const size = att.size != null ? `, ${(att.size / 1024).toFixed(0)}KB` : ''
  const dur = att.durationMs != null ? `, ${Math.round(att.durationMs / 1000)}s` : ''
  return `${safeName(att.name)} (${att.mimetype}${size}${dur}${att.voice ? ', voice note' : ''})`
}

// ── Media ──────────────────────────────────────────────

export function parseMxc(mxc: string): { server: string; mediaId: string } {
  const m = /^mxc:\/\/([^/]+)\/([^/?#]+)$/.exec(mxc)
  if (!m) throw new Error(`invalid mxc url: ${mxc}`)
  return { server: m[1]!, mediaId: m[2]! }
}

export function mxcDownloadPath(mxc: string): string {
  const { server, mediaId } = parseMxc(mxc)
  return `/_matrix/client/v1/media/download/${encodeURIComponent(server)}/${encodeURIComponent(mediaId)}`
}

const EXT_MIME: Record<string, string> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.heic': 'image/heic', '.bmp': 'image/bmp', '.svg': 'image/svg+xml',
  '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/ogg',
  '.wav': 'audio/wav', '.flac': 'audio/flac', '.aac': 'audio/aac',
  '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm', '.mkv': 'video/x-matroska',
  '.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/markdown', '.json': 'application/json',
  '.csv': 'text/csv', '.html': 'text/html', '.zip': 'application/zip', '.log': 'text/plain',
}

export function mimeForPath(path: string): string {
  return EXT_MIME[extname(path).toLowerCase()] ?? 'application/octet-stream'
}

/**
 * Extension for a downloaded attachment. Voice notes are named "Voice message"
 * with no extension, so fall back to the mimetype (audio/ogg -> ogg, audio/mp4 -> m4a).
 */
export function attachmentExt(name: string, mime: string): string {
  const fromName = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1).replace(/[^a-zA-Z0-9]/g, '').slice(0, 10) : ''
  return fromName || extForMime(mime)
}

export function extForMime(mime: string): string {
  for (const [ext, m] of Object.entries(EXT_MIME)) if (m === mime) return ext.slice(1)
  return 'bin'
}

export function msgtypeForMime(mime: string): string {
  // SVG renders inconsistently across clients as m.image; ship it as a file.
  if (mime.startsWith('image/') && mime !== 'image/svg+xml') return 'm.image'
  if (mime.startsWith('audio/')) return 'm.audio'
  if (mime.startsWith('video/')) return 'm.video'
  return 'm.file'
}

// ── Outbound content ───────────────────────────────────

function withReply(content: Record<string, any>, replyTo?: string | null): Record<string, any> {
  if (replyTo) content['m.relates_to'] = { 'm.in_reply_to': { event_id: replyTo } }
  return content
}

export function textContent(text: string, replyTo?: string | null): Record<string, any> {
  const content: Record<string, any> = { msgtype: 'm.text', body: text }
  const html = markdownToHtml(text)
  if (html !== null) {
    content.format = 'org.matrix.custom.html'
    content.formatted_body = html
  }
  return withReply(content, replyTo)
}

export function mediaContent(
  name: string,
  mxc: string,
  mimetype: string,
  size: number,
  replyTo?: string | null,
): Record<string, any> {
  return withReply(
    { msgtype: msgtypeForMime(mimetype), body: name, filename: name, url: mxc, info: { mimetype, size } },
    replyTo,
  )
}

export function editContent(targetEventId: string, text: string): Record<string, any> {
  const fresh = textContent(text)
  const content: Record<string, any> = {
    msgtype: 'm.text',
    body: `* ${text}`,
    'm.new_content': fresh,
    'm.relates_to': { rel_type: 'm.replace', event_id: targetEventId },
  }
  if (fresh.formatted_body) {
    content.format = fresh.format
    content.formatted_body = `* ${fresh.formatted_body}`
  }
  return content
}

export function reactionContent(targetEventId: string, key: string): Record<string, any> {
  return { 'm.relates_to': { rel_type: 'm.annotation', event_id: targetEventId, key } }
}

// ── Chunking ───────────────────────────────────────────

export function chunk(text: string, limit: number, mode: 'length' | 'newline'): string[] {
  if (text.length <= limit) return [text]
  const out: string[] = []
  let rest = text
  while (rest.length > limit) {
    let cut = limit
    if (mode === 'newline') {
      const para = rest.lastIndexOf('\n\n', limit)
      const line = rest.lastIndexOf('\n', limit)
      const space = rest.lastIndexOf(' ', limit)
      cut = para > limit / 2 ? para : line > limit / 2 ? line : space > 0 ? space : limit
    }
    out.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^\n+/, '')
  }
  if (rest) out.push(rest)
  return out
}

// ── Markdown -> Matrix HTML ────────────────────────────
// Matrix clients do not render markdown from `body`; they render
// `formatted_body`. Claude writes markdown out of habit, so convert the common
// subset. Returns null when the text has no markdown, so plain messages stay plain.

export function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function inlineMd(s: string): string {
  const codes: string[] = []
  let out = s.replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(`<code>${escapeHtml(c)}</code>`)
    return `\u0000${codes.length - 1}\u0000`
  })
  out = escapeHtml(out)
  out = out.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_, t, u) => `<a href="${u}">${t}</a>`)
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
  out = out.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?!\w)/g, '$1<em>$2</em>')
  out = out.replace(/(^|[^\w])_([^_\s][^_\n]*?)_(?!\w)/g, '$1<em>$2</em>')
  out = out.replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]!)
}

const MD_HINT = /(```|`[^`\n]+`|\*\*|__|~~|\[[^\]\n]+\]\(|^\s{0,3}#{1,6}\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^>\s|(^|[^*\w])\*[^*\s][^*\n]*\*(?!\w))/m

export function markdownToHtml(text: string): string | null {
  if (!MD_HINT.test(text)) return null
  const lines = text.split('\n')
  const html: string[] = []
  let i = 0
  let para: string[] = []
  const flushPara = () => {
    if (para.length) html.push(`<p>${para.map(inlineMd).join('<br>')}</p>`)
    para = []
  }
  while (i < lines.length) {
    const line = lines[i]!
    const fence = /^\s*```\s*([\w+-]*)\s*$/.exec(line)
    if (fence) {
      flushPara()
      const body: string[] = []
      i++
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i]!)) body.push(lines[i++]!)
      i++
      const cls = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : ''
      html.push(`<pre><code${cls}>${escapeHtml(body.join('\n'))}</code></pre>`)
      continue
    }
    const heading = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      flushPara()
      const n = heading[1]!.length
      html.push(`<h${n}>${inlineMd(heading[2]!)}</h${n}>`)
      i++
      continue
    }
    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      flushPara()
      const ordered = /^\s*\d+[.)]\s+/.test(line)
      const re = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/
      const items: string[] = []
      while (i < lines.length && re.test(lines[i]!)) items.push(`<li>${inlineMd(lines[i++]!.replace(re, ''))}</li>`)
      const tag = ordered ? 'ol' : 'ul'
      html.push(`<${tag}>${items.join('')}</${tag}>`)
      continue
    }
    if (/^>\s?/.test(line)) {
      flushPara()
      const quoted: string[] = []
      while (i < lines.length && /^>\s?/.test(lines[i]!)) quoted.push(inlineMd(lines[i++]!.replace(/^>\s?/, '')))
      html.push(`<blockquote>${quoted.join('<br>')}</blockquote>`)
      continue
    }
    if (line.trim() === '') {
      flushPara()
      i++
      continue
    }
    para.push(line)
    i++
  }
  flushPara()
  return html.join('')
}

// ── History rendering ──────────────────────────────────

/**
 * Renders /messages output (any order) oldest-first, one line per message.
 * Edits are folded onto their targets so the latest text shows, marked (edited).
 */
export function formatHistory(
  rawEvents: any[],
  roomId: string,
  botUserId: string,
  nameOf: (userId: string) => string,
  limit: number,
): string {
  const parsed: InboundMessage[] = []
  for (const ev of rawEvents) {
    const p = parseEvent(roomId, ev)
    if (p.kind === 'message') parsed.push(p.msg)
  }
  const latestEdit = new Map<string, InboundMessage>()
  for (const m of parsed) {
    if (!m.editOf) continue
    const prev = latestEdit.get(m.editOf)
    if (!prev || prev.ts <= m.ts) latestEdit.set(m.editOf, m)
  }
  const rows = parsed
    .filter(m => !m.editOf)
    .sort((a, b) => a.ts - b.ts)
    .slice(-limit)
  if (rows.length === 0) return '(no messages)'
  return rows
    .map(m => {
      const edit = latestEdit.get(m.eventId)
      const shown = edit && edit.sender === m.sender ? edit : m
      const who = m.sender === botUserId ? 'me' : nameOf(m.sender)
      const att = m.attachment ? ` +1att ${describeAttachment(m.attachment)}` : ''
      // Tool output is newline-joined; collapse newlines so content cannot forge rows.
      const text = (shown.body || (m.attachment ? '(attachment)' : '')).replace(/[\r\n]+/g, ' ⏎ ')
      const edited = shown !== m ? ' (edited)' : ''
      return `[${new Date(m.ts).toISOString()}] ${who}: ${text}${edited}  (id: ${m.eventId}${att})`
    })
    .join('\n')
}

// ── Permission relay ───────────────────────────────────

// Same grammar as the Discord channel: 5 lowercase letters a-z minus 'l'.
export const PERMISSION_REPLY_RE = /^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i
export const ALLOW_EMOJI = '✅'
export const DENY_EMOJI = '❌'

export function classifyVerdict(key: string): 'allow' | 'deny' | null {
  const k = key.replace(/️/g, '')
  if (k === ALLOW_EMOJI || k === '👍') return 'allow'
  if (k === DENY_EMOJI || k === '👎') return 'deny'
  return null
}

export interface PermissionRequest {
  request_id: string
  tool_name: string
  description: string
  input_preview: string
}

export function permissionPromptText(p: PermissionRequest): string {
  const lines = [`Permission: ${p.tool_name} (id: ${p.request_id})`, p.description]
  if (p.input_preview) lines.push('', '```', p.input_preview, '```')
  lines.push('', `React ${ALLOW_EMOJI} to allow or ${DENY_EMOJI} to deny, or reply "yes ${p.request_id}" / "no ${p.request_id}".`)
  return lines.join('\n')
}

// ── Misc ───────────────────────────────────────────────

export function localpart(userId: string): string {
  const m = /^@([^:]+):/.exec(userId)
  return m ? m[1]! : userId
}

export function makeTxnId(seq: number): string {
  return `jx${process.pid}.${Date.now()}.${seq}.${Math.random().toString(36).slice(2, 8)}`
}
