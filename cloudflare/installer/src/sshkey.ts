// Per-install ed25519 key (hard requirement 2), generated in the Worker with
// WebCrypto and serialised as an unencrypted openssh-key-v1 private key plus
// an authorized_keys line. The public half is injected at server create; the
// private half lives in this install's DO storage until close.

export interface InstallKey {
  publicKey: string
  privateKeyPem: string
}

const enc = new TextEncoder()

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let o = 0
  for (const p of parts) {
    out.set(p, o)
    o += p.length
  }
  return out
}

function u32(n: number): Uint8Array {
  const b = new Uint8Array(4)
  new DataView(b.buffer).setUint32(0, n >>> 0)
  return b
}

function sshString(data: Uint8Array | string): Uint8Array {
  const d = typeof data === 'string' ? enc.encode(data) : data
  return concat(u32(d.length), d)
}

function b64(bytes: Uint8Array): string {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

export function encodeOpensshEd25519(seed: Uint8Array, pub: Uint8Array, comment: string, check: number): InstallKey {
  if (seed.length !== 32 || pub.length !== 32) throw new Error('ed25519 seed and public key must be 32 bytes')
  const pubBlob = concat(sshString('ssh-ed25519'), sshString(pub))
  let priv = concat(
    u32(check),
    u32(check),
    sshString('ssh-ed25519'),
    sshString(pub),
    sshString(concat(seed, pub)),
    sshString(comment),
  )
  const pad: number[] = []
  for (let i = 1; (priv.length + pad.length) % 8 !== 0; i++) pad.push(i)
  priv = concat(priv, new Uint8Array(pad))
  const body = concat(
    enc.encode('openssh-key-v1\0'),
    sshString('none'),
    sshString('none'),
    sshString(''),
    u32(1),
    sshString(pubBlob),
    sshString(priv),
  )
  const lines = b64(body).match(/.{1,70}/g) ?? []
  return {
    publicKey: `ssh-ed25519 ${b64(pubBlob)} ${comment}`,
    privateKeyPem: `-----BEGIN OPENSSH PRIVATE KEY-----\n${lines.join('\n')}\n-----END OPENSSH PRIVATE KEY-----\n`,
  }
}

export function keyComment(installId: string): string {
  return `behalfbot-installer-${installId}`
}

export async function generateInstallKey(installId: string): Promise<InstallKey> {
  return generateEd25519(keyComment(installId))
}

// "SHA256:<unpadded base64 of sha256(key blob)>", the string `ssh-keygen -lf`
// prints, from an authorized_keys-style public key line.
export async function fingerprintOf(publicKeyLine: string): Promise<string> {
  const blob = publicKeyLine.trim().split(/\s+/)[1] ?? ''
  const bytes = Uint8Array.from(atob(blob), (c) => c.charCodeAt(0))
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
  return `SHA256:${b64(digest).replace(/=+$/, '')}`
}

export async function generateEd25519(comment: string): Promise<InstallKey> {
  const kp = (await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])) as CryptoKeyPair
  const pub = new Uint8Array((await crypto.subtle.exportKey('raw', kp.publicKey)) as ArrayBuffer)
  const pkcs8 = new Uint8Array((await crypto.subtle.exportKey('pkcs8', kp.privateKey)) as ArrayBuffer)
  const seed = pkcs8.slice(pkcs8.length - 32)
  const check = new DataView(crypto.getRandomValues(new Uint8Array(4)).buffer).getUint32(0)
  return encodeOpensshEd25519(seed, pub, comment, check)
}
