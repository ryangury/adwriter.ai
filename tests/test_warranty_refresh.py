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

WT = r"C:\adwriter"
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

SELECTED = {"V23409A", "T22954A", "CT23257A", "P71232", "PM47578",
            "DT23368B", "PM90574A", "PM92080A", "ZT22825A", "ZT22970A"}
code, out = run(["--group", "as_is,large,added"])
reposted = set(out.split("REPOST SET")[1].split("\n")[1].replace(",", " ").split())
check("as_is,large,added selects exactly Ryan's 10", reposted == SELECTED, sorted(reposted ^ SELECTED))
check("dry run lists skipped ads", "SKIPPED (not selected" in out and "[P51460] --group removed" in out)
check("dry run wrote nothing", tmp_hist.read_bytes() == real_sha)

code, out = run(["--only", "P51460"])
check("removal selectable with --only", "REPOST SET (1 " in out and "P51460" in out.split("REPOST SET")[1])

R.ORCHESTRATOR_LOCK_PATH.write_text("x")
code, out = run(["--apply", "--only", "P71232"])
check("--apply refuses while orchestrator.lock exists", tmp_hist.read_bytes() == real_sha, out[-200:])
R.ORCHESTRATOR_LOCK_PATH.unlink()

code, out = run(["--apply", "--group", "as_is,large,added"])
after = json.loads(tmp_hist.read_text(encoding="utf-8"))
check("apply backed up first", len(list(tmp.glob("ad_history.json.backup-*"))) == 1)
changed = {s for s in after if after[s] != before[s]}
check("apply changed exactly the selected 10", changed == SELECTED, sorted(changed ^ SELECTED))
for s in ("P25418", "P51460", "DT23358A", "ZT22912A"):
    check(f"unselected {s} untouched (text, warranty_sentence_date, verification, stale_phrases)", after[s] == before[s])
e = after["P71232"]
check("P71232: new sentence, date stamped, verification cleared, stale phrase added",
      "CARFAX estimates about 47 months" in e["current_ad_text"]
      and "37 months and 49,557 miles" not in e["current_ad_text"]
      and e.get("warranty_sentence_date") and e.get("verification_verdict") is None
      and any("37 months and 49,557 miles" in p for p in e.get("stale_phrases") or []))
e = after["CT23257A"]
check("CT23257A: 'sold without' removed and listed stale, no date stamp",
      "sold without" not in e["current_ad_text"] and not e.get("warranty_sentence_date")
      and any("sold without" in p for p in e["stale_phrases"]))
e = after["PM01856"] if "PM01856" in after else None
check("PM01856 (unselected, EV battery) untouched", e == before.get("PM01856"))
for s in SELECTED:
    keep = {k: v for k, v in before[s].items() if k not in R.VERIFICATION_FIELDS + (
        "paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four", "current_ad_text",
        "warranty_sentence_date", "stale_phrases")}
    if any(after[s].get(k) != v for k, v in keep.items()):
        check(f"{s}: only ad text / warranty date / verification / stale fields change", False,
              [k for k, v in keep.items() if after[s].get(k) != v])
check("live/worktree ad_history.json never written", real.read_bytes() == real_sha)
shutil.rmtree(tmp)
print()
print("FAILED: " + ", ".join(FAIL) if FAIL else "ALL PASSED")
