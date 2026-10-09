"""Tests for the tire-wording fix (Task 2), deterministic recon top-up (Task 3)
and the non-MB scrub (Task 4). In-process, nothing saved, no API call."""
import copy
import json
import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import adwriter  # noqa: E402
import aggregator  # noqa: E402
from aggregator import _RECON_PENDING_FALLBACK, _filter_recon, build_recon_sentence  # noqa: E402

MR = "manufacturer-recommended"
REAL_HISTORY = json.load(open(str(_paths.DATA / "ad_history.json"), encoding="utf-8"))


def li(desc, section="Mechanical Repairs"):
    return {
        "section": section, "description": desc, "completion_status": "TASK COMPLETED",
        "completed": True, "rejected": False, "kind": None, "labor_cost": 100.0,
        "labor_hours": 1.0, "operation_code": None, "parts_cost": 400.0,
        "repair_order": None, "service_id": None, "total_cost": 500.0,
    }


RECON_ITEMS = [
    li("Hendrick Certified Inspection", "Technician Inspection"),
    li("Tires - M&B 4"),
    li("Front Brake Pads and Rotors"),
    li("Wiper Blades - Front"),
]


class Task2Fallback(unittest.TestCase):
    def test_all_tires_fallback_tier_wording(self):
        for sc in (10, 16, 11, 12, 13):
            s = build_recon_sentence({"all_tires_replaced": True, "line_items": []}, sc)
            print(f"  fallback status {sc}: {s}")
            self.assertTrue(s and "four new" in s.lower() and "tires installed" in s)
            if sc in (10, 16):
                self.assertIn(f"{MR} tires", s)
            else:
                self.assertNotIn(MR, s.lower())

    def test_aggregated_tire_item_tier_wording(self):
        for sc in (10, 16, 11, 12, 13):
            s = build_recon_sentence(_filter_recon(RECON_ITEMS, sc), sc)
            print(f"  full recon status {sc}: {s}")
            self.assertEqual(MR in (s or "").lower(), sc in (10, 16))


class FakeRV:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self):
        pass

    def scrape_work_order(self, stock):
        return {"line_items": copy.deepcopy(RECON_ITEMS), "work_order_id": 1}


def _pending_cases():
    """One currently pre-recon ad per tier (the live set changes as recon
    finishes). A tier with no pending ad borrows one whose pending sentence is
    the same (12 and 13 share theirs; 16 uses 10's)."""
    snap = {v["stock_number"]: v.get("status_code") for v in json.load(
        open(str(_paths.DATA / "last_inventory_snapshot.json"), encoding="utf-8"))["vehicles"]}
    by_tier = {}
    for s, e in REAL_HISTORY.items():
        sc = snap.get(s)
        code = 10 if sc == 16 else sc
        pend = _RECON_PENDING_FALLBACK.get(code)
        if e.get("recon_pending") and pend and pend in (e.get("paragraph_one") or "") and code not in by_tier:
            by_tier[code] = s
    cases = []
    for tier in (10, 11, 12, 13):
        if tier in by_tier:
            cases.append((by_tier[tier], tier, f"real status-{tier} pre-recon ad"))
        else:
            donor = by_tier.get({13: 12, 12: 13}.get(tier))
            if donor:
                cases.append((donor, tier, f"no status-{tier} pre-recon ad; same-sentence ad run as {tier}"))
    return cases


class Task3TopUp(unittest.TestCase):
    CASES = _pending_cases()

    def test_top_up_per_tier(self):
        for stock, sc, note in self.CASES:
            with self.subTest(stock=stock, status=sc):
                hist = copy.deepcopy(REAL_HISTORY)
                saved = []
                api = mock.MagicMock(side_effect=AssertionError("Anthropic client constructed"))
                with mock.patch.object(adwriter, "load_ad_history", return_value=hist), \
                     mock.patch.object(adwriter, "save_ad_history", side_effect=lambda h: saved.append(h)), \
                     mock.patch.object(adwriter, "ReconVisionScraper", FakeRV), \
                     mock.patch.object(adwriter.anthropic, "Anthropic", api), \
                     mock.patch.object(adwriter, "_capped_completion", side_effect=AssertionError("model call")):
                    out = adwriter.update_recon(stock, sc)
                api.assert_not_called()
                entry = saved[-1][stock]
                p1 = entry["paragraph_one"]
                pending = _RECON_PENDING_FALLBACK[10 if sc == 16 else sc]
                ymm = adwriter._snapshot_vehicle(stock).get("year_make_model")
                expected = build_recon_sentence(
                    _filter_recon(RECON_ITEMS, sc), sc, adwriter._snapshot_vehicle(stock).get("mileage")
                )
                print(f"\n  {stock} ({note}; {ymm}) status {sc}\n    appended: {expected}")
                self.assertNotIn(pending, out)
                self.assertNotIn(pending, p1)
                self.assertEqual(out.count(expected), 1)
                self.assertTrue(p1.rstrip().endswith(expected))
                self.assertEqual(MR in expected.lower(), sc in (10, 16))
                self.assertEqual(entry["current_ad_text"], out)
                self.assertFalse(entry["recon_pending"])
                self.assertEqual(entry["lifecycle_stage"], "recon_updated")
                self.assertIs(saved[-1], hist)  # only the in-memory copy was "saved"

    def test_rerun_does_not_duplicate(self):
        self.assertEqual(adwriter.append_recon_sentence("A. B.", "B."), "A. B.")
        self.assertEqual(adwriter.append_recon_sentence("A.", "B."), "A. B.")


class Task4Scrub(unittest.TestCase):
    def test_scrub(self):
        t = "Four new manufacturer-recommended tires installed. Manufacturer-recommended tires were fitted."
        self.assertEqual(
            adwriter.scrub_non_mb_tire_wording(t, 11, "2023 Hyundai Santa Fe"),
            "Four new tires installed. Tires were fitted.",
        )
        self.assertEqual(adwriter.scrub_non_mb_tire_wording(t, 10, "2024 Mercedes-Benz GLE"), t)
        self.assertNotIn(MR, adwriter.scrub_non_mb_tire_wording(t, 10, "2022 Ram 2500").lower())
        svc = "all work uses manufacturer-recommended services and parts"
        self.assertEqual(adwriter.scrub_non_mb_tire_wording(svc, 12, "2022 Jeep"), svc)

    def test_generate_path_scrubs(self):
        pkg = {"stock_number": "X1", "vehicle": {"status_code": 12, "year_make_model": "2022 Jeep Grand Cherokee L"}}
        with mock.patch.object(adwriter, "_generate_once", return_value=("Four new manufacturer-recommended tires installed.", "fb")), \
             mock.patch.object(adwriter, "required_sentences_from", return_value={}):
            ad, fb = adwriter._generate_from_package(pkg)
        self.assertEqual(ad, "Four new tires installed.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
