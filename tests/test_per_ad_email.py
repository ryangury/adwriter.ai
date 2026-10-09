"""Offline tests: per-ad emails carry DATA SUMMARY + TOOL FEEDBACK from the
Ads Ready builders (compact for reprices); the Ads Ready email still renders."""
import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import contextlib
import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import adwriter as A  # noqa: E402
import orchestrator as o  # noqa: E402
sys.path.insert(0, __import__('os').path.dirname(__file__))
from _children import fake_recorded_today, fake_run_watched, ok_children  # noqa: E402

PKG = {
    "stock_number": "CT23308A",
    "vehicle": {"year_make_model": "2024 Chevrolet Silverado 1500", "current_price": 40987.0, "status_code": 11},
    "pricing": {"best_proof_point": {"gap": 7763, "direction": "below", "label": "J.D. Power Typical Listing Price", "benchmark_price": 49649}},
    "carfax": {"number_of_owners": 2, "owner_type": "personal", "miles_per_year": 19000, "no_accidents": True},
    "msrp_data": {"source": "carfax_sticker_link", "total_msrp": 65890, "option_packages": [{"name": "Z71 OFF-ROAD PACKAGE", "price": 750}]},
    "recon": {"line_items": [{"description": "Four new tires installed", "recon_reason": "tires"}]},
}
FEEDBACK = "CONFIDENCE: HIGH\nFLAGS: (1) TOWING RULE triggered"
AD = "Paragraph one.\n\nParagraph two.\n\nParagraph three.\n\nParagraph four."


def entry(stage, **kw):
    e = {"stock": "CT23308A", "vehicle": PKG["vehicle"], "ad_copy": AD, "lifecycle_stage": stage,
         "change_note": {"active": "New ad, full pipeline.", "recon_updated": "Recon completed.",
                         "repriced": "Price $41,987 -> $40,987; paragraph two rewritten."}.get(stage, "x")}
    e.update(kw)
    return e


class PerAdEmailTests(unittest.TestCase):
    def test_new_ad_full_block(self):
        body = A.format_per_ad_email(entry("active", pkg=PKG, feedback=FEEDBACK))
        print("\n--- new ad email ---\n" + body)
        for s in ("New ad, full pipeline.", "DATA SUMMARY", "Pricing proof point used: $7,763 below J.D. Power",
                  "Carfax highlights:", "Window sticker source: carfax_sticker_link", "Z71 OFF-ROAD PACKAGE",
                  "Recon items included in copy:", "TOOL FEEDBACK", "TOWING RULE triggered", "FINISHED AD COPY", AD):
            self.assertIn(s, body)
        self.assertNotIn("HendrickCars.com", body)

    def test_recon_update_full_block_without_package(self):
        body = A.format_per_ad_email(entry("recon_updated", feedback="POWERTRAIN_FLAGS:\n- x"))
        print("\n--- recon update email ---\n" + body)
        self.assertIn("DATA SUMMARY", body)
        self.assertIn("(no data package — Recon completed.)", body)
        self.assertIn("TOOL FEEDBACK", body)
        self.assertNotIn("Owners: n/a", body)
        self.assertIn(AD, body)

    def test_reprice_compact_block(self):
        body = A.format_per_ad_email(entry("repriced", pricing=PKG["pricing"], feedback=None))
        print("\n--- reprice email ---\n" + body)
        self.assertIn("DATA SUMMARY", body)
        self.assertIn("Change made: Price $41,987 -> $40,987", body)
        self.assertIn("Pricing proof point used: $7,763 below J.D. Power", body)
        self.assertIn("TOOL FEEDBACK", body)
        self.assertNotIn("Carfax highlights", body, "compact block")
        self.assertIn(AD, body)

    def test_ads_ready_email_still_renders_every_stage(self):
        body = A._format_ads_ready_email([
            entry("active", pkg=PKG, feedback=FEEDBACK, hendrickcars_url="https://x"),
            entry("recon_updated"), entry("repriced", pricing=PKG["pricing"]),
        ])
        self.assertIn("ADS READY", body)
        self.assertIn("HendrickCars.com: https://x", body)
        self.assertEqual(body.count("FINISHED AD COPY"), 3)
        self.assertEqual(body.count("DATA SUMMARY"), 3)


class OrchestratorWiringTest(unittest.TestCase):
    def test_build_email_uses_the_builder(self):
        sent = []
        veh = [{"stock_number": "CT23308A", "status_code": 11, "vin": "V", "year_make_model": "2024 Chevrolet Silverado 1500"}]

        class Fake:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a, **k): pass
            def require_durham(self, c): return "Mercedes-Benz of Durham"

        patches = [
            mock.patch.object(o, "acquire_scraper_lock"), mock.patch.object(o, "release_lock_if_owned", return_value=True),
            mock.patch.object(o, "load_previous_snapshot", return_value=None), mock.patch.object(o, "crawl_inventory", return_value=veh),
            mock.patch.object(o, "prune_sticker_cache", return_value=0), mock.patch.object(o, "flag_absent_ad_history", return_value=([], [])),
            mock.patch.object(o, "save_snapshot"), mock.patch.object(o, "stamp_eligible", return_value=[]),
            mock.patch.object(o, "load_ad_history", return_value={}), mock.patch.object(o, "save_ad_history"),
            mock.patch.object(o, "detect_reprices_needed", return_value=[]), mock.patch.object(o, "_load_reprice_queue", return_value=[]),
            mock.patch.object(o, "_save_reprice_queue"), mock.patch.object(o, "ReconVisionScraper", Fake),
            mock.patch.object(o, "check_recon", return_value={"recon_complete": True}),
            mock.patch.object(o, "aggregate", return_value=dict(PKG, recon_complete=True)),
            mock.patch.object(o, "source_status", return_value={}),
            mock.patch.object(o, "_generate_from_package", return_value=(AD, FEEDBACK)),
            mock.patch.object(o, "record_ad"), mock.patch.object(o, "ACVMaxScraper", Fake),
            mock.patch.object(o, "recorded_today", side_effect=fake_recorded_today()),
            mock.patch.object(o, "run_watched", side_effect=fake_run_watched(ok_children)),
            mock.patch.object(o, "run_verification", return_value=([], [], [])), mock.patch.object(o, "send_verification_alert"),
            mock.patch.object(o, "HendrickCarsScraper", side_effect=RuntimeError("no browser in tests")),
            mock.patch.object(o, "_send_gmail", side_effect=lambda s, b: sent.append((s, b))),
        ]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            self.assertEqual(o.run(), 0)
        per_ad = [b for s, b in sent if s.startswith("Ad Ready — CT23308A")]
        self.assertEqual(len(per_ad), 1)
        self.assertIn("DATA SUMMARY", per_ad[0])
        self.assertIn("TOOL FEEDBACK", per_ad[0])
        self.assertIn("TOWING RULE triggered", per_ad[0])
        self.assertFalse([s for s, _ in sent if "Ads Ready" in s], "Ads Ready is off (email_config.EMAIL_ADS_READY); the builders are covered above")


if __name__ == "__main__":
    unittest.main(verbosity=2)
