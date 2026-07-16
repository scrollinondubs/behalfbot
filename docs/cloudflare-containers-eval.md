# Cloudflare Containers eval - executor host (issues #41 / #66)

Date: 2026-07-16. Sources: developers.cloudflare.com/containers (limits, FAQ, pricing, outbound-traffic, container-package, get-started pages), fetched live on the eval date. Containers GA'd 2025; re-verify limits before any capacity change.

## Verdict: GO-WITH-CAVEAT

Cloudflare Containers fits the executor's profile (clone + `claude -p` + parse, 15-20 min worst case). The one real caveat is **durability, not duration**: there is no enforced runtime cap, but Cloudflare does not guarantee any instance survives a host restart. The executor's existing orphan-sweep + retry semantics already cover that failure mode, so the caveat is absorbed by a pattern we already run. Details below.

## 1. Max duration (THE critical question)

**Finding: there is NO enforced maximum wall-clock duration for a container instance.** From the FAQ: "Cloudflare will not actively shut off a container instance after a specific amount of time." A 15-20 min `claude -p` run is fine.

Two nuances:

- **No survival guarantee.** Host servers restart "on an irregular cadence", and Cloudflare "does not guarantee that any instance will run for any set period of time." A job can die mid-run at any point. This is the caveat in GO-WITH-CAVEAT.
- **The Workers 5-min CPU cap does not apply to the container.** The Worker is only a thin trigger/proxy in front of the Container-class Durable Object; the container's own processes are not metered as Worker CPU time. The Worker stub returns 202 immediately and never holds a connection for the job's duration, so no Worker limit is ever in play.

**Pattern that covers long jobs (implemented in the scaffold):**

1. Mac mini heartbeat POSTs `/trigger` to the Worker (bearer-token auth).
2. Worker forwards to the singleton container instance; the in-container shim starts one queue tick as a child process and answers 202 immediately.
3. `sleepAfter = "45m"` on the Container class, comfortably above the executor's 30-min per-ask wallclock cap, so the idle timer can never reap a live job. Each trigger request resets the timer ("Activity resets the timer").
4. Job state lives in Turso (`behalfbot_queue` row status), exactly as today. If a host restart kills a run mid-flight, the row stays `processing` and the existing `sweepOrphanedProcessingRows()` marks it failed + refunds spend on the next tick - the same recovery path the Mac mini already relies on for its own 30-min timeout sweep. No new failure mode, only a new trigger for an existing one.

No queues or Durable Object alarms are required for v1. If we later want Cloudflare-native scheduling (dropping the Mac mini poke), a Worker cron trigger replaces the poke with zero container-side changes.

## 2. Outbound network

**Finding: GO, no proxy gymnastics.** "By default, a Container will allow internet access." HTTPS to arbitrary hosts works out of the box, which covers both needs:

- `git clone https://x-access-token:<PAT>@github.com/...` (the processor already clones over HTTPS with the PAT, not SSH)
- `POST https://api.anthropic.com` (the `claude` CLI with `ANTHROPIC_API_KEY` set)
- Turso over HTTPS/WebSocket (`libsql://...` resolves to 443)

Hardening bonus available later: `enableInternet = false` plus an `allowedHosts` allowlist (github.com, api.github.com, api.anthropic.com, the Turso host) would give the container a default-deny egress posture the Mac mini could never offer. Not required for v1; noted in the scaffold as a follow-up.

## 3. Memory / instance type

Instance types (from the limits page):

| Type | vCPU | Memory | Disk |
|------|------|--------|------|
| lite | 1/16 | 256 MiB | 2 GB |
| basic | 1/4 | 1 GiB | 4 GB |
| standard-1 | 1/2 | 4 GiB | 8 GB |
| standard-2 | 1 | 6 GiB | 12 GB |
| standard-3 | 2 | 8 GiB | 16 GB |
| standard-4 | 4 | 12 GiB | 20 GB |

**Finding: `standard-1` (1/2 vCPU, 4 GiB, 8 GB disk) fits.** Working set = shallow member-repo clone (tens to hundreds of MB) + Node/tsx processor + the `claude` CLI child (typically well under 2 GiB). 8 GB disk holds the baked executor code (~1 GB with node_modules) plus per-job scratch clones with room to spare. If a pathological member repo blows the clone size, `standard-2` is a one-line `instance_type` bump. Custom shapes up to 4 vCPU / 12 GiB / 20 GB exist if ever needed.

## 4. Scale-to-zero + cold start

**Finding: exactly the model Sean asked for in #66.** Billing is "for every 10ms that they are actively running"; "charges start when a request is sent to the container or when it is manually started. Charges stop after the container instance goes to sleep." With `sleepAfter` set, the instance sleeps 45 min after the last trigger and costs zero until the next poke.

Cold starts: "often in the 1-3 second range", image-size dependent. Irrelevant for a nightly batch executor; fine even for the future #66 support agent.

**Cost estimate** (Workers Paid plan, includes 375 vCPU-min + 25 GiB-h + 200 GB-h disk free per month): a worst-case 20-min job on standard-1 costs roughly $0.012 vCPU + $0.012 memory + $0.001 disk = **about 2.5 cents per long job** before the free allotment, which will usually absorb the whole nightly volume. Egress $0.025/GB after 1 TB included (EU). Cost is a non-issue.

## 5. Account-level limits

50 GB image storage per account, 6 TiB concurrent memory, 1500 concurrent vCPU. All orders of magnitude above this workload. `max_instances: 1` in the scaffold preserves the single-in-flight contract (belt) alongside the DB-level optimistic `UPDATE ... WHERE status='pending_execution'` (braces).

## Summary table

| Requirement | Limit found | Fit |
|---|---|---|
| 15-20 min `claude -p` runs | No enforced duration cap; no survival guarantee across host restarts | GO with existing sweep/retry |
| git clone + Anthropic API egress | Internet on by default, HTTPS anywhere | GO |
| Working set (clone + node + claude CLI) | standard-1: 4 GiB / 8 GB disk | GO |
| Scale-to-zero | Per-10ms billing, sleeps after `sleepAfter` idle | GO |
| Cold start | 1-3 s typical | GO |
| Cost | ~$0.025 worst-case job, mostly inside free allotment | GO |

## Decisions that stay gated on Sean

1. Deploy approval - nothing in this PR touches the live Cloudflare account; `wrangler deploy` has not been run.
2. Cutover approval - Mac mini LaunchAgents stay enabled until Sean signs off on the smoke test.
3. Whether to adopt `enableInternet = false` + allowlist hardening in v1 or as a follow-up.
4. The #66 support-agent container (second Container class, same Worker) - separate scope, lands after this.
