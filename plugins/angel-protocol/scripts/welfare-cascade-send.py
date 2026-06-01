#!/usr/bin/env python3
"""welfare-cascade-send.py - Welfare escalation action executor.

Called by the welfare-check reasoning prompt (scheduled-tasks/welfare-check-prompt.md)
to execute a specific escalation stage action without duplicating Twilio/AgentMail
send logic inside the prompt.

Usage:
  python3 scripts/welfare-cascade-send.py stage <stage_num> --hours <N> [--dry-run]
  python3 scripts/welfare-cascade-send.py clear [--dry-run]
  python3 scripts/welfare-cascade-send.py discord-ping --message "text" [--dry-run]

Exit codes:
  0 - success
  1 - send failure (logged, caller should update state regardless)
  2 - config error (missing creds, bad contacts file)

All Twilio/AgentMail creds come from $CHASSIS_HOME/.env. Never hardcoded here.
Contacts come from $CHASSIS_HOME/data/emergency-contacts.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTACTS_PATH = REPO / "data" / "emergency-contacts.json"
STATE_PATH = REPO / "data" / "welfare-escalation-state.json"
VAULT_DIR = pathlib.Path.home() / ".angel-vault"
REASONING_LOG = VAULT_DIR / "welfare-reasoning.jsonl"

# Per-install identifiers — read at runtime from .env. The chassis ships with
# no hardcoded operator names, phone numbers, or Discord channel IDs.
#   PRINCIPAL_NAME           Full name of the operator (e.g. "Jane Doe")
#   PRINCIPAL_FIRST_NAME     First name only — used in conversational templates
#   PRINCIPAL_MOBILE         E.164 phone (e.g. "+14155551234")
#   ASSISTANT_NAME           Bot's name (e.g. "${ASSISTANT_NAME}", "Asimov")
#   ASSISTANT_DISPLAY_NAME   Full display name (e.g. "${ASSISTANT_NAME} - Jane Doe's AI assistant")
#   DISCORD_PRIMARY_CHANNEL_ID  Numeric Discord channel ID for ops messages
#   AGENTMAIL_FROM           Send-as identity for AgentMail
def _identity(env: dict[str, str]) -> dict[str, str]:
    principal_name = env.get("PRINCIPAL_NAME", "the operator")
    principal_first = env.get("PRINCIPAL_FIRST_NAME") or principal_name.split()[0]
    assistant_name = env.get("ASSISTANT_NAME", "the assistant")
    assistant_display = env.get("ASSISTANT_DISPLAY_NAME", f"{assistant_name} - {principal_name}'s AI assistant")
    return {
        "principal_name": principal_name,
        "principal_first": principal_first,
        "principal_mobile": env.get("PRINCIPAL_MOBILE", ""),
        "assistant_name": assistant_name,
        "assistant_display": assistant_display,
        "discord_primary_channel_id": env.get("DISCORD_PRIMARY_CHANNEL_ID", ""),
        "agentmail_from": env.get("AGENTMAIL_FROM", "noreply@example.com"),
    }


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        from _loadenv import load_env as _unified  # type: ignore
        return dict(_unified())
    except ImportError:
        pass
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def send_twilio_sms(to: str, body: str, env: dict, dry_run: bool) -> bool:
    sid = env.get("TWILIO_ACCOUNT_SID", "")
    token = env.get("TWILIO_AUTH_TOKEN", "")
    from_num = env.get("TWILIO_FROM_NUMBER") or env.get("TWILIO_PHONE_NUMBER", "+18336996475")
    if not sid or not token:
        print(f"ERROR: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN missing from .env", file=sys.stderr)
        return False
    if dry_run:
        print(f"[DRY-RUN] SMS to {to}: {body[:80]}...")
        return True
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urlencode({"From": from_num, "To": to, "Body": body}).encode()
    creds = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = Request(url, data=data, headers={"Authorization": f"Basic {creds}"}, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"SMS sent to {to}: sid={result.get('sid')}")
            return True
    except HTTPError as e:
        body_err = e.read().decode()[:200]
        print(f"ERROR: Twilio SMS to {to} failed: {e.code} {body_err}", file=sys.stderr)
        return False


def send_agentmail(to_email: str, to_name: str, subject: str, body: str, env: dict, dry_run: bool) -> bool:
    api_key = env.get("AGENTMAIL_API_KEY", "")
    if not api_key:
        print(f"ERROR: AGENTMAIL_API_KEY missing from .env", file=sys.stderr)
        return False
    if dry_run:
        print(f"[DRY-RUN] AgentMail to {to_email}: {subject}")
        return True
    ident = _identity(env)
    payload = json.dumps({
        "from": {"email": ident["agentmail_from"], "name": ident["assistant_display"]},
        "to": [{"email": to_email, "name": to_name}],
        "subject": subject,
        "text": body,
    }).encode()
    req = Request(
        "https://api.agentmail.to/v0/send",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"AgentMail sent to {to_email}: id={result.get('id')}")
            return True
    except HTTPError as e:
        body_err = e.read().decode()[:200]
        print(f"ERROR: AgentMail to {to_email} failed: {e.code} {body_err}", file=sys.stderr)
        return False


def send_discord_message(channel_id: str, message: str, env: dict, dry_run: bool) -> bool:
    token = env.get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN missing from .env", file=sys.stderr)
        return False
    if dry_run:
        print(f"[DRY-RUN] Discord #{channel_id}: {message[:80]}...")
        return True
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": message}).encode()
    req = Request(url, data=payload, headers={
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"Discord message sent: id={result.get('id')}")
            return True
    except HTTPError as e:
        body_err = e.read().decode()[:200]
        print(f"ERROR: Discord send failed: {e.code} {body_err}", file=sys.stderr)
        return False


def load_contacts() -> list[dict]:
    if not CONTACTS_PATH.exists():
        print(f"ERROR: contacts file not found: {CONTACTS_PATH}", file=sys.stderr)
        return []
    return json.loads(CONTACTS_PATH.read_text()).get("contacts", [])


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"escalation_active": False, "escalation_stage": 0, "channels_notified": [], "stage_history": []}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def append_reasoning_log(record: dict) -> None:
    VAULT_DIR.mkdir(mode=0o700, exist_ok=True)
    REASONING_LOG.touch(mode=0o600, exist_ok=True)
    with REASONING_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    REASONING_LOG.chmod(0o600)


def run_stage_0(hours: int, env: dict, dry_run: bool) -> int:
    ident = _identity(env)
    state = load_state()
    if state.get("escalation_stage", 0) >= 1:
        print("Stage 0 already fired - skipping")
        return 0
    msg = (
        f"{ident['principal_first']} - I haven't heard from you in {hours}h and none of my ambient "
        "signals can account for it. Please reply anything to let me know you're OK - even a "
        "thumbs up. If I don't hear back in 1 hour I'll send you a direct SMS."
    )
    ok = send_discord_message(ident["discord_primary_channel_id"], msg, env, dry_run)
    now = datetime.now(timezone.utc).isoformat()
    state["escalation_active"] = True
    state["escalation_stage"] = 1
    state["concern_classified_at"] = now
    state.setdefault("channels_notified", []).append("discord")
    state.setdefault("stage_history", []).append({"stage": 0, "fired_at": now, "ok": ok, "dry_run": dry_run})
    if not dry_run:
        save_state(state)
    return 0 if ok else 1


def run_stage_1(hours: int, env: dict, dry_run: bool) -> int:
    ident = _identity(env)
    state = load_state()
    if state.get("escalation_stage", 0) >= 2:
        print("Stage 1 already fired - skipping")
        return 0
    msg = (
        f"Hi {ident['principal_first']} - this is {ident['assistant_name']}, your AI assistant. "
        f"You've been unreachable for {hours}h and I'm unable to confirm you're OK through any "
        "of my ambient signals. Please reply anything so I can cancel further escalation."
    )
    ok = send_twilio_sms(ident["principal_mobile"], msg, env, dry_run)
    now = datetime.now(timezone.utc).isoformat()
    state["escalation_stage"] = 2
    state.setdefault("channels_notified", []).append("twilio_sean")
    state.setdefault("stage_history", []).append({"stage": 1, "fired_at": now, "ok": ok, "dry_run": dry_run})
    if not dry_run:
        save_state(state)
    return 0 if ok else 1


def run_stage_2(hours: int, env: dict, dry_run: bool) -> int:
    ident = _identity(env)
    state = load_state()
    if state.get("escalation_stage", 0) >= 3:
        print("Stage 2 already fired - skipping")
        return 0
    contacts = load_contacts()
    tier1 = [c for c in contacts if c.get("priority") in (1, 2)]
    all_ok = True
    now = datetime.now(timezone.utc).isoformat()
    for contact in tier1:
        name = contact["name"]
        phone = contact.get("phone", "")
        if not phone:
            continue
        principal_mobile_clause = (
            f" Their number is {ident['principal_mobile']}." if ident["principal_mobile"] else ""
        )
        msg = (
            f"Hi {name.split()[0]} - this is {ident['assistant_display']}. "
            f"I've been unable to reach {ident['principal_first']} for {hours}h. I've already "
            "texted them directly with no response. Could you try calling or texting them? If "
            "you hear from them, please reply so I can stand down."
            f"{principal_mobile_clause}"
        )
        ok = send_twilio_sms(phone, msg, env, dry_run)
        if ok:
            state.setdefault("channels_notified", []).append(f"twilio_{name.split()[0].lower()}")
        else:
            all_ok = False
    state["escalation_stage"] = 3
    state.setdefault("stage_history", []).append({"stage": 2, "fired_at": now, "ok": all_ok, "dry_run": dry_run})
    if not dry_run:
        save_state(state)
    return 0 if all_ok else 1


def _stage3_opening_msg(ident: dict, hours: int) -> str:
    return (
        f"Hi everyone - this is {ident['assistant_display']}. I'm contacting you per "
        f"{ident['principal_first']}'s pre-arranged welfare protocol. "
        f"{ident['principal_first']} has been unreachable for {hours}h and their local contacts "
        "haven't confirmed they're OK yet. Nothing necessarily wrong - they could be traveling "
        "or offline. I'm putting you all in touch so you can coordinate. If anyone has heard "
        f"from {ident['principal_first']} recently or can reach them, please reply. I'll "
        "continue monitoring and let you know if the situation resolves."
    )


def run_stage_3(hours: int, env: dict, dry_run: bool) -> int:
    ident = _identity(env)
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        from _imessage_group import send_imessage_group  # type: ignore
    except ImportError:
        send_imessage_group = None  # iMessage group helper is install-specific (private side)

    state = load_state()
    if state.get("escalation_stage", 0) >= 4:
        print("Stage 3 already fired - skipping")
        return 0
    contacts = load_contacts()
    subject = f"Welfare check - {ident['principal_name']} unreachable for {hours}h"
    now = datetime.now(timezone.utc).isoformat()
    all_ok = True

    for contact in contacts:
        name = contact["name"]
        email = contact.get("email", "")
        if not email:
            continue
        template = contact.get("message_template", "")
        body = template if template else (
            f"Hi {name} - this is {ident['assistant_display']}. "
            f"{ident['principal_first']} has been unreachable for {hours}h. You're receiving "
            "this as an emergency contact. Please try to reach them. Reply to this email if "
            "you make contact."
        )
        ok = send_agentmail(email, name, subject, body, env, dry_run)
        if not ok:
            all_ok = False

    state.setdefault("channels_notified", []).append("agentmail_all")

    imessage_capable = [c for c in contacts if c.get("imessage_capable", True)]
    phones = [c["phone"] for c in imessage_capable if c.get("phone")]

    if send_imessage_group is None:
        print("[Stage 3] iMessage group helper not available - emailing only")
        state["stage3_imessage_method"] = "skipped_no_helper"
    elif len(imessage_capable) < len(contacts):
        missing = [c["name"] for c in contacts if not c.get("imessage_capable", True)]
        print(f"[Stage 3] Skipping iMessage group - contacts not marked capable: {missing}")
        print("[Stage 3] Falling back to AgentMail-only for Stage 3")
        state["stage3_imessage_method"] = "skipped_capability_check"
    elif not phones:
        print("[Stage 3] No phone numbers available - skipping iMessage group")
        state["stage3_imessage_method"] = "skipped_no_phones"
    else:
        opening = _stage3_opening_msg(ident, hours)
        imsg_result = send_imessage_group(phones, opening, dry_run=dry_run)
        state["stage3_imessage_method"] = imsg_result["method"]
        if imsg_result.get("chat_id"):
            state["stage3_imessage_chat_id"] = imsg_result["chat_id"]
        if not imsg_result["success"]:
            print(f"[Stage 3] iMessage send failed: {imsg_result.get('error')}", file=sys.stderr)
            all_ok = False
        channels_notified = state.setdefault("channels_notified", [])
        if imsg_result["method"] == "group":
            channels_notified.append("imessage_group")
        elif imsg_result["method"] in ("individual_fallback",):
            channels_notified.append("imessage_individual")
        elif imsg_result["method"] == "cooldown_skip":
            channels_notified.append("imessage_cooldown_skipped")

    state["escalation_stage"] = 4
    state.setdefault("stage_history", []).append({"stage": 3, "fired_at": now, "ok": all_ok, "dry_run": dry_run})
    if not dry_run:
        save_state(state)
    return 0 if all_ok else 1


def run_stage_4(hours: int, env: dict, dry_run: bool) -> int:
    ident = _identity(env)
    state = load_state()
    if state.get("escalation_stage", 0) >= 5:
        print("Stage 4 already fired - skipping")
        return 0
    contacts = load_contacts()
    mother = next((c for c in contacts if c.get("relationship") == "mother"), None)
    now = datetime.now(timezone.utc).isoformat()
    all_ok = True
    if mother:
        mother_first = mother["name"].split()[0]
        default_msg = (
            f"Hi {mother_first} - this is {ident['assistant_display']}. "
            f"{ident['principal_first']} has been unreachable for {hours}h. Please try to reach them."
        )
        msg = mother.get("message_template", default_msg)
        ok = send_twilio_sms(mother["phone"], msg, env, dry_run)
        if not ok:
            all_ok = False
        state.setdefault("channels_notified", []).append(f"twilio_{mother_first.lower()}")
    subject = f"UPDATE: Welfare check - {ident['principal_name']} still unreachable for {hours}h"
    body = (
        f"This is a follow-up from {ident['assistant_display']}. "
        f"As of {now}, {ident['principal_first']} has still not responded. "
        "All contacts on the emergency list have now been notified. "
        "If you can reach them or their address, please try. "
        "Reply to this email if you make contact."
    )
    for contact in contacts:
        email = contact.get("email", "")
        if not email:
            continue
        ok = send_agentmail(email, contact["name"], subject, body, env, dry_run)
        if not ok:
            all_ok = False
    state["escalation_stage"] = 5
    state.setdefault("stage_history", []).append({"stage": 4, "fired_at": now, "ok": all_ok, "dry_run": dry_run})
    if not dry_run:
        save_state(state)
    return 0 if all_ok else 1


def run_clear(hours_since: int, env: dict, dry_run: bool) -> int:
    ident = _identity(env)
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        from _imessage_group import send_imessage_followup, get_last_group_chat_id  # type: ignore
    except ImportError:
        send_imessage_followup = None  # type: ignore
        get_last_group_chat_id = None  # type: ignore

    state = load_state()
    channels = state.get("channels_notified", [])
    now = datetime.now(timezone.utc).isoformat()
    all_ok = True

    principal_twilio_key = f"twilio_{ident['principal_first'].lower()}"

    if "discord" in channels:
        msg = f"Update: {ident['principal_first']} has responded. All clear. Sorry for the concern."
        ok = send_discord_message(ident["discord_primary_channel_id"], msg, env, dry_run)
        if not ok:
            all_ok = False

    contacts = load_contacts()
    # The legacy channel key was "twilio_sean" — accept both the legacy and the parameterised form.
    if principal_twilio_key in channels or "twilio_sean" in channels:
        ok = send_twilio_sms(ident["principal_mobile"], "Update: Clearance confirmed. All good.", env, dry_run)
        if not ok:
            all_ok = False

    # Per-install tier-1 contact keys are configurable via the contacts file's `tier`
    # field; this loop notifies any contact whose twilio_<first> key was recorded.
    for contact in contacts:
        first = contact["name"].split()[0]
        key = f"twilio_{first.lower()}"
        if key in channels and key != principal_twilio_key and key != "twilio_sean":
            msg = (
                f"Hi {first} - update from {ident['assistant_name']}: "
                f"{ident['principal_first']} has responded and is OK. "
                "Thank you for being on the emergency list. No further action needed."
            )
            ok = send_twilio_sms(contact["phone"], msg, env, dry_run)
            if not ok:
                all_ok = False

    if ("imessage_group" in channels or "imessage_individual" in channels) and send_imessage_followup is not None and get_last_group_chat_id is not None:
        chat_id = state.get("stage3_imessage_chat_id") or get_last_group_chat_id()
        clearance_msg = (
            f"False alarm - all clear. {ident['principal_first']} has responded and is OK. "
            "Thank you all for being on the emergency contact list. No further action needed."
        )
        if "imessage_group" in channels and chat_id:
            ok = send_imessage_followup(chat_id, clearance_msg, dry_run=dry_run)
            if not ok:
                all_ok = False
        elif "imessage_individual" in channels:
            imessage_capable = [c for c in contacts if c.get("imessage_capable", True)]
            for contact in imessage_capable:
                phone = contact.get("phone", "")
                if phone:
                    ok = send_imessage_followup(phone, clearance_msg, dry_run=dry_run)
                    if not ok:
                        all_ok = False

    if "agentmail_all" in channels:
        subject = f"UPDATE: {ident['principal_name']} is OK - welfare check cleared"
        body = (
            f"This is an update from {ident['assistant_display']}. "
            f"{ident['principal_first']} has been in contact and is OK. No further action is needed. "
            "Sorry for any concern this may have caused."
        )
        for contact in contacts:
            email = contact.get("email", "")
            if not email:
                continue
            ok = send_agentmail(email, contact["name"], subject, body, env, dry_run)
            if not ok:
                all_ok = False

    state["escalation_active"] = False
    state["cleared_at"] = now
    state["cleared_by"] = "discord_response"
    if not dry_run:
        save_state(state)
        last_seen_path = REPO / "data" / "welfare-last-seen.txt"
        import time
        last_seen_path.write_text(str(int(time.time())))
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Welfare escalation action executor")
    sub = parser.add_subparsers(dest="command")

    p_stage = sub.add_parser("stage", help="Fire a specific escalation stage")
    p_stage.add_argument("stage_num", type=int, choices=[0, 1, 2, 3, 4])
    p_stage.add_argument("--hours", type=int, default=18)
    p_stage.add_argument("--dry-run", action="store_true")

    p_clear = sub.add_parser("clear", help="Send all-clear to notified channels")
    p_clear.add_argument("--hours", type=int, default=0)
    p_clear.add_argument("--dry-run", action="store_true")

    p_discord = sub.add_parser("discord-ping", help="Send a Discord message to #<primary>")
    p_discord.add_argument("--message", required=True)
    p_discord.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 2

    dry_run = getattr(args, "dry_run", False) or os.environ.get("WELFARE_DRY_RUN", "").lower() == "true"
    env = load_env()

    if args.command == "stage":
        fns = {0: run_stage_0, 1: run_stage_1, 2: run_stage_2, 3: run_stage_3, 4: run_stage_4}
        return fns[args.stage_num](args.hours, env, dry_run)
    elif args.command == "clear":
        return run_clear(args.hours, env, dry_run)
    elif args.command == "discord-ping":
        ident = _identity(env)
        ok = send_discord_message(ident["discord_primary_channel_id"], args.message, env, dry_run)
        return 0 if ok else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
