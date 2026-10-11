"""Researched ranges / tow ratings (research.py) and the no-live-search production path: a figure entered for a
configuration beats pass 0 and the caches (a VIN override still wins), a range miss makes no paid search and is
cached for 30 days, misses carry a reason code and candidates, the Build Summary lists them, the site saves a
figure, and "Refresh text" edits a stored ad without a model call. Offline: temp databases and files, stubs."""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)

sys.path.insert(0, str(_paths.ROOT))
import feature_cache  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="research_test_"))
feature_cache.DB_PATH = TMP / "feature_cache.db"          # research.py reads it at call time
import adwriter as A  # noqa: E402
import powertrain as P  # noqa: E402
import research as R  # noqa: E402
import tow_refresh as TR  # noqa: E402
import towing as T  # noqa: E402

T.DB_PATH = feature_cache.DB_PATH

GLC = dict(year=2025, make="Mercedes-Benz", model="GLC", engine="GLC 350e", drivetrain="AWD", cab="", bed="", tow_package="")
GLS = dict(year=2024, make="Mercedes-Benz", model="GLS", engine="GLS 450", drivetrain="AWD", cab="", bed="",
           tow_package="TRAILER HITCH")


def wipe():
    with R._connect() as c:
        c.execute("DELETE FROM config_fact")
        c.execute("DELETE FROM config_candidate")
    with feature_cache._connect() as c:
        c.execute("DELETE FROM electric_range")
        c.commit()


class Facts(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_validation(self):
        for bad in ("abc", "4", "999999"):
            with self.assertRaises(ValueError):
                R.validate("tow", bad, "https://x.com/a", "q")
        with self.assertRaises(ValueError):
            R.validate("range", "54", "not a url", "q")
        with self.assertRaises(ValueError):
            R.validate("range", "54", "https://x.com/a", "  ")
        self.assertEqual(R.validate("tow", "7,700 lbs", "https://www.mbusa.com/x", " GLS 450  tows 7,700 "),
                         (7700, "https://www.mbusa.com/x", "GLS 450 tows 7,700"))

    def test_set_get_clear_and_the_key_id_round_trip(self):
        R.set_fact("tow", GLS, 7700, "https://www.mbusa.com/gls", "Towing capacity 7,700 lbs")
        self.assertEqual(R.get_fact("tow", GLS)["figure"], 7700)
        self.assertIsNone(R.get_fact("tow", dict(GLS, tow_package="")), "the tow package is part of the key")
        kind, key = R.key_from_id(R.key_id("tow", R.key_of("tow", GLS)))
        self.assertEqual((kind, key["engine"], key["tow_package"]), ("tow", "GLS 450", "TRAILER HITCH"))
        self.assertEqual(R.key_of("range", GLS)["tow_package"], "", "a range ignores the tow package")
        self.assertTrue(R.clear_fact("tow", GLS))
        self.assertIsNone(R.get_fact("tow", GLS))

    def test_incomplete_configuration_has_no_key(self):
        self.assertIsNone(R.key_of("range", dict(GLC, engine="")))
        with self.assertRaises(ValueError):
            R.set_fact("range", dict(GLC, engine=""), 54, "https://x.com", "q")

    def test_candidates_are_deduplicated(self):
        items = [{"figure": 41, "url": "https://fueleconomy.gov/a", "label": "2026 E53 wagon"},
                 {"figure": 41, "url": "https://fueleconomy.gov/a", "label": "again"}, {"figure": "x"}]
        self.assertEqual(R.add_candidates("range", GLC, items, "pass 0"), 1)
        self.assertEqual([c["figure"] for c in R.candidates("range", GLC)], [41])


class RangePath(unittest.TestCase):
    def setUp(self):
        wipe()
        self.mfr_calls = []

    def epa_none(self, *a, **k):
        return {"status": "none", "note": "fueleconomy.gov has no 2023 EQE entry; it lists only a different model year",
                "candidates": [{"figure": 282, "label": "2024 Mercedes-Benz EQE 500 4matic (SUV)", "url": "https://www.fueleconomy.gov/x"}]}

    def test_a_pass_0_miss_makes_no_paid_search_and_is_cached_30_days(self):
        self.assertFalse(P.RANGE_PAID_SEARCH)
        with mock.patch.object(P, "manufacturer_search", side_effect=AssertionError("no paid search")):
            r = P.electric_range(2023, "Mercedes-Benz", "EQE", "EQE 500 4MATIC SUV", P.BEV, epa=self.epa_none)
        self.assertIsNone(r["miles"])
        self.assertEqual(r["reason"], "epa_no_entry")
        self.assertIn("research needed", r["flag"])
        self.assertEqual(r["candidates"][0]["figure"], 282)
        calls = []

        def epa_count(*a, **k):
            calls.append(1)
            return self.epa_none()

        again = P.electric_range(2023, "Mercedes-Benz", "EQE", "EQE 500 4MATIC SUV", P.BEV, epa=epa_count,
                                 today=date.today() + timedelta(days=29))
        self.assertEqual((again["cached"], calls, again["reason"]), (True, [], "epa_no_entry"))
        P.electric_range(2023, "Mercedes-Benz", "EQE", "EQE 500 4MATIC SUV", P.BEV, epa=epa_count,
                         today=date.today() + timedelta(days=31))
        self.assertEqual(calls, [1], "after 30 days pass 0 is tried again")

    def test_ambiguous_is_research_needed_with_candidates(self):
        amb = {"status": "ambiguous", "note": "fueleconomy.gov 2026: ambiguous - more than one candidate fits",
               "candidates": [{"figure": 44, "label": "sedan"}, {"figure": 41, "label": "wagon"}]}
        r = P.electric_range(2026, "Mercedes-Benz", "E-Class", "AMG E 53", P.PHEV, epa=lambda *a, **k: amb, use_cache=False)
        self.assertEqual((r["miles"], r["reason"], sorted(c["figure"] for c in r["candidates"])), (None, "epa_ambiguous", [41, 44]))

    def test_a_researched_figure_beats_pass_0_and_keeps_the_wording_rule(self):
        cfg = R.range_config("2025 Mercedes-Benz GLC", "GLC 350e 4MATIC SUV", "SUV", "", P.PHEV)
        self.assertEqual(cfg["engine"], "GLC 350e")
        R.set_fact("range", cfg, 54, "https://www.mbusa.com/glc", "up to 54 miles of all-electric range")
        with mock.patch.object(P, "cached_texts", return_value=("", "")), \
             mock.patch.object(P, "classify", return_value={"class": P.PHEV, "label": "plug-in hybrid", "flag": None}), \
             mock.patch.object(P, "electric_range", side_effect=AssertionError("pass 0 not needed")):
            pt = P.vehicle_powertrain("VIN1", "2025 Mercedes-Benz GLC", "GLC 350e 4MATIC SUV", body_style="SUV")
        rng = pt["range"]
        self.assertEqual((rng["miles"], rng["source"]), (54, "manual"))
        self.assertEqual(rng["phrase"], "estimated up to 54 miles of electric range", "no EPA in the quote: not 'EPA-estimated'")
        self.assertIn("calls a manual-estimated range EPA", P.sentence_problems("It has an EPA-estimated 54 miles of range.", P.PHEV, rng))
        self.assertEqual(P.sentence_problems(R.range_sentence(54, "manual"), P.PHEV, rng), [])
        R.set_fact("range", cfg, 54, "https://www.fueleconomy.gov/x", "EPA: 54 miles all-electric range")
        with mock.patch.object(P, "cached_texts", return_value=("", "")), \
             mock.patch.object(P, "classify", return_value={"class": P.PHEV, "label": "plug-in hybrid", "flag": None}):
            pt = P.vehicle_powertrain("VIN1", "2025 Mercedes-Benz GLC", "GLC 350e 4MATIC SUV", body_style="SUV")
        self.assertEqual(pt["range"]["phrase"], "EPA-estimated up to 54 miles of electric range")

    def test_pass_0_candidates_are_stored_for_the_configuration(self):
        with mock.patch.object(P, "cached_texts", return_value=("", "")), \
             mock.patch.object(P, "classify", return_value={"class": P.BEV, "label": "battery-electric", "flag": None}), \
             mock.patch.object(P, "electric_range", side_effect=lambda *a, real=P.electric_range, **k: real(*a, epa=self.epa_none, **k)):
            pt = P.vehicle_powertrain("VIN2", "2023 Mercedes-Benz EQE", "EQE 500 4MATIC SUV", body_style="SUV")
        cfg = pt["range"]["config"]
        self.assertEqual(cfg["engine"], "battery-electric EQE 500 SUV")
        self.assertEqual([c["figure"] for c in R.candidates("range", cfg)], [282])
        (item,) = R.items_for(pt, None)
        self.assertEqual((item["kind"], item["reason"], item["candidates"][0]["figure"]), ("range", "epa_no_entry", 282))


class TowPath(unittest.TestCase):
    def setUp(self):
        wipe()
        self.ov = mock.patch.object(T, "TOW_OVERRIDES_PATH", TMP / "tow_overrides.json")
        self.ov.start()
        self.addCleanup(self.ov.stop)
        (TMP / "tow_overrides.json").write_text("{}", encoding="utf-8")
        self.sticker = "TRAILER HITCH  4MATIC"

    def tow(self, vin="W1N"):
        return T.towing_for("2024 Mercedes-Benz GLS", "GLS 450 4MATIC SUV", "SUV", self.sticker, [], vin=vin,
                            powertrain_class="gas", allow_lookup=False)

    def test_a_researched_rating_fills_a_cache_miss_and_a_vin_override_still_wins(self):
        miss = self.tow()
        self.assertEqual((miss["rating"], miss["note"]), (None, "not looked up yet"))
        self.assertEqual(R.items_for(None, miss)[0]["reason"], "not_looked_up")
        R.set_fact("tow", miss["config"], 7700, "https://www.mbusa.com/gls", "Towing capacity 7,700 lbs")
        hit = self.tow()
        self.assertEqual((hit["rating"], hit["sentence"]), (7700, "It is rated to tow up to 7,700 lbs."))
        self.assertTrue(hit["researched"])
        self.assertEqual(R.items_for(None, hit), [])
        T.set_tow_override("W1N", 7500, "owner's manual")
        self.assertEqual(self.tow()["rating"], 7500)

    def test_other_cached_configurations_are_shown_as_such(self):
        miss = self.tow()
        T.save(dict(miss["config"], engine="GLS 580"), {"lbs": 7700, "url": "https://www.mbusa.com/580", "page_config": "GLS 580"})
        (c,) = R.candidates("tow", miss["config"])
        self.assertIn("OTHER configuration", c["origin"])


class Summary(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_build_summary_lists_research_needed(self):
        import orchestrator as o

        pt = {"class": P.BEV, "range": {"miles": None, "reason": "epa_no_entry", "flag": "electric range omitted (research needed): no 2023 entry",
                                         "config": dict(GLC, engine="battery-electric EQE 500 SUV", model="EQE", year=2023)}}
        R.add_candidates("range", pt["range"]["config"], [{"figure": 282, "url": "https://www.fueleconomy.gov/x", "label": "2024 EQE 500 SUV"}], "pass 0")
        tow = {"triggered": True, "rating": None, "note": "not looked up yet", "config": GLS, "config_text": "2024 Mercedes-Benz GLS GLS 450 AWD"}
        ads = [{"stock": "N1", "lifecycle_stage": "active", "vehicle": {"year_make_model": "2023 Mercedes-Benz EQE"},
                "pkg": {"powertrain": pt, "towing": tow}},
               {"stock": "N2", "lifecycle_stage": "active", "vehicle": {}, "pkg": {"powertrain": {"class": "gas", "range": {}}, "towing": {}}}]
        body = o._format_build_summary(ads, [], [], [])
        self.assertIn("RESEARCH NEEDED (2)", body)
        self.assertIn("[N1]  2023 Mercedes-Benz EQE  —  electric range missing (epa_no_entry)", body)
        self.assertIn("candidate: 282 miles (pass 0) https://www.fueleconomy.gov/x", body)
        self.assertIn("tow rating missing (not_looked_up)", body)
        self.assertIn("candidates: none found yet", body)
        self.assertNotIn("[N2]", body.split("RESEARCH NEEDED")[1].split("DELETED")[0])
        self.assertLess(body.index("RESEARCH NEEDED"), body.index("DELETED FOR REBUILD"))
        self.assertIn("RESEARCH NEEDED (0)", o._format_build_summary([], [], [], []))

    def test_set_research_needed_on_the_entry(self):
        e = {}
        R.set_research_needed(e, None, {"triggered": True, "rating": None, "note": "no exact match on a manufacturer page", "config": GLS,
                                        "config_text": "x"})
        self.assertEqual(e["research_needed"][0]["reason"], "no_exact_match")
        R.set_research_needed(e, None, {"triggered": True, "rating": 7700})
        self.assertNotIn("research_needed", e)


AD = {
    "paragraph_one": "This 2024 GLS 450 is ready. It has a clean history.",
    "paragraph_two": "It is a three-row SUV. A 3.0L inline-six engine powers it. It tows up to 7,500 lbs with the hitch. Ships free.",
    "paragraph_three": "Warranty applies.",
    "paragraph_four": "Call us.",
}


class Refresh(unittest.TestCase):
    def setUp(self):
        wipe()
        self.dir = Path(tempfile.mkdtemp(prefix="research_refresh_"))
        self.hist = self.dir / "ad_history.json"
        e = dict(AD, current_ad_text="\n\n".join(AD.values()), verification_verdict="match", last_verified="2026-10-10")
        self.hist.write_text(json.dumps({"S1": e}), encoding="utf-8")
        self.lock = self.dir / "orchestrator.lock"
        self.tow = {"triggered": True, "rating": 7700, "sentence": "It is rated to tow up to 7,700 lbs.", "config": GLS, "config_text": "x"}
        self.pt = {"class": "gas", "range": {}}
        self.patches = [
            mock.patch.object(A, "AD_HISTORY_PATH", self.hist),
            mock.patch.object(A, "_snapshot_vehicle", return_value={"vin": "W1N", "year_make_model": "2024 Mercedes-Benz GLS"}),
            mock.patch.object(TR, "_towing", side_effect=lambda *a, **k: dict(self.tow)),
            mock.patch.object(P, "vehicle_powertrain", side_effect=lambda *a, **k: self.pt),
            mock.patch("run_lock.ORCHESTRATOR_LOCK_PATH", self.lock),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_tow_figure_replaced_without_a_model_call(self):
        with mock.patch.object(A, "_generate_once", side_effect=AssertionError("no model call")):
            plan = R.plan_refresh("S1")
        self.assertTrue(plan["changes"])
        self.assertIn("It is rated to tow up to 7,700 lbs.", plan["after"]["paragraph_two"])
        self.assertNotIn("7,500", plan["after"]["paragraph_two"])
        self.assertEqual(plan["removed"], ["It tows up to 7,500 lbs with the hitch."])
        self.assertEqual(json.loads(self.hist.read_text())["S1"]["paragraph_two"], AD["paragraph_two"], "a plan writes nothing")

    def test_apply_backs_up_writes_and_clears_verification_and_refuses_under_the_lock(self):
        self.lock.write_text("x")
        with self.assertRaises(RuntimeError):
            R.apply_refresh("S1")
        self.lock.unlink()
        out = R.apply_refresh("S1")
        self.assertTrue(out["applied"])
        self.assertTrue((self.dir / out["backup"]).exists())
        e = json.loads(self.hist.read_text())["S1"]
        self.assertIn("7,700 lbs", e["current_ad_text"])
        self.assertEqual((e["verification_verdict"], e["last_verified"]), (None, None), "the verifier re-checks it")
        self.assertIn("It tows up to 7,500 lbs with the hitch.", e["stale_phrases"])
        self.assertFalse(R.apply_refresh("S1")["applied"], "a second refresh changes nothing")

    def test_range_sentence_replaces_a_differing_claim_or_is_added(self):
        self.tow = {"triggered": False, "rating": None}
        self.pt = {"class": P.PHEV, "range": {"miles": 54, "source": "epa"}}
        e = json.loads(self.hist.read_text())
        e["S1"]["paragraph_two"] = "It is a plug-in hybrid. It goes 48 miles on electric range alone. Ships free."
        e["S1"]["current_ad_text"] = "\n\n".join(e["S1"][k] for k in AD)
        self.hist.write_text(json.dumps(e))
        plan = R.plan_refresh("S1")
        self.assertIn("It offers up to 54 miles of EPA-estimated electric range.", plan["after"]["paragraph_two"])
        self.assertNotIn("48 miles", plan["after"]["paragraph_two"])
        e["S1"]["paragraph_two"] = "It is a plug-in hybrid. A 2.0L turbocharged engine pairs with a motor. Ships free."
        e["S1"]["current_ad_text"] = "\n\n".join(e["S1"][k] for k in AD)
        self.hist.write_text(json.dumps(e))
        plan = R.plan_refresh("S1")
        self.assertIn("motor. It offers up to 54 miles of EPA-estimated electric range. Ships", plan["after"]["paragraph_two"])
        self.assertEqual(P.check_claims(plan["after"]["paragraph_two"], P.PHEV, {"miles": 54, "source": "epa",
                                                                                  "phrase": P.range_phrase(54, "epa")}, []), [])


class Site(unittest.TestCase):
    def setUp(self):
        wipe()
        import app as adapp

        self.app = adapp
        adapp.app.config["TESTING"] = True
        self.client = adapp.app.test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s["authed"] = True

    def test_signed_out_and_saving_a_figure(self):
        kid = R.key_id("tow", R.key_of("tow", GLS))
        self.assertEqual(self.client.post("/research/fact", data={"key_id": kid, "figure": "7700"}).status_code, 401)
        self.login()
        bad = self.client.post("/research/fact", data={"key_id": kid, "figure": "7700", "source_url": "https://x.com/a", "quote": ""})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/research/fact", data={"key_id": kid, "figure": "7,700", "source_url": "https://www.mbusa.com/gls",
                                                     "quote": "Towing capacity 7,700 lbs"}).get_json()
        self.assertEqual((ok["saved"], ok["figure"]), (True, 7700))
        self.assertEqual(R.get_fact("tow", GLS)["figure"], 7700)
        self.assertTrue(self.client.post("/research/fact", data={"key_id": kid, "clear": "1"}).get_json()["cleared"])

    def test_the_page_lists_live_cars_with_their_candidates(self):
        R.add_candidates("tow", GLS, [{"figure": 7700, "url": "https://www.mbusa.com/gls", "quote": "Towing 7,700 lbs"}], "prototype")
        item = R.items_for(None, {"triggered": True, "rating": None, "note": "not looked up yet", "config": GLS, "config_text": "2024 GLS 450"})
        hist = {"S1": {"research_needed": item, "year_make_model": "2024 Mercedes-Benz GLS"}, "S2": {"research_needed": item, "absent_since": "2026-10-01"}}
        groups = self.app.research_groups(hist, live={"S1", "S2"})
        self.assertEqual(len(groups), 1)
        self.assertEqual([s["stock"] for s in groups[0]["stocks"]], ["S1"], "a car that left the lot is not listed")
        self.assertEqual(groups[0]["candidates"][0]["figure"], 7700)
        self.login()
        with mock.patch.object(self.app, "research_groups", return_value=groups):
            html = self.client.get("/research").get_data(as_text=True)
        self.assertIn("2024 GLS 450", html)
        self.assertIn('data-figure="7700"', html)
        self.assertIn("Refresh text", html)
        self.assertIn('href="/research">Research</a>', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
