"""log_stamp: every line gets HH:MM:SS once, partial writes are stamped when the
line starts, install() is idempotent, and the orchestrator / tow_refresh /
ctr_child entry points install it. Offline."""
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import log_stamp  # noqa: E402

T = datetime(2026, 10, 7, 5, 0, 2)


class Stamp(unittest.TestCase):
    def test_lines(self):
        buf = io.StringIO()
        s = log_stamp._Stamped(buf, clock=lambda: T)
        s.write("one\ntwo")
        s.write(" more\n")
        s.write("\n")
        print("three", file=s)
        self.assertEqual(buf.getvalue(), "05:00:02 one\n05:00:02 two more\n05:00:02 \n05:00:02 three\n")

    def test_install_idempotent(self):
        out, err = sys.stdout, sys.stderr
        try:
            log_stamp.install()
            first = sys.stdout
            log_stamp.install()
            self.assertIs(sys.stdout, first)
        finally:
            sys.stdout, sys.stderr = out, err

    def test_entry_points_install_it(self):
        for name in ("orchestrator.py", "tow_refresh.py", "ctr_child.py"):
            self.assertIn("log_stamp.install()", (ROOT / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
