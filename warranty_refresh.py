#!/usr/bin/env python3
"""warranty_refresh.py — put the CARFAX-estimate factory-warranty sentence into the
stored ads, deterministically (no model call).

    python warranty_refresh.py            # dry run (default): per-ad diff, nothing written
    python warranty_refresh.py --group as_is,large --only P51460 [--apply]
                                          # --apply needs --only and/or --group; it backs up
                                          # ad_history.json first and writes only the selected ads
    groups: as_is (every status-13 change), large (>5 months), small (3-5),
            unchanged (0-2), added (new status 11/12 sentence); a removal
            (P51460) is selectable only by --only

For every LIVE ad (in the inventory snapshot, not flagged absent):
  status 10 / 16  the old factory-warranty sentence in paragraph two is replaced in
                  place by the new one (or removed when none is built)
  status 13       any old factory sentence leaves paragraph two; paragraph three gets
                  the new one after the services sentence and loses "This vehicle is
                  sold without dealer warranty or roadside assistance."
  status 11 / 12  paragraph three gets the new one right before the program warranty
EV battery-warranty sentences and Hendrick program / MB CPO wording are never touched.
Gone cars are skipped. ZT22912A is left unchanged (Carfax has no odometer reading).

Only ads whose text actually changes get warranty_sentence_date, cleared
verification fields (so the verifier re-checks and the Ad Posting Alert lists them
for reposting) and the removed sentences appended to stale_phrases.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import adwriter as A
from aggregator import factory_warranty_remaining, factory_warranty_sentence
from run_lock import ORCHESTRATOR_LOCK_PATH
from vehicle_cache import get_vehicle

HERE = Path(__file__).resolve().parent
SNAPSHOT_PATH = HERE / "last_inventory_snapshot.json"
LEAVE_UNCHANGED = {"ZT22912A": "Carfax has no odometer reading, Ryan to decide"}
VERIFICATION_FIELDS = A.VERIFICATION_FIELDS    # one list, defined where the text is stored

# Every factory-warranty-remaining wording an ad may carry: the old CPO sentence
# ("This vehicle carries an estimated N months and M miles of remaining factory
# warranty coverage, with an additional year ..."), the 2026-10-04 As-Is form,
# and the current CARFAX-estimate sentence. Never the battery or program text.
OLD_FACTORY_RE = re.compile(
    r"^(?:This vehicle (?:also )?carries an estimated \d+ months (?:and|or) [\d,]+ miles of remaining (?:\S+(?: \S+)? )?factory warranty"
    r"|This vehicle carries \d+ months and [\d,]+ miles of remaining \S+(?: \S+)? factory warranty)",
    re.IGNORECASE,
)
_OLD_NUMBERS_RE = re.compile(r"(\d+) months (?:and|or) ([\d,]+) miles", re.IGNORECASE)
_NEW_NUMBERS_RE = re.compile(r"about (\d+) months remain .*? about ([\d,]+) miles", re.IGNORECASE)


def _is_factory(u: str) -> bool:
    return bool(OLD_FACTORY_RE.match(u) or A._FACTORY_WARRANTY_RE.match(u))


def _numbers(sentence: str | None) -> tuple[int | None, int | None]:
    if not sentence:
        return None, None
    m = _NEW_NUMBERS_RE.search(sentence) or _OLD_NUMBERS_RE.search(sentence)
    return (int(m.group(1)), int(m.group(2).replace(",", ""))) if m else (None, None)


def _p2_with(p2: str, new: str | None, keep_new: bool) -> tuple[str, list[str]]:
    """Paragraph two with its factory sentence replaced in place by `new`
    (keep_new) or removed. Returns (paragraph, removed sentences)."""
    units = A._units(p2, [])
    removed, out, placed = [], [], False
    for u in units:
        if _is_factory(u):
            removed.append(u)
            if keep_new and new and not placed:
                out.append(new)
                placed = True
            continue
        out.append(u)
    return " ".join(out), removed


GROUPS = ("as_is", "large", "small", "added", "unchanged")


def _selector_group(status: int, group: str) -> str:
    """--group name: every status-13 change is "as_is"; otherwise by how far
    the months move, "added" for a new 11/12 sentence, "removed" for a dropped
    one (selectable only with --only)."""
    if status == 13:
        return "as_is"
    return {"large (over 5 months)": "large", "small (3-5 months)": "small",
            "unchanged (0-2 months)": "unchanged", "sentence added": "added"}.get(group, "removed")


def plan() -> tuple[list[dict], list[tuple[str, str]]]:
    history = A.load_ad_history()
    snap = {v["stock_number"]: v for v in json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["vehicles"]}
    changes, left = [], []
    for stock, e in sorted(history.items()):
        v = snap.get(stock)
        if not v or e.get("absent_since") or not e.get("current_ad_text"):
            continue  # gone car / no ad
        if stock in LEAVE_UNCHANGED:
            left.append((stock, LEAVE_UNCHANGED[stock]))
            continue
        status = v.get("status_code")
        if status not in (10, 16, 13, 11, 12):
            continue
        row = get_vehicle(v.get("vin") or "") or {}
        try:
            cf = json.loads(row.get("carfax_json") or "null") or {}
        except ValueError:
            cf = {}
        fw = factory_warranty_remaining(v.get("year_make_model"), {"mileage": v.get("mileage")}, cf)
        new = factory_warranty_sentence(fw)
        paras = {k: e.get(k) or "" for k in ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")}
        after = dict(paras)
        removed: list[str] = []
        if status in (10, 16):
            after["paragraph_two"], removed = _p2_with(paras["paragraph_two"], new, keep_new=True)
        else:
            after["paragraph_two"], removed = _p2_with(paras["paragraph_two"], None, keep_new=False)
            old_p3_units = A._units(paras["paragraph_three"], [])
            if status == 13:
                after["paragraph_three"] = A.asis_paragraph_three_with_warranty(paras["paragraph_three"], new)
            else:
                after["paragraph_three"] = A.hendrick_paragraph_three_with_factory_warranty(paras["paragraph_three"], new)
            new_p3_units = set(A._units(after["paragraph_three"], []))
            removed += [u for u in old_p3_units if u not in new_p3_units]
        if after == paras:
            continue
        old_sentence = next((r for r in removed if _is_factory(r)), None)
        om, omi = _numbers(old_sentence)
        nm, nmi = _numbers(new)
        if old_sentence and not new:
            group = "sentence removed"
        elif new and not old_sentence:
            group = "sentence added"
        elif not new and not old_sentence:
            group = "As-Is line removed (no sentence built)"
        else:
            d = abs((nm or 0) - (om or 0))
            group = "unchanged (0-2 months)" if d <= 2 else ("small (3-5 months)" if d <= 5 else "large (over 5 months)")
        changes.append({
            "stock": stock, "ymm": v.get("year_make_model"), "status": status, "group": group,
            "selector_group": _selector_group(status, group),
            "old_months": om, "new_months": nm, "old_miles": omi, "new_miles": nmi,
            "reason": None if new else fw.get("reason"), "removed": removed, "new": new,
            "before": paras, "after": after,
        })
    return changes, left


def apply(changes: list[dict]) -> None:
    if ORCHESTRATOR_LOCK_PATH.exists():
        sys.exit("orchestrator.lock is present — a run may be in progress; not applying.")
    backup = A.AD_HISTORY_PATH.with_name(f"ad_history.json.backup-{date.today().isoformat()}-warranty-refresh")
    shutil.copy2(A.AD_HISTORY_PATH, backup)
    print(f"backed up ad_history.json -> {backup.name}")
    history = A.load_ad_history()
    today = date.today().isoformat()
    for c in changes:
        e = history[c["stock"]]
        e.update(c["after"])
        A.set_current_ad_text(e, "\n\n".join(p for p in c["after"].values() if p))
        if c["new"]:
            e["warranty_sentence_date"] = today
        else:
            e.pop("warranty_sentence_date", None)
        A.clear_verification(e)    # the edit changed the text even if the join came out equal
        stale = list(e.get("stale_phrases") or [])
        e["stale_phrases"] = stale + [r for r in c["removed"] if r not in stale]
    A.save_ad_history(history)
    print(f"applied to {len(changes)} ads")


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: show the diff, write nothing")
    mode.add_argument("--apply", action="store_true", help="write the selected ads (needs --only and/or --group)")
    ap.add_argument("--only", default="", help="STOCK[,STOCK...]")
    ap.add_argument("--group", default="", help="comma list of: " + ", ".join(GROUPS))
    args = ap.parse_args(argv)
    args.only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    args.group = {x.strip().lower() for x in args.group.split(",") if x.strip()}
    bad = args.group - set(GROUPS)
    if bad:
        ap.error(f"unknown --group {', '.join(sorted(bad))}; choose from {', '.join(GROUPS)}")
    if args.apply and not (args.only or args.group):
        ap.error("--apply needs a selector: --only STOCK[,STOCK...] and/or --group {" + ",".join(GROUPS) + "}")
    return args


def main(argv: list[str]) -> int:
    args = _parse(argv)
    changes, left = plan()
    selecting = bool(args.only or args.group)
    for c in changes:
        c["selected"] = (not selecting) or c["stock"] in args.only or c["selector_group"] in args.group
    known = {c["stock"] for c in changes} | {s for s, _ in left}
    unknown = sorted(args.only - known)
    selected = [c for c in changes if c["selected"]]
    skipped = [c for c in changes if not c["selected"]]
    order = ["large (over 5 months)", "small (3-5 months)", "unchanged (0-2 months)", "sentence added",
             "sentence removed", "As-Is line removed (no sentence built)"]
    sel_desc = ("selector: " + " ".join(filter(None, [
        f"--group {','.join(sorted(args.group))}" if args.group else "",
        f"--only {','.join(sorted(args.only))}" if args.only else ""]))) if selecting else "no selector (all shown)"
    print(f"WARRANTY REFRESH — {'APPLY' if args.apply else 'DRY RUN (nothing written)'} — {date.today()} — {sel_desc}")
    for g in order:
        rows = [c for c in selected if c["group"] == g]
        if not rows:
            continue
        print(f"\n{'=' * 78}\n{g.upper()}: {len(rows)}\n{'=' * 78}")
        for c in rows:
            mv = ""
            if c["old_months"] is not None or c["new_months"] is not None:
                mv = (f"  months {c['old_months']} -> {c['new_months']}, miles "
                      f"{c['old_miles'] if c['old_miles'] is None else format(c['old_miles'], ',')} -> "
                      f"{c['new_miles'] if c['new_miles'] is None else format(c['new_miles'], ',')}")
            print(f"\n[{c['stock']}] {c['ymm']} (status {c['status']}, --group {c['selector_group']}){mv}"
                  + (f"  — {c['reason']}" if c["reason"] else ""))
            for k in ("paragraph_two", "paragraph_three"):
                if c["before"][k] != c["after"][k]:
                    print(f"  {k}:\n    - {c['before'][k]}\n    + {c['after'][k]}")
            for r in c["removed"]:
                print(f"  stale_phrase: {r}")
    if selecting:
        print(f"\n{'=' * 78}\nSKIPPED (not selected; text, warranty_sentence_date and verification fields untouched): {len(skipped)}")
        for c in skipped:
            print(f"  [{c['stock']}] --group {c['selector_group']}: {c['group']}")
    print(f"\n{'=' * 78}\nLEFT UNCHANGED: {len(left)}")
    for s_, why in left:
        print(f"  [{s_}] {why}")
    if unknown:
        print(f"\n--only stocks with no change to make (gone, no ad, or already current): {', '.join(unknown)}")
    print(f"\nREPOST SET ({len(selected)} selected ads whose text changes; verification fields cleared on --apply):")
    print("  " + ", ".join(c["stock"] for c in selected))
    if args.apply:
        apply(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
