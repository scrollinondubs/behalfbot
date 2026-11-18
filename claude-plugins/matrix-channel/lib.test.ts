import { describe, test, expect, beforeEach, afterEach } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  loadConfig, loadAccess, shouldForward, shouldAutoJoin, parseSync, parseEvent, stripReplyFallback,
  describeAttachment, mxcDownloadPath, mimeForPath, msgtypeForMime, textContent, mediaContent, editContent,
  reactionContent, chunk, markdownToHtml, formatHistory, classifyVerdict, PERMISSION_REPLY_RE, safeName,
  makeTxnId, attachmentExt, DEFAULT_CHUNK_LIMIT, type Access,
} from './lib'

const BOT = '@bot:example.org'
const ALICE = '@alice:example.org'
const ROOM = '!room:example.org'

function access(over: Partial<Access> = {}): Access {
  return { allowedUsers: [ALICE], ackReaction: null, textChunkLimit: DEFAULT_CHUNK_LIMIT, chunkMode: 'newline', ...over }
}

function msg(content: any, extra: any = {}) {
  return { type: 'm.room.message', event_id: '$e1', sender: ALICE, origin_server_ts: 1700000000000, content, ...extra }
}

describe('loadConfig', () => {
  const base = { MATRIX_HOMESERVER_URL: 'http://127.0.0.1:8008/', MATRIX_ACCESS_TOKEN: 't', MATRIX_BOT_USER_ID: BOT }
  test('strips trailing slash, keeps scheme', () => {
    expect(loadConfig(base).homeserverUrl).toBe('http://127.0.0.1:8008')
  })
  test('adds https when scheme missing', () => {
    expect(loadConfig({ ...base, MATRIX_HOMESERVER_URL: 'matrix.example.com' }).homeserverUrl).toBe('https://matrix.example.com')
  })
  test('throws naming missing vars without echoing values', () => {
    expect(() => loadConfig({ MATRIX_ACCESS_TOKEN: 'secret' })).toThrow(/MATRIX_HOMESERVER_URL, MATRIX_BOT_USER_ID/)
    try { loadConfig({ MATRIX_ACCESS_TOKEN: 'secret' }) } catch (e) { expect(String(e)).not.toContain('secret') }
  })
  test('parses room filter and inbound switch', () => {
    const c = loadConfig({ ...base, MATRIX_ROOM_IDS: '!a:x, !b:x', MATRIX_INBOUND: 'off' })
    expect(c.roomIds).toEqual(['!a:x', '!b:x'])
    expect(c.inbound).toBe(false)
    expect(loadConfig(base).inbound).toBe(true)
    expect(loadConfig(base).roomIds).toBeNull()
  })
})

describe('loadAccess', () => {
  let dir: string
  beforeEach(() => { dir = mkdtempSync(join(tmpdir(), 'mxa-')) })
  afterEach(() => rmSync(dir, { recursive: true, force: true }))
  test('missing file = empty allowlist', () => {
    expect(loadAccess(join(dir, 'nope.json')).allowedUsers).toEqual([])
  })
  test('corrupt file = empty allowlist', () => {
    writeFileSync(join(dir, 'a.json'), '{nope')
    expect(loadAccess(join(dir, 'a.json')).allowedUsers).toEqual([])
  })
  test('reads fields and clamps chunk limit', () => {
    writeFileSync(join(dir, 'a.json'), JSON.stringify({ allowedUsers: [ALICE, 5], ackReaction: '👀', textChunkLimit: 999999, chunkMode: 'length' }))
    const a = loadAccess(join(dir, 'a.json'))
    expect(a.allowedUsers).toEqual([ALICE])
    expect(a.ackReaction).toBe('👀')
    expect(a.textChunkLimit).toBe(32000)
    expect(a.chunkMode).toBe('length')
  })
})

describe('gating', () => {
  test('forwards allowlisted sender', () => {
    expect(shouldForward(ALICE, ROOM, access(), BOT, null)).toBe(true)
  })
  test('drops bot itself, strangers, and filtered rooms', () => {
    expect(shouldForward(BOT, ROOM, access({ allowedUsers: [ALICE, BOT] }), BOT, null)).toBe(false)
    expect(shouldForward('@eve:evil.org', ROOM, access(), BOT, null)).toBe(false)
    expect(shouldForward(ALICE, ROOM, access(), BOT, ['!other:x'])).toBe(false)
  })
  test('auto-joins only on allowlisted invites', () => {
    expect(shouldAutoJoin(ALICE, ROOM, access(), null)).toBe(true)
    expect(shouldAutoJoin('@eve:evil.org', ROOM, access(), null)).toBe(false)
    expect(shouldAutoJoin(ALICE, ROOM, access(), ['!other:x'])).toBe(false)
  })
})

describe('parseSync', () => {
  test('collects events, names and invites', () => {
    const p = parseSync({
      next_batch: 's1',
      rooms: {
        join: {
          [ROOM]: {
            state: { events: [{ type: 'm.room.name', content: { name: 'bot-ops' } }] },
            timeline: { events: [msg({ msgtype: 'm.text', body: 'hi' })] },
          },
        },
        invite: {
          '!new:x': { invite_state: { events: [{ type: 'm.room.member', sender: ALICE, content: { membership: 'invite' } }] } },
        },
      },
    })
    expect(p.nextBatch).toBe('s1')
    expect(p.events).toHaveLength(1)
    expect(p.roomNames.get(ROOM)).toBe('bot-ops')
    expect(p.invites).toEqual([{ roomId: '!new:x', inviter: ALICE }])
  })
  test('tolerates empty sync', () => {
    expect(parseSync({ next_batch: 's2' }).events).toEqual([])
  })
})

describe('parseEvent', () => {
  test('text', () => {
    const p = parseEvent(ROOM, msg({ msgtype: 'm.text', body: 'hello' }))
    expect(p.kind).toBe('message')
    if (p.kind !== 'message') return
    expect(p.msg.body).toBe('hello')
    expect(p.msg.attachment).toBeNull()
  })
  test('image with url', () => {
    const p = parseEvent(ROOM, msg({ msgtype: 'm.image', body: 'cat.png', url: 'mxc://example.org/abc', info: { mimetype: 'image/png', size: 2048 } }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.attachment).toMatchObject({ kind: 'image', name: 'cat.png', mimetype: 'image/png', size: 2048, voice: false })
    expect(p.msg.body).toBe('')
  })
  test('voice note (m.audio + msc3245)', () => {
    const p = parseEvent(ROOM, msg({
      msgtype: 'm.audio', body: 'Voice message', url: 'mxc://example.org/v',
      info: { mimetype: 'audio/ogg', size: 9000, duration: 3000 },
      'org.matrix.msc1767.audio': { duration: 3000 }, 'org.matrix.msc3245.voice': {},
    }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.attachment?.kind).toBe('audio')
    expect(p.msg.attachment?.voice).toBe(true)
    expect(p.msg.attachment?.durationMs).toBe(3000)
    expect(describeAttachment(p.msg.attachment!)).toBe('Voice message (audio/ogg, 9KB, 3s, voice note)')
    expect(attachmentExt(p.msg.attachment!.name, p.msg.attachment!.mimetype)).toBe('ogg')
  })
  test('voice note mime params stripped, audio/mp4 -> m4a', () => {
    const p = parseEvent(ROOM, msg({
      msgtype: 'm.audio', body: 'Voice message', url: 'mxc://example.org/v2',
      info: { mimetype: 'audio/mp4; codecs=mp4a.40.2' }, 'org.matrix.msc1767.audio': { duration: 1200 }, 'org.matrix.msc3245.voice': {},
    }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.attachment?.mimetype).toBe('audio/mp4')
    expect(p.msg.attachment?.durationMs).toBe(1200)
    expect(attachmentExt('Voice message', 'audio/mp4')).toBe('m4a')
    expect(attachmentExt('note.opus', 'audio/ogg')).toBe('opus')
  })
  test('file with caption (MSC2530)', () => {
    const p = parseEvent(ROOM, msg({ msgtype: 'm.file', body: 'see attached', filename: 'report.pdf', url: 'mxc://x/y', info: { mimetype: 'application/pdf' } }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.attachment?.name).toBe('report.pdf')
    expect(p.msg.attachment?.size).toBeNull()
    expect(p.msg.body).toBe('see attached')
  })
  test('video', () => {
    const p = parseEvent(ROOM, msg({ msgtype: 'm.video', body: 'clip.mp4', url: 'mxc://x/v', info: { mimetype: 'video/mp4', size: 1 } }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.attachment?.kind).toBe('video')
  })
  test('encrypted media (content.file) becomes a text notice, no attachment', () => {
    const p = parseEvent(ROOM, msg({ msgtype: 'm.image', body: 'secret.png', file: { url: 'mxc://x/e' } }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.attachment).toBeNull()
    expect(p.msg.body).toContain('encrypted image not supported')
  })
  test('m.room.encrypted is flagged, not parsed', () => {
    expect(parseEvent(ROOM, { type: 'm.room.encrypted', event_id: '$x', sender: ALICE, content: {} }).kind).toBe('encrypted')
  })
  test('reply strips fallback and records reply_to', () => {
    const p = parseEvent(ROOM, msg({
      msgtype: 'm.text', body: '> <@bot:example.org> earlier\n> more\n\nmy answer',
      'm.relates_to': { 'm.in_reply_to': { event_id: '$orig' } },
    }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.replyTo).toBe('$orig')
    expect(p.msg.body).toBe('my answer')
  })
  test('edit uses m.new_content and records edit_of', () => {
    const p = parseEvent(ROOM, msg({
      msgtype: 'm.text', body: '* fixed', 'm.new_content': { msgtype: 'm.text', body: 'fixed' },
      'm.relates_to': { rel_type: 'm.replace', event_id: '$orig' },
    }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.editOf).toBe('$orig')
    expect(p.msg.body).toBe('fixed')
  })
  test('reaction', () => {
    const p = parseEvent(ROOM, { type: 'm.reaction', event_id: '$r', sender: ALICE, content: { 'm.relates_to': { rel_type: 'm.annotation', event_id: '$t', key: '✅' } } })
    expect(p).toEqual({ kind: 'reaction', reaction: { roomId: ROOM, eventId: '$r', sender: ALICE, targetEventId: '$t', key: '✅' } })
  })
  test('redacted and unknown events ignored', () => {
    expect(parseEvent(ROOM, msg({}, { unsigned: { redacted_because: {} } })).kind).toBe('ignored')
    expect(parseEvent(ROOM, { type: 'm.room.member', event_id: '$m' }).kind).toBe('ignored')
  })
  test('emote', () => {
    const p = parseEvent(ROOM, msg({ msgtype: 'm.emote', body: 'waves' }))
    if (p.kind !== 'message') throw new Error('expected message')
    expect(p.msg.body).toBe('* waves')
  })
})

describe('stripReplyFallback', () => {
  test('leaves normal text alone', () => {
    expect(stripReplyFallback('> quoted on purpose')).toBe('')
    expect(stripReplyFallback('plain')).toBe('plain')
  })
})

describe('safe names', () => {
  test('strips delimiters', () => {
    expect(safeName('a]b[c;d\ne')).toBe('a_b_c_d_e')
  })
})

describe('media helpers', () => {
  test('mxc download path uses authenticated media endpoint', () => {
    expect(mxcDownloadPath('mxc://example.org/AbC')).toBe('/_matrix/client/v1/media/download/example.org/AbC')
    expect(() => mxcDownloadPath('https://evil')).toThrow()
  })
  test('mime and msgtype mapping', () => {
    expect(mimeForPath('/a/b.PNG')).toBe('image/png')
    expect(mimeForPath('/a/b.unknown')).toBe('application/octet-stream')
    expect(msgtypeForMime('image/png')).toBe('m.image')
    expect(msgtypeForMime('image/svg+xml')).toBe('m.file')
    expect(msgtypeForMime('audio/ogg')).toBe('m.audio')
    expect(msgtypeForMime('video/mp4')).toBe('m.video')
    expect(msgtypeForMime('application/pdf')).toBe('m.file')
  })
})

describe('content builders', () => {
  test('plain text stays plain and uses m.text (m.notice is push-suppressed)', () => {
    expect(textContent('hello there')).toEqual({ msgtype: 'm.text', body: 'hello there' })
  })
  test('markdown text gets formatted_body', () => {
    const c = textContent('**bold** and `code`')
    expect(c.format).toBe('org.matrix.custom.html')
    expect(c.formatted_body).toBe('<p><strong>bold</strong> and <code>code</code></p>')
  })
  test('reply relation', () => {
    expect(textContent('x', '$e')['m.relates_to']).toEqual({ 'm.in_reply_to': { event_id: '$e' } })
  })
  test('media content', () => {
    expect(mediaContent('a.ogg', 'mxc://s/m', 'audio/ogg', 10)).toEqual({
      msgtype: 'm.audio', body: 'a.ogg', filename: 'a.ogg', url: 'mxc://s/m', info: { mimetype: 'audio/ogg', size: 10 },
    })
  })
  test('edit content', () => {
    const c = editContent('$t', 'new text')
    expect(c.body).toBe('* new text')
    expect(c['m.new_content']).toEqual({ msgtype: 'm.text', body: 'new text' })
    expect(c['m.relates_to']).toEqual({ rel_type: 'm.replace', event_id: '$t' })
  })
  test('reaction content', () => {
    expect(reactionContent('$t', '👍')).toEqual({ 'm.relates_to': { rel_type: 'm.annotation', event_id: '$t', key: '👍' } })
  })
  test('txn ids are unique within a process', () => {
    expect(makeTxnId(1)).not.toBe(makeTxnId(1))
  })
})

describe('chunk', () => {
  test('short text is one chunk', () => {
    expect(chunk('abc', 10, 'newline')).toEqual(['abc'])
  })
  test('prefers paragraph breaks', () => {
    const text = 'a'.repeat(60) + '\n\n' + 'b'.repeat(60)
    expect(chunk(text, 100, 'newline')).toEqual(['a'.repeat(60), 'b'.repeat(60)])
  })
  test('hard cut in length mode', () => {
    expect(chunk('abcdefghij', 4, 'length')).toEqual(['abcd', 'efgh', 'ij'])
  })
})

describe('markdownToHtml', () => {
  test('null for plain text, including snake_case and math', () => {
    expect(markdownToHtml('just words')).toBeNull()
    expect(markdownToHtml('a * b * c')).toBeNull()
  })
  test('escapes html', () => {
    expect(markdownToHtml('**<script>**')).toBe('<p><strong>&lt;script&gt;</strong></p>')
  })
  test('code fence keeps content verbatim', () => {
    expect(markdownToHtml('```ts\nconst a = 1 < 2 **x**\n```')).toBe('<pre><code class="language-ts">const a = 1 &lt; 2 **x**</code></pre>')
  })
  test('lists, headings, links, quotes', () => {
    expect(markdownToHtml('# Title\n- one\n- two')).toBe('<h1>Title</h1><ul><li>one</li><li>two</li></ul>')
    expect(markdownToHtml('1. a\n2. b')).toBe('<ol><li>a</li><li>b</li></ol>')
    expect(markdownToHtml('[site](https://x.dev)')).toBe('<p><a href="https://x.dev">site</a></p>')
    expect(markdownToHtml('[bad](javascript:alert(1)) **x**')).not.toContain('href="javascript')
    expect(markdownToHtml('> quoted\n\nafter **b**')).toBe('<blockquote>quoted</blockquote><p>after <strong>b</strong></p>')
  })
  test('snake_case is not italicised', () => {
    expect(markdownToHtml('**x** my_var_name')).toBe('<p><strong>x</strong> my_var_name</p>')
  })
})

describe('formatHistory', () => {
  const ev = (id: string, ts: number, sender: string, content: any) => ({ type: 'm.room.message', event_id: id, sender, origin_server_ts: ts, content })
  test('oldest-first, me for bot, edits folded, attachments marked, newlines collapsed', () => {
    const out = formatHistory(
      [
        ev('$3', 3000, BOT, { msgtype: 'm.text', body: '* v2', 'm.new_content': { msgtype: 'm.text', body: 'v2' }, 'm.relates_to': { rel_type: 'm.replace', event_id: '$2' } }),
        ev('$2', 2000, BOT, { msgtype: 'm.text', body: 'v1' }),
        ev('$1', 1000, ALICE, { msgtype: 'm.image', body: 'p.png', url: 'mxc://s/p', info: { mimetype: 'image/png', size: 1024 } }),
        ev('$0', 500, ALICE, { msgtype: 'm.text', body: 'line1\nline2' }),
      ],
      ROOM, BOT, () => 'Alice', 20,
    )
    const lines = out.split('\n')
    expect(lines).toHaveLength(3)
    expect(lines[0]).toContain('Alice: line1 ⏎ line2')
    expect(lines[1]).toContain('(id: $1 +1att p.png (image/png, 1KB))')
    expect(lines[2]).toContain('me: v2 (edited)  (id: $2)')
  })
  test('edits by another sender are ignored', () => {
    const out = formatHistory(
      [
        ev('$1', 1000, BOT, { msgtype: 'm.text', body: 'orig' }),
        ev('$2', 2000, ALICE, { msgtype: 'm.text', body: '* hijack', 'm.new_content': { msgtype: 'm.text', body: 'hijack' }, 'm.relates_to': { rel_type: 'm.replace', event_id: '$1' } }),
      ],
      ROOM, BOT, () => 'Alice', 20,
    )
    expect(out).toContain('me: orig  (id: $1)')
  })
  test('limit keeps newest', () => {
    const out = formatHistory([ev('$a', 1, ALICE, { body: 'a' }), ev('$b', 2, ALICE, { body: 'b' })], ROOM, BOT, () => 'S', 1)
    expect(out).toContain('$b')
    expect(out).not.toContain('$a')
  })
  test('empty', () => {
    expect(formatHistory([], ROOM, BOT, () => '', 5)).toBe('(no messages)')
  })
})

describe('permission relay', () => {
  test('reply grammar matches Discord', () => {
    expect(PERMISSION_REPLY_RE.exec('yes abcde')?.[2]).toBe('abcde')
    expect(PERMISSION_REPLY_RE.exec('No ABCDE')).not.toBeNull()
    expect(PERMISSION_REPLY_RE.exec('yes please do it')).toBeNull()
    expect(PERMISSION_REPLY_RE.exec('yes abcle')).toBeNull()
  })
  test('verdict emoji, with or without variation selector', () => {
    expect(classifyVerdict('✅')).toBe('allow')
    expect(classifyVerdict('❌')).toBe('deny')
    expect(classifyVerdict('👍️')).toBe('allow')
    expect(classifyVerdict('👀')).toBeNull()
  })
})
