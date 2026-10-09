#!/usr/bin/env python3
"""run_tests.py — run every tests/test_*.py offline.

    python tests/run_tests.py            # all tests
    python tests/run_tests.py towing     # only files whose name contains "towing"

Each test file runs in its own process with tests/_netguard on PYTHONPATH:
any outbound connection or DNS lookup raises and is logged, and a test whose
log is not empty FAILS even if it otherwise passed (the code under test may
have swallowed the error). A file that genuinely needs the network says so on
a line of its own:  # netguard: allow-network
A file that checks the guard itself says:        # netguard: expect-blocked

A test fails on a non-zero exit, a unittest "FAILED (...)" summary, or a
check-style "FAIL ..." line. Exit status 1 if any test failed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
GUARD = TESTS / "_netguard"
TIMEOUT_S = 300

sys.path.insert(0, str(TESTS))
import _paths  # noqa: E402

# Live files the code under test reads from beside its own modules. A git
# worktree has none of them, so a missing one is copied in from the main
# checkout (never the other way round, never over a non-empty file).
DATA_FILES = ("*.db", "ad_history.json", "last_inventory_snapshot.json", "ad_history_removed_*.json",
              "recon_gate_state.json", "powertrain_overrides.json", "ad_timeline.json")


def seed_worktree_data() -> list[str]:
    if _paths.DATA == ROOT:
        return []
    copied = []
    for pattern in DATA_FILES:
        for src in sorted(_paths.DATA.glob(pattern)):
            dst = ROOT / src.name
            # a missing file, or the schema-only stub sqlite3.connect leaves behind
            if not dst.exists() or (dst.stat().st_size <= 16384 and src.stat().st_size > dst.stat().st_size):
                shutil.copy2(src, dst)
                copied.append(src.name)
    return copied


def run_one(path: Path) -> tuple[bool, str]:
    src = path.read_text(encoding="utf-8")
    allow = "# netguard: allow-network" in src
    expect_blocked = "# netguard: expect-blocked" in src
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "netguard.log"
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": os.pathsep.join(filter(None, [
                str(GUARD), env.get("PYTHONPATH"), str(_paths.DATA) if _paths.DATA != ROOT else None])),
            "PYTHONUTF8": "1",
            "ADWRITER_NETGUARD": "1",
            "ADWRITER_NETGUARD_LOG": str(log),
        })
        if allow:
            env["ADWRITER_ALLOW_NETWORK"] = "1"
        else:
            env.pop("ADWRITER_ALLOW_NETWORK", None)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(path)], cwd=ROOT, env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            rc = proc.returncode
        except subprocess.TimeoutExpired as exc:
            out = f"TIMEOUT after {TIMEOUT_S}s\n{exc.stdout or ''}{exc.stderr or ''}"
            rc = -1
        secs = time.monotonic() - t0
        attempts = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    lines = out.splitlines()
    failed = (
        rc != 0
        or any(ln.startswith("FAILED (") or ln.startswith("FAIL ") or ln.startswith("FAIL:") for ln in lines)
    )
    problems = []
    if failed:
        problems += [ln for ln in lines if ln.startswith(("FAIL", "ERROR", "Traceback", "TIMEOUT"))][:8] or [f"exit code {rc}"]
    if attempts and not allow and not expect_blocked:
        failed = True
        problems.append(f"{len(attempts)} outbound network attempt(s) — first: {attempts[0][:300]}")
    if expect_blocked and not attempts:
        failed = True
        problems.append("expected the network guard to record an attempt; none was recorded")
    summary = next((ln for ln in reversed(lines) if ln.startswith(("Ran ", "ALL PASSED", "OK", "FAILED"))), "")
    status = "FAIL" if failed else "ok  "
    detail = f"{status} {path.name:<38} {secs:6.1f}s  {summary}"
    return not failed, detail + "".join(f"\n       {p}" for p in problems)


def main(argv: list[str]) -> int:
    pattern = argv[0] if argv else ""
    copied = seed_worktree_data()
    if copied:
        print(f"worktree: copied {len(copied)} live data file(s) from {_paths.DATA}: {', '.join(copied)}")
    files = sorted(p for p in TESTS.glob("test_*.py") if pattern in p.name)
    results = [run_one(p) for p in files]
    for ok, line in results:
        print(line)
    n_fail = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - n_fail} passed, {n_fail} failed, {len(results)} test files (network blocked)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
