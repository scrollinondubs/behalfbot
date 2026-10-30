# Spend Snapshots

Placeholder. The spend-snapshots heartbeat does all its work in
`chassis/scripts/gather-spend-snapshots.py`: it pulls billing data into
`state/projections/spend.db`, posts any alert itself through
`discord-post.sh`, and always emits `count: 0`, so no model is invoked.

If you are reading this, something unexpected happened. Check the gather
output in the dispatcher log and confirm the HEARTBEATS.md condition is
`threshold count > 0`. Reply with one line saying the prompt fired by
mistake, and do nothing else.
