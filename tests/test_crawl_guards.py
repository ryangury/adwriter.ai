"""Stub tests for ACV Task 3: wrong-store crawl and low-overlap crawl are
rejected. No browser; snapshot path and sticker cache point at temp copies."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path)
import inventory_crawler as ic  # noqa: E402
import scraper  # noqa: E402

REAL_SNAPSHOT = json.loads(Path(str(_paths.DATA / "last_inventory_snapshot.json")).read_text(encoding="utf-8"))


def fake_scraper_cls(store):
    class Fake(scraper.ACVMaxScraper):
        def __enter__(self):
            self.page = mock.MagicMock(url="https://my.max.auto/inventory")
            self.page.evaluate.return_value = []
            return self

        def __exit__(self, *a):
            return False

        def login(self, *, force=False):
            pass

        def current_dealership(self):
            return store

        def _dump_debug(self, *a, **k):
            pass

    return Fake


def crawl_patches(store, snap_path):
    return [
        mock.patch.object(ic, "ACVMaxScraper", fake_scraper_cls(store)),
        mock.patch.object(ic, "_wait_for_rows", lambda page: None),
        mock.patch.object(ic, "_next_page", lambda page: False),
        mock.patch.object(ic, "_total_count", lambda page: 0),
        mock.patch.object(ic, "SNAPSHOT_PATH", snap_path),
    ]


class WrongStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.snap = Path(self.tmp.name) / "snap.json"
        self.snap.write_text("ORIGINAL", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _crawl(self, store):
        ps = crawl_patches(store, self.snap)
        for p in ps:
            p.start()
        try:
            return ic.crawl_inventory(save=True)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_wrong_store_crawl_raises_and_writes_nothing(self):
        for store in ("Hendrick Motors of Charlotte", "Mercedes-Benz of Northlake", None):
            with self.subTest(store=store):
                with self.assertRaises(scraper.WrongDealershipError):
                    self._crawl(store)
                self.assertEqual(self.snap.read_text(encoding="utf-8"), "ORIGINAL")
                self.assertNotIn("dealership", ic.LAST_CRAWL_HEALTH)

    def test_durham_crawl_saves_page_read_name(self):
        self._crawl("Mercedes-Benz of Durham")
        self.assertEqual(json.loads(self.snap.read_text(encoding="utf-8"))["dealership"], "Mercedes-Benz of Durham")

    def test_save_snapshot_refuses_without_page_read_name(self):
        ic.LAST_CRAWL_HEALTH.clear()
        with mock.patch.object(ic, "SNAPSHOT_PATH", self.snap):
            with self.assertRaises(scraper.ScraperError):
                ic.save_snapshot([{"stock_number": "X1"}])
        self.assertEqual(self.snap.read_text(encoding="utf-8"), "ORIGINAL")


def vehicles(stocks):
    return [{"stock_number": s, "vin": f"VIN{s:0>14}"} for s in stocks]


HEALTH = {"collected": 90, "total": 90, "dealership": "Mercedes-Benz of Durham"}


class OverlapTests(unittest.TestCase):
    PREV = {f"S{i}" for i in range(10)}

    def test_overlap_threshold(self):
        def healthy(n_shared):
            stocks = [f"S{i}" for i in range(n_shared)] + [f"N{i}" for i in range(10 - n_shared)]
            return ic._crawl_healthy(vehicles(stocks), HEALTH, previous_stocks=self.PREV)
        self.assertFalse(healthy(0))
        self.assertFalse(healthy(5))
        self.assertTrue(healthy(6))
        self.assertTrue(healthy(10))

    def test_no_previous_snapshot_skips_overlap(self):
        self.assertTrue(ic._crawl_healthy(vehicles(["N1"]), HEALTH, previous_stocks=None))
        self.assertIsNone(ic.snapshot_stocks(None))
        self.assertIsNone(ic.snapshot_stocks({"vehicles": []}))

    def test_previous_stocks_is_required(self):
        with self.assertRaises(TypeError):
            ic._crawl_healthy(vehicles(["S1"]), HEALTH)

    def test_low_overlap_flags_nothing(self):
        history = {"S1": {"absent_since": None}, "S2": {"absent_since": None}, "OLD": {"absent_since": "2026-09-01"}}
        before = copy.deepcopy(history)
        flagged, cleared = ic.flag_absent_ad_history(
            history, vehicles(["N1", "N2", "N3"]), "2026-10-03", nonretail={}, health=HEALTH,
            previous_stocks=self.PREV,
        )
        self.assertEqual((flagged, cleared), ([], []))
        self.assertEqual(history, before)

    def test_low_overlap_prunes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            for vin in ("AAA", "BBB"):
                (cache / f"{vin}.pdf").write_text("x")
                (cache / f"{vin}_sticker.html").write_text("x")
            with mock.patch.object(ic, "STICKER_CACHE_DIR", cache), \
                 mock.patch.dict(ic.LAST_CRAWL_HEALTH, HEALTH, clear=True):
                removed = ic.prune_sticker_cache(vehicles(["N1", "N2"]), previous_stocks=self.PREV)
            self.assertEqual(removed, 0)
            self.assertEqual(len(list(cache.iterdir())), 4)

    def test_real_snapshot_against_itself_is_healthy(self):
        prev = ic.snapshot_stocks(REAL_SNAPSHOT)
        self.assertEqual(len(prev), REAL_SNAPSHOT["count"])
        self.assertTrue(ic._crawl_healthy(REAL_SNAPSHOT["vehicles"], HEALTH, previous_stocks=prev))


if __name__ == "__main__":
    unittest.main(verbosity=2)
