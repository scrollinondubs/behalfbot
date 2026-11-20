#!/usr/bin/env python3
"""crm_import.py: each export format parses, a dry run writes nothing, a
re-run adds nothing, and existing fields are never changed.

Fixtures under fixtures/crm-import/ are fabricated people with example.*
addresses. No test touches a database: the store is in memory.

Run:
    python3 -m pytest chassis/scripts/tests/test_crm_import.py -v
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import _crm as crm  # noqa: E402
import crm_import as ci  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "crm-import"
TODAY = date(2026, 11, 20)


class MemStore:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: dict[int, dict] = {}
        self.writes = 0
        for r in rows or []:
            self.rows[len(self.rows) + 1] = {"id": len(self.rows) + 1, **crm.clean_contact(r)}

    def contacts(self, q="", limit=1000):
        return [dict(r) for r in list(self.rows.values())[:limit]]

    def add_contact(self, fields):
        self.writes += 1
        cid = max(self.rows, default=0) + 1
        self.rows[cid] = {"id": cid, **fields}
        return cid

    def update_contact(self, contact_id, fields):
        self.writes += 1
        self.rows[contact_id] = {"id": contact_id, **fields}
        return True

    def by_name(self, name):
        return next(r for r in self.rows.values() if r["name"] == name)


def records(name: str, fmt: str = "auto"):
    return ci.load(FIX / name, fmt)


class Detect(unittest.TestCase):
    def test_each_fixture_is_recognised(self) -> None:
        for name, fmt in (("linkedin-Connections.csv", "linkedin"), ("google-contacts.csv", "google"),
                          ("outlook-contacts.csv", "outlook"), ("contacts.vcf", "vcard"),
                          ("facebook-your_friends.json", "facebook"), ("instagram-followers_1.json", "instagram"),
                          ("instagram-following.json", "instagram")):
            with self.subTest(name):
                self.assertEqual(records(name)[0], fmt)

    def test_bytes_in_memory_read_the_same_as_the_file(self) -> None:
        data = (FIX / "linkedin-Connections.csv").read_bytes()
        self.assertEqual(ci.load_bytes("Connections.csv", data), records("linkedin-Connections.csv"))
        with self.assertRaisesRegex(ci.ExportFileError, "whole archive"):
            ci.load_bytes("export.zip", data)

    def test_zip_is_refused_without_opening_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Complete_LinkedInDataExport.zip"
            p.write_bytes(b"PK\x03\x04 not really a zip")
            with self.assertRaisesRegex(ci.ExportFileError, "Connections.csv"):
                ci.load(p)

    def test_unknown_csv_asks_for_format(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_text("a,b,c\n1,2,3\n")
            with self.assertRaisesRegex(ci.ExportFileError, "--format"):
                ci.load(p)


class Parse(unittest.TestCase):
    def test_linkedin_skips_the_notes_preamble(self) -> None:
        _, recs = records("linkedin-Connections.csv")
        self.assertEqual(len(recs), 5)
        a = recs[0]
        self.assertEqual((a.name, a.email, a.company, a.title), ("Alba Quintero", "alba.q@example.com", "Fictional Labs", "Founder"))
        self.assertEqual(recs[1].company, "Made Up, Inc.")
        self.assertIsNone(recs[3].name)

    def test_google_takes_the_first_of_packed_values(self) -> None:
        _, recs = records("google-contacts.csv")
        self.assertEqual((recs[0].name, recs[0].email, recs[0].phone), ("Dario Pellegrini", "dario@example.com", "+351 910 000 001"))
        self.assertEqual(recs[1].email, None)
        self.assertEqual(recs[2].name, "Placeholder Bakery")

    def test_outlook(self) -> None:
        _, recs = records("outlook-contacts.csv")
        self.assertEqual((recs[0].name, recs[0].email, recs[0].phone, recs[0].company),
                         ("Fenna Holt", "FENNA.HOLT@EXAMPLE.COM", "+1 555 0100 003", "Sample Studio"))

    def test_vcard_groups_folding_and_quoted_printable(self) -> None:
        _, recs = records("contacts.vcf")
        self.assertEqual([r.name for r in recs], ["Gael Moreau", "Hedda Ibsen", "János Szép"])
        self.assertEqual((recs[0].email, recs[0].company, recs[0].title), ("gael@example.com", "Invented SARL", "Chef"))
        self.assertEqual(recs[1].email, "hedda@example.org")
        self.assertEqual(recs[2].phone, "+36 20 000 0006")

    def test_facebook_repairs_meta_escaped_utf8(self) -> None:
        _, recs = records("facebook-your_friends.json")
        self.assertEqual([r.name for r in recs], ["Ilse Brandt", "Zoë Kalnina", None, "Cleo Varga"])

    def test_instagram_old_and_new_shapes_give_handles_and_urls(self) -> None:
        _, f1 = records("instagram-followers_1.json")
        _, f2 = records("instagram-following.json")
        self.assertEqual([r.name for r in f1 + f2], ["@jonas.test.handle", "@kira_made_up", "@kira_made_up", "@lior.placeholder"])
        self.assertEqual(f2[0].profile, "https://www.instagram.com/kira_made_up")


class Run(unittest.TestCase):
    def test_dry_run_counts_and_writes_nothing(self) -> None:
        store = MemStore()
        _, recs = records("linkedin-Connections.csv")
        new_names: list[str] = []
        counts = ci.run(store, recs, "linkedin", apply=False, today=TODAY, new_names=new_names)
        self.assertEqual(new_names, ["Alba Quintero", "Bram Oosterveld", "Cleo Varga"])
        self.assertEqual(store.writes, 0)
        self.assertEqual(store.rows, {})
        # Bram appears twice (trailing slash on the second URL): one new, one duplicate.
        self.assertEqual((counts["new"], counts["duplicate"], counts["skipped_no_name"]), (3, 1, 1))

    def test_apply_tags_source_and_profile_then_rerun_adds_nothing(self) -> None:
        store = MemStore()
        _, recs = records("linkedin-Connections.csv")
        ci.run(store, recs, "linkedin", apply=True, today=TODAY)
        self.assertEqual(len(store.rows), 3)
        alba = store.by_name("Alba Quintero")
        self.assertEqual(alba["notes"], "source: linkedin (imported 2026-11-20)\n"
                                        "profile: https://linkedin.com/in/alba-quintero-test\n"
                                        "role: Founder at Fictional Labs")
        before = {k: dict(v) for k, v in store.rows.items()}
        again = ci.run(store, recs, "linkedin", apply=True, today=TODAY)
        self.assertEqual(again["new"], 0)
        self.assertEqual(store.rows, before)

    def test_existing_fields_are_never_overwritten_only_blanks_filled(self) -> None:
        store = MemStore([{"name": "Alba Q.", "email": "ALBA.Q@example.com", "phone": "", "notes": "met at a meetup"},
                          {"name": "Fenna Holt", "email": "", "phone": "+1 (555) 0100-003", "notes": ""}])
        _, recs = records("outlook-contacts.csv")
        counts = ci.run(store, recs, "outlook", apply=True, today=TODAY)
        self.assertEqual((counts["new"], counts["duplicate"]), (0, 2))
        alba = store.rows[1]
        self.assertEqual((alba["name"], alba["email"], alba["notes"]), ("Alba Q.", "ALBA.Q@example.com", "met at a meetup"))
        self.assertEqual(alba["phone"], "+351 910 000 004")
        fenna = store.rows[2]
        self.assertEqual((fenna["phone"], fenna["email"]), ("+1 (555) 0100-003", "fenna.holt@example.com"))
        self.assertTrue(fenna["notes"].startswith("source: outlook"))

    def test_name_only_rows_match_by_name_across_sources(self) -> None:
        store = MemStore()
        ci.run(store, records("linkedin-Connections.csv")[1], "linkedin", apply=True, today=TODAY)
        counts = ci.run(store, records("facebook-your_friends.json")[1], "facebook", apply=True, today=TODAY)
        # Cleo Varga came from LinkedIn already; the blank name is skipped.
        self.assertEqual((counts["new"], counts["duplicate"], counts["skipped_no_name"]), (2, 1, 1))
        self.assertIsNotNone(store.by_name("Zoë Kalnina"))

    def test_instagram_files_overlap_once(self) -> None:
        store = MemStore()
        ci.run(store, records("instagram-followers_1.json")[1], "instagram", apply=True, today=TODAY)
        counts = ci.run(store, records("instagram-following.json")[1], "instagram", apply=True, today=TODAY)
        self.assertEqual((counts["new"], counts["duplicate"]), (1, 1))
        self.assertEqual(len(store.rows), 3)

    def test_phone_match_ignores_formatting(self) -> None:
        store = MemStore([{"name": "Dario", "phone": "00351910000001"}])
        counts = ci.run(store, records("google-contacts.csv")[1], "google", apply=False, today=TODAY)
        self.assertEqual((counts["new"], counts["duplicate"]), (2, 1))

    def test_shared_phone_or_company_site_does_not_merge_two_people(self) -> None:
        store = MemStore()
        site = ci.split_url("https://www.sample-studio.example/")
        self.assertEqual(site, {"website": "https://sample-studio.example"})
        rows = [ci.Record(name="Nadia Okafor", email="nadia@example.com", phone="+351 210 000 100", **site),
                ci.Record(name="Omar Lindqvist", email="omar@example.com", phone="+351 210 000 100", **site),
                ci.Record(name="Pia Lindqvist", phone="210000100")]
        counts = ci.run(store, rows, "google", apply=True, today=TODAY)
        self.assertEqual((counts["new"], counts["duplicate"], counts["filled"]), (3, 0, 0))
        self.assertEqual(store.by_name("Nadia Okafor")["email"], "nadia@example.com")
        self.assertIn("website: https://sample-studio.example", store.by_name("Omar Lindqvist")["notes"])
        self.assertNotIn("profile:", store.by_name("Omar Lindqvist")["notes"])

    def test_social_urls_in_address_books_count_as_profiles(self) -> None:
        self.assertEqual(ci.split_url("linkedin.com/in/alba-quintero-test/"),
                         {"profile": "https://linkedin.com/in/alba-quintero-test"})
        store = MemStore()
        ci.run(store, records("linkedin-Connections.csv")[1], "linkedin", apply=True, today=TODAY)
        rec = ci.Record(name="Alba Q", **ci.split_url("https://www.linkedin.com/in/alba-quintero-test"))
        self.assertEqual(ci.run(store, [rec], "vcard", apply=False, today=TODAY)["duplicate"], 1)

    def test_over_long_fields_are_skipped_not_fatal(self) -> None:
        store = MemStore()
        counts = ci.run(store, [ci.Record(name="x" * 5000), ci.Record(name="Ok Person")], "vcard", apply=True, today=TODAY)
        self.assertEqual((counts["new"], counts["skipped_invalid"]), (1, 1))


class Cli(unittest.TestCase):
    def test_dry_run_is_the_default(self) -> None:
        store = MemStore()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ci.main([str(FIX / "contacts.vcf")], store=store)
        self.assertEqual(rc, 0)
        self.assertEqual(store.writes, 0)
        self.assertIn("would add", out.getvalue())
        self.assertIn("Dry run", out.getvalue())

    def test_unavailable_crm_exits_3_with_the_hint(self) -> None:
        class Down(MemStore):
            def contacts(self, q="", limit=1000):
                raise crm.CrmUnavailable("no dsn", "unconfigured")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ci.main([str(FIX / "contacts.vcf")], store=Down())
        self.assertEqual(rc, 3)
        self.assertIn("CHASSIS_PG_DSN", err.getvalue())

    def test_unreadable_file_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ci.main([str(FIX / "nope.csv")], store=MemStore()), 2)


if __name__ == "__main__":
    unittest.main()
