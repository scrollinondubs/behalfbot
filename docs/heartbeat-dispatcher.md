# Heartbeat Dispatcher Architecture

The chassis's scheduled-work mechanism. Reads `${CHASSIS_HOME}/HEARTBEATS.md` (installer-rendered from `chassis/HEARTBEATS.md.template` at install-time), decides what (if anything) to run on each tick, invokes Claude only when there's actual work, sends notifications to Discord on completion.

This document explains *why* the dispatcher is shaped the way it is. Most of the answers are bug-fix history — see `docs/LESSONS_FROM_V1.md` for the underlying incidents, and `docs/architectural-anti-patterns.md` for the distilled rules.

---

## The core idea: gather-first dispatcher

The dispatcher fires on a fixed schedule (default every 15 min via launchd / systemd). Each tick walks every registered heartbeat, but **does not invoke Claude unless a cheap gather script signals work**. 96 ticks/day → ~4 actual `claude -p` invocations on a typical day. The other 92 are zero-cost shell loops.

The shape:

```
launchd / systemd
    │
    └── dispatcher.sh  (every 15m)
        │
        ├── for each heartbeat in HEARTBEATS.md:
        │       │
        │       ├── schedule_matches()?  ── no ──→ skip
        │       │   │
        │       │   yes
        │       │   │
        │       ├── conservation_mode + criticality?  ── skip if normal/background ──→ next
        │       │   │
        │       ├── run gather script (cheap shell)
        │       │
        │       ├── evaluate_condition() — always | threshold | ask_model
        │       │   │
        │       │   ├── false ──→ log, skip
        │       │   │
        │       │   └── true
        │       │       │
        │       │       ├── invoke_claude() with model + budget + cwd
        │       │       │
        │       │       ├── (optional) run_output_validator()
        │       │       │
        │       │       └── check_and_notify() — Discord webhook on `notify: true` in output
        │       │
        │       └── log SUCCESS / FAIL, persist state
        │
        └── exit
```

Reference: `LESSONS_FROM_V1.md` #7, #20.

---

## Why not just call Claude every tick?

You'd burn ~$200/month on a single user instance for the privilege of mostly-empty checks. The lesson Sean's V1 paid for: put a deterministic check in front of every LLM call. The check is allowed to be wrong (false-positive an unnecessary fire is fine) — what matters is that it short-circuits cheaply.

Reference: `LESSONS_FROM_V1.md` #20.

---

## HEARTBEATS.md as the registry

The dispatcher does NOT discover heartbeats by scanning directories. It parses `${CHASSIS_HOME}/HEARTBEATS.md` at fire time. **A heartbeat that isn't registered there is silently dormant.** This was a recurring bug shape in V1 — see `LESSONS_FROM_V1.md` #11 and #25 (silent-dormancy pattern).

Per anti-pattern #17 (`docs/architectural-anti-patterns.md`), the registry lives at `${CHASSIS_HOME}/HEARTBEATS.md` (one level above `chassis/`), NOT inside `chassis/`. Bootstrap copies `chassis/HEARTBEATS.md.template` to `${CHASSIS_HOME}/HEARTBEATS.md` at install-time. Installer edits the rendered file; never the template.

Why a single file as the registry?

- It's a forcing function: registering is mandatory, not optional, because the file is the only way the dispatcher sees you exist.
- It's reviewable: a PR that adds a new heartbeat MUST touch this file, which makes the cadence + budget + criticality decisions visible at review time.
- It's diff-friendly: state changes (disable, re-budget, change cadence) show up as line diffs.

Per-heartbeat schema is in `chassis/HEARTBEATS.md.template` (and propagates to the rendered `${CHASSIS_HOME}/HEARTBEATS.md`).

---

## Schedule formats

The dispatcher supports three:

| Format | Example | Semantics |
|---|---|---|
| Interval | `every 15m`, `every 1h` | Fire whenever this much time has elapsed since `last_checked` |
| Daily | `daily 08:00` | Fire once per day at the named local time |
| Weekly | `weekly sunday 18:00` | Fire once per week on the named day at the named time |

Daily and weekly heartbeats also support a `jitter:` field that adds a deterministic-per-day random offset (so 30 daily heartbeats with `daily 08:00 + jitter: 30m` don't all hammer at exactly 08:00:01).

---

## Condition types

| Condition | When it fires | Use case |
|---|---|---|
| `always` | Whenever the schedule matches | Periodic actions independent of any signal — daily backups, weekly reports |
| `threshold <field> <op> <value>` | When the gather's JSON output's `<field>` matches the operator (e.g. `count > 0`) | Most heartbeats — only fire when there's real work |
| `ask_model` | When a local Ollama model returns YES to the gathered context + condition_prompt | Ambiguous cases where structured logic doesn't capture nuance |

`ask_model` requires Ollama running (default at `http://localhost:11434`). When Ollama is unreachable the dispatcher fails open (always fires). The `OLLAMA_URL` env var overrides the default.

Reference: `LESSONS_FROM_V1.md` #25 (silent dormancy via missed gates).

---

## Conservation mode

When token budget is running low, conservation mode suspends `normal` and `background` heartbeats while letting `critical` ones continue. The state file `scheduled-tasks/conservation-mode.json` is the source of truth:

```json
{
  "enabled": true,
  "enabled_at": "2026-04-15T18:00:00Z",
  "enabled_by": "manual via toggle script",
  "auto_lift_after": "2026-05-01T00:00:00Z",
  "reason": "5-hour quota soft cap reached"
}
```

The dispatcher checks `auto_lift_after` on every tick — once that timestamp passes, conservation mode self-disables. (`LESSONS_FROM_V1.md` #12 — auto-lift was a bug fix; the V1 dispatcher initially had a too-narrow auto-disable window that left conservation mode permanently on.)

Conservation mode is opt-in: if no JSON file exists, the dispatcher behaves as if it's off. Installers without quota concerns can skip the file entirely.

---

## Output validator (Five Failure Modes)

When a heartbeat's YAML block has `output_validator: true`, the dispatcher runs a haiku-powered Five Failure Modes check on the generated artifact (briefing, content stub, etc.) before "publishing" (notifying Discord, writing to second-brain, etc.). On fail, the artifact is renamed `.quarantined` and the ops webhook (`DISCORD_OPS_WEBHOOK_URL`) gets an alert.

The Five Failure Modes (from `docs/five-failure-modes.md` in the V1 reference install):

1. Hallucinated actions — tool calls referencing values that weren't verified
2. Scope creep — modifying things outside the stated change boundary
3. Cascading errors — workarounds that paper over a root error
4. Context loss — re-asking established questions, contradicting earlier decisions
5. Tool misuse — wrong tool, wrong parameters, ignoring tool output

Validator runs async + fails open on its own errors so the main pipeline isn't blocked. Cost-tracked separately in telemetry.

---

## Telemetry

Every `claude -p` invocation appends a JSONL line to `logs/telemetry/<date>-usage.jsonl`:

```json
{"ts":"2026-05-05T10:15:23","heartbeat":"morning-briefing","model":"opus","cost_usd":0.83,"input_tokens":12800,"output_tokens":4200,"cache_read_tokens":8000,"cache_create_tokens":2400,"wall_seconds":94,"exit_code":0}
```

Useful for:
- Daily cost rollups (a separate heartbeat reads this file)
- Per-heartbeat cost regression detection
- Model performance comparison (haiku vs sonnet vs opus latency + accuracy)

---

## Recovery hooks (plugin-extensible)

Plugins can drop shell scripts into `scheduled-tasks/recovery-hooks.d/` that the dispatcher sources at the start of every tick. Each hook defines a `chassis_recovery_*` function that the dispatcher then invokes.

Use case: detect external state transitions (Android emulator booted, n8n container restarted, Tailscale up after reboot) and rewind a heartbeat's `last_fired` so it picks up immediately rather than waiting for the natural cadence.

The chassis core does not ship any recovery hooks. The `dating` plugin (when activated) installs an Android-emulator recovery hook that addresses the V1 reference install's "Darina incident" pattern (queued Hinge action sat for 95min after emulator reboot).

---

## What this dispatcher is NOT

- **Not a job queue.** No retries, no backoff, no priority queue. Each tick is independent.
- **Not a workflow engine.** No DAGs, no fan-out/fan-in. Each heartbeat is a single shell script + a single Claude invocation.
- **Not a Kubernetes thing.** Fires from launchd (macOS) or systemd (Linux). One process per tick.
- **Not multi-tenant.** Each installer has their own dispatcher running on their own machine. Behalf.bot is single-user-per-install by design.

The simplicity is the point. V1 spent six weeks proving that a 900-line shell script does ~95% of what an ambitious workflow framework does, with zero infrastructure.

---

## Operating concerns

### Lock file

`scheduled-tasks/dispatcher.lock` prevents two dispatcher runs from overlapping. Stale locks (PID no longer running) get cleaned automatically on next tick.

### State file

`scheduled-tasks/heartbeat-state.json` tracks `last_checked`, `last_fired`, `last_result`, `fire_count` per heartbeat. Atomic writes via `jq → tmp → mv`. Gather scripts have their own per-heartbeat state files; the dispatcher does NOT manage those.

### Logs

Daily rollover at `logs/scheduled/YYYY-MM-DD-dispatcher.log`. Never auto-rotated; archive manually if disk pressure becomes an issue.

### Output files

Each heartbeat fire writes its Claude output to a heartbeat-specific path the prompt controls. The dispatcher reads only the leading 20 lines for `notify: true` + `summary: ...` keys to decide whether to ping Discord.

---

## Adding a new heartbeat

1. **Create the gather script** at `chassis/scripts/gather-<name>.sh`. Start from `chassis/scripts/gather-template.sh`. Make sure it emits JSON, short-circuits cheaply, and exits 0 on no-work.
2. **Create the prompt** at `chassis/scheduled-tasks/<name>-prompt.md`. Reference the gather output via the documented input shape; don't assume context the gather doesn't provide.
3. **Register the heartbeat.** Three cases per anti-pattern #17:
   - **Chassis-default heartbeat** (shipped to every install) → add the YAML block to `chassis/HEARTBEATS.md.template`. PR against chassis. Bootstrap propagates to every new install's `${CHASSIS_HOME}/HEARTBEATS.md`.
   - **Plugin-bound heartbeat** (shipped with a specific plugin) → add the YAML block to the plugin's manifest. Bootstrap appends to `${CHASSIS_HOME}/HEARTBEATS.md` for installs that enable the plugin.
   - **Installer-specific heartbeat** (one install only) → add directly to `${CHASSIS_HOME}/HEARTBEATS.md` on the customer machine. NEVER edit `chassis/HEARTBEATS.md.template` for an installer-specific heartbeat.
   **Without registration the heartbeat is silently dormant** (LESSONS_FROM_V1.md #11).
4. **Test in dry-run** — `DRY_RUN=true CHASSIS_HOME=/path/to/chassis chassis/scheduled-tasks/heartbeat-dispatcher.sh` (logs decisions without invoking Claude).
5. **Commit** — including all three files together. Each PR adding a heartbeat must touch the manifest.

Reference: `architectural-anti-patterns.md` #8 (heartbeats must be registered or they're dead).

---

## Cross-references

- `chassis/HEARTBEATS.md.template` — per-heartbeat schema + chassis-default heartbeats (template)
- `${CHASSIS_HOME}/HEARTBEATS.md` — installer-rendered registry (template copy + plugin appends + installer additions); dispatcher reads this
- `chassis/scheduled-tasks/heartbeat-dispatcher.sh` — the dispatcher itself
- `chassis/scripts/gather-template.sh` — gather script template
- `docs/LESSONS_FROM_V1.md` — full lesson list, especially #7, #11, #13, #20, #24, #25, #26
- `docs/architectural-anti-patterns.md` — distilled rules
