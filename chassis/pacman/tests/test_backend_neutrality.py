#!/usr/bin/env python3
"""test_backend_neutrality.py - Pacman behaves identically on all three backends.

Pacman used to have a backend branch, and it was invisible: the queue lived in
SiYuan blocks, so on Obsidian, Notion, or any install running
`second_brain.mode: adapter` the drain reached for `mcp__siyuan__*`, found
nothing registered, and reported a clean run over an empty queue.

The fix removed the branch rather than adding cases to it. That makes the
strongest available assertion a negative one: no Pacman code path, prompt, or
skill instruction may name a backend-specific tool or a SiYuan-shaped
identifier. If one reappears, the branch is back and one of the three backends
is silently broken again.

These are string assertions over shipped prose, which is unusual, but the
prompt and skill ARE the implementation for a model-driven pipeline - a
`mcp__siyuan__delete_block` in the drain prompt is executable code, and this
file is the only thing that can fail when it drifts.

Run:
    python3 -m pytest chassis/pacman/tests/test_backend_neutrality.py -v
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

SKILL = REPO_ROOT / "chassis" / "skills" / "pacman.md"
DRAIN_PROMPT = REPO_ROOT / "chassis" / "scheduled-tasks" / "pacman-drain-prompt.md"
PACMAN_SH = REPO_ROOT / "chassis" / "scripts" / "pacman.sh"
GATHER_SH = REPO_ROOT / "chassis" / "scripts" / "gather-pacman-queue.sh"
QUEUE_ADD = REPO_ROOT / "chassis" / "scripts" / "pacman-queue-add.py"
REACTIONS = REPO_ROOT / "chassis" / "scripts" / "pacman-process-reactions.py"
QUEUE_CLI = REPO_ROOT / "chassis" / "scripts" / "pacman-queue.py"

# The one file allowed to be SiYuan-specific: it exists to read the old SiYuan
# queue once and never runs again.
MIGRATION_SCRIPT = REPO_ROOT / "chassis" / "scripts" / "pacman-migrate-siyuan-queue.py"

RUNTIME_FILES = [SKILL, DRAIN_PROMPT, PACMAN_SH, GATHER_SH, QUEUE_ADD, REACTIONS, QUEUE_CLI]


class TestNoSiyuanToolCallsRemain(unittest.TestCase):
    def test_no_runtime_file_calls_a_siyuan_mcp_tool(self):
        for path in RUNTIME_FILES:
            text = path.read_text(encoding="utf-8")
            for tool in (
                "mcp__siyuan__delete_block",
                "mcp__siyuan__create_doc",
                "mcp__siyuan__append_block",
                "mcp__siyuan__sql_query",
            ):
                self.assertNotIn(tool, text, f"{path.name} still calls {tool}")

    def test_no_runtime_file_reads_the_siyuan_queue_block_id(self):
        """That variable is the SiYuan coupling in env-var form."""
        for path in RUNTIME_FILES:
            self.assertNotIn(
                "${PACMAN_SIYUAN_QUEUE_BLOCK_ID}",
                path.read_text(encoding="utf-8"),
                f"{path.name} still expands PACMAN_SIYUAN_QUEUE_BLOCK_ID",
            )

    def test_pacman_sh_no_longer_requires_the_siyuan_block_vars(self):
        """It used to hard-fail without them, which broke non-SiYuan installs at launch."""
        text = PACMAN_SH.read_text(encoding="utf-8")
        self.assertNotIn(': "${PACMAN_SIYUAN_QUEUE_BLOCK_ID:?', text)
        self.assertNotIn(': "${PACMAN_SIYUAN_DROPPED_BLOCK_ID:?', text)

    def test_the_backfill_script_is_the_only_siyuan_reader(self):
        """Deliberately exempt - it reads the old queue once, then never runs."""
        self.assertIn("PACMAN_SIYUAN_QUEUE_BLOCK_ID", MIGRATION_SCRIPT.read_text(encoding="utf-8"))


class TestWritesGoThroughTheAdapter(unittest.TestCase):
    def test_drain_prompt_creates_proposals_via_the_adapter(self):
        text = DRAIN_PROMPT.read_text(encoding="utf-8")
        self.assertIn("mcp__secondbrain__create_doc", text)
        self.assertIn("mcp__secondbrain__append_to_doc", text)

    def test_skill_creates_proposals_via_the_adapter(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("mcp__secondbrain__create_doc", text)
        self.assertIn("mcp__secondbrain__append_to_doc", text)

    def test_deeplinks_are_taken_from_the_adapter_not_constructed(self):
        """The URL scheme differs per backend; a hand-built one is wrong on two of three."""
        for path in (SKILL, DRAIN_PROMPT):
            text = path.read_text(encoding="utf-8")
            self.assertIn("deeplink", text, path.name)
        self.assertNotIn("Read the expanded proposal: [SiYuan sub-doc](siyuan://", SKILL.read_text(encoding="utf-8"))

    def test_proposal_doc_id_is_treated_as_opaque(self):
        """It is a block id, a UUID, or a path. Core code must never parse it."""
        self.assertIn("opaque", SKILL.read_text(encoding="utf-8").lower())


class TestQueueReadsAndWritesGoThroughPostgres(unittest.TestCase):
    def test_drain_prompt_claims_through_the_queue_cli(self):
        text = DRAIN_PROMPT.read_text(encoding="utf-8")
        self.assertIn("pacman-queue.py", text)
        self.assertIn("claim --limit", text)

    def test_drain_prompt_completes_rows_rather_than_deleting_blocks(self):
        text = DRAIN_PROMPT.read_text(encoding="utf-8")
        self.assertIn("complete <token>", text)

    def test_gather_script_delegates_the_count_rather_than_reimplementing_it(self):
        """A second copy of the predicate would drift from the claim path."""
        text = GATHER_SH.read_text(encoding="utf-8")
        self.assertIn("pacman-queue.py", text)
        self.assertNotIn("SELECT COUNT", text)


class TestDrainPromptFailsLoudly(unittest.TestCase):
    def test_it_forbids_reporting_a_clean_drain_on_error(self):
        """PR #78's Stage 2 property: a silent no-op drain is the failure mode."""
        text = DRAIN_PROMPT.read_text(encoding="utf-8")
        self.assertIn("do NOT report a clean drain", text)
        self.assertIn("do NOT improvise", text)

    def test_pacman_sh_checks_the_queue_before_spending_a_claude_invocation(self):
        text = PACMAN_SH.read_text(encoding="utf-8")
        self.assertIn("pacman-queue.py", text)
        self.assertIn("count", text)


class TestQueueCliContract(unittest.TestCase):
    """The drain prompt names these subcommands; they must exist and parse."""

    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(QUEUE_CLI), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin"},
        )

    def test_every_subcommand_the_prompt_uses_exists(self):
        prompt = DRAIN_PROMPT.read_text(encoding="utf-8")
        for subcommand in ("claim", "complete", "release"):
            self.assertIn(f"pacman-queue.py\" {subcommand}", prompt)
            result = self._run(subcommand, "--help")
            self.assertEqual(result.returncode, 0, f"{subcommand} --help failed: {result.stderr}")

    def test_count_subcommand_exists(self):
        self.assertEqual(self._run("count", "--help").returncode, 0)

    def test_pending_subcommand_exists(self):
        self.assertEqual(self._run("pending", "--help").returncode, 0)

    def test_unconfigured_postgres_exits_nonzero_rather_than_printing_count_zero(self):
        """The whole point. An unreachable queue must not look like an empty one."""
        result = self._run("count")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"count": 0', result.stdout)
        self.assertIn("ERROR", result.stderr)

    def test_the_error_names_the_env_var_to_set(self):
        result = self._run("count")
        self.assertIn("CHASSIS_PG_DSN", result.stderr)

    def test_complete_rejects_an_undocumented_verdict(self):
        result = self._run("complete", "qhtnbz", "--verdict", "maybe")
        self.assertNotEqual(result.returncode, 0)


class TestGatherScriptContract(unittest.TestCase):
    def _run(self, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(GATHER_SH)],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", **env},
        )

    def test_hard_pause_short_circuits_before_touching_the_database(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "PACMAN_HARD_PAUSE"
            pause.touch()
            result = self._run({"PACMAN_HARD_PAUSE": str(pause), "CHASSIS_HOME": tmp})
            self.assertEqual(result.returncode, 0)
            self.assertIn('"count": 0', result.stdout)
            self.assertIn("PACMAN_HARD_PAUSE", result.stdout)

    def test_unreachable_database_exits_nonzero(self):
        """The dispatcher records this as gather_failed and alerts, per its own logic."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = self._run({
                "CHASSIS_HOME": tmp,
                "CHASSIS_PG_DSN": "postgresql://u:p@127.0.0.1:1/nope",
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('"count": 0', result.stdout)

    def test_unconfigured_database_exits_nonzero(self):
        """Previously this printed count=0 and exited 0 - a permanently silent queue."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = self._run({"CHASSIS_HOME": tmp})
            self.assertNotEqual(result.returncode, 0)


class TestReactionProcessorReportsFailures(unittest.TestCase):
    def test_a_failed_enqueue_logs_the_url_verbatim(self):
        """Telegram reactions are the intake where a lost URL is unrecoverable."""
        text = REACTIONS.read_text(encoding="utf-8")
        self.assertIn("queue_failed_urls", text)

    def test_it_distinguishes_a_db_outage_from_a_rejected_url(self):
        text = REACTIONS.read_text(encoding="utf-8")
        self.assertIn("db_unavailable", text)

    def test_it_records_the_approval_token_it_got_back(self):
        text = REACTIONS.read_text(encoding="utf-8")
        self.assertIn('"token": token', text)


class TestQueueAddIsDurableBeforeAck(unittest.TestCase):
    def test_it_no_longer_advertises_silent_failure(self):
        text = QUEUE_ADD.read_text(encoding="utf-8")
        self.assertNotIn("Designed to fail silently", text)

    def test_it_documents_a_distinct_exit_code_for_an_unreachable_database(self):
        text = QUEUE_ADD.read_text(encoding="utf-8")
        self.assertIn("2  Postgres unconfigured or unreachable", text)

    def test_an_unreachable_database_exits_two_and_prints_no_token(self):
        result = subprocess.run(
            [sys.executable, str(QUEUE_ADD), "https://example.com/a"],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "CHASSIS_HOME": "/tmp"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
