#!/usr/bin/env python3
"""Seed the CRM from a contacts export the person downloaded themselves.

    crm_import.py FILE [--format auto|linkedin|google|outlook|vcard|facebook|instagram] [--apply]

Reads one export file and matches every row against the contacts already in
the CRM. Without --apply it only prints what it would do: new, duplicate,
skipped. With --apply it adds the new contacts and, on a duplicate, fills an
email, phone or notes field that is still empty. A field that already holds
something is never changed.

Accepted files (procedure and where each comes from: chassis/skills/crm-contact-import.md):
  linkedin   Connections.csv from LinkedIn's data archive
  google     Google Contacts "Google CSV" export
  outlook    Outlook.com / Outlook on the web "Export contacts" CSV
  vcard      .vcf from Google Contacts, iCloud, macOS/iPhone Contacts
  facebook   your_friends.json from Meta's "Export your information" (JSON)
  instagram  followers_1.json / following.json from the same tool (JSON)

A .zip is refused: archives such as LinkedIn's also hold messages, so only
the one contacts file is ever opened.

Matching, in order: email; a social profile URL (LinkedIn, Instagram, ...);
a phone number when the first names also agree; then the normalised name for
rows that carry none of those (Facebook friends). A company website is kept
in notes but never matched on. Each new contact's notes
get a "source:" line and, when the export has one, a "profile:" line, which
is what a re-run matches on.

Exit codes: 0 done, 2 the file could not be read, 3 the CRM is unavailable.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import quopri
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _crm as crm  # noqa: E402

FORMATS = ("linkedin", "google", "outlook", "vcard", "facebook", "instagram")
MAX_BYTES = 50 * 1024 * 1024
ALL_CONTACTS = 10_000_000


class ExportFileError(ValueError):
    """The file cannot be read as a supported export. Shown to the user."""


@dataclass
class Record:
    name: str | None
    email: str | None = None
    phone: str | None = None
    profile: str | None = None
    website: str | None = None
    company: str | None = None
    title: str | None = None


# ---- normalising ---------------------------------------------------------------

def _clean(value) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def norm_name(name: str | None) -> str | None:
    text = _clean(name)
    return text.casefold() if text else None


def norm_email(email: str | None) -> str | None:
    text = _clean(email)
    if not text or "@" not in text:
        return None
    return text.lower()


def norm_phone(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    # Fewer digits than this is an extension or junk; the tail tolerates
    # one export writing +351 and another 00351 or a bare national number.
    return digits[-9:] if len(digits) >= 7 else None


def norm_url(url: str | None) -> str | None:
    text = _clean(url)
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    if not host:
        return None
    return f"https://{host}{path}"


PROFILE_HOSTS = ("linkedin.com", "instagram.com", "facebook.com", "x.com", "twitter.com", "github.com")


def split_url(url: str | None) -> dict:
    """A personal profile on a social site identifies one person; any other
    URL (a company site) is kept as a website and never matched on."""
    u = norm_url(url)
    if not u:
        return {}
    host = urlsplit(u).netloc
    if urlsplit(u).path and any(host == h or host.endswith("." + h) for h in PROFILE_HOSTS):
        return {"profile": u}
    return {"website": u}


def _first(cell: str | None) -> str | None:
    # Google packs several values into one cell separated by " ::: ".
    return _clean((cell or "").split(":::")[0])


# ---- reading files -------------------------------------------------------------

def _decode(name: str, data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ExportFileError(f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB; is it the right file?")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _csv_rows(text: str, header_test) -> tuple[list[str], list[dict]]:
    """Rows after the first line header_test accepts, so preamble lines
    (LinkedIn's "Notes:") are skipped whatever their count."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        header = next(csv.reader([line]), [])
        if header_test([h.strip() for h in header]):
            reader = csv.DictReader(io.StringIO("".join(lines[i:])))
            reader.fieldnames = [h.strip() for h in reader.fieldnames or []]
            return reader.fieldnames, list(reader)
    return [], []


def _is_linkedin(h: list[str]) -> bool:
    return "First Name" in h and "Connected On" in h


def _is_google(h: list[str]) -> bool:
    return any(re.fullmatch(r"(E-mail|Phone) 1 - Value", x) for x in h)


def _is_outlook(h: list[str]) -> bool:
    return "First Name" in h and "E-mail Address" in h


def detect(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".vcf" or text.lstrip().upper().startswith("BEGIN:VCARD"):
        return "vcard"
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ExportFileError(f"{path.name} is not valid JSON: {e}") from None
        if isinstance(data, dict) and ("friends_v2" in data or "friends" in data):
            return "facebook"
        if "string_list_data" in text:
            return "instagram"
        raise ExportFileError(f"{path.name} is JSON but not a Facebook friends or Instagram followers/following file")
    head = text[:20000]
    for name, test in (("linkedin", _is_linkedin), ("outlook", _is_outlook), ("google", _is_google)):
        if _csv_rows(head, test)[0]:
            return name
    raise ExportFileError(f"cannot tell what export {path.name} is; pass --format")


def parse_linkedin(text: str) -> list[Record]:
    _, rows = _csv_rows(text, _is_linkedin)
    out = []
    for r in rows:
        name = _clean(f"{r.get('First Name') or ''} {r.get('Last Name') or ''}")
        out.append(Record(name=name, email=_clean(r.get("Email Address")), profile=_clean(r.get("URL")),
                          company=_clean(r.get("Company")), title=_clean(r.get("Position"))))
    return out


def parse_google(text: str) -> list[Record]:
    _, rows = _csv_rows(text, _is_google)
    out = []
    for r in rows:
        # 2024+ exports: First/Middle/Last Name, Organization Name/Title.
        # Older ones: Name, Given/Family Name, Organization 1 - Name/Title.
        name = _clean(r.get("Name")) or _clean(" ".join(
            r.get(k) or "" for k in ("First Name", "Middle Name", "Last Name", "Given Name",
                                     "Additional Name", "Family Name")))
        email = next((_first(v) for k, v in r.items() if k and re.fullmatch(r"E-mail \d+ - Value", k) and _first(v)), None)
        phone = next((_first(v) for k, v in r.items() if k and re.fullmatch(r"Phone \d+ - Value", k) and _first(v)), None)
        company = _clean(r.get("Organization Name")) or _clean(r.get("Organization 1 - Name"))
        title = _clean(r.get("Organization Title")) or _clean(r.get("Organization 1 - Title"))
        url = next((_first(v) for k, v in r.items() if k and re.fullmatch(r"Website \d+ - Value", k) and _first(v)), None)
        out.append(Record(name=name or company or email, email=email, phone=phone,
                          company=company, title=title, **split_url(url)))
    return out


def parse_outlook(text: str) -> list[Record]:
    _, rows = _csv_rows(text, _is_outlook)
    out = []
    for r in rows:
        name = _clean(" ".join(r.get(k) or "" for k in ("First Name", "Middle Name", "Last Name")))
        email = next((_clean(r.get(k)) for k in ("E-mail Address", "E-mail 2 Address", "E-mail 3 Address")
                      if _clean(r.get(k))), None)
        phone = next((_clean(r.get(k)) for k in ("Mobile Phone", "Primary Phone", "Business Phone", "Home Phone")
                      if _clean(r.get(k))), None)
        company = _clean(r.get("Company"))
        out.append(Record(name=name or company or email, email=email, phone=phone,
                          company=company, title=_clean(r.get("Job Title")), **split_url(r.get("Web Page"))))
    return out


def _vcard_unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def parse_vcard(text: str) -> list[Record]:
    # Unfold: a line starting with a space or tab continues the previous one.
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper():
            lines[-1] = lines[-1][:-1] + raw
        else:
            lines.append(raw)
    out: list[Record] = []
    card: dict[str, list[str]] | None = None
    for line in lines:
        if ":" not in line:
            continue
        left, value = line.split(":", 1)
        prop, *params = left.split(";")
        prop = prop.split(".")[-1].upper()
        upper = [p.upper() for p in params]
        if prop == "BEGIN" and value.strip().upper() == "VCARD":
            card = {}
            continue
        if card is None:
            continue
        if prop == "END":
            out.append(_vcard_record(card))
            card = None
            continue
        if "ENCODING=QUOTED-PRINTABLE" in upper or "QUOTED-PRINTABLE" in upper:
            charset = next((p.split("=", 1)[1] for p in params if p.upper().startswith("CHARSET=")), "utf-8")
            value = quopri.decodestring(value.encode("latin-1", errors="replace")).decode(charset, errors="replace")
        card.setdefault(prop, []).append(value)
    return out


def _vcard_record(card: dict[str, list[str]]) -> Record:
    def one(prop: str) -> str | None:
        return _clean(_vcard_unescape(card[prop][0])) if card.get(prop) else None

    name = one("FN")
    if not name and card.get("N"):
        family, given, *rest = (card["N"][0].split(";") + ["", ""])[:5]
        name = _clean(f"{_vcard_unescape(given)} {rest[0] if rest else ''} {_vcard_unescape(family)}")
    org = _clean(_vcard_unescape(card["ORG"][0].split(";")[0])) if card.get("ORG") else None
    email = one("EMAIL")
    return Record(name=name or org or email, email=email, phone=one("TEL"),
                  company=org, title=one("TITLE"), **split_url(one("URL")))


def _meta_text(value) -> str | None:
    # Meta's JSON writes UTF-8 bytes as \u00XX escapes; undo that when it applies.
    text = _clean(value)
    if not text:
        return None
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def parse_facebook(text: str) -> list[Record]:
    data = json.loads(text)
    items = data.get("friends_v2") or data.get("friends") or []
    return [Record(name=_meta_text(i.get("name"))) for i in items if isinstance(i, dict)]


def parse_instagram(text: str) -> list[Record]:
    data = json.loads(text)
    if isinstance(data, dict):
        items = [i for k, v in data.items() if k.startswith("relationships_") and isinstance(v, list) for i in v]
    else:
        items = data if isinstance(data, list) else []
    out = []
    for i in items:
        if not isinstance(i, dict):
            continue
        sld = (i.get("string_list_data") or [{}])[0] or {}
        href = sld.get("href") or ""
        # Newer following.json drops "value" and puts the handle in "title";
        # its href is instagram.com/_u/<handle>.
        handle = _clean(sld.get("value")) or _clean(i.get("title")) or _clean(href.rstrip("/").rsplit("/", 1)[-1])
        if not handle:
            out.append(Record(name=None))
            continue
        handle = handle.lstrip("@")
        out.append(Record(name=f"@{handle}", profile=f"https://www.instagram.com/{handle}"))
    return out


PARSERS = {"linkedin": parse_linkedin, "google": parse_google, "outlook": parse_outlook,
           "vcard": parse_vcard, "facebook": parse_facebook, "instagram": parse_instagram}


def _refuse_zip(name: str) -> None:
    if Path(name).suffix.lower() == ".zip":
        raise ExportFileError(
            f"{name} is a whole archive. Unzip it and pass only the contacts file "
            "(for LinkedIn: Connections.csv). Archives also hold messages, which this never opens.")


def load_bytes(name: str, data: bytes, fmt: str = "auto") -> tuple[str, list[Record]]:
    """load() for bytes already in memory (an upload); name is only used for
    its suffix and in messages."""
    _refuse_zip(name)
    text = _decode(name, data)
    fmt = detect(Path(name), text) if fmt == "auto" else fmt
    try:
        return fmt, PARSERS[fmt](text)
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        raise ExportFileError(f"{name} does not look like a {fmt} export: {e}") from None


def load(path: Path, fmt: str = "auto") -> tuple[str, list[Record]]:
    _refuse_zip(path.name)
    if not path.is_file():
        raise ExportFileError(f"{path} is not a file")
    return load_bytes(path.name, path.read_bytes(), fmt)


# ---- matching and writing ------------------------------------------------------

PROFILE_LINE = re.compile(r"^profile:\s*(\S+)\s*$", re.M)


def _first_word(name: str | None) -> str:
    n = norm_name(name)
    return n.lstrip("@").split(" ")[0] if n else ""


class Index:
    def __init__(self) -> None:
        self.names: dict[int, str | None] = {}
        self.by_email: dict[str, int] = {}
        self.by_phone: dict[str, list[int]] = {}
        self.by_profile: dict[str, int] = {}
        self.by_name: dict[str, int] = {}

    def add(self, cid: int, name=None, email=None, phone=None, notes=None) -> None:
        if name:
            self.names[cid] = name
        if norm_email(email):
            self.by_email.setdefault(norm_email(email), cid)
        if norm_phone(phone):
            self.by_phone.setdefault(norm_phone(phone), []).append(cid)
        if norm_name(name):
            self.by_name.setdefault(norm_name(name), cid)
        for u in PROFILE_LINE.findall(notes or ""):
            if norm_url(u):
                self.by_profile.setdefault(norm_url(u), cid)

    def match(self, rec: Record) -> int | None:
        email, phone, profile = norm_email(rec.email), norm_phone(rec.phone), norm_url(rec.profile)
        if email and email in self.by_email:
            return self.by_email[email]
        if profile and profile in self.by_profile:
            return self.by_profile[profile]
        # A shared landline or switchboard is not one person: a phone match
        # also needs the first names to agree.
        for cid in self.by_phone.get(phone or "", []):
            if _first_word(self.names.get(cid)) == _first_word(rec.name):
                return cid
        if not (email or phone or profile):
            return self.by_name.get(norm_name(rec.name) or "")
        return None


def notes_for(rec: Record, source: str, today: date) -> str:
    lines = [f"source: {source} (imported {today.isoformat()})"]
    if norm_url(rec.profile):
        lines.append(f"profile: {norm_url(rec.profile)}")
    if norm_url(rec.website):
        lines.append(f"website: {norm_url(rec.website)}")
    role = " at ".join(x for x in (rec.title, rec.company) if x)
    if role:
        lines.append(f"role: {role}")
    return "\n".join(lines)


def run(store, records: list[Record], source: str, apply: bool, today: date | None = None,
        new_names: list[str] | None = None) -> dict:
    """new_names, when given, collects the name of every contact counted as new."""
    today = today or date.today()
    existing = {int(c["id"]): dict(c) for c in store.contacts(limit=ALL_CONTACTS)}
    index = Index()
    for cid, c in existing.items():
        index.add(cid, c.get("name"), c.get("email"), c.get("phone"), c.get("notes"))
    counts = {"read": len(records), "new": 0, "duplicate": 0, "filled": 0,
              "skipped_no_name": 0, "skipped_invalid": 0}
    pending = -1
    for rec in records:
        if not rec.name:
            counts["skipped_no_name"] += 1
            continue
        email = norm_email(rec.email)
        cid = index.match(rec)
        if cid is not None:
            counts["duplicate"] += 1
            current = existing.get(cid)
            if current is None:
                continue
            fill = {}
            if email and not current.get("email"):
                fill["email"] = email
            if rec.phone and not current.get("phone"):
                fill["phone"] = rec.phone
            if not current.get("notes"):
                fill["notes"] = notes_for(rec, source, today)
            if fill:
                try:
                    merged = crm.clean_contact({**current, **fill})
                except crm.CrmError:
                    continue
                counts["filled"] += 1
                if apply and cid > 0:
                    store.update_contact(cid, merged)
                current.update(merged)
                index.add(cid, email=merged.get("email"), phone=merged.get("phone"), notes=merged.get("notes"))
            continue
        try:
            fields = crm.clean_contact({"name": rec.name, "email": email, "phone": rec.phone,
                                        "notes": notes_for(rec, source, today)})
        except crm.CrmError:
            counts["skipped_invalid"] += 1
            continue
        if apply:
            cid = store.add_contact(fields)
        else:
            cid, pending = pending, pending - 1
        existing[cid] = {"id": cid, **fields}
        index.add(cid, fields["name"], fields["email"], fields["phone"], fields["notes"])
        counts["new"] += 1
        if new_names is not None:
            new_names.append(fields["name"])
    return counts


def report(fmt: str, path: Path, counts: dict, apply: bool) -> str:
    skipped = counts["skipped_no_name"] + counts["skipped_invalid"]
    verb = "added" if apply else "would add"
    lines = [
        f"{fmt}: {counts['read']} rows read from {path.name}",
        f"  new:       {counts['new']} ({verb})",
        f"  duplicate: {counts['duplicate']} (already in the CRM or earlier in this file; "
        f"{counts['filled']} {'had' if apply else 'would have'} an empty email, phone or notes field filled)",
        f"  skipped:   {skipped} (no name: {counts['skipped_no_name']}, invalid: {counts['skipped_invalid']})",
    ]
    lines.append("Imported." if apply else "Dry run: nothing written. Re-run with --apply to import.")
    return "\n".join(lines)


def main(argv: list[str] | None = None, store=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--format", choices=("auto", *FORMATS), default="auto")
    ap.add_argument("--apply", action="store_true", help="write to the CRM (default: dry run)")
    args = ap.parse_args(argv)
    try:
        fmt, records = load(args.file, args.format)
    except (ExportFileError, OSError) as e:
        print(f"crm_import: {e}", file=sys.stderr)
        return 2
    store = store or crm.Store()
    try:
        counts = run(store, records, fmt, args.apply)
    except crm.CrmUnavailable as e:
        print(f"crm_import: the CRM is unavailable ({e.reason}). {crm.unavailable_hint(e.reason)}", file=sys.stderr)
        return 3
    print(report(fmt, args.file, counts, args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
