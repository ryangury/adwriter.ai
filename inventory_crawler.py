#!/usr/bin/env python3
"""inventory_crawler.py — snapshot the ACV MAX retail inventory + detect price moves.

Logs into ACV MAX, reads the full Mercedes-Benz of Durham inventory list (all
pages), keeps the retail vehicles whose status code we've mapped, writes a
timestamped snapshot to last_inventory_snapshot.json, and diffs today's prices
against the snapshot from the previous run.

    python3 inventory_crawler.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scraper import (
    ACVMAX_DEALERSHIP,
    ACVMAX_INVENTORY_URL,
    STICKER_CACHE_DIR,
    ACVMaxScraper,
    ScraperError,
)

SNAPSHOT_PATH = Path(__file__).with_name("last_inventory_snapshot.json")

# Status codes we know how to route (mirrors adwriter.py's system-prompt router:
# 1 = needs certification assigned, 10/11/12/16 = postable). Anything else is
# excluded until it's mapped.
MAPPED_STATUS_CODES = {1, 10, 11, 12, 16}

_PAGE_SAFETY_LIMIT = 100


# --------------------------------------------------------------------------- #
# small parse helpers
# --------------------------------------------------------------------------- #


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _int(text: str | None) -> int | None:
    m = re.search(r"-?[\d,]+", str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _money(text: str | None) -> float | None:
    m = re.search(r"-?\$?\s*([\d,]+(?:\.\d+)?)", str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# per-row extraction
# --------------------------------------------------------------------------- #

_ROW_JS = r"""
() => {
  const rows = [...document.querySelectorAll('tr.v-data-table__tr')];
  return rows.map(tr => {
    const link = tr.querySelector("a[href^='/inventory/']");
    const m = link ? (link.getAttribute('href') || '').match(/\/inventory\/(\d+)/) : null;
    if (!m) return null;
    const cells = [...tr.children];
    const cell = i => (cells[i] ? (cells[i].innerText || '') : '');
    const inputs = [...tr.querySelectorAll('input')];
    const tel = inputs.find(i => i.type === 'tel');
    const obj = inputs.find(i => /^(RETAIL|WHOLESALE|SOLD)$/i.test(i.value || ''));
    return {
      vehicle_id: m[1],
      details_cell: cell(1),                 // keep newlines: age / YMM / trim / body / #stock / VIN / Status: NN
      age_cell: cell(2).replace(/\s+/g, ' ').trim(),
      mileage_cell: cell(4).replace(/\s+/g, ' ').trim(),
      objective_cell: cell(5).replace(/\s+/g, ' ').trim(),
      objective_input: obj ? obj.value : null,
      price_input: tel ? tel.value : null,
    };
  }).filter(Boolean);
}
"""


def _normalize_row(raw: dict[str, Any]) -> dict[str, Any]:
    detail = raw.get("details_cell") or ""
    lines = [ln.strip() for ln in detail.splitlines() if ln.strip()]

    year_make_model = next(
        (ln for ln in lines if re.match(r"^(19|20)\d{2}\s+\S", ln)), None
    )
    vin = next(
        (ln for ln in lines if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", ln)), None
    )
    stock_idx = next((i for i, ln in enumerate(lines) if ln.startswith("#")), None)
    stock_number = lines[stock_idx][1:] if stock_idx is not None else None
    trim = lines[stock_idx - 2] if stock_idx and stock_idx >= 2 else None
    body_style = lines[stock_idx - 1] if stock_idx and stock_idx >= 1 else None

    sc = re.search(r"Status:\s*(\d+)", detail)
    status_code = int(sc.group(1)) if sc else None

    objective = (raw.get("objective_cell") or raw.get("objective_input") or "").strip()
    certified = bool(re.search(r"\bCertified\b", detail))

    return {
        "vehicle_id": raw.get("vehicle_id"),
        "stock_number": stock_number,
        "vin": vin,
        "year_make_model": _clean(year_make_model),
        "trim": _clean(trim) or None,
        "body_style": _clean(re.sub(r"\s*-\s*Certified\s*$", "", body_style or "")) or None,
        "mileage": _int(raw.get("mileage_cell")),
        "exterior_color": None,  # not exposed on the inventory list page
        "current_price": _money(raw.get("price_input")),
        "days_on_lot": _int(raw.get("age_cell")),
        "status_code": status_code,
        "certified": certified,
        "objective": objective or None,
    }


def _is_retail(v: dict[str, Any]) -> bool:
    if (v.get("objective") or "").upper() != "RETAIL":
        return False
    return v.get("status_code") in MAPPED_STATUS_CODES


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #


def _range_text(page: Any) -> str:
    try:
        m = re.search(
            r"\d[\d,]*\s*-\s*\d[\d,]*\s*of\s*\d[\d,]*", page.locator("body").inner_text()
        )
        return m.group(0) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _total_count(page: Any) -> int | None:
    m = re.search(r"of\s*(\d[\d,]*)", _range_text(page)) or re.search(
        r"(\d[\d,]*)\s+vehicles?", _safe_text(page)
    )
    return int(m.group(1).replace(",", "")) if m else None


def _safe_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text()
    except Exception:  # noqa: BLE001
        return ""


def _wait_for_rows(page: Any) -> None:
    try:
        page.wait_for_selector("tr.v-data-table__tr a[href^='/inventory/']", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1_200)


def _next_page(page: Any) -> bool:
    btn = page.locator("[aria-label='Next page']").first
    if not btn.count():
        return False
    try:
        if btn.is_disabled():
            return False
    except Exception:  # noqa: BLE001
        pass
    before = _range_text(page)
    try:
        btn.click(timeout=5_000)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(20):
        page.wait_for_timeout(500)
        if _range_text(page) and _range_text(page) != before:
            return True
    return False


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #


def crawl_inventory(
    *, headless: bool = True, fresh_login: bool = False, save: bool = True
) -> list[dict[str, Any]]:
    """Crawl every page of the ACV MAX inventory list, keep the mapped-status
    retail vehicles, and (optionally) write last_inventory_snapshot.json."""
    raw_rows: list[dict[str, Any]] = []
    with ACVMaxScraper(
        headless=headless, use_saved_session=not fresh_login
    ) as ax:
        ax.login(force=fresh_login)  # also selects "Mercedes-Benz of Durham"
        page = ax.page
        page.goto(ACVMAX_INVENTORY_URL, wait_until="domcontentloaded")
        _wait_for_rows(page)

        if not page.get_by_text(ACVMAX_DEALERSHIP, exact=False).count():
            print(
                f"[crawler] warning: '{ACVMAX_DEALERSHIP}' not shown on the inventory "
                f"page — the dealership selector may not have applied.",
                file=sys.stderr,
            )

        total = _total_count(page)
        seen: set[str] = set()
        pages = 0
        while pages < _PAGE_SAFETY_LIMIT:
            _wait_for_rows(page)
            for raw in page.evaluate(_ROW_JS) or []:
                vid = raw.get("vehicle_id")
                if vid and vid not in seen:
                    seen.add(vid)
                    raw_rows.append(raw)
            pages += 1
            print(
                f"[crawler] page {pages}: {len(raw_rows)} vehicle(s) collected"
                + (f" / {total}" if total else "")
            )
            if total and len(raw_rows) >= total:
                break
            if not _next_page(page):
                break

    vehicles = [_normalize_row(r) for r in raw_rows]
    retail = [v for v in vehicles if _is_retail(v)]

    excluded = len(vehicles) - len(retail)
    wholesale = sum(
        1 for v in vehicles if (v.get("objective") or "").upper() != "RETAIL"
    )
    unmapped = sum(
        1
        for v in vehicles
        if (v.get("objective") or "").upper() == "RETAIL"
        and v.get("status_code") not in MAPPED_STATUS_CODES
    )
    print(
        f"[crawler] {len(vehicles)} vehicles crawled, {len(retail)} retail (mapped "
        f"status), {excluded} excluded ({wholesale} wholesale/sold, {unmapped} "
        f"unmapped status code)"
    )

    if save:
        save_snapshot(retail)
    return retail


# --------------------------------------------------------------------------- #
# snapshot + price-change detection
# --------------------------------------------------------------------------- #


def save_snapshot(vehicles: list[dict[str, Any]]) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dealership": ACVMAX_DEALERSHIP,
        "count": len(vehicles),
        "vehicles": vehicles,
    }
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[crawler] wrote {SNAPSHOT_PATH.name} ({len(vehicles)} vehicles)")


def prune_sticker_cache(vehicles: list[dict[str, Any]]) -> int:
    """Delete sticker_cache/<VIN>.pdf and <VIN>_sticker.html for every VIN not
    in `vehicles` (a fresh crawl's active inventory) — sold/transferred units.
    <VIN>.png files are left alone: vision_processor.py manages those. Skipped
    entirely on an empty crawl so a failed/blank scrape can't wipe the cache.
    Returns the number of files removed."""
    active = {(v.get("vin") or "").strip().upper() for v in vehicles if v.get("vin")}
    if not active:
        print("[sticker-cache] no VINs in this crawl, skipping cleanup")
        return 0
    removed = 0
    for path in [*STICKER_CACHE_DIR.glob("*.pdf"), *STICKER_CACHE_DIR.glob("*_sticker.html")]:
        vin = path.name.removesuffix("_sticker.html").removesuffix(".pdf").upper()
        if vin in active:
            continue
        try:
            path.unlink()
        except OSError as exc:
            print(f"[sticker-cache] could not remove {path.name}: {exc}")
            continue
        removed += 1
        print(f"[sticker-cache] removed {path.name} — no longer in inventory")
    return removed


def load_previous_snapshot() -> dict[str, Any] | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _as_list(snapshot: Any) -> list[dict[str, Any]]:
    if isinstance(snapshot, dict):
        return snapshot.get("vehicles") or []
    return snapshot or []


def detect_price_changes(
    current_snapshot: Any, previous_snapshot: Any
) -> list[dict[str, Any]]:
    """Compare current prices against the previous snapshot. Returns one entry
    per vehicle whose current_price changed, with old_price, new_price and the
    dollar difference (new - old).

    This is informational only (day-over-day inventory movement) — it is NOT
    what drives the reprice queue. Comparing against yesterday's snapshot lets
    a price change go undetected forever if the run that saw it crashes before
    the ad gets rewritten (the snapshot is saved before ad generation runs, so
    the next run's diff comes up empty even though the ad was never fixed).
    See detect_reprices_needed() for the reprice-queue source of truth, which
    compares against ad_history's last_price_at_write instead.
    """
    current = _as_list(current_snapshot)
    previous = _as_list(previous_snapshot)
    if not previous:
        return []

    prev_by_stock = {
        v.get("stock_number"): v for v in previous if v.get("stock_number")
    }
    changes: list[dict[str, Any]] = []
    for v in current:
        stock = v.get("stock_number")
        prev = prev_by_stock.get(stock)
        if not prev:
            continue
        old_price = prev.get("current_price")
        new_price = v.get("current_price")
        if old_price is None or new_price is None or old_price == new_price:
            continue
        changes.append(
            {
                "stock_number": stock,
                "vin": v.get("vin"),
                "year_make_model": v.get("year_make_model"),
                "old_price": old_price,
                "new_price": new_price,
                "difference": round(new_price - old_price, 2),
            }
        )
    return changes


# Minimum dollar move (in either direction) between the price an ad was
# written at and the vehicle's live ACV Max price before it's worth spending a
# Claude call to rewrite paragraph two. Set to $50 to match how often the
# store actually adjusts pricing (small $100-200 tickles happen regularly and
# should be caught) while still filtering out sub-$50 noise from scrape
# timing or rounding.
MIN_REPRICE_THRESHOLD = 50


def detect_reprices_needed(
    current_snapshot: Any,
    ad_history: dict[str, Any],
    *,
    threshold: float = MIN_REPRICE_THRESHOLD,
) -> list[dict[str, Any]]:
    """The reprice-queue source of truth: compare each vehicle's LIVE current
    price against the price its ad was last written at
    (ad_history[stock]["last_price_at_write"]) — not against yesterday's
    inventory snapshot. A vehicle with no ad_history entry (or no
    last_price_at_write on it) is skipped; there's no ad to reprice.

    Returns one entry per vehicle whose price has moved by at least
    `threshold` dollars (either direction) since the ad was written, with
    old_price/new_price/difference/ad_written_date."""
    changes: list[dict[str, Any]] = []
    for v in _as_list(current_snapshot):
        stock = v.get("stock_number")
        if not stock:
            continue
        entry = ad_history.get(stock)
        if not entry:
            continue  # no ad on record -> nothing to reprice

        old_price = entry.get("last_price_at_write")
        new_price = v.get("current_price")
        if old_price is None or new_price is None:
            continue

        difference = new_price - old_price
        if abs(difference) < threshold:
            continue

        changes.append(
            {
                "stock_number": stock,
                "vin": v.get("vin"),
                "year_make_model": v.get("year_make_model"),
                "old_price": old_price,
                "new_price": new_price,
                "difference": round(difference, 2),
                "ad_written_date": entry.get("last_ad_date"),
            }
        )
    return changes


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _fmt_price(p: Any) -> str:
    try:
        return f"${float(p):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def main(argv: list[str] | None = None) -> int:
    previous = load_previous_snapshot()

    try:
        inventory = crawl_inventory(save=False)
    except ScraperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    changes = detect_price_changes(inventory, previous)
    save_snapshot(inventory)
    prune_sticker_cache(inventory)

    print()
    print("=" * 78)
    print(f"RETAIL INVENTORY — {ACVMAX_DEALERSHIP} — {len(inventory)} vehicle(s)")
    print("=" * 78)
    print(
        f"{'stock':<12} {'year / make / model':<34} {'price':>10} "
        f"{'mi':>8} {'DOL':>4} {'st':>3} {'cpo':>4}"
    )
    for v in inventory:
        mi = f"{v['mileage']:,}" if v["mileage"] is not None else ""
        dol = v["days_on_lot"] if v["days_on_lot"] is not None else ""
        st = v["status_code"] if v["status_code"] is not None else ""
        print(
            f"{(v['stock_number'] or ''):<12} "
            f"{(v['year_make_model'] or '')[:33]:<34} "
            f"{_fmt_price(v['current_price']):>10} "
            f"{mi:>8} {dol!s:>4} {st!s:>3} "
            f"{('yes' if v['certified'] else 'no'):>4}"
        )

    print()
    print("=" * 78)
    if previous is None:
        print("PRICE CHANGES — no previous snapshot on file; baseline saved.")
    elif not changes:
        print(
            f"PRICE CHANGES — none since {previous.get('timestamp', 'the last run')}"
        )
    else:
        print(
            f"PRICE CHANGES — {len(changes)} since {previous.get('timestamp', 'last run')}"
        )
        print("=" * 78)
        print(f"{'stock':<12} {'year / make / model':<34} {'old':>10} {'new':>10} {'delta':>10}")
        for c in changes:
            delta = c["difference"]
            print(
                f"{(c['stock_number'] or ''):<12} "
                f"{(c['year_make_model'] or '')[:33]:<34} "
                f"{_fmt_price(c['old_price']):>10} {_fmt_price(c['new_price']):>10} "
                f"{('+' if delta > 0 else '') + f'${delta:,.0f}':>10}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
