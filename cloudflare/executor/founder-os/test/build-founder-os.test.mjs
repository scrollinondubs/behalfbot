// build-founder-os.sh against a local plugins repo (behalfbot#215). Offline:
// the repo URL is a file:// clone, so the tag-to-SHA check is exercised for
// real without GitHub. Needs git and trash on PATH, as the build host does.

import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'build-founder-os.sh')
const hasTrash = spawnSync('sh', ['-c', 'command -v trash']).status === 0

let root, repo, goodSha, bareSha
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8' }).trim()

before(() => {
  root = mkdtempSync(join(tmpdir(), 'fos-build-'))
  repo = join(root, 'plugins')
  mkdirSync(repo)
  git('init', '-q', '-b', 'main')
  git('config', 'user.email', 'test@example.com')
  git('config', 'user.name', 'test')
  writeFileSync(join(repo, 'README.md'), 'no founder-os here\n')
  git('add', '.')
  git('commit', '-q', '-m', 'no founder-os')
  bareSha = git('rev-parse', 'HEAD')
  git('tag', 'v0.1.0')
  mkdirSync(join(repo, 'founder-os', 'skills', 'founder-os-stage-1-audience'), { recursive: true })
  writeFileSync(join(repo, 'founder-os', 'skills', 'founder-os-stage-1-audience', 'SKILL.md'), '---\nname: x\n---\n')
  git('add', '.')
  git('commit', '-q', '-m', 'founder-os')
  goodSha = git('rev-parse', 'HEAD')
  git('tag', '-a', 'v0.2.0', '-m', 'annotated')
})

after(() => rmSync(root, { recursive: true, force: true }))

function run(pinLine) {
  const out = mkdtempSync(join(root, 'out-'))
  const pinFile = join(out, 'PIN')
  writeFileSync(pinFile, `# comment\n${pinLine}\n`)
  const r = spawnSync('bash', [SCRIPT], {
    encoding: 'utf8',
    env: { ...process.env, FOUNDER_OS_PIN_FILE: pinFile, FOUNDER_OS_PLUGINS_REPO_URL: `file://${repo}`, FOUNDER_OS_CONTEXT_PARENT: out },
  })
  const pinJson = join(out, 'founder-os-context', 'plugin', '.pin.json')
  return {
    status: r.status,
    stderr: r.stderr,
    pin: existsSync(pinJson) ? JSON.parse(readFileSync(pinJson, 'utf8')) : null,
    out,
    leftovers: readdirSync(out).filter(f => f.startsWith('.founder-os-staging.')),
  }
}

test('unpinned stages an empty plugin marked pinned:false', { skip: !hasTrash }, () => {
  const r = run('')
  assert.equal(r.status, 0, r.stderr)
  assert.deepEqual(r.pin, { pinned: false })
  assert.deepEqual(r.leftovers, [])
})

test('a good annotated tag stages founder-os at the pinned SHA, without .git', { skip: !hasTrash }, () => {
  const r = run(`v0.2.0 ${goodSha}`)
  assert.equal(r.status, 0, r.stderr)
  assert.deepEqual(r.pin, { pinned: true, tag: 'v0.2.0', sha: goodSha })
  const plugin = join(r.out, 'founder-os-context', 'plugin')
  assert.ok(existsSync(join(plugin, 'skills', 'founder-os-stage-1-audience', 'SKILL.md')))
  assert.ok(!existsSync(join(plugin, '.git')))
  assert.ok(!existsSync(join(r.out, 'founder-os-context', 'clone')))
  assert.deepEqual(r.leftovers, [])
})

test('a moved tag is refused with exit 3 and the previous context survives', { skip: !hasTrash }, () => {
  const first = run('')
  const pinFile = join(first.out, 'PIN')
  writeFileSync(pinFile, `v0.2.0 ${bareSha}\n`)
  const r = spawnSync('bash', [SCRIPT], {
    encoding: 'utf8',
    env: { ...process.env, FOUNDER_OS_PIN_FILE: pinFile, FOUNDER_OS_PLUGINS_REPO_URL: `file://${repo}`, FOUNDER_OS_CONTEXT_PARENT: first.out },
  })
  assert.equal(r.status, 3, r.stderr)
  assert.match(r.stderr, /SECURITY/)
  const kept = JSON.parse(readFileSync(join(first.out, 'founder-os-context', 'plugin', '.pin.json'), 'utf8'))
  assert.deepEqual(kept, { pinned: false })
  assert.deepEqual(readdirSync(first.out).filter(f => f.startsWith('.founder-os-staging.')), [])
})

test('malformed pins are refused with exit 3', { skip: !hasTrash }, () => {
  for (const line of ['v0.2.0', `main ${goodSha}`, 'v0.2.0 abc123', `v0.2.0 ${goodSha.toUpperCase()}`]) {
    const r = run(line)
    assert.equal(r.status, 3, `${line}: ${r.stderr}`)
    assert.equal(r.pin, null)
  }
})

test('a tag without founder-os/ is refused with exit 4', { skip: !hasTrash }, () => {
  const r = run(`v0.1.0 ${bareSha}`)
  assert.equal(r.status, 4, r.stderr)
  assert.equal(r.pin, null)
  assert.deepEqual(r.leftovers, [])
})
