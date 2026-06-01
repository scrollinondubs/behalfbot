# Chassis Architectural Anti-Patterns

> **Read this before touching chassis core.** Distillation of the chassis-universal subset of `LESSONS_FROM_V1.md` (Sean's 30-lessons doc, 2026-05-05). Each pattern below names what to NOT do, what to do instead, and the lesson number(s) where the original bug-fix is documented.
>
> If you're hitting a problem and one of these patterns sounds familiar, go read the underlying lesson before debugging — half the answer is often "we already solved this and the fix is documented."
>
> **Updates:** when a chassis change closes a new gap that future installers will hit, append a new pattern below AND a new lesson to `LESSONS_FROM_V1.md` (#31, #32, ...). Don't leave them undocumented.

---

## 1. Don't reach for platform features that aren't load-bearing

**Anti-pattern:** assuming Anthropic's official primitives (Dispatch, /loop, /voice, auto-mode) are the right building blocks. They're not — the system you're actually building uses Discord channels for inbound, MCPs and CLIs for tool access, computer-use as the fallback. The "official" path is often the wrong one for a persistent agent.

**Do this instead:** when a platform feature looks like it solves your problem, validate it against the agent-not-app constraint (long-running, headless, no human at the keyboard). Most fail. Build the unglamorous path first.

**Lessons:** 1, 4 (Discord+auto-mode incompatibility).

---

## 2. tmux is not optional

**Anti-pattern:** running Claude Code in a bare SSH session without tmux. Closing the laptop kills the process. SSH-started shells also have a different security context than GUI terminals (no macOS keychain integration), which silently breaks OAuth refresh.

**Do this instead:** every long-running Claude Code process lives in a named tmux session. The install runbook should `tmux new -s <v1-reference-install>` (or installer-equivalent name) before launching anything, and the LaunchDaemons / launchd plists that manage long-running services should always wrap the actual process inside tmux.

**Lesson:** 2.

---

## 3. OAuth tokens die. Plan for resurrection, don't fight expiry.

**Anti-pattern:** assuming Claude Code's auth token will live forever once you login. It won't — ~8h TTL headless, no keychain integration in SSH-started shells, and the interactive `/login` flow has no operator at the keyboard at 4 AM.

**Do this instead:** layered seatbelt:
- The OAuth bridge sync (`chassis/scripts/sync-claude-oauth-bridge.sh`, runs every 30 min via launchd). Bidirectional: macOS Keychain ↔ `~/.claude/.credentials.json`. Any live `claude` process that lazily refreshes on use will write a fresh token to the file; the bridge then propagates to the keychain (and vice versa). All peer `claude` processes pick up the new credentials on their next file read.
- A watchdog that detects auth failures and auto-restarts the session if /login is genuinely required.
- A scheduled pre-emptive respawn before the daily critical work fires (Sean uses 5:30 AM ahead of the morning briefing) for fresh process state, NOT for token freshness — that's the bridge's job.

**Do NOT add a 6h-style keepalive that runs `claude --print "ping"` to "tickle" the token.** Anthropic's OAuth refresh-tokens are single-use; each refresh rotates the token server-side. Forcing a rotation on a schedule invalidates the refresh-token held in-memory by every other live `claude` process (your Discord intake session, your in-container dispatcher subprocesses, any interactive terminal session). Those peers then fail with `invalid_grant` on their next refresh, dropping to "Not logged in" until a) a human runs `/login` or b) the next bridge tick + dispatcher tick paper over it. The V1 reference install ran this pattern for months and observed 3x-daily auth invalidation on the Discord session before catching the root cause. See lesson 36.

**Lessons:** 3, 9, 36.

---

## 4. Run with `--dangerously-skip-permissions`; safety lives at the hook layer

**Anti-pattern:** trying to use Claude Code's interactive approval mode in a Discord-channel-driven setup. Approval prompts try to render to a terminal nobody's watching → silent stall.

**Do this instead:** `--dangerously-skip-permissions` for the runtime, deterministic guardrails at `.claude/hooks/guardrails.sh` that the LLM cannot bypass. CLAUDE.md hard limits survive context resets; hook-enforced limits survive the LLM trying to talk its way around them.

**Lessons:** 4, 6.

---

## 5. Treat the agent as an employee with its own accounts

**Anti-pattern:** reusing your personal iCloud / GitHub / Google Workspace identity for the agent. One compromise blast-radius covers your real life. One overzealous OAuth scope eats your real mailbox.

**Do this instead:** dedicated accounts for the agent (separate Apple ID, separate GitHub handle, own MCP credentials). Tight default allowlist (read everything, approve before writing, never spend money, never email externally without approval). Expand the autonomy envelope as the system earns trust. Hard limits in CLAUDE.md AND enforced at the hook layer.

**Lesson:** 6. (See also `INSTALL_PROFILE.md` §8 — identity isolation is a V1 ask, not V2 deferral, for every Behalf.bot install.)

---

## 6. Gather first, prompt second

**Anti-pattern:** firing `claude -p` every time the dispatcher ticks "to see if there's anything to do." 96 invocations per day at $X each adds up fast.

**Do this instead:** put a deterministic gather script in front of every heartbeat. The script does the cheap I/O (jq, find, Postgres select, file mtime check) and only emits a non-empty signal when there's actual work. The dispatcher only invokes Claude when the gather signals work. Net: ~4 LLM invocations/day instead of ~96, same coverage.

**Lessons:** 7, 20.

---

## 7. Channel state is orthogonal to conversation state

**Anti-pattern:** assuming `/clear` only resets the conversation. It also detaches the Discord channel binding — inbound messages keep arriving, nothing inside Claude Code is listening.

**Do this instead:** keepalive watchdog re-establishes the channel pairing after any reset. Treat channel pairing as its own piece of state; don't entangle it with conversation lifecycle.

**Lesson:** 8.

---

## 8. Heartbeats must be registered or they're dead

**Anti-pattern:** scaffolding a new scheduled feature (script + prompt + state file) without adding it to `HEARTBEATS.md`. The dispatcher reads the manifest at fire time; an unregistered heartbeat is silently dormant forever.

**Do this instead:** register every new heartbeat in `HEARTBEATS.md` in the SAME PR as the supporting scripts. Add a CI check (or pre-commit hook) that fails if a heartbeat-shaped script lands without a manifest entry.

**Lessons:** 11, 24.

---

## 9. Lint config files — don't trust silent fallbacks

**Anti-pattern:** assuming `git stash pop` cleanup is exhaustive. It's not — `<<<<<<< Updated upstream` markers can land in JSON/YAML configs and your script's `|| echo false` fallback silently treats the file as "disabled." Behavior reverts; nobody notices.

**Do this instead:** every JSON/YAML config in chassis ships with a CI check (`jq empty`, `yq eval`, `python -m yaml`) that fails the build on parse error. At minimum, pre-commit hook that runs the same checks locally.

**Lessons:** 14, 25.

---

## 10. UserPromptSubmit hooks don't fire on Discord channel inbound

**Anti-pattern:** writing a proof-of-life or activity-tracking hook based on `UserPromptSubmit` and assuming it fires on Discord-channel inbound messages. It doesn't — it fires only on directly-typed prompts.

**Do this instead:** if you need to know about channel activity, query the channel from the gather script. Don't rely on hooks catching a broader surface than they actually do.

**Lesson:** 16.

---

## 11. Three failure patterns recur — name them so you spot them

The same three shapes show up over and over. When you're debugging, match against this taxonomy first:

- **Silent dormancy** — feature is scaffolded but a single piece of config never flipped. Heartbeat without a manifest entry. Hook expecting a surface it doesn't fire on. Disabled module that was never re-enabled. Lessons: 11, 16, 22, 24.
- **Subtraction or race collapse** — two mostly-correct pieces of state math combine into a degenerate zero state. Negative-reference scorer collapsing to zero. Two heartbeats consuming the same destructive read. Merge marker silently disabling a feature. Lessons: 13, 14, 21.
- **Implicit assumptions about API behavior** — "surely it does X" where reality says Y. `gh issue list --label "a,b"` is AND not OR. `splitInBatches` iterates input items not nested arrays. Vercel SSO redirect returns 200. Lessons: 12, 15, 17, 18, 19, 23.

If a bug doesn't match one of these three, you might be in fresh territory — but check carefully first. Most don't.

**Lesson:** 25.

---

## 12. LaunchDaemons survive reboots; LaunchAgents don't

**Anti-pattern (macOS only):** putting reboot-critical infrastructure in `~/Library/LaunchAgents/`. The default `LimitLoadToSessionType=Aqua` binding means the service is loaded but inactive when nobody's logged into a GUI session. Auto-reboots silently disable everything.

**Do this instead:** anything reboot-critical (heartbeat dispatcher, watchdogs, Discord pairing, dashboard) goes in `/Library/LaunchDaemons/` with `UserName=<installer>` to drop privileges. LaunchAgents are fine for things that need the keychain or are user-facing GUI. Infrastructure goes in daemons.

**Lesson:** 26. (Linux installers: equivalent is `systemctl enable --now` for system-scope units vs `--user` units. Same lesson — don't put reboot-critical things in user scope.)

---

## 13. Don't grep raw command strings for shell-command analysis

**Anti-pattern:** writing a hook-layer guardrail as `grep -E '(curl|wget) ... -d'` over the whole bash command string. False-positives on documentation inside heredocs, command substitutions, and string literals. Blocks legitimate `gh issue create` calls because the body documents a `curl` example.

**Do this instead:** anchor command-name detection to actual command boundaries (start-of-string, after `;`, `&`, `|`, `(`, backtick, `$()`). Explicitly early-skip trusted CLI wrappers like `gh` and `tailscale` whose body is documentation. When the guardrail's job is "what does this shell command do," regex over the raw string is brittle — parse properly or scope the check to a known-safe wrapper list.

**Lesson:** 27. (See chassis hook `.claude/hooks/guardrails.sh` for the corrected pattern.)

---

## 14. Production state is "what's on disk right now," not "what's in the merge log"

**Anti-pattern:** assuming a merged PR is a deployed change. It's not — launchd-driven services read scripts from disk at fire time, no git-aware cache. A green PR landing on `origin/main` doesn't change behavior if the local checkout is on a stale feature branch and `git pull` never ran.

**Do this instead:** ship a repo-drift heartbeat that runs every ~30 min, fetches `origin/main` read-only across all tracked repos, and alerts if any commits behind touch heartbeat-runtime paths (`scripts/`, `scheduled-tasks/`, `skills/`, `.claude/hooks/`, `services/`). Detection-only — no auto-pull, because the right remediation depends on whether the local checkout has WIP and only the operator knows that.

**Lesson:** 28. (Chassis ships with the repo-drift heartbeat enabled by default.)

---

## 15. Privacy boundaries are surface-specific — default-deny per surface

**Anti-pattern:** "the agent can read everything I can read." WhatsApp DMs carry a privacy expectation from the other party that the installer cannot unilaterally consent to share. Same for Twitter / LinkedIn / Slack DMs. Group messages are different (already disclosed to N participants).

**Do this instead:** default-deny per surface. Allowlist explicitly. Document the gaps. Hook-layer enforcement so the LLM can't reach a banned surface even if the LLM tries.

Specific patterns shipped:
- WhatsApp: groups-only allowlist via wrapper script + hook block on raw `wacli messages`
- Twitter / LinkedIn / Slack: Playwright DM URLs blocked at hook layer; channel allowlists via config files
- Email: read everything, draft only — never auto-send

**Lesson:** 30. (Chassis `.claude/hooks/guardrails.sh` enforces this. See also `INSTALL_PROFILE.md` §4 trust line.)

---

## 16. Triangulate single-tool verdicts before acting on them (esp. similarity-based scoring)

**Anti-pattern:** acting on a single similarity-based tool's verdict. Yandex visual-similarity over-calls on common-aesthetic subjects (false positives). Same shape applies to any heuristic-based scorer at the tail of the distribution.

**Do this instead:** require consensus across multiple independent backends before taking destructive action. For dating photo verification: TinEye (byte-match) + Google Lens (face + celebrity ID) + PimEyes (face geometry) all agreeing → catfish; one or two disagreeing → walk it back. The cost of a false-positive destructive action is high enough that consensus isn't optional.

**Lesson:** 29. (See `plugins/dating/scripts/verify-match.py` for the consensus implementation.)

---

## 17. Chassis vendoring hygiene: never edit `chassis/` directly

**Anti-pattern:** installer customizations (heartbeats, gather scripts, prompts, custom scripts, dispatcher patches) land inside `chassis/`. On the next `git subtree pull --squash` from upstream, they either conflict every pull or get clobbered silently. Discovered during installer-1 install drift assessment (2026-05-12, <v1-reference-install>#547 sub-agent work) - 12 paths drifted inside `chassis/` despite the "never edit chassis/" rule because the rule wasn't codified anywhere installers would look.

**Do this instead:** treat `chassis/` as read-only after install. Customizations live OUTSIDE `chassis/` at parallel paths in the customer's repo:

| Customization type | Wrong location | Right location |
|---|---|---|
| Installer-rendered heartbeats config | `chassis/HEARTBEATS.md.template` (or pre-rename `chassis/HEARTBEATS.md`) | `${CHASSIS_HOME}/HEARTBEATS.md` (customer-repo root). Bootstrap copies the template here at install-time; installer edits the rendered file. |
| Plugin-bound gather scripts | `chassis/scheduled-tasks/gather/<plugin>-gather.sh` | `plugins/<plugin>/scheduled-tasks/gather.sh` (travels with the plugin on re-vendor) |
| Plugin-bound prompt files | `chassis/scheduled-tasks/<plugin>-prompt.md` | `plugins/<plugin>/scheduled-tasks/<name>-prompt.md` |
| Installer-owned (non-plugin) gather/prompt | `chassis/scheduled-tasks/<name>-{gather.sh,prompt.md}` | `${CHASSIS_HOME}/scheduled-tasks/<name>-{gather.sh,prompt.md}` (parallel to `chassis/`, in customer-repo root) |
| Installer-owned custom scripts | `chassis/scripts/<name>.{sh,py}` | `${CHASSIS_HOME}/scripts/<name>.{sh,py}` |
| Chassis-local bug patches (e.g. cross-platform shim) | inline edit of `chassis/scheduled-tasks/heartbeat-dispatcher.sh` | re-apply as commit in customer repo OUTSIDE `chassis/`, OR track in `project_chassis_jax_divergence_debt.md` as known divergence pending the broader chassis work that will obsolete it (e.g. containerization PR) |

The taxonomy split between **plugin-bound** and **installer-owned** matters: plugin-bound items travel with plugin re-vendoring; installer-bound items don't. Get this right at install time and re-vendoring stays clean for the life of the install.

**Do not reflexively upstream chassis-local patches.** Default is **local-track**, not PR. Some patches are deliberately local pending a planned broader chassis change (the installer-1 dispatcher patches are local pending containerization). Upstream only when (a) the fix is universally beneficial AND (b) it doesn't compete with planned work AND (c) the principal explicitly opts to PR.

**Lessons:** installer-1 install drift assessment (2026-05-12 #547). installer-2 install runbook follow-on.

---

## 18. Install-session state-loss is silent dormancy — write to memory, not to conversation context

**Anti-pattern:** trusting that a multi-day install will preserve its progress in the LLM's session context. Claude Code conversations are isolated — Day 2 opens with no knowledge of Day 1's SSH outputs, ratified files, or step completions. The agent will claim "no SSH session has happened yet" even when the prior session logged a completion in Discord.

**Do this instead:** every install-step transition writes a structured entity to the installer's chassis memory (e.g. `project_<installer>_install_state.md`). The driving session reads that file first, before issuing any SSH command. "What has been done" is a durable fact; it must not live in conversation context alone. Treat install-state-loss as the silent-dormancy pattern applied to time: the scaffolding (prior session's work) exists, but the signal (memory of it) is missing.

**Lesson:** 37.

---

## 19. Exposing secrets in Claude Code transcripts is a permanent leak — use targeted inspection, not file dumps

**Anti-pattern:** running `cat .mcp.json` or `cat .env` inside a Claude Code session to "check the config." These files contain live API keys and tokens. The full content appears in the session transcript, which is stored locally on whichever machine the Claude session ran on (and may be accessible to future sessions, log scrapers, or support).

**Do this instead:** inspect specific fields without dumping the whole file. Check keys present without printing values: `python3 -c 'import json; print(list(json.load(open(".mcp.json"))["mcpServers"].keys()))'`. If you must view a value, print only the key you need, not the whole file. Any secrets that landed in a transcript get rotated post-install — add them to the post-install rotation checklist.

**Lesson:** (installer-1 install 2026-05-07, per PR #57 security-hygiene section.)

---

## 20. INSTALL_PROFILE assumptions decay on contact with reality — probe, don't assume

**Anti-pattern:** drafting INSTALL_PROFILE.md from interview material, getting installer ratification, then treating it as ground truth at install time. Every field describing the installer's existing setup (ports, URLs, emails, service binaries, account names) is an assumption that will be wrong in at least one case. installer-1 install: VW URL wrong (8443 → 8222), VW master email wrong (ozzy@ → ben@benlakoff.com), Strava assumed needed but wrong, Notion scope assumed broad but actually narrow.

**Do this instead:** chassis hydration probes every live-system field before consuming it. `curl` the VW URL. Run `whoami`. Check `ollama --version`. Prefer live-probe truth over profile-doc truth for anything that describes an existing system state. Reserve profile-doc trust for intent-fields (what the installer WANTS), not reality-fields (what's currently on disk).

**Lesson:** 36.

---

## 21. "Plugin" and "core module" are semantically distinct — the validator must know the difference

**Anti-pattern:** treating every `chassis.config.yaml` module as requiring a `plugins/<name>/` directory. Core capabilities (briefing, crm, outreach, admin) are implemented in `chassis/scripts/` and ship unconditionally — they are not opt-in plugins. A validator that directory-checks ALL module names will reject valid core modules as "missing plugins," confusing installers.

**Do this instead:** maintain a registry of core module names in the chassis. The validator directory-checks only entries NOT in the core list. V2 recommendation: rename the config schema to `modules.core.*` vs `modules.plugins.*` to make the distinction machine-readable. Until that lands, any new module that ships in `chassis/scripts/` (not `plugins/<name>/`) must be added to the core-module exemption list explicitly.

**Lesson:** 33.

---

## 22. Vaultwarden writes invalidate the hydration cache — clear it, don't trust mtime

**Anti-pattern:** a host-side sidecar (e.g. an OAuth token refresher) writes a fresh value to Vaultwarden via `bw edit item`, then immediately re-bakes `.env.baked` so the container picks the value up on its next recreate. The rebake sources `.env`, which triggers `hydrate-from-vaultwarden.sh`. Hydrate caches `bw list items` output for 300s to amortize the cost across parallel gather scripts — so the rebake reads the OLD values, captures them into `.env.baked`, and the container restart loads stale secrets while VW holds the correct ones.

The symptom is sneaky: VW is correct, the sidecar's `bw edit` succeeded, but the container behaves as if the rotation didn't happen. Tracing through `.env.baked` confirms the staleness without revealing the cause unless you know about the cache TTL.

**Do this instead:** any caller that writes to VW and expects an immediate-consumer to see the new value must invalidate the cache file before the next hydrate call. The cache path follows the convention `${TMPDIR:-/tmp}/<v1-reference-install>-bw-items-cache.$(id -un).json` (see `hydrate-from-vaultwarden.sh`). `rm -f` is enough — hydrate already falls through to a fresh `bw list items` on cache miss.

For chassis-shipped scripts that round-trip VW writes (currently none, but adding any future sidecar of this shape qualifies), do this in the script. For installer-side scripts, document the convention so customers don't relearn it.

**Lesson:** (`scrollinondubs/new-jaxity#67`, fixed 2026-05-27 in the strava-refresh-sidecar. Surfaced during a Strava re-auth where every sidecar tick wrote fresh tokens to VW but `.env.baked` kept the previous values because the cache was younger than 300s.)

---

## 23. Heartbeats don't auto-create work — propose to the operator, file only on ratification

**Anti-pattern:** a heartbeat prompt evaluates incoming signal (research candidates, surfaced opportunities, errors that look like opportunities) against an internal pass/fail gate and, on PASS, calls `gh issue create` (or any equivalent "queue this for later" sink) directly. Each green-light pass injects unapproved work into the operator's queue without their knowledge. The queue accumulates items the operator never agreed to do — by the time they look, the backlog is full of speculative-research issues that aren't aligned with current priorities. The signal-to-noise ratio of the issue tracker collapses; the operator stops trusting their own queue as a source of "things I committed to."

The shape recurs because the prompt-author thinks of issue creation as "saving the suggestion so we don't lose it" rather than as "committing to do this work." From the heartbeat's perspective those look identical; from the operator's perspective they are not. A queue is a contract about future attention — auto-filing violates that contract.

Concrete instance: `scrollinondubs/new-jaxity` pulse-triage heartbeat filed 5 issues across 3 days against <v1-reference-install> (#703, #704, #705, #712, #713) before Sean noticed on 2026-05-31 and called it out. The agent's quality gate was tight (only 2-3 issues per day, dedup window, beneficial+feasible check) — the gate worked. The problem was structural: even high-quality auto-filing pollutes the queue with non-ratified work.

**Do this instead:** any heartbeat that surfaces "should we do X?" must post a **ratification proposal** to the operator's channel (`#<install>` Discord or equivalent), not file an issue directly. The proposal format must be self-contained — the operator should be able to react 👍 / 👎 / ❓ without re-reading the source material. Concretely:

```markdown
**Heartbeat proposal — react 👍 to greenlight, 👎 to reject, ❓ to defer**

**[<concise action title>](<source_url>)**
_Source: <briefing date or signal origin>_

**Case:** <1-2 sentences tying the proposal to a concrete install goal>

**Effort:** <small | medium | large + rough hours>

**Risk:** <one line>

**My take:** <✅ yes | 🟡 conditional | ⚠️ low-confidence> — <recommendation>
```

Filing happens in a separate flow that triggers on the operator's 👍 — either a follow-up reaction-watcher heartbeat or a verbal "file proposal N" from the operator. The heartbeat that surfaced the proposal must **never** call `gh issue create` itself.

State management: the heartbeat's state file gains a `pending_proposals[]` list (with `status: pending | filed | rejected`) so the ratification flow can resolve each proposal. The previously-named `issues_filed_today` counter is renamed to `proposals_posted_today` because filing is no longer the heartbeat's responsibility.

**Lesson:** (`scrollinondubs/new-jaxity#127`, merged 2026-05-31. Pulse-triage heartbeat rewritten from auto-file to propose-first. The 5 already-filed issues stayed open until the operator ratified or rejected each one in the channel; 3 stayed (annotated with the operator's conditions), 2 closed with reject reasons quoted.)

---

## How to use this document

1. **Before adding to chassis core:** scan this list for "have I solved this shape before?" If yes, follow the pattern.
2. **When debugging a chassis bug:** match against the three failure patterns in #11 first. Most bugs are silent-dormancy, subtraction-collapse, or implicit-assumption shaped.
3. **When a new lesson emerges:** append to `LESSONS_FROM_V1.md` (#31, #32, ...) AND add a corresponding pattern here. Don't leave new lessons undocumented; they're the highest-signal-per-byte chassis input we have.
4. **When reviewing a chassis PR:** cross-check the diff against the relevant patterns. A heartbeat-adding PR should also touch HEARTBEATS.md (#8). A new guardrail rule should anchor to command boundaries (#13). And so on.

---

*Distillation by ${ASSISTANT_NAME} 2026-05-05 from Sean's `LESSONS_FROM_V1.md`. Pattern numbers are stable — append, don't renumber.*
