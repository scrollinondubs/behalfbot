#!/usr/bin/env python3
"""gmail-attachment.py - Fetch Gmail attachments over IMAP.

The Claude.ai Gmail connector exposes get_thread / get_message /
search_threads / create_draft / label ops and no attachment endpoint at all.
Every install hits that wall the first time someone forwards a PDF, so this
belongs in the chassis rather than in one customer's tree. Ported from
scrollinondubs/new-jaxity#314 (decision record: new-jaxity#311).

Why IMAP + a Workspace app password, and not a self-hosted Gmail MCP with our
own OAuth: an OAuth consent screen left at External + Testing issues refresh
tokens that expire after seven days. The failure is silent - the agent simply
stops being able to read mail, with no error until someone looks. At least one
install in this fleet is believed to have been sitting in that state
(gmail-ozzy). IMAP has no consent screen, no Cloud project, and no expiry
cliff. A service account with domain-wide delegation was also rejected: DWD
grants access to every mailbox on the domain, which is wildly disproportionate
for fetching a PDF.

Standard library only - imaplib, email, ssl. No new dependencies, nothing to
add to requirements.txt.

Read-only. This script never sets IMAP flags, moves messages, or marks
anything as read: it opens mailboxes with readonly=True and fetches with
BODY.PEEK[]. Flag-writing (a Processed-label workflow) would land separately.

Credentials
===========
Two env vars, both already in the default Vaultwarden manifest in
chassis/scripts/hydrate-env-from-vw.sh under "Behalf.bot - Google Workspace
agent":

    GOOGLE_AGENT_EMAIL          the mailbox to read (also the IMAP username)
    GOOGLE_AGENT_APP_PASSWORD   a Google app password for that account

Deliberately no aliases and no second accepted name for either var. The
chassis has already been bitten once by carrying two names for one secret
(NOTION_API_TOKEN vs NOTION_INTEGRATION_TOKEN - see the note in
hydrate-env-from-vw.sh): an installer who followed the wrong one shipped a
literal placeholder as a bearer token and 401'd on every call.

The password is read at invocation time, never written to disk by this
script, and never included in any log or error message.

Operator setup: see docs/gmail-attachments.md.

CLI usage:
  # List attachments on the message matching a Gmail search
  python3 chassis/scripts/gmail-attachment.py list --gmail-search 'subject:"Invoice"'

  # Fetch one by name (substring match) or by the index shown by `list`
  python3 chassis/scripts/gmail-attachment.py fetch --gmail-search '...' --name Invoice -o ~/Downloads
  python3 chassis/scripts/gmail-attachment.py fetch --gmail-search '...' --index 0 -o ~/Downloads

  # Fetch everything
  python3 chassis/scripts/gmail-attachment.py fetch-all --message-id '<abc@mail.example>' -o ~/Downloads

  # Config check for smoke-test.sh - env only, no network
  python3 chassis/scripts/gmail-attachment.py check
"""
from __future__ import annotations

import argparse
import email
import imaplib
import os
import re
import sys
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# The env var contract. See the module docstring for why there is exactly one
# accepted name per value.
ENV_USER = "GOOGLE_AGENT_EMAIL"
ENV_PASSWORD = "GOOGLE_AGENT_APP_PASSWORD"

# Gmail's "All Mail" is the only folder guaranteed to contain a message
# regardless of which label it carries, so it is the right default to search.
DEFAULT_MAILBOX = "[Gmail]/All Mail"

# Refuse to write anything larger than this. A 25 MB cap matches Gmail's own
# per-message attachment limit, so a legitimate attachment cannot exceed it.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

# Characters allowed through to the filesystem. Everything else becomes "_".
# Deliberately an allowlist: attachment filenames arrive from whoever sent
# the mail, so a denylist is the wrong shape here.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ ()\[\]+,&@-]")

_MAX_FILENAME_LEN = 200

# How many matching messages to download while hunting for one with an
# attachment. A thread search can match dozens; downloading all of them to
# answer "where is the PDF" is wasteful.
_MAX_SCAN = 25


class Attachment(NamedTuple):
    """One attachment leaf found in a message tree."""

    filename: str  # already sanitised
    raw_filename: str  # as it appeared on the wire, for display only
    content_type: str
    part: Message
    path: tuple[int, ...]  # index path through the MIME tree
    nested: bool  # True if it lives inside a message/rfc822 forward


# ---------------------------------------------------------------------------
# Pure helpers. These are the parts under unit test - no mailbox required.
# ---------------------------------------------------------------------------


def decode_encoded_word(value: str | None) -> str:
    """Decode an RFC 2047 encoded-word header, e.g. =?UTF-8?B?...?=.

    Message.get_filename() collapses RFC 2231 continuations but leaves RFC
    2047 encoded-words untouched, so senders that use the older form come
    back as literal "=?UTF-8?B?..." text without this step.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        # A malformed header must not abort the whole fetch. Falling back to
        # the raw string is safe: sanitize_filename() runs on it either way.
        return value


def sanitize_filename(raw: str | None, fallback: str = "attachment.bin") -> str:
    """Reduce an attacker-controlled filename to a single safe path segment.

    Someone can email a file called ../../.ssh/authorized_keys. Everything
    that could steer the write elsewhere is stripped here; safe_join() then
    re-checks the resolved path as a second, independent barrier.
    """
    if not raw:
        return fallback

    name = decode_encoded_word(raw)
    name = name.replace("\x00", "")

    # Normalise backslashes to forward slashes before taking the basename,
    # otherwise a Windows-style "..\\..\\evil" survives posix basename intact.
    name = name.replace("\\", "/")
    name = name.split("/")[-1]

    name = _SAFE_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()

    # Leading/trailing dots would leave ".." and "." intact and would create
    # hidden files from names like ".bashrc".
    name = name.strip(". ")

    if not name:
        return fallback

    if len(name) > _MAX_FILENAME_LEN:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: _MAX_FILENAME_LEN - len(ext) - 1] + "." + ext
        else:
            name = name[:_MAX_FILENAME_LEN]

    return name


def safe_join(target_dir: str | os.PathLike, filename: str) -> Path:
    """Resolve filename inside target_dir, refusing anything that escapes.

    resolve() follows symlinks, so this also rejects the case where the
    target directory contains a symlink pointing outside the tree.
    """
    base = Path(target_dir).resolve()
    candidate = (base / filename).resolve()
    if candidate == base or base not in candidate.parents:
        raise ValueError(
            f"refusing to write outside {base}: sanitised name {filename!r} "
            f"resolved to {candidate}"
        )
    return candidate


def get_part_filename(part: Message) -> str:
    """Best-effort filename for a MIME part, before sanitisation."""
    raw = part.get_filename()
    if not raw:
        # Some senders put the name only in Content-Type; name= is legacy but
        # still common from Outlook and from scanner/MFP appliances.
        raw = part.get_param("name")
        if isinstance(raw, tuple):
            # RFC 2231 form: (charset, language, value)
            raw = raw[2]
    return decode_encoded_word(raw) if raw else ""


def is_attachment(part: Message, include_inline: bool = False) -> bool:
    """Decide whether a leaf part is something a caller wanted saved.

    The case that matters: an HTML mail with an embedded logo has an
    image/png part with Content-Disposition: inline and a Content-ID that the
    HTML references. That is page furniture, not an attachment, and treating
    it as one buries the real file in noise.
    """
    disposition = (part.get_content_disposition() or "").lower()
    filename = get_part_filename(part)
    ctype = part.get_content_type()

    if disposition == "attachment":
        return True

    if disposition == "inline":
        if part.get("Content-ID") and ctype.startswith("image/"):
            return include_inline
        return bool(filename)

    # No Content-Disposition at all. A filename is then the only signal that
    # this is a file rather than the message body.
    return bool(filename)


def walk_parts(
    part: Message, path: tuple[int, ...] = (), nested: bool = False
) -> Iterator[tuple[Message, tuple[int, ...], bool]]:
    """Yield every leaf part, descending into message/rfc822 forwards.

    message/rfc822 must be handled before the generic is_multipart() branch:
    it reports as multipart, but its payload is a one-element list holding the
    whole forwarded message, and the attachments we want are inside that.
    """
    if part.get_content_type() == "message/rfc822":
        # A message/rfc822 carrying Content-Disposition: attachment is itself
        # a file the sender attached - Gmail shows it as a .eml. Yield it as
        # well as descending, because the two cases both occur and neither
        # subsumes the other: a forwarded proposal is a PDF *inside* a
        # message/rfc822, while a covering note with seven .eml files attached
        # has nothing but body text inside each one. Found by scanning real
        # mail during new-jaxity#314: recursing without yielding reported that
        # second message as having zero attachments.
        if (part.get_content_disposition() or "").lower() == "attachment":
            yield part, path, nested

        payload = part.get_payload()
        if isinstance(payload, list):
            for i, sub in enumerate(payload):
                yield from walk_parts(sub, path + (i,), nested=True)
        return

    if part.is_multipart():
        payload = part.get_payload()
        if isinstance(payload, list):
            for i, sub in enumerate(payload):
                yield from walk_parts(sub, path + (i,), nested=nested)
        return

    yield part, path, nested


def collect_attachments(msg: Message, include_inline: bool = False) -> list[Attachment]:
    """Every attachment in a message, forwards included, in tree order."""
    found: list[Attachment] = []
    for part, path, nested in walk_parts(msg):
        if not is_attachment(part, include_inline=include_inline):
            continue
        raw = get_part_filename(part)
        subtype = part.get_content_subtype() or "bin"
        fallback = f"part-{'-'.join(str(i) for i in path) or '0'}.{subtype}"
        found.append(
            Attachment(
                filename=sanitize_filename(raw, fallback=fallback),
                raw_filename=raw or "(no filename)",
                content_type=part.get_content_type(),
                part=part,
                path=path,
                nested=nested,
            )
        )
    return found


def decode_payload(att: Attachment, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Decoded bytes for an attachment, with a size cap.

    get_payload(decode=True) handles base64 and quoted-printable, and returns
    the raw bytes for 7bit/8bit/binary. It returns None only when the part is
    a container, which cannot happen for a leaf.
    """
    if att.content_type == "message/rfc822":
        # get_payload(decode=True) returns None for a container. An attached
        # .eml is reconstructed by serialising the message it holds.
        payload = att.part.get_payload()
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"{att.filename}: empty message/rfc822 part")
        data = payload[0].as_bytes()
    else:
        data = att.part.get_payload(decode=True)

    if data is None:
        raise ValueError(f"{att.filename}: part has no decodable payload")
    if len(data) > max_bytes:
        raise ValueError(
            f"{att.filename}: {len(data)} bytes exceeds the {max_bytes} byte cap. "
            f"Raise it with --max-bytes if this attachment is genuinely that large."
        )
    return data


def unique_path(path: Path) -> Path:
    """Add a -1, -2 ... suffix rather than clobbering an existing file."""
    if not path.exists():
        return path
    stem, ext = path.stem, path.suffix
    for n in range(1, 1000):
        candidate = path.with_name(f"{stem}-{n}{ext}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not find a free filename near {path}")


def write_attachment(
    att: Attachment, target_dir: str | os.PathLike, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[Path, int]:
    """Write one attachment into target_dir. Returns (path, bytes written)."""
    data = decode_payload(att, max_bytes=max_bytes)
    dest = unique_path(safe_join(target_dir, att.filename))
    dest.write_bytes(data)
    return dest, len(data)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def customer_home() -> Path | None:
    """Customer state root, per the resolution order in chassis/scripts/_env.sh.

    Same precedence the other Python helpers use (pacman-queue-add.py):
    CHASSIS_HOME first for legacy co-located installs, then CUSTOMER_HOME,
    then the in-container bind-mount, then the new-install host default.
    """
    for var in (os.environ.get("CHASSIS_HOME"), os.environ.get("CUSTOMER_HOME")):
        if var and var.strip():
            return Path(var.strip()).expanduser()
    for candidate in (Path("/app/customer"), Path.home() / ".behalfbot"):
        if candidate.is_dir():
            return candidate
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=value reader for a hydrated .env. Never raises."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        # Strip one layer of matched quotes; hydrate-env-from-vw.sh writes
        # bare values but hand-edited .env files often quote them.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _from_env_file(name: str) -> str:
    """Re-read a var straight off the hydrated .env on disk.

    This is the chassis equivalent of the `bw sync` retry in the new-jaxity
    original. There, the bw CLI reads a local encrypted cache rather than the
    server, so an item created minutes earlier was invisible until a sync -
    the very first live run of that script failed on exactly this. The chassis
    never reaches Vaultwarden at runtime (hydration is bootstrap-time only, see
    hydrate-env-from-vw.sh), so the same staleness shows up in a different
    place: the process env was captured before the operator hydrated or
    re-hydrated. Reading the file settles it without a restart.
    """
    home = customer_home()
    if home is None:
        return ""
    for candidate in (home / ".env", home / ".env.baked"):
        if candidate.is_file():
            value = _parse_env_file(candidate).get(name, "").strip()
            if value:
                return value
    return ""


def resolve_credential(name: str) -> str:
    """Process env first, hydrated .env on disk second, empty string if neither."""
    return os.environ.get(name, "").strip() or _from_env_file(name)


def credential_status() -> tuple[str, str]:
    """(status, message) for the smoke test. Env only - deliberately no network.

    No login attempt here. smoke-test.sh runs at every boot, and repeated
    failed IMAP logins against Google get the account rate-limited and
    eventually flagged. A half-configured install is the failure this catches,
    and that is visible from the env alone.
    """
    user = resolve_credential(ENV_USER)
    password = resolve_credential(ENV_PASSWORD)

    if not user and not password:
        return "SKIP", (
            f"{ENV_USER}/{ENV_PASSWORD} unset - Gmail attachment fetching not "
            f"configured for this install"
        )
    if not user:
        return "FAIL", (
            f"{ENV_PASSWORD} is set but {ENV_USER} is not - no mailbox to read. "
            f"Both come from the Vaultwarden item 'Behalf.bot - Google Workspace agent'."
        )
    if not password:
        return "FAIL", (
            f"{ENV_USER} is set but {ENV_PASSWORD} is not - IMAP login will fail. "
            f"Both come from the Vaultwarden item 'Behalf.bot - Google Workspace agent'."
        )
    return "PASS", f"Gmail IMAP credentials present for {user}"


def require_credentials() -> tuple[str, str]:
    """Both credentials or a RuntimeError naming the fix."""
    user = resolve_credential(ENV_USER)
    password = resolve_credential(ENV_PASSWORD)
    missing = [n for n, v in ((ENV_USER, user), (ENV_PASSWORD, password)) if not v]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} not set. Populate the Vaultwarden item "
            f"'Behalf.bot - Google Workspace agent' (username = the mailbox, "
            f"password = a Google app password), then re-run "
            f"chassis/scripts/hydrate-env-from-vw.sh. See docs/gmail-attachments.md."
        )
    return user, password


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


def connect(mailbox: str = DEFAULT_MAILBOX) -> imaplib.IMAP4_SSL:
    """Log in and select a mailbox read-only.

    Google app passwords are shown with spaces for legibility; IMAP wants
    them without. Stripping here means a paste from the Google UI works.
    """
    user, password = require_credentials()
    password = password.replace(" ", "")
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error:
        # Deliberately does not echo the server's reply: a failed IMAP LOGIN
        # response can quote the credential back.
        raise RuntimeError(
            f"IMAP login failed for {user}. Check that 2FA is on for that account, "
            f"that the Workspace admin console still permits app passwords, and "
            f"that {ENV_PASSWORD} holds a current one. See docs/gmail-attachments.md."
        ) from None
    finally:
        del password

    status, _ = conn.select(f'"{mailbox}"', readonly=True)
    if status != "OK":
        raise RuntimeError(f"could not select mailbox {mailbox!r}")
    return conn


def imap_quote(value: str) -> str:
    """Wrap a search term as an IMAP quoted string.

    Search terms containing spaces are not optional to quote: an unquoted
    subject:"Two Words" is parsed as two atoms and the server answers
    BAD Could not parse command.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _search(
    conn: imaplib.IMAP4_SSL, prefix: list[str], term: str | None = None
) -> list[bytes]:
    """UID SEARCH where `term` is the final, caller-supplied argument.

    Non-ASCII terms cannot go inline: they must be sent as a CHARSET UTF-8
    literal. imaplib exposes that through the `literal` attribute, which it
    appends to the end of the command, so `term` has to be the last argument.
    Any accented subject line takes this path.
    """
    if term is None:
        status, data = conn.uid("SEARCH", *prefix)
    elif term.isascii():
        status, data = conn.uid("SEARCH", *prefix, imap_quote(term))
    else:
        conn.literal = term.encode("utf-8")
        status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *prefix)

    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {data!r}")
    return data[0].split() if data and data[0] else []


def find_uids(
    conn: imaplib.IMAP4_SSL,
    message_id: str | None = None,
    gmail_search: str | None = None,
    subject: str | None = None,
    imap_search: str | None = None,
) -> list[bytes]:
    """Locate candidate message UIDs by whichever selector the caller gave."""
    if message_id:
        mid = message_id if message_id.startswith("<") else f"<{message_id}>"
        return _search(conn, ["HEADER", "Message-ID"], mid)
    if gmail_search:
        # X-GM-RAW is Gmail's IMAP extension: it accepts the same query
        # language as the Gmail search box, which is far stronger than plain
        # IMAP SEARCH and is the selector to reach for by default.
        return _search(conn, ["X-GM-RAW"], gmail_search)
    if subject:
        return _search(conn, ["HEADER", "SUBJECT"], subject)
    if imap_search:
        return _search(conn, imap_search.split())
    raise ValueError("one of --message-id, --gmail-search, --subject or --search is required")


def fetch_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> Message:
    """Download and parse one message by UID.

    BODY.PEEK[] rather than RFC822 so the \\Seen flag is not set - this
    script stays read-only even though the mailbox is already selected
    readonly.
    """
    status, data = conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"IMAP fetch failed for UID {uid.decode()}")
    return email.message_from_bytes(data[0][1])


def describe(msg: Message) -> str:
    subject = decode_encoded_word(msg.get("Subject")) or "(no subject)"
    sender = decode_encoded_word(msg.get("From")) or "(no sender)"
    date = msg.get("Date") or "(no date)"
    return f"{subject}\n  from: {sender}\n  date: {date}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_one_message(conn: imaplib.IMAP4_SSL, args) -> Message:
    """Pick the message to operate on, preferring one that has attachments.

    "Most recent match" is the wrong default here. A subject search across a
    thread returns every reply, and the replies are almost always the newest
    while the attachment sits on the original. Measured during new-jaxity#314:
    a subject search returned 7 messages, the PDF was on the oldest, and the 6
    newer replies carried nothing. So scan newest-first and stop at the first
    message that actually has an attachment.
    """
    if args.uid:
        return fetch_message(conn, str(args.uid).encode())

    uids = find_uids(
        conn,
        message_id=args.message_id,
        gmail_search=args.gmail_search,
        subject=args.subject,
        imap_search=args.search,
    )
    if not uids:
        raise RuntimeError("no message matched that selector")
    if len(uids) == 1:
        return fetch_message(conn, uids[0])

    scanned = list(reversed(uids))[:_MAX_SCAN]
    for uid in scanned:
        msg = fetch_message(conn, uid)
        if collect_attachments(msg, include_inline=args.include_inline):
            print(
                f"{len(uids)} messages matched; using UID {uid.decode()}, the "
                f"most recent one carrying an attachment. Pin another with --uid.",
                file=sys.stderr,
            )
            return msg

    print(
        f"{len(uids)} messages matched and none of the {len(scanned)} scanned "
        f"carry an attachment; reporting on the most recent.",
        file=sys.stderr,
    )
    return fetch_message(conn, uids[-1])


def _print_listing(attachments: Iterable[Attachment]) -> None:
    items = list(attachments)
    if not items:
        print("no attachments found")
        return
    for i, att in enumerate(items):
        marker = " [inside a forwarded message]" if att.nested else ""
        print(f"[{i}] {att.filename}  ({att.content_type}){marker}")
        if att.raw_filename and att.raw_filename != att.filename:
            print(f"     wire name: {att.raw_filename}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Gmail attachments over IMAP (read-only)."
    )
    parser.add_argument("command", choices=["list", "fetch", "fetch-all", "check"])

    selector = parser.add_argument_group("message selector (pick one)")
    selector.add_argument("--message-id", help="RFC 5322 Message-ID, with or without angle brackets")
    selector.add_argument("--gmail-search", help="Gmail search-box syntax, via IMAP X-GM-RAW")
    selector.add_argument("--subject", help="substring of the Subject header")
    selector.add_argument("--search", help="raw IMAP SEARCH criteria, space separated")
    selector.add_argument("--uid", type=int, help="exact IMAP UID, skipping the search step")

    parser.add_argument("--name", help="fetch: substring of the attachment filename")
    parser.add_argument("--index", type=int, help="fetch: index from the `list` output")
    parser.add_argument("-o", "--output-dir", default=".", help="directory to write into")
    parser.add_argument("--mailbox", default=DEFAULT_MAILBOX)
    parser.add_argument(
        "--include-inline",
        action="store_true",
        help="also save inline images referenced by the HTML body",
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)

    if args.command == "check":
        # smoke-test.sh contract: exactly one STATUS|message line, always
        # exit 0. The caller decides what a status means for the run.
        status, message = credential_status()
        print(f"{status}|{message}")
        return 0

    if args.command == "fetch" and args.name is None and args.index is None:
        parser.error("fetch requires --name or --index")

    conn = connect(args.mailbox)
    try:
        msg = _resolve_one_message(conn, args)
        print(describe(msg), file=sys.stderr)
        attachments = collect_attachments(msg, include_inline=args.include_inline)

        if args.command == "list":
            _print_listing(attachments)
            return 0

        if not attachments:
            print("no attachments found", file=sys.stderr)
            return 1

        if args.command == "fetch-all":
            selected = attachments
        elif args.index is not None:
            if not 0 <= args.index < len(attachments):
                print(
                    f"index {args.index} out of range (0..{len(attachments) - 1})",
                    file=sys.stderr,
                )
                return 1
            selected = [attachments[args.index]]
        else:
            needle = args.name.lower()
            selected = [
                a
                for a in attachments
                if needle in a.filename.lower() or needle in a.raw_filename.lower()
            ]
            if not selected:
                print(f"no attachment matched {args.name!r}", file=sys.stderr)
                _print_listing(attachments)
                return 1

        target = Path(args.output_dir).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        for att in selected:
            dest, size = write_attachment(att, target, max_bytes=args.max_bytes)
            print(f"{dest}  ({size} bytes)")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"gmail-attachment: {exc}", file=sys.stderr)
        sys.exit(2)
