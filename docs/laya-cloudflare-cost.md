# Laya on Cloudflare Containers: cost

behalfbot#214. Rates come from the Cloudflare Containers pricing page (last
updated 2026-08-28). Latencies are the local measurements in
[cloudflare/laya/README.md](../cloudflare/laya/README.md): arm64 limited to the
instance's CPU and memory. The real x86 numbers come after deploy.

## The numbers that drive everything

| | standard-1 (1/2 vCPU, 4 GiB) | standard-2 (1 vCPU, 6 GiB) |
|---|---|---|
| english call, p50 | **8.41 s** | 4.22 s |
| multilingual call, p50 | 3.11 s | 1.54 s |
| cold start (local arm64) | 4.6 s english / 6.6 s multilingual | 3.5 s |
| cold start (x86 under emulation, no network) | 47 s, upper bound | not measured |

## Rates

Workers Paid ($5/mo) includes 25 GiB-hours of memory, 375 vCPU-minutes and
200 GB-hours of disk a month. Beyond that:

- memory: $0.0000025 per GiB-second, which is $0.009 per GiB-hour
- CPU: $0.000020 per vCPU-second, **active usage only**, which is $0.072 per
  busy vCPU-hour
- disk: $0.00000007 per GB-second, which is $0.000252 per GB-hour

Memory and disk bill for the provisioned size whenever the instance is awake.
CPU bills only while it computes.

| Per awake hour | standard-1 | standard-2 |
|---|---|---|
| memory | $0.036 | $0.054 |
| disk | $0.002 | $0.003 |
| CPU, only while computing | up to $0.036 | up to $0.072 |

The included memory covers about 6.25 awake hours a month on standard-1.

## Always-on

Assumes 730 hours a month, after the included usage, with CPU idle. Busy CPU
comes on top.

| Setup | Monthly |
|---|---|
| 1 x standard-1 (english only) | **$27.48** ($26.06 memory + $1.42 disk) |
| 2 x standard-1 (english + multilingual, as configured) | $55.23 |
| 1 x standard-2 (english only) | $41.36 ($39.20 memory + $2.16 disk) |

Always-on buys no speed: a warm standard-1 still takes 8.4 s per English call.
It only removes the cold start, which is 5 s locally and unmeasured on x86.

## Scale to zero (configured: `sleepAfter = '10m'`)

Each wake costs the cold start plus a 10-minute idle tail. That is about
$0.006 of memory on standard-1, and the CPU is idle during the tail. Each
English item then costs about $0.00017 once the allowance is used up: 8.41 s
of memory plus 8.41 s at half a vCPU.

The included CPU covers about 12.5 busy hours, or roughly 5,300 English items
a month, before CPU billing starts.

| Scenario, standard-1, english | Busy | Awake (busy + tails) | Monthly after inclusions |
|---|---|---|---|
| Cohort: 20 founders x 200 items, one job per founder a week (80 wakes) | 9.3 h | ~23 h | **~$0.60** |
| 10x cohort: 40,000 items, 200 wakes | 93 h | ~127 h | **~$7.50** ($4.35 memory, $2.91 CPU, $0.21 disk) |

This is the same order as the epic's "$2-3/mo on top of Workers Paid at cohort
usage". The multilingual instance adds nothing unless it is used.

## Recommendation

1. **Async jobs only for FounderOS, on standard-1, scale to zero.** Pennies a
   month at cohort volume. The cold start and the 8.4 s per English item
   disappear into a job that nobody waits on.
2. **Batch per founder, not per post.** Every separate wake pays a
   10-minute tail, so 80 one-item jobs cost roughly 80 times the tail of one
   80-item job.
3. **If a synchronous path is ever required, use standard-2, not an always-on
   standard-1.** It halves the latency (4.2 s English) for $41.36/mo always-on,
   or $0.054 an awake hour scaled to zero. Scaled to zero, the per-item cost
   is about the same as standard-1 ($0.00015 vs $0.00017), because the item
   finishes in half the time. Change `instance_type` to `"standard-2"` in
   `cloudflare/laya/wrangler.jsonc`; the image needs no change.
4. **Revisit after deploy** with the measured x86 cold start and per-call
   latency (README.md, "After deploy").
