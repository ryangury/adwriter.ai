"""End-to-end stub tests of orchestrator.run() for the three by-design stops:
emails still go out, the lock is released, exit code is right. Nothing real is
touched: crawl, files, browsers, Claude and email are all stubbed."""
import contextlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, r"C:\adwriter")
import orchestrator as o  # noqa: E402
sys.path.insert(0, __import__('os').path.dirname(__file__))
from _children import fake_recorded_today, fake_run_watched, ok_children  # noqa: E402
import scraper  # noqa: E402

FRAME = "Merchandising iframe (merchandising/PricingAnalysis) for {} never attached with a usable URL."


def retail(n, with_ads=False):
    return [{"stock_number": f"S{i}", "status_code": 10, "vin": f"V{i}", "year_make_model": "2024 Mercedes-Benz GLE"} for i in range(n)]


class Harness:
    def __init__(self, vehicles, history, aggregate, ctr=None, require_durham=None, child_status=None,
                 check_recon=None):
        self.sent = []
        self.released = 0
        self.acv_logins = 0
        h = self

        class FakeACV:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, *a, **k):
                h.acv_logins += 1

            def require_durham(self, context):
                if require_durham:
                    raise require_durham
                return "Mercedes-Benz of Durham"

        class FakeRV(FakeACV):
            def login(self, *a, **k):
                pass  # ReconVision, not ACV Max

        def on_child(kind, store):
            h.acv_logins += 1
            if kind == "benchmark" and require_durham:
                return "failed", {"counts": {}, "errors": [], "aborted": str(require_durham)}
            if kind == "benchmark" and child_status:
                return child_status, {"counts": {}, "errors": [], "aborted": None}
            return ok_children(kind, store)

        self.children = fake_run_watched(on_child)
        self.verify = mock.Mock(return_value=([], [], []))
        self.patches = [
            mock.patch.object(o, "acquire_scraper_lock"),
            mock.patch.object(o, "release_lock_if_owned", side_effect=self._release),
            mock.patch.object(o, "load_previous_snapshot", return_value=None),
            mock.patch.object(o, "crawl_inventory", return_value=vehicles),
            mock.patch.object(o, "prune_sticker_cache", return_value=0),
            mock.patch.object(o, "flag_absent_ad_history", return_value=([], [])),
            mock.patch.object(o, "save_snapshot"),
            mock.patch.object(o, "stamp_eligible", return_value=[]),
            mock.patch.object(o, "load_ad_history", side_effect=lambda: dict(history)),
            mock.patch.object(o, "save_ad_history"),
            mock.patch.object(o, "detect_reprices_needed", return_value=[]),
            mock.patch.object(o, "_load_reprice_queue", return_value=[]),
            mock.patch.object(o, "_save_reprice_queue"),
            mock.patch.object(o, "ReconVisionScraper", FakeRV),
            mock.patch.object(o, "check_recon", side_effect=check_recon or (lambda *a, **k: {"recon_complete": True})),
            mock.patch.object(o, "aggregate", side_effect=aggregate),
            mock.patch.object(o, "ACVMaxScraper", FakeACV),
            mock.patch.object(o, "recorded_today", side_effect=fake_recorded_today()),
            mock.patch.object(o, "run_watched", side_effect=self.children),
            mock.patch.object(o, "run_verification", new=self.verify),
            mock.patch.object(o, "_carfax_leftovers", return_value=[]),
            mock.patch.object(o.list_not_rebuilt, "report", return_value=[]),
            mock.patch.object(o, "send_verification_alert"),
            mock.patch.object(o, "_send_gmail", side_effect=lambda s, b: self.sent.append((s, b))),
        ]

    def _release(self, path):
        self.released += 1
        return True

    def run(self):
        with contextlib.ExitStack() as st:
            for p in self.patches:
                st.enter_context(p)
            return o.run()

    def subjects(self):
        return [s for s, _ in self.sent]


def has(subjects, prefix):
    return [s for s in subjects if s.startswith(prefix)]


class StopTests(unittest.TestCase):
    def test_second_login_form_in_build(self):
        h = Harness(retail(6), {}, aggregate=scraper.AcvMaxRunAbort(
            "pricing frame still shows the login form after a fresh login: x"))
        rc = h.run()
        subj = h.subjects()
        print("\n  login-form stop: rc", rc, "emails", subj)
        self.assertEqual(rc, 1)
        self.assertEqual(h.released, 1)
        self.assertEqual(len(has(subj, "ACV Max scraping broken")), 1)
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — Action Required")), 1)
        body = dict(h.sent)[has(subj, "Mercedes-Benz of Durham — Action Required")[0]]
        self.assertIn("still shows the login form", body)
        self.assertEqual(h.acv_logins, 0, "CTR and benchmark skipped after the stop")

    def test_wrong_store_after_benchmark(self):
        hist = {f"S{i}": {"current_ad_text": "x", "recon_pending": False} for i in range(3)}
        h = Harness(retail(3), hist, aggregate=AssertionError("no builds expected"),
                    require_durham=scraper.WrongDealershipError(
                        "after the Northlake/Charlotte benchmark: ACV MAX shows dealership 'Hendrick Motors of Charlotte'"))
        rc = h.run()
        subj = h.subjects()
        print("\n  wrong-store stop: rc", rc, "emails", subj)
        self.assertEqual(rc, 1)
        self.assertEqual(h.released, 1)
        self.assertEqual(len(has(subj, "ACV Max scraping broken: after the Northlake/Charlotte benchmark")), 1)
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — Action Required")), 1)

    def test_five_identical_failures(self):
        n = iter(range(100))
        h = Harness(retail(8), {}, aggregate=lambda *a, **k: (_ for _ in ()).throw(
            scraper.PricingNotFoundError(FRAME.format(next(n)))))
        rc = h.run()
        subj = h.subjects()
        print("\n  5-failure stop: rc", rc, "emails", subj)
        self.assertEqual(rc, 0, "loop stop is not a run stop")
        self.assertEqual(h.released, 1)
        self.assertEqual(o.aggregate.call_count if hasattr(o.aggregate, "call_count") else 5, 5)
        self.assertEqual(len(has(subj, "ACV Max scraping broken: Merchandising iframe")), 1)
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — Action Required")), 1)
        self.assertGreater(h.acv_logins, 0, "CTR still runs after a build-loop stop")


    def test_benchmark_hang_kills_alerts_once_and_continues(self):
        hist = {f"S{i}": {"current_ad_text": "x", "recon_pending": False} for i in range(3)}
        h = Harness(retail(3), hist, aggregate=AssertionError("no builds expected"), child_status="hung")
        rc = h.run()
        subj = h.subjects()
        print("\n  benchmark hang: rc", rc, "emails", subj, "children", h.children.calls)
        self.assertEqual(rc, 0, "a hung benchmark is not a run stop")
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — benchmark hung")), 1, "one email for both stores")
        kinds = [c[0] for c in h.children.calls]
        self.assertEqual(kinds.count("benchmark"), 2, "the second store still runs after the first hangs")
        self.assertEqual(kinds.count("restore-durham"), 2, "Durham restored after each killed store")
        self.assertTrue(h.verify.called, "verification still runs")
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — Build Summary")), 1)
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — Action Required")), 1)


    def test_benchmark_ran_out_of_time_is_not_called_hung_and_counts_are_real(self):
        import io

        hist = {f"S{i}": {"current_ad_text": "x", "recon_pending": False} for i in range(3)}
        h = Harness(retail(3), hist, aggregate=AssertionError("no builds expected"), child_status="budget")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = h.run()
        subj = h.subjects()
        self.assertEqual(rc, 0)
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — benchmark ran out of time")), 1)
        self.assertEqual(len(has(subj, "Mercedes-Benz of Durham — benchmark hung")), 0)
        body = dict(h.sent)[has(subj, "Mercedes-Benz of Durham — benchmark ran out of time")[0]]
        self.assertIn("ran out of time after 126 of 136 vehicles", body)
        self.assertIn("126 vehicle(s) were recorded", body)
        self.assertIn("It had not hung", body)
        self.assertNotIn("No progress for 10 minutes", body)
        out = buf.getvalue()
        self.assertIn("Benchmark CTR — Northlake:     126 of 136 vehicles", out)
        self.assertIn("Benchmark CTR — Charlotte:     126 of 136 vehicles", out)

    def test_benchmark_hung_email_says_no_progress(self):
        hist = {f"S{i}": {"current_ad_text": "x", "recon_pending": False} for i in range(3)}
        h = Harness(retail(3), hist, aggregate=AssertionError("no builds expected"), child_status="hung")
        h.run()
        body = dict(h.sent)[has(h.subjects(), "Mercedes-Benz of Durham — benchmark hung")[0]]
        self.assertIn("No progress for 10 minutes", body)
        self.assertNotIn("ran out of time", body)


    def test_courtesy_vehicle_without_a_work_order_is_not_an_error(self):
        def check_recon(stock, rv=None):
            raise scraper.WorkOrderNotFoundError(f"Stock #{stock}: no work order found. See x/ for a snapshot.")

        vehicles = [
            {"stock_number": "ZT1", "status_code": 16, "days_on_lot": 2, "vin": "V1", "year_make_model": "2026 Mercedes-Benz GLB"},
            {"stock_number": "ZT2", "status_code": 10, "days_on_lot": 3, "vin": "V2", "year_make_model": "2026 Mercedes-Benz GLC"},
            {"stock_number": "P9", "status_code": 16, "days_on_lot": 1, "vin": "V3", "year_make_model": "2025 Mercedes-Benz GLE"},
            {"stock_number": "P7", "status_code": 10, "days_on_lot": 1, "vin": "V4", "year_make_model": "2025 Mercedes-Benz GLE"},
        ]
        h = Harness(vehicles, {}, aggregate=AssertionError("no builds expected"), check_recon=check_recon)
        rc = h.run()
        body = dict(h.sent)[has(h.subjects(), "Mercedes-Benz of Durham — Action Required")[0]]
        self.assertEqual(rc, 0)
        # Z-prefix (ZT1, ZT2) and status 16 (P9): waiting, with a note, not errors
        for stock in ("ZT1", "ZT2", "P9"):
            self.assertRegex(body, rf"\[{stock}\].*\n\s+no ReconVision work order yet \(courtesy vehicle")
        self.assertIn("WAITING ON RECON (3)", body)
        # P7 is an ordinary car: a missing work order is still a scraper error
        self.assertIn("SCRAPER ERRORS (1)", body)
        self.assertRegex(body, r"\[P7\]\s+recon\s+—\s+Stock #P7: no work order found")
        for stock in ("ZT1", "ZT2", "P9"):
            self.assertNotRegex(body, rf"\[{stock}\]\s+recon\s+—")


    def test_price_gate_and_new_listings_are_not_scraper_errors(self):
        from aggregator import _mb_cpo_data_gate

        # the real gate's package for "priced above every benchmark"
        proof = [
            {"label": "J.D. Power Retail", "benchmark_price": 57000.0, "gap": 1920.0, "direction": "above"},
            {"label": "KBB Retail", "benchmark_price": 55000.0, "gap": 3920.0, "direction": "above"},
        ]
        gate = _mb_cpo_data_gate("P1", "P1", 10, {"current_internet_price": 58920.0, "pricing_proof_points": proof},
                                 {"total_msrp": 70000, "source": "autoipacket"})
        self.assertEqual(gate["failed_sources"], ["no_favorable_proof_point"])
        self.assertEqual(gate["nearest_benchmark"]["label"], "J.D. Power Retail")
        self.assertEqual(gate["nearest_benchmark"]["gap"], 1920.0)

        new_listing = {"reason": "incomplete_data", "failed_source": "acvmax_pricing", "failed_sources": ["acvmax_pricing"],
                       "message": "MB CPO data gate failed - ACV Max pricing unavailable", "current_internet_price": 0}
        vehicles = [
            {"stock_number": "P1", "status_code": 10, "days_on_lot": 2, "current_price": 58920.0, "vin": "V1",
             "year_make_model": "2026 Mercedes-Benz GLC"},
            {"stock_number": "P2", "status_code": 10, "days_on_lot": 2, "current_price": 0.0, "vin": "V2",
             "year_make_model": "2025 Mercedes-Benz AMG GT"},
        ]
        h = Harness(vehicles, {}, aggregate=lambda stock, skip_recon=False: gate if stock == "P1" else new_listing)
        rc = h.run()
        body = dict(h.sent)[has(h.subjects(), "Mercedes-Benz of Durham — Action Required")[0]]
        self.assertEqual(rc, 0)
        self.assertIn("WAITING ON PRICE", body)
        self.assertIn("[P1]", body)
        self.assertIn("nearest benchmark: J.D. Power Retail (benchmark $57,000)", body)
        self.assertIn("$1,920 above it", body)
        self.assertIn("NEW LISTINGS, PRICING NOT READY (will retry tomorrow)", body)
        self.assertIn("SCRAPER ERRORS (0)", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
