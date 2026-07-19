#!/usr/bin/env python3
"""test_tokens.py - the approval token cannot collide with `approve N`.

The bug this locks down is the one the design note called out as easy to miss
and expensive to get wrong: `chassis/skills/pacman.md` matched approvals
against a 14-digit SiYuan block ID, which a Notion UUID and an Obsidian path
both fail. Moving the queue to Postgres does not fix that on its own, and a
queue-migration test would never have caught it, so it gets its own file.

The collision constraint is the sharp one. The outreach flow uses `approve 1 3 5`
to approve drafts by list position. If a Pacman token could be read as a number,
an approval would route into the wrong handler. That is prevented structurally
rather than probabilistically - the token alphabet contains no digits - and this
file proves it in both directions rather than asserting it once.

Run:
    python3 -m pytest chassis/pacman/tests/test_tokens.py -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from chassis.pacman.tokens import (  # noqa: E402
    APPROVAL_RE,
    TOKEN_ALPHABET,
    TOKEN_LENGTH,
    is_token,
    new_token,
    parse_approval,
)

# The outreach flow's pattern, reproduced here rather than imported: it lives in
# the customer repo (skills/triggers/outreach-approval.md), and the point of
# this file is to prove the two grammars are disjoint even though neither side
# can see the other.
OUTREACH_APPROVE_RE = re.compile(r"^approve\s+(\d+(?:\s+\d+)*)$", re.IGNORECASE)

SAMPLE_SIZE = 20_000


class TestTokenShape(unittest.TestCase):
    def test_alphabet_has_no_digits(self):
        """The structural guarantee everything else rests on."""
        self.assertFalse(any(c.isdigit() for c in TOKEN_ALPHABET))

    def test_alphabet_has_no_vowels(self):
        """No vowels and no y means a token cannot spell an English word."""
        for vowel in "aeiouy":
            self.assertNotIn(vowel, TOKEN_ALPHABET)

    def test_alphabet_excludes_visually_ambiguous_characters(self):
        """Sean retypes these on a phone. i/l/1 and o/0 must not appear."""
        for ambiguous in "ilo":
            self.assertNotIn(ambiguous, TOKEN_ALPHABET)

    def test_generated_tokens_are_well_formed(self):
        for _ in range(1000):
            token = new_token()
            self.assertEqual(len(token), TOKEN_LENGTH)
            self.assertTrue(is_token(token))
            self.assertTrue(set(token) <= set(TOKEN_ALPHABET))


class TestNoCollisionWithNumericApprove(unittest.TestCase):
    """Both directions. Either one alone would leave the other unproven."""

    def test_no_generated_token_is_numeric(self):
        for _ in range(SAMPLE_SIZE):
            token = new_token()
            self.assertFalse(token.isdigit())
            with self.assertRaises(ValueError):
                int(token)

    def test_no_generated_token_matches_the_outreach_pattern(self):
        for _ in range(SAMPLE_SIZE):
            self.assertIsNone(OUTREACH_APPROVE_RE.match(f"approve {new_token()}"))

    def test_numeric_approvals_are_not_parsed_as_pacman_approvals(self):
        """`approve 1 3 5` belongs to the outreach handler and must stay there."""
        for message in ("approve 1", "approve 3 5", "approve 1 3 5", "approve 42", "approve 007"):
            self.assertIsNone(parse_approval(message), message)
            self.assertIsNotNone(OUTREACH_APPROVE_RE.match(message), message)

    def test_the_two_grammars_are_disjoint_over_a_shared_input_set(self):
        """No single message may be accepted by both handlers."""
        messages = [f"approve {new_token()}" for _ in range(2000)]
        messages += ["approve 1", "approve 12", "approve 1 2 3"]
        for message in messages:
            pacman = parse_approval(message) is not None
            outreach = OUTREACH_APPROVE_RE.match(message) is not None
            self.assertFalse(pacman and outreach, f"both handlers claim: {message}")


class TestNoWordCollision(unittest.TestCase):
    def test_common_trailing_words_are_not_tokens(self):
        """`approve later` must not be read as a token and silently approved."""
        for word in ("all", "later", "yes", "now", "please", "this", "rhythm", "flight"):
            self.assertFalse(is_token(word), word)

    def test_generated_tokens_contain_no_vowel(self):
        for _ in range(5000):
            self.assertFalse(set(new_token()) & set("aeiouy"))


class TestApprovalParsing(unittest.TestCase):
    def test_parses_action_and_token(self):
        parsed = parse_approval("approve qhtnbz")
        self.assertEqual(parsed["action"], "approve")
        self.assertEqual(parsed["id"], "qhtnbz")
        self.assertIsNone(parsed["trailer"])
        self.assertEqual(parsed["legacy"], "false")

    def test_parses_reject_and_defer(self):
        for action in ("reject", "defer"):
            parsed = parse_approval(f"{action} qhtnbz")
            self.assertEqual(parsed["action"], action)

    def test_captures_the_caveat_trailer(self):
        parsed = parse_approval("approve qhtnbz but scope it to the gather script only")
        self.assertEqual(parsed["id"], "qhtnbz")
        self.assertEqual(parsed["trailer"], "but scope it to the gather script only")

    def test_case_insensitive_action(self):
        self.assertEqual(parse_approval("APPROVE qhtnbz")["action"], "approve")

    def test_legacy_block_id_still_parses_and_is_flagged(self):
        """In-flight proposals posted before the cutover must remain approvable.

        Flagged rather than silently accepted so the handler knows to resolve it
        via proposal_doc_id instead of by token.
        """
        parsed = parse_approval("approve 20260718120000-abc1234")
        self.assertEqual(parsed["id"], "20260718120000-abc1234")
        self.assertEqual(parsed["legacy"], "true")
        self.assertFalse(is_token("20260718120000-abc1234"))

    def test_rejects_a_notion_uuid_and_an_obsidian_path(self):
        """Neither should ever have been an approval handle. Both stay rejected.

        The fix is not "widen the regex until UUIDs pass" - it is that the
        approval handle stopped being a document id at all.
        """
        self.assertIsNone(parse_approval("approve 1f2e3d4c5b6a7988990011223344556677"))
        self.assertIsNone(parse_approval("approve To Investigate/2026-07-19-thing.md"))

    def test_rejects_wrong_length_tokens(self):
        self.assertIsNone(parse_approval("approve qhtnb"))
        self.assertIsNone(parse_approval("approve qhtnbzz"))

    def test_rejects_unrelated_messages(self):
        for message in ("Pacman https://example.com", "approve", "hello qhtnbz", ""):
            self.assertIsNone(parse_approval(message), message)

    def test_module_regex_and_parse_helper_agree(self):
        """Callers copying APPROVAL_RE must get the same answer as parse_approval."""
        for message in ("approve qhtnbz", "approve 1", "defer 20260718120000-abc1234", "nope"):
            self.assertEqual(
                APPROVAL_RE.match(message) is not None,
                parse_approval(message) is not None,
                message,
            )


class TestSkillDocumentsTheSamePattern(unittest.TestCase):
    """The skill is prose the model follows, so a drifted regex there is a live bug.

    Config promising more than code delivers is the recurring failure in this
    codebase; this asserts the documented pattern is the implemented one.
    """

    def test_skill_quotes_the_current_alphabet_and_length(self):
        skill = (Path(__file__).resolve().parents[2] / "skills" / "pacman.md").read_text(encoding="utf-8")
        self.assertIn(f"[{TOKEN_ALPHABET}]{{{TOKEN_LENGTH}}}", skill)

    def test_skill_no_longer_names_the_siyuan_queue_block(self):
        skill = (Path(__file__).resolve().parents[2] / "skills" / "pacman.md").read_text(encoding="utf-8")
        # One surviving mention is allowed: the note pointing at the one-time
        # backfill script, which genuinely still reads it.
        self.assertNotIn("mcp__siyuan__delete_block", skill)
        self.assertNotIn("mcp__siyuan__create_doc", skill)
        self.assertNotIn("mcp__siyuan__sql_query", skill)


if __name__ == "__main__":
    unittest.main()
