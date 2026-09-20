"""Re-run Carfax vision parsing over every cached report image — no scraping, no
login, just the local carfax_cache/*.png files and the current
vision_parser.CARFAX_VISION_PROMPT.

Uses aggregator._apply_carfax_vision(), the same overlay aggregate() applies,
rather than saving parse_carfax_image()'s raw JSON: the vision schema ("owners",
"owner_type": "personal"/"lease", "annual_mileage", ...) is not the cached
carfax_json shape ("number_of_owners", "owner_type" mapped, "miles_per_year",
plus raw_text, service_facilities, warranty_*, window_sticker_url, ...), and
saving it directly would replace a full cache row with mismatched keys.

Only carfax_json is rewritten. carfax_date and last_updated are left alone: a
re-parse of an old screenshot is not a fresh Carfax pull, so it must not
restart the 14-day TTL the way save_carfax() (which stamps today) would.

Usage:
    C:\\adwriter\\adwriter-env\\Scripts\\python.exe revision_carfax.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import aggregator
from aggregator import _apply_carfax_vision
from vehicle_cache import _connect, _get_row
from vision_parser import parse_carfax_image

CARFAX_CACHE_DIR = Path(__file__).with_name("carfax_cache")

# Fields worth reporting a before/after on besides the owner count.
_COMPARE = ("owner_type", "accident_count", "no_accidents", "title_brands", "service_record_count")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    images = sorted(CARFAX_CACHE_DIR.glob("*.png"))
    print(f"Found {len(images)} cached Carfax images to re-parse" + (" (dry run)" if args.dry_run else ""))

    updated = failed = skipped = 0
    owner_changes: list[tuple[str, object, object, str | None]] = []
    other_changes: dict[str, int] = {k: 0 for k in _COMPARE}
    real_parse = aggregator.parse_carfax_image

    for path in images:
        vin = path.stem
        row = _get_row(vin)
        if not row or not row["carfax_json"]:
            print(f"[revision] {vin}: no cached carfax_json to update, skipping")
            skipped += 1
            continue
        existing = json.loads(row["carfax_json"])
        if not existing.get("carfax_image_path"):
            existing["carfax_image_path"] = str(path)

        # One vision call per image: parse here, then hand that result to
        # _apply_carfax_vision() instead of letting it parse the image again.
        parsed = parse_carfax_image(path, vin)
        if parsed is None:
            print(f"[revision] {vin}: re-parse failed, leaving existing cache untouched")
            failed += 1
            continue
        aggregator.parse_carfax_image = lambda _img, _vin, _p=parsed: _p
        try:
            merged = _apply_carfax_vision(dict(existing), vin)
        finally:
            aggregator.parse_carfax_image = real_parse

        old_owners, new_owners = existing.get("number_of_owners"), merged.get("number_of_owners")
        if old_owners != new_owners:
            # owners_evidence is vision's own claim and has been shown to be
            # fabricated, so it is deliberately not printed here as a reason.
            print(f"[revision] {vin}: owner count changed {old_owners} -> {new_owners}")
            owner_changes.append((vin, old_owners, new_owners, None))
        for k in _COMPARE:
            if existing.get(k) != merged.get(k):
                other_changes[k] += 1

        if not args.dry_run:
            with _connect() as conn:
                conn.execute(
                    "UPDATE vehicle_data SET carfax_json = ? WHERE vin = ?",
                    (json.dumps(merged), vin),
                )
                conn.commit()
        updated += 1

    print(f"Done. {updated} {'parsed (not written)' if args.dry_run else 'updated'}, {failed} failed, {skipped} skipped.")
    print(f"Owner counts changed: {len(owner_changes)} of {updated}")
    for vin, old, new, _ in owner_changes:
        print(f"  {vin}: {old} -> {new}")
    print("Other fields that differ (rows):", {k: v for k, v in other_changes.items() if v} or "none")


if __name__ == "__main__":
    main()
