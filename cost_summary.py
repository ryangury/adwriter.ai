"""cost_summary.py - the Spending page's own-log numbers, from api_cost_log rows.

Pure functions over rows (dicts or sqlite Rows with the api_cost_log columns),
so they test without a database or a server.

  * cost per ad built: a "build" is one stock on one UTC generation date with
    any generate / research_search call (retries and the no-search fallback
    included). A stock rebuilt on another day is another build. first_ad_date is
    not used. Cost per ad = month's build cost / number of builds.
  * cost per reprice: reprice calls grouped by (stock, date).
  * cost per recon update: recon_update calls grouped by (stock, date). A recon
    update makes no model call today (it is deterministic), so this is normally 0.
  * by purpose for the month.
  * unlogged = the Console's month total minus what the log holds for the same
    finished UTC days (the Console reports a day only once it has ended).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

BUILD_PURPOSES = ("generate", "research_search")
PRODUCTION_WORKSPACE = "wrkspc_016aT5EnHT2NwWUqUvrMz7zW"
DEV_WORKSPACE = "wrkspc_01212Wh4e8d4piQ6EqENibMB"


def _get(r: Any, k: str) -> Any:
    try:
        return r[k]
    except (KeyError, IndexError, TypeError):
        return getattr(r, k, None)


def split_by_workspace(rows: Iterable[Any]) -> tuple[list, list, list]:
    """(production, dev, other) rows. The anthropic-workspace-id recorded on the
    row decides; a row without one (an aborted request returns no headers) falls
    back to the key class it was made with."""
    prod: list = []
    dev: list = []
    other: list = []
    for r in rows:
        ws, kc = _get(r, "workspace_id"), _get(r, "key_class")
        if ws == PRODUCTION_WORKSPACE or (not ws and kc == "production"):
            prod.append(r)
        elif ws == DEV_WORKSPACE or (not ws and kc == "dev"):
            dev.append(r)
        else:
            other.append(r)
    return prod, dev, other


def _day(ts: str) -> str:
    return (ts or "")[:10]


def _cost(r: Any) -> float:
    return float(_get(r, "cost_usd") or 0.0)


def _per_group(rows: list, purposes: tuple[str, ...]) -> dict[str, Any]:
    groups: dict[tuple, float] = defaultdict(float)
    for r in rows:
        if _get(r, "purpose") in purposes:
            groups[(_get(r, "stock") or "?", _day(_get(r, "ts")))] += _cost(r)
    n = len(groups)
    total = sum(groups.values())
    top = sorted(groups.items(), key=lambda kv: -kv[1])[:10]
    return {"count": n, "total": total, "each": (total / n) if n else None,
            "top": [{"stock": s, "day": d, "cost": c} for (s, d), c in top]}


def summarize(rows: Iterable[Any], console_total: float | None = None, now: datetime | None = None) -> dict[str, Any]:
    rows = list(rows)
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    by_purpose: dict[str, dict[str, float]] = {}
    for r in rows:
        p = by_purpose.setdefault(_get(r, "purpose") or "other", {
            "calls": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0,
            "searches": 0, "retries": 0, "aborted": 0})
        p["calls"] += 1
        p["cost"] += _cost(r)
        p["input_tokens"] += int(_get(r, "input_tokens") or 0)
        p["output_tokens"] += int(_get(r, "output_tokens") or 0)
        p["cache_tokens"] += int(_get(r, "cache_creation_tokens") or 0) + int(_get(r, "cache_read_tokens") or 0)
        p["searches"] += int(_get(r, "web_search_requests") or 0)
        p["retries"] += 1 if _get(r, "retry") else 0
        p["aborted"] += 1 if str(_get(r, "stop_reason") or "").startswith(("aborted", "error", "guard")) else 0

    logged_total = sum(_cost(r) for r in rows)
    complete = [r for r in rows if _day(_get(r, "ts")) < today]
    logged_complete = sum(_cost(r) for r in complete)
    unlogged = None if console_total is None else console_total - logged_complete
    return {
        "builds": _per_group(rows, BUILD_PURPOSES),
        "reprices": _per_group(rows, ("reprice",)),
        "recon_updates": _per_group(rows, ("recon_update",)),
        "by_purpose": sorted(({"purpose": k, **v} for k, v in by_purpose.items()), key=lambda d: -d["cost"]),
        "logged_total": logged_total,
        "logged_complete_days": logged_complete,
        "today_logged": logged_total - logged_complete,
        "console_total": console_total,
        "unlogged": unlogged,
        "unpriced_calls": sum(1 for r in rows if _get(r, "cost_usd") is None),
        "calls": len(rows),
    }
