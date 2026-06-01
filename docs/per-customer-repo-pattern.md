# Per-customer repo pattern

How a Behalf.bot install should be structured: one repo per customer (or per install owner if owner = installer), vendoring this chassis at `chassis/` via git subtree.

## Why a separate repo per customer

The chassis V1 phase used a single shared repo (`scrollinondubs/behalfbot`) with `customer/<name>` branches for each install (installer-1, Marc, Toby). That pattern was expedient for the initial customer onboardings but accrues debt fast:

- **Diverging customer branches become fork-with-deletions.** Each customer branch ends up DELETING chassis plugins it doesn't want (BFL scripts a non-quantified-self customer doesn't use, dating scripts a non-dating customer doesn't run, remarkable scripts a non-reMarkable customer doesn't need). This makes chassis upgrades painful — every `git pull` into a customer branch becomes a merge-conflict resolution session.
- **No clean install boundary.** Customer-specific docs, install profiles, and per-customer config bleed into chassis's main branch tree as untracked-but-related files. Chassis main becomes a junk drawer.
- **No customer ownership.** A customer can't `git clone` their install. They can't own their repo's history. They can't bring their install offline from Sean's GitHub org.

Per-customer-repo solves all three:

- Customer enables/disables plugins via `chassis.config.yaml`; chassis source stays untouched.
- Customer-specific everything lives in the customer's repo; chassis main is clean.
- Customer owns their repo (eventually — initial bootstrap may happen under the chassis maintainer's namespace, then transfer).

## Target layout

```
<customer-repo>/
├── chassis/                      # vendored from scrollinondubs/behalfbot (git subtree, --squash)
├── chassis.config.yaml           # which plugins are ENABLED + per-plugin config knobs
├── INSTALL_PROFILE.md            # install identity (human-readable; companion to chassis.config.yaml)
├── CLAUDE.md                     # install operating manual
├── HEARTBEATS.md                 # install's heartbeat config (consumed by chassis dispatcher)
├── chassis-compose.override.yml  # compose overrides for this install
├── .env.example                  # template (NEVER commit the real .env or .env.baked)
├── plugins/                      # install-specific plugins (NOT in chassis core)
├── scripts/                      # install-specific scripts
├── scheduled-tasks/              # install-specific gather scripts + prompts
├── skills/                       # install-specific skill files
├── docs/                         # install-specific runbooks
├── context/                      # install-specific context files
└── .gitignore                    # .env, .env.baked, data/, logs/, briefings/, memory/, backups/
```

## Bootstrap

For a NEW customer (e.g. Toby):

```bash
# 1. Create the repo (customer-owned or under chassis maintainer's namespace pending transfer)
gh repo create <namespace>/<customer-name> --private --description "<customer>'s Behalf.bot install"

# 2. Clone, vendor chassis, push initial commit
git clone https://github.com/<namespace>/<customer-name>.git
cd <customer-name>
git subtree add --prefix=chassis https://github.com/scrollinondubs/behalfbot.git main --squash

# 3. Scaffold the install-specific files
cp chassis/INSTALL_PROFILE.md ./INSTALL_PROFILE.md  # then customize
cp chassis/chassis.config.yaml ./chassis.config.yaml  # then customize per-customer
touch CLAUDE.md HEARTBEATS.md .env.example chassis-compose.override.yml
mkdir plugins scripts scheduled-tasks skills docs context

# 4. Initial commit + push
git add -A
git commit -m "bootstrap: <customer>'s install vendoring chassis @ <chassis-sha>"
git push -u origin main
```

For Sean (legacy install at `$CHASSIS_HOME`), the migration is incremental — `$CHASSIS_HOME` stays live while `~/new-jaxity` is populated. See `docs/migration-from-legacy-<v1-reference-install>.md` (TBD).

## Subtree maintenance

Pull upstream chassis improvements:

```bash
git subtree pull --prefix=chassis https://github.com/scrollinondubs/behalfbot.git main --squash
```

The `--squash` keeps the customer repo's history clean (one commit per chassis pull instead of thousands of upstream commits).

If a customer finds a chassis bug or wants to upstream a fix:

```bash
# Branch chassis directly (NOT inside the customer repo's chassis/ subtree)
cd /some/other/clone/of/chassis
git checkout -b fix/foo
# edit, commit, push, PR to scrollinondubs/behalfbot
```

Then once the PR lands in chassis main, the customer repo absorbs it via the standard subtree pull.

**Never edit files inside the customer repo's `chassis/` directory directly.** That edit will be silently clobbered on the next subtree pull. The chassis is read-only-by-convention from inside customer repos.

## Plugin enablement

The chassis ships every plugin source-of-record. Customer-side enablement is via `chassis.config.yaml`:

```yaml
modules:
  bfl:
    enabled: false        # this customer doesn't quantify their fitness
  dating:
    enabled: false        # this customer isn't using dating-app automation
  midnight-oil:
    enabled: true         # this customer wants kanban-driven autonomous work
    kanban:
      project_owner: <github-user-or-org>
      project_number: <N>
      # ...
```

The chassis dispatcher reads `chassis.config.yaml` at startup and:
- Registers heartbeats only for enabled plugins
- Loads plugin-specific env vars from `.env`
- Skips disabled plugins entirely (no source-deletion needed)

This means a customer can flip a plugin from disabled → enabled without ANY git changes — just edit `chassis.config.yaml`, populate the env vars, restart the chassis container.

## Customer-specific plugins

Plugins that are SEAN-PERSONAL or CUSTOMER-PERSONAL (not generally useful enough to ship in chassis core) live under the customer repo's `plugins/<plugin-name>/` directory. Example: `midnight-oil` (kanban-driven token-window consumer) is Sean's, lives in `new-jaxity/plugins/midnight-oil/`.

Chassis dispatcher discovers both chassis-core plugins (under `chassis/plugins/`) AND install-side plugins (under `./plugins/`). Naming collisions are resolved install-side-wins (install overrides chassis).

## Repository ownership during transition

Initial bootstraps may happen under the chassis maintainer's GitHub namespace (e.g. `jacketyjax/<customer-name>`) if the customer doesn't yet have a GitHub account or if there are permission constraints. Plan to transfer to the customer's namespace once they're set up:

```bash
gh api -X POST repos/<namespace>/<customer-name>/transfer -f new_owner=<customer-username>
```

After transfer, update the customer's local clone's remote URL.

## Migration from V1 customer branches

For customers currently on V1 `customer/<name>` branches in the chassis repo (installer-1, Marc, Toby as of 2026-05-22):

1. Identify what's UNIQUE to the customer branch (per-customer docs, customer-specific scripts) — typically 4-7 new files under `docs/` plus dating-templates.
2. Convert the "DELETE plugin source we don't want" pattern to "set `enabled: false` in chassis.config.yaml" — the deletions become config-driven enables.
3. Create the per-customer repo (see Bootstrap above).
4. Copy the UNIQUE files to the new repo's root.
5. Populate `chassis.config.yaml` with the customer's enable list + per-plugin config (substituting in the values that were previously hardcoded into customer-branch source).
6. Have the customer (or you, on their behalf) update their local checkout's remote to the new repo.
7. Delete the old `customer/<name>` branch from chassis repo to prevent confusion.

Customer-specific plugins identified during step 1-2 (e.g. Marc's restaurant-booking, Sean's midnight-oil) either move to:
- The customer's repo's `plugins/<plugin-name>/` (if customer-specific), OR
- Chassis core's `plugins/<plugin-name>/` (if generally useful) via a chassis PR.

## When chassis ships a new plugin

When chassis core adds a new plugin (e.g. a hypothetical `meal-prep` plugin), customer repos absorb it via the next `git subtree pull`. By default the plugin is disabled (`modules.meal-prep.enabled: false` not set, or explicitly `false`). Customers who want to opt in flip the flag in `chassis.config.yaml`.

## Reference

- `chassis/docs/credential-bake.md` — credential pattern (`.env` → `.env.baked` via `bake-env.sh`).
- `chassis/docs/hydration.md` — install-time bootstrap walkthrough.
- `chassis/docs/containerization.md` — chassis container architecture.
- Issue history: chassis V1 customer-branch pattern explanation lives in chassis git log under `customer/installer-1`, `customer/installer-2`, `customer/installer-3` branch history.
