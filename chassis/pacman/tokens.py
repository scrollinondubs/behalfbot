"""Pacman approval tokens - the backend-independent replacement for block IDs.

## The problem being solved

`chassis/skills/pacman.md` used to match installer approvals against
`^(approve|reject|defer)\\s+(\\d{14}-\\w{7})...$`. That pattern is a SiYuan
block ID and nothing else. A Notion UUID fails it, an Obsidian vault path
fails it, and moving the queue to Postgres does not fix it on its own - it is
an independently SiYuan-shaped assumption sitting in the approval path, which
is exactly why a queue-migration test would never have caught it.

Once the queue row is the durable record, the approval handle should be the
queue row's own identifier and not a second brain's identifier at all. Nothing
about approving a proposal needs to know where the proposal document was
filed; `proposal_doc_id` already holds that, opaquely, for the one step that
does. So the token is ours, it is stable across every backend, and it is the
same string whether the install runs SiYuan, Obsidian, Notion, or no second
brain.

## Why this alphabet

    bcdfghjkmnpqrstvwxz   19 characters, exactly 6 of them per token

Three constraints drove it, in order of how expensive they are to get wrong:

1. **It must never collide with the numeric `approve N` form.** The outreach
   flow uses `approve 1 3 5` to approve drafts by list position. A Pacman
   token that could be read as a number would route an approval into the wrong
   handler. Excluding all ten digits makes this a structural guarantee rather
   than a probability: `int(token)` cannot succeed on a string containing no
   digits, so no token in this space is ever a valid `approve N` argument.
   This is proved by test, both directions, in tests/test_tokens.py.

2. **It must not accidentally spell a word.** Dropping the vowels (and `y`,
   which acts as one) means a token cannot be an English word - English has no
   six-letter vowel-free words, and near-misses like "rhythm" need the `y`
   this alphabet does not have. That keeps `approve later` and similar from
   ever being read as a token, and keeps generated tokens from being obscene
   or confusing.

3. **Sean approves from his phone.** Six characters, one case, no digits to
   confuse with letters, and no visually ambiguous pairs - the alphabet has no
   `i`/`l`/`1`, no `o`/`0`. A 32-character Notion UUID fails this badly enough
   that the design note called it out by name.

19**6 is 47 million, against a queue that holds tens of live items. Collisions
are handled by retry at insert against the UNIQUE constraint rather than
assumed away.
"""

from __future__ import annotations

import re
import secrets

# No vowels (a, e, i, o, u), no y, no digits. See module docstring for why
# each of those exclusions is load-bearing.
TOKEN_ALPHABET = "bcdfghjkmnpqrstvwxz"
TOKEN_LENGTH = 6

TOKEN_RE = re.compile(rf"^[{TOKEN_ALPHABET}]{{{TOKEN_LENGTH}}}$")

# Legacy SiYuan block ID, still accepted by the approval matcher so proposals
# posted to Discord before the cutover can still be approved. Deprecated: the
# skill stops emitting these the moment this ships, and this alternative can be
# dropped once no pre-cutover proposal is outstanding.
LEGACY_BLOCK_ID_RE = re.compile(r"^\d{14}-\w{7}$")

# The full approval matcher the skill and any Discord handler should use.
# The numeric `approve N` form used by the outreach flow does not match this
# pattern, because neither alternative in the id group admits a bare number.
APPROVAL_RE = re.compile(
    r"^(?P<action>approve|reject|defer)\s+"
    rf"(?P<id>[{TOKEN_ALPHABET}]{{{TOKEN_LENGTH}}}|\d{{14}}-\w{{7}})"
    r"(?:\s+(?P<trailer>.+))?$",
    re.IGNORECASE,
)


def new_token() -> str:
    """Generate one approval token. Uniqueness is enforced by the DB, not here."""
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def is_token(candidate: str) -> bool:
    """True for a well-formed current-format token. False for legacy block IDs."""
    return bool(TOKEN_RE.match(candidate))


def parse_approval(message: str) -> dict[str, str | None] | None:
    """Parse an installer approval message, or return None if it is not one.

    Returns {'action', 'id', 'trailer', 'legacy'}. `legacy` is 'true' when the
    id matched the deprecated SiYuan block-ID form, so callers can log that a
    pre-cutover proposal was approved and know to look it up by
    proposal_doc_id rather than by token.

    Returns None for the numeric `approve N` outreach form, which is the whole
    point - that message belongs to a different handler.
    """
    match = APPROVAL_RE.match(message.strip())
    if not match:
        return None
    identifier = match.group("id")
    return {
        "action": match.group("action").lower(),
        "id": identifier,
        "trailer": match.group("trailer"),
        "legacy": "true" if LEGACY_BLOCK_ID_RE.match(identifier) else "false",
    }
