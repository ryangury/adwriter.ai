#!/usr/bin/env python3
"""vision_parser.py — vision-based document reading for Carfax reports,
window stickers, and ReconVision work orders, using Claude's vision input
instead of regex over scraped text/HTML.

    from vision_parser import parse_carfax_image, parse_sticker_image, parse_recon_image
    cf = parse_carfax_image(image_bytes, vin)      # dict or None
    st = parse_sticker_image(image_bytes, vin)     # dict or None
    rc = parse_recon_image(image_bytes, vin)       # dict or None

Both return None on any failure (API error, malformed response, no image) —
callers are expected to fall back to the existing text/regex parsers in that
case; nothing here raises for an ordinary parse failure.

The Anthropic client/API key come from adwriter.py via a *local* import inside
call_claude_vision() rather than a module-level one. adwriter.py imports from
aggregator.py, and aggregator.py imports from this module — a module-level
`from adwriter import API_KEY` here would make that a real import cycle.
Deferring it to call time (same pattern adwriter.py itself uses for its own
aggregator import — see its update_recon() docstring) means both modules are
already fully loaded by the time this ever actually runs.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import anthropic

VISION_MODEL = "claude-haiku-4-5-20251001"
VISION_MAX_TOKENS = 4096  # raised from 1000 after a ReconVision work order
                          # with many line items got cut off mid-JSON,
                          # producing an unclosed ```json fence that failed
                          # to parse and silently fell back to the DOM/regex
                          # parser. This is a ceiling, not a reservation —
                          # short responses (most Carfax/sticker parses)
                          # cost the same as before.

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _media_type(image_bytes: bytes) -> str:
    """Sniff the image's magic bytes rather than trust a filename/caller
    claim — cheap and avoids sending a mislabeled media_type to the API."""
    if image_bytes.startswith(_PNG_MAGIC):
        return "image/png"
    if image_bytes.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    return "image/png"  # both scraper capture paths write PNG; safe default


# Claude's vision input hard-rejects any image with either dimension over
# 8000px. A full_page=True Carfax screenshot (see
# scraper.ACVMaxScraper._capture_carfax_screenshot()) is exactly the kind of
# image that can cross this on a long report — observed in practice
# (messages.0.content.0.image.source.base64.data: "At least one of the image
# dimensions exceed max allowed size: 8000 pixels"). Resize down before
# sending rather than let the whole call fail over to the text parser.
_MAX_IMAGE_DIMENSION = 7500


def _resize_if_needed(image_bytes: bytes) -> bytes:
    """Downscale `image_bytes` (preserving aspect ratio) if either dimension
    exceeds _MAX_IMAGE_DIMENSION. Returns the original bytes unchanged (and
    on any error opening the image, since a resize failure shouldn't block
    an otherwise-sendable image) if no resize is needed or possible."""
    try:
        from PIL import Image
    except ImportError:
        return image_bytes
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            width, height = img.size
            longest = max(width, height)
            if longest <= _MAX_IMAGE_DIMENSION:
                return image_bytes
            scale = _MAX_IMAGE_DIMENSION / longest
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            resized = img.convert("RGB").resize(new_size, Image.LANCZOS)
            out = BytesIO()
            resized.save(out, format="PNG")
            print(
                f"[vision] resized image {width}x{height} -> "
                f"{new_size[0]}x{new_size[1]} (over the {_MAX_IMAGE_DIMENSION}px "
                f"API limit)",
                file=sys.stderr,
            )
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - fall through with the original bytes
        print(f"[vision] image resize failed, sending original: {exc}", file=sys.stderr)
        return image_bytes


def _extract_json(text: str) -> dict[str, Any] | None:
    """Parse the model's reply as JSON. The prompts ask for a bare JSON
    object, but a fenced ```json ... ``` block is tolerated defensively."""
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def call_claude_vision(image_bytes: bytes, prompt: str) -> dict[str, Any] | None:
    """Send one image + prompt to Claude vision and return the parsed JSON
    dict it replies with, or None on any failure (API error, non-JSON reply,
    empty image). Never raises — this is meant to sit in a fallback chain."""
    if not image_bytes:
        print("[vision] call_claude_vision: no image bytes given", file=sys.stderr)
        return None

    from adwriter import API_KEY  # local import — see module docstring

    image_bytes = _resize_if_needed(image_bytes)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    client = anthropic.Anthropic(api_key=API_KEY)
    try:
        resp = client.messages.create(
            model=VISION_MODEL,
            max_tokens=VISION_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": _media_type(image_bytes),
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        print(f"[vision] Claude vision call failed: {exc}", file=sys.stderr)
        return None

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _extract_json(text)
    if parsed is None:
        print(
            f"[vision] response was not valid JSON (first 200 chars): "
            f"{text[:200]!r}",
            file=sys.stderr,
        )
    return parsed


def _load_image_bytes(image_bytes_or_path: bytes | str | Path) -> bytes | None:
    """Accept either raw bytes or a path to an image file on disk."""
    if isinstance(image_bytes_or_path, (bytes, bytearray)):
        return bytes(image_bytes_or_path)
    path = Path(image_bytes_or_path)
    try:
        return path.read_bytes()
    except OSError as exc:
        print(f"[vision] could not read image file {path}: {exc}", file=sys.stderr)
        return None


CARFAX_VISION_PROMPT = """\
This is a CARFAX vehicle history report. Extract and return ONLY a JSON object with these exact fields:

For every field in this schema: if the report does not explicitly state a value, return null or an empty value for that field. Never infer, estimate, or fill in a plausible-sounding answer based on general knowledge, similar vehicles, or partial evidence. A wrong guess is worse than an honest "not found" — a null value can be corrected, a confident wrong answer usually isn't caught. This applies especially to any field describing dates, locations, counts, or specific technical content, where a plausible-sounding wrong answer could produce a real business or legal problem downstream.

{
  'owners': integer,  # Read this number directly from the report's own
                       # summary — either the "CARFAX 1-Owner Vehicle" badge
                       # (means 1) or the "N Previous Owners" summary stat
                       # near the top of the report (means N). Carfax has
                       # already analyzed the full history and computed this
                       # number; do not recompute or second-guess it by
                       # counting dealer names, service visits, or
                       # transaction rows in the Detailed History timeline.
                       # If the Detailed History section has explicit
                       # "Owner 1", "Owner 2", etc. headers, they will
                       # always match the summary count — use them only to
                       # confirm, never to override.
                       # A title number changing partway through the history
                       # (e.g. "Title #775971210710014" early on, a different
                       # "Title #772175223219947" later) does NOT by itself
                       # mean a new owner — title numbers can change for
                       # reasons unrelated to ownership (lien payoff, lost
                       # title reissue, registration renewal). Only trust the
                       # "CARFAX 1-Owner Vehicle" badge, the "N Previous
                       # Owners" summary stat, or the count of distinct
                       # "Owner 1" / "Owner 2" section headers in Ownership
                       # History — never infer an owner change from a title
                       # number changing alone.
  'owners_evidence': string,  # The exact text you read to determine the owner
                       # count — e.g. "CARFAX 1-Owner Vehicle badge" or
                       # "3 Previous Owners" or "Owner 1, Owner 2, Owner 3
                       # section headers". This must be a direct quote or
                       # precise description of what you saw, not a
                       # restatement of your conclusion.
  'owner_type': 'personal' or 'lease' or 'corporate' or 'unknown',
  'accident_count': integer (0 if none reported),
  'accident_severity': 'none' or 'minor' or 'moderate' or 'severe',
  'title_brands': array of strings (empty array if none),
  'airbag_deployed': boolean,
  'structural_damage': boolean,
  'all_service_authorized_dealer': boolean,  # True only if every service record
                       # in the history was performed at a dealer of THIS
                       # vehicle's own brand (read the make from the report's own
                       # vehicle description at the top — e.g. a Honda's service
                       # history must show authorized Honda dealers, not any
                       # dealer in general, and never assume Mercedes-Benz).
                       # False if any service was at an independent shop, a
                       # different brand's dealer, or if service history is
                       # absent/unclear.
  'geographic_states': array of state abbreviations where titled,
  'annual_mileage': integer or null,
  'service_record_count': integer or null,
  'low_mileage': boolean
}
For 'structural_damage': read the report's actual Structural Damage verdict line ("No Issues Reported" -> false, "Issues Reported" -> true). The report also prints "Structural Damage" once more, elsewhere, as a plain legend/glossary heading with no verdict attached (usually in a pipe-separated list of damage-brand terms) — that occurrence names no finding and must not be read as true. Only the line stating an actual verdict for this vehicle counts.
For 'geographic_states': read this only from the report's own explicit statements — the "Owned in the following states/provinces" table row, or a "Last Owned in X" summary line. Do not infer a state from indirect signals (a dealer's location in the service history, a phone area code, an emissions certification type, or any other secondary evidence). If the report does not explicitly name a titled state, return an empty list — do not guess, and do not default to a plausible-sounding state. A specific wrong state is a specific factual claim about this vehicle's history that can end up in buyer-facing copy.
Return only valid JSON. No explanation, no markdown, no code blocks."""

STICKER_VISION_PROMPT = """\
This is a vehicle window sticker. Extract and return ONLY a JSON object with these exact fields:
{
  'exterior_color': string,
  'interior_color': string,
  'base_price': number or null,
  'total_msrp': number or null,
  'packages': array of {'name': string, 'price': number, 'contents': array of strings},
  'standard_features': array of {'category': string, 'features': array of strings}
}
For packages: only include items that have a listed price. Section headers like ADVANCED SAFETY TECHNOLOGY that group standard features are NOT packages.
Exclude purely administrative items: owners manuals, license plate holders, VIN labels, dispatch codes, omission items, and regulatory compliance codes.
Return only valid JSON. No explanation, no markdown, no code blocks."""

RECON_VISION_PROMPT = """\
This is a vehicle reconditioning work order from ReconVision. Extract and return ONLY a JSON object with:
{
  'line_items': [
    {
      'description': string,
      'department': string (Mechanical Repairs, Recon, Detail, etc.),
      'labor_hours': number or null,
      'labor': number or null,
      'parts': number or null,
      'total': number or null,
      'completed': boolean (true if green thumbs up and TASK COMPLETED, false if red thumbs down or INCOMPLETE)
    }
  ]
}
Exclude workflow/administrative tasks: Pre-Wash, Check In, Parts Estimate, Review Estimate, Parts Fulfillment, Recon Service (generic), Vehicle Ready, Final Quality Control.
Include only actual service work: named repairs, services, parts installations.
Return only valid JSON."""


def parse_carfax_image(
    image_bytes_or_path: bytes | str | Path, vin: str | None
) -> dict[str, Any] | None:
    """Vision-parse a Carfax report screenshot into structured JSON. Returns
    None (never raises) if the image can't be read, the API call fails, or
    the reply isn't valid JSON — callers fall back to the text/regex parser."""
    image_bytes = _load_image_bytes(image_bytes_or_path)
    if image_bytes is None:
        return None
    parsed = call_claude_vision(image_bytes, CARFAX_VISION_PROMPT)
    if parsed is None:
        print(
            f"[vision] carfax vision parse failed for VIN {vin or 'unknown'} "
            f"— falling back to text parser",
            file=sys.stderr,
        )
        return None
    print(f"[vision] parsed carfax for VIN {vin or 'unknown'}")
    return parsed


def parse_sticker_image(
    image_bytes_or_path: bytes | str | Path, vin: str | None
) -> dict[str, Any] | None:
    """Vision-parse a window sticker image into structured JSON. Returns None
    (never raises) if the image can't be read, the API call fails, or the
    reply isn't valid JSON — callers fall back to the text/regex parser."""
    image_bytes = _load_image_bytes(image_bytes_or_path)
    if image_bytes is None:
        return None
    parsed = call_claude_vision(image_bytes, STICKER_VISION_PROMPT)
    if parsed is None:
        print(
            f"[vision] sticker vision parse failed for VIN {vin or 'unknown'} "
            f"— falling back to text parser",
            file=sys.stderr,
        )
        return None
    print(f"[vision] parsed window sticker for VIN {vin or 'unknown'}")
    return parsed


def parse_recon_image(
    image_bytes_or_path: bytes | str | Path, vin: str | None
) -> dict[str, Any] | None:
    """Vision-parse a ReconVision work-order screenshot into structured JSON
    ({'line_items': [...]}). Returns None (never raises) if the image can't
    be read, the API call fails, or the reply isn't valid JSON — callers fall
    back to the existing DOM-walker/regex parser."""
    image_bytes = _load_image_bytes(image_bytes_or_path)
    if image_bytes is None:
        return None
    parsed = call_claude_vision(image_bytes, RECON_VISION_PROMPT)
    if parsed is None:
        print(
            f"[vision] recon vision parse failed for VIN {vin or 'unknown'} "
            f"— falling back to DOM/regex parser",
            file=sys.stderr,
        )
        return None
    print(f"[vision] parsed recon work order for VIN {vin or 'unknown'}")
    return parsed
