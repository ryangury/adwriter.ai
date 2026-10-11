"""Latched recon: once a VIN's recon reads complete (a live read, or the cache already saying so) it stays complete for
that VIN and stock number. Complete-then-regressed (no ReconVision read, no error, no waiting, builds / reprices / the
website's Generate New Ad go ahead, a reopened work order's items never reach an ad), a returned trade-in under a new
stock number (latch reset), no latch from an incomplete / stub / failed read, a car going absent (latch reset), the
recon update running once, and the last-known work-order status. Offline: temp databases and state, stub scrapers."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)

TMP = Path(tempfile.mkdtemp(prefix="recon_latch_"))
import vehicle_cache as VC  # noqa: E402

VC.DB_PATH = TMP / "vehicle_cache.db"
import adwriter as A  # noqa: E402
import aggregator as AG  # noqa: E402
import orchestrator as o  # noqa: E402
import scraper as S  # noqa: E402

AG.RECON_GATE_STATE_PATH = TMP / "recon_gate_state.json"
AG.time.sleep = lambda s: None


def items(rows):
    return [S.ReconVisionScraper._normalize_line_item({"section": s, "kind": "task", "description": s,
                                                      "completion_status": c, "rejected": False}) for s, c in rows]


DONE = items([("Mechanical Repairs", "TASK COMPLETED"), ("Close RO", "TASK COMPLETED"), ("Final Quality Control", "TASK COMPLETED")])
REOPENED = items([("Close RO", "TASK COMPLETED"), ("Final Quality Control", "INCOMPLETE")])
VENDOR_WORDING = items([("Close RO", "INCOMPLETE"), ("Final Quality Control", "Completed by Vendor Add PO")])


class RV:
    """A logged-in ReconVision stand-in; counts reads."""

    def __init__(self, its, header="Mechanical Repairs", vin="VIN1", wo="4001"):
        self.its, self.header, self.vin, self.wo, self.reads = its, header, vin, wo, 0

    def login(self, force=False):
        pass

    def scrape_work_order(self, stock):
        self.reads += 1
        return {"work_order_id": self.wo, "vin": self.vin, "header_status": self.header, "line_items": list(self.its)}


def wipe():
    with VC._connect() as c:
        c.execute("DELETE FROM vehicle_data")
        c.commit()
    AG.RECON_GATE_STATE_PATH.write_text("{}", encoding="utf-8")


class CompleteThenRegressed(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_a_live_complete_read_latches_and_a_reopened_order_is_never_read(self):
        first = RV(DONE, header="Closed/Ready For Sale")
        out = AG.check_recon("PM53873", rv=first, vin="VIN1")
        self.assertEqual((out["recon_complete"], out["latched"], first.reads), (True, True, 1))
        self.assertTrue(VC.recon_latched("VIN1", "PM53873"))
        later = RV(REOPENED)                                   # sent back: FQC open again
        out = AG.check_recon("PM53873", rv=later, vin="VIN1")
        self.assertEqual((out["recon_complete"], out["note"], later.reads), (True, AG.RECON_LATCHED_NOTE, 0))
        self.assertTrue(VC.recon_latched("VIN1", "PM53873"), "no downgrade, ever")
        self.assertFalse(hasattr(VC, "downgrade_recon"))
        self.assertFalse(hasattr(AG, "cached_recon_still_complete"))

    def test_a_cache_that_already_says_complete_is_the_latch(self):
        VC.save_recon("VIN2", "PM87365", {"work_order_id": "4000", "line_items": DONE}, True)
        rv = RV(VENDOR_WORDING, wo="4053254", vin="VIN2")      # the newer work order, FQC "Completed by Vendor Add PO"
        out = AG.check_recon("PM87365", rv=rv)                 # the VIN comes from the cached row
        self.assertEqual((out["recon_complete"], rv.reads), (True, 0))

    def test_the_build_and_the_websites_generate_use_the_cached_recon(self):
        # aggregate() (the build and the website's Generate New Ad, which passes expected_vin) never opens ReconVision
        # for a latched car and never re-checks the stored items; it goes straight on to ACV Max.
        VC.save_recon("VIN3", "P11111", {"work_order_id": "4100", "line_items": DONE, "header_status": "Closed/Ready For Sale"}, True)

        class Stop(Exception):
            pass

        with mock.patch.object(AG, "ReconVisionScraper", side_effect=AssertionError("ReconVision must not be opened")), \
             mock.patch.object(AG, "ACVMaxScraper", side_effect=Stop("reached ACV Max")):
            with self.assertRaises(Stop):
                AG.aggregate("P11111", expected_vin="VIN3")

    def test_the_gate_sends_a_latched_car_on_without_a_session(self):
        VC.save_recon("VIN4", "P22222", {"work_order_id": "4200", "line_items": DONE}, True)
        self.assertTrue(o.recon_latched("VIN4", "P22222"))
        src = Path(o.__file__).read_text(encoding="utf-8")
        self.assertIn("recon latched complete -> recon update queue (no ReconVision read)", src)
        self.assertIn("no ad yet, recon latched complete -> build queue (no ReconVision read)", src)

    def test_the_recon_update_uses_the_cached_work_order_and_runs_once(self):
        VC.save_recon("VIN5", "P33333", {"work_order_id": "4300", "line_items": DONE}, True)
        hist = {"P33333": {"recon_pending": True, "lifecycle_stage": "pre_recon", "paragraph_one": "P1.",
                           "paragraph_two": "P2.", "paragraph_three": "P3.", "paragraph_four": "P4.",
                           "current_ad_text": "P1.\n\nP2.\n\nP3.\n\nP4."}}
        saved = []
        with mock.patch.object(A, "load_ad_history", return_value=hist), \
             mock.patch.object(A, "save_ad_history", side_effect=lambda h: saved.append(json.loads(json.dumps(h)))), \
             mock.patch.object(A, "_snapshot_vehicle", return_value={"vin": "VIN5", "status_code": 10, "mileage": 9000}), \
             mock.patch.object(A, "ReconVisionScraper", side_effect=AssertionError("no ReconVision read")), \
             mock.patch.object(A, "_topup_powertrain_check"), \
             mock.patch.object(A, "_filter_recon", side_effect=lambda its, sc: (seen.append(its) or [])):
            seen = []
            A.update_recon("P33333", 10)
        self.assertEqual([i["description"] for i in seen[0]], [i["description"] for i in DONE], "the cached work order")
        self.assertEqual(saved[-1]["P33333"]["lifecycle_stage"], "recon_updated")
        again = dict(saved[-1]["P33333"], recon_pending=True)          # even if a flag were left on
        with mock.patch.object(A, "load_ad_history", return_value={"P33333": again}), \
             mock.patch.object(A, "save_ad_history", side_effect=AssertionError("never runs again")), \
             mock.patch.object(A, "ReconVisionScraper", side_effect=AssertionError("never runs again")):
            A.update_recon("P33333", 10)


class TradeInAndAbsent(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_a_returned_trade_in_under_a_new_stock_number_is_read_again(self):
        VC.save_recon("VIN6", "T10001", {"work_order_id": "3900", "line_items": DONE}, True)
        self.assertTrue(VC.recon_latched("VIN6", "T10001"))
        self.assertFalse(VC.recon_latched("VIN6", "T20002"), "the latch belongs to the VIN AND the stock number")
        rv = RV(REOPENED, vin="VIN6", wo="4400")
        out = AG.check_recon("T20002", rv=rv, vin="VIN6")
        self.assertEqual((out["recon_complete"], rv.reads >= 1), (False, True))
        VC.save_recon("VIN6", "T20002", {"work_order_id": "4400", "line_items": REOPENED}, False)
        row = VC.get_vehicle("VIN6")
        self.assertEqual((row["stock_number"], bool(row["recon_complete"])), ("T20002", False), "the old latch did not carry over")
        self.assertNotIn("Mechanical Repairs", row["recon_json"], "the old work order's items are gone")

    def test_going_absent_resets_the_latch(self):
        VC.save_recon("VIN7", "P44444", {"work_order_id": "4500", "line_items": DONE}, True)
        self.assertTrue(VC.reset_recon_latch("VIN7", "P44444 absent from inventory"))
        self.assertFalse(VC.recon_latched("VIN7", "P44444"))
        self.assertIsNone(VC.get_recon("VIN7"))
        src = Path(o.__file__).read_text(encoding="utf-8")
        self.assertIn('reset_recon_latch(gone["vin"], f"{s} absent from inventory")', src)


class NeverLatchFromABadRead(unittest.TestCase):
    def setUp(self):
        wipe()

    def test_incomplete_stub_and_failed_reads(self):
        rv = RV(REOPENED)
        self.assertFalse(AG.check_recon("P55555", rv=rv, vin="VIN8")["recon_complete"])
        self.assertFalse(VC.recon_latched("VIN8", "P55555"), "an incomplete read never latches")
        VC.save_recon("VIN8", "P55555", {"work_order_id": None, "line_items": []}, True)
        self.assertFalse(VC.recon_latched("VIN8", "P55555"), "a stub / empty read never latches")
        VC.save_recon("VIN8", "P55555", {"work_order_id": "4600", "line_items": []}, True)
        self.assertFalse(VC.recon_latched("VIN8", "P55555"))

        class Broken(RV):
            def scrape_work_order(self, stock):
                raise S.WorkOrderLoadError("timeout")

        out = AG.check_recon("P55555", rv=Broken(DONE), vin="VIN8")
        self.assertFalse(out["recon_complete"])
        self.assertFalse(VC.recon_latched("VIN8", "P55555"), "a failed read never latches")

    def test_a_complete_read_for_another_vin_does_not_latch_this_one(self):
        rv = RV(DONE, header="Closed/Ready For Sale", vin="SOMEONEELSE")
        out = AG.check_recon("P66666", rv=rv, vin="VIN9")
        self.assertTrue(out["recon_complete"])
        self.assertFalse(VC.recon_latched("VIN9", "P66666"))


class LastKnownStatus(unittest.TestCase):
    """The last parsed work-order status is still kept (only the downgrade streak went)."""

    def setUp(self):
        wipe()

    def test_status_kept_and_used_when_a_scrape_has_none(self):
        AG.check_recon("P29369", rv=RV(VENDOR_WORDING, header="Closed/Ready For Sale", vin="VINA"), vin="VINA")
        state = json.loads(AG.RECON_GATE_STATE_PATH.read_text())
        self.assertEqual(state["P29369"], {"last_status": "Closed/Ready For Sale", "last_status_date": state["P29369"]["last_status_date"]})
        self.assertTrue(VC.recon_latched("VINA", "P29369"))
        VC.reset_recon_latch("VINA", "test")
        out = AG.check_recon("P29369", rv=RV(VENDOR_WORDING, header=None, vin="VINA"), vin="VINA")
        self.assertTrue(out["recon_complete"], "a missing status falls back to the last known 'Closed/Ready For Sale'")


class ReconReview(unittest.TestCase):
    """An archived work order that reads incomplete for a car with no latch is listed (status, FQC text, row counts)
    in the Build Summary's RECON REVIEW section, and nothing else is done: it is not treated as complete."""

    def setUp(self):
        wipe()
        AG.RECON_REVIEW.clear()

    def test_listed_with_its_details_and_not_treated_as_complete(self):
        its = VENDOR_WORDING + [dict(DONE[0], kind="service", completed=True, completion_status="TASK COMPLETED",
                                     description="A Service", section="Mechanical"),
                                dict(DONE[0], kind="service", completed=False, completion_status="Completed by Vendor Add PO",
                                     description="remove dents", section="Body")]
        out = AG.check_recon("PX0001", rv=RV(its, header="Archived", wo="4053254", vin="VINR"), vin="VINR")
        self.assertFalse(out["recon_complete"], "an archived order is NOT counted as complete")
        self.assertFalse(VC.recon_latched("VINR", "PX0001"))
        self.assertEqual(AG.RECON_REVIEW, [{"stock": "PX0001", "work_order": "4053254", "status": "Archived",
                                            "fqc_text": "Completed by Vendor Add PO", "service_rows": 2,
                                            "service_completed": 1, "service_not_completed": 1}])
        AG.check_recon("PX0001", rv=RV(its, header="Archived", wo="4053254", vin="VINR"), vin="VINR")
        self.assertEqual(len(AG.RECON_REVIEW), 1, "once per stock per run")
        body = o._format_build_summary([], [], [], [])
        self.assertIn("RECON REVIEW (archived work order, no latch; listed only, no action taken) (1)", body)
        self.assertIn("[PX0001]  work order 4053254  status Archived", body)
        self.assertIn("FQC 'Completed by Vendor Add PO'; service rows 1 TASK COMPLETED, 1 not (2 in all)", body)

    def test_not_listed_when_complete_open_or_latched(self):
        AG.check_recon("PX0002", rv=RV(REOPENED, header="Recon"), vin="VIN2X")                  # open, not archived
        AG.check_recon("PX0003", rv=RV(DONE, header="Archived", vin="VIN3X"), vin="VIN3X")      # archived and complete
        VC.save_recon("VIN4X", "PX0004", {"work_order_id": "1", "line_items": DONE}, True)
        AG.check_recon("PX0004", rv=RV(VENDOR_WORDING, header="Archived", vin="VIN4X"), vin="VIN4X")   # latched: not read
        self.assertEqual(AG.RECON_REVIEW, [])
        self.assertIn("RECON REVIEW (archived work order, no latch; listed only, no action taken) (0)", o._format_build_summary([], [], [], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
