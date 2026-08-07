#!/usr/bin/env python3
"""Headless Gmail MCP server for the gmail-monitor heartbeat.

Why this exists: a hosted / interactive Gmail connector needs a browser and a
human, so it is unreachable from a headless `claude -p` (the dispatcher's
execution mode) - the triage prompt would have no Gmail tools. This is a small
stdio MCP server (stdlib only, no pip deps) that wraps the Gmail REST API with a
GMAIL_MONITOR_* OAuth refresh token, so `claude -p --mcp-config .mcp.json` gets
real read + modify + draft tools.

Draft-only by construction: there is deliberately NO send tool. The heartbeat
proposes and drafts; the principal sends. Archive + label go through modify.

Attachments and HTML: `get_thread` lists every attachment inline and reports a
`body_mime`, and `get_attachment` writes the bytes to disk and returns the path
rather than inlining base64. Both exist because the read surface was incomplete
in a way that was invisible from the outside: an HTML-only message - which is
most machine-sent mail - came back with `body: ""` and no attachment list, so
the caller could not tell an empty message from an unreadable one. The HTML
fallback keeps link targets, since a sign-in link or a document URL living only
in an href is exactly the content worth having.

Credentials: read from the environment first (GMAIL_MONITOR_CLIENT_ID /
_CLIENT_SECRET / _REFRESH_TOKEN). If absent - the running chassis container's
process env can predate the bake - fall back to parsing the install's
.env.baked (and .env) off disk, the same self-sourcing the gather script uses.
This keeps the server working without a container recreate. The install root is
resolved from CHASSIS_HOME / CUSTOMER_HOME (compose sets these), falling back to
the launch cwd.

Sender identity: the draft From header is taken from GMAIL_MONITOR_SENDER when
set (env or the .env files). When unset, the From header is omitted and Gmail
uses the address the refresh token consented as - so a single-account install
needs no extra config.

Protocol: MCP over stdio, JSON-RPC 2.0, newline-delimited messages. stdlib only
(json, urllib, base64, email) so it runs anywhere python3 does.
"""

import base64
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SERVER_NAME = "gmail-monitor"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL = "2025-06-18"


def log(msg):
    """Diagnostics go to stderr - stdout is the JSON-RPC channel."""
    print(f"[gmail-monitor-mcp] {msg}", file=sys.stderr, flush=True)


# --- Credential loading -----------------------------------------------------

def _install_root():
    return (os.environ.get("CHASSIS_HOME")
            or os.environ.get("CUSTOMER_HOME")
            or os.getcwd())


def _parse_env_file(path, keys, out):
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, _, v = line.partition("=")
                k = k.strip()
                if k in keys and k not in out:
                    v = v.strip().strip("'").strip('"')
                    if v:
                        out[k] = v
    except OSError:
        pass


def _read_env_keys(keys):
    """Return the requested keys from process env, then .env.baked, then .env."""
    out = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if len(out) < len(keys):
        root = _install_root()
        for fname in (".env.baked", ".env"):
            if len(out) >= len(keys):
                break
            _parse_env_file(os.path.join(root, fname), keys, out)
    return out


def load_creds():
    keys = ("GMAIL_MONITOR_CLIENT_ID", "GMAIL_MONITOR_CLIENT_SECRET",
            "GMAIL_MONITOR_REFRESH_TOKEN")
    out = _read_env_keys(keys)
    missing = [k for k in keys if k not in out]
    if missing:
        raise RuntimeError("missing Gmail creds: " + ", ".join(missing))
    return out


def get_sender():
    """Optional From address. None means 'let Gmail use the consented account'."""
    return _read_env_keys(("GMAIL_MONITOR_SENDER",)).get("GMAIL_MONITOR_SENDER")


_TOKEN_CACHE = {"access_token": None, "expires_at": 0}


def get_access_token():
    now = time.time()
    if _TOKEN_CACHE["access_token"] and now < _TOKEN_CACHE["expires_at"] - 60:
        return _TOKEN_CACHE["access_token"]
    creds = load_creds()
    data = urllib.parse.urlencode({
        "client_id": creds["GMAIL_MONITOR_CLIENT_ID"],
        "client_secret": creds["GMAIL_MONITOR_CLIENT_SECRET"],
        "refresh_token": creds["GMAIL_MONITOR_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.load(resp)
    tok = body.get("access_token")
    if not tok:
        raise RuntimeError("token refresh returned no access_token")
    _TOKEN_CACHE["access_token"] = tok
    _TOKEN_CACHE["expires_at"] = now + int(body.get("expires_in", 3600))
    return tok


# --- Gmail REST helpers -----------------------------------------------------

def api(method, path, query=None, body=None):
    url = GMAIL_API + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    headers = {"Authorization": "Bearer " + get_access_token()}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _headers_map(payload):
    return {h["name"].lower(): h["value"]
            for h in (payload or {}).get("headers", [])}


def _decode_part(body):
    data = (body or {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")


def _extract_plaintext(payload):
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        txt = _decode_part(payload.get("body"))
        if txt:
            return txt
    best = ""
    for part in payload.get("parts", []) or []:
        txt = _extract_plaintext(part)
        if txt and (part.get("mimeType") == "text/plain" or not best):
            best = txt or best
            if part.get("mimeType") == "text/plain":
                return txt
    return best


_TAG_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG_BREAK = re.compile(r"<\s*(br|/p|/div|/tr|/h[1-6])\b[^>]*>", re.I)
_TAG_LINK = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                       re.I | re.S)
_TAG_ANY = re.compile(r"<[^>]+>")
_BLANK_RUN = re.compile(r"\n{3,}")


def _html_to_text(raw):
    """Good enough to read, and it keeps the URLs.

    Not a general HTML renderer and not trying to be. The one thing it must not
    do is drop link targets: a sign-in link or a document URL living only in an
    href is exactly the content that matters in a machine-sent email, and a
    naive tag strip throws it away.
    """
    if not raw:
        return ""
    txt = _TAG_DROP.sub(" ", raw)
    # Parentheses, not angle brackets: the generic tag strip below would eat
    # "<https://...>" as if it were markup and silently take the URL with it.
    txt = _TAG_LINK.sub(
        lambda m: f"{_TAG_ANY.sub('', m.group(2)).strip()} ({m.group(1)})", txt)
    txt = _TAG_BREAK.sub("\n", txt)
    txt = _TAG_ANY.sub("", txt)
    txt = html_mod.unescape(txt)
    txt = "\n".join(line.strip() for line in txt.splitlines())
    return _BLANK_RUN.sub("\n\n", txt).strip()


def _extract_html(payload):
    if not payload:
        return ""
    if payload.get("mimeType") == "text/html":
        raw = _decode_part(payload.get("body"))
        if raw:
            return raw
    for part in payload.get("parts", []) or []:
        raw = _extract_html(part)
        if raw:
            return raw
    return ""


def _extract_body(payload):
    """Plain text if the sender provided any, otherwise the HTML rendered down.

    Returns (text, mime). Before this fell back, an HTML-only message - which
    is most machine-sent mail, including every transactional notification -
    came back with `body: ""` and no indication that anything had been missed.
    """
    txt = _extract_plaintext(payload)
    if txt:
        return txt, "text/plain"
    raw = _extract_html(payload)
    if raw:
        return _html_to_text(raw), "text/html"
    return "", ""


def _walk_attachments(payload, out):
    """Collect every part that carries an attachment id."""
    if not payload:
        return out
    body = payload.get("body") or {}
    filename = payload.get("filename") or ""
    if body.get("attachmentId") and filename:
        out.append({
            "attachment_id": body["attachmentId"],
            "filename": filename,
            "mime_type": payload.get("mimeType", ""),
            "size_bytes": body.get("size", 0),
        })
    for part in payload.get("parts", []) or []:
        _walk_attachments(part, out)
    return out


def _attachment_dir():
    root = (os.environ.get("GMAIL_MONITOR_ATTACHMENT_DIR")
            or os.path.join(_install_root(), "data", "gmail-attachments"))
    os.makedirs(root, exist_ok=True)
    return root


# Blocklist rather than allowlist, so "Apólice.pdf" survives as itself instead
# of arriving as "Apo_lice.pdf". What has to go is anything that can escape the
# directory or confuse a shell: separators, NUL and control bytes.
_UNSAFE_NAME = re.compile(r"[/\\\x00-\x1f\x7f]+")


def _safe_filename(name, fallback):
    """A filename from an email is attacker-controlled. Treat it that way."""
    name = os.path.basename(name or "").strip()
    name = _UNSAFE_NAME.sub("_", name).lstrip(". ")
    return name[:120] or fallback


# --- Tool implementations ---------------------------------------------------

def tool_search_threads(args):
    q = args.get("query", "in:inbox -label:Processed")
    maxr = int(args.get("max_results", 20))
    resp = api("GET", "/threads", query={"q": q, "maxResults": maxr})
    out = []
    for t in resp.get("threads", []) or []:
        det = api("GET", f"/threads/{t['id']}", query={
            "format": "metadata",
            "metadataHeaders": ["Subject", "From", "Date"]})
        msgs = det.get("messages", [])
        hm = _headers_map(msgs[-1]["payload"]) if msgs else {}
        out.append({
            "thread_id": t["id"],
            "snippet": (msgs[-1].get("snippet") if msgs else t.get("snippet", "")),
            "subject": hm.get("subject", ""),
            "from": hm.get("from", ""),
            "date": hm.get("date", ""),
        })
    return {"count": len(out), "threads": out}


def tool_get_thread(args):
    tid = args["thread_id"]
    det = api("GET", f"/threads/{tid}", query={"format": "full"})
    msgs = []
    for m in det.get("messages", []) or []:
        hm = _headers_map(m.get("payload"))
        text, mime = _extract_body(m.get("payload"))
        atts = _walk_attachments(m.get("payload"), [])
        msgs.append({
            "message_id": m.get("id"),
            "from": hm.get("from", ""),
            "to": hm.get("to", ""),
            "subject": hm.get("subject", ""),
            "date": hm.get("date", ""),
            "rfc822_message_id": hm.get("message-id", ""),
            "label_ids": m.get("labelIds", []),
            "body": text[:20000],
            "body_mime": mime,
            # Listed inline so a caller never has to guess whether a message
            # has attachments before deciding to fetch one.
            "attachments": [
                {k: a[k] for k in ("filename", "mime_type", "size_bytes",
                                   "attachment_id")}
                for a in atts],
        })
    return {"thread_id": tid, "messages": msgs}


def tool_get_attachment(args):
    """Download one attachment to disk and hand back the path.

    The bytes go to a file rather than into the JSON-RPC result on purpose:
    a PDF or an image is not useful as base64 in a transcript, and the caller
    has a Read tool that handles real files.
    """
    mid = args["message_id"]
    aid = args.get("attachment_id")
    want_name = args.get("filename")

    listed = _walk_attachments(
        api("GET", f"/messages/{mid}", query={"format": "full"}).get("payload"), [])
    if not listed:
        return {"error": "message has no attachments", "message_id": mid}

    chosen = None
    if aid:
        chosen = next((a for a in listed if a["attachment_id"] == aid), None)
    elif want_name:
        chosen = next((a for a in listed if a["filename"] == want_name), None)
        if chosen is None:
            low = want_name.lower()
            chosen = next((a for a in listed if low in a["filename"].lower()), None)
    elif len(listed) == 1:
        chosen = listed[0]
    if chosen is None:
        return {"error": "no matching attachment; pass attachment_id or filename",
                "available": [{k: a[k] for k in ("filename", "mime_type",
                                                 "size_bytes", "attachment_id")}
                              for a in listed]}

    limit = int(os.environ.get("GMAIL_MONITOR_MAX_ATTACHMENT_BYTES",
                               str(25 * 1024 * 1024)))
    if chosen.get("size_bytes", 0) > limit:
        return {"error": "attachment exceeds size limit; raise "
                         "GMAIL_MONITOR_MAX_ATTACHMENT_BYTES to override",
                "filename": chosen["filename"],
                "size_bytes": chosen["size_bytes"], "limit_bytes": limit}

    blob = api("GET", f"/messages/{mid}/attachments/{chosen['attachment_id']}")
    raw = base64.urlsafe_b64decode((blob.get("data") or "") + "===")

    name = _safe_filename(chosen["filename"], f"attachment-{chosen['attachment_id'][:12]}")
    path = os.path.join(_attachment_dir(), f"{mid}-{name}")
    with open(path, "wb") as fh:
        fh.write(raw)
    return {"path": path, "filename": chosen["filename"],
            "mime_type": chosen["mime_type"], "size_bytes": len(raw),
            "message_id": mid, "attachment_id": chosen["attachment_id"]}


def tool_list_attachments(args):
    mid = args["message_id"]
    payload = api("GET", f"/messages/{mid}", query={"format": "full"}).get("payload")
    return {"message_id": mid, "attachments": _walk_attachments(payload, [])}


def tool_list_labels(args):
    resp = api("GET", "/labels")
    return {"labels": [{"id": l["id"], "name": l["name"]}
                       for l in resp.get("labels", []) or []]}


_SYSTEM_LABELS = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH",
                  "SENT", "DRAFT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
                  "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}


def _resolve_label(value, name_to_id):
    if value in _SYSTEM_LABELS or value.startswith("Label_"):
        return value
    return name_to_id.get(value, value)


def tool_modify_thread(args):
    tid = args["thread_id"]
    add = args.get("add_labels", []) or []
    remove = args.get("remove_labels", []) or []
    if any(v not in _SYSTEM_LABELS and not v.startswith("Label_")
           for v in add + remove):
        labels = api("GET", "/labels").get("labels", []) or []
        n2i = {l["name"]: l["id"] for l in labels}
        add = [_resolve_label(v, n2i) for v in add]
        remove = [_resolve_label(v, n2i) for v in remove]
    resp = api("POST", f"/threads/{tid}/modify",
               body={"addLabelIds": add, "removeLabelIds": remove})
    return {"thread_id": tid, "label_ids": resp.get("messages", [{}])[-1].get(
        "labelIds", []) if resp.get("messages") else [], "applied": {
        "add": add, "remove": remove}}


def tool_create_draft(args):
    msg = EmailMessage()
    sender = get_sender()
    if sender:
        msg["From"] = sender
    msg["To"] = args["to"]
    msg["Subject"] = args.get("subject", "")
    if args.get("in_reply_to"):
        msg["In-Reply-To"] = args["in_reply_to"]
        msg["References"] = args.get("references", args["in_reply_to"])
    msg.set_content(args.get("body", ""))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message = {"raw": raw}
    if args.get("thread_id"):
        message["threadId"] = args["thread_id"]
    resp = api("POST", "/drafts", body={"message": message})
    return {"draft_id": resp.get("id"),
            "message_id": resp.get("message", {}).get("id"),
            "note": "DRAFT created - not sent. The principal sends after ratify."}


TOOLS = {
    "search_threads": {
        "fn": tool_search_threads,
        "description": ("Search Gmail threads. Default query "
                        "'in:inbox -label:Processed' (new unhandled mail). "
                        "Returns thread_id, subject, from, date, snippet."),
        "schema": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "Gmail search query. Default 'in:inbox -label:Processed'."},
            "max_results": {"type": "integer", "description": "Default 20."}},
            "required": []},
    },
    "get_thread": {
        "fn": tool_get_thread,
        "description": ("Read a full thread by thread_id: every message with "
                        "from/to/subject/date, rfc822_message_id (use as "
                        "in_reply_to when drafting), label_ids, body, "
                        "body_mime, and an attachments list. HTML-only mail is "
                        "rendered to text with link targets preserved, so "
                        "body_mime tells you which form you got. Fetch an "
                        "attachment's bytes with get_attachment."),
        "schema": {"type": "object", "properties": {
            "thread_id": {"type": "string"}}, "required": ["thread_id"]},
    },
    "list_attachments": {
        "fn": tool_list_attachments,
        "description": ("List the attachments on one message: filename, "
                        "mime_type, size_bytes, attachment_id. get_thread "
                        "already returns this per message, so reach for it only "
                        "when you have a message_id and nothing else."),
        "schema": {"type": "object", "properties": {
            "message_id": {"type": "string"}}, "required": ["message_id"]},
    },
    "get_attachment": {
        "fn": tool_get_attachment,
        "description": ("Download an attachment to disk and return its path, "
                        "ready to Read. Identify it by attachment_id, or by "
                        "filename (exact, then substring); with a single "
                        "attachment on the message both may be omitted. Bytes "
                        "go to a file rather than into the result, because a "
                        "PDF is not useful as base64. Saves under "
                        "GMAIL_MONITOR_ATTACHMENT_DIR, default "
                        "<install>/data/gmail-attachments."),
        "schema": {"type": "object", "properties": {
            "message_id": {"type": "string"},
            "attachment_id": {"type": "string",
                              "description": "From get_thread or list_attachments."},
            "filename": {"type": "string",
                         "description": "Alternative to attachment_id."}},
            "required": ["message_id"]},
    },
    "list_labels": {
        "fn": tool_list_labels,
        "description": "List all Gmail labels with their ids (resolve names like 'Processed').",
        "schema": {"type": "object", "properties": {}, "required": []},
    },
    "modify_thread": {
        "fn": tool_modify_thread,
        "description": ("Apply label changes to a thread. To archive + mark "
                        "handled: remove_labels=['INBOX'], "
                        "add_labels=['Processed']. Accepts label names or ids. "
                        "ONLY call after the principal ratifies (or for confident noise)."),
        "schema": {"type": "object", "properties": {
            "thread_id": {"type": "string"},
            "add_labels": {"type": "array", "items": {"type": "string"}},
            "remove_labels": {"type": "array", "items": {"type": "string"}}},
            "required": ["thread_id"]},
    },
    "create_draft": {
        "fn": tool_create_draft,
        "description": ("Create a DRAFT reply (never sends - there is no send "
                        "tool). Threads correctly when thread_id + in_reply_to "
                        "(the target message's rfc822_message_id) are passed. "
                        "From is the configured GMAIL_MONITOR_SENDER (or the "
                        "token's own account). Sign as the install's assistant "
                        "persona; never impersonate the principal."),
        "schema": {"type": "object", "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "thread_id": {"type": "string", "description": "Keep the draft in-thread."},
            "in_reply_to": {"type": "string",
                            "description": "The target message's rfc822_message_id."},
            "references": {"type": "string"}},
            "required": ["to", "body"]},
    },
}


# --- JSON-RPC / MCP plumbing ------------------------------------------------

def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        _result(rid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _result(rid, {})
        return
    if method == "tools/list":
        _result(rid, {"tools": [
            {"name": name, "description": t["description"],
             "inputSchema": t["schema"]}
            for name, t in TOOLS.items()]})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool:
            _error(rid, -32601, f"unknown tool: {name}")
            return
        try:
            out = tool["fn"](args)
            _result(rid, {"content": [
                {"type": "text", "text": json.dumps(out, ensure_ascii=False)}]})
        except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
            log(f"tool {name} failed: {exc}")
            _result(rid, {"content": [
                {"type": "text", "text": f"ERROR calling {name}: {exc}"}],
                "isError": True})
        return
    if not is_notification:
        _error(rid, -32601, f"method not found: {method}")


def main():
    log("gmail-monitor MCP server up (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad JSON: {exc}")
            continue
        try:
            handle(msg)
        except Exception as exc:  # noqa: BLE001
            log(f"handler error: {exc}")
            if "id" in msg:
                _error(msg.get("id"), -32603, f"internal error: {exc}")


if __name__ == "__main__":
    main()
