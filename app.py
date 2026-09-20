#!/usr/bin/env python3
"""app.py — a small Flask demo UI for the ad-writer pipeline.

Run it with the project virtualenv:

    C:\\adwriter\\adwriter-env\\Scripts\\python.exe app.py

Then open http://localhost:5000/ , type a stock number, and hit Submit. The
/generate endpoint runs the same two calls the orchestrator's build path uses —
aggregator.aggregate() then adwriter._generate_from_package() — and returns the
result (scraper status, vehicle summary, ad copy, feedback block) as JSON.

This is a demo surface only. It is single-threaded and blocking: one ad build at
a time, and the client shows step messages while it waits rather than a true
live stream (a single JSON response cannot stream).
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# Pin the working directory and import path so the pipeline behaves identically
# whether app.py is launched directly or as a background task. (Session files and
# the SQLite DBs are already __file__-anchored, but relative paths / debug dumps
# and any cwd-sensitive scraper step are not.)
os.chdir("C:/adwriter")
sys.path.insert(0, "C:/adwriter")

import anthropic
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

# Imported exactly as orchestrator.py does.
from adwriter import (
    _generate_from_package,
    format_data_package,
    load_ad_history,
    record_ad,
    save_ad_history,
    source_status,
)
from aggregator import ScraperError, aggregate
from scraper import WorkOrderNotFoundError
from vehicle_cache import get_carfax, get_carfax_image_path, get_recon, get_vehicle_by_stock, get_window_sticker, needs_recon
from credentials import DEMO_PASSWORD
from run_lock import ScraperBusyError

# Simple shared-password gate for the demo — no user accounts. Change this to
# rotate the password; every existing session is invalidated the next time the
# process restarts (app.secret_key is regenerated on each launch).
PASSWORD = DEMO_PASSWORD

app = Flask(__name__)
app.secret_key = os.urandom(24)
# debug=False (below) turns off Jinja's default auto_reload too, so without
# this the dev server silently keeps serving whatever index.html/login.html
# content it first compiled — template edits never show up until the process
# is restarted, even though a plain file read (or curl) sees the new content.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# source_status() keys -> the names the frontend expects.
_SOURCE_KEY_MAP = {
    "acvmax_pricing": "acvmax",
    "autoipacket": "autoipacket",
    "reconvision": "reconvision",
    "carfax": "carfax",
}
_ALL_SOURCES = ("acvmax", "autoipacket", "reconvision", "carfax")

_PROOF_POINT_RE = re.compile(r"^PROOF_POINT_USED:\s*(.+)$", re.MULTILINE)

SNAPSHOT_PATH = Path(__file__).with_name("last_inventory_snapshot.json")


def _snapshot_lookup(stock_number: str) -> dict[str, Any] | None:
    """The last inventory crawl's raw vehicle dict for this stock number (vin,
    vehicle_id, year_make_model, ...), or None. Two uses:

    1. A VIN source when aggregate() fails before it resolves one itself (a
       WorkOrderNotFoundError happens in aggregate()'s ReconVision step, which
       runs before ACV Max pricing).
    2. A vehicle_id source so a retry never has to search ACV Max inventory
       again for a stock number the last crawl already resolved — see
       ACVMaxScraper.scrape_pricing()'s docstring on why a second independent
       search is a real race, not just wasted work.
    """
    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for v in data.get("vehicles") or []:
        if v.get("stock_number") == stock_number:
            return v
    return None


def _scraper_status(pkg: dict[str, Any]) -> dict[str, str]:
    """{acvmax|autoipacket|reconvision|carfax: 'success'|'failed'} from
    adwriter.source_status(). Anything we can't read is reported 'failed'."""
    status = {name: "failed" for name in _ALL_SOURCES}
    try:
        raw = source_status(pkg)
    except Exception:  # noqa: BLE001 - a malformed pkg shouldn't 500 the demo
        return status
    for src_key, (state, _detail) in raw.items():
        name = _SOURCE_KEY_MAP.get(src_key)
        if name:
            status[name] = "success" if state == "ok" else "failed"
    return status


# pkg["scraper_status"] keys -> the vehicle_cache.db field each one caches.
# There's no entry for acvmax: it's never cached (see aggregator.aggregate()).
_CACHE_KEY_MAP = {
    "autoipacket": "window_sticker",
    "carfax": "carfax",
    "reconvision": "recon",
}
_EMPTY_CACHE_STATUS = {
    "window_sticker": "not_available",
    "carfax": "not_available",
    "recon": "not_available",
}
_EMPTY_SCRAPER_STATUS_DETAIL = {
    name: {"status": "failed", "note": ""} for name in _ALL_SOURCES
}


def _cache_status(pkg: dict[str, Any]) -> dict[str, str]:
    """{window_sticker|carfax|recon: 'cached'|'already_cached'|'not_available'}.

    This reports what aggregate() itself already did — it does NOT re-cache
    anything. aggregate() writes to vehicle_cache.db as part of scraping
    (see aggregator.aggregate()), tagging each source "scraped" (freshly
    pulled and cached this run) or "cache_hit" (already in vehicle_cache.db)
    in pkg["scraper_status"]; "failed" or a missing key means nothing usable
    to cache.

    Deliberately NOT implemented as a second save_window_sticker() /
    save_carfax() / save_recon() call from pkg's shaped fields: pkg["recon"]
    is the FILTERED line-item list (Close RO and other workflow items are
    dropped by aggregator._filter_recon()), so caching it would make
    aggregator._recon_is_complete() read every cached vehicle as complete;
    pkg["msrp_data"] drops standard_options/added_options_all; pkg["carfax"]
    drops raw_text. aggregate()'s own cache writes use the full raw scrape
    data and are the correct ones to keep.
    """
    raw = pkg.get("scraper_status") or {}
    mapped = {"scraped": "cached", "cache_hit": "already_cached"}
    return {
        cache_key: mapped.get(raw.get(src_key), "not_available")
        for src_key, cache_key in _CACHE_KEY_MAP.items()
    }


def _vehicle_summary(pkg: dict[str, Any]) -> dict[str, Any]:
    """Basic buyer-facing vehicle fields: year / make / model / mileage / price.
    year/make/model are split best-effort out of the combined year_make_model
    string ACV MAX returns."""
    v = pkg.get("vehicle") or {}
    ymm = (v.get("year_make_model") or "").strip()
    year = make = model = None
    if ymm:
        parts = ymm.split()
        if parts and re.fullmatch(r"(19|20)\d{2}", parts[0]):
            year, parts = parts[0], parts[1:]
        if parts:
            make, parts = parts[0], parts[1:]
        model = " ".join(parts) or None
    return {
        "year": year,
        "make": make,
        "model": model,
        "year_make_model": ymm or None,
        "trim_body": v.get("trim_body"),
        "vin": v.get("vin"),
        "mileage": v.get("mileage"),
        "price": v.get("current_price"),
    }


def _proof_point(feedback: str | None, pkg: dict[str, Any]) -> str | None:
    """Which pricing proof point was used and the dollar gap. Prefer the model's
    own PROOF_POINT_USED line from the feedback block; fall back to the best
    favorable proof point the aggregator found."""
    if feedback:
        m = _PROOF_POINT_RE.search(feedback)
        if m:
            answer = m.group(1).strip().strip("[]").strip()
            if answer and answer.lower() not in {"none", "n/a"}:
                return answer
    best = ((pkg.get("pricing") or {}).get("best_proof_point")) or {}
    if best.get("summary"):
        return best["summary"]
    if best.get("label"):
        gap = best.get("gap")
        return f"{best['label']} (${gap:,.0f} below)" if gap else best["label"]
    return None


def _slugify(text: str | None) -> str | None:
    """'Four-wheel alignment' -> 'four_wheel_alignment'; None/blank -> None.
    Used to give excluded recon items a short machine-readable tag alongside
    their human-readable description — there's no such tag in the source
    data (see _filter_recon()'s excluded_line_items), only for kept items."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return slug or None


def _carfax_dashboard(pkg: dict[str, Any]) -> dict[str, Any]:
    """Raw cached Carfax fields plus a link to the cached report image, for
    a side-by-side 'what Carfax says vs. what the ad says' review — replaces
    the old boolean Carfax checkmark in the Scrapers box, which only showed
    whether the scrape succeeded, not what it actually found."""
    cf = pkg.get("carfax") or {}
    vin = (pkg.get("vehicle") or {}).get("vin")
    image_path = get_carfax_image_path(vin) if vin else None
    return {
        "number_of_owners": cf.get("number_of_owners"),
        "owner_type": cf.get("owner_type"),
        "no_accidents": cf.get("no_accidents"),
        "accident_details": cf.get("accident_details"),
        "title_brands": cf.get("title_brands"),
        "carfax_date": cf.get("carfax_date"),
        "image_available": bool(image_path and os.path.isfile(image_path)),
        "vin": vin,
        "image_path": image_path,
    }


def _recon_dashboard(pkg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(included, excluded) quick-glance lists from pkg["recon"] (aggregator's
    recon_block) — the same kept/excluded line items build_recon_sentence()
    reads, reshaped for the dashboard card."""
    recon = pkg.get("recon") or {}
    included = [
        {"reason": li.get("recon_reason"), "description": li.get("description")}
        for li in recon.get("line_items") or []
    ]
    excluded = [
        {
            "reason": _slugify(li.get("section") or li.get("description")),
            "description": li.get("description"),
            "rule": li.get("reason"),
        }
        for li in recon.get("excluded_line_items") or []
    ]
    return included, excluded


# aggregator._filter_recon()'s categorized recon_reason for the excluded
# ACV Max Retail benchmark's key (scraper._proof_point_key()) — never a
# buyer-facing anchor, see aggregator._select_proof_points()'s docstring.
_ACV_MAX_RETAIL_KEY = "acv_max_retail"


def _proof_points_all(pkg: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pricing proof point ACV Max showed for this vehicle, tagged with
    whether aggregator._select_proof_points() picked it as the ad's primary
    or secondary anchor (or, if not, why not) — for the dashboard's quick
    glance at the full pricing picture, not just the one or two anchors the
    ad copy actually uses."""
    pricing = pkg.get("pricing") or {}
    all_points = pricing.get("all_proof_points") or []
    primary = pricing.get("primary_proof_point") or {}
    secondary = pricing.get("secondary_proof_point") or {}

    def _is_anchor(p: dict[str, Any], anchor: dict[str, Any]) -> bool:
        return (
            bool(anchor)
            and p.get("direction") == "below"
            and p.get("direction") == anchor.get("direction")
            and p.get("gap") == anchor.get("gap")
        )

    out = []
    secondary_claimed = False
    for p in all_points:
        entry = {
            "label": p.get("label"),
            "gap": p.get("gap"),
            "direction": p.get("direction"),
        }
        if _is_anchor(p, primary):
            entry["selected"] = "primary"
        elif not secondary_claimed and _is_anchor(p, secondary):
            entry["selected"] = "secondary"
            secondary_claimed = True
        else:
            entry["selected"] = False
            if p.get("key") == _ACV_MAX_RETAIL_KEY:
                entry["skip_reason"] = "never buyer-facing"
            elif p.get("direction") != "below":
                entry["skip_reason"] = "not favorable"
            else:
                entry["skip_reason"] = "not selected as primary/secondary anchor"
        out.append(entry)
    return out


def _scraper_status_detail(pkg: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per-source status plus a short note — the note surfaces AutoiPacket's
    finer-grained cache tier (msrp_data["source"], e.g. "rarity_db_cache")
    when there is one; the other sources have no equivalent sub-tier today."""
    raw = pkg.get("scraper_status") or {}
    msrp_source = (pkg.get("msrp_data") or {}).get("source") or ""
    return {
        "acvmax": {"status": raw.get("acvmax", "failed"), "note": ""},
        "autoipacket": {"status": raw.get("autoipacket", "failed"), "note": msrp_source},
        "reconvision": {"status": raw.get("reconvision", "failed"), "note": ""},
        "carfax": {"status": raw.get("carfax", "failed"), "note": ""},
    }


def _shipping_reason(pkg: dict[str, Any]) -> str | None:
    """Best-effort label for why build_shipping_sentence() fired — a
    dashboard hint, not a re-implementation of its per-tier trigger rules
    (AMG / MANUFAKTUR / price / scarcity, gated differently per status_code;
    see aggregator.build_shipping_sentence()'s docstring)."""
    vehicle = pkg.get("vehicle") or {}
    if "AMG" in str(vehicle.get("year_make_model") or "").upper():
        return "AMG model"
    packages = (pkg.get("msrp_data") or {}).get("option_packages") or []
    if any("MANUFAKTUR" in str(p.get("name") or "").upper() for p in packages):
        return "MANUFAKTUR"
    price = vehicle.get("advertised_price")
    if price is not None and price > 80000:
        return "price above $80k"
    matching_count = (pkg.get("market_velocity") or {}).get("matching_count")
    if matching_count is not None and matching_count < 15:
        return "limited market availability"
    return "market factors"


def _market_scope_label(search_distance: Any) -> str | None:
    """Bare region name for the dashboard (e.g. "Southeast") matching the
    same mile bands aggregator._get_market_scope() uses for the ad-copy
    sentences, just without the "across the"/"nationwide" sentence framing.
    None when search_distance is missing or unparseable — never guess."""
    if search_distance is None:
        return None
    try:
        miles = int(search_distance)
    except (TypeError, ValueError):
        return None
    if miles <= 50:
        return "Triangle"
    elif miles <= 150:
        return "Central NC"
    elif miles <= 250:
        return "Carolinas"
    elif miles <= 500:
        return "Southeast"
    elif miles <= 1000:
        return "Eastern Seaboard"
    else:
        return "Nationwide"


def _market_data(pkg: dict[str, Any]) -> dict[str, Any]:
    """Market-velocity quick-glance fields from pkg["market_velocity"] (ACV
    Max's Market Days Supply / Competitive Set / Rank widget — see
    aggregator._market_velocity()), plus whether the nationwide-shipping
    sentence fired and a best-effort guess at why."""
    mv = pkg.get("market_velocity") or {}
    market_rank = mv.get("market_rank")
    market_rank_of = mv.get("market_rank_of")
    shipping_sentence = pkg.get("shipping_sentence")
    search_distance = mv.get("search_distance")
    return {
        "matching_count": mv.get("matching_count"),
        "market_days_supply": mv.get("matching_market_days"),
        "overall_days_supply": mv.get("overall_market_days"),
        # Not scraped anywhere today — never fabricated, see _market_velocity().
        "avg_mileage": None,
        "price_rank": (
            f"#{market_rank} of {market_rank_of}"
            if market_rank is not None and market_rank_of is not None
            else None
        ),
        "shipping_triggered": shipping_sentence is not None,
        "shipping_reason": _shipping_reason(pkg) if shipping_sentence is not None else None,
        "search_distance": search_distance,
        "market_scope": _market_scope_label(search_distance),
    }


@app.get("/")
def home():
    if not session.get("authed"):
        return render_template("login.html", error=request.args.get("error"))
    return render_template("index.html")


@app.post("/auth")
def auth():
    password = request.form.get("password") or ""
    if password == PASSWORD:
        session["authed"] = True
        return redirect(url_for("home"))
    return redirect(url_for("home", error="1"))


@app.get("/logout")
def logout():
    session.pop("authed", None)
    return redirect(url_for("home"))


@app.post("/generate")
def generate():
    if not session.get("authed"):
        return redirect(url_for("home"))
    stock_number = (request.form.get("stock_number") or "").strip().lstrip("#")
    empty = {name: "failed" for name in _ALL_SOURCES}
    if not stock_number:
        return (
            jsonify(
                {
                    "stock_number": None,
                    "scraper_status": empty,
                    "cache_status": dict(_EMPTY_CACHE_STATUS),
                    "vehicle": None,
                    "ad_copy": None,
                    "feedback": None,
                    "peacock_mode": None,
                    "proof_point": None,
                    "error": "No stock number provided.",
                }
            ),
            400,
        )

    result: dict[str, Any] = {
        "stock_number": stock_number,
        "scraper_status": dict(empty),
        "cache_status": dict(_EMPTY_CACHE_STATUS),
        "vehicle": None,
        "ad_copy": None,
        "feedback": None,
        "peacock_mode": None,
        "proof_point": None,
        "data_package": None,
        "recon_included": [],
        "recon_excluded": [],
        "carfax_detail": None,
        "proof_points_all": [],
        "scraper_status_detail": dict(_EMPTY_SCRAPER_STATUS_DETAIL),
        "market_data": None,
        "error": None,
    }

    # --- 1. aggregate (all scrapers) ---------------------------------- #
    # A prior crawl's cached vehicle_id (when we have one) is passed to every
    # aggregate() call below so ACV Max's inventory search only ever runs
    # once, if at all — see ACVMaxScraper.scrape_pricing()'s docstring for why
    # a second independent search for the same stock number is a real race.
    snap = _snapshot_lookup(stock_number)
    snap_vehicle_id = snap.get("vehicle_id") if snap else None
    print(
        f"[app] /generate: aggregate({stock_number!r}) starting  "
        f"(cwd={os.getcwd()}, vehicle_id={snap_vehicle_id!r})",
        file=sys.stderr,
        flush=True,
    )
    try:
        pkg = aggregate(
            stock_number, skip_recon=False, expected_vin=None, vehicle_id=snap_vehicle_id,
            bypass_rate_limits=True,
        )
    except WorkOrderNotFoundError as exc:
        print(
            f"[app] /generate: WorkOrderNotFoundError for {stock_number!r}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        vin = snap.get("vin") if snap else None
        if vin and not needs_recon(vin):
            # We already have complete recon for this VIN cached from an
            # earlier run — retry and let aggregate() itself use the cache
            # (see aggregator.aggregate()'s expected_vin/needs_recon path)
            # instead of hitting ReconVision again.
            print(
                f"[app] /generate: recon cache hit for VIN {vin} — retrying "
                f"{stock_number!r} with expected_vin",
                file=sys.stderr,
                flush=True,
            )
            try:
                pkg = aggregate(
                    stock_number, skip_recon=False, expected_vin=vin,
                    vehicle_id=snap_vehicle_id, bypass_rate_limits=True,
                )
            except ScraperError as exc2:
                traceback.print_exc()
                result["error"] = f"Scraper error: {exc2}"
                return jsonify(result), 200
            except Exception as exc2:  # noqa: BLE001
                traceback.print_exc()
                result["error"] = f"Aggregation failed: {exc2}"
                return jsonify(result), 502
        else:
            # No cached recon to fall back on. Still try to show whatever ACV
            # Max / AutoiPacket / Carfax data is available (skip_recon=True
            # bypasses ReconVision entirely) so the demo isn't a blank wall.
            try:
                partial_pkg = aggregate(
                    stock_number, skip_recon=True, expected_vin=vin,
                    vehicle_id=snap_vehicle_id, bypass_rate_limits=True,
                )
                result["scraper_status"] = _scraper_status(partial_pkg)
                result["scraper_status"]["reconvision"] = "failed"
                result["cache_status"] = _cache_status(partial_pkg)
                result["vehicle"] = _vehicle_summary(partial_pkg)
            except Exception:  # noqa: BLE001 - this is already a fallback path
                result["scraper_status"]["reconvision"] = "failed"
            result["error"] = (
                "ReconVision work order not found for this stock number. "
                "Recon data unavailable — ad cannot be generated without "
                "reconditioning data."
            )
            return jsonify(result), 200
    except ScraperBusyError:
        result["error"] = "An inventory sync is currently running. Try again in a few minutes."
        return jsonify(result), 503
    except ScraperError as exc:  # includes VehicleIdentityError
        print(
            f"[app] /generate: ScraperError for {stock_number!r} — full traceback:",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        result["error"] = f"Scraper error: {exc}"
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001
        print(
            f"[app] /generate: aggregate() failed for {stock_number!r} — "
            f"full traceback:",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        result["error"] = f"Aggregation failed: {exc}"
        return jsonify(result), 502

    # Recon still in progress — aggregate() returns an early stub, no ad.
    if pkg.get("recon_complete") is False:
        result["scraper_status"]["reconvision"] = "failed"
        result["error"] = pkg.get("note") or "Recon is not complete for this vehicle."
        return jsonify(result), 200

    result["scraper_status"] = _scraper_status(pkg)
    result["cache_status"] = _cache_status(pkg)
    result["vehicle"] = _vehicle_summary(pkg)
    result["peacock_mode"] = bool(pkg.get("peacock_mode"))
    result["recon_included"], result["recon_excluded"] = _recon_dashboard(pkg)
    result["carfax_detail"] = _carfax_dashboard(pkg)
    result["proof_points_all"] = _proof_points_all(pkg)
    result["scraper_status_detail"] = _scraper_status_detail(pkg)
    result["market_data"] = _market_data(pkg)
    try:
        result["data_package"] = format_data_package(pkg)[0]
    except Exception:  # noqa: BLE001 - the data-package view is a bonus, not core
        traceback.print_exc()

    # MB CPO data-completeness gate tripped inside aggregate() — no Claude call.
    if pkg.get("reason") == "incomplete_data":
        result["error"] = pkg.get("message") or "Required source data was incomplete."
        return jsonify(result), 200

    # --- 2. write the ad ----------------------------------------------- #
    try:
        ad_copy, feedback = _generate_from_package(pkg)
    except (anthropic.APIError, RuntimeError) as exc:
        traceback.print_exc()
        result["error"] = f"Ad generation failed: {exc}"
        return jsonify(result), 502
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        result["error"] = f"Ad generation failed: {exc}"
        return jsonify(result), 500

    result["ad_copy"] = ad_copy
    result["feedback"] = feedback
    result["proof_point"] = _proof_point(feedback, pkg)
    return jsonify(result), 200


def _parse_price(raw: Any) -> float | None:
    """'$47,995' / '47995' / 47995.0 -> 47995.0. None on anything unparseable."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^0-9.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


@app.post("/save")
def save_ad():
    """Persist an ad drafted in the demo UI into ad_history.json — the same
    store orchestrator.py's batch pipeline writes to via record_ad(). A
    /generate call never saves anything on its own (see its docstring); this
    is the explicit "keep this one" step for the human reviewing it here."""
    if not session.get("authed"):
        return jsonify({"saved": False, "error": "Not authenticated."}), 401

    data = request.get_json(silent=True) or {}
    stock_number = (data.get("stock_number") or "").strip().lstrip("#")
    ad_copy = data.get("ad_copy") or ""
    if not stock_number or not ad_copy:
        return (
            jsonify({"saved": False, "error": "stock_number and ad_copy are required."}),
            400,
        )

    price = _parse_price(data.get("price"))
    year_make_model = (data.get("year_make_model") or "").strip() or None
    lifecycle_stage = (data.get("lifecycle_stage") or "").strip() or "active"

    try:
        history = load_ad_history()
        # The Save button only ever renders after a /generate response that
        # reached ad generation, which means pkg["recon_complete"] was True
        # for that build (see /generate's early-return above) — recon is
        # always included by the time there's something here to save.
        entry = record_ad(
            history,
            stock_number,
            ad_text=ad_copy,
            price=price,
            lifecycle_stage=lifecycle_stage,
            recon_included=True,
            recon_pending=False,
        )
        # record_ad()'s schema has no year_make_model field (see adwriter.py's
        # ad_history.json schema comment) — /lookup currently sources it from
        # vehicle_cache.db instead. Add it here so a saved ad carries its own
        # copy even if that cache entry is ever missing or stale.
        if year_make_model:
            entry["year_make_model"] = year_make_model
        save_ad_history(history)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"saved": False, "error": str(exc)}), 500

    return jsonify({"saved": True, "stock_number": stock_number}), 200


def _ad_history_lookup(raw_stock: str | None) -> tuple[str, dict[str, Any] | None]:
    """Normalized stock number + its ad_history.json entry (or None if there's
    no entry, or it has never had `current_ad_text` set)."""
    stock_number = (raw_stock or "").strip().lstrip("#")
    if not stock_number:
        return stock_number, None
    entry = load_ad_history().get(stock_number)
    if not entry or not entry.get("current_ad_text"):
        return stock_number, None
    return stock_number, entry


def _last_written(entry: dict[str, Any]) -> str | None:
    # The most recent generation/update, not the first-ever write —
    # last_ad_date is bumped on every record_ad() call (fresh ad, reprice, or
    # recon update); first_ad_date never changes after the initial write.
    return entry.get("last_ad_date") or entry.get("first_ad_date")


@app.get("/carfax-image/<vin>")
def carfax_image(vin: str):
    """Serve the cached Carfax report screenshot for the review panel."""
    if not session.get("authed"):
        return redirect(url_for("home"))
    if not re.fullmatch(r"[A-Za-z0-9]{11,17}", vin):
        abort(404)
    path = get_carfax_image_path(vin)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@app.get("/check")
def check():
    """Lightweight existence check for the input-debounce badge — just whether
    a stock number has an ad on file and when it was last written. No
    vehicle_cache.db hit; use /lookup for the full stored-ad payload."""
    if not session.get("authed"):
        return redirect(url_for("home"))

    _stock, entry = _ad_history_lookup(request.args.get("stock"))
    if not entry:
        return jsonify({"found": False})
    return jsonify({"found": True, "last_written": _last_written(entry)})


@app.get("/lookup")
def lookup():
    """Look up a stock number in ad_history.json without touching any scraper —
    lets the demo show an already-written ad instantly. {"found": false} if the
    stock number has no ad on record (or has never had `current_ad_text` set).

    Option A: the data-summary fields (year/make/model, cache_status) come
    from vehicle_cache.db, keyed by a reverse stock_number -> vin lookup —
    ad_history.json itself doesn't store a VIN, though /save (see its
    docstring) does write year_make_model onto the entry when it has one.
    """
    if not session.get("authed"):
        return redirect(url_for("home"))

    stock_number, entry = _ad_history_lookup(request.args.get("stock"))
    if not entry:
        return jsonify({"found": False})

    cache_row = get_vehicle_by_stock(stock_number)
    vin = cache_row.get("vin") if cache_row else None
    year_make_model = entry.get("year_make_model") or (cache_row or {}).get("year_make_model")
    if not year_make_model:
        # Fall back to the last inventory crawl snapshot, same source
        # /generate's own VIN fallback uses, in case this stock number was
        # never scraped into vehicle_cache.db (e.g. an ad written before the
        # cache existed).
        snap = _snapshot_lookup(stock_number)
        year_make_model = (snap or {}).get("year_make_model")

    cache_status = {
        "window_sticker": "cached" if vin and get_window_sticker(vin) else "not_cached",
        "carfax": "cached" if vin and get_carfax(vin) else "not_cached",
        "recon": "cached" if vin and get_recon(vin) else "not_cached",
    }

    # proof_point_used isn't a stored field either; it only ever lived in the
    # ===FEEDBACK=== block from the generation that produced current_ad_text,
    # so pull it out with the same regex /generate uses on a live pkg.
    proof_point = ""
    m = _PROOF_POINT_RE.search(entry.get("last_feedback") or "")
    if m:
        answer = m.group(1).strip().strip("[]").strip()
        if answer and answer.lower() not in {"none", "n/a"}:
            proof_point = answer

    return jsonify(
        {
            "found": True,
            "stock_number": stock_number,
            "ad_copy": entry.get("current_ad_text"),
            "feedback": entry.get("last_feedback", ""),
            "price": entry.get("last_price_at_write"),
            "year_make_model": year_make_model,
            "last_written": _last_written(entry),
            "lifecycle_stage": entry.get("lifecycle_stage"),
            "proof_point": proof_point,
            "source": "ad_history",
            "cache_status": cache_status,
        }
    )


@app.get("/status")
def status():
    return jsonify({"status": "running", "time": datetime.now().isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
