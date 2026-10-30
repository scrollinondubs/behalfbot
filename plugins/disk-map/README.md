# disk-map

On-demand treemap of the host's disk usage.

`scripts/disk-map.py` runs `du -k -d 6` over a set of roots, folds anything under 150 MB into its parent, and writes:

| File | What |
|---|---|
| `$CUSTOMER_HOME/state/disk-map.json` | Tree plus used / total / measured KB. Machine-readable, for dashboards. |
| `$CUSTOMER_HOME/briefings/disk-map.html` | Self-contained zoomable treemap (d3 from cdnjs). |

Because the page lands in `briefings/`, the chassis briefing-server already serves it at `/briefings/disk-map.html`. No new port, no new service.

The page also reports how much used space sits **outside** the scanned roots. On macOS that gap is usually APFS local snapshots or system data, and it is often the actual answer to "the numbers don't add up".

## Usage

```bash
python3 plugins/disk-map/scripts/disk-map.py                  # default roots
python3 plugins/disk-map/scripts/disk-map.py --roots /srv /home
DISK_MAP_ROOTS=/srv:/home python3 plugins/disk-map/scripts/disk-map.py
```

Default roots: `$HOME`, `/opt`, `/Applications`, `/Library`, `/private/var` on macOS; `$HOME`, `/opt`, `/var`, `/usr`, `/srv` on Linux.

Run it on the host. Inside the container, `du` only sees bind mounts. Stdlib only, no dependencies.

## Privacy

The page lists directory paths and sizes. If `briefings/` is exposed with Tailscale **Funnel** (public internet) rather than **Serve** (tailnet only), the disk map is public too. Pass `--html-out` somewhere unserved if that matters.

## Dashboards

If an install has its own web dashboard, read `state/disk-map.json` and run the script in the background for a "Scan now" button. `used_kb - measured_kb` is the unscanned gap.
