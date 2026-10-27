"""The stale-website guard: site_version reads HEAD from the .git files (branch ref, packed-refs, detached, a
worktree's gitdir file); staleness() flags a HEAD that moved or a code file newer than the process; app.py imports
every lazily loaded project module at startup (EAGER_MODULES, checked against a scan of the code); every signed-in
page shows the "restart needed" banner when stale and none otherwise; /version is signed-in only; restart_site's
window checks (locks, 7:50-8:10, 10 minutes around a verifier pass). Offline: temp directories, no restart."""
import ast
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401

sys.path.insert(0, str(_paths.ROOT))
import restart_site as RS  # noqa: E402
import site_version as SV  # noqa: E402

A, B = "a" * 40, "b" * 40


def fake_repo(ref_text=None, packed=None, head=None):
    root = Path(tempfile.mkdtemp(prefix="site_version_"))
    g = root / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text(head or "ref: refs/heads/main\n", encoding="utf-8")
    if ref_text:
        (g / "refs" / "heads" / "main").write_text(ref_text + "\n", encoding="utf-8")
    if packed:
        (g / "packed-refs").write_text(f"# pack-refs\n{packed} refs/heads/main\n", encoding="utf-8")
    (root / "templates").mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(root / "app.py", (old, old))
    return root


class Head(unittest.TestCase):
    def test_ref_packed_detached_and_worktree(self):
        self.assertEqual(SV.head_commit(fake_repo(ref_text=A)), A)
        self.assertEqual(SV.head_commit(fake_repo(packed=B)), B)
        self.assertEqual(SV.head_commit(fake_repo(head=A + "\n")), A)
        main = fake_repo(ref_text=A)
        wt = Path(tempfile.mkdtemp(prefix="site_version_wt_"))
        gd = main / ".git" / "worktrees" / "x"
        gd.mkdir(parents=True)
        (gd / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (gd / "commondir").write_text("../..\n", encoding="utf-8")
        (wt / ".git").write_text(f"gitdir: {gd}\n", encoding="utf-8")
        self.assertEqual(SV.head_commit(wt), A)
        self.assertIsNone(SV.head_commit(Path(tempfile.mkdtemp())))


class Staleness(unittest.TestCase):
    def test_fresh_moved_head_and_newer_file(self):
        root = fake_repo(ref_text=A)
        started = time.time() - 60
        fresh = SV.staleness(root=root, started=started, running=A)
        self.assertEqual((fresh["stale"], fresh["reasons"]), (False, []))
        (root / ".git" / "refs" / "heads" / "main").write_text(B, encoding="utf-8")
        moved = SV.staleness(root=root, started=started, running=A)
        self.assertTrue(moved["stale"])
        self.assertIn(f"main is now {B[:7]}; the website is running {A[:7]}", moved["reasons"][0])
        (root / ".git" / "refs" / "heads" / "main").write_text(A, encoding="utf-8")
        (root / "templates" / "research.html").write_text("new", encoding="utf-8")
        newer = SV.staleness(root=root, started=started, running=A)
        self.assertEqual(newer["newer_files"], ["templates/research.html"])
        self.assertIn("1 code file(s) changed after the website started", newer["reasons"][0])


class EagerImports(unittest.TestCase):
    def test_every_lazily_imported_project_module_is_listed(self):
        root = Path(_paths.ROOT)
        project = {p.stem for p in root.glob("*.py")}
        lazy = set()
        for p in root.glob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            top = set(map(id, tree.body))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top:
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    lazy |= {n.split(".")[0] for n in names if n.split(".")[0] in project}
        lazy -= {"credentials"}                    # gitignored secrets module, imported at top level by app.py
        missing = sorted(lazy - set(SV.EAGER_MODULES))
        self.assertEqual(missing, [], "add these to site_version.EAGER_MODULES")

    def test_app_startup_imports_them(self):
        import app  # noqa: F401

        self.assertEqual([m for m in SV.EAGER_MODULES if m not in sys.modules], [])


class Banner(unittest.TestCase):
    def setUp(self):
        import app as adapp

        self.app = adapp
        adapp.app.config["TESTING"] = True
        self.client = adapp.app.test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s["authed"] = True

    STALE = {"stale": True, "reasons": [f"main is now {B[:7]}; the website is running {A[:7]}"],
             "running_commit": A, "head_commit": B, "started": "2026-10-23T08:07:01", "newer_files": []}

    def test_banner_on_signed_in_pages_only_when_stale(self):
        self.login()
        with mock.patch.object(SV, "staleness", return_value=self.STALE):
            for page in ("/about", "/inventory"):     # pages with no network call (/cost reads the billing API)
                html = self.client.get(page).get_data(as_text=True)
                self.assertIn("restart-banner", html, page)
                self.assertIn(f"main is now {B[:7]}; the website is running {A[:7]}", html)
        with mock.patch.object(SV, "staleness", return_value=dict(self.STALE, stale=False, reasons=[])):
            self.assertNotIn("restart-banner", self.client.get("/about").get_data(as_text=True))

    def test_signed_out_and_version(self):
        with mock.patch.object(SV, "staleness", return_value=self.STALE):
            self.assertNotIn("restart-banner", self.client.get("/").get_data(as_text=True))
            self.assertEqual(self.client.get("/version").status_code, 401)
            self.login()
            self.assertEqual(self.client.get("/version").get_json()["running_commit"], A)


class RestartWindow(unittest.TestCase):
    def test_blockers(self):
        day = datetime(2026, 10, 27)
        lock = Path(tempfile.mkdtemp()) / "orchestrator.lock"
        self.assertEqual(RS.blockers(day.replace(hour=9), locks=(lock,)), [])
        lock.write_text("4242", encoding="utf-8")
        self.assertEqual(RS.blockers(day.replace(hour=9), locks=(lock,)), ["orchestrator.lock exists (PID 4242)"])
        self.assertIn("7:50-8:10", RS.blockers(day.replace(hour=7, minute=55), locks=())[0])
        self.assertEqual(RS.blockers(day.replace(hour=8, minute=10), locks=()), [])
        nine = day.replace(hour=9, minute=55)
        self.assertEqual(RS.blockers(nine, locks=(), next_verifier=nine + timedelta(minutes=5)),
                         ["a verifier pass is due at 10:00"])
        self.assertEqual(RS.blockers(nine, locks=(), last_verifier=nine - timedelta(minutes=3)),
                         ["a verifier pass started at 09:52"])
        self.assertEqual(RS.blockers(nine, locks=(), next_verifier=nine + timedelta(minutes=11),
                                     last_verifier=nine - timedelta(minutes=11)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
