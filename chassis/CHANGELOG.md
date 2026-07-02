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
