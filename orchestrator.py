#!/usr/bin/env python3
"""orchestrator.py — the daily runner.

Ties the whole pipeline together in one pass:

  1. Crawl the retail inventory, diff prices against yesterday, save a snapshot.
  2. Queue price-changed vehicles for an ad rewrite.
  3. Gate every retail vehicle on status code + ReconVision RO status.
  4. Build ads for the build queue (aggregate -> Claude, status-aware prompt).
  5. Capture CTR for every retail vehicle into ctr_history.db.
  6. Send the "Ads Ready" and "Action Required" emails.
  7. Print a completion summary.

    python3 orchestrator.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from adwriter import (
    _format_ads_ready_email,
    _generate_from_package,
    _recon_has_includeable,
    _send_gmail,
    fresh_pricing_data,
    load_ad_history,
    record_ad,
    reprice_ad,
    save_ad_history,
    source_status,
    update_recon,
)
from aggregator import DEALER_DOC_FEE, ScraperError, aggregate, check_recon
from ctr_database import BENCHMARK_DEALERSHIPS, infer_tier, record_ctr
from verifier import run_verification, send_verification_alert
from inventory_crawler import (
    crawl_inventory,
    detect_reprices_needed,
    save_snapshot,
)
from scraper import ACVMaxScraper, ReconVisionScraper
from run_lock import ScraperBusyError

BUILD_STATUS_CODES = {10, 11, 12, 13, 16}

# A vehicle whose reprice can't be processed this run (over the per-run cap)
# is persisted here and given priority next run, ahead of newly detected
# reprices — see _load_reprice_queue() / _save_reprice_queue().
REPRICE_QUEUE_PATH = Path(__file__).with_name("reprice_queue.json")
MAX_REPRICES_PER_RUN = 15


def _load_reprice_queue() -> list[dict[str, Any]]:
    if not REPRICE_QUEUE_PATH.exists():
        return []
    try:
        data = json.loads(REPRICE_QUEUE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_reprice_queue(overflow: list[dict[str, Any]]) -> None:
    REPRICE_QUEUE_PATH.write_text(json.dumps(overflow, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# single-instance lock — a stuck/hung run must never let a second one start
# and race it for ad_history.json / vehicle_cache.db / reprice_queue.json.
# Shared with app.py's /generate route via run_lock.py so Flask and the
# orchestrator never scrape the same platform at the same time.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #


def _fmt_price(p: Any) -> str:
    try:
        return f"${float(p):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _advertised(price: Any) -> Any:
    """current_price (raw ACV Max) -> advertised_price, for call sites that
    only have a retail-crawl vehicle dict (no aggregator pkg, so no
    precomputed advertised_price field) to build a subject line from."""
    try:
        return float(price) + DEALER_DOC_FEE
    except (TypeError, ValueError):
        return None


def _fmt_runtime(seconds: float) -> str:
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _dol(v: dict[str, Any]) -> str:
    d = v.get("days_on_lot")
    return f"{d} days on lot" if d is not None else "days on lot unknown"


# --------------------------------------------------------------------------- #
# per-vehicle email — subject line varies by lifecycle stage
# --------------------------------------------------------------------------- #


def _lifecycle_subject(
    stage: str,
    stock: str | None,
    ymm: str | None,
    price: Any,
    *,
    old_price: Any = None,
    new_price: Any = None,
) -> str:
    """Per-vehicle email subject line, keyed on lifecycle stage:
      active       -> "Ad Ready — [STOCK] — [YMM] — [PRICE]"
      repriced     -> "Reprice Ready — [STOCK] — [YMM] — [OLD] -> [NEW]"
      recon_updated-> "Recon Update — [STOCK] — [YMM] — [PRICE]"
      pre_recon    -> "Pre-Recon Ad — [STOCK] — [YMM] — [PRICE]"
    """
    stock = stock or "?"
    ymm = ymm or "unknown vehicle"
    if stage == "repriced":
        return (
            f"Reprice Ready — {stock} — {ymm} — "
            f"{_fmt_price(old_price)} → {_fmt_price(new_price)}"
        )
    if stage == "recon_updated":
        return f"Recon Update — {stock} — {ymm} — {_fmt_price(price)}"
    if stage == "pre_recon":
        return f"Pre-Recon Ad — {stock} — {ymm} — {_fmt_price(price)}"
    return f"Ad Ready — {stock} — {ymm} — {_fmt_price(price)}"


def _format_action_email(
    waiting_recon: list[dict[str, Any]],
    needs_cert: list[dict[str, Any]],
    price_changes: list[dict[str, Any]],
    rewritten_stocks: set[str],
    errors: list[dict[str, Any]],
    pre_recon_watching: list[dict[str, Any]] | None = None,
) -> str:
    pre_recon_watching = pre_recon_watching or []
    out: list[str] = [
        f"MERCEDES-BENZ OF DURHAM — ACTION REQUIRED  {date.today().isoformat()}",
        "",
    ]

    def section(title: str, n: int) -> None:
        out.append(f"{title} ({n})")
        out.append("=" * 60)

    section("PRE-RECON WATCHING", len(pre_recon_watching))
    if not pre_recon_watching:
        out.append("(none)")
    for w in pre_recon_watching:
        days = w.get("days_since_initial_ad")
        age = (
            f"{days} day{'s' if days != 1 else ''} since initial ad"
            if days is not None
            else "age unknown"
        )
        out.append(
            f"  [{w.get('stock_number')}]  {w.get('year_make_model') or 'unknown'}"
            f"  —  {age}"
        )
        out.append(
            "      pre-recon ad is live; recon not yet complete "
            "(paragraph one tops up when it finishes)"
        )
    out += ["", ""]

    section("WAITING ON RECON", len(waiting_recon))
    if not waiting_recon:
        out.append("(none)")
    for v in waiting_recon:
        out.append(
            f"  [{v.get('stock_number')}]  {v.get('year_make_model') or 'unknown'}"
            f"  —  {_dol(v)}"
        )
        if v.get("note"):
            out.append(f"      {v['note']}")
    out += ["", ""]

    section("NEEDS CERTIFICATION ASSIGNED", len(needs_cert))
    if not needs_cert:
        out.append("(none)")
    for v in needs_cert:
        out.append(
            f"  [{v.get('stock_number')}]  {v.get('year_make_model') or 'unknown'}"
            f"  —  {_dol(v)}   (status code 1 — not yet assigned in the system)"
        )
    out += ["", ""]

    section("PRICE CHANGES — ADS QUEUED FOR REWRITE", len(price_changes))
    if not price_changes:
        out.append("(none)")
    for c in price_changes:
        delta = c.get("difference") or 0
        sign = "+" if delta > 0 else "-"
        state = "rewritten today" if c.get("stock_number") in rewritten_stocks else "queued"
        out.append(
            f"  [{c.get('stock_number')}]  {c.get('year_make_model') or 'unknown'}"
            f"  —  {_fmt_price(c.get('old_price'))} -> {_fmt_price(c.get('new_price'))}"
            f"  ({sign}${abs(delta):,.0f})  —  {state}"
        )
    out += ["", ""]

    section("SCRAPER ERRORS", len(errors))
    if not errors:
        out.append("(none)")
    for e in errors:
        out.append(
            f"  [{e.get('stock')}]  {e.get('phase')}  —  {e.get('error')}"
        )
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def run(
    *, limit: int | None = None, send_email: bool = True, status: list[int] | None = None
) -> int:
    try:
        return _run_inner(limit=limit, send_email=send_email, status=status)
    except ScraperBusyError as exc:
        print(f"[orchestrator] {exc} — exiting", file=sys.stderr)
        return 0


def _run_inner(
    *, limit: int | None = None, send_email: bool = True, status: list[int] | None = None
) -> int:
    started = time.monotonic()
    today = date.today().isoformat()
    errors: list[dict[str, Any]] = []

    def _safe_send(subject: str, body: str) -> None:
        if not send_email:
            print(f"[email] (--no-email) would send: {subject}")
            return
        try:
            _send_gmail(subject, body)
            print(f"[email] sent: {subject}")
        except Exception as exc:  # noqa: BLE001
            print(f"[email] FAILED to send '{subject}': {exc}", file=sys.stderr)

    # --- 1. INVENTORY CRAWL ------------------------------------------------ #
    print("\n=== 1. INVENTORY CRAWL ===")
    retail = crawl_inventory(save=False)
    excluded_not_certified = [v for v in retail if v.get("status_code") == 1]
    retail = [v for v in retail if v.get("status_code") != 1]
    if excluded_not_certified:
        print(
            f"[orchestrator] excluding {len(excluded_not_certified)} vehicle(s) "
            f"at status 1 (in stock, not yet certified) — never run by design"
        )
    if status:
        retail = [v for v in retail if v.get("status_code") in status]
        print(f"[orchestrator] --status {status}: processing {len(retail)} vehicle(s)")
    if limit:
        retail = retail[:limit]
        print(f"[orchestrator] --limit {limit}: processing {len(retail)} vehicle(s)")
    save_snapshot(retail)

    # --- 2. PRICE CHANGE HANDLING --------------------------------------- #
    #
    # The reprice queue is driven by ad_history's last_price_at_write, not by
    # yesterday's inventory snapshot: a vehicle is queued when its live ACV Max
    # price has moved by >= MIN_REPRICE_THRESHOLD dollars since the ad was
    # written. (Comparing against the snapshot instead let a price change go
    # permanently undetected if the run that saw it crashed before the ad got
    # rewritten — the snapshot is saved in step 1, before ad generation ever
    # runs, so the next run's diff would come up empty.)
    print("\n=== 2. PRICE CHANGE HANDLING ===")
    ad_history = load_ad_history()
    reprice_candidates = detect_reprices_needed(retail, ad_history)
    for c in reprice_candidates:
        delta = c["difference"]
        direction = "drop" if delta < 0 else "increase"
        print(
            f"[reprice] {c['stock_number']}: price changed from "
            f"{_fmt_price(c['old_price'])} to {_fmt_price(c['new_price'])} "
            f"(${abs(delta):,.0f} {direction} since ad written on "
            f"{c.get('ad_written_date') or 'unknown date'}) — queued for rewrite"
        )

    # Previously-deferred overflow gets priority — process it first, then any
    # newly detected reprices, up to MAX_REPRICES_PER_RUN this run. Whatever's
    # left over is written back to reprice_queue.json for the next run. A
    # stock number that no longer needs a reprice (price corrected back inside
    # the threshold, vehicle sold/delisted) is silently dropped rather than
    # carried forward forever.
    candidates_by_stock = {c["stock_number"]: c for c in reprice_candidates}
    queued_stocks = [
        c["stock_number"] for c in _load_reprice_queue() if c.get("stock_number")
    ]
    ordered_stocks = [s for s in queued_stocks if s in candidates_by_stock]
    ordered_stocks += [s for s in candidates_by_stock if s not in ordered_stocks]

    this_run_stocks = set(ordered_stocks[:MAX_REPRICES_PER_RUN])
    overflow_stocks = ordered_stocks[MAX_REPRICES_PER_RUN:]
    _save_reprice_queue([candidates_by_stock[s] for s in overflow_stocks])
    print(
        f"[queue] reprices — {len(ordered_stocks)} needed, {len(this_run_stocks)} "
        f"this run, {len(overflow_stocks)} deferred (limit {MAX_REPRICES_PER_RUN})"
    )

    # price_changes feeds the Action Required email + summary below — every
    # vehicle currently needing a reprice, whether processed this run or
    # deferred to the queue file.
    price_changes = [candidates_by_stock[s] for s in ordered_stocks]
    repricing_queue = this_run_stocks

    # --- 3. RECON AND CERTIFICATION GATE ------------------------------- #
    #
    # Per retail vehicle (status 10/11/12/13/16 only):
    #   no ad yet + recon complete   -> build_queue      (lifecycle "active")
    #   no ad yet + recon incomplete -> pre_recon_queue  (lifecycle "pre_recon")
    #   recon_pending + now complete -> recon_update_queue ("recon_updated")
    #   recon_pending + still open   -> pre_recon_watching (no work, just report)
    #   has ad + price changed       -> reprice_queue    ("repriced")
    #   has ad, nothing pending      -> skip
    print("\n=== 3. RECON AND CERTIFICATION GATE ===")
    needs_cert: list[dict[str, Any]] = []
    skipped_status: list[dict[str, Any]] = []
    waiting_recon: list[dict[str, Any]] = []
    build_queue: list[dict[str, Any]] = []
    pre_recon_queue: list[dict[str, Any]] = []
    recon_update_queue: list[dict[str, Any]] = []
    reprice_queue: list[dict[str, Any]] = []

    rv: ReconVisionScraper | None = None
    try:
        for v in retail:
            stock = v.get("stock_number")
            sc = v.get("status_code")

            if sc not in BUILD_STATUS_CODES:
                if sc == 1:
                    needs_cert.append(v)
                    print(f"[gate] {stock}: status 1 — needs certification assigned")
                else:
                    skipped_status.append(v)
                    print(f"[gate] {stock}: status {sc} not in {{10,11,12,13,16}} — skipped")
                continue

            entry = ad_history.get(stock)
            price_changed = stock in repricing_queue and bool(
                (entry or {}).get("current_ad_text")
            )

            if entry:
                if entry.get("recon_pending"):
                    if rv is None:
                        rv = ReconVisionScraper(headless=True, use_saved_session=True)
                        rv.__enter__()
                        rv.login()
                    try:
                        rc = check_recon(stock, rv=rv)
                    except PlaywrightTimeoutError as exc:
                        print(f"[orchestrator] ReconVision timeout on {stock} — skipping to next vehicle")
                        errors.append(
                            {
                                "stock": stock,
                                "phase": "recon",
                                "error": f"ReconVision timeout — will retry on next run: {exc}",
                            }
                        )
                        continue
                    except ScraperError as exc:
                        errors.append({"stock": stock, "phase": "recon", "error": str(exc)})
                        print(f"[gate] {stock}: recon check failed — {exc}")
                        continue
                    if rc["recon_complete"]:
                        recon_update_queue.append(v)
                        print(f"[gate] {stock}: recon complete -> recon update queue")
                    else:
                        print(f"[gate] {stock}: pre-recon ad live, recon still open — watching")
                if price_changed:
                    reprice_queue.append(v)
                    print(f"[gate] {stock}: price changed -> reprice queue")
                if not entry.get("recon_pending") and not price_changed:
                    print(f"[gate] {stock}: ad current, nothing pending — skip")
                continue

            # No ad on record yet — decide full vs pre-recon on the recon gate.
            if rv is None:
                rv = ReconVisionScraper(headless=True, use_saved_session=True)
                rv.__enter__()
                rv.login()
            try:
                rc = check_recon(stock, rv=rv)
            except PlaywrightTimeoutError as exc:
                print(f"[orchestrator] ReconVision timeout on {stock} — skipping to next vehicle")
                errors.append(
                    {
                        "stock": stock,
                        "phase": "recon",
                        "error": f"ReconVision timeout — will retry on next run: {exc}",
                    }
                )
                continue
            except ScraperError as exc:
                errors.append({"stock": stock, "phase": "recon", "error": str(exc)})
                print(f"[gate] {stock}: recon check failed — {exc}")
                continue

            if rc["recon_complete"]:
                build_queue.append(v)
                print(f"[gate] {stock}: no ad yet, recon complete -> build queue")
            else:
                pre_recon_queue.append(v)
                print(f"[gate] {stock}: no ad yet, recon open -> pre-recon queue")
    finally:
        if rv is not None:
            rv.__exit__(None, None, None)

    # --- 4. AD GENERATION -------------------------------------------- #
    n_queued = (
        len(build_queue)
        + len(pre_recon_queue)
        + len(recon_update_queue)
        + len(reprice_queue)
    )
    print(f"\n=== 4. AD GENERATION ({n_queued} vehicle(s)) ===")
    ads_generated: list[dict[str, Any]] = []
    aggregated_ctr: dict[str, Any] = {}
    price_by_stock = {
        c["stock_number"]: c for c in price_changes if c.get("stock_number")
    }

    def _build_and_record(v: dict[str, Any], *, skip_recon: bool) -> None:
        stock = v.get("stock_number")
        stage = "pre_recon" if skip_recon else "active"
        tag = "pre-recon" if skip_recon else "build"
        print(f"[{tag}] {stock}: aggregating (skip_recon={skip_recon}) ...")
        try:
            pkg = aggregate(stock, skip_recon=skip_recon)
        except PlaywrightTimeoutError as exc:
            print(f"[orchestrator] ReconVision timeout on {stock} — skipping to next vehicle")
            errors.append(
                {
                    "stock": stock,
                    "phase": "aggregate",
                    "error": f"ReconVision timeout — will retry on next run: {exc}",
                }
            )
            return
        except ScraperError as exc:
            errors.append({"stock": stock, "phase": "aggregate", "error": str(exc)})
            print(f"[{tag}] {stock}: aggregate failed — {exc}")
            return

        if isinstance(pkg.get("ctr"), dict):
            aggregated_ctr[stock] = pkg["ctr"]

        if pkg.get("recon_complete") is False:
            waiting_recon.append({**v, "note": pkg.get("note")})
            print(f"[{tag}] {stock}: recon flipped incomplete — moved to waiting")
            return

        failed = {k: d for k, (s, d) in source_status(pkg).items() if s == "failed"}
        if failed:
            detail = "; ".join(f"{k}: {d}" for k, d in failed.items())
            errors.append({"stock": stock, "phase": "sources", "error": detail})
            print(f"[{tag}] {stock}: source failure — {detail}")
            return

        try:
            ad_copy, feedback = _generate_from_package(pkg)
        except (anthropic.APIError, RuntimeError) as exc:
            errors.append({"stock": stock, "phase": "claude", "error": str(exc)})
            print(f"[{tag}] {stock}: ad generation failed — {exc}")
            return

        vehicle = pkg.get("vehicle") or {}
        recon_incl = _recon_has_includeable(pkg.get("recon") or {})
        change_note = (
            "Pre-recon ad posted; recon sentence pending."
            if skip_recon
            else "New ad, full pipeline."
        )
        ads_generated.append(
            {
                "stock": stock,
                "vehicle": vehicle,
                "ad_copy": ad_copy,
                "feedback": feedback,
                "pkg": pkg,
                "lifecycle_stage": stage,
                "change_note": change_note,
            }
        )
        record_ad(
            ad_history,
            stock,
            ad_text=ad_copy,
            price=vehicle.get("current_price"),
            lifecycle_stage=stage,
            recon_included=recon_incl,
            recon_pending=skip_recon,
            today=today,
            last_feedback=feedback,
        )
        print(f"[{tag}] {stock}: ad generated ({stage})")

        _safe_send(
            _lifecycle_subject(
                stage, stock, vehicle.get("year_make_model"), vehicle.get("advertised_price")
            ),
            f"{change_note}\n\n{'=' * 60}\nAD COPY\n{'=' * 60}\n\n{ad_copy}\n",
        )

    for v in build_queue:
        _build_and_record(v, skip_recon=False)
    for v in pre_recon_queue:
        _build_and_record(v, skip_recon=True)

    save_ad_history(ad_history)

    # recon updates — update_recon() persists ad_history itself
    for v in recon_update_queue:
        stock = v.get("stock_number")
        print(f"[recon-update] {stock}: topping up paragraph one ...")
        try:
            ad_copy = update_recon(stock)
        except PlaywrightTimeoutError as exc:
            print(f"[orchestrator] ReconVision timeout on {stock} — skipping to next vehicle")
            errors.append(
                {
                    "stock": stock,
                    "phase": "recon_update",
                    "error": f"ReconVision timeout — will retry on next run: {exc}",
                }
            )
            continue
        except (anthropic.APIError, RuntimeError, ScraperError, ValueError) as exc:
            errors.append({"stock": stock, "phase": "recon_update", "error": str(exc)})
            print(f"[recon-update] {stock}: failed — {exc}")
            continue
        ads_generated.append(
            {
                "stock": stock,
                "vehicle": v,
                "ad_copy": ad_copy,
                "lifecycle_stage": "recon_updated",
                "change_note": "Recon completed; paragraph one now names the reconditioning work.",
            }
        )
        print(f"[recon-update] {stock}: paragraph one updated")

        _safe_send(
            _lifecycle_subject(
                "recon_updated", stock, v.get("year_make_model"), _advertised(v.get("current_price"))
            ),
            f"Recon completed; paragraph one now names the reconditioning work.\n\n"
            f"{'=' * 60}\nAD COPY\n{'=' * 60}\n\n{ad_copy}\n",
        )

    # reprices — reprice_ad() persists ad_history itself
    for v in reprice_queue:
        stock = v.get("stock_number")
        print(f"[reprice] {stock}: rewriting paragraph two ...")
        try:
            pricing_data = fresh_pricing_data(stock)
            ad_copy = reprice_ad(stock, pricing_data)
        except (anthropic.APIError, RuntimeError, ScraperError, ValueError) as exc:
            errors.append({"stock": stock, "phase": "reprice", "error": str(exc)})
            print(f"[reprice] {stock}: failed — {exc}")
            continue
        pc = price_by_stock.get(stock, {})
        note = "Pricing paragraph updated."
        if pc:
            note = (
                f"Price {_fmt_price(pc.get('old_price'))} -> "
                f"{_fmt_price(pc.get('new_price'))}; paragraph two rewritten."
            )
        ads_generated.append(
            {
                "stock": stock,
                "vehicle": v,
                "ad_copy": ad_copy,
                "lifecycle_stage": "repriced",
                "change_note": note,
            }
        )
        print(f"[reprice] {stock}: paragraph two updated")

        _safe_send(
            _lifecycle_subject(
                "repriced",
                stock,
                v.get("year_make_model"),
                v.get("current_price"),
                old_price=pc.get("old_price"),
                new_price=pc.get("new_price"),
            ),
            f"{note}\n\n{'=' * 60}\nAD COPY\n{'=' * 60}\n\n{ad_copy}\n",
        )

    # refresh the in-memory copy after update_recon / reprice_ad writes
    ad_history = load_ad_history()

    # --- 5. DURHAM CTR CAPTURE (every retail vehicle) ------------- #
    print(f"\n=== 5. DURHAM CTR CAPTURE ({len(retail)} vehicle(s)) ===")
    ctr_records = 0
    try:
        with ACVMaxScraper(headless=True) as ax:
            ax.login()
            for v in retail:
                stock = v.get("stock_number")
                ctr_data = aggregated_ctr.get(stock)
                if not isinstance(ctr_data, dict) or ctr_data.get("error"):
                    try:
                        pr = ax.scrape_pricing(stock)
                        ctr_data = ax.scrape_ctr(pr.get("vehicle_id"))
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"stock": stock, "phase": "ctr", "error": str(exc)})
                        print(f"[ctr] {stock}: scrape failed — {exc}")
                        continue
                hist = ad_history.get(stock, {})
                try:
                    record_ctr(
                        v,
                        ctr_data,
                        ad_written=bool(hist),
                        ad_written_date=hist.get("last_ad_date"),
                    )
                    ctr_records += 1
                    print(
                        f"[ctr] {stock}: recorded "
                        f"(AT {ctr_data.get('latest_autotrader_ctr')}, "
                        f"CG {ctr_data.get('latest_cargurus_ctr')}, "
                        f"avg {ctr_data.get('latest_average_ctr')})"
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append({"stock": stock, "phase": "ctr_db", "error": str(exc)})
    except ScraperError as exc:
        errors.append({"stock": "-", "phase": "ctr_login", "error": str(exc)})
        print(f"[ctr] ACV MAX login failed — {exc}", file=sys.stderr)

    # --- 6. BENCHMARK CTR CAPTURE ------------------------------- #
    print("\n=== 6. BENCHMARK CTR CAPTURE ===")
    benchmark_counts: dict[str, int] = {d: 0 for d in BENCHMARK_DEALERSHIPS}
    try:
        with ACVMaxScraper(headless=True) as bx:
            bx.login()  # lands on Mercedes-Benz of Durham
            for dealership_name in BENCHMARK_DEALERSHIPS:
                short = dealership_name.split()[-1]
                try:
                    vehicles = bx.scrape_benchmark_inventory(dealership_name)
                except Exception as exc:  # noqa: BLE001 - one store must not stop the other
                    errors.append(
                        {"stock": "-", "phase": "benchmark",
                         "error": f"{dealership_name}: {exc}"}
                    )
                    print(f"[benchmark] {short} FAILED — {exc}", file=sys.stderr)
                    continue

                n_total = len(vehicles)
                for i, veh in enumerate(vehicles, 1):
                    tier = infer_tier(
                        certified=veh.get("certified"),
                        price=veh.get("current_price"),
                    )
                    try:
                        record_ctr(
                            veh,
                            veh.get("ctr_data") or {},
                            dealership_name=dealership_name,
                            dealership_role="benchmark",
                            certification_tier=tier,
                        )
                        benchmark_counts[dealership_name] += 1
                    except Exception as exc:  # noqa: BLE001
                        errors.append(
                            {"stock": veh.get("stock_number"),
                             "phase": "benchmark_db", "error": str(exc)}
                        )
                        continue
                    print(
                        f"[benchmark] {short}: {i} of {n_total} vehicles — "
                        f"{veh.get('year_make_model') or '?'} "
                        f"{veh.get('stock_number') or '?'}"
                    )
                print(
                    f"[benchmark] {short} complete — {n_total} vehicles, CTR recorded"
                )
    except ScraperError as exc:
        errors.append({"stock": "-", "phase": "benchmark_login", "error": str(exc)})
        print(f"[benchmark] ACV MAX login failed — {exc}", file=sys.stderr)

    # --- 7. AD POSTING VERIFICATION ------------------------------ #
    print("\n=== 7. AD POSTING VERIFICATION ===")
    verif_current: list[dict[str, Any]] = []
    needs_posting: list[dict[str, Any]] = []
    needs_update: list[dict[str, Any]] = []
    try:
        verif_current, needs_posting, needs_update = run_verification(ad_history)
        ad_history = load_ad_history()  # run_verification persisted verdicts
    except Exception as exc:  # noqa: BLE001 - verification must not sink the run
        errors.append({"stock": "-", "phase": "verification", "error": str(exc)})
        print(f"[verify] verification pass failed — {exc}", file=sys.stderr)
    verification_checks_run = (
        len(verif_current) + len(needs_posting) + len(needs_update)
    )
    print(
        f"[verify] checks run {verification_checks_run} | "
        f"not posted {len(needs_posting)} | outdated {len(needs_update)}"
    )

    # --- 8. EMAIL REPORTS ---------------------------------------- #
    print("\n=== 8. EMAIL REPORTS ===")
    if ads_generated:
        _safe_send(
            f"Mercedes-Benz of Durham — Ads Ready {today}",
            _format_ads_ready_email(ads_generated),
        )
    else:
        print("[email] no ads generated — Ads Ready email not sent")

    # Everything still carrying a pre-recon ad (recon_pending), oldest first.
    ymm_by_stock = {
        r.get("stock_number"): r.get("year_make_model") for r in retail
    }
    pre_recon_watching: list[dict[str, Any]] = []
    for stock, entry in ad_history.items():
        if not entry.get("recon_pending"):
            continue
        first = entry.get("first_ad_date")
        days = None
        if first:
            try:
                days = (date.today() - date.fromisoformat(first)).days
            except ValueError:
                days = None
        pre_recon_watching.append(
            {
                "stock_number": stock,
                "year_make_model": ymm_by_stock.get(stock),
                "days_since_initial_ad": days,
            }
        )
    pre_recon_watching.sort(
        key=lambda w: (w.get("days_since_initial_ad") is None, -(w.get("days_since_initial_ad") or 0))
    )

    rewritten = {a["stock"] for a in ads_generated}
    if waiting_recon or needs_cert or price_changes or errors or pre_recon_watching:
        _safe_send(
            f"Mercedes-Benz of Durham — Action Required {today}",
            _format_action_email(
                waiting_recon,
                needs_cert,
                price_changes,
                rewritten,
                errors,
                pre_recon_watching,
            ),
        )
    else:
        print("[email] nothing to action — Action Required email not sent")

    # third email: the ad-posting alert (only if something needs posting/updating)
    if needs_posting or needs_update:
        if send_email:
            send_verification_alert(needs_posting, needs_update, send=True)
            print(
                f"[email] sent: Ad Posting Alert "
                f"({len(needs_posting)} not posted, {len(needs_update)} outdated)"
            )
        else:
            send_verification_alert(needs_posting, needs_update, send=False)
            print("[email] (--no-email) would send: Ad Posting Alert")
    else:
        print("[email] nothing unposted/outdated — Ad Posting Alert not sent")

    # --- 9. COMPLETION LOG ------------------------------------- #
    runtime = time.monotonic() - started
    print()
    print("=" * 60)
    print("DAILY ORCHESTRATOR SUMMARY")
    print("=" * 60)
    by_stage: dict[str, int] = {}
    for a in ads_generated:
        by_stage[a.get("lifecycle_stage", "active")] = (
            by_stage.get(a.get("lifecycle_stage", "active"), 0) + 1
        )
    print(f"  Total retail vehicles crawled: {len(retail)}")
    print(f"  Ads generated today:           {len(ads_generated)}")
    print(f"    new (full pipeline):         {by_stage.get('active', 0)}")
    print(f"    pre-recon:                   {by_stage.get('pre_recon', 0)}")
    print(f"    recon updates:               {by_stage.get('recon_updated', 0)}")
    print(f"    repriced:                    {by_stage.get('repriced', 0)}")
    print(f"  Price changes detected:        {len(price_changes)}")
    print(f"  Pre-recon ads still watching:  {len(pre_recon_watching)}")
    print(f"  Vehicles waiting on recon:     {len(waiting_recon)}")
    print(f"  Needs certification assigned:  {len(needs_cert)}")
    print(f"  Unmapped status (skipped):     {len(skipped_status)}")
    print(f"  CTR records written:           {ctr_records}")
    print(
        f"  Benchmark CTR — Northlake:     "
        f"{benchmark_counts.get('Mercedes-Benz of Northlake', 0)} vehicles"
    )
    print(
        f"  Benchmark CTR — Charlotte:     "
        f"{benchmark_counts.get('Hendrick Motors of Charlotte', 0)} vehicles"
    )
    print(
        f"  Verification checks run: {verification_checks_run} | "
        f"Not posted: {len(needs_posting)} | Outdated: {len(needs_update)}"
    )
    print(f"  Errors:                        {len(errors)}")
    print(f"  Total runtime:                 {_fmt_runtime(runtime)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily ad-pipeline orchestrator")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="process only the first N retail vehicles (testing)",
    )
    parser.add_argument(
        "--no-email", action="store_true", help="build reports but do not send email"
    )
    parser.add_argument(
        "--status", nargs="+", type=int,
        help="only process vehicles with these status codes",
    )
    args = parser.parse_args(argv)
    return run(limit=args.limit, send_email=not args.no_email, status=args.status)


if __name__ == "__main__":
    raise SystemExit(main())
