const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

export const INSTALL_ID_RE = /^inst_[0-9A-HJKMNP-TV-Z]{26}$/

export function ulid(nowMs: number): string {
  let time = ''
  let t = nowMs
  for (let i = 0; i < 10; i++) {
    time = CROCKFORD[t % 32] + time
    t = Math.floor(t / 32)
  }
  const rnd = crypto.getRandomValues(new Uint8Array(16))
  let rand = ''
  for (let i = 0; i < 16; i++) rand += CROCKFORD[rnd[i] % 32]
  return time + rand
}

export function newInstallId(nowMs: number): string {
  return `inst_${ulid(nowMs)}`
}
