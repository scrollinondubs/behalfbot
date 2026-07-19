"""Unit tests for chassis/scripts/gmail-attachment.py.

Run from repo root:
  python3 -m pytest chassis/scripts/tests/test_gmail_attachment.py -v

No live mailbox, no Vaultwarden, no network. Every test builds MIME messages
in memory or points the credential resolver at a temp directory, so this suite
runs identically in CI and on a laptop with no chassis install at all.

Ported from scrollinondubs/new-jaxity#314 (38 tests) plus coverage for the
chassis-side credential resolution, which is the part that was rewritten.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

# The module has a hyphen in its name, so it cannot be imported normally.
# Same importlib shim test_check_second_brain_backend.py uses for the other
# chassis/scripts helper under test.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "gmail-attachment.py"
_spec = importlib.util.spec_from_file_location("gmail_attachment", _MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["gmail_attachment"] = mod
_spec.loader.exec_module(mod)


class TestSanitizeFilename(unittest.TestCase):
    def test_plain_name_survives(self):
        self.assertEqual(mod.sanitize_filename("Proposta_Alma.pdf"), "Proposta_Alma.pdf")

    def test_posix_traversal_stripped(self):
        self.assertEqual(
            mod.sanitize_filename("../../.ssh/authorized_keys"), "authorized_keys"
        )

    def test_windows_traversal_stripped(self):
        self.assertEqual(mod.sanitize_filename(r"..\..\windows\evil.exe"), "evil.exe")

    def test_absolute_path_stripped(self):
        self.assertEqual(mod.sanitize_filename("/etc/passwd"), "passwd")

    def test_dot_dot_alone_falls_back(self):
        self.assertEqual(mod.sanitize_filename(".."), "attachment.bin")

    def test_empty_and_none_fall_back(self):
        self.assertEqual(mod.sanitize_filename(""), "attachment.bin")
        self.assertEqual(mod.sanitize_filename(None), "attachment.bin")

    def test_null_byte_removed(self):
        self.assertEqual(mod.sanitize_filename("evil\x00.pdf"), "evil.pdf")

    def test_leading_dot_not_hidden_file(self):
        self.assertEqual(mod.sanitize_filename(".bashrc"), "bashrc")

    def test_shell_metacharacters_replaced(self):
        self.assertEqual(mod.sanitize_filename("a;rm -rf $HOME`.pdf"), "a_rm -rf _HOME_.pdf")

    def test_long_name_truncated_keeping_extension(self):
        name = mod.sanitize_filename("x" * 500 + ".pdf")
        self.assertLessEqual(len(name), mod._MAX_FILENAME_LEN)
        self.assertTrue(name.endswith(".pdf"))

    def test_encoded_word_filename_decoded(self):
        # =?UTF-8?B?UmVsYXTDs3Jpby5wZGY=?= is "Relatório.pdf"
        got = mod.sanitize_filename("=?UTF-8?B?UmVsYXTDs3Jpby5wZGY=?=")
        # The accented char is not in the allowlist, so it becomes "_", but
        # the encoded-word wrapper must be gone.
        self.assertNotIn("=?", got)
        self.assertTrue(got.endswith(".pdf"))


class TestDecodeEncodedWord(unittest.TestCase):
    def test_base64_utf8(self):
        self.assertEqual(
            mod.decode_encoded_word("=?UTF-8?B?UmVsYXTDs3Jpby5wZGY=?="), "Relatório.pdf"
        )

    def test_quoted_printable_word(self):
        self.assertEqual(
            mod.decode_encoded_word("=?utf-8?Q?Reabilita=C3=A7=C3=A3o?="), "Reabilitação"
        )

    def test_plain_ascii_passthrough(self):
        self.assertEqual(mod.decode_encoded_word("Plain Subject"), "Plain Subject")

    def test_none_returns_empty(self):
        self.assertEqual(mod.decode_encoded_word(None), "")

    def test_malformed_does_not_raise(self):
        self.assertIsInstance(mod.decode_encoded_word("=?UTF-8?B?!!!not-base64!!!?="), str)


class TestSafeJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_child_allowed(self):
        self.assertEqual(mod.safe_join(self.base, "report.pdf"), self.base / "report.pdf")

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            mod.safe_join(self.base, "../escaped.pdf")

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            mod.safe_join(self.base, "/etc/passwd")

    def test_symlink_out_of_tree_rejected(self):
        outside = Path(self._tmp.name).parent / "outside-target"
        outside.mkdir(exist_ok=True)
        link = self.base / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            mod.safe_join(self.base, "link/evil.pdf")
        outside.rmdir()

    def test_sanitized_traversal_is_safe_end_to_end(self):
        name = mod.sanitize_filename("../../.ssh/authorized_keys")
        dest = mod.safe_join(self.base, name)
        self.assertEqual(dest.parent, self.base)


def _pdf_bytes(body: bytes = b"acceptance") -> bytes:
    return b"%PDF-1.4\n" + body + b"\n%%EOF\n"


def _make_forwarded_message() -> EmailMessage:
    """A forward whose PDF lives inside the message/rfc822 part.

    The attachment is not a child of the outer message, so a walker that stops
    at the first level reports zero attachments.
    """
    inner = EmailMessage()
    inner["Subject"] = "Proposta Alma - Fasurb Lda"
    inner["From"] = "orcamentos@example.pt"
    inner.set_content("Segue em anexo a proposta.")
    inner.add_attachment(
        _pdf_bytes(),
        maintype="application",
        subtype="pdf",
        filename="415_01_26_PropostaAlma.pdf",
    )

    outer = EmailMessage()
    outer["Subject"] = "Fwd: Proposta Alma - Fasurb Lda"
    outer.set_content("FYI")
    outer.add_attachment(inner, filename="original.eml")
    return outer


class TestMimeWalking(unittest.TestCase):
    def test_flat_attachment_found(self):
        msg = EmailMessage()
        msg.set_content("hello")
        msg.add_attachment(b"data", maintype="application", subtype="pdf", filename="a.pdf")
        found = mod.collect_attachments(msg)
        self.assertEqual([a.filename for a in found], ["a.pdf"])
        self.assertFalse(found[0].nested)

    def test_nested_rfc822_attachment_found(self):
        found = mod.collect_attachments(_make_forwarded_message())
        by_name = {a.filename: a for a in found}
        self.assertIn("415_01_26_PropostaAlma.pdf", by_name)
        self.assertTrue(
            by_name["415_01_26_PropostaAlma.pdf"].nested,
            "attachment inside a forward must be flagged nested",
        )

    def test_attached_eml_container_also_reported(self):
        # The container and its contents are both real attachments. Found by
        # scanning a real mailbox during new-jaxity#314: a covering note with
        # seven .eml files whose own contents are body text only, so recursing
        # without yielding the container reported zero attachments.
        found = mod.collect_attachments(_make_forwarded_message())
        names = [a.filename for a in found]
        self.assertIn("original.eml", names)
        self.assertLess(
            names.index("original.eml"),
            names.index("415_01_26_PropostaAlma.pdf"),
            "container should be listed before its contents",
        )

    def test_attached_eml_payload_is_the_serialised_message(self):
        found = mod.collect_attachments(_make_forwarded_message())
        eml = next(a for a in found if a.filename == "original.eml")
        data = mod.decode_payload(eml)
        self.assertIn(b"Proposta Alma - Fasurb Lda", data)
        self.assertIn(b"415_01_26_PropostaAlma.pdf", data)

    def test_bounce_style_rfc822_without_disposition_not_an_attachment(self):
        # Delivery-status bounces embed the original message inline, with no
        # Content-Disposition. That is not a file the sender attached.
        import email as _email

        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/report; boundary=B\r\n"
            "\r\n"
            "--B\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "Delivery failed.\r\n"
            "--B\r\n"
            "Content-Type: message/rfc822\r\n"
            "\r\n"
            "Subject: original\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "the body\r\n"
            "--B--\r\n"
        )
        self.assertEqual(mod.collect_attachments(_email.message_from_string(raw)), [])

    def test_nested_payload_decodes_to_original_bytes(self):
        found = mod.collect_attachments(_make_forwarded_message())
        pdf = next(a for a in found if a.filename.endswith(".pdf"))
        self.assertEqual(mod.decode_payload(pdf), _pdf_bytes())

    def test_body_parts_are_not_attachments(self):
        msg = EmailMessage()
        msg.set_content("plain body")
        msg.add_alternative("<p>html body</p>", subtype="html")
        self.assertEqual(mod.collect_attachments(msg), [])

    def test_inline_cid_image_excluded_by_default(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_related(
            b"\x89PNG\r\n",
            maintype="image",
            subtype="png",
            cid="<logo@example>",
            filename="logo.png",
            disposition="inline",
        )
        self.assertEqual(mod.collect_attachments(msg), [])

    def test_inline_cid_image_included_on_request(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_related(
            b"\x89PNG\r\n",
            maintype="image",
            subtype="png",
            cid="<logo@example>",
            filename="logo.png",
            disposition="inline",
        )
        found = mod.collect_attachments(msg, include_inline=True)
        self.assertEqual([a.filename for a in found], ["logo.png"])

    def test_double_nested_forward(self):
        inner = _make_forwarded_message()
        outer = EmailMessage()
        outer["Subject"] = "Fwd: Fwd: Proposta"
        outer.set_content("passing this along")
        outer.add_attachment(inner)
        found = mod.collect_attachments(outer)
        self.assertIn("415_01_26_PropostaAlma.pdf", [a.filename for a in found])

    def test_attachment_without_filename_gets_positional_fallback(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(b"data", maintype="application", subtype="octet-stream")
        found = mod.collect_attachments(msg)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].filename.startswith("part-"))


class TestTransferEncodings(unittest.TestCase):
    def test_base64_roundtrip(self):
        payload = bytes(range(256))
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(payload, maintype="application", subtype="pdf", filename="b.pdf")
        found = mod.collect_attachments(msg)
        self.assertEqual(mod.decode_payload(found[0]), payload)

    def test_quoted_printable_roundtrip(self):
        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=BOUND\r\n"
            "\r\n"
            "--BOUND\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "body\r\n"
            "--BOUND\r\n"
            'Content-Type: text/plain; name="notes.txt"\r\n'
            'Content-Disposition: attachment; filename="notes.txt"\r\n'
            "Content-Transfer-Encoding: quoted-printable\r\n"
            "\r\n"
            "Reabilita=C3=A7=C3=A3o de telhado\r\n"
            "--BOUND--\r\n"
        )
        import email as _email

        msg = _email.message_from_string(raw)
        found = mod.collect_attachments(msg)
        self.assertEqual([a.filename for a in found], ["notes.txt"])
        self.assertEqual(
            mod.decode_payload(found[0]).decode("utf-8").strip(),
            "Reabilitação de telhado",
        )


class TestSizeCap(unittest.TestCase):
    def test_over_cap_raises(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(b"x" * 2048, maintype="application", subtype="pdf", filename="big.pdf")
        att = mod.collect_attachments(msg)[0]
        with self.assertRaises(ValueError) as ctx:
            mod.decode_payload(att, max_bytes=1024)
        self.assertIn("exceeds", str(ctx.exception))

    def test_under_cap_passes(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(b"x" * 100, maintype="application", subtype="pdf", filename="ok.pdf")
        att = mod.collect_attachments(msg)[0]
        self.assertEqual(len(mod.decode_payload(att, max_bytes=1024)), 100)


class TestWriteAttachment(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_traversal_filename_lands_inside_target(self):
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(
            b"pwned",
            maintype="application",
            subtype="octet-stream",
            filename="../../.ssh/authorized_keys",
        )
        att = mod.collect_attachments(msg)[0]
        dest, size = mod.write_attachment(att, self.base)
        self.assertEqual(dest.parent, self.base)
        self.assertEqual(dest.name, "authorized_keys")
        self.assertEqual(size, 5)

    def test_collision_gets_suffix_not_overwrite(self):
        (self.base / "a.pdf").write_bytes(b"original")
        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(b"new", maintype="application", subtype="pdf", filename="a.pdf")
        att = mod.collect_attachments(msg)[0]
        dest, _ = mod.write_attachment(att, self.base)
        self.assertEqual(dest.name, "a-1.pdf")
        self.assertEqual((self.base / "a.pdf").read_bytes(), b"original")


# ---------------------------------------------------------------------------
# Chassis-side credential resolution. This is the part that replaced the
# new-jaxity Vaultwarden read, so it carries the new coverage.
# ---------------------------------------------------------------------------


class _CredentialTestCase(unittest.TestCase):
    """Points CHASSIS_HOME at a temp dir so no real install is consulted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self._env = mock.patch.dict(
            "os.environ",
            {"CHASSIS_HOME": str(self.home), "CUSTOMER_HOME": str(self.home)},
            clear=False,
        )
        self._env.start()
        # patch.dict restores on stop(), so popping the two vars under test
        # here is safe even if the developer running this has them set.
        import os as _os

        _os.environ.pop(mod.ENV_USER, None)
        _os.environ.pop(mod.ENV_PASSWORD, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write_env_file(self, body: str, name: str = ".env") -> None:
        (self.home / name).write_text(body, encoding="utf-8")


class TestCustomerHome(_CredentialTestCase):
    def test_chassis_home_wins(self):
        self.assertEqual(mod.customer_home(), self.home)

    def test_customer_home_used_when_chassis_home_absent(self):
        import os as _os

        _os.environ.pop("CHASSIS_HOME")
        self.assertEqual(mod.customer_home(), self.home)


class TestEnvFileParsing(_CredentialTestCase):
    def test_plain_key_value(self):
        self.write_env_file("GOOGLE_AGENT_EMAIL=agent@example.com\n")
        self.assertEqual(mod.resolve_credential(mod.ENV_USER), "agent@example.com")

    def test_comments_and_blanks_ignored(self):
        self.write_env_file(
            "# hydrated by hydrate-env-from-vw.sh\n"
            "\n"
            "GOOGLE_AGENT_APP_PASSWORD=abcd efgh ijkl mnop\n"
        )
        self.assertEqual(
            mod.resolve_credential(mod.ENV_PASSWORD), "abcd efgh ijkl mnop"
        )

    def test_quoted_value_unwrapped(self):
        self.write_env_file('GOOGLE_AGENT_EMAIL="agent@example.com"\n')
        self.assertEqual(mod.resolve_credential(mod.ENV_USER), "agent@example.com")

    def test_value_containing_equals_survives(self):
        # Base64-ish secrets end in "=" padding; splitting on every "=" would
        # truncate them.
        self.write_env_file("GOOGLE_AGENT_APP_PASSWORD=abc=def==\n")
        self.assertEqual(mod.resolve_credential(mod.ENV_PASSWORD), "abc=def==")

    def test_process_env_beats_disk(self):
        import os as _os

        self.write_env_file("GOOGLE_AGENT_EMAIL=stale@example.com\n")
        _os.environ[mod.ENV_USER] = "fresh@example.com"
        self.assertEqual(mod.resolve_credential(mod.ENV_USER), "fresh@example.com")

    def test_env_baked_used_when_env_absent(self):
        # LaunchDaemon-style contexts read .env.baked - same fallback the DB
        # helper uses.
        self.write_env_file("GOOGLE_AGENT_EMAIL=baked@example.com\n", name=".env.baked")
        self.assertEqual(mod.resolve_credential(mod.ENV_USER), "baked@example.com")

    def test_missing_file_returns_empty_not_raises(self):
        self.assertEqual(mod.resolve_credential(mod.ENV_USER), "")


class TestCredentialStatus(_CredentialTestCase):
    def test_neither_set_is_skip(self):
        status, _ = mod.credential_status()
        self.assertEqual(status, "SKIP")

    def test_both_set_is_pass(self):
        self.write_env_file(
            "GOOGLE_AGENT_EMAIL=agent@example.com\nGOOGLE_AGENT_APP_PASSWORD=secret\n"
        )
        status, message = mod.credential_status()
        self.assertEqual(status, "PASS")
        self.assertIn("agent@example.com", message)

    def test_half_configured_is_fail(self):
        # The failure this check exists for: hydration pulled one field and not
        # the other, which looks configured but cannot log in.
        self.write_env_file("GOOGLE_AGENT_EMAIL=agent@example.com\n")
        status, message = mod.credential_status()
        self.assertEqual(status, "FAIL")
        self.assertIn(mod.ENV_PASSWORD, message)

    def test_password_without_user_is_fail(self):
        self.write_env_file("GOOGLE_AGENT_APP_PASSWORD=secret\n")
        status, message = mod.credential_status()
        self.assertEqual(status, "FAIL")
        self.assertIn(mod.ENV_USER, message)

    def test_status_message_never_contains_the_password(self):
        self.write_env_file(
            "GOOGLE_AGENT_EMAIL=agent@example.com\n"
            "GOOGLE_AGENT_APP_PASSWORD=hunter2secretvalue\n"
        )
        _, message = mod.credential_status()
        self.assertNotIn("hunter2secretvalue", message)


class TestRequireCredentials(_CredentialTestCase):
    def test_returns_pair_when_present(self):
        self.write_env_file(
            "GOOGLE_AGENT_EMAIL=agent@example.com\nGOOGLE_AGENT_APP_PASSWORD=secret\n"
        )
        self.assertEqual(
            mod.require_credentials(), ("agent@example.com", "secret")
        )

    def test_missing_raises_naming_the_rehydration_step(self):
        with self.assertRaises(RuntimeError) as ctx:
            mod.require_credentials()
        message = str(ctx.exception)
        self.assertIn(mod.ENV_USER, message)
        self.assertIn("hydrate-env-from-vw.sh", message)


class TestCheckCommand(_CredentialTestCase):
    def test_emits_one_pipe_delimited_line_and_exits_zero(self):
        # smoke-test.sh parses status="${out%%|*}" from the last stdout line
        # and treats an unparseable line as a FAIL.
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mod.main(["check"])
        self.assertEqual(code, 0)
        lines = buffer.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn(lines[0].split("|", 1)[0], {"PASS", "FAIL", "SKIP"})

    def test_check_makes_no_network_call(self):
        # A boot-time smoke test must not attempt an IMAP login: repeated
        # failed logins get the Google account rate-limited and eventually
        # flagged.
        import io
        import contextlib

        with mock.patch.object(mod.imaplib, "IMAP4_SSL") as imap:
            with contextlib.redirect_stdout(io.StringIO()):
                mod.main(["check"])
        imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
