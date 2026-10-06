// Reads the founder-os plugin tree baked into the image at a pinned tag+SHA
// (build-founder-os.sh, FOUNDER_OS_PIN). Only skills/, core/, gates/ and
// basic/ are ever read. contrib/ is opt-in per the plugin README and stays out.

import { existsSync, readFileSync, readdirSync, lstatSync } from 'node:fs'
import { basename, join } from 'node:path'

export const SKILL_NAME_RE = /^founder-os-[a-z0-9]+(?:-[a-z0-9]+)*$/
const GATE_ID_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

// Plugin frontmatter type -> the executor's skill type, which keys the write
// allowlist (envelope.mjs). The plugin names its types stage-skill,
// auditor-skill, coach-skill and basic-skill (behalfbot-plugins
// scripts/lint_content.py SKILL_KINDS). The bare coach and auditor names are
// the executor's own and stay accepted. A basic-skill also needs a role.
const TYPE_BY_FRONTMATTER = {
  'stage-skill': 'stage-skill',
  'coach-skill': 'coach',
  coach: 'coach',
  'auditor-skill': 'auditor',
  auditor: 'auditor',
}
const BASIC_TYPE_BY_ROLE = { coach: 'basic-coach', review: 'basic-review' }
export const SKILL_TYPES = new Set([...Object.keys(TYPE_BY_FRONTMATTER), 'basic-skill'])

function skillType(meta) {
  if (meta.type === 'basic-skill') return Object.hasOwn(BASIC_TYPE_BY_ROLE, meta.role) ? BASIC_TYPE_BY_ROLE[meta.role] : null
  return Object.hasOwn(TYPE_BY_FRONTMATTER, meta.type) ? TYPE_BY_FRONTMATTER[meta.type] : null
}

export function isBasicSkill(skill) {
  return skill.track === 'basic'
}

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
  if (!parsed || parsed.meta.name !== name) return null
  const type = skillType(parsed.meta)
  if (!type) return null
  const stage = parsed.meta.stage === undefined ? null : Number(parsed.meta.stage)
  if (stage !== null && !(Number.isInteger(stage) && stage >= 0 && stage <= 9)) return null
  const gate = parsed.meta.gate && GATE_ID_RE.test(parsed.meta.gate) ? parsed.meta.gate : null
  const track = type.startsWith('basic-') ? 'basic' : 'advanced'
  return { name, type, track, stage, gate, text: readFileSync(path, 'utf8') }
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

// Advanced skills get core/stage-N cards and their own gate. Basic skills
// name no gate: they get basic/stage-N cards (Coach checks included) and the
// stage's panel, basic/gates/stage-N-<slug>.md. Each path comes back with the
// path relative to the session workdir it is copied to.
export function stageMaterials(pluginDir, stage, skill) {
  if (isBasicSkill(skill)) {
    const cards = markdownFiles(join(pluginDir, 'basic', `stage-${stage}`))
      .map(p => ({ from: p, to: join('basic', `stage-${stage}`, basename(p)) }))
    const gates = markdownFiles(join(pluginDir, 'basic', 'gates'))
      .filter(p => basename(p).startsWith(`stage-${stage}-`))
      .map(p => ({ from: p, to: join('basic', 'gates', basename(p)) }))
    return { cards, gates }
  }
  const gate = skill.gate
  const cards = markdownFiles(join(pluginDir, 'core', `stage-${stage}`))
    .map(p => ({ from: p, to: join('cards', basename(p)) }))
  const gatePath = gate ? join(pluginDir, 'gates', `${gate}.md`) : null
  const gates = gatePath && existsSync(gatePath) && lstatSync(gatePath).isFile()
    ? [{ from: gatePath, to: `gate-${basename(gatePath)}` }]
    : []
  return { cards, gates }
}
