#!/usr/bin/env python3
"""ad_timeline.py — per-vehicle timestamps for the future "time to post" report.

Two dates per stock number, stamped once and never overwritten:

    first_eligible_date        first crawl (the orchestrator's daily one or
                               the verifier's) that saw the vehicle at a
                               build-eligible status (10/11/12/13/16)
    first_eligible_at          the same moment with the local time of day
                               (ISO, seconds); absent on stamps made before
                               it was recorded - those are never backfilled
    first_confirmed_live_date  first verifier run where identity_confirmed
                               became True (the live VDP matched our ad)

These live in ad_timeline.json, NOT on the ad_history.json entries: the
orchestrator treats "an ad_history entry exists" as "this vehicle already has
an ad", so a stub entry written at eligibility time (before any ad is built)
would stop the vehicle from ever being queued. A vehicle that is eligible but
not yet built has to be recordable without an entry.

`baseline` marks vehicles stamped on the very first run — their real eligibility
date is unknown (they were already eligible when tracking started), so a report
should exclude them from time-to-post averages.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

TIMELINE_PATH = Path(__file__).with_name("ad_timeline.json")
from status_codes import BUILD_STATUS_CODES as BUILD_ELIGIBLE_STATUS_CODES  # noqa: E402


def load_timeline() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(TIMELINE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    tmp = TIMELINE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, TIMELINE_PATH)


def stamp_eligible(
    vehicles: Iterable[dict[str, Any]], today: str, now: datetime | None = None
) -> list[str]:
    """Stamp first_eligible_date (+ first_eligible_at, the local time) for every
    vehicle at a build-eligible status that has none yet; status 1 and any other
    non-build status is ignored, so such a vehicle is stamped on the first crawl
    that sees it buildable. Idempotent: an existing stamp is never touched.
    Returns the stock numbers newly stamped."""
    now = now or datetime.now()
    first_run = not TIMELINE_PATH.exists()
    data = load_timeline()
    new: list[str] = []
    for v in vehicles:
        stock = (v.get("stock_number") or "").strip()
        sc = v.get("status_code")
        if not stock or sc not in BUILD_ELIGIBLE_STATUS_CODES:
            continue
        entry = data.setdefault(stock, {})
        if entry.get("first_eligible_date"):
            continue
        entry["first_eligible_date"] = today
        entry["first_eligible_at"] = now.isoformat(timespec="seconds")
        entry["first_eligible_status"] = sc
        if first_run:
            entry["baseline"] = True
        new.append(stock)
    if new or first_run:
        _save(data)
    return new


def stamp_confirmed_live(stock: str, today: str) -> bool:
    """Stamp first_confirmed_live_date if unset. True when newly stamped."""
    stock = (stock or "").strip()
    if not stock:
        return False
    data = load_timeline()
    entry = data.setdefault(stock, {})
    if entry.get("first_confirmed_live_date"):
        return False
    entry["first_confirmed_live_date"] = today
    _save(data)
    return True
