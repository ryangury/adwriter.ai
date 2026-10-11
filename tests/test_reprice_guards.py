"""Activation guards for Sonnet 5.5 on the non-MB side: the reprice survival check (every sentence of the old
paragraph two that is not Python-built survives verbatim; one retry, then the old text is kept and the vehicle
flagged), the package-coverage warning (log only), and the generated_with tag on generate and reprice. Built on real
outputs: the 2026-10-11 PM87365 reprice (5.5 dropped a clause) and the Silverado's two paragraph twos. Offline."""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import adwriter as A  # noqa: E402
import model_config as M  # noqa: E402

FX = Path(__file__).parent / "fixtures"
REP = json.loads((FX / "reprice_pm87365.json").read_text(encoding="utf-8"))
SIL = json.loads((FX / "silverado_p2s.json").read_text(encoding="utf-8"))
DROPPED = ("The warm brown against the grey exterior reads as sophisticated rather than sporty, and it is not a pairing "
           "most buyers find sitting on a CPO lot.")


class Survival(unittest.TestCase):
    def test_the_dropped_clause_on_pm87365_is_caught(self):
        self.assertEqual(A.reprice_lost_sentences(REP["old_p2"], REP["new_p2_55"], {}), [DROPPED])
        self.assertEqual(A.reprice_lost_sentences(REP["old_p2"], REP["new_p2_46"], {}), [], "4.6 kept every sentence")

    def test_dollar_figures_may_change_python_built_sentences_are_exempt(self):
        old = ("Selenite Grey is rare. Original MSRP was $63,915. At $42,000, this represents $21,915 in depreciation the "
               "next buyer does not absorb. Current asking price is $42,899 (includes $899 dealer administrative fee), "
               "$4,376 below J.D. Power Typical Listing Price for comparable units.")
        new = ("Selenite Grey is rare. Original MSRP was $63,915. At $41,774, this represents $22,141 in depreciation the "
               "next buyer does not absorb. Current asking price is $41,774 (includes $899 dealer administrative fee), "
               "$5,275 below Kelley Blue Book Typical Listing Price for comparable units.")
        self.assertEqual(A.reprice_lost_sentences(old, new, {}), [])
        self.assertEqual(A.reprice_lost_sentences(old, new.replace("Selenite Grey is rare.", "Selenite Grey is uncommon."), {}),
                         ["Selenite Grey is rare."], "no-dollar sentences are verbatim")
        reworded_price = new.replace("in depreciation the next buyer does not absorb", "of depreciation already taken")
        self.assertEqual(len(A.reprice_lost_sentences(old, reworded_price, {})), 1, "a $ sentence may change its numbers only")
        warranty_old = "CARFAX estimates about 30 months remain on the original Mercedes-Benz basic warranty."
        warranty_new = "CARFAX estimates about 27 months remain on the original Mercedes-Benz basic warranty."
        self.assertEqual(A.reprice_lost_sentences(warranty_old, warranty_new, {"factory_warranty_sentence": warranty_new}), [])


HIST = {"PM87365": {
    "paragraph_one": "This 2024 C 300 is ready.", "paragraph_two": REP["old_p2"], "paragraph_three": "P3.", "paragraph_four": "P4.",
    "current_ad_text": "This 2024 C 300 is ready.\n\n" + REP["old_p2"] + "\n\nP3.\n\nP4.", "first_ad_date": "2026-10-01",
    "last_ad_date": "2026-10-01", "ad_count": 1,
}}
PRICING = {"current_price": 40875.0, "advertised_price": 41774.0, "status_code": 11, "mileage": 9000}


class RepriceEndToEnd(unittest.TestCase):
    def run_reprice(self, outputs, status=11):
        hist = copy.deepcopy(HIST)
        saved, flags, calls = [], [], []
        queue = list(outputs)

        def fake(client, *, stock, label, system, user, size_text, floor, status_code=None):
            calls.append(status_code)
            return queue.pop(0)

        tow = {"triggered": False, "rating": None}
        with mock.patch.object(A, "load_ad_history", return_value=hist), \
             mock.patch.object(A, "save_ad_history", side_effect=lambda h: saved.append(copy.deepcopy(h))), \
             mock.patch.object(A, "towing_for_package", return_value=tow), \
             mock.patch.object(A, "powertrain_for", return_value={"class": "gas", "range": {}, "flags": []}), \
             mock.patch.object(A, "flag_generation_problem", side_effect=lambda *a: flags.append(a)), \
             mock.patch.object(A, "_capped_completion", side_effect=fake), mock.patch.object(A.anthropic, "Anthropic"):
            try:
                out = A.reprice_ad("PM87365", dict(PRICING, status_code=status))
                err = None
            except A.RepriceSurvivalError as exc:
                out, err = None, exc
        return out, err, saved, flags, calls

    def test_twice_dropped_keeps_the_old_paragraph_and_flags(self):
        out, err, saved, flags, calls = self.run_reprice([REP["new_p2_55"], REP["new_p2_55"]])
        self.assertIsNotNone(err)
        self.assertIsInstance(err, A.TruncatedGenerationError, "handled like a truncated reprice: old text kept")
        self.assertEqual(saved, [], "nothing written: the old paragraph stays")
        self.assertEqual(flags[0][1], "reprice_dropped_sentences")
        self.assertIn("sophisticated rather than sporty", flags[0][2])
        self.assertEqual(calls, [11, 11], "one retry, on the non-MB flag")

    def test_a_good_retry_is_kept_and_tagged(self):
        out, err, saved, flags, calls = self.run_reprice([REP["new_p2_55"], REP["new_p2_46"]])
        self.assertIsNone(err)
        e = saved[-1]["PM87365"]
        self.assertIn(DROPPED, e["paragraph_two"])
        self.assertEqual(e["generated_with"], {"model": "claude-sonnet-5-5", "effort": "medium"})
        out, err, saved, *_ = self.run_reprice([REP["new_p2_46"], REP["new_p2_46"]], status=10)
        self.assertEqual(saved[-1]["PM87365"]["generated_with"], {"model": "claude-sonnet-4-6", "effort": None})


class PackageCoverage(unittest.TestCase):
    def pkg(self):
        return {"msrp_data": {"option_packages": SIL["option_packages"]}}

    def test_silverado_both_models_name_every_package_of_750_or_more(self):
        for key in ("p2_46", "p2_55"):
            ad = "P1.\n\n" + SIL[key] + "\n\nP3.\n\nP4."
            self.assertEqual(A.package_coverage_missing(ad, self.pkg()), [], key)

    def test_a_missing_package_is_listed_and_cheap_ones_are_ignored(self):
        p2 = SIL["p2_55"].replace("Assist steps ($950) make getting into the cab easier, and the", "The").replace(
            "The Protection Package ($685) brings", "A kit brings")
        missing = A.package_coverage_missing("P1.\n\n" + p2 + "\n\nP3.\n\nP4.", self.pkg())
        self.assertEqual(missing, [{"name": "ASSIST STEPS", "price": 950.0}], "the $685 Protection Package is under $750")

    def test_the_warning_reaches_the_feedback_block_and_the_build_summary_and_never_blocks(self):
        import orchestrator as o

        pkg = dict(self.pkg(), stock_number="CT23308A", vehicle={"status_code": 11, "year_make_model": "2024 Chevrolet Silverado 1500"},
                   powertrain={"class": "diesel", "range": {}, "flags": []}, towing={"triggered": False})
        p2 = SIL["p2_55"].replace("Assist steps ($950) make getting into the cab easier, and the", "The")
        ad = "P1 one. P1 two.\n\n" + p2 + "\n\nP3.\n\nP4."

        def once(p):
            A.GENERATED_WITH["CT23308A"] = M.tag(M.SONNET_55)
            return ad, "CONFIDENCE: HIGH"

        with mock.patch.object(A, "_generate_once", side_effect=once), mock.patch.object(A, "_sticker_text_for", return_value=""), \
             mock.patch.object(A, "find_tool_output_leaks", return_value=[]), mock.patch.object(A, "find_banned_scarcity_phrases", return_value=[]), \
             mock.patch.object(A, "missing_required_sentences", return_value=[]), mock.patch.object(A, "check_claims", return_value=[]), \
             mock.patch.object(A, "fact_check_problems", return_value=([], [])), mock.patch.object(A, "history_claim_problems", return_value=[]), \
             mock.patch.object(A, "package_price_missing", return_value=[]), mock.patch.object(A, "standard_equipment_missing", return_value=None):
            # (a package named without its price is test_package_prices.py's: retried, then its price is inserted)
            out_ad, fb = A._generate_from_package(pkg)
        self.assertEqual(out_ad.split("\n\n")[1], p2, "an unnamed package is a warning: it never changes or blocks the ad")
        self.assertIn("PACKAGE COVERAGE (warning only)", fb)
        self.assertIn("ASSIST STEPS ($950)", fb)
        self.assertTrue(fb.rstrip().endswith("MODEL: claude-sonnet-5-5 (effort medium)"))
        body = o._format_build_summary([{"stock": "CT23308A", "lifecycle_stage": "active", "vehicle": pkg["vehicle"], "pkg": pkg}], [], [], [])
        self.assertIn("PACKAGES NOT NAMED IN PARAGRAPH TWO (warning only) (1)", body)
        self.assertIn("[CT23308A]", body.split("PACKAGES NOT NAMED")[1].split("DELETED")[0])


class Tags(unittest.TestCase):
    def test_record_ad_stores_generated_with_and_the_reprice_feedback_shows_it(self):
        import orchestrator as o

        A.GENERATED_WITH["S9"] = M.tag(M.SONNET_55)
        hist = {}
        A.record_ad(hist, "S9", ad_text="P1.\n\nP2.\n\nP3.\n\nP4.", price=1, lifecycle_stage="active", recon_included=False,
                    recon_pending=False)
        self.assertEqual(hist["S9"]["generated_with"], {"model": "claude-sonnet-5-5", "effort": "medium"})
        self.assertNotIn("S9", A.GENERATED_WITH)
        A.record_ad(hist, "S9", ad_text="P1.\n\nP2.\n\nP3.\n\nP4.", price=1, lifecycle_stage="active", recon_included=False,
                    recon_pending=False)
        self.assertNotIn("generated_with", hist["S9"], "an untagged rewrite does not keep a stale tag")
        with mock.patch.object(o, "load_ad_history", return_value={"R1": {"generated_with": M.tag(M.SONNET_46)}}):
            self.assertEqual(o._stored_flags_feedback("R1"), "MODEL: claude-sonnet-4-6")

    def test_inventory_detail_shows_the_model(self):
        tpl = (_paths.ROOT / "templates" / "inventory_detail.html").read_text(encoding="utf-8")
        self.assertIn("written with", tpl)
        self.assertIn("ad_entry.generated_with.model", tpl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
