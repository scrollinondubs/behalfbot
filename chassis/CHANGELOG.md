# Chassis Changelog

Single source of truth for chassis releases. Read by the auto-updater (issue #33) to surface diffs and detect breaking changes.

## Format conventions

Each release is a `## vX.Y.Z` heading. Under it:

- **Added** / **Changed** / **Fixed** / **Removed** subsections (any subset)
- **BREAKING CHANGES:** marker (uppercase, on its own line) when a release requires manual review before applying. The auto-updater notification flips into `--force` mode whenever this marker appears for an unreleased version between the customer's `current` and `latest`.
- **Migration:** subsection pointing at `scripts/chassis-migrations/vX.Y.Z.sh` if the bump needs a state migration. Migration scripts are strictly automated shell scripts only (no Claude judgment, no LLM calls). Judgment-heavy migrations get flagged as BREAKING CHANGES and gated behind explicit operator review.

Semver:

- `MAJOR` (`1.0.0` → `2.0.0`): always BREAKING. Reserved for chassis architecture changes (Docker image base, dispatcher API, subtree layout).
- `MINOR` (`0.3.0` → `0.4.0`): new features, optional fields, opt-in behaviors. Backwards-compatible.
- `PATCH` (`0.3.1` → `0.3.2`): fixes, prompt tweaks, internal refactors. Always backwards-compatible.

## v0.2.0 — 2026-07-20

Plugins move from image-baked to fetched-at-boot. This is the release that makes `behalfbot-plugins` real: the chassis now pulls its plugin tree from that repo at a pinned tag+SHA instead of carrying whatever was baked into the image.

### Added
- `chassis/PLUGINS_PIN` — single line `<tag> <40-hex-sha>` recording the pinned plugin release. Currently `v0.1.0 7f826679d73f3b507e1472c920709766e159bc5e`.
- `chassis/scripts/fetch-plugins.sh` — seed copy of the canonical fetcher from `behalfbot-plugins`. Resolves the pinned tag, **requires the resolved SHA to equal the pinned SHA**, installs into `$CUSTOMER_HOME/vendored-plugins`, and writes `$CUSTOMER_HOME/plugins.lock`.
- Boot-time fetch: `docker/entrypoint.sh` calls the fetcher from `dispatcher` and `bootstrap` modes.
- CI workflow `fetcher-seed-matches-canonical` — fails the build if the chassis seed drifts from canonical.
- `plugins.lock` now records a `skipped[]` array naming any plugin held back by `min_chassis_version`.

### Changed
- **`CHASSIS_PLUGINS_ROOT` now prefers `$CUSTOMER_HOME/vendored-plugins`** over the baked `/app/plugins`, falling back to baked when no fetched tree is present. The fetched tree is only selected if it actually contains at least one `*/openclaw.plugin.json` — an empty or half-written directory must not shadow the baked tree and leave the chassis silently loading nothing.

### Fixed
- `min_chassis_version` in `registry.json` was declared and never evaluated. A plugin requiring a newer chassis installed silently and failed later at runtime. It is now enforced: too-new plugins are skipped individually (the rest of the tree still installs), logged per plugin, and recorded in the lockfile.
- The chassis fetcher seed was committed non-executable, so the boot hook's `[[ -x ]]` guard skipped it and the fetch never ran. Every other artifact was correct and completely inert. Caught by booting a built image rather than trusting green CI — `diff` compares content and ignores mode, so the drift check passed on an unrunnable file.

### Why this bump matters operationally

`loom-vision` declares `min_chassis_version: 0.2.0`. On `0.1.1` the fetch succeeds and **skips** it, leaving `vendored-plugins` without any plugin manifest, so the loader correctly stays on the baked tree. At `0.2.0` the plugin installs and the loader flips to the fetched tree. Verified in a container both ways before this bump.

### Migration

None required. The first boot after upgrading fetches the pinned tree and writes `vendored-plugins/` and `plugins.lock` alongside existing state. Nothing is removed. If the fetch fails for any reason the chassis continues on the baked tree, so a network-isolated install is unaffected.

## v0.1.0 — 2026-06-22

Initial versioned release. Establishes the contract for the auto-updater (#33).

### Added
- `chassis/VERSION` — semver source of truth
- `chassis/CHANGELOG.md` — this file
- `chassis-update-check` weekly heartbeat (notify-only)
- `chassis/scripts/chassis-update.sh` apply script (consent-gated)
- `chassis/skills/chassis-update.md` Discord trigger handler
- `auto_update` block in `chassis.config.yaml` (default on; opt-out via `check: false`)

## v0.1.1 — 2026-07-02

### Added
- `chassis/scripts/daily-log-gather.py` — multi-surface gather for the nightly daily-log heartbeat. Dynamic GitHub repo discovery via `gh api graphql viewer` (captures customer-owned repos AND collaborator repos, so student project repos surface automatically), SiYuan block-mining, Gmail sent-scan (deferred to prompt if no OAuth wiring), Discord postmortem regex-mining. Env var contract: `DAILY_LOG_GH_USER`, `DAILY_LOG_GMAIL_IDENTITY`, `DAILY_LOG_DISCORD_CHANNEL_ID`, `DAILY_LOG_SIYUAN_URL`, `DAILY_LOG_SIYUAN_TOKEN`, plus optional `DAILY_LOG_EXTRA_METRICS_SCRIPT` extension point for install-specific metrics.
- `chassis/scheduled-tasks/daily-log-prompt.md.template` — parameterized prompt with 5 sections (Shipped / Learnings & Tribal Knowledge / Open Threads / Metrics Snapshot / Reflection). Reflection section header stays even when body is empty per Sean's directive.
- `chassis/docs/heartbeats/daily-log.md` — env var reference, per-install setup steps, example customer extra-metrics script.
- `chassis/scripts/test-daily-log-gather.sh` — 6 smoke scenarios (all surfaces off / individual surfaces off / valid JSON on partial failure). No network or creds required.

### Changed
- `chassis/HEARTBEATS.md.template` — added commented `daily-log` heartbeat block with recommended wiring (`gather: chassis/scripts/daily-log-gather.py`).

Backwards compatible. Existing installs that keep pointing `HEARTBEATS.md` at their local `scripts/daily-log-gather.sh` continue to work; the new chassis script is opt-in via HEARTBEATS.md gather-line change plus setting the `DAILY_LOG_*` env vars.

Rationale + design discussion: scrollinondubs/behalfbot#42 (merged 2026-07-02).
