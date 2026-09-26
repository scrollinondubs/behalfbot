// Reads the founder-os plugin tree baked into the image at a pinned tag+SHA
// (build-founder-os.sh, FOUNDER_OS_PIN). Only skills/, core/ and gates/ are
// ever read. contrib/ is opt-in per the plugin README and stays out.

import { existsSync, readFileSync, readdirSync, lstatSync } from 'node:fs'
import { join } from 'node:path'

export const SKILL_NAME_RE = /^founder-os-[a-z0-9]+(?:-[a-z0-9]+)*$/
const GATE_ID_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
export const SKILL_TYPES = new Set(['stage-skill', 'coach', 'auditor'])

export function readPin(pluginDir) {
  try {
    const pin = JSON.parse(readFileSync(join(pluginDir, '.pin.json'), 'utf8'))
    return pin && pin.pinned === true ? pin : { pinned: false }
  } catch {
    return { pinned: false }
  }
}

export function parseFrontmatter(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(text)
  if (!match) return null
  const meta = {}
  for (const line of match[1].split(/\r?\n/)) {
    const kv = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(line)
    if (!kv) continue
    let value = kv[2].trim()
    if (/^(['"]).*\1$/.test(value)) value = value.slice(1, -1)
    meta[kv[1]] = value
  }
  return { meta, body: match[2] }
}

// Returns null for anything that is not a well-formed skill of a known type,
// so the caller answers 404 without saying why.
export function loadSkill(pluginDir, name) {
  if (typeof name !== 'string' || !SKILL_NAME_RE.test(name)) return null
  const path = join(pluginDir, 'skills', name, 'SKILL.md')
  if (!existsSync(path) || !lstatSync(path).isFile()) return null
  const parsed = parseFrontmatter(readFileSync(path, 'utf8'))
  if (!parsed || parsed.meta.name !== name || !SKILL_TYPES.has(parsed.meta.type)) return null
  const stage = parsed.meta.stage === undefined ? null : Number(parsed.meta.stage)
  if (stage !== null && !(Number.isInteger(stage) && stage >= 0 && stage <= 9)) return null
  const gate = parsed.meta.gate && GATE_ID_RE.test(parsed.meta.gate) ? parsed.meta.gate : null
  return { name, type: parsed.meta.type, stage, gate, text: readFileSync(path, 'utf8') }
}

// Regular markdown files only: a symlink in the plugin tree must never become
// a way to read the rest of the container filesystem into a session.
function markdownFiles(dir) {
  if (!existsSync(dir) || !lstatSync(dir).isDirectory()) return []
  return readdirSync(dir)
    .filter(f => f.endsWith('.md'))
    .map(f => join(dir, f))
    .filter(p => lstatSync(p).isFile())
}

export function stageMaterials(pluginDir, stage, gate) {
  const cards = markdownFiles(join(pluginDir, 'core', `stage-${stage}`))
  const gatePath = gate ? join(pluginDir, 'gates', `${gate}.md`) : null
  const gates = gatePath && existsSync(gatePath) && lstatSync(gatePath).isFile() ? [gatePath] : []
  return { cards, gates }
}
