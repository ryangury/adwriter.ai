"""Offline tests: ENGINE_SENTENCE from the sticker (builder, generate guard,
reprice guard) and the sticker header colors (CT23308A). Nothing is saved; no
model call is made."""
import copy
import json
import re
import sqlite3
import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import adwriter as A  # noqa: E402
import aggregator as AG  # noqa: E402
import powertrain as P  # noqa: E402
from scraper import sticker_header_color  # noqa: E402

DB = sqlite3.connect(str(_paths.DATA / "vehicle_cache.db"))
SNAP = {v["stock_number"]: v for v in json.load(open(str(_paths.DATA / "last_inventory_snapshot.json"), encoding="utf-8"))["vehicles"]}
HIST = json.load(open(str(_paths.DATA / "ad_history.json"), encoding="utf-8"))


def sticker(stock):
    row = DB.execute("select window_sticker_json from vehicle_data where vin=?", (SNAP[stock]["vin"],)).fetchone()
    return json.loads(row[0])


SILVERADO = sticker("CT23308A")
SILVERADO_SENTENCE = "Power comes from the 3.0L Duramax turbo-diesel with a 10-speed automatic transmission."


class EngineSentenceTests(unittest.TestCase):
    def test_silverado(self):
        self.assertEqual(P.engine_sentence(SILVERADO["raw_text"]), SILVERADO_SENTENCE)

    def test_only_printed_words(self):
        cases = {
            "XH51984A": "Power comes from the 1.3L Ecotec turbo engine.",            # "SUMMIT WHITE ECOTEC 1.3L TURBO"
            "XH08745B": "Power comes from the 3.6L V6 engine.",                       # "...24V VVT Engine w/ESS Power Windows"
            "PM90574A": "Power comes from the 3.5L twin-turbo V6 engine with an 8-speed automatic transmission.",
            "D23371A": "Power comes from the 2.5L I-4 hybrid engine.",               # "2.5L I-VCT ATK I-4 HYB ENG"
            "PM29466B": "Power comes from the 6.4L HEMI V8 engine.",
        }
        for stock, want in cases.items():
            with self.subTest(stock=stock):
                got = P.engine_sentence(sticker(stock)["raw_text"])
                self.assertEqual(got, want)
                self.assertNotRegex(got, r"(?i)horsepower|\bhp\b|inline|cylinder layout")

    def test_no_unprinted_layout_or_horsepower_on_silverado(self):
        s = P.engine_sentence(SILVERADO["raw_text"])
        self.assertEqual(P.engine_problems(s, P.sticker_engine(SILVERADO["raw_text"])), [])
        self.assertNotRegex(s, r"(?i)inline|six|V6|horsepower|lb-ft")

    def test_mercedes_stickers_print_no_engine(self):
        for stock in ("P14497", "PM87365", "PM32120"):
            with self.subTest(stock=stock):
                self.assertIsNone(P.engine_sentence(sticker(stock)["raw_text"]))

    def test_transfer_case_is_not_the_transmission(self):
        self.assertIsNone(P.sticker_transmission_phrase("2-SPEED AUTOTRAC TRANSFER CASE"))
        self.assertEqual(P.sticker_transmission_phrase("TRANSMISSION: 10-SPEED AUTO"), "10-speed automatic transmission")


def pt_for(stock):
    raw = sticker(stock)["raw_text"]
    info = P.classify(SNAP[stock]["vin"], SNAP[stock]["year_make_model"], None, sticker_text=raw, recon_text="")
    info.update(range={}, flags=[], mild_sentence=None, engine_sentence=P.engine_sentence(raw))
    return info


AD = (
    "This is a Hendrick Certified 2024 Chevrolet Silverado 1500 RST.\n\n"
    "The RST positions itself as the sport-appearance trim. The MultiFlex Tailgate ($445) operates in five positions.\n\n"
    "Paragraph three.\n\nParagraph four."
)


class GenerateGuardTests(unittest.TestCase):
    def test_missing_engine_sentence_is_inserted_after_opening_sentence(self):
        pkg = {"stock_number": "CT23308A", "vehicle": {"status_code": 11, "year_make_model": "2024 Chevrolet Silverado 1500"},
               "powertrain": pt_for("CT23308A"), "towing": {"triggered": False}}
        calls = []
        with mock.patch.object(A, "_generate_once", side_effect=lambda p: (calls.append(1), (AD, "fb"))[1]):
            ad, _ = A._generate_from_package(pkg)
        self.assertEqual(len(calls), 2, "one retry before inserting")
        p2 = A.split_ad_paragraphs(ad)["paragraph_two"]
        units = A._units(p2, [SILVERADO_SENTENCE])
        self.assertEqual(units[1], SILVERADO_SENTENCE)
        self.assertEqual(ad.count(SILVERADO_SENTENCE), 1)

    def test_present_engine_sentence_needs_no_retry(self):
        ad = AD.replace("trim. ", f"trim. {SILVERADO_SENTENCE} ")
        pkg = {"stock_number": "CT23308A", "vehicle": {"status_code": 11}, "powertrain": pt_for("CT23308A"), "towing": {"triggered": False}}
        calls = []
        with mock.patch.object(A, "_generate_once", side_effect=lambda p: (calls.append(1), (ad, "fb"))[1]):
            out, _ = A._generate_from_package(pkg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out.count(SILVERADO_SENTENCE), 1)

    def test_mercedes_gets_no_engine_sentence(self):
        pt = pt_for("P14497")
        self.assertIsNone(pt["engine_sentence"])
        pkg = {"stock_number": "P14497", "vehicle": {"status_code": 10}, "powertrain": pt, "towing": {"triggered": False}}
        with mock.patch.object(A, "_generate_once", return_value=(AD, "fb")):
            A._generate_from_package(pkg)
        self.assertNotIn("engine_sentence", A.required_sentences_from(pkg))


class RepriceGuardTests(unittest.TestCase):
    def test_reprice_requires_it_and_drops_the_old_engine_sentence(self):
        hist = copy.deepcopy(HIST)
        e = hist["CT23308A"]
        old = "Power comes from a 3.0-liter inline-six diesel making 305 horsepower."
        p2 = A._paragraph(e, "paragraph_two")
        first, rest = p2.split(". ", 1)
        e["paragraph_two"] = f"{first}. {old} {rest}"
        e["current_ad_text"] = "\n\n".join([e["paragraph_one"], e["paragraph_two"], e["paragraph_three"], e["paragraph_four"]])
        seen = []

        def fake_completion(client, *, stock, label, system, user, size_text, floor):
            existing = user.split("EXISTING PARAGRAPH TWO:\n", 1)[1].split("\n\nNEW PRICING DATA:", 1)[0]
            seen.append(existing)
            return existing  # the "model" returns the paragraph unchanged

        saved = []
        pd = {"current_price": 40987.0, "advertised_price": 41886.0, "status_code": 11, "mileage": 38141}
        with mock.patch.object(A, "load_ad_history", return_value=hist), \
             mock.patch.object(A, "save_ad_history", side_effect=lambda h: saved.append(h)), \
             mock.patch.object(A, "powertrain_for", return_value=pt_for("CT23308A")), mock.patch.object(A, "towing_for_package", return_value={"triggered": False}), \
             mock.patch.object(A, "_capped_completion", side_effect=fake_completion), \
             mock.patch.object(A.anthropic, "Anthropic"):
            A.reprice_ad("CT23308A", pd)
        new_p2 = saved[-1]["CT23308A"]["paragraph_two"]
        self.assertNotIn(old, seen[0], "old engine sentence removed before the rewrite")
        self.assertEqual(new_p2.count(SILVERADO_SENTENCE), 1)
        self.assertNotIn("inline-six", new_p2)
        self.assertIs(saved[-1], hist)


class ColorTests(unittest.TestCase):
    def test_silverado_header_colors(self):
        raw = SILVERADO["raw_text"]
        self.assertEqual(sticker_header_color(raw, "EXTERIOR"), "Summit White")
        self.assertEqual(sticker_header_color(raw, "INTERIOR"), "Jet Black")

    def test_silverado_resolves_jet_black_not_white(self):
        colors = AG._resolve_colors({"exterior_color": "Summit White"}, SILVERADO, SILVERADO)
        self.assertEqual(colors["interior_color"], "Jet Black")
        self.assertNotEqual(colors["interior_color"].lower(), "white")

    def test_raw_text_scan_reads_forward_only(self):
        raw = "EXTERIOR: SUMMIT WHITE ENG: DURAMAX 3.0L TURBO-DIESEL\nINTERIOR: JET BLACK TRANSMISSION: 10-SPEED AUTO"
        self.assertEqual(AG._interior_from_raw_text(raw), "Black")

    def test_comma_after_transmission_label(self):
        raw = "2025 SILVERADO CREW LT EXTERIOR: SUMMIT WHITE ENGINE: 5.3L ECOTEC3 V8\nINTERIOR: JET BLACK TRANSMISSION, 10-SPEED AUTO"
        self.assertEqual(sticker_header_color(raw, "INTERIOR"), "Jet Black")

    def test_non_color_values_rejected(self):
        self.assertIsNone(sticker_header_color("INTERIOR: GVWR 7,100 LBS", "INTERIOR"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
