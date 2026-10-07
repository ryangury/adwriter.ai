"""step_watchdog: a simulated hang. Fake child processes (no browser, no
network) that stop making progress, overrun one vehicle, or run past the
budget; the parent must kill the whole process tree (grandchild included),
skip-and-restart past a slow vehicle, and return so the run can continue."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import step_watchdog as W  # noqa: E402

CHILD = textwrap.dedent('''
    import json, subprocess, sys, time
    sys.path.insert(0, ROOT)
    from step_watchdog import ProgressWriter
    mode, progress, out = sys.argv[1], sys.argv[2], sys.argv[3]
    skip = set(sys.argv[4].split(",")) if len(sys.argv) > 4 and sys.argv[4] else set()
    p = ProgressWriter(progress)
    if mode == "silent":
        # a grandchild that would outlive us if only this process were killed
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        open(out, "w").write(json.dumps({"grandchild": g.pid}))
        p.beat("logged in")
        time.sleep(600)
    elif mode == "slow-vehicle":
        done = []
        for v in ("V1", "V2", "V3"):
            if v in skip:
                continue
            p.start(v)
            if v == "V2":
                time.sleep(600)  # stuck on one vehicle
            done.append(v)
            p.done()
        open(out, "w").write(json.dumps({"done": done}))
    elif mode == "busy":
        while True:
            p.beat("still going")
            time.sleep(0.2)
''').replace("ROOT", repr(str(ROOT)))


def _alive(pid: int) -> bool:
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    return str(pid) in r.stdout


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.script = self.d / "child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.progress = self.d / "progress.json"
        self.out = self.d / "out.json"
        self.logs = []

    def argv(self, mode):
        return lambda skipped: [sys.executable, str(self.script), mode, str(self.progress), str(self.out),
                                ",".join(skipped)]

    def run_w(self, mode, **kw):
        return W.run_watched(self.argv(mode), step="test step", progress_path=self.progress, poll_s=0.2,
                             log=self.logs.append, **kw)

    def test_no_progress_kills_the_tree(self):
        t0 = time.time()
        res = self.run_w("silent", idle_s=2, budget_s=60, item_s=60)
        self.assertEqual(res["status"], "hung")
        self.assertIn("no progress", res["reason"])
        self.assertLess(time.time() - t0, 30)
        grandchild = json.loads(self.out.read_text())["grandchild"]
        time.sleep(1)
        self.assertFalse(_alive(grandchild), "the grandchild (the browser, in production) must be killed too")

    def test_slow_vehicle_is_skipped_and_the_step_resumes(self):
        res = self.run_w("slow-vehicle", idle_s=60, budget_s=60, item_s=1.5)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["skipped"], ["V2"])
        self.assertEqual(res["restarts"], 1)
        self.assertEqual(json.loads(self.out.read_text())["done"], ["V1", "V3"])

    def test_budget(self):
        res = self.run_w("busy", idle_s=60, budget_s=2, item_s=60)
        self.assertEqual(res["status"], "budget")
        self.assertIn("budget", res["reason"])

    def test_progress_writer(self):
        p = W.ProgressWriter(self.progress, clock=lambda: 100.0)
        p.start("X1")
        self.assertEqual(W.read_progress(self.progress)["current"], "X1")
        p.done()
        st = W.read_progress(self.progress)
        self.assertEqual((st["current"], st["done"]), (None, 1))


if __name__ == "__main__":
    unittest.main()
