// Interface 3 (CONTRACT.md): every command on a customer's box goes through
// BoxTransport.run, so the transport can change without touching callers.
//
// v1 is SSH from the installer container (SPIKE.md). The Worker side pins
// the target here; the container re-checks the host key it actually sees
// against the same pin before it will connect (container/ssh.mjs). Secrets
// travel only in the request body's stdin field, never in cmd.

export interface RunOpts {
  stdin?: Uint8Array
  timeoutMs?: number
}

export interface RunResult {
  code: number
  stdout: string
  stderr: string
}

export interface BoxTransport {
  run(cmd: string, opts?: RunOpts): Promise<RunResult>
}

export interface BoxPin {
  serverIp: string
  hostKeyFp: string
}

export class HostPinError extends Error {}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/
const IPV6 = /^[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}$/
export const HOST_KEY_FP = /^SHA256:[A-Za-z0-9+/]{43}$/
const SSH_USER = /^[a-z_][a-z0-9_-]{0,31}$/

// A literal IP only: a hostname would let DNS decide where we connect.
export function isLiteralIp(s: unknown): s is string {
  return typeof s === 'string' && (IPV4.test(s) || IPV6.test(s))
}

export function assertPinned(pin: Partial<BoxPin> | null | undefined): asserts pin is BoxPin {
  if (!pin || !isLiteralIp(pin.serverIp)) throw new HostPinError('refusing: server_ip is not pinned to a literal IP')
  if (typeof pin.hostKeyFp !== 'string' || !HOST_KEY_FP.test(pin.hostKeyFp)) {
    throw new HostPinError('refusing: host_key_fp is not pinned (want SHA256:<43 base64 chars>)')
  }
}

function b64(bytes: Uint8Array): string {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

export const DEFAULT_TIMEOUT_MS = 120_000
export const MAX_TIMEOUT_MS = 30 * 60_000

export class SshTransport implements BoxTransport {
  private readonly pin: BoxPin
  private readonly user: string
  private readonly privateKeyPem: string
  private readonly send: (req: Request) => Promise<Response>

  constructor(o: { pin: Partial<BoxPin>; user: string; privateKeyPem: string; send: (req: Request) => Promise<Response> }) {
    assertPinned(o.pin)
    if (!SSH_USER.test(o.user)) throw new HostPinError('refusing: bad ssh user')
    if (!o.privateKeyPem) throw new Error('no install key: it was never generated or was deleted at close')
    this.pin = { serverIp: o.pin.serverIp, hostKeyFp: o.pin.hostKeyFp }
    this.user = o.user
    this.privateKeyPem = o.privateKeyPem
    this.send = o.send
  }

  // Hard pin: the only host this transport will ever reach. There is no way
  // to pass a different one to run().
  target(): BoxPin {
    return { ...this.pin }
  }

  async run(cmd: string, opts: RunOpts = {}): Promise<RunResult> {
    const timeoutMs = Math.min(Math.max(opts.timeoutMs ?? DEFAULT_TIMEOUT_MS, 1000), MAX_TIMEOUT_MS)
    const res = await this.send(
      new Request('http://ssh-box/run', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          host: this.pin.serverIp,
          port: 22,
          user: this.user,
          host_key_fp: this.pin.hostKeyFp,
          private_key: this.privateKeyPem,
          cmd,
          stdin_b64: opts.stdin ? b64(opts.stdin) : null,
          timeout_ms: timeoutMs,
        }),
      }),
    )
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>
    if (res.status === 403 && body.error === 'host_key_mismatch') {
      throw new HostPinError(`refusing: ${this.pin.serverIp} presented a host key that does not match the pin`)
    }
    if (!res.ok) throw new Error(`ssh-box answered HTTP ${res.status}: ${String(body.error ?? 'unknown')}`)
    // Belt and braces: the container must report the fingerprint it verified.
    if (body.host_key_fp !== this.pin.hostKeyFp) {
      throw new HostPinError('refusing: ssh-box did not confirm the pinned host key')
    }
    return {
      code: typeof body.code === 'number' ? body.code : -1,
      stdout: typeof body.stdout === 'string' ? body.stdout : '',
      stderr: typeof body.stderr === 'string' ? body.stderr : '',
    }
  }
}
