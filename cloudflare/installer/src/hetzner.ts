// Minimal Hetzner Cloud client for creating the customer's server in their
// own project, with the project token they gave us. There is deliberately no
// server delete here: closing an install must never touch the customer's
// server (CONTRACT.md hard requirement 7).

import type { FetchLike } from './types.ts'

const API = 'https://api.hetzner.cloud/v1'

export const SERVER_TYPE = 'cx23'
export const LOCATION = 'nbg1'
export const IMAGE = 'ubuntu-24.04'

export class HetznerError extends Error {
  readonly status: number
  constructor(status: number, method: string, path: string, detail: string) {
    super(`hetzner ${method} ${path} -> HTTP ${status}${detail ? `: ${detail}` : ''}`)
    this.status = status
  }
}

export interface CreatedServer {
  id: number
  ipv4: string
  name: string
}

export class HetznerClient {
  private readonly token: string
  private readonly fetchImpl: FetchLike

  constructor(o: { token: string; fetch: FetchLike }) {
    this.token = o.token
    this.fetchImpl = o.fetch
  }

  private async req(method: string, path: string, body?: unknown): Promise<unknown> {
    const res = await this.fetchImpl(
      new Request(`${API}${path}`, {
        method,
        headers: {
          authorization: `Bearer ${this.token}`,
          'user-agent': 'behalfbot-installer',
          ...(body === undefined ? {} : { 'content-type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
    )
    if (!res.ok) {
      // Error bodies are Hetzner's {error: {code, message}}; never echo the request.
      const j = (await res.json().catch(() => ({}))) as { error?: { code?: string; message?: string } }
      throw new HetznerError(res.status, method, path, [j.error?.code, j.error?.message].filter(Boolean).join(' ').slice(0, 200))
    }
    if (res.status === 204) return null
    return res.json()
  }

  async createSshKey(name: string, publicKey: string): Promise<number> {
    const r = (await this.req('POST', '/ssh_keys', { name, public_key: publicKey })) as { ssh_key: { id: number } }
    return r.ssh_key.id
  }

  // Only our own key object, registered just to inject it at create time.
  async deleteSshKey(id: number): Promise<void> {
    await this.req('DELETE', `/ssh_keys/${id}`)
  }

  async createServer(o: { name: string; sshKeyId: number; userData: string; labels: Record<string, string> }): Promise<CreatedServer> {
    const r = (await this.req('POST', '/servers', {
      name: o.name,
      server_type: SERVER_TYPE,
      location: LOCATION,
      image: IMAGE,
      ssh_keys: [o.sshKeyId],
      user_data: o.userData,
      labels: o.labels,
      start_after_create: true,
      public_net: { enable_ipv4: true, enable_ipv6: true },
    })) as { server: { id: number; name: string; public_net: { ipv4: { ip: string } | null } } }
    const ip = r.server.public_net.ipv4?.ip
    if (!ip) throw new Error(`hetzner created server ${r.server.id} without an IPv4 address`)
    return { id: r.server.id, ipv4: ip, name: r.server.name }
  }
}

// cloud-init that installs a host key we generated, so the fingerprint we pin
// is known before the box boots: no trust-on-first-use. The other key types
// cloud-init still generates are never trusted (the transport only accepts
// ssh-ed25519).
export function cloudInitWithHostKey(hostPrivatePem: string, hostPublicKey: string): string {
  const indented = hostPrivatePem.trimEnd().split('\n').map((l) => `    ${l}`).join('\n')
  return `#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
${indented}
  ed25519_public: ${hostPublicKey}
`
}

export function serverName(installId: string): string {
  return `bb-${installId.toLowerCase().replace(/_/g, '-')}`
}
