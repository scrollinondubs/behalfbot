// Bearer check for the site and admin routes. Both sides are hashed before
// comparing so neither length nor content leaks through timing, and a
// missing or empty configured secret never matches anything.

const enc = new TextEncoder()

async function sha256(s: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(s)))
}

export async function bearerMatches(header: string | null, secret: string | undefined): Promise<boolean> {
  if (typeof secret !== 'string' || secret.length === 0) return false
  if (typeof header !== 'string') return false
  const m = /^Bearer (.+)$/.exec(header)
  const presented = m ? m[1] : ''
  const [a, b] = await Promise.all([sha256(presented), sha256(secret)])
  let diff = presented.length === 0 ? 1 : 0
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i]
  return diff === 0
}
