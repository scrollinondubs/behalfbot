// Images a founder attached to a Basic card submission (VCL stores them in
// Vercel Blob and puts the URL in the artifact's meta.blob_url). The model
// has no network, so the shim downloads the image into the session workdir
// and the prompt names the local path, which Read opens as an image.
//
// The URL is founder-influenced data, so the fetch is narrow: https only,
// Vercel Blob public hosts only, no redirects, no headers that carry
// anything, the content type VCL recorded, a byte cap enforced while
// streaming, and the same magic-byte check VCL ran on upload. A failure never
// fails the session; the prompt says the image could not be loaded.

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

// VCL's MAX_CARD_FILE_BYTES (src/lib/founderos/files.ts).
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024
export const IMAGE_FETCH_TIMEOUT_MS = 20_000

const BLOB_HOST_RE = /^[a-z0-9-]+\.public\.blob\.vercel-storage\.com$/

const ascii = (b, from, to) => String.fromCharCode(...b.subarray(from, to))

export const IMAGE_TYPES = {
  'image/png': { ext: 'png', magic: b => b[0] === 0x89 && ascii(b, 1, 4) === 'PNG' },
  'image/jpeg': { ext: 'jpg', magic: b => b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff },
  'image/webp': { ext: 'webp', magic: b => ascii(b, 0, 4) === 'RIFF' && ascii(b, 8, 12) === 'WEBP' },
}

export class AttachmentError extends Error {
  constructor(code) {
    super(code)
    this.name = 'AttachmentError'
    this.code = code
  }
}

function metaOf(artifact) {
  const meta = artifact?.meta
  if (typeof meta === 'string') {
    try {
      return JSON.parse(meta)
    } catch {
      return {}
    }
  }
  return meta && typeof meta === 'object' ? meta : {}
}

// The image an artifact points at, or null when it carries none.
export function imageRef(artifact) {
  const meta = metaOf(artifact)
  if (typeof meta.blob_url !== 'string') return null
  const contentType = typeof meta.content_type === 'string' ? meta.content_type.toLowerCase() : ''
  if (!Object.hasOwn(IMAGE_TYPES, contentType)) return null
  return { url: meta.blob_url, contentType }
}

export function checkBlobUrl(raw) {
  let url
  try {
    url = new URL(raw)
  } catch {
    throw new AttachmentError('bad_url')
  }
  if (url.protocol !== 'https:') throw new AttachmentError('not_https')
  if (url.username || url.password || url.port) throw new AttachmentError('bad_url')
  if (!BLOB_HOST_RE.test(url.hostname)) throw new AttachmentError('host_not_allowed')
  return url
}

export async function fetchImage(rawUrl, contentType, { fetchImpl = fetch, timeoutMs = IMAGE_FETCH_TIMEOUT_MS, maxBytes = MAX_IMAGE_BYTES } = {}) {
  const url = checkBlobUrl(rawUrl)
  const kind = IMAGE_TYPES[contentType]
  if (!kind) throw new AttachmentError('not_an_image')
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    let res
    try {
      res = await fetchImpl(url.href, { redirect: 'error', signal: controller.signal })
    } catch {
      throw new AttachmentError(controller.signal.aborted ? 'timeout' : 'fetch_failed')
    }
    if (res.redirected || (res.status >= 300 && res.status < 400)) throw new AttachmentError('redirect')
    if (!res.ok) throw new AttachmentError(`http_${res.status}`)
    const served = (res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase()
    if (served !== contentType) throw new AttachmentError('content_type_mismatch')
    if (Number(res.headers.get('content-length')) > maxBytes) throw new AttachmentError('too_large')

    const chunks = []
    let size = 0
    if (res.body) {
      const reader = res.body.getReader()
      for (;;) {
        let step
        try {
          step = await reader.read()
        } catch {
          throw new AttachmentError(controller.signal.aborted ? 'timeout' : 'fetch_failed')
        }
        if (step.done) break
        size += step.value.byteLength
        if (size > maxBytes) {
          controller.abort()
          throw new AttachmentError('too_large')
        }
        chunks.push(step.value)
      }
    }
    const bytes = Buffer.concat(chunks)
    if (bytes.length < 12 || !kind.magic(bytes)) throw new AttachmentError('bad_image')
    return bytes
  } finally {
    clearTimeout(timer)
  }
}

// Downloads every referenced image into <workdir>/attachments/ and returns
// the referenced list with each image artifact annotated: attached_image is
// the absolute path the model can Read, attached_image_error says why not.
export async function stageImageAttachments(referenced, workdir, opts = {}) {
  const out = []
  for (const [i, artifact] of referenced.entries()) {
    const ref = imageRef(artifact)
    if (!ref) {
      out.push(artifact)
      continue
    }
    try {
      const bytes = await fetchImage(ref.url, ref.contentType, opts)
      const dir = join(workdir, 'attachments')
      mkdirSync(dir, { recursive: true })
      const path = join(dir, `ref-${i + 1}.${IMAGE_TYPES[ref.contentType].ext}`)
      writeFileSync(path, bytes)
      out.push({ ...artifact, attached_image: path })
    } catch (err) {
      out.push({ ...artifact, attached_image_error: err instanceof AttachmentError ? err.code : 'fetch_failed' })
    }
  }
  return out
}
