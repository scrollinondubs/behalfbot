# Briefing Pipeline

Daily-briefing infrastructure for chassis instances. A heartbeat fires Claude on a schedule, Claude writes a markdown briefing to `briefings/<date>-morning-briefing.md`, the chassis renders it to a self-contained HTML, and the briefing-server makes it accessible at a stable URL on the installer's tailnet / LAN.

The point: **the installer reads one well-formatted page each morning**, not a Discord text dump. HTML rendering happens once on write; the server is just static-file serving.

---

## Components

### `chassis/scripts/md-to-briefing-html.py`

Wraps a briefing markdown file into a self-contained newspaper-style HTML with sticky left nav + serif typography. Single-file output (no external CSS, no network fonts, no JS deps). Mobile-collapsing sidebar. Dark-mode aware via `prefers-color-scheme`.

Section detection: any callout-shaped section (e.g. "Asks / Waiting on installer") gets attention styling. The detection rule lives in the script and matches against words like `ask`, `waiting on`, `action needed`, etc.

```bash
CHASSIS_HOME=/path/to/chassis python3 chassis/scripts/md-to-briefing-html.py \
    briefings/2026-05-05-morning-briefing.md \
    --output briefings/2026-05-05-morning-briefing.html
```

### `chassis/scripts/briefing-server.py`

Read-only static-file server bound to `127.0.0.1:8765` by default. Sources the directory from `${CHASSIS_HOME}/briefings`. Tailscale Funnel, nginx, or Caddy can expose it externally — the server itself stays local.

```bash
CHASSIS_HOME=/path/to/chassis python3 chassis/scripts/briefing-server.py
```

### Heartbeat orchestration

There is no separate `run-morning-briefing.sh` script. The chassis heartbeat dispatcher invokes Claude directly per the heartbeat's YAML block in `${CHASSIS_HOME}/HEARTBEATS.md` (rendered from `chassis/HEARTBEATS.md.template` at install-time):

```yaml
## morning-briefing

```yaml
schedule: daily 08:00
gather: ${CHASSIS_HOME}/chassis/scripts/gather-briefing-readiness.sh
condition: threshold count > 0
prompt: ${CHASSIS_HOME}/chassis/scheduled-tasks/morning-briefing-prompt.md
model: opus
budget: 5
criticality: normal
output_validator: true
```
```

Claude's output goes to `briefings/<date>-morning-briefing.md`. A post-write hook (typically a wrapper around `md-to-briefing-html.py`) renders it to HTML. The briefing-server picks up the new file automatically (it's just static serving).

---

## Optional but recommended

### Tailscale Funnel for external access

The briefing-server binds to localhost. To make the URL clickable from anywhere (phone, laptop on cellular), enable Tailscale Funnel on the briefing path:

```bash
sudo tailscale funnel --bg --https 443 --set-path /briefings http://localhost:8765
```

Stable URL: `https://<machine>.<tailnet>.ts.net/briefings/<date>-morning-briefing.html`. No auth (Tailscale Funnel exposes it to the public internet — fine for non-sensitive briefings; if your briefings contain sensitive data, use Tailscale Serve instead which gates on the tailnet).

### Discord notification on briefing ready

The chassis dispatcher's notification hook (`check_and_notify`) reads the leading 20 lines of the heartbeat's output for `notify: true` + `summary: ...` keys. Make the briefing prompt instruct Claude to write those at the top:

```markdown
notify: true
summary: Briefing ready. 3 asks for installer; 2 events worth flagging.

# Daily briefing — 2026-05-05
...
```

Discord webhook fires on `notify: true`, the message includes the summary + the briefing's filename. Installer clicks → opens in browser.

---

## Lessons baked in

- **#7 + #20:** the briefing fires once a day on a deterministic schedule; the gather script (e.g. `gather-briefing-readiness.sh`) checks if a precondition is met (Sean's ref install gates on Oura readiness sync) before invoking Claude. Cheap no-op gate.
- **#11:** the briefing heartbeat is registered in `HEARTBEATS.md`; without that, the dispatcher would never fire it.
- **#14:** the briefing's markdown is committed to `briefings/` for grep + audit; HTML is regenerated on demand. Source-of-truth lives in the markdown.
- **#26:** the briefing-server runs under launchd / systemd, NOT a user-session GUI agent — it survives reboots.

---

## What this pipeline is NOT

- Not a CMS. The markdown is opaque to the renderer; what Claude writes is what gets shown.
- Not multi-user. One briefing per day per installer. The server has no auth model.
- Not a content management workflow. Editorial review happens before Claude writes; nothing post-renders.

---

## Cross-references

- `chassis/scripts/md-to-briefing-html.py` — markdown → HTML renderer
- `chassis/scripts/briefing-server.py` — static-file server
- `chassis/HEARTBEATS.md.template` — chassis-default morning-briefing heartbeat shipped here; propagates to `${CHASSIS_HOME}/HEARTBEATS.md` at install
- `docs/heartbeat-dispatcher.md` — how the dispatcher fires the briefing
- `docs/LESSONS_FROM_V1.md` — full lesson list, especially #7, #11, #20, #26
