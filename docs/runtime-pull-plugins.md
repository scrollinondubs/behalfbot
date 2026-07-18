# Runtime-pull plugins (behalfbot#53, Phase 0)

Plugins are moving from image-baked copies to a single source-of-truth repo,
`scrollinondubs/behalfbot-plugins` (PLUGINS_REPO), fetched at a pinned tag+SHA.
This kills the copy-drift class of bug (the loom-vision transcript break of
2026-07-15) structurally: one copy, fetched fresh, nothing local to fall out
of sync.

## The four layers

Plugin discovery searches these roots in order; the first directory containing
`<name>/openclaw.plugin.json` wins:

| Layer | Path (container) | What lives here |
|---|---|---|
| 1. plugins-local | `$CUSTOMER_HOME/plugins-local/` | Customer-private plugins that are never published (e.g. angel-protocol, dating on the reference install). Highest precedence - local always overrides. |
| 2. legacy customer | `$CUSTOMER_HOME/plugins/` | Pre-existing customer-local plugins (midnight-oil etc.). Kept for back-compat; new private plugins should use plugins-local/. NEVER a fetch destination. |
| 3. vendored (fetched) | `$CUSTOMER_HOME/vendored-plugins/` | Public plugins fetched from behalfbot-plugins at the pinned tag+SHA. On the customer bind mount: writable at runtime, survives container recreate. Gitignored customer-side; reproducible from plugins.lock. |
| 4. baked fallback | `/app/plugins` | Image-baked OFFLINE FALLBACK during migration. Read-only. Goes away one VERSION cycle after Phase 2. |

## Pin and lockfile

- **`chassis/PLUGINS_PIN`** (next to `chassis/VERSION`): one line, `<tag> <40-hex-sha>`.
  The SHA is the gate. `fetch-plugins.sh` resolves the tag remotely and refuses
  to fetch when the resolved SHA differs from the pin - a force-moved tag never
  auto-refetches. The pin moves ONLY at a chassis VERSION bump (Sean-gated),
  in the same PR as VERSION.
- **`$CUSTOMER_HOME/plugins.lock`** (committed customer-side): records repo,
  tag, commit, fetched_at, chassis_version, frozen flag, and per-plugin
  `{version, tag, sha}`. The lockfile diff in each customer repo is the audit
  trail for every plugin change.

## Flow

1. `docker/entrypoint.sh` runs `fetch_plugins` at `dispatcher` and `bootstrap`
   boot (best-effort - a fetch problem never takes the bot down), and exposes
   an explicit `update-plugins` mode that propagates failures.
2. `chassis/scripts/fetch-plugins.sh` reads the pin, verifies tag-vs-SHA,
   downloads the tarball at the pinned SHA, sanity-checks the tree against
   `registry.json`, atomically swaps it into `vendored-plugins/`, and writes
   `plugins.lock`. Unpinned (no pin line) = no-op; `--freeze` skips fetching
   entirely for air-gapped installs.
3. `bootstrap.sh` step 9 (`activate_plugins`) calls
   `chassis/scripts/activate-plugins.sh`, which for each enabled module
   (`chassis.config.yaml` `modules.<name>.enabled`, parsed by the shared
   `chassis/scripts/lib/enabled-plugins.py`):
   - runs the plugin's `setup.sh` (idempotent contract; failures WARN),
   - writes `$CUSTOMER_HOME/chassis-env.sh` from manifest `contracts.env`,
   - merges manifest `contracts.mcpServers` into `$CUSTOMER_HOME/.mcp.json`
     using `"_managed_by": "behalfbot-plugin:<name>"` markers (re-runs replace
     managed entries; manual entries are never touched),
   - re-merges plugin triggers via `merge-plugin-triggers.sh`, which now
     searches the same four layers.

## Delivery caveat (not zero-touch)

`entrypoint.sh` and `bootstrap.sh` execute from the IMAGE, not the clone
overlay. Activating this machinery therefore requires an image release plus a
container recreate. Until then, a refreshed clone alone changes nothing at
boot (old image has no fetch step), and a new image with a stale clone
degrades gracefully: `activate_plugins` finds no `activate-plugins.sh` and
skips with a WARN, `fetch_plugins` finds no pin line and no-ops, and the baked
tree keeps driving - byte-identical behavior to today. The chassis-script side
(fetcher, activator, trigger merge) deliberately rides the clone so later
fixes there do NOT need an image release.

## Status

Phase 0 ships the machinery dormant: `scrollinondubs/behalfbot-plugins` does
not exist yet and `PLUGINS_PIN` is unpinned, so every install keeps running the
baked tree until Sean creates the repo, tags it, and lands a pin + VERSION
bump. Repo creation, first tag, and every pin move are Sean-gated.
