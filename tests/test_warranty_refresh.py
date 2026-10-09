"""warranty_refresh.py selectors. --apply runs only against a TEMP COPY of
ad_history.json (AD_HISTORY_PATH and the lock path are redirected)."""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _paths
WT = str(_paths.ROOT)
DATA = str(_paths.DATA)
os.chdir(WT)
sys.path.insert(0, WT)
import adwriter as A  # noqa: E402
import warranty_refresh as R  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))
    if not ok:
        FAIL.append(name)


def run(argv):
    buf = io.StringIO()
    code = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = R.main(argv)
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


tmp = Path(tempfile.mkdtemp())
real = A.AD_HISTORY_PATH
tmp_hist = tmp / "ad_history.json"
shutil.copy2(real, tmp_hist)
real_sha = real.read_bytes()
A.AD_HISTORY_PATH = tmp_hist
R.ORCHESTRATOR_LOCK_PATH = tmp / "orchestrator.lock"
before = json.loads(tmp_hist.read_text(encoding="utf-8"))

code, out = run(["--apply"])
check("--apply without a selector refuses", code == 2 and "needs a selector" in out, out[-300:])
check("refused apply wrote nothing and made no backup",
      tmp_hist.read_bytes() == real_sha and not list(tmp.glob("ad_history.json.backup-*")))
code, out = run(["--group", "bogus"])
check("unknown --group rejected", code == 2 and "unknown --group" in out, out[-200:])


# The data moves every day (runs reprice ads; the 2026-10-05 apply already
# refreshed the original ten), so the selection is read from the dry run and
# the apply is checked against it, not against a fixed list.
code, out = run(["--group", "as_is,large,added"])
section = out.split("REPOST SET")[1].split("\n")
selected = set(section[1].replace(",", " ").split()) if len(section) > 1 else set()
check("dry run wrote nothing", tmp_hist.read_bytes() == real_sha)
check("dry run prints a repost set and the skipped list", "REPOST SET (" in out)

R.ORCHESTRATOR_LOCK_PATH.write_text("x")
code, out = run(["--apply", "--group", "as_is,large,added"])
check("--apply refuses while orchestrator.lock exists", tmp_hist.read_bytes() == real_sha, out[-200:])
R.ORCHESTRATOR_LOCK_PATH.unlink()

code, out = run(["--apply", "--group", "as_is,large,added"])
after = json.loads(tmp_hist.read_text(encoding="utf-8"))
check("apply backed up first", len(list(tmp.glob("ad_history.json.backup-*"))) == 1)
changed = {s for s in after if after[s] != before[s]}
check("apply changed exactly the dry run's selection", changed == selected, sorted(changed ^ selected))
for s in sorted(set(before) - selected)[:20]:
    check(f"unselected {s} untouched", after.get(s) == before[s])
for s in selected:
    e = after[s]
    check(f"{s}: verification cleared", e.get("verification_verdict") is None)
    check(f"{s}: stale_phrases kept and extended", set(before[s].get("stale_phrases") or []) <= set(e.get("stale_phrases") or []))
    keep = {k: v for k, v in before[s].items() if k not in R.VERIFICATION_FIELDS + (
        "paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four", "current_ad_text",
        "warranty_sentence_date", "stale_phrases")}
    if any(after[s].get(k) != v for k, v in keep.items()):
        check(f"{s}: only ad text / warranty date / verification / stale fields change", False,
              [k for k, v in keep.items() if after[s].get(k) != v])
code, out = run(["--apply", "--group", "as_is,large,added"])
again = json.loads(tmp_hist.read_text(encoding="utf-8"))
check("a second apply of the same groups changes nothing (idempotent)",
      {s for s in again if again[s] != after[s]} == set(), sorted(s for s in again if again[s] != after[s]))
check("live ad_history.json never written", real.read_bytes() == real_sha)
shutil.rmtree(tmp)
print()
print("FAILED: " + ", ".join(FAIL) if FAIL else "ALL PASSED")