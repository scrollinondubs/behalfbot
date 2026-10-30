# Dashboard

Every install gets a web dashboard with a **Briefings** tab and a **DevOps**
tab. **Projections** shows up once you list a paid service. Installed plugins
add their own tabs, and you can add a tab of your own by dropping one Python
file in a folder.

The code lives in `chassis/dashboard/` (FastAPI + Jinja). It runs on the host,
not in the container, because DevOps probes call `launchctl`, `docker`,
`tmux` and `ssh` there.

## Running it

It replaces the old static briefing server on the same port (8765) and keeps
its URLs working: `/<file>.html` and `/briefings/<file>.html` still serve the
rendered briefing. `chassis/scripts/briefing-server.py` now starts the
dashboard, so a launchd or systemd unit that already runs that script needs
no change beyond installing the packages.

```bash
python3 -m pip install -r chassis/dashboard/requirements.txt   # fastapi, uvicorn, jinja2, pyyaml
python3 chassis/dashboard/app.py                               # 127.0.0.1:8765
```

If the python running `briefing-server.py` is missing those packages, it logs
one line with the install command and falls back to serving the briefings
folder as static files, the way it did before. Briefing links keep working
either way.

| Env | Default | |
|---|---|---|
| `CUSTOMER_HOME` | `CHASSIS_HOME`, then `~/.behalfbot`, then `/app/customer` | Same order as `_env.sh` |
| `DASHBOARD_PORT` | `BRIEFING_PORT`, then `8765` | |
| `DASHBOARD_BIND` | `BRIEFING_BIND`, then `127.0.0.1` | Keep it on localhost; expose with `tailscale serve` |
| `INSTANCE_NAME` | `Behalf.bot` | Name shown at the top of the sidebar |
| `CHASSIS_PLUGINS_ROOT` | see below | Where plugin tabs are read from |
| `BRIEFINGS_ROOT` | `$CUSTOMER_HOME/briefings` | |
| `DISK_MAP_ROOTS` | `$HOME` plus OS system dirs | Colon-separated |

New services: render `chassis/launchd/com.behalfbot.dashboard.plist.template`
(macOS, a LaunchAgent) or `chassis/systemd/dashboard.service.template` (Linux
user unit). Run either that or `briefing-server.py`, not both: they bind the
same port.

There is no login. Keep it on `127.0.0.1` and reach it over your tailnet:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8765
```

## Briefings

Lists `briefings/YYYY-MM-DD-<slug>.html` newest first and opens today's
`morning-briefing` (or the newest file) in a frame. Nothing to configure. The
Markdown and `md-to-briefing-html.py` are unchanged.

## DevOps

Without any config the page shows heartbeat state
(`scheduled-tasks/heartbeat-state.json`), the backups folder if one exists,
and the disk panel. To probe services, add a `dashboard.devops` block to
`$CUSTOMER_HOME/chassis.config.yaml`:

```yaml
dashboard:
  devops:
    services:
      - {label: Heartbeat dispatcher, container: behalfbot}       # docker inspect, healthcheck aware
      - {label: Ollama, http: "http://127.0.0.1:11434/api/tags"}   # any 2xx is up
      - {label: Discord bot, tmux: assistant-discord}             # tmux has-session
      - {label: Dashboard, launchd: com.behalfbot.dashboard}      # launchctl list (gui domain)
    remote_hosts:
      - {ssh: bigpoppa, containers: [n8n, nginx-proxy], sudo: true}   # docker ps over ssh
    backups_dir: backups                 # relative to CUSTOMER_HOME, or absolute
    archived_backup_targets: [old-db]    # subdirs not listed at all
    static_backup_targets: [configs]     # listed, but never go stale
    backup_stale_hours: 48
    disk_map: true                       # false hides the disk panel
```

Each service names exactly one probe: `container`, `http`, `tmux` or
`launchd`. An entry that names none, or two, shows as a note at the top of
the page instead of breaking it. The same goes for a config file that does
not parse, or a python without `pyyaml`. `remote_hosts` uses ssh in batch
mode, so the host needs key auth set up for the user the dashboard runs as.

Every subdir of the backups folder is a target. Its newest file decides its
age, and it goes red after `backup_stale_hours`.

### Disk map

`/devops/disk` is a treemap of what is using the disk. **Scan now** runs
`chassis/scripts/disk-map-scan.py` in the background (a minute or two of `du`)
and writes `state/disk-map.json`; a pidfile stops two scans running at once.
It scans `$HOME` plus `/opt /Applications /Library /private/var` on macOS or
`/opt /srv /usr /var` on Linux, and reads capacity from `df -k $HOME`. Set
`DISK_MAP_ROOTS=/path/a:/path/b` to scan something else. The script can also
run from a heartbeat on its own.

## Projections

Spend to date, projected and baseline for each paid service, with a daily
chart. The tab appears once `$CUSTOMER_HOME/config/spend-services.json` exists
and lists at least one service (restart the dashboard after creating it).

1. Copy `chassis/dashboard/spend-services.json.example` to
   `$CUSTOMER_HOME/config/spend-services.json` and keep only what you pay for.
2. Put each key in `$CUSTOMER_HOME/.env` under the name its `env` field gives.
   Keys never go in the JSON.
3. Uncomment the `spend-snapshots` block in `HEARTBEATS.md`. The first run
   backfills three billing periods.

Each top-level key is a service name you choose. `provider` says which billing
API to read and defaults to the key itself, so two Vercel teams can be
`vercel` and `vercel_client` with `"provider": "vercel"`.

| provider | Needs | Notes |
|---|---|---|
| `vercel` | `env` (token), optional `team_slug` | Billing charges export |
| `openai` | `env` (admin key) | Organization costs |
| `anthropic` | `env` (admin key) | Cost report; a workspace key shows as "needs key" |
| `github` | `env` (PAT with billing read), `user` | Per-user billing usage |
| `cloudflare` | `env` (token with billing read), `anchor_day` | Usage plus monthly plan fees |
| `turso` | `env`, `org`, optional `fixed` | Invoices; `fixed` is the plan price until there are any |
| `resend` | `env`, `fixed`, `quota`, `over_per_1000` | Counts emails sent this period |
| `fixed` | `fixed` | A flat monthly amount you enter |

Common fields: `label`, `tz` (the timezone the vendor bills in), `anchor_day`
(the day the billing period renews), `cap` (alert when the projection passes
it), `note`.

Alerts post through `chassis/scripts/discord-post.sh` (needs
`DISCORD_BOT_TOKEN`) to `_settings.alert_channel_id`, falling back to
`SPEND_ALERT_CHANNEL_ID`. `_settings.dashboard_url` (or `DASHBOARD_URL`) adds a
link to the service's page. With no channel the alert is logged and retried
next run. `DRY_RUN=1` prints alerts instead of posting them.

## Adding a custom tab

Drop a file in `$CUSTOMER_HOME/dashboard/tabs/` and restart the dashboard:

```python
# $CUSTOMER_HOME/dashboard/tabs/leads.py
from fastapi import APIRouter, Request
from tabs import page

TAB = {"label": "Leads", "prefix": "/leads", "order": 10}
router = APIRouter()

@router.get("")
def index(request: Request):
    return page(request, "Leads", "<p>Hello from a custom tab.</p>")
```

- Every `*.py` that defines both `TAB` and `router` becomes a tab under
  "Custom" in the sidebar. Files starting with `_` and helper modules that
  define neither are skipped, and the tab's own folder is on `sys.path` while
  it loads, so it can import its helpers.
- Routes are relative to `prefix`: `@router.get("")` is `/leads`,
  `@router.get("/{id}")` is `/leads/42`.
- `page(request, "Title", "<html>")` wraps raw HTML in the dashboard shell.
  `page(request, "leads.html", {...})` renders a template; set
  `TAB["templates"] = "templates"` (relative to the tabs folder) and write
  templates that `{% extends "base.html" %}` and fill `{% block topbar %}`,
  `{% block content %}` and, for extra CSS, `{% block head %}`. Set
  `{% set active_section = "/leads" %}` to highlight the tab, or leave it out
  and the URL decides.
- Shared CSS classes: `panel`, `status-table`, `stat-row` / `stat`, `pill`,
  `btn`, `muted`, `small`, `ok` / `warn` / `fail`, `table-scroll`, `subnav`.

A tab that raises while loading (including `sys.exit`) is skipped. The app
logs one line and shows the error at the top of DevOps; the rest of the
dashboard keeps working.

## How plugins add tabs

A plugin declares its tab in `openclaw.plugin.json`, paths relative to the
plugin dir:

```json
"contracts": {
  "dashboard": {
    "tab": "Dating",
    "prefix": "/dating",
    "router": "dashboard/tab.py",
    "templates": "dashboard/templates",
    "devops_panels": ["dashboard/emulator_panel.py"]
  }
}
```

- `router` is a module that defines `router = APIRouter()`, mounted at
  `prefix` under "Plugins" in the sidebar. It is imported by file path, with
  its own folder on `sys.path`, so `from helpers import x` works for sibling
  files. Render with `request.app.state.templates.TemplateResponse(request,
  "dating_review.html", {...})` (the shared Jinja environment: chassis
  templates plus every loaded tab's `templates`), or `from tabs import page`,
  the same renderer custom tabs use. Set `{% set active_section = "/dating" %}`
  (the tab prefix) in templates to highlight the tab.
- `templates` is added to the template search path. Name templates so they do
  not collide with other plugins (`dating_review.html`, not `review.html`).
- Each `devops_panels` module defines `panel()` returning
  `{"title": str, "rows": [{"label", "value", "status": "ok"|"warn"|"fail", "detail"}]}`.
  It runs on every `/devops` load, so keep it quick. A panel that raises
  shows as a fail row.
- Tabs come from the plugin root the rest of the chassis uses:
  `CHASSIS_PLUGINS_ROOT` if set, else the `resolved_root` that
  `resolve-plugin-root.sh` recorded in `$CUSTOMER_HOME/plugins-root.state.json`,
  else `$CHASSIS_HOME/plugins`, else `/app/plugins`. A plugin that is not
  installed adds nothing.
- A plugin whose module fails to import is skipped with one logged line and a
  row on DevOps: `plugin tab failed to load: <id>: <error>`.

Plugin tabs run inside the dashboard process with full access, same as core
tabs. Install plugins you trust.
