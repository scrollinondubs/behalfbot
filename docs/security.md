# Chassis Security Model

The chassis runs Claude Code with `--dangerously-skip-permissions`. This is required because Discord-channel mode is incompatible with Anthropic's auto-mode classifier (lesson #4) — runtime approval prompts try to render to a terminal nobody's watching, and the inbound message silently stalls.

In exchange, **deterministic safety lives at the hook layer**, not in CLAUDE.md alone. The LLM cannot talk its way around hook-enforced rules.

## Hook layer

`chassis/.claude/hooks/guardrails.sh` is the single source of truth. It's invoked before every tool call (PreToolUse hook). Returns `permissionDecision: deny` to block; exits 0 to allow.

What it blocks (chassis-universal):

| Block | Lesson | Notes |
|---|---|---|
| `rm -rf /`, `sudo rm`, `rm -rf ~` | — | Use `trash` instead |
| `git push --force` / `-f` | — | Use `git revert` |
| `git push <anything> main` / `master` | — | Create a PR |
| `git reset --hard` | — | Use `git stash` or `git revert` |
| `git clean -f` | — | Review files manually |
| `DROP TABLE` / `TRUNCATE` / `DELETE FROM` (any CLI) | — | Exception: tables/DBs ending `-preview` |
| Lat/lng coords in `git commit`, `git add`, `gh issue/pr create/comment` | — | Angel Protocol rule; coords are local-only |
| `mail` / `sendmail` / `mutt` / `msmtp` invocations | — | Email sends require installer approval |
| Mutating HTTP (`curl -X POST/PUT/DELETE/PATCH`, `--data`) outside allowlist | #27 | Anchored regex — no heredoc false positives |
| Raw `wacli messages\|send\|media` outside the safe wrapper | #30 | Conditional on WhatsApp plugin being enabled |
| Twitter / LinkedIn / Slack DM URLs in Playwright commands | #30 | Privacy boundary — DMs carry sender's expectation |
| GitHub `mcp__github__merge_pull_request` | — | Installer reviews + merges |
| GitHub `mcp__github__delete_repository` | — | Repo deletion prohibited |
| Turso `mcp__turso__delete_database` | — | DB deletion prohibited |
| Turso destructive SQL on non-`-preview` DBs | — | Production DB protection |
| `Write` / `Edit` outside `CHASSIS_HOME` (or `CHASSIS_DIR_ALLOWLIST`) | — | Plus always-allow for `~/.claude/` harness config |

## Configuration

The hook reads its allowlists from environment variables, populated by the chassis bootstrap script from `chassis.config.yaml`:

```yaml
guardrails:
  directory_allowlist:
    - ${CHASSIS_HOME}
    - /home/installer/some-other-repo
  http_allowlist:
    - localhost
    - 127\.0\.0\.1
    - my-self-hosted-n8n\.example\.com
    - notes\.example\.com
  e2e_mode_flag_path: ${CHASSIS_HOME}/.claude/<v1-reference-install>-e2e-mode.flag
  e2e_mode_max_age_seconds: 7200
  whatsapp_safe_wrapper: ${CHASSIS_HOME}/plugins/whatsapp/scripts/wacli-safe.sh
```

Bootstrap → env-var translation:

```bash
export CHASSIS_DIR_ALLOWLIST="${CHASSIS_HOME}:/home/installer/some-other-repo"
export CHASSIS_HTTP_ALLOWLIST='(localhost|127\.0\.0\.1|my-self-hosted-n8n\.example\.com|notes\.example\.com)'
export CHASSIS_E2E_FLAG_PATH="${CHASSIS_HOME}/.claude/<v1-reference-install>-e2e-mode.flag"
export CHASSIS_WHATSAPP_SAFE="${CHASSIS_HOME}/plugins/whatsapp/scripts/wacli-safe.sh"
```

The launchd plist (macOS) or systemd unit (Linux) wiring the dispatcher needs these in its `Environment=` block (or sources a single `chassis-env.sh`).

## E2E mode flag

Some test workflows need to write to Discord webhooks or Stripe test endpoints — both blocked by default. The E2E flag is a session-scoped escape hatch:

```bash
touch ${CHASSIS_HOME}/.claude/<v1-reference-install>-e2e-mode.flag   # ON (auto-expires after 2h)
rm -f ${CHASSIS_HOME}/.claude/<v1-reference-install>-e2e-mode.flag   # OFF
```

When the flag is present and younger than 2h, the HTTP allowlist extends to cover `discord.com/api/webhooks`, `api.stripe.com`, `api.telnyx.com`. The 2h auto-expire is a safety net (lesson #12 — auto-on switches need aggressive auto-off).

## Adding a new MCP that hits external APIs

If you wire a new MCP that talks to a third-party API:

1. Identify the host(s) the MCP hits (e.g. `api.notion.com` for Notion).
2. Add to `chassis.config.yaml.guardrails.http_allowlist` as an escaped regex fragment.
3. Re-run the bootstrap script (or manually update the env-var file).
4. Restart the chassis launchd / systemd unit so it picks up the new env.

Per lesson #28 — what's on disk is what runs. A merged config change is not live until the process re-reads.

## Plugin-extending the guardrails

Plugins can extend the rules but cannot weaken them:

- **dating** plugin enables the Twitter / LinkedIn / Slack DM block unconditionally (already in chassis core)
- **whatsapp** plugin sets `CHASSIS_WHATSAPP_SAFE` to its `wacli-safe.sh` wrapper path; chassis sees the env var and activates the WhatsApp DM block
- **angel-protocol** plugin doesn't extend (the lat/lng block is in chassis core); it documents that the local-only `~/.angel-vault/` is the intended store

Plugins MUST NOT bypass the chassis-core hook. Per lesson #6 + #4 — hook-layer enforcement is the security boundary.

## What's NOT enforced at the hook layer

- **Skill-level guidance** (e.g. "never dinner first" in dating). Lives in the skill body
- **Semantic correctness** (e.g. "reads as installer's voice"). The hook is structural, not semantic
- **Memory writes**. CLAUDE.md auto-memory rules cover that
- **Discord channel moderation**. Discord's role system handles who can post where

Intentionally out of scope. The hook is for hard limits the LLM might rationalize past; everything else lives in CLAUDE.md / skills / memory.

## Cross-references

- `chassis/.claude/hooks/guardrails.sh` — the hook itself
- `docs/architectural-anti-patterns.md` — pattern #4 + #13
- `docs/LESSONS_FROM_V1.md` — full lesson list, especially #4, #6, #13, #27, #30
- `docs/mcp-setup.md` — when adding a new MCP, also extend the HTTP allowlist
