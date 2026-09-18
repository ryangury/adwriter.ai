#!/usr/bin/env python3
"""vision_processor.py — nightly batch vision-parse of cached Carfax report
screenshots and window-sticker images that haven't been vision-parsed yet.

Under normal operation aggregator.py's live pipeline already vision-parses a
Carfax/sticker image inline, right after capturing it (see
aggregator._apply_carfax_vision() / _apply_sticker_vision()), so this script
mostly exists to retry the cases where that inline call failed (a transient
API error, a rate limit, ...) — the image is still sitting in carfax_cache/
or sticker_cache/ even though the cached vehicle_cache.db row still reflects
the text/regex parse. Also picks up any image dropped into either cache
directory outside the normal scrape flow.

"Already processed" is tracked in a simple JSON index (vision_processed.json)
keyed by VIN, independent of vehicle_cache.db state — an image is reprocessed
only if you delete its entry from that index (or the whole file).

CLI:
    python vision_processor.py [--limit N] [--headed]

--headed is accepted for command-line consistency with the browser scrapers
(scraper.py, aggregator.py) but is a no-op here: this script never launches a
browser, it only reads cached image files and calls the Claude API.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aggregator import _apply_carfax_vision
from scraper import CARFAX_CACHE_DIR, STICKER_CACHE_DIR
from vehicle_cache import (
    get_carfax,
    get_vehicle,
    get_window_sticker,
    save_carfax,
    save_window_sticker,
)
from vision_parser import parse_sticker_image

INDEX_PATH = Path(__file__).with_name("vision_processed.json")
LOG_PATH = Path(__file__).with_name("vision_process.log")
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


class _Tee:
    """Mirrors writes to every stream it wraps — used below to send this
    run's stdout/stderr to both the console and vision_process.log, since the
    AdWriter-VisionProcess scheduled task (see setup_tasks.ps1) runs
    unattended and its console output would otherwise go nowhere."""

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for s in self._streams:
            s.write(data)

    def flush(self) -> None:
        for s in self._streams:
            s.flush()


def _load_index() -> dict[str, dict[str, str]]:
    if not INDEX_PATH.exists():
        return {"carfax": {}, "sticker": {}}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("carfax", {})
    data.setdefault("sticker", {})
    return data


def _save_index(index: dict[str, dict[str, str]]) -> None:
    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _pending_images(
    cache_dir: Path, processed: dict[str, str], limit: int | None
) -> list[tuple[str, Path]]:
    """VIN/path pairs in `cache_dir` whose VIN isn't already a key in
    `processed`, oldest-filename-first, capped at `limit` (None = no cap)."""
    out: list[tuple[str, Path]] = []
    if not cache_dir.exists():
        return out
    for f in sorted(cache_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
            continue
        vin = f.stem.upper()
        if vin in processed:
            continue
        out.append((vin, f))
        if limit is not None and len(out) >= limit:
            break
    return out


def _process_carfax(vin: str, image_path: Path) -> bool:
    """Vision-parse one cached Carfax screenshot and overlay the result onto
    whatever's already cached for this VIN (same field-by-field merge the
    live pipeline uses — see aggregator._apply_carfax_vision()). Returns True
    if vision actually produced usable data."""
    carfax_raw = get_carfax(vin) or {"vin": vin}
    carfax_raw["carfax_image_path"] = str(image_path)
    merged = _apply_carfax_vision(carfax_raw, vin)
    if merged.get("carfax_parse_source") != "vision":
        return False
    row = get_vehicle(vin) or {}
    save_carfax(vin, row.get("stock_number"), merged, image_path=str(image_path))
    print(f"[vision] processed carfax for VIN {vin}")
    return True


def _process_sticker(vin: str, image_path: Path) -> bool:
    """Vision-parse one cached sticker image and merge the result into
    whatever's already cached for this VIN, in the same *raw* scraper-output
    shape save_window_sticker()/_msrp_data() expect (option_packages +
    added_options_all, priced-then-unpriced in order) — NOT the already
    pkg-shaped output aggregator._apply_sticker_vision() produces, which
    would corrupt sub_items if saved back as raw sticker_data (see that
    function's docstring on why _packages_with_sub_items() needs
    added_options_all to correctly re-derive sub_items on the next read).
    Returns True if vision actually produced usable data."""
    msrp_raw = get_window_sticker(vin) or {"vin": vin, "source": "vision_processor"}
    msrp_raw["sticker_image_path"] = str(image_path)

    vjson = parse_sticker_image(image_path, vin)
    if vjson is None:
        return False

    if vjson.get("exterior_color"):
        msrp_raw["exterior_color"] = vjson["exterior_color"]
    if vjson.get("interior_color"):
        msrp_raw["interior_color"] = vjson["interior_color"]
    if vjson.get("base_price") is not None:
        msrp_raw["base_price"] = vjson["base_price"]
    if vjson.get("total_msrp") is not None:
        msrp_raw["total_msrp"] = vjson["total_msrp"]
    if vjson.get("packages") is not None:
        added_options_all: list[dict[str, Any]] = []
        option_packages: list[dict[str, Any]] = []
        for p in vjson["packages"]:
            entry = {"code": None, "name": p.get("name"), "price": p.get("price")}
            option_packages.append(entry)
            added_options_all.append(entry)
            for content in p.get("contents") or []:
                added_options_all.append({"code": None, "name": content, "price": None})
        msrp_raw["option_packages"] = option_packages
        msrp_raw["added_options_all"] = added_options_all
    if vjson.get("standard_features") is not None:
        msrp_raw["standard_features"] = vjson["standard_features"]
    msrp_raw["sticker_parse_source"] = "vision"

    row = get_vehicle(vin) or {}
    save_window_sticker(
        vin,
        row.get("stock_number"),
        row.get("year_make_model") or msrp_raw.get("year_make_model"),
        msrp_raw,
        msrp_raw.get("source") or "vision_processor",
        image_path=str(image_path),
    )
    print(f"[vision] processed sticker for VIN {vin}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="max images to process per category (default: no limit)",
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="accepted for CLI consistency with the browser scrapers; unused "
             "here (this script never opens a browser)",
    )
    args = parser.parse_args(argv)

    index = _load_index()
    now = datetime.now(timezone.utc).isoformat()

    sticker_pending = _pending_images(STICKER_CACHE_DIR, index["sticker"], args.limit)
    carfax_pending = _pending_images(CARFAX_CACHE_DIR, index["carfax"], args.limit)
    print(
        f"[vision] {len(sticker_pending)} sticker image(s) and "
        f"{len(carfax_pending)} carfax image(s) pending"
    )

    stickers_done = 0
    carfax_done = 0
    failed = 0

    for vin, path in sticker_pending:
        try:
            ok = _process_sticker(vin, path)
        except Exception as exc:  # noqa: BLE001 - one bad image must not stop the batch
            print(f"[vision] sticker processing error for VIN {vin}: {exc}", file=sys.stderr)
            ok = False
        if ok:
            stickers_done += 1
            index["sticker"][vin] = now
        else:
            failed += 1

    for vin, path in carfax_pending:
        try:
            ok = _process_carfax(vin, path)
        except Exception as exc:  # noqa: BLE001 - one bad image must not stop the batch
            print(f"[vision] carfax processing error for VIN {vin}: {exc}", file=sys.stderr)
            ok = False
        if ok:
            carfax_done += 1
            index["carfax"][vin] = now
        else:
            failed += 1

    _save_index(index)

    print(
        f"[vision] summary: {stickers_done} stickers processed, "
        f"{carfax_done} carfax processed, {failed} failed"
    )
    return 0


if __name__ == "__main__":
    _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
    with open(LOG_PATH, "a", encoding="utf-8") as _log_file:
        _log_file.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
        sys.stdout = _Tee(_orig_stdout, _log_file)
        sys.stderr = _Tee(_orig_stderr, _log_file)
        try:
            _rc = main()
        finally:
            # Restore before the log file closes (end of this `with`) and
            # before interpreter shutdown's own final stdout flush — leaving
            # sys.stdout pointed at a _Tee over an already-closed file makes
            # that shutdown-time flush raise.
            sys.stdout, sys.stderr = _orig_stdout, _orig_stderr
    raise SystemExit(_rc)
