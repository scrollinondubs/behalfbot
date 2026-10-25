// Skill and stage material for a session, as VCL serves it over the session
// token (docs/founder-os-ledger-api.md, /content/skill and /content/stage).
// The image carries no FounderOS content of its own: everything the model
// reads comes from VCL at session time, so an edit there needs no release.

export const SKILL_NAME_RE = /^founder-os-[a-z0-9]+(?:-[a-z0-9]+)*$/
const GATE_ID_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

// Skill frontmatter type -> the executor's skill type, which keys the write
// allowlist (envelope.mjs). Content names its types stage-skill,
// auditor-skill, coach-skill and basic-skill. The bare coach and auditor
// names are the executor's own and stay accepted. A basic-skill also needs a
// role.
const TYPE_BY_FRONTMATTER = {
  'stage-skill': 'stage-skill',
  'coach-skill': 'coach',
  coach: 'coach',
  'auditor-skill': 'auditor',
  auditor: 'auditor',
}
const BASIC_TYPE_BY_ROLE = { coach: 'basic-coach', review: 'basic-review' }
export const SKILL_TYPES = new Set([...Object.keys(TYPE_BY_FRONTMATTER), 'basic-skill'])

// A skill is about 10 KB; this only stops a runaway answer filling the prompt.
export const MAX_SKILL_BYTES = 256 * 1024

function skillType(meta) {
  if (meta.type === 'basic-skill') return Object.hasOwn(BASIC_TYPE_BY_ROLE, meta.role) ? BASIC_TYPE_BY_ROLE[meta.role] : null
  return Object.hasOwn(TYPE_BY_FRONTMATTER, meta.type) ? TYPE_BY_FRONTMATTER[meta.type] : null
}

export function isBasicSkill(skill) {
  return skill.track === 'basic'
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

// Returns null for anything that is not a well-formed skill of a known type
// named `name`, so the caller answers 404 without saying why.
export function parseSkill(name, text) {
  if (typeof name !== 'string' || !SKILL_NAME_RE.test(name)) return null
  if (typeof text !== 'string' || Buffer.byteLength(text, 'utf8') > MAX_SKILL_BYTES) return null
  const parsed = parseFrontmatter(text)
  if (!parsed || parsed.meta.name !== name) return null
  const type = skillType(parsed.meta)
  if (!type) return null
  const stage = parsed.meta.stage === undefined ? null : Number(parsed.meta.stage)
  if (stage !== null && !(Number.isInteger(stage) && stage >= 0 && stage <= 9)) return null
  const gate = parsed.meta.gate && GATE_ID_RE.test(parsed.meta.gate) ? parsed.meta.gate : null
  const track = type.startsWith('basic-') ? 'basic' : 'advanced'
  return { name, type, track, stage, gate, text }
}

const SLUG = '[a-z0-9]+(?:-[a-z0-9]+)*'

// Where each file VCL serves for a stage goes in the session workdir. Basic
// keeps VCL's paths (basic/stage-N/, basic/gates/), which the Basic skills
// name. Advanced keeps the layout its skills were written against: cards/
// for core/stage-N, gate-<file> for gates/stage-N-<slug>.md.
export function stageLayout(stage, skill) {
  if (isBasicSkill(skill)) {
    return {
      card: new RegExp(`^basic/stage-${stage}/${SLUG}\\.md$`),
      gate: new RegExp(`^basic/gates/stage-${stage}-${SLUG}\\.md$`),
      to: path => path,
    }
  }
  return {
    card: new RegExp(`^core/stage-${stage}/${SLUG}\\.md$`),
    gate: new RegExp(`^gates/stage-${stage}-${SLUG}\\.md$`),
    to: path => (path.startsWith('core/') ? `cards/${path.split('/').at(-1)}` : `gate-${path.split('/').at(-1)}`),
  }
}
