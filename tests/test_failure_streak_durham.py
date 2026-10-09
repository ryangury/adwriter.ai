"""Stub tests for ACV Task 2a (identical-failure streak) and 2c (Durham check)."""
import sys
import unittest
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import ctr_warmup  # noqa: E402
import orchestrator  # noqa: E402
import scraper  # noqa: E402
from failure_streak import FailureStreak, failure_key  # noqa: E402

FRAME = "Merchandising iframe (merchandising/PricingAnalysis) for {} never attached with a usable URL."


class StreakTests(unittest.TestCase):
    def test_ids_masked(self):
        self.assertEqual(failure_key(FRAME.format(86606789)), failure_key(FRAME.format(87712350)))
        self.assertNotEqual(failure_key(FRAME.format(1)), failure_key("Stock #P1: expected 1 inventory match, got 0"))

    def test_five_identical_trips(self):
        s = FailureStreak("t")
        self.assertEqual([s.fail(FRAME.format(i)) for i in range(5)], [False] * 4 + [True])

    def test_different_failure_or_success_resets(self):
        s = FailureStreak("t")
        for i in range(4):
            s.fail(FRAME.format(i))
        self.assertFalse(s.fail("Stock #X: something else"))
        for i in range(4):
            self.assertFalse(s.fail(FRAME.format(i)))
        s.ok()
        self.assertFalse(s.fail(FRAME.format(9)))

    def test_build_loop_counts_only_aggregate_failures(self):
        f = orchestrator._aggregate_failure
        self.assertEqual(f([{"phase": "aggregate", "error": "boom 1"}]), "boom 1")
        self.assertIsNone(f([{"phase": "aggregate", "error": "ReconVision timeout — will retry"}]))
        self.assertIsNone(f([{"phase": "sources", "error": "carfax: x"}]))
        self.assertIsNone(f([]))


class CtrLoopTests(unittest.TestCase):
    def test_ctr_loop_stops_after_five(self):
        acv = mock.MagicMock()
        n = iter(range(100))
        acv.scrape_pricing.side_effect = lambda stock: (_ for _ in ()).throw(
            scraper.PricingNotFoundError(FRAME.format(next(n)))
        )
        retail = [{"stock_number": f"S{i}"} for i in range(20)]
        errors = []
        counts = ctr_warmup.capture_durham_ctr(acv, retail, dry_run=True, errors=errors, streak=FailureStreak("ctr"))
        self.assertEqual(counts["attempted"], 5)
        self.assertEqual(acv.scrape_pricing.call_count, 5)
        self.assertTrue(counts["aborted"].startswith("Merchandising iframe"))

    def test_ctr_loop_without_streak_runs_to_end(self):
        acv = mock.MagicMock()
        acv.scrape_pricing.side_effect = scraper.PricingNotFoundError(FRAME.format(1))
        counts = ctr_warmup.capture_durham_ctr(acv, [{"stock_number": f"S{i}"} for i in range(8)], dry_run=True)
        self.assertEqual(counts["attempted"], 8)
        self.assertIsNone(counts["aborted"])


class DurhamCheckTests(unittest.TestCase):
    def _ax(self, shown):
        ax = scraper.ACVMaxScraper(headless=True)
        ax.page = mock.MagicMock(url="https://my.max.auto/inventory")
        ax.current_dealership = lambda: shown
        ax._dump_debug = lambda *a, **k: None
        return ax

    def test_durham_passes(self):
        self._ax("Mercedes-Benz of Durham").require_durham("after benchmark")

    def test_other_store_raises(self):
        with self.assertRaises(scraper.WrongDealershipError) as cm:
            self._ax("Hendrick Motors of Charlotte").require_durham("after benchmark")
        self.assertIn("Charlotte", str(cm.exception))
        self.assertIsInstance(cm.exception, scraper.AcvMaxRunAbort)

    def test_no_header_raises(self):
        with self.assertRaises(scraper.WrongDealershipError):
            self._ax(None).require_durham("after benchmark")

    def test_wrong_store_aborts_run_with_alert(self):
        sent = []
        with mock.patch.object(orchestrator, "acquire_scraper_lock"), \
             mock.patch.object(orchestrator, "release_lock_if_owned", return_value=False), \
             mock.patch.object(orchestrator, "_run_inner", side_effect=scraper.WrongDealershipError(
                 "after the Northlake/Charlotte benchmark: ACV MAX shows dealership 'Hendrick Motors of Charlotte'")), \
             mock.patch.object(orchestrator, "_send_gmail", side_effect=lambda s, b: sent.append(s)):
            self.assertEqual(orchestrator.run(), 1)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("ACV Max scraping broken: after the Northlake/Charlotte benchmark"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
