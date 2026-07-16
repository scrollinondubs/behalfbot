# Behalf.bot executor on Cloudflare Containers

Scaffold for behalfbot#41 (executor off the Mac mini) and the shared
foundation for behalfbot#66 (VCL support agent). **Nothing here is
deployed.** Deploy and cutover are gated on Sean's explicit approval.

Go/no-go analysis: [docs/cloudflare-containers-eval.md](../../docs/cloudflare-containers-eval.md).
Verdict: GO-WITH-CAVEAT (no enforced duration cap; host restarts covered by
the existing orphan sweep).

## What runs where

```
Mac mini heartbeat (launchd, unchanged schedule)
  -> POST https://behalfbot-executor.<subdomain>.workers.dev/trigger   (Bearer EXECUTOR_TRIGGER_TOKEN)
    -> Worker stub (src/index.ts) forwards to the singleton container
      -> container shim (container/server.mjs) answers 202, spawns one tick
        -> tsx scripts/behalfbot-heartbeat.ts   (unmodified vibecodelisboa executor)
          -> clone member repo / claude -p --bare / parse / PR / notify
```

The executor code itself is NOT vendored here - `build.sh` stages a
stripped shallow clone of scrollinondubs/vibecodelisboa into
`build-context/` (gitignored) at image-build time. The processor, its
security defenses, and the queue semantics stay canonical in that repo.

## Trust boundary (behalfbot#66) - packaging, not prompts

The image structurally contains:

- node 22 + git + the `claude` CLI
- the executor slice of vibecodelisboa (fresh shallow clone, `.git`
  removed because that repo's history contains leaked prod secrets
  pending rotation, `.env*` scrubbed)
- the HTTP shim

It structurally does NOT contain: chassis skills, dating context or
skills, `~/jax-private` anything, SiYuan content or credentials,
Discord-bridge identity, Vaultwarden access, Postgres access, or any OAuth
token of Sean's. The container's secret set (below) cannot reach any of
those systems.

Preserved executor defenses (all live in vibecodelisboa code, verified
2026-07-16 in `src/lib/contribution-ledger/behalfbot-prompts.ts` and
`behalfbot-safety.ts`):

- `claude -p --bare` (no hooks, no CLAUDE.md auto-discovery, no keychain)
- `--allowedTools Read,Glob,Grep,WebFetch,WebSearch` and
  `--disallowedTools Bash,Edit,Write,MultiEdit,Task,SkillRun,KillBash,BashOutput,NotebookEdit,mcp__*`
- workdir scrub of `.claude/`, `.mcp.json`, hook files before every run
- `git -c core.hooksPath=/dev/null` on all git ops
- cwd + `--add-dir` pinned to the per-job scratch clone
- dedicated `BEHALFBOT_ANTHROPIC_API_KEY` swapped into the child env
  (fail-closed if missing) - billing NEVER touches Sean's Max subscription
- spend caps (monthly USD cap fail-closed at 90%, per-row token budget)
- 25-min CLI timeout, 30-min row wallclock sweep; the shim adds a 35-min
  hard kill and the Container class sleeps only after 45 idle minutes

Additional isolation the Mac mini never had: non-root runtime user,
ephemeral filesystem per instance lifetime, and (optional follow-up)
`enableInternet = false` + an `allowedHosts` egress allowlist.

## Secret migration

Container/Worker secrets, set once via `wrangler secret put <NAME>` (values
sourced from Vaultwarden / the existing Mac mini env, entered
interactively, never committed):

| Secret | Purpose | Source of truth today |
|---|---|---|
| `EXECUTOR_TRIGGER_TOKEN` | authenticates the Mac mini poke | mint fresh (32+ random bytes), store in Vaultwarden |
| `DATABASE_URL` | Turso (vibecodelisboa prod) | Mac mini env / Vaultwarden |
| `DATABASE_AUTH_TOKEN` | Turso auth | Mac mini env / Vaultwarden |
| `ENCRYPTION_SECRET` | repo-credential decryption | Mac mini env (`NEXTAUTH_SECRET` fallback exists in code; set the real one) |
| `GITHUB_PAT` | jacketyjax PAT - member-repo clone, push, PR | `/Users/jax/.behalfbot/.env` |
| `BEHALFBOT_ANTHROPIC_API_KEY` | dedicated Anthropic key, $60/mo Console cap | Anthropic Console / Vaultwarden |

That is the complete set. Anything not in this table (Vaultwarden master,
Postgres DSN, SiYuan token, Discord token, OAuth refresh tokens) must
never be `wrangler secret put` on this Worker - that is the #66 boundary.

### Cloudflare auth for wrangler (deploy-time only)

The deploy token is account-owned, named `behalfbot-fable-cf-containers`,
scoped to sean@grid7.com's account `8a119b24123c444dddea567ffde1a405`
only (Workers Scripts:Edit, Workers Containers:Edit, Cloudchamber:Edit,
Workers R2 Storage:Edit, Account Settings:Read). Wrangler reads it from
the `CLOUDFLARE_API_TOKEN` env var; it is never hardcoded anywhere.

**Trap:** the ambient `CLOUDFLARE_API_TOKEN` in the Jax install
environment belongs to the AllBets account. Every deploy shell must
override it from Vaultwarden first:

```bash
export BW_SESSION="$(bash /Users/jax/.behalfbot/scripts/bw-unlock.sh)"
export CLOUDFLARE_API_TOKEN="$(bw get item 937aaaf0-48e6-40de-8ba0-b4290ad5328d --session "$BW_SESSION" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['login']['password'])")"
wrangler whoami   # MUST show Sean@grid7.com's Account / 8a119b24123c444dddea567ffde1a405
```

`account_id` is also pinned in `wrangler.jsonc`, so a wrong token
fails closed instead of deploying to the wrong account.

## Deploy + cutover plan (GATED - nothing below runs without Sean's OK)

1. **Build**: `GITHUB_PAT=... ./build.sh` (stages build-context), then
   `npm install` here.
2. **Secrets**: `wrangler secret put` the six names above.
3. **Deploy**: `wrangler deploy` (builds the image via local Docker,
   pushes to the Cloudflare-managed registry, deploys Worker + container).
4. **Smoke test**: seed a test ask on a throwaway repo, `curl -X POST
   .../trigger -H "Authorization: Bearer ..."`, watch `GET /status` until
   the tick completes, confirm the PR opens and the queue row lands in
   `completed`. Mac mini executor stays enabled throughout.
5. **Cutover** (separate Sean approval): repoint the two launchd plists'
   scripts to `curl` the Worker instead of running the local tick.
   Prescan uses `POST /trigger?mode=prescan`. Keep the plists themselves -
   the Mac mini remains the scheduler for now; only execution moves.
6. **Bake period**: one week of both paths observable (Worker path live,
   Mac mini path disabled but re-enablable with one `launchctl` command).
7. **Decommission** (behalfbot#41 step 5): remove the local executor code
   path and archive logs. Logs on the CF side: `observability.enabled`
   streams to Workers Logs; longer retention (R2 sink) is a follow-up.

## Follow-ups (out of scope for this PR)

- `enableInternet = false` + `allowedHosts` egress allowlist
  (github.com, api.github.com, api.anthropic.com, Turso host)
- Worker cron trigger to replace the Mac mini poke entirely
- behalfbot#66 support-agent container: second Container class in this
  same Worker, its own image with the support/managed-install docs slice
  and NO repo-write credentials
- R2 log retention sink
