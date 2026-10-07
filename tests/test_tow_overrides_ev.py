"""EV tow key, tow overrides (storage, precedence, Database page), the required
TOWING_SENTENCE (generate + reprice), tow_refresh.py and the generate-path
search allow-list flag. Offline: temp DB / overrides / ad_history only."""
import copy
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, r"C:\adwriter")
import adwriter as A  # noqa: E402
import tow_refresh as R  # noqa: E402
import towing as T  # noqa: E402

DB = sqlite3.connect(r"C:\adwriter\vehicle_cache.db")
SNAP = {v["stock_number"]: v for v in json.load(open(r"C:\adwriter\last_inventory_snapshot.json", encoding="utf-8"))["vehicles"]}
HIST = json.load(open(r"C:\adwriter\ad_history.json", encoding="utf-8"))


def raw(stock):
    r = DB.execute("select window_sticker_json from vehicle_data where vin=?", (SNAP[stock]["vin"],)).fetchone()
    return (json.loads(r[0]) if r and r[0] else {}).get("raw_text") or ""


def cfg(stock, pclass=None):
    v = SNAP[stock]
    return T.vehicle_config(v["year_make_model"], v["trim"], v["body_style"], raw(stock), pclass)


class TempStores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(T, "DB_PATH", d / "fc.db"),
                        mock.patch.object(T, "TOW_OVERRIDES_PATH", d / "tow_overrides.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class EvKeyTests(unittest.TestCase):
    def test_tesla_model_x_key(self):
        c = cfg("PS18127A", "bev")
        self.assertTrue(c["ev"])
        self.assertEqual((c["engine"], c["drivetrain"], c["missing"]), ("battery-electric Long Range Plus", "AWD", []))
        page = {"year": "2020", "make": "Tesla", "model": "Model X Long Range Plus", "engine": "electric (dual motor)",
                "drivetrain": "AWD", "cab": "n/a", "bed": "n/a"}
        self.assertEqual(T.match_problems(c, page), [])
        self.assertTrue(T.match_problems(c, dict(page, model="Model X Performance")), "another trim")
        self.assertTrue(T.match_problems(c, dict(page, year="2021")), "another year")

    def test_mercedes_eqe_suv_key(self):
        c = cfg("PM32120", "bev")
        self.assertEqual((c["engine"], c["drivetrain"]), ("battery-electric EQE 500", "AWD"))
        page = {"year": "2023", "make": "Mercedes-Benz", "model": "EQE 500 4MATIC SUV", "engine": "electric",
                "drivetrain": "4MATIC", "cab": "n/a", "bed": "n/a"}
        self.assertEqual(T.match_problems(c, page), [])
        self.assertTrue(any("body" in p for p in T.match_problems(c, dict(page, model="EQE 500 4MATIC Sedan"))), "sedan is not the SUV")
        self.assertTrue(T.match_problems(c, dict(page, engine="2.0L plug-in hybrid")), "not battery-electric")

    def test_gas_car_keeps_engine_key(self):
        self.assertFalse(cfg("CT23308A", "diesel")["ev"])
        self.assertEqual(cfg("CT23308A", "diesel")["engine"], "3.0L Duramax turbo-diesel")


class EvLookupTests(TempStores):
    def test_ev_cached_and_needs_review_fallback(self):
        v = SNAP["PS18127A"]
        args = (v["year_make_model"], v["trim"], v["body_style"], raw("PS18127A"), [])
        self.assertFalse(T.towing_for(*args, powertrain_class="bev", allow_lookup=False)["triggered"])
        with mock.patch.object(T, "lookup", return_value={"lbs": None, "note": "no exact match"}):
            t = T.towing_for(*args, powertrain_class="bev", force=True)
        self.assertIsNone(t["rating"])
        self.assertEqual(t["note"], "no exact match")
        with mock.patch.object(T, "lookup", return_value={"lbs": 5000, "url": "https://www.tesla.com/x", "note": None}):
            with mock.patch.object(T, "get_cached", return_value=None):
                t = T.towing_for(*args, powertrain_class="bev", force=True)
        self.assertEqual((t["rating"], t["sentence"]), (5000, "It is rated to tow up to 5,000 lbs."))


class OverrideTests(TempStores):
    def test_set_load_remove(self):
        e = T.set_tow_override("1gcudee84rz306239", 9000, "Chevrolet 2024 trailering guide p.12")
        self.assertEqual(e["rating"], 9000)
        self.assertEqual(T.load_tow_overrides()["1GCUDEE84RZ306239"]["source"], "Chevrolet 2024 trailering guide p.12")
        T.set_tow_override("1GCUDEE84RZ306239", None)
        self.assertEqual(T.load_tow_overrides(), {})
        with self.assertRaises(ValueError):
            T.set_tow_override("X", 12)

    def test_override_beats_lookup_cache_and_incomplete_config(self):
        v = SNAP["XH08745B"]  # Gladiator: no cab / bed on the sticker
        T.set_tow_override(v["vin"], 6000, "Jeep 2021 Gladiator spec sheet")
        with mock.patch.object(T, "lookup") as lk:
            t = T.towing_for(v["year_make_model"], v["trim"], v["body_style"], raw("XH08745B"), [], vin=v["vin"])
        lk.assert_not_called()
        self.assertEqual((t["rating"], t["sentence"]), (6000, "It is rated to tow up to 6,000 lbs."))
        self.assertTrue(t["override"])

    def test_database_page_saves_and_shows_override(self):
        import app as adapp

        client = adapp.app.test_client()
        with client.session_transaction() as s:
            s["authed"] = True
        r = client.post("/cache/CT23308A/towing", data={"rating": "9,000", "source": "Chevrolet trailering guide"})
        self.assertEqual(r.get_json(), {"saved": True, "override": True, "rating": 9000})
        page = client.get("/cache/CT23308A").get_data(as_text=True)
        self.assertIn("9,000 lbs", page)
        self.assertIn("set by override", page)
        self.assertEqual(client.post("/cache/CT23308A/towing", data={"rating": "9000"}).status_code, 400, "source required")
        self.assertEqual(client.post("/cache/CT23308A/towing", data={"rating": "abc", "source": "x"}).status_code, 400)
        self.assertEqual(client.post("/cache/CT23308A/towing", data={"rating": "", "source": ""}).get_json()["override"], False)


TOW7700 = {"triggered": True, "rating": 7700, "sentence": "It is rated to tow up to 7,700 lbs.", "config_text": "c", "source_url": "u"}
AD = ("This is a 2025 Mercedes-Benz GLE 450.\n\nThe GLE 450 opens the lineup. The factory Trailer Hitch is rated for "
      "7,716 lbs of towing capacity. Air suspension is standard.\n\nP3.\n\nP4.")


class SentenceTests(unittest.TestCase):
    def _gen(self, ad, tow):
        pkg = {"stock_number": "X58848", "vehicle": {"status_code": 10}, "towing": tow,
               "powertrain": {"class": "mild_hybrid", "flags": [], "range": {}}}
        calls = []
        with mock.patch.object(A, "_generate_once", side_effect=lambda p: (calls.append(1), (ad, "fb"))[1]), \
             mock.patch.object(A, "_sticker_text_for", return_value=""):
            out, fb = A._generate_from_package(pkg)
        return out, calls

    def test_required_and_inserted_after_retry_wrong_figure_removed(self):
        out, calls = self._gen(AD, TOW7700)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("7,716", out)
        self.assertEqual(out.count("It is rated to tow up to 7,700 lbs."), 1)

    def test_present_sentence_passes(self):
        ad = AD.replace("The factory Trailer Hitch is rated for 7,716 lbs of towing capacity.", "It is rated to tow up to 7,700 lbs.")
        out, calls = self._gen(ad, TOW7700)
        self.assertEqual(len(calls), 1)

    def test_package_lines_carry_the_sentence(self):
        lines = "\n".join(A.towing_package_lines(dict(TOW7700, override={"rating": 7700})))
        self.assertIn("TOWING_SENTENCE", lines)
        self.assertIn("set by a manual override", lines)

    def test_reprice_states_the_figure_once(self):
        hist = copy.deepcopy(HIST)
        saved = []

        def fake(client, *, stock, label, system, user, size_text, floor):
            return user.split("EXISTING PARAGRAPH TWO:\n", 1)[1].split("\n\nNEW PRICING DATA:", 1)[0]

        with mock.patch.object(A, "load_ad_history", return_value=hist), \
             mock.patch.object(A, "save_ad_history", side_effect=lambda h: saved.append(h)), \
             mock.patch.object(A, "towing_for_package", return_value=TOW7700), \
             mock.patch.object(A, "_capped_completion", side_effect=fake), mock.patch.object(A.anthropic, "Anthropic"):
            A.reprice_ad("X58848", {"current_price": 60000.0, "advertised_price": 60899.0, "status_code": 10, "mileage": 9000})
        p2 = saved[-1]["X58848"]["paragraph_two"]
        self.assertEqual(p2.count("7,700"), 1)
        self.assertIn("It is rated to tow up to 7,700 lbs.", p2)


class TowRefreshTests(unittest.TestCase):
    def test_kg_flag(self):
        self.assertEqual(R.kg_conversion(7716), 3500)
        self.assertIsNone(R.kg_conversion(7700))
        self.assertIsNone(R.kg_conversion(5950))

    def test_edits(self):
        e = {"paragraph_one": "P1.", "paragraph_two": "Opening. Power comes from the 3.0L engine. It tows 7,716 lbs with the hitch. Closing.",
             "paragraph_three": "P3.", "paragraph_four": "P4."}
        rep, removed = R._edit(e, "replace", "It is rated to tow up to 7,700 lbs.")
        self.assertIn("It is rated to tow up to 7,700 lbs.", rep["after"]["paragraph_two"])
        self.assertNotIn("7,716", rep["after"]["paragraph_two"])
        self.assertEqual(removed, ["It tows 7,716 lbs with the hitch."])
        rem, _ = R._edit(e, "remove", None)
        self.assertNotIn("lbs", rem["after"]["paragraph_two"])
        gap = {**e, "paragraph_two": "Opening. Power comes from the 3.0L engine. Closing."}
        add, _ = R._edit(gap, "add", "It is rated to tow up to 9,000 lbs.")
        self.assertIn("engine. It is rated to tow up to 9,000 lbs.", add["after"]["paragraph_two"])

    # A fixed, synthetic set of ads and inventory: the live data moves every day
    # (runs reprice and regenerate ads), so these tests never read it.
    ADS = {
        "S1": "Opening. Power comes from the GLS 450. The factory Trailer Hitch is rated for 7,716 lbs of towing capacity. Closing.",
        "S2": "Opening. It is rated to tow up to 7,700 lbs. Closing.",
        "S3": "Opening. A front trunk adds practical storage, and the factory tow rating on this model is 5,000 pounds. Closing.",
        "S4": "Opening. Power comes from the 3.0L Duramax turbo-diesel. Closing.",
        "S5": "Opening. The factory hitch is rated for 7,700 lbs. Closing.",
        "S6": "Opening. Air suspension is standard. Closing.",
    }
    RATINGS = {"S1": 7700, "S2": 7700, "S3": None, "S4": 9000}  # S5: not looked up yet; S6: can't tow

    def _fixture(self, d):
        hist = {s: {"current_ad_text": f"P1.\n\n{p2}\n\nP3.\n\nP4.", "paragraph_one": "P1.", "paragraph_two": p2,
                    "paragraph_three": "P3.", "paragraph_four": "P4.", "verification_verdict": "current",
                    "last_feedback": ""} for s, p2 in self.ADS.items()}
        snap = {"vehicles": [{"stock_number": s, "vin": f"VIN{s}", "year_make_model": "2026 Mercedes-Benz GLS",
                              "trim": "GLS 450 AWD", "body_style": "SUV", "status_code": 10} for s in self.ADS]}
        hp, sp = Path(d) / "ad_history.json", Path(d) / "snap.json"
        hp.write_text(json.dumps(hist), encoding="utf-8")
        sp.write_text(json.dumps(snap), encoding="utf-8")
        return hp, sp

    def _fake_towing(self, stock, v, allow_lookup):
        if stock == "S6":
            return {"triggered": False, "rating": None, "note": None, "config": {}, "config_text": stock,
                    "sentence": None, "override": None, "sticker_triggers": False, "source_url": None}
        if stock not in self.RATINGS:
            return {"triggered": True, "rating": None, "note": "not looked up yet",
                    "config": T.vehicle_config(v["year_make_model"], v["trim"], v["body_style"], ""),
                    "config_text": stock, "sentence": None, "override": None, "sticker_triggers": True, "source_url": None}
        r = self.RATINGS[stock]
        return {"triggered": True, "rating": r, "note": None if r else "no exact match", "config": {}, "config_text": stock,
                "sentence": T.towing_sentence(r), "override": None, "sticker_triggers": True, "source_url": "https://x"}

    def _run(self, fn):
        with tempfile.TemporaryDirectory() as d:
            hp, sp = self._fixture(d)
            lock = Path(d) / "orchestrator.lock"
            with mock.patch.object(R, "_towing", side_effect=self._fake_towing), \
                 mock.patch.object(R, "SNAPSHOT_PATH", sp), mock.patch.object(R, "_old_cache_rows", return_value=[]), \
                 mock.patch.object(A, "AD_HISTORY_PATH", hp), mock.patch.object(R, "ORCHESTRATOR_LOCK_PATH", lock), \
                 mock.patch.object(R.shutil, "copy2"):
                return fn(hp, lock)

    def test_groups_and_actions(self):
        rows, pending = self._run(lambda hp, lock: R.plan(allow_lookup=False))
        by = {r["stock"]: r for r in rows}
        self.assertEqual((by["S1"]["group"], by["S1"]["action"]), ("overstated", "replace"))
        self.assertEqual(by["S1"]["kg"], {7716: 3500})
        self.assertEqual((by["S2"]["group"], by["S2"]["action"], by["S2"]["changes"]), ("matches", "keep", False))
        self.assertEqual((by["S3"]["group"], by["S3"]["action"]), ("unverifiable", "remove"))
        self.assertIn("A front trunk adds practical storage.", by["S3"]["after"]["paragraph_two"], "only the tow clause goes")
        self.assertEqual((by["S4"]["group"], by["S4"]["action"]), ("gaps", "add"))
        self.assertEqual((by["S5"]["action"], by["S5"]["changes"]), ("pending lookup", False))
        self.assertNotIn("S6", by)
        self.assertEqual(len(pending), 1)

    def test_apply_writes_only_changed_ads_and_respects_the_lock(self):
        def go(hp, lock):
            rows, _ = R.plan(allow_lookup=False)
            before = json.loads(hp.read_text(encoding="utf-8"))
            lock.write_text("1")
            with self.assertRaises(SystemExit):
                R.apply(rows)
            lock.unlink()
            n = R.apply([r for r in rows if r["stock"] in ("S1", "S2", "S5")])
            return n, before, json.loads(hp.read_text(encoding="utf-8"))
        n, before, after = self._run(go)
        self.assertEqual(n, 1)
        self.assertEqual(after["S2"], before["S2"], "a keep is untouched")
        self.assertEqual(after["S5"], before["S5"], "pending lookup is untouched")
        e = after["S1"]
        self.assertIn("It is rated to tow up to 7,700 lbs.", e["current_ad_text"])
        self.assertNotIn("7,716", e["current_ad_text"])
        self.assertTrue(any("7,716" in s for s in e["stale_phrases"]))
        self.assertIsNone(e["verification_verdict"])
        changed = {s for s in after if after[s] != before[s]}
        self.assertEqual(changed, {"S1"})


class AllowlistFlagTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(A.GENERATE_SEARCH_ALLOWLIST)
        self.assertNotIn("allowed_domains", A.web_search_tool_for("Mercedes-Benz"))

    def test_on(self):
        with mock.patch.object(A, "GENERATE_SEARCH_ALLOWLIST", True):
            tool = A.web_search_tool_for("Mercedes-Benz")
            self.assertIn("mbusa.com", tool["allowed_domains"])
            self.assertIn("fueleconomy.gov", tool["allowed_domains"])
            self.assertNotIn("blocked_domains", tool)
            self.assertNotIn("allowed_domains", A.web_search_tool_for("Studebaker"), "unknown make stays unrestricted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
