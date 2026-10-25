// Synthetic skills and stage cards the mock VCL serves (mock-vcl.mjs). Lorem
// placeholders only: they exercise the executor's mechanics (frontmatter,
// skill types, paths, workdir layout) and carry no real course content.

const skill = (name, fields, body) =>
  ['---', `name: ${name}`, 'description: Test fixture for the executor tests.', ...fields, '---', '', body, ''].join('\n')

export const SKILLS = {
  'founder-os-stage-1-alpha': skill('founder-os-stage-1-alpha', ['type: stage-skill', 'stage: 1', 'gate: stage-1-alpha'],
    '# Stage 1 alpha (fixture)\n\nLorem ipsum dolor sit amet. Record one artifact with add_artifact.'),
  'founder-os-stage-1-auditor': skill('founder-os-stage-1-auditor', ['type: auditor', 'stage: 1', 'gate: stage-1-alpha'],
    '# Stage 1 auditor (fixture)\n\nConsectetur adipiscing elit. Record audits and a gate decision.'),
  'founder-os-stage-1-checker': skill('founder-os-stage-1-checker', ['type: auditor-skill', 'stage: 1', 'gate: stage-1-alpha'],
    '# Stage 1 checker (fixture)\n\nSed do eiusmod tempor.'),
  'founder-os-coach-fixture': skill('founder-os-coach-fixture', ['type: coach-skill'],
    '# Coach (fixture)\n\nUt enim ad minim veniam.'),
  'founder-os-basic-coach': skill('founder-os-basic-coach', ['type: basic-skill', 'track: basic', 'role: coach'],
    '# Basic coach (fixture)\n\nRead $FOUNDER_OS_DIR/basic/stage-<N>/*.md and coach the current card.'),
  'founder-os-basic-review': skill('founder-os-basic-review', ['type: basic-skill', 'track: basic', 'role: review'],
    '# Basic review (fixture)\n\nReview one submission against its card and end with a founderos-verdict block.'),
  'founder-os-basic-judge': skill('founder-os-basic-judge', ['type: basic-skill', 'track: basic', 'role: judge'],
    '# Unknown role (fixture)'),
}

const card = (id, stage, order, gate) =>
  [
    '---', `id: ${id}`, 'type: card', 'track: basic', `stage: ${stage}`, `order: ${order}`, `title: ${id} (fixture)`, `gate: ${gate}`,
    'submit: [text, file]', '---', '## What this is', 'Lorem ipsum card.', '', '## Coach checks', `- COACH CHECK ${id}`, '',
  ].join('\n')
const gate = (id, stage) =>
  ['---', `id: ${id}`, 'type: gate', 'track: basic', `stage: ${stage}`, `title: ${id} (fixture)`, 'signoff: coach', '---', '## What this is', 'Lorem ipsum panel.', ''].join('\n')

// What VCL's /content/stage answers, by track and stage, at VCL's paths.
export const STAGE_FILES = {
  basic: {
    0: [
      { path: 'basic/stage-0/lorem-start.md', text: card('basic-lorem-start', 0, 1, 'basic-stage-0-lorem') },
      { path: 'basic/gates/stage-0-lorem.md', text: gate('basic-stage-0-lorem', 0) },
    ],
    1: [
      { path: 'basic/stage-1/alpha-card.md', text: card('basic-alpha-card', 1, 1, 'basic-stage-1-ipsum') },
      { path: 'basic/stage-1/beta-card.md', text: card('basic-beta-card', 1, 2, 'basic-stage-1-ipsum') },
      { path: 'basic/stage-1/gamma-card.md', text: card('basic-gamma-card', 1, 3, 'basic-stage-1-ipsum') },
      { path: 'basic/gates/stage-1-ipsum.md', text: gate('basic-stage-1-ipsum', 1) },
    ],
  },
  advanced: {
    1: [
      { path: 'core/stage-1/dolor-card.md', text: '# Dolor card (fixture)\n\nLorem ipsum dolor.\n' },
      { path: 'gates/stage-1-alpha.md', text: '# Gate: stage 1 alpha (fixture)\n\nNeeds one artifact.\n' },
    ],
  },
}

export const CONTENT_SHA = 'f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1'
