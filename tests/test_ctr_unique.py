"""ctr_history: one row per (day, store, stock). record_ctr upserts (a restarted
step that re-reads a vehicle replaces its row), the unique index exists, and
ctr_dedupe keeps the LAST row of each duplicate group, saving the rest to a
file first. Temp databases only."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ctr_database as C  # noqa: E402
import ctr_dedupe as D  # noqa: E402

V = {"stock_number": "S1", "vin": "VIN1", "year_make_model": "2024 GLE", "current_price": 50000.0, "days_on_lot": 5}


def ctr(at):
    return {"latest_autotrader_ctr": at, "latest_cargurus_ctr": 2.0, "latest_average_ctr": 1.5}


class Guard(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "ctr_history.db"
        p = mock.patch.object(C, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def rows(self):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT stock_number, dealership_name, autotrader_ctr FROM ctr_history ORDER BY id").fetchall()
        finally:
            c.close()

    def test_second_read_the_same_day_replaces_the_row(self):
        a = C.record_ctr(V, ctr(1.0))
        b = C.record_ctr(V, ctr(9.0))  # a restarted step reads the vehicle again
        self.assertEqual(a, b, "same row")
        self.assertEqual(self.rows(), [("S1", "Mercedes-Benz of Durham", 9.0)])

    def test_other_store_or_day_still_gets_its_own_row(self):
        C.record_ctr(V, ctr(1.0))
        C.record_ctr(V, ctr(2.0), dealership_name="Mercedes-Benz of Northlake", dealership_role="benchmark")
        with mock.patch.object(C, "date") as d:
            d.today.return_value = __import__("datetime").date(2026, 10, 9)
            C.record_ctr(V, ctr(3.0))
        self.assertEqual(len(self.rows()), 3)

    def test_unique_index_exists(self):
        C.recorded_today("x")
        c = sqlite3.connect(self.db)
        names = [r[1] for r in c.execute("PRAGMA index_list(ctr_history)")]
        c.close()
        self.assertIn("ux_ctr_history_day_store_stock", names)


class Dedupe(unittest.TestCase):
    def test_keeps_the_last_row_and_saves_the_rest(self):
        d = Path(tempfile.mkdtemp())
        db = d / "ctr_history.db"
        c = sqlite3.connect(db)
        c.executescript(C._SCHEMA)  # old database: no unique index yet
        for stock, store, day, at in [("A", "Durham", "2026-09-12", 1.0), ("A", "Durham", "2026-09-12", 2.0),
                                      ("A", "Durham", "2026-09-12", 3.0), ("A", "Durham", "2026-09-13", 4.0),
                                      ("B", "Durham", "2026-09-12", 5.0)]:
            c.execute("INSERT INTO ctr_history (date, stock_number, dealership_name, autotrader_ctr) VALUES (?,?,?,?)",
                      (day, stock, store, at))
        c.commit()
        c.close()
        # the index cannot be created while duplicates exist; opening the db still works
        with mock.patch.object(C, "DB_PATH", db):
            C.recorded_today("Durham")
            self.assertEqual(D.main(["--db", str(db)]), 0)  # dry run
            self.assertEqual(sqlite3.connect(db).execute("SELECT COUNT(*) FROM ctr_history").fetchone()[0], 5)
            with mock.patch.object(D, "date") as dd:
                dd.today.return_value = __import__("datetime").date(2026, 10, 8)
                self.assertEqual(D.main(["--db", str(db), "--apply"]), 0)
        kept = sqlite3.connect(db).execute("SELECT stock_number, date, autotrader_ctr FROM ctr_history ORDER BY id").fetchall()
        self.assertEqual(kept, [("A", "2026-09-12", 3.0), ("A", "2026-09-13", 4.0), ("B", "2026-09-12", 5.0)])
        saved = json.loads((d / "ctr_history_duplicates_2026-10-08.json").read_text())
        self.assertEqual(sorted(r["autotrader_ctr"] for r in saved), [1.0, 2.0])
        self.assertTrue((d / "ctr_history.db.backup-2026-10-08-dedupe").exists())
        with self.assertRaises(sqlite3.IntegrityError):
            c = sqlite3.connect(db)
            c.execute("INSERT INTO ctr_history (date, stock_number, dealership_name) VALUES ('2026-09-12','A','Durham')")


if __name__ == "__main__":
    unittest.main()
