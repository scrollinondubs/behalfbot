"""Tests for the spend projections math and the alert dedupe.

Covers the 7-day blend in project(), the median-of-3 baseline, the alert
crossing rule (1.5x baseline AND $10 above it, or past the cap) and that a
crossing posts once per (service, period, level).

Run from repo root:
  python3 -m pytest chassis/scripts/tests/test_spend_projections.py -v

The page tests need fastapi and httpx and are skipped without them.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timezone
from pathlib import Path

CHASSIS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHASSIS / "scripts"))
sys.path.insert(0, str(CHASSIS / "dashboard"))

import _spend  # noqa: E402

try:
    from core import projections  # noqa: E402
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

_gspec = importlib.util.spec_from_file_location("gather_spend", CHASSIS / "scripts" / "gather-spend-snapshots.py")
gather = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(gather)

OCT = _spend.Period(date(2026, 10, 1), date(2026, 11, 1))


class PeriodTests(unittest.TestCase):
    def test_calendar_month(self):
        self.assertEqual(_spend.period_for(date(2026, 10, 24)), OCT)
        self.assertEqual(OCT.days_total, 31)

    def test_anchor_day_before_and_after(self):
        p = _spend.period_for(date(2026, 10, 5), 10)
        self.assertEqual((p.start, p.end), (date(2026, 9, 10), date(2026, 10, 10)))
        p = _spend.period_for(date(2026, 10, 10), 10)
        self.assertEqual((p.start, p.end), (date(2026, 10, 10), date(2026, 11, 10)))

    def test_anchor_clamps_to_short_month(self):
        p = _spend.period_for(date(2026, 2, 28), 31)
        self.assertEqual((p.start, p.end), (date(2026, 2, 28), date(2026, 3, 31)))

    def test_year_wrap(self):
        p = _spend.period_for(date(2027, 1, 3), 25)
        self.assertEqual((p.start, p.end), (date(2026, 12, 25), date(2027, 1, 25)))

    def test_previous_periods(self):
        prev = _spend.previous_periods(OCT, 1, 3)
        self.assertEqual([p.start for p in prev], [date(2026, 9, 1), date(2026, 8, 1), date(2026, 7, 1)])


class ProjectTests(unittest.TestCase):
    def test_no_data_projects_the_baseline(self):
        self.assertAlmostEqual(_spend.project([], OCT, 31.0), 31.0)

    def test_one_quiet_day_does_not_project_zero(self):
        # Day 1 cost nothing; 6/7 of the rate still comes from the baseline.
        got = _spend.project([0.0], OCT, 62.0)
        self.assertAlmostEqual(got, 0 + (6 / 7) * 2.0 * 30)
        self.assertGreater(got, 0)

    def test_blend_weight_at_three_days(self):
        got = _spend.project([7.0, 7.0, 7.0], OCT, 31.0)
        rate = (3 / 7) * 7.0 + (4 / 7) * 1.0
        self.assertAlmostEqual(got, 21.0 + rate * 28)

    def test_seven_days_uses_only_recent_rate(self):
        got = _spend.project([2.0] * 7, OCT, 1000.0)
        self.assertAlmostEqual(got, 14.0 + 2.0 * 24)

    def test_rate_is_last_seven_days(self):
        days = [100.0] * 3 + [1.0] * 7
        self.assertAlmostEqual(_spend.project(days, OCT, 0.0), 307.0 + 1.0 * 21)

    def test_no_baseline_means_no_blend(self):
        self.assertAlmostEqual(_spend.project([3.0], OCT, None), 3.0 + 3.0 * 30)

    def test_full_period_projects_its_total(self):
        self.assertAlmostEqual(_spend.project([1.5] * 31, OCT, 9.0), 46.5)


class BaselineTests(unittest.TestCase):
    def test_median_of_three_ignores_one_bad_month(self):
        self.assertEqual(_spend.median_baseline([45.17, 143.58, 40.0]), (45.17, 3))

    def test_median_of_two(self):
        self.assertEqual(_spend.median_baseline([40.0, 50.0]), (45.0, 2))

    def test_one_period(self):
        self.assertEqual(_spend.median_baseline([93.7]), (93.7, 1))

    def test_none(self):
        self.assertEqual(_spend.median_baseline([]), (None, 0))

    def test_only_last_three_count(self):
        self.assertEqual(_spend.median_baseline([1.0, 2.0, 3.0, 1000.0]), (2.0, 3))


class LevelTests(unittest.TestCase):
    def test_warned_over_ratio_and_floor(self):
        self.assertEqual(_spend.level_for(73.3, 45.17, 100), "warned")

    def test_normal_below_ratio(self):
        self.assertIsNone(_spend.level_for(60.0, 45.17, 100))

    def test_floor_blocks_tiny_lines(self):
        # 3x baseline but only $8 above it.
        self.assertIsNone(_spend.level_for(12.0, 4.0, None))

    def test_exactly_at_floor_is_not_warned(self):
        self.assertIsNone(_spend.level_for(30.0, 20.0, None))
        self.assertEqual(_spend.level_for(30.01, 20.0, None), "warned")

    def test_capped_beats_warned(self):
        self.assertEqual(_spend.level_for(141.6, 45.17, 100), "capped")

    def test_cap_without_baseline(self):
        self.assertEqual(_spend.level_for(101.0, None, 100), "capped")
        self.assertIsNone(_spend.level_for(99.0, None, 100))


def _seed(conn, service: str, day: str, cost: float, source: str = "api"):
    conn.execute(
        "INSERT OR REPLACE INTO spend_daily (service, day, period_start, cost_usd, source, fetched_at)"
        " VALUES (?,?,?,?,?,?)", (service, day, day[:8] + "01", cost, source, "t"))


class SummaryAndAlertTests(unittest.TestCase):
    CFG = {"vercel": {"label": "Vercel", "kind": "api", "tz": "UTC", "anchor_day": 1, "cap": 100}}
    NOW = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _spend.connect(Path(self.tmp.name) / "spend.db")
        for m, total in ((7, 93.70), (8, 40.00), (9, 45.17)):
            _seed(self.conn, "vercel", f"2026-{m:02d}-15", total)
        self.posts: list[str] = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_alerts(self):
        return gather.check_alerts(self.conn, self.CFG, {"vercel": "ok"}, self.NOW, False, poster=self.posts.append,
                                   cfg_settings={"dashboard_url": "https://dash.example"})

    def test_baseline_is_median_of_three(self):
        s = _spend.summarize(self.conn, "vercel", self.CFG["vercel"], self.NOW)
        self.assertEqual((s.baseline, s.baseline_n), (45.17, 3))

    def test_fewer_periods_reports_count(self):
        self.conn.execute("DELETE FROM spend_daily WHERE day='2026-07-15'")
        s = _spend.summarize(self.conn, "vercel", self.CFG["vercel"], self.NOW)
        self.assertEqual((round(s.baseline, 3), s.baseline_n), (42.585, 2))

    def test_quiet_month_posts_nothing(self):
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 1.4)
        self.assertEqual(self.run_alerts(), [])
        self.assertEqual(self.posts, [])

    def test_warned_once_then_capped_once(self):
        for d, c in enumerate([1.0, 1.0, 1.0, 1.0, 1.37, 8.02, 2.12], start=1):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", c)
        self.assertEqual(self.run_alerts(), ["vercel:warned"])
        self.assertIn("above its normal path", self.posts[0])
        self.assertIn("Baseline $45.17 (+", self.posts[0])
        self.assertIn("https://dash.example/projections/vercel", self.posts[0])
        # Same data, next run: nothing new.
        self.assertEqual(self.run_alerts(), [])
        self.assertEqual(len(self.posts), 1)
        # A big day pushes the projection past the $100 cap: one more post.
        _seed(self.conn, "vercel", "2026-10-07", 25.0)
        self.assertEqual(self.run_alerts(), ["vercel:capped"])
        self.assertIn("will pass its $100 cap", self.posts[1])
        self.assertEqual(self.run_alerts(), [])
        rows = self.conn.execute("SELECT level FROM spend_alerts ORDER BY level").fetchall()
        self.assertEqual(rows, [("capped",), ("warned",)])

    def test_capped_first_suppresses_later_warned(self):
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 10.0)
        self.assertEqual(self.run_alerts(), ["vercel:capped"])
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 2.5)
        self.assertEqual(self.run_alerts(), [])

    def test_new_period_alerts_again(self):
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 10.0)
        self.run_alerts()
        self.conn.execute("UPDATE spend_alerts SET period_start='2026-09-01'")
        self.assertEqual(self.run_alerts(), ["vercel:capped"])

    def test_failed_post_is_retried_next_run(self):
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 10.0)

        def boom(_text):
            raise RuntimeError("discord down")

        self.assertEqual(gather.check_alerts(self.conn, self.CFG, {"vercel": "ok"}, self.NOW, False, poster=boom), [])
        self.assertEqual(self.run_alerts(), ["vercel:capped"])

    def test_dry_run_records_nothing(self):
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 10.0)
        got = gather.check_alerts(self.conn, self.CFG, {"vercel": "ok"}, self.NOW, True, poster=self.posts.append)
        self.assertEqual(got, ["vercel:capped:dry"])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM spend_alerts").fetchone()[0], 0)

    def test_failed_fetch_never_alerts(self):
        for d in range(1, 8):
            _seed(self.conn, "vercel", f"2026-10-{d:02d}", 10.0)
        got = gather.check_alerts(self.conn, self.CFG, {"vercel": "error"}, self.NOW, False, poster=self.posts.append)
        self.assertEqual(got, [])

    def test_plan_fee_stays_out_of_daily_rate(self):
        cfg = {"label": "Cloudflare", "kind": "api", "tz": "UTC", "anchor_day": 1}
        for d in range(1, 8):
            _seed(self.conn, "cf", f"2026-10-{d:02d}", 1.0)
        self.conn.execute(
            "INSERT INTO spend_daily (service, day, period_start, cost_usd, source, fetched_at)"
            " VALUES ('cf','2026-10-01','2026-10-01',5.0,'fixed','t')")
        s = _spend.summarize(self.conn, "cf", cfg, self.NOW)
        self.assertEqual(s.daily, [1.0] * 7)
        self.assertAlmostEqual(s.to_date, 12.0)
        self.assertAlmostEqual(s.projected, 31.0 + 5.0)


class StackedChartTests(unittest.TestCase):
    """build_stacked: one bar per calendar day, segments per usage-billed service."""

    def series(self):
        return [
            {"service": "vercel", "label": "Vercel", "color": "#3987e5",
             "by_day": {"2026-10-01": 2.0, "2026-10-02": 3.0, "2026-10-03": 1.0}},
            {"service": "cloudflare", "label": "Cloudflare", "color": "#d95926",
             "by_day": {"2026-10-02": 1.0, "2026-10-03": 9.0}},
        ]

    def test_days_stack_by_calendar_date(self):
        st = projections.build_stacked(self.series(), date(2026, 10, 1), date(2026, 10, 3))
        d1, d2, d3 = st["days"][:3]
        self.assertEqual([g["service"] for g in d1["segments"]], ["vercel"])
        self.assertEqual([g["service"] for g in d2["segments"]], ["vercel", "cloudflare"])
        self.assertEqual((d2["total"], d3["total"]), (4.0, 10.0))

    def test_tallest_day_fills_the_chart(self):
        st = projections.build_stacked(self.series(), date(2026, 10, 1), date(2026, 10, 3))
        d3 = st["days"][2]
        self.assertAlmostEqual(sum(g["pct"] for g in d3["segments"]), 100.0)
        self.assertEqual(st["top"], 10.0)
        self.assertEqual(st["range"], "Oct 1 to Oct 3")

    def test_whole_month_of_slots_with_future_days_empty(self):
        st = projections.build_stacked(self.series(), date(2026, 10, 1), date(2026, 10, 3))
        self.assertEqual(len(st["days"]), 31)
        self.assertTrue(all(d["future"] and not d["segments"] for d in st["days"][3:]))
        self.assertFalse(st["days"][2]["future"])

    def test_baseline_line_scales_with_the_bars(self):
        st = projections.build_stacked(self.series(), date(2026, 10, 1), date(2026, 10, 3), base_per_day=5.0)
        self.assertEqual(st["base_pct"], 50.0)
        st = projections.build_stacked(self.series(), date(2026, 10, 1), date(2026, 10, 3), base_per_day=20.0)
        self.assertEqual(st["base_pct"], 100.0)
        self.assertEqual(st["days"][2]["segments"][1]["pct"], 45.0)

    def test_no_series_is_no_chart(self):
        self.assertIsNone(projections.build_stacked([], date(2026, 10, 1), date(2026, 10, 3)))

    def test_first_of_month_has_no_complete_day(self):
        st = projections.build_stacked(self.series(), date(2026, 11, 1), date(2026, 10, 31))
        self.assertIsNone(st["range"])
        self.assertEqual(st["top"], 0)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class ProjectionsPageTests(unittest.TestCase):
    """The routes end to end against a seeded DB: stacked index, drill-down, fees kept out of the bars."""

    CFG = {
        "vercel": {"label": "Vercel", "kind": "api", "tz": "UTC", "anchor_day": 1, "cap": 100},
        "claude_max": {"label": "Claude Max", "kind": "fixed", "fixed": 100, "tz": "UTC", "anchor_day": 1},
        "cloudflare": {"label": "Cloudflare", "kind": "api", "tz": "UTC", "anchor_day": 1},
    }

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "spend.db"
        conn = _spend.connect(db)
        today = datetime.now(timezone.utc).date()
        self.month = today.replace(day=1)
        self.has_complete_day = today.day > 1
        _seed(conn, "vercel", self.month.isoformat(), 2.5)
        _seed(conn, "cloudflare", self.month.isoformat(), 0.75)
        _seed(conn, "cloudflare", self.month.isoformat(), 5.0, "fixed")
        _seed(conn, "claude_max", self.month.isoformat(), 100.0, "fixed")
        conn.commit()
        conn.close()
        self._db, self._cfg = _spend.DB_PATH, _spend.load_config
        _spend.DB_PATH = db
        _spend.load_config = lambda path=None: self.CFG
        app = FastAPI()
        app.include_router(projections.router, prefix="/projections")
        self.client = TestClient(app)

    def tearDown(self):
        _spend.DB_PATH, _spend.load_config = self._db, self._cfg
        self.tmp.cleanup()

    def test_index_stacks_usage_and_lists_fees_apart(self):
        html = self.client.get("/projections").text
        self.assertIn('class="spend-chip selected" href="/projections"', html)
        self.assertIn("Fixed plans, not in the bars", html)
        self.assertIn("Claude Max $100.00", html)
        self.assertIn("Cloudflare plan $5.00", html)
        if self.has_complete_day:
            self.assertIn('<a href="/projections/vercel" style="height: ', html)
            self.assertIn('<a href="/projections/cloudflare" style="height: ', html)
            self.assertNotIn('<a href="/projections/claude_max" style="height: ', html)

    def test_service_page_keeps_chips_with_one_selected(self):
        html = self.client.get("/projections/vercel").text
        self.assertIn('href="/projections/vercel" style="--c: #3987e5" aria-current="page"', html)
        self.assertIn('href="/projections/claude_max"', html)
        self.assertIn('href="/projections"', html)
        self.assertNotIn("Fixed plans, not in the bars", html)

    def test_fixed_service_page_explains_no_chart(self):
        html = self.client.get("/projections/claude_max").text
        self.assertIn("no daily usage to chart", html)

    def test_unknown_service_404s(self):
        self.assertEqual(self.client.get("/projections/nope").status_code, 404)


class InvoiceKindTests(unittest.TestCase):
    NOW = datetime(2026, 12, 8, 6, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _spend.connect(Path(self.tmp.name) / "spend.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_baseline_from_invoiced_periods(self):
        cfg = {"label": "Turso", "kind": "invoice", "tz": "UTC", "anchor_day": 1}
        _seed(self.conn, "turso", "2026-10-01", 1.16, "fixed")
        _seed(self.conn, "turso", "2026-11-01", 6.30, "fixed")
        _seed(self.conn, "turso", "2026-12-01", 6.30, "fixed")
        s = _spend.summarize(self.conn, "turso", cfg, self.NOW)
        self.assertEqual((round(s.baseline, 2), s.baseline_n), (3.73, 2))
        self.assertAlmostEqual(s.to_date, 6.30)
        self.assertAlmostEqual(s.projected, 6.30)

    def test_uninvoiced_period_projects_plan_price(self):
        cfg = {"label": "Turso", "kind": "invoice", "tz": "UTC", "anchor_day": 1, "fixed": 5.0}
        _seed(self.conn, "turso", "2026-11-01", 5.0, "fixed")
        s = _spend.summarize(self.conn, "turso", cfg, self.NOW)
        self.assertEqual((s.to_date, s.projected, s.baseline), (0, 5.0, 5.0))


class ResendCostTests(unittest.TestCase):
    CFG = {"fixed": 20, "quota": 50000, "over_per_1000": 0.90}

    def test_under_quota(self):
        self.assertEqual(gather.resend_cost(312, self.CFG), 20.0)

    def test_overage_rounds_up_per_thousand(self):
        self.assertAlmostEqual(gather.resend_cost(51001, self.CFG), 21.80)


class MissingKeyTests(unittest.TestCase):
    def test_empty_env_value_is_missing(self):
        os.environ["SPEND_TEST_EMPTY_KEY"] = ""
        with self.assertRaises(gather.MissingKey):
            gather.key_for({"env": "SPEND_TEST_EMPTY_KEY"})


if __name__ == "__main__":
    unittest.main()


class ProviderTests(unittest.TestCase):
    def test_kind_follows_provider_unless_set(self):
        self.assertEqual(_spend.kind_of("vercel", {}), "api")
        self.assertEqual(_spend.kind_of("db", {"provider": "turso"}), "invoice")
        self.assertEqual(_spend.kind_of("mail", {"provider": "resend"}), "resend")
        self.assertEqual(_spend.kind_of("plan", {"provider": "fixed"}), "fixed")
        self.assertEqual(_spend.kind_of("plan", {"kind": "fixed"}), "fixed")
        self.assertEqual(_spend.provider_of("vercel_team2", {"provider": "vercel"}), "vercel")

    def test_unknown_provider_is_an_error_row_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _spend.connect(Path(tmp) / "spend.db")
            status = gather.collect(conn, {"x": {"provider": "nope", "env": "UNSET_FOR_TEST"}},
                                    datetime(2026, 10, 8, tzinfo=timezone.utc))
            self.assertEqual(status, {"x": "error"})
            conn.close()

    def test_alert_target_from_settings_then_env(self):
        self.assertEqual(gather.alert_target({"alert_channel_id": "1", "dashboard_url": "https://d.example/"}),
                         ("1", "https://d.example/projections"))
        with unittest.mock.patch.dict(os.environ, {"SPEND_ALERT_CHANNEL_ID": "2", "DASHBOARD_URL": ""}):
            self.assertEqual(gather.alert_target({}), ("2", ""))
