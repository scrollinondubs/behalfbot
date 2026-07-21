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

## Unreleased

### Fixed
- **v0.2.0's plugin-root preference never ran.** The Changed entry below ("`CHASSIS_PLUGINS_ROOT` now prefers `$CUSTOMER_HOME/vendored-plugins`") described behaviour that was unreachable in every container: the preference lived in `_env.sh` behind an is-unset guard, but the Dockerfile ENV baked `CHASSIS_PLUGINS_ROOT=/app/plugins` and `docker/entrypoint.sh` defaulted and exported the same value before `_env.sh` could ever run. The fetch worked, `vendored-plugins/` and `plugins.lock` were written, and every install silently kept loading the baked tree. Verified empirically on a live 0.2.0 install (dispatcher env showed `/app/plugins` while a usable fetched tree sat ignored). The "verified in a container both ways" claim in the v0.2.0 notes covered the fetch and the loader's selection logic in isolation, not the boot path that preloads the variable.

### Changed
- **Plugin root is now an overlay, not a tree swap.** `chassis/scripts/resolve-plugin-root.sh` (new) resolves plugins per plugin NAME: a plugin present in the fetched `vendored-plugins/` tree wins, anything only in the baked tree still loads. This replaces the v0.2.0 wholesale preference, which - had it been reachable - would have shrunk every install from 7 plugins to the 1 currently published in `behalfbot-plugins`. The result is materialised as a composed symlink root at `$CUSTOMER_HOME/state/plugins-root` so every existing single-root consumer (entrypoint `install-plugin`, `smoke-test` enumeration, plugin script paths) works unchanged. Per-plugin safety is kept: a fetched dir without an `openclaw.plugin.json` never shadows the baked copy, and an empty or failed fetch degrades to baked per plugin.
- `CHASSIS_PLUGINS_ROOT` is no longer baked as Dockerfile ENV or defaulted by the entrypoint. A set value now reliably means an operator set it (compose environment, `docker -e`, or the customer `.env`) and is honoured verbatim with no overlay. The chassis default path is "unset", resolved at boot.
- Boot now logs the resolved plugin root and writes `$CUSTOMER_HOME/plugins-root.state.json` (mode, source roots, per-plugin provenance). If a usable fetched tree exists but is not active, the resolver exits 5 and the entrypoint logs an unmissable ERROR - the v0.2.0 silent no-op class cannot recur quietly.

### Added
- `chassis/scripts/test-plugin-root-resolution.sh` + CI job `plugin-root-resolution` in `shell-tests.yml`: behavioural tests asserting the resolved root actually serves the fetched copy when a fetched tree is present. The pre-existing tests and CI were green throughout the period the feature did nothing.

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
