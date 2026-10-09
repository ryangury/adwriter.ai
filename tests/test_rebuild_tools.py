"""Delete-and-rebuild tools: list_not_rebuilt reasons, copy_dates (earliest
archived first_ad_date, last_ad_date untouched), restore_removed (only when no
current entry), and run_lock.lock_blocks_edit. Temp files only; offline."""
import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import copy_dates  # noqa: E402
import list_not_rebuilt as L  # noqa: E402
import restore_removed  # noqa: E402
import run_lock  # noqa: E402

LOG = """
[gate] A1: status 1 — needs certification assigned
[ctr] 3 of 9 — B2: recorded (AT 1.0)
[build] B2: aggregating (skip_recon=False) ...
[build] B2: incomplete window sticker data for a CPO vehicle
[build] C3: new listing (2 days on lot), ACV Max price is 0 — pricing not ready, will retry tomorrow
[gate] D4: ReconVision unreachable — recon not checked -> waiting
[build] E5: ad generation failed — overloaded
"""


class Reasons(unittest.TestCase):
    def test_reasons(self):
        self.assertEqual(L.reason_for("A1", LOG)[0], "status 1 (needs certification)")
        self.assertEqual(L.reason_for("B2", LOG)[0], "no sticker")
        self.assertEqual(L.reason_for("C3", LOG)[0], "pricing gate (ACV Max price not ready)")
        self.assertEqual(L.reason_for("D4", LOG)[0], "recon open / not checked")
        self.assertEqual(L.reason_for("E5", LOG)[0], "error")
        self.assertTrue(L.reason_for("Z9", LOG)[0].startswith("not reached"))

    def test_ctr_lines_never_decide(self):
        self.assertTrue(L.reason_for("Q1", "[ctr] 1 of 2 — Q1: scrape failed — boom")[0].startswith("not reached"))


class Archives(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.old = self.d / "ad_history_removed_2026-10-02.json"
        self.new = self.d / "ad_history_removed_2026-10-07.json"
        self.old.write_text(json.dumps({"P1": {"first_ad_date": "2026-09-24", "current_ad_text": "old"}}))
        self.new.write_text(json.dumps({"P1": {"first_ad_date": "2026-10-03", "current_ad_text": "mid"},
                                        "P2": {"first_ad_date": "2026-09-30", "current_ad_text": "p2"}}))
        os.utime(self.old, (1, 1))

    def test_not_rebuilt(self):
        rows = L.not_rebuilt({"P1": {}}, [self.old, self.new], {"P1", "P2"}, "[build] P2: aggregate failed — x")
        self.assertEqual([(r["stock"], r["reason"], r["archive"]) for r in rows],
                         [("P2", "error", self.new.name)])
        # sold (not in inventory): not listed
        self.assertEqual(L.not_rebuilt({}, [self.new], {"P1"}, "")[0]["stock"], "P1")

    def test_copy_dates_earliest_and_last_untouched(self):
        hist = {"P1": {"first_ad_date": "2026-10-08", "last_ad_date": "2026-10-08"}}
        self.assertEqual(copy_dates.plan(hist, {"P1", "P2"}, [self.old, self.new]),
                         [("P1", "2026-10-08", "2026-09-24")])
        self.assertEqual(copy_dates.plan({"P1": {"first_ad_date": "2026-09-01"}}, {"P1"}, [self.old, self.new]), [])

    def test_restore_only_without_current_entry(self):
        hpath = self.d / "ad_history.json"
        hpath.write_text(json.dumps({"P1": {"current_ad_text": "rebuilt"}}))
        saved = {}
        with mock.patch.object(restore_removed.A, "AD_HISTORY_PATH", hpath), \
             mock.patch.object(restore_removed.A, "load_ad_history", lambda: json.loads(hpath.read_text())), \
             mock.patch.object(restore_removed.A, "save_ad_history", lambda h: saved.update(h)), \
             mock.patch.object(restore_removed, "lock_blocks_edit", lambda p: None):
            restore_removed.main(["--all", "--archive", str(self.new)])
        self.assertEqual(saved["P1"]["current_ad_text"], "rebuilt")  # never overwritten
        self.assertEqual(saved["P2"]["current_ad_text"], "p2")


class LockHolder(unittest.TestCase):
    def test_named_holder_and_stale(self):
        p = Path(tempfile.mkdtemp()) / "orchestrator.lock"
        self.assertIsNone(run_lock.lock_blocks_edit(p))
        p.write_text("4242")
        with mock.patch.object(run_lock, "_pid_running", lambda pid: True):
            self.assertEqual(run_lock.lock_blocks_edit(p), 4242)
            with mock.patch.dict(os.environ, {"ADWRITER_LOCK_HOLDER": "4242"}):
                self.assertIsNone(run_lock.lock_blocks_edit(p))
        with mock.patch.object(run_lock, "_pid_running", lambda pid: False):
            self.assertIsNone(run_lock.lock_blocks_edit(p))  # stale


if __name__ == "__main__":
    unittest.main()
