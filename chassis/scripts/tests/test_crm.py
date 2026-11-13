#!/usr/bin/env python3
"""CRM: input cleaning, the yes/maybe headcount, how database failures turn
into the setup page, and the dashboard tab (origin check, forms, escaping).

The tab tests run against an in-memory store with the same interface as
_crm.Store, so they need no database. The Postgres class applies the real
migrations and drives the real store and tab; it runs only when
CHASSIS_TEST_PG_DSN points at a throwaway database.

Run:
    python3 -m pytest chassis/scripts/tests/test_crm.py -v
    CHASSIS_TEST_PG_DSN=postgresql://... python3 -m pytest chassis/scripts/tests/test_crm.py -v
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
DASHBOARD = SCRIPTS.parent / "dashboard"
for p in (str(SCRIPTS), str(DASHBOARD)):
    if p not in sys.path:
        sys.path.insert(0, p)
import _crm as crm  # noqa: E402
from chassis.db.connection import DSN_ENV_VARS, ChassisDBUnavailable  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

PG_DSN = os.environ.get("CHASSIS_TEST_PG_DSN", "").strip()


class Cleaning(unittest.TestCase):
    def test_contact_needs_a_name_and_blanks_become_null(self) -> None:
        with self.assertRaises(crm.CrmError):
            crm.clean_contact({"name": "   "})
        got = crm.clean_contact({"name": " Ana ", "email": "", "whatsapp": " +1 555 0100 "})
        self.assertEqual(got, {"name": "Ana", "email": None, "phone": None, "whatsapp": "+1 555 0100", "notes": None})

    def test_list_date_must_be_iso(self) -> None:
        self.assertEqual(crm.clean_list({"name": "Picnic", "event_date": "2026-05-01"})["event_date"], date(2026, 5, 1))
        with self.assertRaises(crm.CrmError):
            crm.clean_list({"name": "Picnic", "event_date": "May 1st"})

    def test_member_status_and_headcount_are_checked(self) -> None:
        self.assertEqual(crm.clean_member({})["status"], "invited")
        self.assertEqual(crm.clean_member({"headcount": ""})["headcount"], 1)
        self.assertEqual(crm.clean_member({"status": "YES", "headcount": "3"})["status"], "yes")
        for bad in ({"status": "attending"}, {"headcount": "two"}, {"headcount": "-1"}, {"headcount": "1000"},
                    {"last_followup_on": "yesterday"}):
            with self.subTest(bad), self.assertRaises(crm.CrmError):
                crm.clean_member(bad)

    def test_partial_member_keeps_only_sent_fields(self) -> None:
        self.assertEqual(crm.clean_member({"status": "no", "channel": ""}, partial=True),
                         {"status": "no", "channel": None})

    def test_summary_counts_heads_for_yes_and_maybe_only(self) -> None:
        got = crm.summarize([{"status": "yes", "headcount": 2}, {"status": "yes", "headcount": 3},
                             {"status": "maybe", "headcount": 1}, {"status": "no", "headcount": 4},
                             {"status": "invited", "headcount": 1}])
        self.assertEqual(got["counts"], {"invited": 1, "yes": 2, "maybe": 1, "no": 1, "unknown": 0})
        self.assertEqual((got["total"], got["headcount_yes"], got["headcount_maybe"], got["headcount_coming"]),
                         (5, 5, 1, 6))


class FakeError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


class FakeConn:
    def __init__(self, raise_on_execute: Exception | None = None) -> None:
        self.error = raise_on_execute
        self.closed = self.committed = self.rolled_back = False

    def cursor(self):
        conn = self

        class Cur:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                if conn.error:
                    raise conn.error
        return Cur()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class Failures(unittest.TestCase):
    def test_no_dsn_is_reported_as_unconfigured(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in DSN_ENV_VARS}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(crm.CrmUnavailable) as got:
                crm.Store().contacts()
        self.assertEqual(got.exception.reason, "unconfigured")
        self.assertIn("CHASSIS_PG_DSN", crm.unavailable_hint("unconfigured"))

    def test_driver_and_reachability_reasons_pass_through(self) -> None:
        for reason in ("driver_missing", "unreachable"):
            def boom(reason=reason):
                raise ChassisDBUnavailable("nope", reason=reason)
            with self.subTest(reason), self.assertRaises(crm.CrmUnavailable) as got:
                crm.Store(boom).lists()
            self.assertEqual(got.exception.reason, reason)

    def test_missing_tables_ask_for_the_migration(self) -> None:
        conn = FakeConn(FakeError("42P01"))
        with self.assertRaises(crm.CrmUnavailable) as got:
            crm.Store(lambda: conn).contacts()
        self.assertEqual(got.exception.reason, "no_tables")
        self.assertIn("chassis.db.migrate", crm.unavailable_hint("no_tables"))
        self.assertTrue(conn.rolled_back and conn.closed)

    def test_constraint_violation_is_a_user_error_not_a_crash(self) -> None:
        with self.assertRaises(crm.CrmError):
            crm.Store(lambda: FakeConn(FakeError("23503"))).add_member(1, 2)

    def test_success_commits_and_closes(self) -> None:
        conn = FakeConn()
        crm.Store(lambda: conn).check()
        self.assertTrue(conn.committed and conn.closed)


class MemoryStore(crm.Store):
    """Same interface as crm.Store, held in dicts, for the tab tests."""

    def __init__(self) -> None:
        self.c: dict[int, dict] = {}
        self.l: dict[int, dict] = {}
        self.m: dict[tuple[int, int], dict] = {}
        self.ids = itertools.count(1)

    def contacts(self, q="", limit=1000):
        out = [{**c, "list_count": sum(1 for (_, cid) in self.m if cid == c["id"])} for c in self.c.values()
               if not q or any(q.lower() in (c.get(f) or "").lower() for f in crm.CONTACT_FIELDS)]
        return sorted(out, key=lambda c: (c["name"].lower(), c["id"]))[:limit]

    def contact(self, contact_id):
        return self.c.get(contact_id)

    def add_contact(self, fields):
        cid = next(self.ids)
        self.c[cid] = {"id": cid, **fields}
        return cid

    def update_contact(self, contact_id, fields):
        if contact_id not in self.c:
            return False
        self.c[contact_id].update(fields)
        return True

    def delete_contact(self, contact_id):
        self.m = {k: v for k, v in self.m.items() if k[1] != contact_id}
        return self.c.pop(contact_id, None) is not None

    def memberships_of(self, contact_id):
        return [{"list_id": lid, "list_name": self.l[lid]["name"], "event_date": self.l[lid]["event_date"],
                 "status": m["status"], "headcount": m["headcount"]}
                for (lid, cid), m in self.m.items() if cid == contact_id]

    def lists(self):
        def heads(lid, s):
            return sum(m["headcount"] for (l2, _), m in self.m.items() if l2 == lid and m["status"] == s)
        return [{**l, "members": sum(1 for (l2, _) in self.m if l2 == l["id"]),
                 "headcount_yes": heads(l["id"], "yes"), "headcount_maybe": heads(l["id"], "maybe")}
                for l in self.l.values()]

    def get_list(self, list_id):
        return self.l.get(list_id)

    def add_list(self, fields):
        lid = next(self.ids)
        self.l[lid] = {"id": lid, **fields}
        return lid

    def update_list(self, list_id, fields):
        if list_id not in self.l:
            return False
        self.l[list_id].update(fields)
        return True

    def delete_list(self, list_id):
        self.m = {k: v for k, v in self.m.items() if k[0] != list_id}
        return self.l.pop(list_id, None) is not None

    def members(self, list_id):
        return sorted(({"contact_id": cid, **{k: self.c[cid].get(k) for k in ("name", "email", "phone", "whatsapp")},
                        **m} for (lid, cid), m in self.m.items() if lid == list_id),
                      key=lambda r: r["name"].lower())

    def add_member(self, list_id, contact_id, fields=None, raw_source=None):
        if (list_id, contact_id) in self.m:
            return False
        self.m[(list_id, contact_id)] = {**crm.clean_member({}), **(fields or {}), "raw_source": raw_source}
        return True

    def update_member(self, list_id, contact_id, fields):
        if (list_id, contact_id) not in self.m:
            return False
        self.m[(list_id, contact_id)].update(fields)
        return True

    def remove_member(self, list_id, contact_id):
        return self.m.pop((list_id, contact_id), None) is not None


class TabBase(unittest.TestCase):
    SAME = {"origin": "http://testserver"}

    def make_store(self):
        return MemoryStore()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"CUSTOMER_HOME": str(home), "CHASSIS_PLUGINS_ROOT": str(home / "none")})
        patcher.start()
        self.addCleanup(patcher.stop)
        from fastapi import FastAPI
        from core import crm as tab
        self.store = self.make_store()
        app = FastAPI()
        app.include_router(tab.build_router(store=self.store), prefix="/crm")
        self.client = TestClient(app, follow_redirects=False)

    def post(self, path: str, data: dict, **kw):
        return self.client.post(path, data=data, headers=self.SAME, **kw)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi and httpx")
class Tab(TabBase):
    def test_empty_pages_render(self) -> None:
        self.assertIn("No contacts yet", self.client.get("/crm").text)
        self.assertIn("No lists yet", self.client.get("/crm/lists").text)

    def test_every_post_needs_our_origin(self) -> None:
        cid = self.store.add_contact(crm.clean_contact({"name": "Ana"}))
        lid = self.store.add_list(crm.clean_list({"name": "Picnic"}))
        self.store.add_member(lid, cid)
        posts = ["/crm/contacts", f"/crm/contacts/{cid}", f"/crm/contacts/{cid}/delete", "/crm/lists",
                 f"/crm/lists/{lid}", f"/crm/lists/{lid}/delete", f"/crm/lists/{lid}/members",
                 f"/crm/lists/{lid}/members/{cid}", f"/crm/lists/{lid}/members/{cid}/remove"]
        for path in posts:
            for headers in ({}, {"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}):
                with self.subTest(path=path, headers=headers):
                    got = self.client.post(path, data={"name": "X", "status": "no"}, headers=headers)
                    self.assertEqual(got.status_code, 403)
        self.assertEqual([c["name"] for c in self.store.contacts()], ["Ana"])
        self.assertEqual(self.store.m[(lid, cid)]["status"], "invited")
        self.assertIn(self.client.get(f"/crm/contacts/{cid}/delete").status_code, (404, 405))

    def test_add_edit_search_and_delete_a_contact(self) -> None:
        got = self.post("/crm/contacts", {"name": "Ana Example", "email": "ana@example.com", "whatsapp": "+1 555 0100"})
        self.assertEqual(got.status_code, 303)
        cid = int(got.headers["location"].rsplit("/", 1)[1])
        self.assertIn('value="ana@example.com"', self.client.get(got.headers["location"]).text)
        self.post("/crm/contacts", {"name": "Bo Sample"})
        self.assertEqual(self.post(f"/crm/contacts/{cid}", {"name": "Ana E.", "phone": "555"}).status_code, 303)
        self.assertEqual(self.store.contact(cid)["phone"], "555")
        self.assertIsNone(self.store.contact(cid)["email"])
        page = self.client.get("/crm?q=bo").text
        self.assertIn("Bo Sample", page)
        self.assertNotIn("Ana E.", page)
        self.assertEqual(self.post(f"/crm/contacts/{cid}/delete", {}).headers["location"], "/crm")
        self.assertIsNone(self.store.contact(cid))
        self.assertEqual(self.post(f"/crm/contacts/{cid}/delete", {}).status_code, 404)

    def test_bad_input_shows_the_error_and_keeps_what_was_typed(self) -> None:
        got = self.post("/crm/contacts", {"name": " ", "email": "kept@example.com"})
        self.assertEqual(got.status_code, 400)
        self.assertIn("Name is required", got.text)
        self.assertIn('value="kept@example.com"', got.text)
        got = self.post("/crm/lists", {"name": "Picnic", "event_date": "soon"})
        self.assertEqual(got.status_code, 400)
        self.assertIn("must be a date", got.text)

    def test_list_view_counts_and_inline_member_edits(self) -> None:
        lid = int(self.post("/crm/lists", {"name": "Picnic", "event_date": "2026-05-01"}).headers["location"].rsplit("/", 1)[1])
        ids = [self.store.add_contact(crm.clean_contact({"name": n})) for n in ("Ana", "Bo", "Cy", "Di")]
        for cid, (status, heads) in zip(ids, [("yes", "2"), ("yes", "3"), ("maybe", "1"), ("no", "4")]):
            self.assertEqual(self.post(f"/crm/lists/{lid}/members",
                                       {"contact_id": str(cid), "status": status, "headcount": heads}).status_code, 303)
        page = self.client.get(f"/crm/lists/{lid}").text
        self.assertIn('<div class="stat-num">6</div><div class="stat-label">Heads yes + maybe</div>', page)
        self.assertIn("5 + 1", page)
        self.assertIn("Every contact is already on this list", page)

        got = self.post(f"/crm/lists/{lid}/members/{ids[3]}",
                        {"status": "maybe", "headcount": "2", "channel": "email", "last_followup_on": "2026-04-20",
                         "back_status": "no"})
        self.assertEqual(got.headers["location"], f"/crm/lists/{lid}?status=no#m{ids[3]}")
        self.assertEqual(self.store.m[(lid, ids[3])]["last_followup_on"], date(2026, 4, 20))
        self.assertEqual(crm.summarize(self.store.members(lid))["headcount_coming"], 8)

        filtered = self.client.get(f"/crm/lists/{lid}?status=yes").text
        self.assertIn("Ana", filtered)
        self.assertNotIn(">Cy<", filtered)
        self.assertIn("Picnic", self.client.get(f"/crm/contacts/{ids[0]}").text)

    def test_bad_member_values_and_duplicates_are_refused(self) -> None:
        lid = self.store.add_list(crm.clean_list({"name": "Picnic"}))
        cid = self.store.add_contact(crm.clean_contact({"name": "Ana"}))
        self.post(f"/crm/lists/{lid}/members", {"contact_id": str(cid)})
        got = self.post(f"/crm/lists/{lid}/members", {"contact_id": str(cid)})
        self.assertEqual(got.status_code, 400)
        self.assertIn("already on this list", got.text)
        for bad in ({"status": "attending"}, {"headcount": "lots"}):
            with self.subTest(bad):
                self.assertEqual(self.post(f"/crm/lists/{lid}/members/{cid}", bad).status_code, 400)
        self.assertEqual(self.store.m[(lid, cid)]["status"], "invited")
        self.assertEqual(self.post(f"/crm/lists/{lid}/members", {"contact_id": "abc"}).status_code, 400)
        self.assertEqual(self.post(f"/crm/lists/{lid}/members/999", {"status": "yes"}).status_code, 404)

    def test_remove_member_and_delete_list_keep_the_contact(self) -> None:
        lid = self.store.add_list(crm.clean_list({"name": "Picnic"}))
        cid = self.store.add_contact(crm.clean_contact({"name": "Ana"}))
        self.store.add_member(lid, cid)
        self.assertEqual(self.post(f"/crm/lists/{lid}/members/{cid}/remove", {}).status_code, 303)
        self.assertEqual(self.store.members(lid), [])
        self.store.add_member(lid, cid)
        self.assertEqual(self.post(f"/crm/lists/{lid}/delete", {}).headers["location"], "/crm/lists")
        self.assertIsNone(self.store.get_list(lid))
        self.assertIsNotNone(self.store.contact(cid))
        self.assertEqual(self.client.get(f"/crm/lists/{lid}").status_code, 404)

    def test_authored_text_is_escaped(self) -> None:
        cid = self.store.add_contact(crm.clean_contact({"name": "<script>alert(1)</script>"}))
        lid = self.store.add_list(crm.clean_list({"name": "<img src=x onerror=alert(1)>"}))
        self.store.add_member(lid, cid)
        for path in ("/crm", f"/crm/contacts/{cid}", "/crm/lists", f"/crm/lists/{lid}"):
            with self.subTest(path):
                text = self.client.get(path).text
                self.assertNotIn("<script>alert(1)", text)
                self.assertNotIn("<img src=x", text)


@unittest.skipUnless(HAVE_FASTAPI, "needs fastapi and httpx")
class Mounted(unittest.TestCase):
    def test_core_tab_without_a_dsn_shows_setup_not_an_error(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in DSN_ENV_VARS}
        env.update({"CUSTOMER_HOME": str(home), "CHASSIS_PLUGINS_ROOT": str(home / "none")})
        with mock.patch.dict(os.environ, env, clear=True):
            import app as dash_app
            import tabs
            client = TestClient(dash_app.create_app(tabs.Registry(), home / "none", home / "none"),
                                follow_redirects=False)
            for path in ("/crm", "/crm/lists", "/crm/lists/1"):
                with self.subTest(path):
                    got = client.get(path)
                    self.assertEqual(got.status_code, 503)
                    self.assertIn("CRM needs Postgres", got.text)
                    self.assertIn("CHASSIS_PG_DSN", got.text)
            got = client.post("/crm/contacts", data={"name": "Ana"}, headers={"origin": "http://testserver"})
            self.assertEqual(got.status_code, 503)
            self.assertIn('href="/crm"', client.get("/briefings").text)

    def test_router_mounts_in_another_app_with_its_own_renderer(self) -> None:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from core import crm as tab

        store = MemoryStore()
        store.add_contact(crm.clean_contact({"name": "Ana"}))

        def render(request, template, context, status_code=200):
            return JSONResponse({"template": template, "names": [c["name"] for c in context["contacts"]],
                                 "section": context["active_section"]}, status_code=status_code)
        other = FastAPI()
        other.include_router(tab.build_router(render, store), prefix="/people")
        self.assertEqual(TestClient(other).get("/people").json(),
                         {"template": "crm_contacts.html", "names": ["Ana"], "section": "crm"})


@unittest.skipUnless(PG_DSN and HAVE_FASTAPI, "set CHASSIS_TEST_PG_DSN to a throwaway database")
class Postgres(TabBase):
    """The real migration, store and constraints. Drops the CRM tables first."""

    @classmethod
    def setUpClass(cls) -> None:
        from chassis.db.connection import connect
        from chassis.db.migrate import apply_migrations
        with connect(dsn=PG_DSN) as conn:
            conn.execute("DROP TABLE IF EXISTS crm_memberships, crm_lists, crm_contacts")
            if conn.execute("SELECT to_regclass('chassis_schema_migrations')").fetchone()[0]:
                conn.execute("DELETE FROM chassis_schema_migrations WHERE name = '003_crm.sql'")
            conn.commit()
            cls.applied = apply_migrations(conn)

    def make_store(self):
        from chassis.db.connection import connect
        return crm.Store(lambda: connect(dsn=PG_DSN, dict_rows=True))

    def test_migration_applied_and_reruns_as_a_no_op(self) -> None:
        from chassis.db.connection import connect
        from chassis.db.migrate import apply_migrations
        self.assertIn("003_crm.sql", self.applied)
        with connect(dsn=PG_DSN) as conn:
            self.assertEqual(apply_migrations(conn), [])

    def test_store_round_trip_and_constraints(self) -> None:
        s = self.store
        cid = s.add_contact(crm.clean_contact({"name": "Ana", "email": "a@example.com"}))
        lid = s.add_list(crm.clean_list({"name": "Picnic", "event_date": "2026-05-01"}))
        self.assertTrue(s.add_member(lid, cid, crm.clean_member({"status": "yes", "headcount": "2"}),
                                     raw_source="Ana +1 yes"))
        self.assertFalse(s.add_member(lid, cid))
        self.assertEqual(s.members(lid)[0]["raw_source"], "Ana +1 yes")
        self.assertTrue(s.update_member(lid, cid, {"raw_source": "Ana +1 (yes)"}))
        self.assertEqual(s.members(lid)[0]["raw_source"], "Ana +1 (yes)")
        self.assertTrue(s.update_contact(cid, crm.clean_contact({"name": "Ana B", "whatsapp": "+1 555 0100"})))
        self.assertEqual((s.contact(cid)["name"], s.contact(cid)["email"], s.contact(cid)["whatsapp"]),
                         ("Ana B", None, "+1 555 0100"))
        self.assertEqual(s.memberships_of(cid)[0]["list_name"], "Picnic")
        self.assertTrue(s.update_list(lid, crm.clean_list({"name": "Picnic 2", "event_date": ""})))
        self.assertEqual((s.get_list(lid)["name"], s.get_list(lid)["event_date"]), ("Picnic 2", None))
        self.assertFalse(s.update_contact(999999, crm.clean_contact({"name": "X"})))
        self.assertTrue(s.update_member(lid, cid, crm.clean_member({"last_followup_on": "2026-04-20"}, partial=True)))
        m = s.members(lid)[0]
        self.assertEqual((m["status"], m["headcount"], m["last_followup_on"]), ("yes", 2, date(2026, 4, 20)))
        self.assertEqual(s.lists()[0]["headcount_yes"], 2)
        self.assertEqual(s.contacts("+1 555")[0]["list_count"], 1)
        with self.assertRaises(crm.CrmError):
            s.update_member(lid, cid, {"status": "attending"})
        with self.assertRaises(crm.CrmError):
            s.add_member(lid, 999999)
        self.assertTrue(s.delete_contact(cid))
        self.assertEqual(s.members(lid), [])
        self.assertTrue(s.delete_list(lid))

    def test_tab_end_to_end(self) -> None:
        lid = int(self.post("/crm/lists", {"name": "Dinner"}).headers["location"].rsplit("/", 1)[1])
        cid = int(self.post("/crm/contacts", {"name": "Bo"}).headers["location"].rsplit("/", 1)[1])
        self.assertEqual(self.post(f"/crm/lists/{lid}/members", {"contact_id": str(cid), "headcount": "3",
                                                                 "status": "maybe"}).status_code, 303)
        self.assertIn('<div class="stat-num">3</div><div class="stat-label">Heads yes + maybe</div>',
                      self.client.get(f"/crm/lists/{lid}").text)
        self.assertEqual(self.post(f"/crm/contacts/{cid}", {"name": "Bo Edited", "email": "bo@example.com"}).status_code, 303)
        self.assertIn("Dinner", self.client.get(f"/crm/contacts/{cid}").text)
        self.assertEqual(self.post(f"/crm/lists/{lid}", {"name": "Dinner moved", "event_date": "2026-06-01"}).status_code, 303)
        self.assertIn("Bo Edited", self.client.get(f"/crm/lists/{lid}").text)
        self.assertIn("2026-06-01", self.client.get("/crm/lists").text)
        self.assertEqual(self.post(f"/crm/contacts/{cid}/delete", {}).status_code, 303)
        self.assertEqual(self.post(f"/crm/lists/{lid}/delete", {}).status_code, 303)


if __name__ == "__main__":
    unittest.main()
