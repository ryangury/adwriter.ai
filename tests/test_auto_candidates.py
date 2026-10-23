"""Auto Find candidates (research.py --auto-candidates): which rows are searched and in what order, the per-run
limits (12 searches, $2.00 logged), the 30-day skip, the orchestrator.lock wait, errors, and the Research page's
candidate ranking. Offline: a temp feature_cache.db, a stubbed search, no network, nothing written to live files."""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)

sys.path.insert(0, str(_paths.ROOT))
import feature_cache  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="auto_cand_test_"))
feature_cache.DB_PATH = TMP / "feature_cache.db"
import api_cost  # noqa: E402
import research as R  # noqa: E402

TODAY = date(2026, 10, 23)


def key(model, engine, kind="tow"):
    k = dict(year=2024, make="Mercedes-Benz", model=model, engine=engine, drivetrain="AWD", cab="", bed="",
             tow_package="TRAILER HITCH" if kind == "tow" else "")
    return R.key_id(kind, k)


def row(section, model, stocks, *, kind="tow", fact=None, reason_code="not_looked_up", incomplete=False):
    return {"kind": kind, "section": section, "key_id": None if incomplete else key(model, model.upper() + " 450", kind),
            "config_text": f"2024 Mercedes-Benz {model}", "reason": "Never looked up", "reason_code": reason_code,
            "fact": fact, "cars": [{"stock": s} for s in stocks]}


VEHICLES = [{"stock_number": s, "vin": "V" + s} for s in ("S1", "S2", "S3", "S4", "S5", "SOLD")]
HISTORY = {"SOLD": {"absent_since": "2026-10-20"}}


def page(sections):
    return mock.patch.object(R, "page_rows", return_value={"sections": {s: sections.get(s, []) for s in R.AUTO_SECTIONS}})


class Spend:
    """A fake logged-cost counter: each stubbed search adds `per` dollars."""

    def __init__(self, per=0.07):
        self.total, self.per = 0.0, per

    def __call__(self):
        return self.total


def finder(spend, results=None, calls=None):
    def find(kind, k, purpose=None, origin=None):
        if calls is not None:
            calls.append((kind, k["model"], purpose, origin))
        spend.total += spend.per
        r = (results or {}).get(k["model"])
        if isinstance(r, Exception):
            raise r
        return r or {"candidates": [{"figure": 7700}], "error": None}
    return find


def wipe():
    with R._connect_auto() as c:
        c.execute("DELETE FROM config_search")
        c.execute("DELETE FROM config_search_run")


class Plan(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_priority_order_and_skips(self):
        sections = {
            "range": [row("range", "EQE", ["S3"], kind="range", reason_code="epa_no_entry"),
                      row("range", "EQS", ["S3"], kind="range", reason_code="not_checked")],
            "tow_missing": [row("tow_missing", "GLE", ["S2"]), row("tow_missing", "GLB", ["S2"], fact={"figure": 3500}),
                            row("tow_missing", "RX", ["S2"], incomplete=True)],
            "tow_stated": [row("tow_stated", "GLS", ["S1"]), row("tow_stated", "GLC", ["SOLD"])],
        }
        with page(sections):
            todo, skipped = R.auto_plan(VEHICLES, HISTORY, today=TODAY)
        self.assertEqual([t["config_text"].split()[-1] for t in todo], ["GLS", "GLE", "EQE"],
                         "tow figures stated first, then hitch with no figure, then no-EPA range rows")
        why = {s["config_text"].split()[-1]: s["skip"] for s in skipped}
        self.assertEqual(why["GLC"], "no live car")
        self.assertEqual(why["GLB"], "entry exists")
        self.assertEqual(why["RX"], "configuration incomplete")
        self.assertIn("free EPA check", why["EQS"])

    def test_thirty_day_skip(self):
        sections = {"tow_stated": [row("tow_stated", "GLS", ["S1"]), row("tow_stated", "GLE", ["S1"])]}
        R._record_search("tow", key("GLS", "GLS 450"), "x", "none", 0, 0.07, None, (TODAY - timedelta(days=29)).isoformat())
        R._record_search("tow", key("GLE", "GLE 450"), "x", "none", 0, 0.07, None, (TODAY - timedelta(days=30)).isoformat())
        with page(sections):
            todo, skipped = R.auto_plan(VEHICLES, HISTORY, today=TODAY)
        self.assertEqual([t["config_text"].split()[-1] for t in todo], ["GLE"], "30 days ago: due again")
        self.assertIn("29 day(s) ago", skipped[0]["skip"])


class Run(unittest.TestCase):
    def setUp(self):
        wipe()
        self.sleeps = []

    def run_auto(self, sections, spend, *, results=None, lock=None, calls=None, clock=None):
        with page(sections):
            return R.run_auto_candidates(VEHICLES, HISTORY, find=finder(spend, results, calls),
                                         lock_held=lock or (lambda: False), sleep=self.sleeps.append,
                                         clock=clock or (lambda: 0.0), spent=spend, today=TODAY, log=lambda *a: None)

    def many(self, n):
        return {"tow_missing": [row("tow_missing", f"M{i:02d}", ["S2"]) for i in range(n)]}

    def test_searches_store_results_with_the_auto_purpose(self):
        calls = []
        s = self.run_auto({"tow_stated": [row("tow_stated", "GLS", ["S1"])]}, Spend(), calls=calls)
        self.assertEqual(calls, [("tow", "GLS", "research_candidates_auto", "Auto search")])
        self.assertEqual((s["searched"], s["found"], s["stopped"]), (1, 1, None))
        last = R.last_auto_search("tow", key("GLS", "GLS 450"))
        self.assertEqual((last["result"], last["searched_date"], last["n_candidates"]), ("found", TODAY.isoformat(), 1))
        self.assertIn("1 configuration, $0.07", R.auto_summary_line())

    def test_twelve_search_limit(self):
        s = self.run_auto(self.many(15), Spend(per=0.01))
        self.assertEqual(s["searched"], 12)
        self.assertEqual(s["stopped"], "12-search limit reached")

    def test_cap_reached(self):
        s = self.run_auto(self.many(12), Spend(per=0.24))
        # $0.24 each: the 8th starts at $1.68 ($1.68 + the $0.25 cap <= $2.00); at $1.92 a 9th could pass $2.00
        self.assertEqual(s["searched"], 8)
        self.assertTrue(s["stopped"].startswith("$2.00 cap reached"), s["stopped"])
        self.assertAlmostEqual(s["cost"], 1.92)
        self.assertLessEqual(s["cost"], R.AUTO_MAX_COST_USD)

    def test_error_is_recorded_and_the_run_continues(self):
        sections = {"tow_stated": [row("tow_stated", "GLS", ["S1"]), row("tow_stated", "GLE", ["S1"]),
                                   row("tow_stated", "GLC", ["S1"])]}
        results = {"GLS": RuntimeError("boom"), "GLE": {"candidates": [], "error": "cost_cap: aborted at $0.25"},
                   "GLC": {"candidates": [], "error": None}}
        s = self.run_auto(sections, Spend(), results=results)
        self.assertEqual([r["result"] for r in s["results"]], ["error", "error", "none"])
        self.assertIn("boom", R.last_auto_search("tow", key("GLS", "GLS 450"))["error"])
        self.assertEqual(s["searched"], 3)

    def test_lock_present_waits_then_searches(self):
        state = {"n": 0}

        def lock():
            state["n"] += 1
            return state["n"] <= 3          # held for three checks, then released
        s = self.run_auto({"tow_stated": [row("tow_stated", "GLS", ["S1"])]}, Spend(), lock=lock)
        self.assertEqual(self.sleeps, [30, 30, 30])
        self.assertEqual(s["searched"], 1)

    def test_lock_never_clears_stops_after_ninety_minutes_without_writing(self):
        t = {"now": 0.0}

        def clock():
            return t["now"]

        def sleep(sec):
            t["now"] += sec
        with page({"tow_stated": [row("tow_stated", "GLS", ["S1"])]}):
            s = R.run_auto_candidates(VEHICLES, HISTORY, find=finder(Spend()), lock_held=lambda: True, sleep=sleep,
                                      clock=clock, spent=Spend(), today=TODAY, log=lambda *a: None)
        self.assertEqual(s["searched"], 0)
        self.assertEqual(s["stopped"], "orchestrator.lock still held after 90 minutes")
        self.assertIsNone(R.last_auto_search("tow", key("GLS", "GLS 450")))

    def test_dry_run_searches_nothing_and_estimates(self):
        with page(self.many(3)), mock.patch.object(R, "find_candidates", side_effect=AssertionError("no search")):
            s = R.run_auto_candidates(VEHICLES, HISTORY, dry_run=True, today=TODAY)
        self.assertEqual((s["planned"], s["searched"], s["worst_case"]), (3, 0, 0.75))
        self.assertAlmostEqual(s["estimate"], 3 * R.AUTO_EST_COST_USD, places=1)
        self.assertIsNone(R.last_auto_run())

    def test_never_writes_ad_history(self):
        import adwriter as A

        with mock.patch.object(A, "save_ad_history", side_effect=AssertionError("no ad_history write")):
            self.run_auto({"tow_stated": [row("tow_stated", "GLS", ["S1"])]}, Spend())


class Ranking(unittest.TestCase):
    def test_order_and_marks(self):
        k = {"year": 2024, "make": "Mercedes-Benz", "model": "GLS", "engine": "GLS 450"}
        cands = [
            {"figure": 7700, "source_url": "", "quote": "", "origin": "Find candidates (model's reply, no quote)"},
            {"figure": 7700, "source_url": "https://www.caranddriver.com/x", "quote": "The 2023 GLS 450 tows 7,700 lbs in our tested run",
             "origin": "Auto search (quoted page)", "found_date": "2026-10-20"},
            {"figure": 7700, "source_url": "https://www.mbusa.com/en/vehicles/gls", "quote": "2024 GLS 450 4MATIC: up to 7,700 lbs",
             "origin": "Auto search (quoted page)", "found_date": "2026-10-22"},
            {"figure": 7500, "source_url": "https://www.mercedes-benz.com.py/py/gls", "quote": "GLS 450 7,500 lbs",
             "origin": "Find candidates (quoted page)"},
            {"figure": 7700, "source_url": "https://x.com", "quote": "q", "origin": "tow cache, OTHER configuration: GLS 580"},
        ]
        out = R.rank_candidates("tow", k, cands)
        self.assertEqual(out[0]["domain"], "mbusa.com")
        self.assertEqual(out[0]["tier_label"], "quoted page, right year and trim")
        self.assertIn("manufacturer", out[0]["marks"])
        self.assertEqual(out[0]["n_sources"], 2, "mbusa.com and caranddriver.com agree on 7,700")
        cd = next(c for c in out if c["domain"] == "caranddriver.com")
        self.assertIn("media", cd["marks"])
        self.assertIn("real-world / tested figure", cd["weak"])
        self.assertIn("quote names 2023", cd["weak"])
        py = next(c for c in out if c["figure"] == 7500)
        self.assertIn("non-US page", py["weak"])
        self.assertEqual(out[-2]["tier_label"], "reply only, no quote")
        self.assertEqual(out[-1]["tier_label"], "another configuration")
        epa = R.rank_candidates("range", {"year": 2023, "make": "Mercedes-Benz", "model": "EQE", "engine": "x"},
                                [{"figure": 269, "source_url": "https://www.fueleconomy.gov/feg/x", "quote": "2023 EQE 269"}])
        self.assertIn("EPA", epa[0]["marks"])

    def test_google_query(self):
        self.assertEqual(R.google_query("tow", "2019 Lexus RX engine ? AWD [tow equipment: TOWING PREP PACKAGE]"),
                         "2019 Lexus RX AWD towing capacity")
        self.assertEqual(R.google_query("range", "2023 Mercedes-Benz EQE battery-electric EQE 500 AWD"),
                         "2023 Mercedes-Benz EQE battery-electric EQE 500 AWD EPA electric range")


class Wiring(unittest.TestCase):
    def test_purpose_listed(self):
        self.assertIn("research_candidates_auto", api_cost.PURPOSES)

    def test_build_summary_line(self):
        wipe()
        self.assertEqual(R.auto_summary_line(), "Research candidates found last night: no auto run yet")

    def test_task_registered_in_setup_script(self):
        src = (Path(_paths.ROOT) / "setup_tasks.ps1").read_text(encoding="utf-8")
        self.assertIn('Register-AdWriterTask -Name "AdWriter-ResearchCandidates"', src)
        self.assertIn('-At (Get-Date "09:30")', src.split("AdWriter-ResearchCandidates")[1][:300])


if __name__ == "__main__":
    unittest.main(verbosity=2)
