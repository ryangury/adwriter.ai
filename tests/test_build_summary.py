"""The early Build Summary email (sent after the build step, before CTR):
built ads by kind, the not-rebuilt list with restore commands, errors so far,
and Carfax-refresh leftovers. Offline; nothing sent."""
import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestrator as o  # noqa: E402


class BuildSummary(unittest.TestCase):
    def test_sections(self):
        ads = [
            {"stock": "R1", "lifecycle_stage": "repriced", "vehicle": {"year_make_model": "2024 GLE", "advertised_price": 50899}},
            {"stock": "N1", "lifecycle_stage": "active", "vehicle": {"year_make_model": "2025 GLC", "advertised_price": 45899}},
            {"stock": "U1", "lifecycle_stage": "recon_updated", "vehicle": {}},
        ]
        body = o._format_build_summary(ads, [{"stock": "D1", "reason": "no sticker"}],
                                       [{"stock": "E1", "phase": "sources", "error": "boom"}],
                                       [{"stock": "C1", "ymm": "2023 GLE", "group": "damage", "manual": False}])
        self.assertIn("ADS BUILT (3)", body)
        self.assertLess(body.index("NEW          N1"), body.index("RECON UPDATE U1"))
        self.assertLess(body.index("RECON UPDATE U1"), body.index("REPRICE      R1"))
        self.assertIn("restore: python restore_removed.py --only D1", body)
        self.assertIn("[E1]  sources  —  boom", body)
        self.assertIn("python carfax_refresh.py --only C1", body)

    def test_empty_and_unchecked(self):
        body = o._format_build_summary([], [], [], None)
        self.assertIn("ADS BUILT (0)", body)
        self.assertIn("(could not be checked this run)", body)


if __name__ == "__main__":
    unittest.main()
