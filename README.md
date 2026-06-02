# Behalf.bot Chassis

> A bare-bones, opinionated foundation for building your own always-on personal AI agent. Force-multiplier shaped: it works on your behalf — calendar, briefings, inbox triage, second-brain notes, plugin-driven extensions — instead of waiting for you to prompt it.

The chassis is the generic engine. Everything that makes it *yours* — name, channels, contacts, calendar identity, plugins you enable — lives in a small overlay you supply at install time. Two profile files (`INSTALL_PROFILE.md` + `chassis.config.yaml`) parameterise the entire stack.

---

## Three paths to get running

| Path | For | How |
|------|-----|-----|
| **Self-serve** | "I'm comfortable on the terminal and want to read the runbook" | `git clone` this repo, follow [`docs/install-runbook-streamlined.md`](docs/install-runbook-streamlined.md), supply your own `INSTALL_PROFILE.md` + `chassis.config.yaml` | 
| **Wizard-assisted** *(recommended)* | "I want guided setup without paying for white-glove" | Sign up at **[behalf.bot](https://behalf.bot)** → run the install-wizard interview → it generates your `INSTALL_PROFILE.md` + `chassis.config.yaml` for you. Drop those into the cloned chassis, run `bootstrap.sh`. 30 minutes vs. half a day. |
| **White-glove (paid)** | "Just make it work for me" | Sign up at [behalf.bot](https://behalf.bot) → book the white-glove install. We drive the install via SSH, ratify your config with you live, and hand off a working agent. Same chassis, faster runway, ongoing support included. |

All three paths produce the same artifact — the same chassis runs underneath, just a different amount of time you spend at the terminal.

---

## What you get out of the box

- **Heartbeat dispatcher** — gather-first scheduled tasks. ~96 ticks/day, ~4 actual model invocations (cost-aware by design).
- **Morning briefing pipeline** — daily synthesis of calendar, inbox, news pulse, second-brain notes, and anything else you wire in.
- **Discord intake** — primary chat surface; channel routing convention + voice-note transcription built in.
- **MCP wiring stubs** — pre-shaped slots for Anthropic-style MCP servers (Gmail, Google Calendar, Drive, GitHub, plus your install's pickups).
- **Hook-layer guardrails** — safety enforced as code, not as runtime prompts. Hardcoded limits survive context resets.
- **Welfare-check** — generic "operator-silent → emergency-contact cascade" pattern. Configurable per install.
- **OpenClaw plugin compatibility** — see next section.

---

## Plugin ecosystem

Behalf.bot honours the **OpenClaw plugin standard**. Any plugin published on [**ClawHub.ai**](https://clawhub.ai) installs against the chassis without adaptation — same manifest schema (`openclaw.plugin.json`), same skill+script layout, same activation contract. Drop a plugin under `plugins/<name>/`, flip its `chassis.config.yaml` toggle, and it's live on the next heartbeat tick.

A few opt-in plugins ship in this repo as references:

- `plugins/welfare-check/` — operator-welfare escalation cascade (generic)
- `plugins/bfl/` — Body for Life quantified-self pipeline (photo + vision + Postgres ingest)
- `plugins/dating/` — multi-platform dating-app automation (Hinge / Tinder / Bumble) with concierge framing
- `plugins/restaurant-booking/` — calendar-aware booking helper
- `plugins/remarkable/` — reMarkable tablet sync (notebook OCR + cross-link)
- `plugins/midnight-oil/` — opportunistic kanban-driven token-window consumer

Personal-security components (live-location tracking, duress codewords, mode inference, camera integration) are intentionally **not** in this repo — exposing those publicly would hand any would-be adversary the operator's safety playbook. Build that layer as a private plugin in your install-specific repo. See `plugins/welfare-check/skills/welfare-check.md` for the carve-out rationale.

---

## Repo structure

| Path | Purpose |
|------|---------|
| `INSTALL_PROFILE.md` | Generic installer template. The wizard fills this in for you, or you author by hand. |
| `chassis.config.yaml` | Machine-readable config template. Same — wizard-generated or hand-authored. |
| `chassis/` | Generic core: heartbeat dispatcher, briefing pipeline, MCP wiring stubs, guardrails, second-brain adapters |
| `chassis/scripts/templates/` | Per-install script templates (restart/watchdog) rendered into CUSTOMER_HOME at bootstrap |
| `chassis/launchd/` | macOS LaunchAgent plist templates (rendered into CUSTOMER_HOME/launchd/ at bootstrap) |
| `chassis/skills/` | Skill scaffolding (templates; populated per install) |
| `chassis/scripts/` | Shared utilities (gather scripts, bake helpers, OAuth bridge, hydration) |
| `chassis/memory/` | Memory format + seeded entries (structure only; no install-specific content) |
| `plugins/` | Opt-in plugins (see ecosystem section above) |
| `docs/` | Install runbook, hydration guide, architecture notes, anti-patterns, lessons |
| `bootstrap.sh` | Auto-bootstrap orchestrator — reads your profile + config, wires everything up |

### Directory layout on a customer machine (post-issue-#6)

Customer state and chassis code live in two physically separate trees:

```
~/behalfbot/              # CHASSIS_HOME - fully disposable, re-pullable
  chassis/                # the vendored chassis subtree
  plugins/                # chassis plugins
  bootstrap.sh
  docker-compose.yml
  Dockerfile
  README.md
  requirements.txt

~/.behalfbot/             # CUSTOMER_HOME - never touched by reinstall
  .env                    # customer secrets
  CLAUDE.md               # hydrated per-install
  HEARTBEATS.md           # per-customer heartbeat config
  chassis.config.yaml     # per-customer chassis config
  INSTALL_PROFILE.md      # per-customer install profile
  scripts/                # customer-side: restart-${BOT}-discord.sh, watchdog-${BOT}-discord.sh
  state/                  # heartbeat-state.json, conservation-mode.json
  scheduled-tasks/        # customer overrides + per-tick state
  memory/                 # installer-specific memory
  briefings/              # generated artifacts
  logs/                   # all logs
  data/                   # any customer data
  temp/
  launchd/                # rendered LaunchAgent plists (symlinked into ~/Library/LaunchAgents/)
```

`rm -rf ~/behalfbot && git clone https://github.com/scrollinondubs/behalfbot.git ~/behalfbot` is safe - customer state is at `~/.behalfbot/` and untouched.

### Migration from a pre-issue-#6 install

If your install was bootstrapped before issue #6 (customer state under `~/behalfbot/`), run the migration script once:

```
cd ~/behalfbot
bash chassis/scripts/migrate-customer-state.sh --dry-run   # preview
bash chassis/scripts/migrate-customer-state.sh             # execute
```

The script `mv`'s every customer-side artifact (`.env`, `CLAUDE.md`, `HEARTBEATS.md`, `scripts/`, `state/`, `briefings/`, `logs/`, `data/`, `temp/`, installer-specific `chassis/memory/*`) from `~/behalfbot/` to `~/.behalfbot/`, re-renders the customer-side restart/watchdog scripts from chassis templates, and reloads the LaunchAgent plists at the new paths.

Refer to `chassis/scripts/migrate-customer-state.sh --help` for flag details. The script is idempotent: a sentinel at `~/.behalfbot/.migrated-from-chassis-home` makes subsequent runs a no-op.

---

## Install pathway in detail (self-serve and wizard paths)

1. **Get your `INSTALL_PROFILE.md` + `chassis.config.yaml`.** Wizard at [behalf.bot](https://behalf.bot) is the fastest path; hand-authoring against [`INSTALL_PROFILE.md`](INSTALL_PROFILE.md) + [`chassis.config.yaml`](chassis.config.yaml) templates is the alternative.
2. **Read** [`docs/installer-homework.md`](docs/installer-homework.md) — pre-flight checklist (accounts to provision, machine prep, credentials to stage in Vaultwarden or your password manager of choice).
3. **Clone this repo** to your install machine: `git clone https://github.com/scrollinondubs/behalfbot.git`
4. **Drop your profile + config** into the repo root, overwriting the templates.
5. **Run** `bash bootstrap.sh`. The script reads your overlay, hydrates secrets, bakes the runtime env, and brings up the chassis container (or systemd unit on Linux installs).
6. **First-heartbeat smoke test.** Three consecutive clean morning briefings to your configured Discord channel = install signed off.

Total wall-clock time, wizard-assisted: 30-45 minutes once your accounts are provisioned. Self-serve from cold: 2-4 hours typical, more if you're new to Docker / Tailscale / Vaultwarden.

---

## Status

**Beta.** The chassis runs the V1 reference install and a small set of beta installers. Public source is stable; the install runbook is actively iterated based on installer feedback. Issues + PRs welcome.

**Where to file feedback:** GitHub issues on this repo, or Discord (link via [behalf.bot](https://behalf.bot)).

---

## License

[O'Saasy License Agreement](LICENSE). MIT-shaped with light-touch attribution. Use it, modify it, ship it — read the file for the specifics.

---

## Contributing

Pull requests welcome. A few things to know before opening one:

- **Bug fixes + small features:** open a PR against `main`. Reference an issue if one exists.
- **Architectural changes:** open an issue first to discuss the shape. The chassis prizes "boring + portable" over "clever"; novel patterns need to argue their case against the existing lessons in [`docs/LESSONS_FROM_V1.md`](docs/LESSONS_FROM_V1.md) + [`docs/architectural-anti-patterns.md`](docs/architectural-anti-patterns.md).
- **Plugin contributions:** new plugins live in `plugins/`. Follow the OpenClaw manifest spec (`openclaw.plugin.json`); see existing plugins for shape. Plugins that publish to ClawHub.ai can be vendored back into this repo as references with author attribution.
- **Anti-patterns:** [`docs/architectural-anti-patterns.md`](docs/architectural-anti-patterns.md) is the running list of "we tried this; here's why it broke." Read it before introducing a workaround — most workarounds have a documented anti-pattern equivalent.
