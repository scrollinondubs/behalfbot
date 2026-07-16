# Behalf.bot support agent on Cloudflare Containers

Scaffold for behalfbot#66: a general-purpose support agent that answers
VCL course tech-support questions and guides Behalf.bot managed installs,
off the Mac mini. Second container class beside the #41 executor
(`../executor/`), sharing its Dockerfile pattern and host decision but
with a much sharper trust boundary. **Nothing here is deployed.** Deploy
and intake wiring are gated on Sean's explicit approval.

Host go/no-go analysis: [docs/cloudflare-containers-eval.md](../../docs/cloudflare-containers-eval.md)
(#41's GO-WITH-CAVEAT stands; this workload is strictly easier - shorter
runs, no repo writes, no DB).

## What runs where

```
Intake surface (Discord bridge relay / future web form)
  -> POST https://behalfbot-support-agent.<subdomain>.workers.dev/ask   (Bearer SUPPORT_TRIGGER_TOKEN)
    -> Worker stub (src/index.ts) forwards to the container
      -> container shim (container/server.mjs) runs one `claude -p --bare`
         over the read-only baked docs corpus, answers synchronously
```

Scale-to-zero: `sleepAfter = 15m`, per-10ms billing while awake, zero
while asleep. Cold start 1-3 s is fine for a support ask.

## The #66 trust boundary - packaging, not prompts

Three structural layers, each independently sufficient to stop a class of
leak. None of them is a prompt instruction.

### Layer 1: the image contains only the allowlisted corpus

`build.sh` stages `build-context/knowledge/` strictly from
`knowledge-manifest.txt` (deny-by-default; unlisted paths do not exist in
the image), then runs a fail-closed verifier that aborts the build if any
staged path matches `dating|whatsapp|bfl|jax-private|welfare-check|vaultwarden|.env`.

What IS in the image, and why each item is safe:

| Content | Why it is safe |
|---|---|
| behalfbot repo docs (`docs/*.md` per manifest) + chassis/ + install entrypoints (README, INSTALL_PROFILE, bootstrap.sh, compose files) | The PUBLIC chassis repo: it is what every customer already receives. This is the managed-install corpus the agent quotes from. |
| `persona/SUPPORT_AGENT.md` | Written for this scaffold; support persona, scope limits, injection framing. Contains no personal or business-internal facts. |
| `container/server.mjs` shim | This scaffold; no secrets, no state. |
| node 22 + the `claude` CLI | Runtime only. |

What is PROVABLY ABSENT: dating skills and dating context, WhatsApp/BFL
and other personal-life plugins, `~/jax-private` anything, Sean's memory
graphs, SiYuan content, company-confidential material, git, and any
repo-write tooling. `build.sh` reads only from this public repo checkout;
it has no mechanism to reach Sean's install (new-jaxity), his home
directory, or any private repo - there is no PAT in the build at all.

### Layer 2: the credential set cannot reach anything of Sean's

The COMPLETE secret set for this Worker:

| Secret | Purpose | Source of truth |
|---|---|---|
| `SUPPORT_TRIGGER_TOKEN` | authenticates the intake surface | mint fresh (32+ random bytes), store in Vaultwarden |
| `BEHALFBOT_ANTHROPIC_API_KEY` | Anthropic billing, dedicated key | Anthropic Console / Vaultwarden - NEVER Sean's Max subscription |

Two secrets. The container itself receives exactly ONE
(`BEHALFBOT_ANTHROPIC_API_KEY`); the shim passes the claude child a
scrubbed env (HOME, PATH, that key). There is no credential in the
Worker, the container, or the image that can reach Vaultwarden, Postgres,
SiYuan, the Discord-bridge identity, GitHub, Turso, or any OAuth token of
Sean's. Never `wrangler secret put` anything beyond the two names above
on this Worker - that is the boundary.

This is also why the support agent is a SEPARATE Worker rather than a
second Container class inside `behalfbot-executor` (which the executor
README floated as an option): Worker secrets are scoped per Worker, so
separation makes "the executor's GITHUB_PAT/Turso/ENCRYPTION_SECRET can
never appear in the support agent's env" a platform guarantee instead of
a code-review promise.

### Layer 3: runtime posture

- `claude -p --bare`: no hooks, no CLAUDE.md auto-discovery, no keychain.
- `--allowedTools Read,Glob,Grep,WebFetch,WebSearch`, everything that
  writes/executes/reaches MCP denied (same posture as the executor).
- cwd + `--add-dir` pinned to `/app/knowledge`, which is root-owned and
  read-only to the non-root `support` runtime user.
- Question + context framed as untrusted data in the prompt; the persona
  is injection-aware, but nothing DEPENDS on the persona - a fully jailbroken
  instance still has nothing to leak and no credential to abuse. Worst
  case is a wrong answer and wasted API spend, capped below.
- 10-min hard kill per ask, 2 concurrent asks max per instance, input
  size caps, fail-closed if the API key is missing.

## Session/state continuity - recommendation

**Recommendation: stateless container, caller-owned continuity.** Each
/ask is self-contained; the intake surface passes prior turns in the
`context` field (Discord threads already hold the transcript, so the
bridge relay can replay the last N turns cheaply).

Why:

1. Cloudflare gives no instance-survival guarantee and we want
   scale-to-zero, so in-container session state is unreliable by design.
2. A support conversation's real state is small (a few turns of text) and
   already lives where the member is talking. Replaying it costs a few KB
   per request.
3. Managed installs DO need durable multi-day state, but the right home
   for that is the per-customer install repo / GitHub issue journal
   (already the pattern per docs/per-customer-repo-pattern.md), not
   container memory. The agent reads whatever the intake includes.

If the intake surface later cannot replay history, the clean CF-native
upgrade is storing transcripts in the Durable Object's SQLite keyed by a
`session_id` field - the DO already fronts the container and survives
sleeps. That is a small, additive change; do not build it until a real
intake needs it.

## Cloudflare auth for wrangler (deploy-time only)

Same as the executor. The deploy token is account-owned, named
`behalfbot-fable-cf-containers`, scoped to sean@grid7.com's account
`8a119b24123c444dddea567ffde1a405` only. Wrangler reads it from the
`CLOUDFLARE_API_TOKEN` env var; it is never hardcoded anywhere.

**Trap:** the ambient `CLOUDFLARE_API_TOKEN` in the Jax install
environment belongs to the AllBets account. Every deploy shell must
override it from Vaultwarden first:

```bash
export BW_SESSION="$(bash /Users/jax/.behalfbot/scripts/bw-unlock.sh)"
export CLOUDFLARE_API_TOKEN="$(bw get item 937aaaf0-48e6-40de-8ba0-b4290ad5328d --session "$BW_SESSION" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['login']['password'])")"
wrangler whoami   # MUST show Sean@grid7.com's Account / 8a119b24123c444dddea567ffde1a405
```

`account_id` is also pinned in `wrangler.jsonc`, so a wrong token fails
closed instead of deploying to the wrong account.

## Deploy plan (GATED - nothing below runs without Sean's OK)

1. **Build**: `./build.sh` (stages + verifies the corpus; no PAT needed),
   then `npm install` here.
2. **Secrets**: `wrangler secret put` the two names above.
3. **Deploy**: `wrangler deploy`.
4. **Smoke test**: `curl -X POST .../ask -H "Authorization: Bearer ..."`
   with a real install question ("how do I re-run hydration?"), confirm
   the answer cites the right doc and the Mac mini is not involved.
5. **Intake wiring** (separate approval): point the Discord-bridge relay
   for course-support channels at this Worker.

## Decisions that stay gated on Sean (pre-deploy)

1. **Deploy approval** - nothing in this PR touches the live Cloudflare
   account.
2. **VCL course-content corpus** - the baked corpus today is the chassis
   install docs only. Answering COURSE questions well likely needs a
   slice of the course material itself (lesson runbooks, cohort FAQs).
   That content lives in private repos/SiYuan, so what gets baked - and
   whether any of it is too close-to-the-vest - is Sean's call. The
   manifest + verifier pattern extends to it cleanly once decided.
3. **Anthropic key split** - reuse the executor's
   `BEHALFBOT_ANTHROPIC_API_KEY` or mint a second Console key so support
   spend is separately capped/attributable and revocable without touching
   the executor. Recommendation: separate key, same $-capped Console
   workspace pattern.
4. **Software-engineering skill port** - the canonical
   `skills/software-engineering.md` lives in Sean's private install repo.
   The persona currently embeds condensed guidance; if Sean wants the
   full skill baked, it should be ported into this public repo (scrubbed)
   and added to the manifest, not pulled from new-jaxity at build time.
5. **Intake surface** - which channel(s) feed /ask, and who holds
   `SUPPORT_TRIGGER_TOKEN`.
6. **Egress hardening** - `enableInternet = false` + `allowedHosts`
   (api.anthropic.com only, since this agent needs no git/Turso) would
   give the support container the tightest egress of anything we run.
   WebFetch/WebSearch would stop working; alternative is keeping them and
   accepting default egress. Sean's call on v1 vs follow-up.
