#!/usr/bin/env python3
"""daily-log-gather.py - Multi-surface daily activity gather for the daily-log heartbeat.

Pre-fetches the day's activity across every operational surface the customer's
Jax touches, then emits a single JSON payload on stdout for the prompt to
consume. Runs on the host / dispatcher context (not inside Claude), so all
API access uses env vars + shell tools, never MCP.

Contract (per chassis gather-script template):
  - stdout: exactly one line of valid parseable JSON (see OUTPUT_SHAPE below)
  - stderr: debug logging with --verbose
  - exit 0 on success, even when everything got skipped gracefully
  - non-zero exit ONLY on invariant violations (e.g. can't emit JSON)

Design goals:
  1. Repo scope is DYNAMIC: query jacketyjax's viewer graph for every repo
     with recent activity, not scrollinondubs-only. Rationale: customer
     installs on the VCL platform will have students assigning tasks to Jax
     on their own project repos.
  2. Postmortem source is Discord #jax message mining (regex patterns
     against bot-authored messages in the last 24h).
  3. Reflection section is prompt-side; this script only supplies raw
     material.
  4. Operational surfaces scanned: GitHub, Gmail, SiYuan, Discord.

Env var contract (chassis is generic; customer install sets these):
  CHASSIS_HOME                     - customer install root (required)
  DAILY_LOG_GH_USER                - GitHub username Jax pushes as
  DAILY_LOG_GMAIL_IDENTITY         - Gmail address Jax sends from
  DAILY_LOG_DISCORD_CHANNEL_ID     - #jax channel ID for postmortem mining
  DAILY_LOG_SIYUAN_URL             - SiYuan HTTP API URL (default: $SIYUAN_URL)
  DAILY_LOG_SIYUAN_TOKEN           - SiYuan API token   (default: $SIYUAN_TOKEN)
  DAILY_LOG_EXTRA_METRICS_SCRIPT   - path to a customer-side script that emits
                                     JSON under a `custom` metrics key
  DISCORD_TOKEN / DISCORD_BOT_TOKEN - Discord bot token (probes both)

Unset env vars degrade gracefully: the corresponding surface is skipped, a
warning is emitted into the `warnings` array, and the rest of the gather
proceeds.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# --- Config ---------------------------------------------------------------


DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_REPO_ACTIVITY_DAYS = 14
DEFAULT_SIYUAN_MIN_CONTENT_LEN = 200
DEFAULT_SIYUAN_LIMIT = 50
DEFAULT_DISCORD_MSG_LIMIT = 100

POSTMORTEM_PATTERNS = [
    r"[Ss]urprises\s*:",
    r"[Ss]anity-check priorities\s*:",
    r"[Rr]eview priorities\s*:",
    r"deviat(ed|ion) from spec",
    r"didn'?t work",
    r"did not work",
    r"broke because",
    r"[Rr]oot cause\s*:",
    r"root cause was",
    r"[Gg]otcha\s*:",
    r"[Ll]earned\s*:",
]
POSTMORTEM_REGEX = re.compile("|".join(POSTMORTEM_PATTERNS))


# --- Utilities ------------------------------------------------------------


def log(msg: str, *, verbose: bool) -> None:
    """Debug log to stderr, no-op unless --verbose."""
    if verbose:
        print(f"[daily-log-gather] {msg}", file=sys.stderr)


def today_yesterday(now: datetime | None = None) -> tuple[str, str, datetime, datetime]:
    """Return (today_str, yesterday_str, since_dt, until_dt) for the 24h window
    ending at the given `now` (defaults to real now).

    Chassis convention: the daily log covers the calendar day that just
    ended. When the script runs at 02:00, "today" = the day just starting,
    "yesterday" = the day being logged.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    return (
        today.isoformat(),
        yesterday.isoformat(),
        datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc),
        datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
    )


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run_json(argv: list[str], *, verbose: bool, timeout: int = 60) -> Any:
    """Run a subprocess that emits JSON on stdout. Returns parsed JSON or None."""
    log(f"run: {' '.join(argv)}", verbose=verbose)
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        log(f"timeout: {' '.join(argv)}", verbose=verbose)
        return None
    if r.returncode != 0:
        log(f"non-zero exit {r.returncode}: {r.stderr.strip()[:400]}", verbose=verbose)
        return None
    if not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        log(f"json parse failed: {e}; stdout head: {r.stdout[:200]}", verbose=verbose)
        return None


def run_text(argv: list[str], *, verbose: bool, timeout: int = 60) -> str | None:
    log(f"run: {' '.join(argv)}", verbose=verbose)
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        log(f"timeout: {' '.join(argv)}", verbose=verbose)
        return None
    if r.returncode != 0:
        log(f"non-zero exit {r.returncode}: {r.stderr.strip()[:400]}", verbose=verbose)
        return None
    return r.stdout


# --- GitHub surface -------------------------------------------------------


def gather_github(gh_user: str, since: datetime, until: datetime, *, verbose: bool
                  ) -> tuple[dict[str, dict], list[dict], list[str]]:
    """Return (prs_by_repo, open_issues_awaiting_input, warnings)."""
    warnings: list[str] = []
    prs_by_repo: dict[str, dict] = {}
    open_issues: list[dict] = []

    if not have("gh"):
        warnings.append("gh CLI not installed - skipped GitHub scan")
        return prs_by_repo, open_issues, warnings

    # Step 1: dynamic repo discovery via viewer graphql.
    activity_cutoff = (until - timedelta(days=DEFAULT_REPO_ACTIVITY_DAYS)).isoformat()
    query = (
        "{ viewer { repositories("
        "first:100, "
        "affiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER], "
        "orderBy:{field:PUSHED_AT,direction:DESC}"
        ") { nodes { nameWithOwner pushedAt } } } }"
    )
    graph = run_json(
        ["gh", "api", "graphql", "-f", f"query={query}"], verbose=verbose, timeout=45
    )
    if graph is None:
        warnings.append("gh graphql viewer query failed - skipped GitHub scan")
        return prs_by_repo, open_issues, warnings

    try:
        nodes = graph["data"]["viewer"]["repositories"]["nodes"] or []
    except (KeyError, TypeError):
        warnings.append("gh graphql viewer returned unexpected shape - skipped GitHub scan")
        return prs_by_repo, open_issues, warnings

    active_repos: list[str] = []
    for node in nodes:
        name = node.get("nameWithOwner")
        pushed = node.get("pushedAt")
        if not name or not pushed:
            continue
        if pushed >= activity_cutoff:
            active_repos.append(name)
    log(f"active repos found: {len(active_repos)}", verbose=verbose)

    # Step 2: for each active repo, pull PRs authored by gh_user in the window.
    since_str = since.date().isoformat()
    until_str = until.date().isoformat()
    for repo in active_repos:
        pr_data = run_json(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--author", gh_user,
                "--state", "all",
                "--search", f"updated:{since_str}..{until_str}",
                "--json", "number,title,url,state,body,mergedAt,closedAt,createdAt",
            ],
            verbose=verbose, timeout=45,
        )
        if pr_data is None or not isinstance(pr_data, list) or not pr_data:
            continue
        merged, opened, closed_unmerged = [], [], []
        for pr in pr_data:
            body = pr.get("body") or ""
            item = {
                "num": pr.get("number"),
                "title": pr.get("title"),
                "url": pr.get("url"),
                "body_excerpt": body[:300].strip(),
            }
            state = (pr.get("state") or "").upper()
            merged_at = pr.get("mergedAt")
            closed_at = pr.get("closedAt")
            created_at = pr.get("createdAt")
            if merged_at and since_str <= merged_at[:10] <= until_str:
                merged.append(item)
            elif state == "OPEN" and created_at and since_str <= created_at[:10] <= until_str:
                opened.append(item)
            elif state == "CLOSED" and closed_at and not merged_at:
                if since_str <= closed_at[:10] <= until_str:
                    closed_unmerged.append(item)
        if merged or opened or closed_unmerged:
            prs_by_repo[repo] = {
                "merged": merged,
                "opened": opened,
                "closed_unmerged": closed_unmerged,
            }

        # Step 3: open issues where Jax commented but someone else spoke last,
        # OR Jax spoke last and is waiting. Best-effort search.
        issue_data = run_json(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--state", "open",
                "--search", f"commenter:{gh_user}",
                "--json", "number,title,url,labels,updatedAt,author",
                "--limit", "30",
            ],
            verbose=verbose, timeout=30,
        )
        if not issue_data or not isinstance(issue_data, list):
            continue
        for issue in issue_data:
            open_issues.append({
                "repo": repo,
                "num": issue.get("number"),
                "title": issue.get("title"),
                "url": issue.get("url"),
                "last_updated": issue.get("updatedAt"),
                "labels": [lbl.get("name") for lbl in (issue.get("labels") or [])
                           if isinstance(lbl, dict) and lbl.get("name")],
            })

    return prs_by_repo, open_issues, warnings


# --- Gmail surface --------------------------------------------------------


def gather_gmail(identity: str, since: datetime, until: datetime, *, verbose: bool
                 ) -> tuple[list[dict], bool, list[str]]:
    """Return (messages, deferred_flag, warnings).

    Gmail MCP tools aren't available in the host shell; the prompt will do
    the actual retrieval when this returns `deferred=True`. We only try a
    local `google-api-python-client` path if that lib is installed.
    """
    warnings: list[str] = []
    # Fast-path skip: not installed and no lib available -> defer.
    try:
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        return [], True, warnings

    # If we get here, googleapiclient is installed. The customer would need
    # OAuth creds wired up; that's out-of-scope for chassis. Defer to prompt.
    log("googleapiclient available but Gmail credential wiring is customer-side; deferring",
        verbose=verbose)
    return [], True, warnings


# --- SiYuan surface -------------------------------------------------------


def siyuan_post(url: str, path: str, token: str | None, payload: dict,
                *, verbose: bool, timeout: int = 30) -> Any:
    endpoint = url.rstrip("/") + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Token {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log(f"siyuan POST {path} failed: {e}", verbose=verbose)
        return None
    except json.JSONDecodeError as e:
        log(f"siyuan POST {path} bad JSON: {e}", verbose=verbose)
        return None
    return data


def gather_siyuan(url: str, token: str | None, since: datetime, until: datetime,
                  *, verbose: bool) -> tuple[list[dict], list[str]]:
    """Return (activity, warnings). Each activity item = {id, hpath, len, updated_at, excerpt}."""
    warnings: list[str] = []
    activity: list[dict] = []
    since_stamp = since.strftime("%Y%m%d%H%M%S")
    until_stamp = until.strftime("%Y%m%d%H%M%S")
    sql = (
        "SELECT id, hpath, LENGTH(content) as len, updated, created "
        "FROM blocks "
        "WHERE type='d' "
        f"AND updated >= '{since_stamp}' "
        f"AND updated < '{until_stamp}' "
        f"AND LENGTH(content) > {DEFAULT_SIYUAN_MIN_CONTENT_LEN} "
        "ORDER BY updated DESC "
        f"LIMIT {DEFAULT_SIYUAN_LIMIT}"
    )
    resp = siyuan_post(url, "/api/query/sql", token, {"stmt": sql}, verbose=verbose)
    if resp is None or not isinstance(resp, dict):
        warnings.append("siyuan SQL query failed - skipped SiYuan scan")
        return activity, warnings
    if resp.get("code") not in (0, None):
        warnings.append(f"siyuan returned code={resp.get('code')} msg={resp.get('msg')}")
        return activity, warnings
    rows = resp.get("data") or []
    for row in rows:
        block_id = row.get("id")
        if not block_id:
            continue
        item = {
            "id": block_id,
            "hpath": row.get("hpath") or "",
            "len": row.get("len") or 0,
            "updated_at": row.get("updated") or "",
            "excerpt": "",
        }
        # For docs created (not just updated) today, pull first 300 chars of
        # content as a hint. Cheap SELECT against blocks.
        created = row.get("created") or ""
        if created and created >= since_stamp:
            excerpt_sql = (
                "SELECT content FROM blocks "
                f"WHERE root_id = '{block_id}' AND type = 'p' "
                "ORDER BY created ASC LIMIT 3"
            )
            excerpt_resp = siyuan_post(
                url, "/api/query/sql", token, {"stmt": excerpt_sql}, verbose=verbose
            )
            if isinstance(excerpt_resp, dict) and excerpt_resp.get("code") in (0, None):
                paras = excerpt_resp.get("data") or []
                joined = " ".join((p.get("content") or "").strip() for p in paras)
                item["excerpt"] = joined[:300].strip()
        activity.append(item)
    return activity, warnings


# --- Discord postmortem mining --------------------------------------------


def gather_discord_postmortems(channel_id: str, token: str, since: datetime,
                               *, verbose: bool) -> tuple[list[dict], list[str]]:
    """Fetch recent messages from `#jax` and regex-extract postmortem candidates."""
    warnings: list[str] = []
    postmortems: list[dict] = []
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}/messages"
        f"?limit={DEFAULT_DISCORD_MSG_LIMIT}"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "chassis-daily-log-gather/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        warnings.append(f"discord fetch failed: {e}")
        return postmortems, warnings
    except json.JSONDecodeError as e:
        warnings.append(f"discord returned bad JSON: {e}")
        return postmortems, warnings

    if not isinstance(data, list):
        warnings.append("discord API returned non-list body - skipped postmortem mining")
        return postmortems, warnings

    since_iso = since.isoformat().replace("+00:00", "")
    for msg in data:
        if not isinstance(msg, dict):
            continue
        author = msg.get("author") or {}
        if not author.get("bot"):
            continue
        ts = msg.get("timestamp") or ""
        if ts and ts < since_iso:
            continue
        content = msg.get("content") or ""
        if not content:
            continue
        if not POSTMORTEM_REGEX.search(content):
            continue
        # Extract 1-3 sentence excerpt around the match.
        excerpt = content
        if len(excerpt) > 500:
            m = POSTMORTEM_REGEX.search(excerpt)
            if m:
                start = max(0, m.start() - 150)
                end = min(len(excerpt), m.end() + 350)
                excerpt = excerpt[start:end].strip()
                if start > 0:
                    excerpt = "..." + excerpt
                if end < len(content):
                    excerpt = excerpt + "..."
        postmortems.append({
            "source": f"discord msg {msg.get('id')}",
            "timestamp": ts,
            "excerpt": excerpt.strip(),
        })
    return postmortems, warnings


# --- Metrics --------------------------------------------------------------


def gather_metrics(chassis_home: Path, prs_by_repo: dict, open_issues: list,
                   since: datetime, until: datetime, extra_script: str | None,
                   *, verbose: bool) -> dict:
    metrics: dict[str, Any] = {}

    # PR counts (deterministic derivation from GH surface).
    prs_merged = sum(len(v.get("merged", [])) for v in prs_by_repo.values())
    prs_opened = sum(len(v.get("opened", [])) for v in prs_by_repo.values())
    issues_closed = sum(len(v.get("closed_unmerged", [])) for v in prs_by_repo.values())
    metrics["prs_merged"] = prs_merged
    metrics["prs_opened"] = prs_opened
    metrics["issues_closed"] = issues_closed
    metrics["open_issues_awaiting_input"] = len(open_issues)

    today_str = until.date().isoformat()
    yesterday_str = since.date().isoformat()

    # Commits in the customer install repo.
    commits = 0
    if (chassis_home / ".git").exists():
        out = run_text(
            [
                "git", "-C", str(chassis_home), "log", "--oneline",
                f"--since={yesterday_str} 02:00",
                f"--until={today_str} 02:00",
            ],
            verbose=verbose, timeout=15,
        )
        if out:
            commits = len([ln for ln in out.splitlines() if ln.strip()])
    metrics["commits_customer_repo"] = commits

    # Briefings generated today.
    briefings_dir = chassis_home / "briefings"
    briefings_count = 0
    if briefings_dir.exists():
        briefings_count = len(list(briefings_dir.glob(f"{today_str}-*.md")))
    metrics["briefings_generated"] = briefings_count

    # Heartbeat failures (unique heartbeats with any ERROR/FAIL today).
    logs_dir = chassis_home / "logs" / "scheduled"
    heartbeat_failures = 0
    if logs_dir.exists():
        failed_names: set[str] = set()
        pattern = re.compile(r"error|fail", re.IGNORECASE)
        for log_file in logs_dir.glob(f"{today_str}-*.log"):
            try:
                text = log_file.read_text(errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                # Filename like YYYY-MM-DD-<heartbeat>.log -> extract heartbeat name.
                stem = log_file.stem
                parts = stem.split("-", 3)
                if len(parts) >= 4:
                    failed_names.add(parts[3])
                else:
                    failed_names.add(stem)
        heartbeat_failures = len(failed_names)
    metrics["heartbeat_failures"] = heartbeat_failures

    # Dating sessions (customer-install-specific; empty when absent).
    dating_dir = chassis_home / "logs" / "dating"
    if dating_dir.exists():
        metrics["dating_sessions"] = len(list(dating_dir.glob(f"{today_str}-*")))
    else:
        metrics["dating_sessions"] = 0

    # Custom metrics from customer-side extension script.
    if extra_script and Path(extra_script).is_file() and os.access(extra_script, os.X_OK):
        custom = run_json([extra_script], verbose=verbose, timeout=30)
        if isinstance(custom, dict):
            metrics["custom"] = custom
        else:
            metrics["custom"] = {"_error": "extra metrics script produced no JSON"}
    else:
        metrics["custom"] = {}

    return metrics


# --- Main -----------------------------------------------------------------


def build_output(now: datetime | None = None, *, verbose: bool = False) -> dict:
    """Assemble the JSON payload. Never raises; all errors -> warnings."""
    today, yesterday, since, until = today_yesterday(now)
    warnings: list[str] = []

    chassis_home_raw = os.environ.get("CHASSIS_HOME", "").strip()
    if not chassis_home_raw:
        # Fall back to $HOME/.behalfbot to avoid a hard crash in dev, but
        # emit a loud warning.
        chassis_home_raw = str(Path.home() / ".behalfbot")
        warnings.append("CHASSIS_HOME unset - defaulted to ~/.behalfbot")
    chassis_home = Path(chassis_home_raw)

    prs_by_repo: dict[str, dict] = {}
    open_issues: list[dict] = []
    operational_email: list[dict] = []
    email_deferred = False
    siyuan_activity: list[dict] = []
    postmortems: list[dict] = []

    # GitHub.
    gh_user = os.environ.get("DAILY_LOG_GH_USER", "").strip()
    if gh_user:
        prs_by_repo, open_issues, gh_warn = gather_github(
            gh_user, since, until, verbose=verbose
        )
        warnings.extend(gh_warn)
    else:
        warnings.append("DAILY_LOG_GH_USER unset - skipped GitHub scan")

    # Gmail.
    gmail_identity = os.environ.get("DAILY_LOG_GMAIL_IDENTITY", "").strip()
    if gmail_identity:
        operational_email, email_deferred, gmail_warn = gather_gmail(
            gmail_identity, since, until, verbose=verbose
        )
        warnings.extend(gmail_warn)
    else:
        warnings.append("DAILY_LOG_GMAIL_IDENTITY unset - skipped Gmail scan")

    # SiYuan.
    siyuan_url = (os.environ.get("DAILY_LOG_SIYUAN_URL")
                  or os.environ.get("SIYUAN_URL") or "").strip()
    siyuan_token = (os.environ.get("DAILY_LOG_SIYUAN_TOKEN")
                    or os.environ.get("SIYUAN_TOKEN") or "").strip() or None
    if siyuan_url:
        siyuan_activity, siyuan_warn = gather_siyuan(
            siyuan_url, siyuan_token, since, until, verbose=verbose
        )
        warnings.extend(siyuan_warn)
    else:
        warnings.append("DAILY_LOG_SIYUAN_URL / SIYUAN_URL unset - skipped SiYuan scan")

    # Discord postmortems.
    channel_id = os.environ.get("DAILY_LOG_DISCORD_CHANNEL_ID", "").strip()
    discord_token = (os.environ.get("DISCORD_TOKEN")
                     or os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if channel_id and discord_token:
        postmortems, discord_warn = gather_discord_postmortems(
            channel_id, discord_token, since, verbose=verbose
        )
        warnings.extend(discord_warn)
    elif not channel_id:
        warnings.append("DAILY_LOG_DISCORD_CHANNEL_ID unset - skipped Discord scan")
    else:
        warnings.append("DISCORD_TOKEN / DISCORD_BOT_TOKEN unset - skipped Discord scan")

    # Metrics.
    extra_script = os.environ.get("DAILY_LOG_EXTRA_METRICS_SCRIPT", "").strip() or None
    metrics = gather_metrics(
        chassis_home, prs_by_repo, open_issues, since, until, extra_script,
        verbose=verbose,
    )

    return {
        "date": yesterday,
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
        },
        "prs_by_repo": prs_by_repo,
        "open_issues_awaiting_input": open_issues,
        "operational_email": operational_email,
        "gmail_scan_deferred": email_deferred,
        "siyuan_activity": siyuan_activity,
        "postmortems": postmortems,
        "metrics": metrics,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true",
                        help="Log debug info to stderr")
    parser.add_argument("--now", type=str, default=None,
                        help="Override current time (ISO 8601, for testing)")
    args = parser.parse_args()

    now: datetime | None = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError as e:
            print(f"invalid --now: {e}", file=sys.stderr)
            return 2

    try:
        payload = build_output(now=now, verbose=args.verbose)
    except Exception as e:  # noqa: BLE001 - JSON contract is invariant
        # Even in catastrophic failure emit valid JSON so the dispatcher
        # doesn't crash on parse.
        payload = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "prs_by_repo": {},
            "open_issues_awaiting_input": [],
            "operational_email": [],
            "gmail_scan_deferred": False,
            "siyuan_activity": [],
            "postmortems": [],
            "metrics": {},
            "warnings": [f"gather crashed: {type(e).__name__}: {e}"],
        }

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
