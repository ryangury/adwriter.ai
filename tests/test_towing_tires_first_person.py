"""Offline tests: first-person filter, verified towing, tire-size check.
No network, no model call; the tow cache runs against a temp DB."""
import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, r"C:\adwriter")
import adwriter as A  # noqa: E402
import orchestrator as O  # noqa: E402
import towing as T  # noqa: E402

DB = sqlite3.connect(r"C:\adwriter\vehicle_cache.db")
SNAP = {v["stock_number"]: v for v in json.load(open(r"C:\adwriter\last_inventory_snapshot.json", encoding="utf-8"))["vehicles"]}
HIST = json.load(open(r"C:\adwriter\ad_history.json", encoding="utf-8"))


def raw(stock):
    return json.loads(DB.execute("select window_sticker_json from vehicle_data where vin=?", (SNAP[stock]["vin"],)).fetchone()[0])["raw_text"]


def cfg(stock):
    v = SNAP[stock]
    return T.vehicle_config(v["year_make_model"], v["trim"], v["body_style"], raw(stock))


class FirstPersonTests(unittest.TestCase):
    def test_roman_numerals_and_words_ending_in_I_pass(self):
        for s in ("The Class II Trailer Tow Package adds a receiver.", "Driver Assistance Package I adds sensors.",
                  "The Premium Package II adds seats.", "Phase I brought new lights.",
                  "The Convenience Package II (Bose) adds a seven-speaker system.",
                  "The 5.7-liter HEMI V8 makes 375 horsepower.", "MMI Navigation plus is standard.",
                  "Exit off I-40 at the Hendrick Automotive Mall."):
            self.assertFalse(A._is_first_person(s), s)

    def test_pronouns_strip(self):
        for s in ("I searched the site.", "I couldn't find a figure.", "I'm using J.D. Power.", "I've confirmed it.",
                  "I'll note that.", "I'd recommend the hitch.", "Here I found 9,000 lbs.", "I could not confirm."):
            self.assertTrue(A._is_first_person(s), s)

    def test_filter_keeps_package_ii_sentence(self):
        text = "Paragraph one is fine.\n\nThe Convenience Package II ($1,340) adds Bose audio. I searched for the price."
        out = A._strip_reasoning_sentences(text)
        self.assertIn("Convenience Package II", out)
        self.assertNotIn("I searched", out)


class ConfigTests(unittest.TestCase):
    def test_silverado(self):
        c = cfg("CT23308A")
        self.assertEqual((c["engine"], c["drivetrain"], c["cab"], c["bed"]), ("3.0L Duramax turbo-diesel", "4WD", "crew", "short"))
        self.assertEqual(c["missing"], [])

    def test_escape_and_mercedes(self):
        self.assertEqual(cfg("D23371A")["engine"], "2.5L I-4 hybrid")
        self.assertEqual(cfg("X58848")["engine"], "GLE 450")
        self.assertEqual(cfg("XH08745B")["missing"], ["cab", "bed"])

    def test_trigger(self):
        self.assertTrue(T.triggered("TRAILERING PACKAGE", []))
        self.assertTrue(T.triggered("", ["Class II Trailer Tow Package"]))
        self.assertFalse(T.triggered("PANORAMIC ROOF", ["Night Package"]))


SILV_PAGE = {"year": "2024", "make": "Chevrolet", "model": "Silverado 1500", "engine": "3.0L Duramax Turbo-Diesel I6",
             "drivetrain": "4WD", "cab": "Crew Cab", "bed": "Short Bed"}


class MatchTests(unittest.TestCase):
    def test_exact_silverado_page_matches(self):
        self.assertEqual(T.match_problems(cfg("CT23308A"), SILV_PAGE), [])

    def test_the_9000_source_does_not_match(self):
        page = dict(SILV_PAGE, bed="Standard Bed (147\")")
        self.assertTrue(any("bed" in p for p in T.match_problems(cfg("CT23308A"), page)))

    def test_the_3500_source_does_not_match(self):
        page = {"year": "2024", "make": "Ford", "model": "Escape", "engine": "2.0L EcoBoost", "drivetrain": "AWD", "cab": "n/a", "bed": "n/a"}
        probs = T.match_problems(cfg("D23371A"), page)
        self.assertTrue(any("year" in p for p in probs) and any("engine" in p for p in probs), probs)

    def test_hybrid_vs_gas_and_drivetrain(self):
        page = {"year": "2023", "make": "Ford", "model": "Escape", "engine": "2.5L iVCT Hybrid", "drivetrain": "FWD", "cab": "", "bed": ""}
        self.assertTrue(any("drivetrain" in p for p in T.match_problems(cfg("D23371A"), page)))
        page["drivetrain"] = "AWD"
        self.assertEqual(T.match_problems(cfg("D23371A"), page), [])
        page["engine"] = "2.5L iVCT"
        self.assertTrue(T.match_problems(cfg("D23371A"), page))

    def test_mercedes_designation(self):
        page = {"year": "2025", "make": "Mercedes-Benz", "model": "GLE 450 4MATIC SUV", "engine": "3.0L inline-6 turbo with EQ Boost",
                "drivetrain": "4MATIC all-wheel drive", "cab": "n/a", "bed": "n/a"}
        self.assertEqual(T.match_problems(cfg("X58848"), page), [])
        self.assertTrue(T.match_problems(cfg("X58848"), dict(page, model="GLE 350 4MATIC SUV")))

    def test_parse_reply_domain_and_seen(self):
        c = cfg("CT23308A")
        doms = T.allowed_domains("Chevrolet")
        line = ("TOW :: 9,000 :: 2024 :: Chevrolet :: Silverado 1500 :: 3.0L Duramax Turbo-Diesel :: 4WD :: Crew Cab :: Short Bed :: "
                "Trailering Package :: https://www.chevrolet.com/trucks/silverado/1500/specs")
        ok = T.parse_reply(line, c, doms, {"https://www.chevrolet.com/trucks/silverado/1500/specs"})
        self.assertEqual(ok["lbs"], 9000)
        maxpkg = line.replace(":: 9,000 ::", ":: 13,000 ::").replace("Trailering Package ::", "Max Trailering Package ::")
        rej = T.parse_reply(maxpkg, c, doms, {"https://www.chevrolet.com/trucks/silverado/1500/specs"})
        self.assertIsNone(rej["lbs"], "a Max Trailering rating is not this truck's")
        self.assertIn("Max Trailering", rej["note"])
        self.assertIsNone(T.parse_reply(line.replace("www.chevrolet.com", "gmauthority.com"), c, doms, {"https://gmauthority.com/trucks/silverado/1500/specs"})["lbs"])
        self.assertIsNone(T.parse_reply(line, c, doms, set())["lbs"], "page not among results")
        self.assertIsNone(T.parse_reply(line.replace(":: 13,000 ::", ":: NONE ::"), c, doms, {"x"})["lbs"])


class PackageTests(unittest.TestCase):
    def test_tow_equipment_printed(self):
        self.assertEqual(cfg("CT23308A")["tow_package"], "TRAILERING PACKAGE")
        self.assertIn("CLASS II TRAILER TOW PACKAGE", cfg("D23371A")["tow_package"])
        self.assertIn("TRAILER HITCH", cfg("X58848")["tow_package"])

    def test_package_problem(self):
        self.assertIsNone(T.package_problem("none", "TRAILERING PACKAGE"))
        self.assertIsNone(T.package_problem("Trailering Package", "TRAILERING PACKAGE"))
        self.assertIsNotNone(T.package_problem("Max Trailering Package", "TRAILERING PACKAGE"))
        self.assertIsNone(T.package_problem("Class II Trailer Tow Package", "CLASS II TRAILER TOW PACKAGE"))
        self.assertIsNotNone(T.package_problem("Class IV Trailer Tow Package", "CLASS II TRAILER TOW PACKAGE"))
        self.assertIsNone(T.package_problem("Trailer Hitch (option 550)", "INCREASED TOWING; TRAILER HITCH"))
        self.assertIsNotNone(T.package_problem("Trailer Hitch", ""))


class CacheAndEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(T, "DB_PATH", Path(self.tmp.name) / "fc.db")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def _for(self, stock, **kw):
        v = SNAP[stock]
        return T.towing_for(v["year_make_model"], v["trim"], v["body_style"], raw(stock), [], **kw)

    def test_lookup_cached_by_full_key(self):
        with mock.patch.object(T, "lookup", return_value={"lbs": 13000, "url": "https://www.chevrolet.com/x", "page_config": "p", "note": None}) as lk:
            a = self._for("CT23308A")
            b = self._for("CT23308A")
        self.assertEqual((a["rating"], b["rating"]), (13000, 13000))
        self.assertEqual(lk.call_count, 1, "second call is a cache hit")

    def test_no_match_omits_and_notes(self):
        with mock.patch.object(T, "lookup", return_value={"lbs": None, "url": "https://gmauthority.com/x", "note": "rejected: gmauthority.com is not ..."}):
            t = self._for("CT23308A")
        self.assertIsNone(t["rating"])
        self.assertIn("rejected", t["note"])

    def test_incomplete_config_never_looks_up(self):
        with mock.patch.object(T, "lookup") as lk:
            t = self._for("XH08745B")
        lk.assert_not_called()
        self.assertIn("no cab, bed", t["note"])


AD = ("This is a 2023 Ford Escape Platinum.\n\nThe Platinum is the top trim. The Class II Trailer Tow Package adds a hitch, "
      "and the Escape is rated to tow up to 3,500 lbs. The tires are LT275/60R20 all-terrains.\n\nP3.\n\nP4.")


class GuardTests(unittest.TestCase):
    def _gen(self, tow, sticker="2023 ESCAPE PLATINUM"):
        pkg = {"stock_number": "D23371A", "vehicle": {"status_code": 11}, "towing": tow,
               "powertrain": {"class": "hybrid", "flags": [], "range": {}}}
        calls = []
        with mock.patch.object(A, "_generate_once", side_effect=lambda p: (calls.append(1), (AD, "fb"))[1]), \
             mock.patch.object(A, "_sticker_text_for", return_value=sticker):
            ad, fb = A._generate_from_package(pkg)
        return ad, fb, calls

    def test_unverified_tow_and_tire_are_retried_then_removed(self):
        ad, fb, calls = self._gen({"triggered": True, "rating": None, "config_text": "2023 Ford Escape ...", "note": "no exact match"})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("3,500", ad)
        self.assertNotIn("LT275/60R20", ad)
        self.assertIn("Class II", ad.split("\n\n")[1] if "Class II" in ad else "Class II")  # the package may still be named
        self.assertIn("TOWING RATING NEEDS REVIEW", fb)

    def test_verified_figure_and_printed_tire_size_pass(self):
        ad, fb, calls = self._gen({"triggered": True, "rating": 3500, "config_text": "x", "source_url": "https://ford.com/x"},
                                  sticker="ALL-TERRAIN TIRES LT275/60R20")
        self.assertEqual(len(calls), 1)
        self.assertIn("3,500 lbs", ad)
        self.assertIn("LT275/60R20", ad)

    def test_package_lines(self):
        self.assertIn("State no towing figure", "\n".join(A.towing_package_lines({"triggered": True, "rating": None, "config_text": "c", "note": "n"})))
        self.assertIn("TOWING CAPACITY: 9,000 lbs", "\n".join(A.towing_package_lines({"triggered": True, "rating": 9000, "config_text": "c", "source_url": "u"})))
        self.assertEqual(A.towing_package_lines({"triggered": False}), [])


class RepriceAndReportTests(unittest.TestCase):
    def test_reprice_drops_unverified_tow_before_rewrite(self):
        hist = copy.deepcopy(HIST)
        e = hist["D23371A"]
        p2 = A._paragraph(e, "paragraph_two")
        e["paragraph_two"] = p2 + " The Escape is rated to tow up to 3,500 lbs with the tow package."
        seen, saved = [], []

        def fake(client, *, stock, label, system, user, size_text, floor):
            ex = user.split("EXISTING PARAGRAPH TWO:\n", 1)[1].split("\n\nNEW PRICING DATA:", 1)[0]
            seen.append(ex)
            return ex

        tow = {"triggered": True, "rating": None, "config_text": "2023 Ford Escape 2.5L I-4 hybrid AWD", "note": "no exact match"}
        with mock.patch.object(A, "load_ad_history", return_value=hist), \
             mock.patch.object(A, "save_ad_history", side_effect=lambda h: saved.append(h)), \
             mock.patch.object(A, "towing_for_package", return_value=tow), \
             mock.patch.object(A, "_capped_completion", side_effect=fake), mock.patch.object(A.anthropic, "Anthropic"):
            A.reprice_ad("D23371A", {"current_price": 26731.0, "advertised_price": 27630.0, "status_code": 11, "mileage": 40030})
        self.assertNotIn("3,500", seen[0])
        self.assertNotIn("3,500", saved[-1]["D23371A"]["paragraph_two"])
        self.assertIn("2023 Ford Escape", saved[-1]["D23371A"]["towing_review"])

    def test_action_required_section(self):
        body = O._format_action_email([], [], [], set(), [], [], [], [], [
            {"stock_number": "D23371A", "year_make_model": "2023 Ford Escape", "note": "2023 Ford Escape 2.5L I-4 hybrid AWD: no exact match"}])
        self.assertIn("TOWING RATING NEEDS REVIEW (1)", body)
        self.assertIn("[D23371A]", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
