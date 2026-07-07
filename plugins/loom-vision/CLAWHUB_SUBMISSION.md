# ClawHub Submission Runbook - Loom Vision

Everything needed to publish the Loom Vision skill to [ClawHub.ai](https://clawhub.ai), OpenClaw's public registry. Prepared 2026-07-07 by Jax. Nothing has been submitted - every step below is manual and yours.

Verified against the live docs on 2026-07-07:
- https://docs.openclaw.ai/clawhub (overview)
- https://docs.openclaw.ai/clawhub/publishing
- https://docs.openclaw.ai/clawhub/skill-format
- https://docs.openclaw.ai/clawhub/cli
- https://docs.openclaw.ai/clawhub/auth

## What ClawHub expects

ClawHub hosts two artifact types: **skills** (versioned text bundles: a folder with `SKILL.md` plus supporting text files) and **plugins** (npm-style packages with `package.json` + `openclaw.compat.pluginApi` metadata). Loom Vision is a skill bundle - SKILL.md + one shell script - so we submit it as a **skill**, not a plugin. That path needs no package.json, no npm scope, and no `openclaw.plugin.json` (that file stays chassis-only).

Submission is via the `clawhub` CLI (`clawhub skill publish <folder>`), authenticated through GitHub sign-in at clawhub.ai. There is no PR-to-a-registry-repo flow and no web upload form for local folders (the web GitHub importer exists but only scans public non-fork repos for SKILL.md files - the CLI is the recommended and more controllable path).

## Staged files (this PR)

| File | Purpose |
|---|---|
| `clawhub/loom-vision/SKILL.md` | The publishable skill definition. ClawHub parses its YAML frontmatter for registry metadata: `name`, `description` (becomes the search/UI summary), `version`, and `metadata.openclaw` (emoji, homepage, `requires.bins`, install specs for loom-dl + ffmpeg, and optional `envVars`). The env vars the script reads are declared with `required: false` because ClawHub's automated security analysis flags undeclared env var usage as a metadata mismatch. Chassis-only frontmatter (`plugin:`, `enabled_when:`) removed. |
| `clawhub/loom-vision/process-loom.sh` | Standalone copy of the chassis script. Only differences from the canonical `skills/loom-vision/process-loom.sh`: default `OUTPUT_ROOT` is `${TMPDIR:-/tmp}/loom-vision` instead of `${CHASSIS_HOME}/temp`, and chassis/setup.sh references are gone from comments. Publishing uploads the whole folder, so this ships inside the bundle - `{baseDir}` in SKILL.md resolves to it at runtime. |
| `clawhub/loom-vision/README.md` | Optional supporting file (allowed; only text files are accepted). Chassis install instructions replaced with `clawhub install`, plus the Behalf.bot pointer. No license section - see the licensing note below. |
| `CLAWHUB_SUBMISSION.md` | This runbook. Not part of the published bundle (it lives one level above the skill folder, and publish only uploads `clawhub/loom-vision/`). |

The chassis plugin (`openclaw.plugin.json`, `setup.sh`, `skills/loom-vision/`) is untouched. The `clawhub/loom-vision/` folder is the exact directory you point the publish command at; its folder name becomes the default slug (`loom-vision`).

## Decide before you publish: licensing

ClawHub publishes **all** skills under **MIT-0** (public domain equivalent: anyone may use, modify, redistribute, commercially, no attribution). Per-skill license overrides are not supported, and conflicting license terms in SKILL.md are explicitly disallowed. This repo is O'Saasy (MIT + SaaS-compete restriction). You are the copyright holder, so publishing is effectively dual-licensing this one bundle as MIT-0. If you are not comfortable releasing Loom Vision under MIT-0, stop here - there is no other license option on ClawHub.

## Submit checklist

1. Merge this PR (or check out the branch) so `plugins/loom-vision/clawhub/loom-vision/` exists on disk.
2. Confirm the licensing decision above: publishing releases this bundle under MIT-0.
3. Create the account: go to https://clawhub.ai and click "Sign in with GitHub". Use your `scrollinondubs` GitHub account (uploads require a GitHub account old enough to pass ClawHub's anti-abuse gate; a years-old account passes). Your publisher handle is derived from the GitHub account.
4. Install the CLI: `npm i -g clawhub` (Node required). Verify with `clawhub --help`.
5. Authenticate: run `clawhub login` - it opens the browser to clawhub.ai/cli/auth, you complete GitHub sign-in, and the CLI stores an API token in `~/Library/Application Support/clawhub/config.json`. (Headless alternative: `clawhub login --device`.)
6. Verify auth: `clawhub whoami` should print your handle. Note it - it is the `@owner` in the published URL.
7. Dry run from the repo root:
   `clawhub skill publish plugins/loom-vision/clawhub/loom-vision --dry-run`
   This resolves the full publish plan (slug `loom-vision`, version `1.0.0` for a new skill, file list) without uploading. Fix anything it flags before continuing.
8. Publish:
   `clawhub skill publish plugins/loom-vision/clawhub/loom-vision --slug loom-vision --name "Loom Vision"`
   No `--version` needed - new skills start at 1.0.0. No `--owner` needed unless you want to publish under an org publisher handle instead of your personal one (org scope must match an owner you control).
9. Expect a security-scan hold: ClawHub runs automated checks on new releases, and a new skill may stay out of public catalog/install surfaces until scanning and verification finish. Track status at https://clawhub.ai/dashboard (owner-visible even while held). The declared env vars and bins in the frontmatter were written to match exactly what process-loom.sh uses, so the metadata-mismatch check should pass.
10. Verify it is live:
    - `clawhub inspect @<your-handle>/loom-vision --files` (metadata + file list)
    - open `https://clawhub.ai/<your-handle>/loom-vision`
11. Test a real install in a scratch directory:
    `cd $(mktemp -d) && clawhub install @<your-handle>/loom-vision && cat skills/loom-vision/SKILL.md`
    Or the native flow on an OpenClaw machine: `openclaw skills install @<your-handle>/loom-vision`.
12. Future updates: edit the files under `plugins/loom-vision/clawhub/loom-vision/`, then re-run the publish command - changed content auto-bumps the next patch version (pass `--version` for minor/major). Optional automation: ClawHub's reusable GitHub Actions workflow `openclaw/clawhub/.github/workflows/skill-publish.yml` with a `CLAWHUB_TOKEN` repo secret (get the token via `clawhub token`).

## Gotchas

- Keep `clawhub/loom-vision/` in sync with the canonical chassis script by hand - the two copies intentionally differ only in OUTPUT_ROOT default and comments.
- Do not add pricing or license text to SKILL.md; ClawHub rejects/ignores both.
- Only text-based files are accepted in the bundle; the .sh is fine (scripts are scanned after upload). Bundle limit 50MB - ours is ~12KB.
- `CLAWHUB_DISABLE_TELEMETRY=1` disables the CLI's install-count telemetry if you care.
