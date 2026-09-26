# Laya on Cloudflare Containers

behalfbot#214, part of the FounderOS epic (scrollinondubs/behalfbot-plugins#22).
Laya is the System One model that handles FounderOS's high-volume tagging
(per-post pain tags, Mom Test tags, earlyvangelist criteria). This directory
serves it to the VCL SaaS path.

**Not deployed.** Each deploy needs Sean's explicit approval. The command is at
the bottom of this file.

## Measured numbers (read these first)

All measured locally on 2026-09-26 with the image in this directory. The client
ran on the host, and the post was a 90-word Sales Safari post with 4 questions
(2 yes/no, 1 choice, 1 score). The host was a Mac mini M4 running arm64 natively
in a dedicated colima VM, limited to `--cpus=0.5 --memory=4g` to match
standard-1.

| | english | multilingual |
|---|---|---|
| Cold start, `docker run` to healthy (3 runs) | 4.6 - 4.7 s | 6.6 s |
| **Per call, p50 of 10** | **8.41 s** (8.39 - 8.59) | 3.11 s (3.09 - 3.17) |
| Memory, idle after load | 2042 MiB | 1920 MiB |
| Memory peak, 12k-char post x 16 questions | 2818 MiB | 3809 MiB |
| 12k-char post x 16 questions, wall time | 137 s | 120 s |

- **An English call takes 8.4 s on half a vCPU.** Laya's English checkpoint is
  the one that works for English pain detection (AUC 0.78 on new-jaxity#603;
  multilingual was at chance). So the useful checkpoint is also the slow one,
  and nothing user-facing should wait on it. See the recommendation below.
- **The same image with 1 vCPU and 6 GiB (the standard-2 shape)** measured
  3.5 s cold start, 4.22 s p50 english and 1.54 s p50 multilingual.
- **x86 numbers are unmeasured.** Cloudflare runs linux/amd64. The amd64 image
  booted with no network at all (`--network none`) under Rosetta emulation and
  was healthy in 47 s. That proves the baked weights are complete, and it is
  an upper bound for cold start. Emulated inference was too slow to be
  meaningful (one english call ran past 600 s), so that attempt was stopped.
  Real per-call latency on a Cloudflare half vCPU is measured after deploy.
  Expect it to be the same order as the arm64 numbers, not better.
- **Image size:** 1.74 GB compressed (amd64), 2.7 GB unpacked, including
  1.45 GB of weights for both checkpoints. That is well inside standard-1's
  8 GB disk.

### Why one checkpoint per container

The brief asked for both checkpoints preloaded in one instance if they fit in
4 GiB. They do not:

- **Both resident (stock `laya-serve`, `LAYA_MODELS=english,multilingual`):**
  idle at 3.7 GiB of 4. The first multilingual call with a 12k-char post and
  4 questions was OOM-killed.
- **Lazy loading with one resident at a time (Router `max_loaded=1`):** the
  swap does not return memory, even with `gc.collect()` and `malloc_trim`.
  Multilingual alone was 1.6 GiB; after one round trip through english it sat
  at 3.2 GiB, and it grew on every further swap. One unlucky sequence would
  OOM the same way.
- **One checkpoint pinned per process (what ships):** english peaks at
  2.8 GiB, and multilingual at 3.7 GiB only on the 16-question stress call.
  The Worker caps calls at 8 questions to keep that margin.

So the image bakes both checkpoints, and `serve.py` pins each running instance
to one of them via `LAYA_MODEL`. The Worker runs one Durable Object and
container per checkpoint and routes by the request's `model` field (default
`english`). A multilingual container costs nothing until someone asks for
multilingual.

### Threads

A 0.5 CPU quota punishes idle OpenMP workers, which spin-wait and burn the
quota. With only `LAYA_THREADS=1`, an english call took 20.4 s. Adding
`OMP_NUM_THREADS=1` and `MKL_NUM_THREADS=1` brought it to 8.7 s. The Dockerfile
sets all three.

## Recommendation

**FounderOS uses the async job API only.** Tagging is batch work (a founder's
Sales Safari log, a set of interview utterances), and results feed a gate
review that happens later anyway. Submit a job, get an id back in
milliseconds, poll. A cold start, or the 8.4 s per English item, then never
blocks a request.

The synchronous `/v1/systemone` stays for debugging and one-off checks. If a
product surface ever genuinely needs a synchronous answer, move the english
instance to **standard-2**: 4.2 s per call measured locally, at about $41/mo
always-on per instance or $0.054 per awake hour (costs in
[docs/laya-cloudflare-cost.md](../../docs/laya-cloudflare-cost.md)). Do not
try to make standard-1 synchronous by keeping it warm: that costs about $28/mo
per instance and still answers in 8.4 s.

## What runs where

```
VCL backend (holds LAYA_API_TOKEN)
  -> https://behalfbot-laya.<subdomain>.workers.dev   (Bearer LAYA_API_TOKEN)
    -> Worker (src/index.ts): auth, validation, routing by `model`
      -> Durable Object "english" | "multilingual"  (LayaContainer)
           owns the job table (DO SQLite) and one container
        -> container: serve.py = laya-serve pinned to LAYA_MODEL, port 8080
```

- The container has no internet access (`enableInternet = false`), and the
  image sets `HF_HUB_OFFLINE=1`. A cold start cannot download anything.
- `sleepAfter = '10m'`: every proxied call, including each item a job drain
  sends, resets the timer.
- `max_instances: 2`, one per checkpoint. This is also the ceiling on the bill.

## API

Every route except `/healthz` needs `Authorization: Bearer <LAYA_API_TOKEN>`.

| Route | Does |
|---|---|
| `GET /healthz` | `{"ok":true}`. No auth, never wakes a container. |
| `POST /v1/systemone` | Body as laya-serve (`state`, `questions`, optional `model`). Synchronous; pays the cold start if the instance is asleep. |
| `POST /v1/jobs` | `{"model": "english", "items": [{"state": ..., "questions": {...}}, ...]}`. Answers `202 {"id", "status":"queued", "total", "poll"}` at once. |
| `GET /v1/jobs/:id` | `{"status": "queued" \| "running" \| "done", "total", "completed", ...}`, plus `results` once done: one `{index, status, body}` per item, in order. `status` is laya-serve's HTTP status for that item. |

Limits (enforced in the Worker, before any container wakes):

- 8 questions per call or item. This is the memory margin above; split larger
  question sets across items.
- 500 items per job. Request bodies are capped at 2 MiB (sync) and 16 MiB (job).
- `model` is `english` (the default) or `multilingual`. `typed-decisions` is not
  baked in, so it gets a 400.

Job behaviour:

- One job runs on one instance. The job's `model` overrides any per-item
  `model`, and the job id carries the checkpoint (`english-<uuid>`), so polling
  never wakes the other instance.
- The drain runs from the Durable Object's alarm (`Container.schedule`), in
  60-second slices. It survives the submitting request ending and a DO
  restart.
- A failure to start or reach the container is retried 3 times, 30 s apart. A
  4xx/5xx from laya-serve itself is the item's answer and is stored as-is.
- Finished jobs are purged 7 days after completion.

## Local verification

**Image.** Build it in its own colima VM so the measurements do not squeeze the
live chassis VM:

```bash
colima start laya-bench --activate=false --cpu 4 --memory 7 --vm-type vz --vz-rosetta
docker --context colima-laya-bench build --platform linux/arm64 -t behalfbot-laya-cf:arm64 .
docker --context colima-laya-bench run -d --cpus=0.5 --memory=4g --memory-swap=4g \
  -e LAYA_MODEL=english -p 127.0.0.1:18090:8080 behalfbot-laya-cf:arm64
curl -s 127.0.0.1:18090/health    # {"status":"ok","loaded":["english"],...}
colima stop laya-bench            # the Mac mini cannot spare 7 GiB for long
```

**Worker + Durable Object**, verified on 2026-09-26 with `wrangler dev --local`.
The run used a stub container that mimics `/v1/systemone`, so it did not need
x86 torch. It ran with an empty `HOME`, so wrangler had no Cloudflare
credentials, and `.dev.vars` held a throwaway token.

- `/healthz` returned 200, and missing or wrong bearer tokens got 401.
- Sync calls routed to separate english and multilingual containers, each of
  which started with the right `LAYA_MODEL`.
- A 9-question call and `typed-decisions` were rejected with a 400.
- A 3-item job got a 202 at once, polled `running`, then `done` 2 s later.
  Results were in order, and one item's 422 was stored as that item's answer.
- After `docker kill` of the english container, a new job restarted it and
  completed.

## Secrets

Set once with `wrangler secret put <NAME>`, entered interactively, never
committed:

| Secret | Purpose |
|---|---|
| `LAYA_API_TOKEN` | Bearer for every route except `/healthz`. Mint fresh (`openssl rand -hex 32`), store in Vaultwarden, give it to the VCL backend only. |

That is the whole set. This is a separate Worker from `behalfbot-executor` and
`behalfbot-support-agent`, so nothing here can reach Turso, GitHub or an
Anthropic key.

## Deploy (GATED - needs Sean's explicit approval)

Cloudflare auth is the same as for the executor: the account-owned
`behalfbot-fable-cf-containers` token from Vaultwarden, exported per
[../executor/README.md](../executor/README.md). `account_id` is pinned in
`wrangler.jsonc`, so a wrong token fails closed.

```bash
cd cloudflare/laya
npm install
npx wrangler whoami                  # MUST show account 8a119b24123c444dddea567ffde1a405
npx wrangler secret put LAYA_API_TOKEN
npx wrangler deploy                  # builds the amd64 image locally, pushes, deploys
```

After deploy:

1. `curl -s https://behalfbot-laya.<subdomain>.workers.dev/healthz`
2. Submit a small english job, and time the gap from submit to the first
   `completed: 1`. That gap is the real cold start.
3. Time 10 sync calls against the warm instance. That is the real per-call
   latency on half a vCPU.
4. Post both numbers on #214 and update the cost doc.
