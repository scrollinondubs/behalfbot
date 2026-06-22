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
