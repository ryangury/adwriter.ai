"""Stub tests: ReconVision login failures in the step-3 recon gate."""
import contextlib
import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import orchestrator as o  # noqa: E402
sys.path.insert(0, __import__('os').path.dirname(__file__))
from _children import fake_recorded_today, fake_run_watched, ok_children  # noqa: E402
import scraper  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402

TIMEOUT = PWTimeout('Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to "https://app.reconvision.com/"')


def vehicles(n):
    return [{"stock_number": f"S{i}", "status_code": 10, "vin": f"V{i}", "year_make_model": f"2024 Mercedes-Benz GLE {i}"} for i in range(n)]


class Harness:
    def __init__(self, n, logins, history=None, recon_open=(), latched=(), reprices=()):
        """logins: per new ReconVision session, 'ok', 'timeout' or 'enter_fail'."""
        self.sent, self.sleeps, self.rv_sessions, self.agg_calls = [], [], 0, []
        self.recon_updates, self.reprices = [], []
        script = list(logins)
        h = self

        class FakeRV:
            def __init__(self, *a, **k):
                h.rv_sessions += 1
                self.mode = script.pop(0) if script else "ok"
                self.closed = False

            def __enter__(self):
                if self.mode == "enter_fail":
                    raise scraper.ScraperError("browser launch failed")
                return self

            def __exit__(self, *a):
                self.closed = True
                return False

            def login(self, *a, **k):
                if self.mode == "timeout":
                    raise TIMEOUT

        def check_recon(stock, rv=None, vin=None):
            assert rv is not None and rv.mode == "ok"
            return {"recon_complete": stock not in recon_open}

        def aggregate(stock, skip_recon=False):
            h.agg_calls.append((stock, skip_recon))
            raise scraper.ScraperError(f"stub aggregate stop {stock}")

        class FakeACV:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, *a, **k):
                pass

            def require_durham(self, c):
                return "Mercedes-Benz of Durham"

        self.check_recon = mock.MagicMock(side_effect=check_recon)
        self.patches = [
            mock.patch.object(o, "acquire_scraper_lock"),
            mock.patch.object(o, "release_lock_if_owned", return_value=True),
            mock.patch.object(o, "load_previous_snapshot", return_value=None),
            mock.patch.object(o, "crawl_inventory", return_value=vehicles(n)),
            mock.patch.object(o, "prune_sticker_cache", return_value=0),
            mock.patch.object(o, "flag_absent_ad_history", return_value=([], [])),
            mock.patch.object(o, "save_snapshot"),
            mock.patch.object(o, "stamp_eligible", return_value=[]),
            mock.patch.object(o, "load_ad_history", side_effect=lambda: dict(history or {})),
            mock.patch.object(o, "save_ad_history"),
            mock.patch.object(o, "detect_reprices_needed", return_value=[
                {"stock_number": s, "difference": -500, "old_price": 50000, "new_price": 49500} for s in reprices]),
            mock.patch.object(o, "_load_reprice_queue", return_value=[]),
            mock.patch.object(o, "_save_reprice_queue"),
            mock.patch.object(o, "ReconVisionScraper", FakeRV),
            mock.patch.object(o, "check_recon", self.check_recon),
            mock.patch.object(o, "recon_latched", side_effect=lambda vin, stock: stock in latched),
            mock.patch.object(o, "aggregate", side_effect=aggregate),
            mock.patch.object(o, "update_recon", side_effect=lambda stock, sc=None: h.recon_updates.append(stock) or (_ for _ in ()).throw(scraper.ScraperError("stub update_recon"))),
            mock.patch.object(o, "fresh_pricing_data", side_effect=lambda stock: h.reprices.append(stock) or (_ for _ in ()).throw(scraper.ScraperError("stub reprice"))),
            mock.patch.object(o, "ACVMaxScraper", FakeACV),
            mock.patch.object(o, "recorded_today", side_effect=fake_recorded_today()),
            mock.patch.object(o, "run_watched", side_effect=fake_run_watched(ok_children)),
            mock.patch.object(o, "run_verification", return_value=([], [], [])),
            mock.patch.object(o, "send_verification_alert"),
            mock.patch.object(o, "_send_gmail", side_effect=lambda s, b: h.sent.append((s, b))),
            mock.patch.object(o.time, "sleep", side_effect=lambda s: h.sleeps.append(s)),
        ]

    def run(self):
        with contextlib.ExitStack() as st:
            for p in self.patches:
                st.enter_context(p)
            return o.run()

    def email(self, prefix):
        return [b for s, b in self.sent if s.startswith(prefix)]

    def waiting_section(self):
        body = self.email("Mercedes-Benz of Durham — Action Required")[0]
        start = body.index("WAITING ON RECON")
        return body[start: body.index("NEEDS CERTIFICATION")]

    def errors_section(self):
        body = self.email("Mercedes-Benz of Durham — Action Required")[0]
        return body[body.index("SCRAPER ERRORS"):]


class LatchedReconTests(unittest.TestCase):
    def test_latched_cars_never_wait_even_with_reconvision_down(self):
        """S0 has no ad, S1 a pre-recon ad, S2 a live ad whose price changed; recon is latched for all three and has
        since regressed in ReconVision, which is unreachable today. Nothing waits, nothing errors on recon, the
        build / recon update / reprice all go ahead, and ReconVision is never opened."""
        history = {"S1": {"current_ad_text": "A.", "recon_pending": True, "lifecycle_stage": "pre_recon"},
                   "S2": {"current_ad_text": "A.", "recon_pending": False, "lifecycle_stage": "recon_updated"}}
        h = Harness(3, ["timeout", "timeout", "timeout"], history=history, recon_open=("S0", "S1", "S2"),
                    latched=("S0", "S1", "S2"), reprices=("S2",))
        self.assertEqual(h.run(), 0)
        self.assertEqual((h.rv_sessions, h.check_recon.call_count), (0, 0), "no ReconVision read at all")
        self.assertEqual([s for s, _ in h.agg_calls], ["S0"], "the build goes ahead")
        self.assertEqual(h.recon_updates, ["S1"], "the recon update goes ahead")
        self.assertEqual(h.reprices, ["S2"], "the reprice goes ahead")
        self.assertEqual(h.email("ReconVision unreachable"), [])
        waiting = h.waiting_section()
        for s in ("[S0]", "[S1]", "[S2]"):
            self.assertNotIn(s, waiting)
        self.assertNotIn("recon  —", h.errors_section())

    def test_a_recon_updated_ad_is_never_recon_updated_again(self):
        history = {"S0": {"current_ad_text": "A.", "recon_pending": True, "lifecycle_stage": "recon_updated"}}
        h = Harness(1, ["ok"], history=history, latched=("S0",))
        self.assertEqual(h.run(), 0)
        self.assertEqual((h.recon_updates, h.check_recon.call_count), ([], 0))


class ReconGateTests(unittest.TestCase):
    def test_one_timeout_then_success(self):
        h = Harness(4, ["timeout", "ok"])
        rc = h.run()
        self.assertEqual(rc, 0)
        self.assertEqual(h.sleeps, [o.RECON_LOGIN_RETRY_WAIT_S])
        self.assertEqual(h.check_recon.call_count, 3)            # S1-S3 checked on the 2nd session
        self.assertEqual([s for s, _ in h.agg_calls], ["S1", "S2", "S3"])  # build queue unchanged
        waiting = h.waiting_section()
        print("\n  one timeout then success:\n" + waiting)
        self.assertIn("[S0]", waiting)
        self.assertIn("ReconVision unreachable — recon not checked (TimeoutError", waiting)
        self.assertNotIn("[S1]", waiting)
        self.assertEqual(h.errors_section().count("phase" if False else "  recon  —"), 1)
        self.assertEqual(h.email("ReconVision unreachable"), [])

    def test_three_timeouts_stop_the_gate(self):
        h = Harness(6, ["timeout", "timeout", "timeout"])
        rc = h.run()
        self.assertEqual(rc, 0, "the run keeps going")
        self.assertEqual(h.rv_sessions, 3, "no 4th login attempt")
        self.assertEqual(h.sleeps, [o.RECON_LOGIN_RETRY_WAIT_S] * 2)
        self.assertEqual(h.check_recon.call_count, 0)
        self.assertEqual(h.agg_calls, [])
        alerts = [s for s, _ in h.sent if s == "ReconVision unreachable"]
        self.assertEqual(len(alerts), 1)
        waiting = h.waiting_section()
        print("\n  three timeouts:\n" + waiting)
        for i in range(6):
            self.assertIn(f"[S{i}]", waiting)
        self.assertEqual(waiting.count("recon gate stopped for this run"), 3)   # S3-S5
        errs = h.errors_section()
        self.assertEqual(errs.count("  recon  —"), 4)   # S0-S2 + one gate-stop error
        self.assertIn("recon gate stopped for the rest of the run after 3 failed", errs)
        self.assertTrue(h.email("Mercedes-Benz of Durham — Action Required"))
        # later steps still ran
        o_ctr = [p for p in h.patches]  # noqa: F841

    def test_timeout_on_first_vehicle_with_pending_ad_then_queues_normal(self):
        hist = {"S0": {"current_ad_text": "x", "recon_pending": True}}
        h = Harness(4, ["enter_fail", "ok"], history=hist, recon_open={"S2"})
        rc = h.run()
        self.assertEqual(rc, 0)
        waiting = h.waiting_section()
        print("\n  first-vehicle failure (pending ad, browser launch failed):\n" + waiting)
        self.assertIn("[S0]", waiting)
        self.assertIn("ScraperError: browser launch failed", waiting)
        # S1, S3: recon complete -> build; S2: recon open -> pre-recon (existing behavior)
        self.assertEqual(sorted(h.agg_calls), [("S1", False), ("S2", True), ("S3", False)])
        self.assertEqual(h.email("ReconVision unreachable"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
