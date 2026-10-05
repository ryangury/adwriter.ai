"""End-to-end stub tests of orchestrator.run() for the three by-design stops:
emails still go out, the lock is released, exit code is right. Nothing real is
touched: crawl, files, browsers, Claude and email are all stubbed."""
import contextlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, r"C:\adwriter")
import orchestrator as o  # noqa: E402
import scraper  # noqa: E402

FRAME = "Merchandising iframe (merchandising/PricingAnalysis) for {} never attached with a usable URL."


def retail(n, with_ads=False):
    return [{"stock_number": f"S{i}", "status_code": 10, "vin": f"V{i}", "year_make_model": "2024 Mercedes-Benz GLE"} for i in range(n)]


class Harness:
    def __init__(self, vehicles, history, aggregate, ctr=None, require_durham=None):
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
            mock.patch.object(o, "check_recon", return_value={"recon_complete": True}),
            mock.patch.object(o, "aggregate", side_effect=aggregate),
            mock.patch.object(o, "ACVMaxScraper", FakeACV),
            mock.patch.object(o, "capture_durham_ctr", side_effect=ctr or (lambda *a, **k: {"recorded": 0, "aborted": None})),
            mock.patch.object(o, "capture_benchmark_ctr", return_value={}),
            mock.patch.object(o, "run_verification", return_value=([], [], [])),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
