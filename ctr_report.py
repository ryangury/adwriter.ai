#!/usr/bin/env python3
"""ctr_report.py — read-only analytics behind the /ctr page.

Everything reads ctr_history.db (see ctr_database.py) and never writes. CTR
values in the table are already percentages (1.2 means 1.2%).

Rules shared by the model breakdown sections:
  * One row per vehicle per store per day (a day can hold repeat scrapes — the
    last one wins).
  * "Current" means each store's latest FULL scrape day: partial runs (a day
    with well under the store's normal vehicle count) are skipped so a half-
    finished capture doesn't masquerade as the current state.
  * A model's CTR at a store is shown only when that store has MIN_VEHICLES+
    vehicles of the model on that day.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from typing import Any

from ctr_database import DB_PATH

STORES = ("Durham", "Northlake", "Charlotte")
_DEALER_TO_STORE = {
    "Mercedes-Benz of Durham": "Durham",
    "Mercedes-Benz of Northlake": "Northlake",
    "Hendrick Motors of Charlotte": "Charlotte",
}
MIN_VEHICLES = 3
FULL_DAY_FRACTION = 0.7  # a scrape day counts as "full" at >= 70% of the store's median day

MB_MODELS = ("GLE", "GLC", "GLS", "C-Class", "E-Class")
_MODEL_RES = (
    ("GLE", re.compile(r"(?<![A-Z])GLE(?![A-Z])", re.I)),
    ("GLC", re.compile(r"(?<![A-Z])GLC(?![A-Z])", re.I)),
    ("GLS", re.compile(r"(?<![A-Z])GLS(?![A-Z])", re.I)),
    ("C-Class", re.compile(r"\bC-Class\b|\bC\s?\d{3}\b|\bC\s?4[3-9]\b", re.I)),
    ("E-Class", re.compile(r"\bE-Class\b|\bE\s?\d{3}\b|\bE\s?5[3-9]\b", re.I)),
)


def normalize_model(ymm: str | None) -> str:
    """'2024 Mercedes-Benz GLE 450' -> 'GLE'; anything outside the five
    high-volume models -> 'Other'."""
    text = re.sub(r"^\d{4}\s+", "", ymm or "")
    for name, rx in _MODEL_RES:
        if rx.search(text):
            return name
    return "Other"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_rows() -> list[dict[str, Any]]:
    """All rows, one per (date, store, vehicle) — latest scrape wins."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date, dealership_name, stock_number, vin, year_make_model, "
            "autotrader_ctr, cargurus_ctr, certification_tier, ad_written, scraped_at, id "
            "FROM ctr_history ORDER BY scraped_at, id"
        ).fetchall()
    latest: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        store = _DEALER_TO_STORE.get(r["dealership_name"])
        if store is None:
            continue
        key = (r["date"], store, r["stock_number"] or r["vin"])
        latest[key] = {**dict(r), "store": store}
    return list(latest.values())


def _snapshot_dates(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Latest full scrape day per store."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        counts[r["store"]][r["date"]] += 1
    out: dict[str, str] = {}
    for store, by_day in counts.items():
        median = statistics.median(by_day.values())
        full = [d for d, n in by_day.items() if n >= FULL_DAY_FRACTION * median]
        if full:
            out[store] = max(full)
    return out


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def model_breakdown(tier: str) -> dict[str, Any]:
    """Current CarGurus / AutoTrader CTR by model and store for one
    certification tier ('mb_cpo', 'hendrick_certified', 'hendrick_affordable').

    Returns {"as_of": {store: date}, "models": [{"model", "cells": {store:
    {"n", "at", "cg", "at_n", "cg_n"} | None}}]}. A cell is None when the store
    has fewer than MIN_VEHICLES of that model."""
    rows = _load_rows()
    as_of = _snapshot_dates(rows)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["certification_tier"] != tier or as_of.get(r["store"]) != r["date"]:
            continue
        model = normalize_model(r["year_make_model"]) if tier == "mb_cpo" else "All"
        groups[(model, r["store"])].append(r)

    order = list(MB_MODELS) + ["Other"] if tier == "mb_cpo" else ["All"]
    models = []
    for model in order:
        cells: dict[str, Any] = {}
        for store in STORES:
            rs = groups.get((model, store), [])
            if len(rs) < MIN_VEHICLES:
                cells[store] = None
                continue
            at = [r["autotrader_ctr"] for r in rs if r["autotrader_ctr"] is not None]
            cg = [r["cargurus_ctr"] for r in rs if r["cargurus_ctr"] is not None]
            cells[store] = {"n": len(rs), "at": _avg(at), "cg": _avg(cg), "at_n": len(at), "cg_n": len(cg)}
        if any(cells.values()):
            models.append({"model": model, "cells": cells})
    return {"as_of": as_of, "models": models}


def before_after() -> dict[str, Any]:
    """Durham vehicles with CTR readings both before and after their ad was
    written (ctr_history.ad_written flipping to 1). Per vehicle: mean CTR after
    minus mean CTR before, per platform; the headline is the median across
    vehicles. 'solid' requires 3+ distinct days on each side; 'any' only 1+."""
    rows = [r for r in _load_rows() if r["store"] == "Durham"]
    by_vehicle: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"before": [], "after": []})
    for r in rows:
        key = r["stock_number"] or r["vin"]
        by_vehicle[key]["after" if r["ad_written"] else "before"].append(r)

    def deltas(min_days: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for label, col in (("at", "autotrader_ctr"), ("cg", "cargurus_ctr")):
            ds = []
            for sides in by_vehicle.values():
                b = [r[col] for r in sides["before"] if r[col] is not None]
                a = [r[col] for r in sides["after"] if r[col] is not None]
                if len(b) >= min_days and len(a) >= min_days:
                    ds.append(_avg(a) - _avg(b))
            out[label] = {"n": len(ds), "median": statistics.median(ds) if ds else None,
                          "improved": sum(1 for d in ds if d > 0)}
        return out

    return {"solid": deltas(3), "any": deltas(1), "vehicles_with_ctr": len(by_vehicle)}


STORE_COLORS = {"Durham": "#00263e", "Northlake": "#4f8fc0", "Charlotte": "#d98e04"}


def grouped_bar_svg(models: list[dict[str, Any]], metric: str, title: str) -> str:
    """Inline SVG grouped bar chart: one group per model, one bar per store
    (consistent store colors). `metric` is "cg" or "at". Stores with fewer than
    MIN_VEHICLES of a model simply have no bar in that group."""
    from html import escape

    vals = [c[metric] for m in models for c in m["cells"].values() if c and c[metric] is not None]
    if not vals:
        return ""
    top = max(vals)
    step = 1.0 if top > 4 else 0.5
    ymax = (int(top / step) + 1) * step
    w, h, left, bottom, topm = 760, 250, 44, 34, 28
    plot_h = h - bottom - topm
    group_w = (w - left - 10) / len(models)
    bar_w = min(26.0, group_w / (len(STORES) + 1))
    parts = [
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}" '
        f'style="width:100%;max-width:{w}px;height:auto;font:12px sans-serif">',
        f'<text x="{left}" y="16" font-weight="600" fill="currentColor">{escape(title)}</text>',
    ]
    ticks = int(ymax / step)
    for i in range(ticks + 1):
        v = i * step
        y = topm + plot_h - (v / ymax) * plot_h
        parts.append(f'<line x1="{left}" x2="{w - 10}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#6b7280">{v:g}%</text>')
    for gi, m in enumerate(models):
        gx = left + gi * group_w + (group_w - bar_w * len(STORES)) / 2
        for si, store in enumerate(STORES):
            cell = m["cells"].get(store)
            if not cell or cell[metric] is None:
                continue
            bh = (cell[metric] / ymax) * plot_h
            x, y = gx + si * bar_w, topm + plot_h - bh
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 2:.1f}" height="{bh:.1f}" '
                f'fill="{STORE_COLORS[store]}"><title>{store} {m["model"]}: '
                f'{cell[metric]:.2f}% (n={cell["n"]})</title></rect>'
            )
        parts.append(
            f'<text x="{left + gi * group_w + group_w / 2:.1f}" y="{h - 12}" text-anchor="middle" '
            f'fill="currentColor">{escape(m["model"])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
