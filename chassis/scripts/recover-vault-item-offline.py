#!/usr/bin/env python3
"""Read one item's notes out of a Vaultwarden db.sqlite3 backup, offline.

Why this exists
---------------
Vaultwarden keeps no history for Secure Notes. If an item is overwritten, the
only copy of what it held is in a backup, and the backup is a `db.sqlite3` with
everything encrypted client-side. Standing up a throwaway Vaultwarden instance
to read one string is a lot of moving parts to introduce on the day something
has already gone wrong, and it means pointing your `bw` CLI at a different
server mid-incident.

This reads the blob directly. No server, no `bw` config change, no network.

Written 2026-08-19 after a bad edit destroyed the age identity used to decrypt
this install's S3 backups. Recovery took one run against the previous night's
Vaultwarden backup; the alternative path had not been tried and would have been
tried for the first time under pressure.

Usage
-----
    tar -xzf backups/vaultwarden/<date>.tar.gz -C /some/scratch
    python3 recover-vault-item-offline.py \\
        /some/scratch/vaultwarden-<date>/db.sqlite3 \\
        <item-uuid> \\
        [--pattern 'AGE-SECRET-KEY-[A-Z0-9]+' --out ./recovered.key]

With --pattern and --out, only the first match is written, mode 0600, and
nothing sensitive reaches stdout or your shell history. Without them the notes
are printed, which is the right default for a non-secret item and the wrong one
for a key - so prefer --pattern when recovering credentials.

Delete the extracted db.sqlite3 when you are done. It is the whole vault.

Crypto
------
Bitwarden's PBKDF2 path:

    master_key  = PBKDF2-SHA256(password, salt=lowercased email, iterations, 32)
    stretched   = HKDF-Expand(master_key, "enc") || HKDF-Expand(master_key, "mac")
    user symkey = decrypt(users.akey, stretched)      -> 64 bytes: enc || mac
    plaintext   = decrypt(ciphers.notes, user symkey)

EncString type 2 is "2.<iv b64>|<ct b64>|<mac b64>": AES-256-CBC, then
HMAC-SHA256 over iv||ct. The MAC is verified, so a wrong master password fails
loudly instead of returning plausible garbage.

Not handled, deliberately, because this install does not use them and untested
recovery code is worse than none: Argon2id KDF (client_kdf_type != 0),
organisation-owned items, and per-item cipher keys (ciphers.key non-null). Each
one exits with a message naming what it found rather than guessing.

The master password is read from the OS keychain, never passed on the command
line. Override the lookup with VAULT_MASTER_PASSWORD_CMD if your platform or
entry name differs.
"""
import argparse
import base64
import hashlib
import hmac
import os
import re
import shlex
import sqlite3
import subprocess
import sys

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    sys.exit("needs `cryptography` (pip install cryptography)")

DEFAULT_PASSWORD_CMD = (
    'security find-generic-password -a "$USER" -s vaultwarden-master -w'
)


def hkdf_expand(prk: bytes, info: str, length: int = 32) -> bytes:
    """HKDF-Expand, single block. Bitwarden never needs more than 32 bytes here."""
    return hmac.new(prk, info.encode() + b"\x01", hashlib.sha256).digest()[:length]


def decrypt_encstring(s: str, enc_key: bytes, mac_key: bytes, what: str) -> bytes:
    header, _, body = s.partition(".")
    if header != "2":
        sys.exit(f"{what}: EncString type {header!r} is not supported (expected 2)")
    try:
        iv_b64, ct_b64, mac_b64 = body.split("|")
    except ValueError:
        sys.exit(f"{what}: malformed EncString")
    iv, ct, mac = (base64.b64decode(x) for x in (iv_b64, ct_b64, mac_b64))

    if not hmac.compare_digest(hmac.new(mac_key, iv + ct, hashlib.sha256).digest(), mac):
        sys.exit(f"{what}: MAC mismatch - wrong master password, or this is not the blob you think")

    dec = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    padded = dec.update(ct) + dec.finalize()
    return padded[: -padded[-1]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db", help="path to an extracted db.sqlite3")
    ap.add_argument("item_uuid", help="uuid of the item to read")
    ap.add_argument("--pattern", help="regex; write only the first match instead of printing")
    ap.add_argument("--out", help="file to write the match to, mode 0600")
    args = ap.parse_args()

    if bool(args.pattern) != bool(args.out):
        sys.exit("--pattern and --out go together")

    pw_cmd = os.environ.get("VAULT_MASTER_PASSWORD_CMD", DEFAULT_PASSWORD_CMD)
    proc = subprocess.run(["/bin/sh", "-c", pw_cmd], capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        sys.exit(f"could not read the master password via: {shlex.quote(pw_cmd)}")
    password = proc.stdout.strip()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    users = con.execute(
        "SELECT email, client_kdf_type, client_kdf_iter, akey FROM users"
    ).fetchall()
    if len(users) != 1:
        sys.exit(f"expected exactly one user in this vault, found {len(users)}")
    email, kdf_type, kdf_iter, akey = users[0]
    if kdf_type != 0:
        sys.exit(f"client_kdf_type={kdf_type} (Argon2id?); this script only handles PBKDF2")

    row = con.execute(
        'SELECT notes, organization_uuid, "key" FROM ciphers WHERE uuid = ?',
        (args.item_uuid,),
    ).fetchone()
    if row is None:
        sys.exit(f"no cipher with uuid {args.item_uuid} in this backup")
    notes_enc, org_uuid, item_key = row
    if org_uuid:
        sys.exit("item is organisation-owned; needs the org key, not handled here")
    if item_key:
        sys.exit("item has a per-item cipher key; not handled here")
    if not notes_enc:
        sys.exit("that item has no notes")

    master = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), email.lower().encode(), kdf_iter, 32
    )
    symkey = decrypt_encstring(
        akey, hkdf_expand(master, "enc"), hkdf_expand(master, "mac"), "user key"
    )
    notes = decrypt_encstring(notes_enc, symkey[:32], symkey[32:64], "notes").decode()

    if not args.pattern:
        print(notes)
        return

    m = re.search(args.pattern, notes)
    if not m:
        sys.exit(f"recovered {len(notes)} chars of notes, but nothing matched {args.pattern!r}")
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(m.group(0) + "\n")
    print(f"recovered {len(notes)} chars of notes; wrote the match to {args.out} (mode 0600)")


if __name__ == "__main__":
    main()
