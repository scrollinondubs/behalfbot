// In-container HTTP shim for the installer's SSH transport. Only the install
// DO reaches it, through the SSH_BOX container binding.

import http from 'node:http'
import { runOnBox, RefusedError } from './ssh.mjs'

const MAX_BODY = 4 * 1024 * 1024

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(JSON.stringify(body))
}

http
  .createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/healthz') return send(res, 200, { ok: true })
    if (req.method !== 'POST' || req.url !== '/run') return send(res, 404, { error: 'not_found' })
    const chunks = []
    let size = 0
    req.on('data', (c) => {
      size += c.length
      if (size > MAX_BODY) req.destroy()
      else chunks.push(c)
    })
    req.on('end', async () => {
      let body
      try {
        body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      } catch {
        return send(res, 400, { error: 'bad_json' })
      }
      try {
        send(res, 200, await runOnBox(body))
      } catch (e) {
        if (e instanceof RefusedError) {
          return send(res, e.code === 'host_key_mismatch' ? 403 : 400, { error: e.code, ...e.extra })
        }
        // Message only, never the body: it carries the key and stdin.
        console.error('run failed:', e instanceof Error ? e.message : String(e))
        send(res, 500, { error: 'run_failed' })
      }
    })
  })
  .listen(8080)
