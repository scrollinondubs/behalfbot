---
name: disk-map
description: Scan the host disk and publish a zoomable treemap of where the space goes. Use when the installer asks what is eating disk space, asks for a disk map, or a low-disk alert needs a breakdown.
---

# Skill: Disk map

Answers "where did my disk space go?" with a picture instead of a wall of `du` output.

## Run it

On the **host** (a terminal session, not a heartbeat inside the container - `du` in the container only sees its bind mounts):

```bash
python3 "${CHASSIS_PLUGINS_ROOT:-$CHASSIS_HOME/plugins}/disk-map/scripts/disk-map.py"
```

It takes one to three minutes and prints one JSON line:

```json
{"html": ".../briefings/disk-map.html", "json": ".../state/disk-map.json",
 "used_gb": 281.1, "free_gb": 179.3, "unscanned_gb": 53.8,
 "top": [{"path": "/home/alex/.cache", "gb": 32.4}, ...]}
```

Narrower scan: `--roots /some/dir /other/dir` or `DISK_MAP_ROOTS=/a:/b`.

## Report back

One message to the installer:

- The link: `https://<machine>.<tailnet>.ts.net/briefings/disk-map.html` (whatever base URL their briefings use - see `docs/briefing-pipeline.md`).
- Used / free.
- The top five from `top`, with sizes.
- `unscanned_gb` if it is more than about 10% of used. That space sits outside the scanned folders: APFS local snapshots (`tmutil listlocalsnapshots /`), other users, or system data. Say so plainly; it is usually the answer when the numbers do not add up.

## Do not

- Delete anything the map surfaces. The map is evidence. Cleanup is the installer's call, item by item.
- Run it on a schedule without asking. A full `du` is minutes of disk I/O.
