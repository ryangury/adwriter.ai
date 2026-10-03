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
    AcvMaxRunAbort,
    ACVMaxScraper,
    ScraperError,
)

SNAPSHOT_PATH = Path(__file__).with_name("last_inventory_snapshot.json")

# Status codes we know how to route: status_codes.MAPPED_STATUS_CODES (1 = needs
# certification assigned, 10/11/12/13/16 = postable). Anything else is excluded
# until it's mapped there.
from status_codes import MAPPED_STATUS_CODES  # noqa: E402

_PAGE_SAFETY_LIMIT = 100

# stock (upper) -> "wholesale" | "unmapped_status", for rows the most recent
# crawl_inventory() saw but dropped as non-retail. Reset on every crawl.
LAST_CRAWL_NONRETAIL: dict[str, str] = {}

# Rows collected (ALL rows, wholesale included — not just the retail list
# crawl_inventory returns) vs the page's own reported total, plus the store name
# read off the page, for the most recent crawl_inventory(). Read by
# _crawl_healthy() and save_snapshot().
LAST_CRAWL_HEALTH: dict[str, Any] = {}

# A crawl is healthy only if it collected at least this share of the rows the
# page says exist. Leaves room for the odd row that fails to render without
# accepting a genuinely partial crawl.
MIN_CRAWL_COMPLETENESS = 0.9

# ...and only if at least this share of the PREVIOUS snapshot's stock numbers
# are in it. A complete crawl of the wrong store, or of a page that rendered
# someone else's list, passes the completeness check but shares almost no stock
# numbers with yesterday; real day-to-day turnover is a handful of units.
MIN_STOCK_OVERLAP = 0.6


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
    LAST_CRAWL_HEALTH.clear()
    with ACVMaxScraper(
        headless=headless, use_saved_session=not fresh_login
    ) as ax:
        ax.login(force=fresh_login)  # also selects "Mercedes-Benz of Durham"
        page = ax.page
        page.goto(ACVMAX_INVENTORY_URL, wait_until="domcontentloaded")
        _wait_for_rows(page)

        # The store named in the header, read off the page. Anything but Durham
        # raises WrongDealershipError: nothing below (snapshot, absent flags,
        # sticker pruning, ad building) may run on another store's inventory.
        dealership = ax.require_durham("inventory crawl")

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
        # The count can be missing at first read (it was, on the 9/28 5am run)
        # but is on screen by the last page ("81-97 of 97"), so try again.
        total = total or _total_count(page)

    LAST_CRAWL_HEALTH.update(collected=len(raw_rows), total=total, dealership=dealership)

    vehicles =[_normalize_row(r) for r in raw_rows]
    retail = [v for v in vehicles if _is_retail(v)]

    # Remember why each non-retail row was dropped, so flag_absent_ad_history()
    # can label an absent stock "wholesale" / "unmapped_status" instead of
    # guessing "sold". Rows the crawl never saw at all are the truly sold ones.
    LAST_CRAWL_NONRETAIL.clear()
    for v in vehicles:
        if v.get("stock_number") and not _is_retail(v):
            LAST_CRAWL_NONRETAIL[str(v["stock_number"]).strip().upper()] = (
                "wholesale"
                if (v.get("objective") or "").upper() != "RETAIL"
                else "unmapped_status"
            )

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
    """Write `vehicles` (the most recent crawl's retail list) as the snapshot,
    stamped with the dealership read off the page by that crawl. Refuses to
    write without one, so the file never claims a store nobody checked."""
    dealership = LAST_CRAWL_HEALTH.get("dealership")
    if not dealership:
        raise ScraperError(
            "save_snapshot: no page-read dealership from crawl_inventory() - "
            "refusing to write the snapshot"
        )
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dealership": dealership,
        "count": len(vehicles),
        "vehicles": vehicles,
    }
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[crawler] wrote {SNAPSHOT_PATH.name} ({len(vehicles)} vehicles)")


def _active_vins(vehicles: list[dict[str, Any]]) -> set[str]:
    """VINs in a crawl."""
    return {(v.get("vin") or "").strip().upper() for v in vehicles if v.get("vin")}


def snapshot_stocks(snapshot: dict[str, Any] | None) -> set[str] | None:
    """Stock numbers (upper-cased) in a snapshot from load_previous_snapshot(),
    or None when there is no snapshot or it lists no stock numbers. Load it
    BEFORE the new crawl's snapshot overwrites the file."""
    stocks = {
        str(v.get("stock_number")).strip().upper()
        for v in (snapshot or {}).get("vehicles") or []
        if v.get("stock_number")
    }
    return stocks or None


def _crawl_healthy(
    vehicles: list[dict[str, Any]],
    health: dict[str, Any] | None = None,
    *,
    previous_stocks: set[str] | None,
) -> bool:
    """The crawl-health guard shared by prune_sticker_cache and
    flag_absent_ad_history: destructive follow-ups (deleting cached stickers,
    flagging ad_history entries absent) run only when this is True. Logs the
    reason when it isn't.

    Healthy means: the crawl has at least one VIN, AND it collected at least
    MIN_CRAWL_COMPLETENESS of the total the inventory page itself reports
    (`health`, default LAST_CRAWL_HEALTH), AND at least MIN_STOCK_OVERLAP of
    `previous_stocks` (the previous snapshot's stock numbers, see
    snapshot_stocks()) are in it. An unreadable page total fails closed — the
    guard can't vouch for the crawl, so nothing destructive runs until a crawl
    that can be checked. previous_stocks=None (no previous snapshot) skips only
    the overlap check; it is keyword-only and required so every caller decides."""
    if not _active_vins(vehicles):
        print("[crawl-health] WARNING: no VINs in this crawl — treating as unhealthy")
        return False
    health = LAST_CRAWL_HEALTH if health is None else health
    collected, total = health.get("collected"), health.get("total")
    if not total:
        print(
            "[crawl-health] WARNING: the page's reported inventory total was "
            "unreadable — cannot verify the crawl is complete, treating as unhealthy"
        )
        return False
    if collected is None or collected < MIN_CRAWL_COMPLETENESS * total:
        print(
            f"[crawl-health] WARNING: partial crawl — collected {collected} of the "
            f"{total} rows the page reports (< {MIN_CRAWL_COMPLETENESS:.0%}) — "
            f"treating as unhealthy"
        )
        return False
    if previous_stocks:
        present = {
            str(v.get("stock_number")).strip().upper() for v in vehicles if v.get("stock_number")
        }
        overlap = len(previous_stocks & present) / len(previous_stocks)
        if overlap < MIN_STOCK_OVERLAP:
            print(
                f"[crawl-health] WARNING: only {overlap:.0%} of the previous snapshot's "
                f"{len(previous_stocks)} stock numbers are in this crawl "
                f"(< {MIN_STOCK_OVERLAP:.0%}) — treating as unhealthy"
            )
            return False
    return True


def flag_absent_ad_history(
    history: dict[str, Any],
    vehicles: list[dict[str, Any]],
    today: str,
    nonretail: dict[str, str] | None = None,
    health: dict[str, Any] | None = None,
    *,
    previous_stocks: set[str] | None,
) -> tuple[list[str], list[str]]:
    """Keep ad_history's absent_since / absent_reason in step with a crawl.

    `vehicles` is the crawl's retail list, INCLUDING status-1 units (an
    uncertified unit is still in inventory, so it is never "absent"). A stock
    in history but not in `vehicles` gets absent_since=today (only if not
    already set) and an absent_reason: "wholesale" / "unmapped_status" when the
    crawl saw the row but dropped it (see LAST_CRAWL_NONRETAIL), else "sold".
    A flagged stock that is back in `vehicles` has both fields cleared.
    Entries are mutated in place; returns (newly_flagged, cleared) stock lists.

    Same health guard as prune_sticker_cache (_crawl_healthy): an unhealthy
    crawl — no VINs, or well short of the page's reported total — flags and
    clears nothing; so does one sharing under MIN_STOCK_OVERLAP of
    `previous_stocks` (the previous snapshot's, loaded before it was
    overwritten — see snapshot_stocks())."""
    if not _crawl_healthy(vehicles, health, previous_stocks=previous_stocks):
        print("[ad-history] unhealthy crawl, skipping absent-flagging")
        return [], []
    nonretail = LAST_CRAWL_NONRETAIL if nonretail is None else nonretail
    present = {
        str(v.get("stock_number")).strip().upper() for v in vehicles if v.get("stock_number")
    }
    flagged: list[str] = []
    cleared: list[str] = []
    for stock, entry in history.items():
        key = str(stock).strip().upper()
        if key in present:
            if entry.get("absent_since") or entry.get("absent_reason"):
                entry["absent_since"] = None
                entry["absent_reason"] = None
                cleared.append(stock)
        elif not entry.get("absent_since"):
            entry["absent_since"] = today
            entry["absent_reason"] = nonretail.get(key, "sold")
            flagged.append(stock)
    return flagged, cleared


def prune_sticker_cache(
    vehicles: list[dict[str, Any]], *, previous_stocks: set[str] | None
) -> int:
    """Delete sticker_cache/<VIN>.pdf and <VIN>_sticker.html for every VIN not
    in `vehicles` (a fresh crawl's active inventory) — sold/transferred units.
    <VIN>.png files are left alone: vision_processor.py manages those. Skipped
    entirely on an unhealthy crawl (_crawl_healthy(), including the overlap
    check against `previous_stocks`) so a failed, blank or wrong-store scrape
    can't wipe the cache. Returns the number of files removed."""
    active = _active_vins(vehicles)
    if not _crawl_healthy(vehicles, previous_stocks=previous_stocks):
        print("[sticker-cache] unhealthy crawl, skipping cleanup")
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
    except (ScraperError, AcvMaxRunAbort) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    changes = detect_price_changes(inventory, previous)
    save_snapshot(inventory)
    prune_sticker_cache(inventory, previous_stocks=snapshot_stocks(previous))

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
