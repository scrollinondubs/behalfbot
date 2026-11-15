// Hard requirement 1 (CONTRACT.md): no secret value reaches the journal or
// the model. Two layers: exact values we know about (anything that went to
// the box over stdin, the install's private key, the Hetzner token) in their
// raw, base64 and URL-encoded forms, plus token shapes we can recognise
// without knowing the value.

export const REDACTED = '[REDACTED]'

// Shorter values would redact ordinary words and numbers out of every row.
const MIN_SECRET_LEN = 8

const PATTERNS: RegExp[] = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)/g,
  /\bsk-ant-[A-Za-z0-9_-]{16,}/g,
  /\bsk-(proj-)?[A-Za-z0-9_-]{20,}/g,
  /\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/g,
  /\bgithub_pat_[A-Za-z0-9_]{20,}/g,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/g,
  /\bya29\.[A-Za-z0-9_-]{20,}/g,
  /\b1\/\/[A-Za-z0-9_-]{30,}/g,
  /\bGOCSPX-[A-Za-z0-9_-]{20,}/g,
  // Google API keys (the browser autopilot's Places key).
  /\bAIza[0-9A-Za-z_-]{35}/g,
  // Discord bot tokens: base64 user id . timestamp . hmac
  /\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}\b/g,
  // Hetzner Cloud API tokens are 64 alphanumerics. Also eats sha256 hex
  // digests, which is an acceptable loss in a journal.
  /\b[A-Za-z0-9]{64}\b/g,
]

function b64(s: string): string {
  const bytes = new TextEncoder().encode(s)
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

function variants(value: string): string[] {
  const out = new Set<string>()
  const add = (v: string) => {
    const t = v.trim()
    if (t.length >= MIN_SECRET_LEN) out.add(t)
  }
  add(value)
  for (const line of value.split(/\r?\n/)) {
    add(line)
    // KEY=value and KEY: value lines: the value alone is what a command echoes.
    const kv = /^[^=:\s]+\s*[=:]\s*(.+)$/.exec(line.trim())
    if (kv) add(kv[1].replace(/^["']|["']$/g, ''))
  }
  for (const v of [...out]) {
    add(b64(v))
    add(b64(v).replace(/=+$/, ''))
    add(encodeURIComponent(v))
  }
  return [...out]
}

export class Redactor {
  private secrets = new Set<string>()

  add(value: string | null | undefined): this {
    if (typeof value !== 'string') return this
    for (const v of variants(value)) this.secrets.add(v)
    return this
  }

  contains(text: string): boolean {
    for (const s of this.secrets) if (text.includes(s)) return true
    return false
  }

  redact(text: string): string {
    let out = text
    const exact = [...this.secrets].sort((a, b) => b.length - a.length)
    for (const s of exact) out = out.split(s).join(REDACTED)
    for (const re of PATTERNS) out = out.replace(re, REDACTED)
    return out
  }
}
