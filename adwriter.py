#!/usr/bin/env python3
"""adwriter.py — turn a stock number into finished ad copy via the Claude API.

Usage:
    python adwriter.py DT23358A            # full pipeline: scrape -> Claude -> email
    python adwriter.py DT23358A --no-email # generate and print only
    python adwriter.py                     # legacy: paste raw data on stdin

Full pipeline (given a stock number):
    aggregate(stock)  ->  recon-complete gate  ->  error check (-> FAILED email)
    ->  format data package  ->  Claude API  ->  ad copy  ->  "ready" email
"""

import argparse
import json
import re
import smtplib
import ssl
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import anthropic

import credentials
from credentials import ANTHROPIC_API_KEY
from email_config import EMAIL_ADS_READY
from aggregator import (
    _RECON_DONE_FALLBACK,
    _RECON_PENDING_FALLBACK,
    DEALER_DOC_FEE,
    _filter_recon,
    aggregate,
    build_recon_sentence,
    dedupe_equipment_descriptors,
)
from feature_cache import (
    get_feature,
    get_trim_knowledge,
    save_feature,
    save_trim_knowledge,
)
from carfax_history import carfax_history, claim_problems, scrub_claims
from towing import tow_figures as _tow_figures_in
from towing import towing_for, towing_sentence, unverified_tow_sentences
from vehicle_cache import get_vehicle, get_window_sticker
from powertrain import (
    MILD_HYBRID_SENTENCE,
    mild_wording_ok,
    CLASS_LABELS,
    UNKNOWN,
    cached_texts,
    check_claims,
    classify,
    split_ymm,
    data_package_lines,
    engine_problems,
    flags_block,
    strip_violations,
    vehicle_powertrain,
)
from reprice_prompt import reprice_prompt_for
from scraper import ReconVisionScraper, ScraperError
from shared_prompt_constants import (
    API_FEEDBACK_BLOCK,
    PACKAGE_CONTENT_VERIFICATION_RULE,
    POWERTRAIN_RULE,
    PREDICTIVE_STICKER_RULE,
    PROVENANCE_RULE,
    RECON_FALLBACK_RULE,
    SCARCITY_RULE,
    SELLER_COMMENTS_RULE,
    STICKER_PRICES_APPROXIMATE_RULE,
    COLOR_SOURCE_RULE,
    STORE_CLOSER_PARAGRAPH,
)
from system_prompt_as_is import AS_IS_PROMPT
from system_prompt_hendrick_affordable import HENDRICK_AFFORDABLE_PROMPT
from system_prompt_hendrick_certified import HENDRICK_CERTIFIED_PROMPT

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Sourced from credentials.py (gitignored, never committed) — never hardcode
# a real key here again.
API_KEY = ANTHROPIC_API_KEY

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2500

# ACV MAX inventory status codes that mean "certified and postable". Anything
# else routes the vehicle to a review section of the daily report:
#   1 / None -> certification not yet assigned in the system
#   not in {1, 10, 11, 12, 13, 16} -> unknown code, needs mapping
from status_codes import BUILD_STATUS_CODES as POSTABLE_STATUS_CODES  # noqa: E402
from status_codes import NON_CPO_STATUS_CODES  # noqa: E402

# --------------------------------------------------------------------------- #
# Ad framework — this is the system prompt. Edit freely to match your house style.
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = f"""\
You are an expert automotive copywriter working exclusively for Mercedes-Benz of Durham, the #1 Certified Pre-Owned Mercedes-Benz dealer in the Triangle region of North Carolina. You are part of the Hendrick Automotive Group.

Your sole job is to receive raw vehicle data and write finished, publish-ready ad copy for Certified Pre-Owned (CPO) Mercedes-Benz inventory. You write every ad from scratch using only the data provided. You never invent facts, never fabricate features, and never make claims that cannot be supported by the data given to you.

Every response you produce is a single block of finished copy, ready to paste directly into Homenet. No commentary, no explanations, no options, no alternatives. Just the finished ad.

---

OUTPUT FORMAT — MANDATORY

CRITICAL OUTPUT FORMAT
Wrap your entire ad in <ad> and </ad> tags:

<ad>
[paragraph one]

[paragraph two]

[paragraph three]

[paragraph four]
</ad>

Then write your FEEDBACK block after the closing </ad> tag.

Everything inside <ad></ad> is buyer-facing copy only.
Everything outside the tags is internal — reasoning, flags, decisions — and will be stripped.
Never put reasoning inside the tags.
Never put ad copy outside the tags.

Begin your response with <ad> immediately. Do not write any planning, decisions, or notes before the opening <ad> tag. Any text written before <ad> wastes token budget and will be discarded.

Four paragraphs of clean prose. No headers. No dividers. No bullet points. No numbered lists. No bold text. ABSOLUTE RULE: No em dashes anywhere in this ad, in any paragraph. No exceptions, no exemptions. Use a period, comma, or colon instead. The fixed warranty and store closer paragraphs have already been rewritten without em dashes and must be reproduced exactly as given below. Short declarative sentences. Natural human prose, not marketing language. One blank line between paragraphs. Nothing before the first paragraph. Nothing after the last paragraph.

ABSOLUTE RULE: Never append drivetrain designations such as 4MATIC, AWD, RWD, or xDrive to a model name unless that designation appears explicitly in the data provided. Never infer drivetrain from model name alone.

SENTENCE SPACING: Always include exactly one space after every period before the next sentence. Never allow a period immediately followed by a capital letter with no space. NLP tokenizers read run-on sentences as malformed tokens.

---

PRICING: Always use the advertised_price from the data package in all ad copy. This is the ACV Max price plus the $899 dealer administrative fee, which is the legally required online price. Never use the ACV Max price alone in buyer-facing copy. The proof point gaps in the data package are calculated against the pre-fee ACV Max price, since that is what ACV Max's own benchmarks are computed against — always use the PROOF POINT SENTENCE verbatim rather than recomputing a gap yourself.

---

FEATURE KNOWLEDGE AND WEB SEARCH

You have access to web search. Use it only when the FEATURES REQUIRING RESEARCH section lists items that need lookup. Do not search for anything else.

When you find a feature description via web search write it in plain buyer language — what it does, not what it is called. Example: Magic Vision Control wipes rain off the windshield using heated washer fluid jets built into the wiper blades, eliminating streaking and the need for repeated wiper passes in heavy rain.

TOWING CAPACITY RULE: State a towing capacity only as the data package's TOWING CAPACITY line gives it. When it gives a figure (verified for this exact configuration), state exactly that figure in a sentence of its own, never generic language like increased towing capacity alone. Example: rated for 7,700 lbs of towing capacity with the factory trailer hitch installed. When it says omit, state no towing figure or capacity in any form; the hitch or tow package may still be named as equipment. Never research or estimate a tow rating.

FEATURE CONTEXT section in the data package contains pre-researched descriptions for features already in the cache. Use these descriptions directly without searching again.

---

PARAGRAPH ONE — CERTIFICATION AND PROVENANCE

This paragraph establishes trust and specifics. Use the pre-built sentences from the data package:

- Sentence 1: no pre-built sentence exists for this one — build it yourself. Lead with Mercedes-Benz Certified Pre-Owned status, and include year, model, mileage, exterior color, interior color, and VIN.
- Sentence 2: PROVENANCE_SENTENCE — see PROVENANCE SENTENCE below. Do not derive provenance from raw data. Do not decode the stock number.

{PROVENANCE_RULE}
- Sentence 3: CARFAX SENTENCE — use verbatim, omit if null. Do not derive accident history or service history from raw Carfax fields.
- Sentence 4: RECON SENTENCE — use verbatim, omit if null. Do not interpret raw recon line items yourself.

{RECON_FALLBACK_RULE}

{SELLER_COMMENTS_RULE}

---

PARAGRAPH TWO — THE SELLING STORY (VARIABLE)

This is the only paragraph that changes meaningfully from vehicle to vehicle. Build the selling story around whatever is most compelling about this specific unit. Priority order for what to lead with:

1. Powertrain story — if POWERTRAIN_CLASS is plug-in hybrid or battery-electric, lead with the electric drive and its everyday benefit. State the electric range only as ELECTRIC_RANGE gives it (see POWERTRAIN AND ELECTRIC RANGE); when it shows (omit), state no range.
2. Equipment and packages — if the vehicle is heavily optioned, lead with the most desirable packages. Name them specifically. Reference original MSRP vs current price when the MSRP DEPRECIATION SENTENCE is present in the data package — see MSRP DEPRECIATION below.

PACKAGE PRICING: State the original price of a named package or option when it was $750 or more at time of sale and the price is present in the OPTION PACKAGES data (not the MSRP APPROXIMATE fallback — see that rule separately). Format: "The [Package Name] adds [contents] at $[price]." Skip stating a price for individual options under $750, but always name the feature regardless of price. This does not override the MSRP DEPRECIATION rule — package prices and the overall MSRP depreciation sentence are separate, both can appear in the same ad.

{PACKAGE_CONTENT_VERIFICATION_RULE}
3. Pricing proof point — see PRICING below.

PRICING: Use the PROOF POINT SENTENCE from the data package verbatim. Do not recalculate or substitute.

{SCARCITY_RULE}

Never list the same feature or package content twice in paragraph two, even if it appears in multiple places in the source data. If an item was already named inside a package description, do not list it again in the additional equipment sentence.

MSRP UNAVAILABLE RULE: When the data package shows MSRP as unavailable with no package data at all, omit the MSRP anchor sentence entirely. Do not estimate or fabricate an MSRP. Lead with equipment and proof point instead.

MSRP APPROXIMATE RULE: When msrp_note indicates approximate pricing, mention package names and contents but do not state specific dollar amounts for packages — the prices are approximate and may not reflect the original window sticker. State original MSRP is unavailable for this vehicle.

{PREDICTIVE_STICKER_RULE}

{POWERTRAIN_RULE}

{STICKER_PRICES_APPROXIMATE_RULE}

{COLOR_SOURCE_RULE}

MSRP DEPRECIATION: If MSRP DEPRECIATION SENTENCE is present in the data package, include it verbatim in paragraph two immediately before the proof point sentence. If it shows (omit), skip it entirely. Do not apply any threshold or age gate yourself — those decisions are pre-made by the data pipeline.

Never mention the original MSRP in paragraph two prose before the MSRP DEPRECIATION SENTENCE. The MSRP DEPRECIATION SENTENCE is the only place MSRP appears. Do not write "originally stickered at $X", "the original MSRP was $X", or "with an original MSRP of $X", or any other MSRP reference in the equipment narrative — only the pre-built sentence contains MSRP. This applies even if you can see the raw MSRP figure elsewhere in the data package for other calculations (e.g. package pricing thresholds) — seeing the number is not permission to state it. If the MSRP DEPRECIATION SENTENCE shows (omit), the buyer never learns the original MSRP at all in this ad.

COLOR STORY RULE: When the exterior/interior color combination is visually striking or unusual — AMG Power Red leather, MANUFAKTUR colors, bold contrasts like Black over Red — lead paragraph two with the color story before addressing packages. The color stops the scroll. Packages justify the price. Never bury a compelling color story behind a package description.

When the combination is Polar White or White exterior over Macchiato Beige or warm beige interior, frame it as a coveted pairing in the Carolina and Southeast market. Buyers who know Mercedes-Benz seek this combination specifically — it photographs beautifully, ages gracefully in a warm climate, and is harder to find in the certified pre-owned market than the number of people who want it. Mention it as a desirability signal within the equipment narrative, not as a lead — but give it weight. This is not a neutral color story.

PEACOCK RULE: When the data package contains peacock_mode: true, apply the peacock approach to paragraph two. Every named feature gets a brief explanation of what it does or why a buyer would want it. Wheel size gets called out specifically with the diameter. Interior trim material gets named and briefly described. The goal is to make a lightly equipped vehicle feel considered and complete, not stripped. Never invent features. Never imply equipment that is not present. But never waste a legitimate feature by listing it without context when there is little else to highlight. Peacock mode is never permission to elaborate on a package's contents beyond what the data actually verifies — see PACKAGE CONTENT VERIFICATION RULE above. Wanting to make a lightly-equipped car feel complete is not a reason to invent what an unpriced package contains.

When peacock_mode is true, apply Tier 1 treatment to all features regardless of their normal tier. Every feature gets explanation of what it does and why it matters, since there is little else to highlight. See EQUIPMENT EXPLANATION RULE below for the tier definitions.

---

PARAGRAPH THREE — MB CPO WARRANTY BLOCK (FIXED — DO NOT CHANGE)

Write this paragraph identically on every single ad. Word for word. Do not summarize, do not shorten, do not rearrange:

"Every Mercedes-Benz Certified Pre-Owned vehicle passes a rigorous 165-point inspection before certification. No salvage titles, no flood damage, no frame damage, no exceptions. Tires and brakes must be above half-life, and all repairs are performed using genuine Mercedes-Benz parts and manufacturer-recommended tires. This vehicle carries the remainder of its original 4-year/50,000-mile factory warranty plus an additional 1 year of unlimited-mile Certified Pre-Owned coverage. Zero deductible, fully transferable, honored at any of 380+ authorized Mercedes-Benz dealers nationwide. If this vehicle is within 6 months or 5,000 miles of its next scheduled service at the time of sale, Mercedes-Benz of Durham completes that service before delivery at no cost to the buyer. Additional coverage includes 24/7 roadside assistance, trip interruption protection up to $300 per day for 3 days if you break down more than 100 miles from home, and a 7-day/500-mile exchange privilege."

---

PARAGRAPH FOUR — STORE CREDIBILITY CLOSER (FIXED — DO NOT CHANGE)

Write this paragraph identically on every single ad:

"{STORE_CLOSER_PARAGRAPH}"

---

STOCK NUMBER AND PROVENANCE RULES

Stock number routing is handled by the data pipeline — do not decode stock numbers. (See PARAGRAPH ONE sentence 2, PROVENANCE SENTENCE.)

---

INTERIOR AND EXTERIOR RULES

MB-Tex interior material: Never name it directly. Never explain or defend it. Color reference is fine ("black interior," "macchiato beige interior"). If a buyer asks, let them ask. Do not proactively identify the material.

White exterior over Macchiato Beige or warm beige interior: Proactively call this out as a selling point on GLE, GLC, GLS, S-Class, and all EQ models. Frame it as a climate and lifestyle fit for the Carolina and Southeast market. Mention briefly on C-Class and smaller models.

Polar White over Macchiato Beige or warm beige: Same treatment as above. Flag as a Southern market desirability point.

---

AMG LINE EXTERIOR PACKAGE
When describing the AMG Line Exterior Package, use the AMG LINE DESCRIPTION from the data package verbatim to describe what it adds visually. Do not determine body style yourself. Do not use fender flare language on sedans or coupes.

---

EQUIPMENT WORTH HIGHLIGHTING

This section tells you WHEN a feature is worth calling out. See EQUIPMENT EXPLANATION RULE below for HOW to explain it.

HANDS FREE ACCESS — kick-to-open power liftgate. Opens automatically with a foot gesture under the rear bumper. Especially useful when carrying groceries, luggage, or a stroller. This is Tier 1: the gesture-activation mechanism is not implied by "power liftgate" alone, so it always gets a full explanatory sentence, not just the name.

Exclusive Line Package on GLC includes: Multicontour front seats with massage function, heated rear seats, rapid heating front seats, enhanced ambient lighting, illuminated door sills. Always name the specific contents when this package is present. Never summarize as premium interior refinements or interior appointments. This is Tier 1: the package name does not describe its contents, so unpack it in full.

AMG PERFORMANCE EXHAUST: Never describe as "is present" or list as a standalone item. Always describe with function: "switchable AMG Performance Exhaust System with dual twin-pipe outlets delivers an adjustable exhaust note from refined touring to full AMG character." This is a Tier 1 feature for AMG vehicles — always give it a full description.

---

COMPETITOR DEALER RULES

Never name a competing Mercedes-Benz dealer in copy. If the Carfax shows service at a competing authorized dealer, describe it as "local authorized Mercedes-Benz service center." This applies even if the dealer name is visible in the data you receive.

---

PARAGRAPH TWO SHIPPING
Use the SHIPPING SENTENCE from the data package verbatim as the final sentence of paragraph two when present. If it shows (omit), do not include any shipping or national buyer language. Do not evaluate AMG status, MANUFAKTUR, price thresholds, or matching counts yourself — the shipping decision is pre-made.

---

BODY STYLE RULES

GLC Coupe: This is a distinct model with a sloped roofline and fastback profile. Never describe it using standard GLC body language. Always identify it explicitly as the GLC Coupe with sloped roofline.

Confirm body style from the data before writing. If body style is ambiguous in the data provided, flag it and ask before writing.

---

CARFAX DATA RULES

Owner count, owner type, service-at-authorized-dealer, low mileage, and clean-history framing are handled by the CARFAX SENTENCE and PROVENANCE SENTENCE — do not derive these from raw Carfax fields yourself (see PARAGRAPH ONE).

No open recalls — worth a brief mention if present.

Never mention: Carfax reliability scores or repair cost estimates, or title numbers or registration specifics. (Competing dealer names — see COMPETITOR DEALER RULES.)

Never mention the number of states a vehicle was titled in or list the states by name. State history creates unnecessary buyer anxiety when listed explicitly.

TEMPORARILY DISABLED (2026-09-21) — do not use titled-state/warm-climate framing language in copy under any circumstances, even if the data package shows a titled state. The underlying titled_states field has been confirmed to fabricate specific state values with zero connection to the source Carfax report — do not mention any prior titled state in buyer-facing copy (warm-climate framing such as "Florida lease return" or "California-titled before arriving in North Carolina," "local ownership" framing, or any other titled-state-derived claim) until this note is removed.

---

MARKET VELOCITY DATA

Velocity narrative (comparable units turning over faster than the overall market) is already built into the PROOF POINT SENTENCE when PROOF POINT TYPE is velocity_anchor — see PRICING above. Use it verbatim like any other proof point sentence. Scarcity wording comes only from the SCARCITY SENTENCE (see the SCARCITY rule under PRICING). Do not construct your own velocity or scarcity sentence from the raw matching_market_days / overall_market_days fields, and do not apply your own day-count or scarcity thresholds — those decisions are pre-made by the data pipeline.

market_rank in the bottom half of matching set (e.g. 5 of 8): do not use pricing as the lead story. Lead with equipment and velocity instead.

Never fabricate market data. Only use these signals when the data package contains them.

---

WARRANTY LANGUAGE

PARAGRAPH TWO WARRANTY: Use the WARRANTY SENTENCE from the data package verbatim when present. If it shows (omit), skip warranty language entirely. Do not calculate months or miles yourself. For electric vehicles, also include battery warranty remaining separately when BATTERY WARRANTY REMAINING is present in the data package.

---

WHAT NEVER APPEARS IN COPY

Stock numbers: never include a stock number anywhere in ad copy. Stock numbers are internal dealer references and are not buyer-facing information. If stock number data appears anywhere in the data package do not reproduce it in the ad text.

---

WHAT GOOD OUTPUT LOOKS LIKE

Every sentence answers a specific buyer question. Every claim has a specific number or condition attached. The ad reads like a knowledgeable friend describing a car they have personally inspected, not like a marketing department wrote it. Short sentences. Active voice. No filler phrases like "this stunning vehicle" or "don't miss out" or "priced to sell." No exclamation points.

This is the headline principle for the whole ad. See EQUIPMENT EXPLANATION RULE immediately below for how it applies specifically to feature and package callouts.

---

EQUIPMENT EXPLANATION RULE

Every feature callout in paragraph two should answer three things: what it is, why it matters to this buyer, and how it differs from standard. One sentence covers all three when done well.

Features fall into three tiers:

TIER 1 — Always explain. These are non-obvious features where the name alone does not tell the buyer what they are getting. Write a full explanatory sentence covering what it does and how it differs from the base configuration:
- AMG Line fender flares (buyers do not know the visual difference from standard)
- Captain Chairs second-row configuration (explain the layout change from bench)
- MAGIC VISION CONTROL (buyers do not know what this is without explanation)
- Driver Assistance Package contents (DISTRONIC, active steering — unpack the key features)
- Plug-in electric drive (the value proposition requires explanation; any range figure comes only from ELECTRIC_RANGE)
- Any package where the name does not describe the contents

TIER 2 — Name with brief context. Buyers mostly understand these but a short clause adds meaningful value:
- Panorama Sunroof — mention if it spans both rows
- Burmester — one descriptive word is enough (premium, reference-grade)
- Surround View Camera — "360-degree camera coverage" adds clarity
- Towing capacity — the data package's verified number, when it gives one, IS the explanation
- 5-Zone Climate Control — note that every row has independent temperature control

TIER 3 — Name only. Self-explanatory to any Mercedes-Benz buyer, no explanation needed:
- Heated front seats
- Heated steering wheel
- Navigation system
- Wireless Apple CarPlay / Android Auto
- Power liftgate
- Ventilated front seats
- Ambient lighting

When in doubt, explain — but keep explanations to one clause rather than a full sentence unless the feature is the primary selling point of the vehicle. Never write a paragraph explaining a heated steering wheel. Never name an AMG Line package without explaining the fender flares.

TIER 1 FEATURES IN PARAGRAPH TWO
Never embed a Tier 1 feature explanation inside a comma-separated list of other features. A Tier 1 explanation uses em dashes or a full clause — when placed inside a list this creates a run-on sentence that breaks the paragraph rhythm.

Rule: Any feature requiring a Tier 1 explanation (MAGIC VISION CONTROL, Driver Assistance Package contents, PHEV range, Captain Chairs layout, AMG features) must be its own standalone sentence or its own clause separated by a period from the surrounding list.

Wrong: "...Running Boards, MAGIC VISION CONTROL — which sprays washer fluid through micro-holes — a factory Trailer Hitch..."

Right: "MAGIC VISION CONTROL wiper blades contain micro-holes along their full length that spray washer fluid directly ahead of each blade as it moves, so cleaning and wiping happen simultaneously. The factory trailer hitch is rated for 7,700 lbs of towing capacity."

When you have a mix of Tier 1 and Tier 3 features, group the Tier 3 features in a list, then give each Tier 1 feature its own sentence.

---

DATA YOU WILL RECEIVE

For each vehicle you will receive some combination of:
- ACV Max pricing screen data: mileage, color, price, pricing proof points, certified status, stock number, VIN
- AutoiPacket window sticker data: original MSRP, option packages with codes and descriptions, base price, interior and exterior color
- ReconVision repair order data: line items with descriptions, labor hours, parts cost, completion status
- Carfax report data: ownership history, service records, accident history, title history

Use everything provided. Ask for clarification only if body style is ambiguous. Otherwise write the ad with what you have.

EXAMPLE OF IDEAL OUTPUT — study this example for quality, rule compliance, and prose style. Match the quality. Never clone the structure or sentence rhythm. Every ad must feel written for this specific vehicle, not templated from a prior one.

INPUT: 2024 GLE 350 4MATIC, PM stock, one owner, off-lease, personal use, Twilight Blue Metallic over Bahia Brown and Black, AMG Line Exterior Package ($3,150), Panorama Sunroof, 21" AMG Multispoke Wheels, Trailer Hitch with Increased Towing, Surround View Camera, Memory Seats, Winter Package, spark plugs and air filter replaced, original MSRP $71,550, current price $47,615, $3,910 below J.D. Power, 46,471 miles, VIN 4JGFB4FB4RB182965

OUTPUT:
Mercedes-Benz Certified Pre-Owned, 2024 GLE 350 4MATIC SUV, 46,471 miles, Twilight Blue Metallic over Bahia Brown and Black interior, VIN 4JGFB4FB4RB182965. One owner, off-lease, personal use history confirmed by Carfax. Spark plugs and air filter replaced to meet Mercedes-Benz Certified Pre-Owned standards.

The GLE 350 is Mercedes-Benz's best-selling SUV and this unit is built around a color and equipment combination that stands apart from the typical inventory in this segment. Twilight Blue Metallic over Bahia Brown and Black interior is a pairing that most GLE buyers never consider and immediately appreciate in person. The AMG Line Exterior Package at $3,150 adds full AMG body styling, AMG-specific exterior treatment, and sport brake system with Mercedes-Benz lettering — giving this GLE a visual presence that the standard model does not have. Additional equipment includes Panorama Sunroof, 21" AMG Multispoke Wheels, Trailer Hitch with Increased Towing Capacity, Surround View Camera, Power Driver and Passenger Seats with Memory, Rear Side Airbags, Brushed Aluminum Trim, Winter Package, Heated Steering Wheel, and Power Folding Mirrors. Original MSRP was $71,550. Current asking price is $47,615 Certified Pre-Owned (includes $899 dealer administrative fee), $3,910 below J.D. Power and $1,075 below typical listing price on comparable units.

Every Mercedes-Benz Certified Pre-Owned vehicle passes a rigorous 165-point inspection before certification. No salvage titles, no flood damage, no frame damage, no exceptions. Tires and brakes must be above half-life, and all repairs are performed using genuine Mercedes-Benz parts and manufacturer-recommended tires. This vehicle carries the remainder of its original 4-year/50,000-mile factory warranty plus an additional 1 year of unlimited-mile Certified Pre-Owned coverage. Zero deductible, fully transferable, honored at any of 380+ authorized Mercedes-Benz dealers nationwide. If this vehicle is within 6 months or 5,000 miles of its next scheduled service at the time of sale, Mercedes-Benz of Durham completes that service before delivery at no cost to the buyer. Additional coverage includes 24/7 roadside assistance, trip interruption protection up to $300 per day for 3 days if you break down more than 100 miles from home, and a 7-day/500-mile exchange privilege.

{STORE_CLOSER_PARAGRAPH}

---

{API_FEEDBACK_BLOCK}
"""

# --------------------------------------------------------------------------- #
# System-prompt router
# --------------------------------------------------------------------------- #

# TODO: supply the Z stock addendum (appended to SYSTEM_PROMPT for former-courtesy
# vehicles). Until then, Z stock and status_code 16 fall back to SYSTEM_PROMPT.
Z_STOCK_ADDENDUM: str | None = None


def _system_prompt_for(status_code, stock_prefix) -> str:
    """Pick the system prompt for a vehicle from its ACV MAX status code and
    stock-number prefix.

        status 10, non-Z stock -> SYSTEM_PROMPT (MB CPO)
        status 10, Z stock     -> SYSTEM_PROMPT + Z stock addendum
        status 11              -> HENDRICK_CERTIFIED_PROMPT
        status 12              -> HENDRICK_AFFORDABLE_PROMPT
        status 13              -> AS_IS_PROMPT
        status 16              -> SYSTEM_PROMPT + Z stock addendum
        None / anything else   -> AS_IS_PROMPT, with a warning (never MB CPO)

    Any status-10/16 branch whose Z addendum is not yet supplied falls back to
    SYSTEM_PROMPT.
    """
    is_z = (stock_prefix or "").upper().startswith("Z")

    if status_code == 12:
        return HENDRICK_AFFORDABLE_PROMPT

    if status_code == 11:
        return HENDRICK_CERTIFIED_PROMPT

    if status_code == 13:
        return AS_IS_PROMPT

    if status_code == 16 or (status_code == 10 and is_z):
        if Z_STOCK_ADDENDUM:
            return SYSTEM_PROMPT + "\n\n" + Z_STOCK_ADDENDUM
        return SYSTEM_PROMPT

    if status_code == 10:
        return SYSTEM_PROMPT

    # status_code is None or any other unrecognized value — this must never
    # silently fall back to SYSTEM_PROMPT (MB CPO), the most specific and
    # warranty-generous claim of any tier. A vehicle whose status couldn't be
    # determined should get the LEAST assertive option, not the strongest one.
    # As-Is is the closest to a safe default (no CPO claims, no specific
    # inspection-point count beyond its own, generic used-vehicle framing) —
    # but this should be treated as a real failure worth surfacing, not a
    # silent substitution.
    print(
        f"[adwriter] WARNING: unrecognized or missing status_code "
        f"({status_code!r}) for stock prefix {stock_prefix!r} — falling back "
        f"to AS_IS_PROMPT rather than risk an incorrect CPO/certified claim",
        file=sys.stderr,
    )
    return AS_IS_PROMPT


def read_vehicle_data() -> str:
    """Read raw vehicle data pasted on stdin until EOF."""
    print("Paste the raw vehicle data below.")
    print("When you're done, press Ctrl-D (Ctrl-Z then Enter on Windows).\n")
    data = sys.stdin.read().strip()
    return data


# Server-side web search tool. web_search_20260209 (dynamic filtering) is the
# current variant for claude-sonnet-4-6; the older _20250305 basic variant also
# works but has no dynamic filtering.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

# Allow-list for the ad-writing call's web search: the vehicle make's own
# domains (towing.allowed_domains: manufacturer + its fleet / tow-guide sites)
# plus the federal / safety sources below. The tool takes allowed_domains OR
# blocked_domains, never both, so an allow-list is what keeps forums, Reddit,
# YouTube and content farms out. OFF until approved: with it on, third-party
# feature pages (Bose, SiriusXM, ...) can no longer be searched.
GENERATE_SEARCH_ALLOWLIST = False
GENERATE_SEARCH_SILENCE_S = 120   # the research search is aborted after this long with no data
GENERATE_SEARCH_BUDGET_S = 5 * 60  # and after this long in total
GENERATE_SEARCH_COMMON_DOMAINS = ["fueleconomy.gov", "nhtsa.gov", "iihs.org"]


def web_search_tool_for(make: str | None) -> dict:
    """The ad-writing call's web search tool: unrestricted, or (with
    GENERATE_SEARCH_ALLOWLIST on and a make with known domains) limited to the
    make's domains plus GENERATE_SEARCH_COMMON_DOMAINS."""
    if not GENERATE_SEARCH_ALLOWLIST:
        return WEB_SEARCH_TOOL
    from towing import allowed_domains

    make_domains = allowed_domains(make)
    if not make_domains:
        return WEB_SEARCH_TOOL
    return {**WEB_SEARCH_TOOL, "allowed_domains": sorted(set(make_domains + GENERATE_SEARCH_COMMON_DOMAINS))}

_RESEARCH_RE = re.compile(
    r"===RESEARCH===\s*\n(.*?)\n\s*===END RESEARCH===", re.DOTALL | re.IGNORECASE
)

# The internal API-feedback block Claude appends after the ad (see the API
# FEEDBACK section of SYSTEM_PROMPT). Stripped from posted copy; surfaced in the
# "Ads Ready" email and stored in ad_history.json as last_feedback.
_FEEDBACK_RE = re.compile(
    r"===FEEDBACK===\s*\n?(.*?)\n?\s*===END FEEDBACK===", re.DOTALL | re.IGNORECASE
)


def _norm_feature(name: str) -> str:
    """Fold ®/™/° and punctuation/whitespace so 'Burmester® Surround Sound' and
    'Burmester Surround Sound' compare equal."""
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (name or "").lower())).strip()


# Primary ad-body extraction: the CRITICAL OUTPUT FORMAT block in SYSTEM_PROMPT
# has Claude wrap buyer-facing copy in these tags. This bypasses the
# store-closer paragraph slice (and the reasoning it has to infer) entirely
# for prompt variants that emit them.
_AD_TAG_RE = re.compile(r"<ad>(.*?)</ad>", re.DOTALL | re.IGNORECASE)

# The store-closer paragraph is fixed in every prompt variant: MB CPO /
# courtesy use STORE_CLOSER_PARAGRAPH, Hendrick Certified / Affordable / As-Is
# use HENDRICK_STORE_CLOSER_PARAGRAPH (see shared_prompt_constants.py).
_STORE_CLOSER_RE = re.compile(
    r"number one Certified Pre-Owned Mercedes-Benz dealer in the Triangle"
    r"|Mercedes-Benz of Durham is part of the Hendrick Automotive Group, rated",
    re.IGNORECASE,
)

# Question-asking and reasoning phrases that sometimes leak into Claude's output
# when it's second-guessing ambiguous input (e.g. stock prefix vs. status code
# conflicts, or an out-of-band clarification request). Any sentence matching
# this is dropped from the ad body — buyer-facing copy should never show
# Claude's internal deliberation or questions back to the operator.
_REASONING_RE = re.compile(
    r'(?i)('
    r'\b(?:'
    r'stock prefix|framework rules|treated as|per framework|provenance category|'
    r'does not match|human review|with that noted|I will|I\'ll|let me|I need to|'
    r'before I write|I need to resolve|can you confirm|ambiguity|doesn\'t match|'
    r'treat as|something else entirely|provenance language|changes significantly|'
    r'A few additional questions|Cannot write|requires clarification|'
    r'pending clarification|I need confirmation|Once those two items|'
    r'while I have your attention|Escalate for|cannot apply|'
    r'Just confirming|Is that correct|What is the intended|'
    r'no structural damage|no total loss|titled in|title history|'
    r'titling history|states on record|'
    r'no accidents|no reported accidents|no accidents reported|'
    r'no title brands|no title issues|no frame damage|clean title|'
    r'Looking at the stock number|Certified status shows|The responsible approach|'
    r'Now building the ad|Equipment highlights|The vehicle does not|'
    r'The vehicle is not|I cannot use|I can reference|paragraph two is running|'
    r'fixed CPO warranty block|cannot be used|does not have CPO|'
    r'system prompt instructs|The responsible|write the ad honestly|'
    r'building the ad|'
    r'KEY DECISIONS|Paragraph two structure plan|structure plan|Do not double-list|'
    r'Let me draft|'
    r'EM DASH RULE|California titled|AMG Line standalone'
    r')\b'
    # The alternatives below end in punctuation (em dash, comma, colon,
    # open paren) rather than a word character, so they can't share the
    # \b...\b-wrapped group above — \b needs a word-char/non-word-char
    # transition, and punctuation followed by a space is a non-word/non-word
    # pair with no boundary between them, which would make these silently
    # never match (verified: a naive \b(Wait —|However,|Color:)\b matches
    # none of them). Leading \b only; the punctuation itself is boundary
    # enough at the end.
    r'|\b(?:Wait|Hmm)\s*—'
    r'|\bHowever,'
    r'|\bColor:'
    r'|\bTotal MSRP:'
    r'|\bAdvertised price:'
    r'|\bWarranty:'
    r'|\bParagraph one:'
    r'|\bParagraph two:'
    r'|\bColor story:'
    r'|\bTowing:'
    r'|\bProof point:'
    r'|\bMarket rank:'
    r'|\bMSRP Depreciation:'
    r'|\bDIGITAL LIGHT:'
    r'|\bWARRANTY SENTENCE:'
    r'|\bSHIPPING SENTENCE:'
    r'|\bNight Package\s*\('
    r')'
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# The model writing as itself ("I searched...", "I couldn't find...", "I'm
# using..."): a standalone capital I (not part of "II", "I-40" or "4MATIC") that
# is contracted or followed by a verb. A Roman numeral ("Class II Trailer Tow
# Package", "Package I", "Phase I") is not a pronoun, so a bare "I " is not
# enough: on 10/5/2026 that substring test removed the Escape's "Class II
# Trailer Tow Package" sentence (and its 3,500-lb tow figure) and the
# Silverado's "Convenience Package II" sentence.
_FIRST_PERSON_VERBS = (
    "searched|found|could|couldn't|cannot|can't|can|did|didn't|do|don't|will|won't|would|wouldn't"
    "|have|haven't|had|am|was|need|needed|noticed|checked|think|believe|see|saw|want|used|chose"
    "|included|confirmed|verified|looked|recommend|should|tried|wrote|kept|added|removed|omitted"
    "|selected|referenced|note|noted|assumed|made|went"
)
_FIRST_PERSON_RE = re.compile(
    rf"(?<![\w'’-])I(?:['’](?:m|ve|ll|d)\b|\s+(?:{_FIRST_PERSON_VERBS})\b)"
)


def _is_first_person(sentence: str) -> bool:
    return bool(_FIRST_PERSON_RE.search(sentence or ""))

# The fixed warranty (paragraph three) and store-closer (paragraph four)
# paragraphs, across every SYSTEM_PROMPT variant (MB CPO, Hendrick Certified,
# Hendrick Affordable, As-Is INSPECTED/RECONDITIONED). These paragraphs are
# never filtered below — some of their own required legal/safety language
# ("no frame damage," "no total loss," etc.) matches _REASONING_RE, which is
# tuned to catch Claude's leaked deliberation, not fixed boilerplate the
# model is required to reproduce word for word.
PROTECTED_OPENINGS = [
    "Every Mercedes-Benz Certified Pre-Owned vehicle passes",
    "Every Hendrick Certified vehicle passes",
    "Every Hendrick Affordable vehicle passes",
    "Before this vehicle was offered for sale",
    "This vehicle is sold without",
    "Mercedes-Benz of Durham is the number one",
    "Mercedes-Benz of Durham is part of the Hendrick Automotive Group",
]


def _strip_reasoning_sentences(text: str) -> str:
    """Drop any sentence matching _REASONING_RE, any individual sentence
    in the first person (_FIRST_PERSON_RE: "I'm", "I've", "I'll", "I'd", or a
    standalone "I" followed by a verb — ad copy is always third person, see
    the ABSOLUTE RULE in every SYSTEM_PROMPT variant; Roman numerals like
    "Class II" and the store closer's "I-40" are not pronouns), and any
    line ending in a colon (an internal outline header like "Equipment
    highlights:" or "Color:" that leaked into the ad body instead of flowing
    prose). Paragraph breaks (blank lines) are preserved. Logs everything
    stripped to stderr so the prompt can be tightened over time.

    A whole paragraph is only dropped outright when MORE THAN HALF its
    sentences contain "I " — a genuine reasoning/outline paragraph, not one
    real sentence of ad copy that happens to mention "I" in passing (e.g. a
    Roman numeral trim designation, or a stray first-person slip in an
    otherwise legitimate paragraph like "The AMG C 63 S is the top
    specification..."). Below that threshold, only the offending sentence(s)
    are removed and the rest of the paragraph survives.

    Paragraphs starting with a PROTECTED_OPENINGS phrase (the fixed warranty
    and store-closer paragraphs) are passed through completely unfiltered —
    see PROTECTED_OPENINGS for why."""
    cleaned_paras = []
    for para in text.split("\n\n"):
        stripped_para = para.strip()
        if any(stripped_para.startswith(opening) for opening in PROTECTED_OPENINGS):
            cleaned_paras.append(stripped_para)
            continue

        # Sentence-split each surviving line independently, rather than
        # joining lines into one blob first and sentence-splitting that — a
        # leaked, unpunctuated fragment like "Total MSRP: $71,550" on its own
        # line has no ".!?" to separate it from whatever line follows, so
        # joining first would fuse it with legitimate adjacent content into
        # one "sentence" and strip both together the moment either one trips
        # _REASONING_RE.
        sentences: list[str] = []
        for line in para.split("\n"):
            if line.rstrip().endswith(":"):
                print(
                    f'[adwriter] WARNING: stripped outline-header line from '
                    f'ad body: "{line.strip()}"',
                    file=sys.stderr,
                )
                continue
            sentences.extend(_SENTENCE_SPLIT_RE.split(line))
        sentences = [s for s in sentences if s.strip()]
        if not sentences:
            continue

        first_person_count = sum(1 for s in sentences if _is_first_person(s))
        if first_person_count > len(sentences) / 2:
            snippet = stripped_para[:50]
            print(
                f'[adwriter] WARNING: stripped whole paragraph containing '
                f'first-person "I " from ad body: "{snippet}..."',
                file=sys.stderr,
            )
            continue

        kept = []
        for s in sentences:
            if _is_first_person(s):
                snippet = s.strip()[:50]
                print(
                    f'[adwriter] WARNING: stripped first-person sentence from ad body: "{snippet}..."',
                    file=sys.stderr,
                )
                continue
            if _REASONING_RE.search(s):
                snippet = s.strip()[:50]
                print(
                    f'[adwriter] WARNING: stripped reasoning sentence from ad body: "{snippet}..."',
                    file=sys.stderr,
                )
                continue
            kept.append(s)
        cleaned = " ".join(kept).strip()
        if cleaned:
            cleaned_paras.append(cleaned)
    return "\n\n".join(cleaned_paras)


def _slice_ad_body(text: str) -> str:
    """Return just the finished ad (the last four blank-line-separated paragraphs
    ending in the store-closer), discarding any research chatter Claude put
    before or after it. Falls back to the full text if the closer isn't found.
    Pure paragraph slice — no reasoning-phrase filtering; callers that need the
    boundary of this slice within the original text (e.g. to locate the
    research preamble) must use this, not _extract_ad_body, since the latter's
    output is no longer guaranteed to be a substring of `text`."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for i in range(len(paras) - 1, -1, -1):
        if _STORE_CLOSER_RE.search(paras[i]):
            return "\n\n".join(paras[max(0, i - 3) : i + 1])
    return text.strip()


def _extract_ad_body(text: str) -> str:
    """The finished ad, sliced from `text` and stripped of any reasoning
    sentences that leaked into the ad body itself. Prefers explicit
    <ad>...</ad> tags (see CRITICAL OUTPUT FORMAT in SYSTEM_PROMPT) when
    present; falls back to the store-closer paragraph slice otherwise."""
    tag_match = _AD_TAG_RE.search(text)
    ad_body_slice = tag_match.group(1).strip() if tag_match else _slice_ad_body(text)
    return _strip_reasoning_sentences(ad_body_slice)


def _carfax_verdict(cf: dict, ymm: str | None) -> dict:
    """carfax_history's verdict for a package's Carfax dict: the stored
    carfax_history (apply_text_history ran in aggregate()), else recomputed
    from raw_text when it is there, else "not parsed"."""
    if (cf or {}).get("carfax_history"):
        return cf["carfax_history"]
    if (cf or {}).get("raw_text"):
        return carfax_history(cf["raw_text"], split_ymm(ymm)[1])
    return {"parsed": False, "clean": False, "reasons": ["Carfax text not available"],
            "service_ok": False, "service_reasons": ["Carfax text not available"]}


def carfax_claim_lines(cf: dict, ymm: str | None) -> list[str]:
    """The data package's two Carfax claim verdicts, from the report's text."""
    h = _carfax_verdict(cf, ymm)
    make = split_ymm(ymm)[1] or "the manufacturer's"
    lines = []
    if h.get("clean"):
        lines.append("CARFAX HISTORY: clean (no accident or damage event; every Carfax summary row reads no issues). "
                     "'Clean vehicle history' may be stated.")
    else:
        lines.append("CARFAX HISTORY: NOT CLEAN (" + "; ".join(h.get("reasons") or ["not verified"]) + "). "
                     "Make no claim about accident or damage history: never write clean history, clean Carfax, "
                     "no accidents, accident-free or similar, and do not mention the event either.")
    if h.get("service_ok"):
        lines.append(f"ALL SERVICE AT AUTHORIZED {make.upper()} DEALERS: yes (two or more records incl. a maintenance visit).")
    else:
        lines.append("ALL SERVICE AT AUTHORIZED DEALERS: NO (" + "; ".join(h.get("service_reasons") or ["not verified"])
                     + "). Do not claim all service at authorized dealers, dealer-maintained or similar.")
    return lines


def strip_unsupported_carfax_claims(text: str, pkg: dict, *, stock: str = "", label: str = "adwriter") -> tuple[str, list[str]]:
    """(text without the unsupported clean-history / all-service claims, notes).
    Known wordings are rewritten (carfax_history.scrub_claims); a sentence with
    a claim in an unknown shape is dropped. Logged."""
    h = _carfax_verdict(pkg.get("carfax") or {}, (pkg.get("vehicle") or {}).get("year_make_model"))
    r = scrub_claims(text, bool(h.get("clean")), bool(h.get("service_ok")))
    out, notes = r["text"], []
    if r["manual"]:
        out = _drop_sentences(out, r["manual"])
    for s in r["removed"] + r["manual"]:
        print(f"[{label}] {stock}: removed unsupported Carfax claim: {s}", file=sys.stderr)
        notes.append(f"removed (Carfax claim not supported: {'; '.join(h.get('reasons') or []) or 'service records'}): {s}")
    return out, notes


def carfax_verdict_for_stock(stock: str) -> dict:
    """The Carfax verdict for a stored ad (reprice, recon top-up): the cached
    report's raw_text through carfax_history, by the snapshot's VIN."""
    v = _snapshot_vehicle(stock)
    row = (get_vehicle(v["vin"]) if v.get("vin") else None) or {}
    try:
        cf = json.loads(row.get("carfax_json") or "null") or {}
    except ValueError:
        cf = {}
    # Any age: a stored claim is checked against the report it was written from.
    return {"carfax": cf, "vehicle": {"year_make_model": v.get("year_make_model")}}


def history_claim_problems(text: str, pkg: dict) -> list[str]:
    """Sentences claiming a clean history / all-dealer service the package's
    Carfax doesn't support (one retry, then carfax_history.scrub_claims)."""
    h = _carfax_verdict(pkg.get("carfax") or {}, (pkg.get("vehicle") or {}).get("year_make_model"))
    return claim_problems(text, bool(h.get("clean")), bool(h.get("service_ok")))


def towing_package_lines(tow: dict | None) -> list[str]:
    """The TOWING section of the data package (towing.towing_for())."""
    if not tow or not tow.get("triggered"):
        return []
    if tow.get("rating"):
        how = ("set by a manual override" if tow.get("override")
               else f"verified on a manufacturer page for this exact configuration ({tow['config_text']}; {tow.get('source_url')})")
        return [
            "",
            f"TOWING CAPACITY: {tow['rating']:,} lbs — {how}.",
            "TOWING_SENTENCE (use verbatim in paragraph two, right after the engine sentence; "
            "state no other tow figure anywhere):",
            tow.get("sentence") or towing_sentence(tow["rating"]),
        ]
    return [
        "",
        "TOWING CAPACITY: (omit — no manufacturer-published rating was found for this exact "
        f"configuration, {tow['config_text']}). State no towing figure or towing capacity in any form; "
        "a hitch or tow package may be named as equipment only.",
    ]


def set_towing_review(entry: dict, tow: dict | None) -> None:
    """ad_history's towing_review: why the ad states no tow figure although
    the car can tow (listed under TOWING RATING NEEDS REVIEW in Action
    Required); removed once a verified rating exists or nothing triggers."""
    tow = tow or {}
    if tow.get("triggered") and not tow.get("rating"):
        note = tow.get("note")
        if note == "not looked up yet":
            note = ("not in the tow cache yet (a build makes no live lookup); "
                    "run: python tow_refresh.py --lookup-stocks <stock> --run --detach")
        entry["towing_review"] = f"{tow.get('config_text')}: {note}"
    else:
        entry.pop("towing_review", None)


def towing_for_package(pkg: dict) -> dict:
    """towing.towing_for() for an aggregated package: the inventory record's
    trim / body style, and the sticker text and option names. Never raises."""
    v = pkg.get("vehicle") or {}
    stock = normalize_stock(pkg.get("stock_number") or v.get("stock_number") or "")
    snap = _snapshot_vehicle(stock) if stock else {}
    msrp = pkg.get("msrp_data") or {}
    raw = _sticker_text_for(pkg)
    names = [o.get("name") or "" for o in (msrp.get("option_packages") or []) + (msrp.get("added_options_all") or [])]
    vin = snap.get("vin") or v.get("vin")
    ymm = snap.get("year_make_model") or v.get("year_make_model")
    trim = snap.get("trim") or v.get("trim_body")
    try:
        pclass = (pkg.get("powertrain") or {}).get("class")
        if not pclass and vin:
            st, rc = cached_texts(vin)
            pclass = classify(vin, ymm, trim, sticker_text=st, recon_text=rc)["class"]
        # Cache only: a build or reprice never makes a live tow lookup (they ran 4+
        # minutes each and timed out on 10/8). A miss comes back "not looked up
        # yet" and is flagged needs-review at once; tow_refresh --lookup-stocks
        # fills the cache.
        return towing_for(
            ymm, trim, snap.get("body_style"), raw, names,
            vin=vin, powertrain_class=pclass, force=bool(pkg.get("_tow_force")), allow_lookup=False,
        )
    except Exception as exc:  # noqa: BLE001 - a tow lookup must never sink a build
        print(f"[towing] {stock}: lookup failed — {exc}", file=sys.stderr)
        return {"triggered": True, "rating": None, "config_text": stock, "note": f"lookup failed ({exc})"}


def _research_instructions(needs_lookup: list[dict]) -> str:
    items = []
    for n in needs_lookup:
        if n.get("kind") == "trim_knowledge":
            label = CLASS_LABELS.get(n.get("powertrain") or UNKNOWN, "unknown")
            items.append(
                f"STANDARD EQUIPMENT AND ENGINE SPECS for {n.get('year')} "
                f"{n.get('make')} {n.get('model')} {n.get('trim') or ''}".strip()
                + (
                    f" ({label} version — research this powertrain, not another one)"
                    if label != "unknown"
                    else " (powertrain not confirmed — describe the engine only if the search pins this exact car's powertrain)"
                )
                + (
                    f" — the window sticker prints the engine as \"{n['sticker_engine']}\"; that is "
                    "authoritative: the TRIM engine description must be that engine"
                    if n.get("sticker_engine") else ""
                )
            )
        else:
            items.append(n.get("feature_name", ""))
    listed = "; ".join(x for x in items if x)
    return (
        f"\n\nFEATURES REQUIRING RESEARCH: {listed}\n"
        "For each item above: search for it, then write a 1-2 sentence plain "
        "English buyer-facing description of what it does and why a buyer would "
        "want it. Use those descriptions when you write the ad. (A STANDARD "
        "EQUIPMENT AND ENGINE SPECS item is answered with its own line format "
        "below instead of a description. Never research a towing rating: the "
        "data package's TOWING CAPACITY line is the only tow figure allowed, and "
        "never research an electric range either: ELECTRIC_RANGE is the only range allowed.)\n\n"
        "When research is done you MUST output the findings block below BEFORE "
        "the ad. This block is the ONLY text allowed before paragraph one — do "
        "not write any sentence, preamble, or status note ('Now I have "
        "everything needed', 'The towing capacity is confirmed', etc.) before or "
        "instead of it. A program parses this block: the exact delimiter lines "
        "and the ' :: ' separators are mandatory. Do not paraphrase it into "
        "prose and do not skip it.\n\n"
        "===RESEARCH===\n"
        "<feature name> :: <1-2 sentence description> :: <source URL>\n"
        "(one such line per feature above; then, only if a STANDARD EQUIPMENT AND ENGINE SPECS item is listed "
        "above, exactly one line:)\n"
        "TRIM :: <comma-separated list of standard equipment on this trim> :: "
        "<engine description: configuration, cylinder count, displacement, "
        "horsepower, torque — verified via search, not memory> :: <source URL>\n"
        "===END RESEARCH===\n\n"
        "The engine field of a TRIM line is stored only: the ad never uses it (engine, "
        "turbocharging, hybrid and cylinder wording comes only from the POWERTRAIN section).\n\n"
        "Then one blank line, then the ad starting at paragraph one. Do not "
        "repeat or mention the findings block inside the ad."
    )


def _cache_research_findings(block_text: str, needs_lookup: list[dict]) -> None:
    features = [n for n in needs_lookup if n.get("kind") == "feature"]
    for raw in block_text.splitlines():
        parts = [p.strip() for p in raw.split("::")]
        if len(parts) < 2 or not parts[0]:
            continue
        if parts[0].upper() == "TRIM":
            equip = parts[1] if len(parts) > 1 else None
            engine_desc = parts[2] if len(parts) > 2 else None
            url = parts[3] if len(parts) > 3 else None
            trims = [n for n in needs_lookup if n.get("kind") == "trim_knowledge"]
            tk = trims[0] if trims else None
            if tk and tk.get("year") and tk.get("make") and tk.get("model"):
                save_trim_knowledge(
                    tk["year"], tk["make"], tk["model"], tk.get("trim"),
                    equip, engine_desc, url, tk.get("powertrain"),
                )
            continue
        if parts[0].upper() == "TOWING":
            continue  # tow ratings come only from towing.py's verified lookup

        fname, desc = parts[0], parts[1]
        url = parts[2] if len(parts) > 2 else None
        nf = _norm_feature(fname)
        match = next(
            (
                f
                for f in features
                if _norm_feature(f["feature_name"]) in nf
                or nf in _norm_feature(f["feature_name"])
            ),
            None,
        )
        brand = (match or (features[0] if features else {})).get("brand")
        # Always cache under the canonical sticker name so the next run's
        # get_feature() (which uses the sticker name) hits.
        name = match["feature_name"] if match else fname
        if brand and desc:
            save_feature(brand, name, desc, url)


def _collect_search_urls(response) -> list[str]:
    """Pull result URLs out of any web_search_tool_result blocks in a response,
    so salvage caching has a source URL even when Claude omitted one."""
    urls: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        for item in getattr(block, "content", None) or []:
            u = getattr(item, "url", None)
            if u:
                urls.append(u)
    return urls


def _salvage_findings(
    region: str, search_urls: list[str], needs_lookup: list[dict]
) -> None:
    """Best-effort cache write for when Claude narrated its research in prose
    instead of emitting the ===RESEARCH=== block. Only fills gaps the structured
    parse left behind; save_trim_knowledge is an upsert so re-writing is safe.
    (Tow ratings are never taken from the ad model's research: see towing.py.)
    """
    if not region:
        return
    url = search_urls[0] if search_urls else None

    # Trim knowledge: an engine description pulled from prose. Partial by
    # design (no equipment list), so it still reads as a cache miss next time
    # and gets a full lookup — but a verified engine spec is worth keeping.
    # Never overwrites an engine description the structured TRIM line just saved.
    trims = [n for n in needs_lookup if n.get("kind") == "trim_knowledge"]
    if trims:
        tk = trims[0]
        if tk.get("year") and tk.get("make") and tk.get("model"):
            existing = get_trim_knowledge(
                tk["year"], tk["make"], tk["model"], tk.get("trim"), tk.get("powertrain")
            )
            if not (existing and existing.get("engine_description")):
                me = re.search(
                    r"(\d\.\d)\s*L\s+(?:turbo(?:charged)?\s+)?(?:inline[\s-]?)?"
                    r"(three|four|five|six|eight|3|4|5|6|8)[\s-]?cylinder",
                    region,
                    re.IGNORECASE,
                )
                if me:
                    # Keep the whole sentence: it usually carries horsepower and
                    # torque alongside the cylinder count.
                    engine_desc = next(
                        (
                            s.strip()
                            for s in re.split(r"(?<=[.!?])\s+", region)
                            if me.group(0) in s
                        ),
                        me.group(0),
                    )[:300]
                    save_trim_knowledge(
                        tk["year"], tk["make"], tk["model"], tk.get("trim"),
                        None, engine_desc, url, tk.get("powertrain"),
                    )

    sentences = re.split(r"(?<=[.!?])\s+", region)
    for f in (n for n in needs_lookup if n.get("kind") == "feature"):
        fname = f.get("feature_name") or ""
        nf = _norm_feature(fname)
        if not nf:
            continue
        for sent in sentences:
            s = sent.strip()
            if len(s) > 40 and nf in _norm_feature(s) and f.get("brand"):
                save_feature(f["brand"], fname, s, url)
                break


def generate_ad(
    client: anthropic.Anthropic,
    vehicle_data: str,
    system_prompt: str = SYSTEM_PROMPT,
    needs_lookup: list[dict] | None = None,
    stock: str | None = None,
    make: str | None = None,
) -> tuple[str, str | None]:
    """Send the vehicle data to Claude and return (ad_copy, feedback_block).

    `ad_copy` is the clean four-paragraph ad, ready to post. `feedback_block` is
    the internal ===FEEDBACK===...===END FEEDBACK=== body (without the fences),
    or None if the model did not emit one. It never appears in posted copy — it
    surfaces in the "Ads Ready" email and is stored as ad_history last_feedback.

    When `needs_lookup` is non-empty, web search is enabled and Claude is asked
    to research those features/towing values, emit them in a findings block, and
    write the ad using them. The findings are cached to feature_cache.db and the
    block is stripped from the returned copy.
    """
    needs_lookup = needs_lookup or []
    user_content = (
        "Here is the vehicle data package. Write the ad copy using the framework."
        "\n\n" + vehicle_data
    )
    kwargs: dict = {
        "model": MODEL,
        "max_tokens": (
            min(10000, 3000 + 700 * len(needs_lookup)) if needs_lookup else MAX_TOKENS
        ),
        "system": system_prompt,
    }
    if needs_lookup:
        user_content += _research_instructions(needs_lookup)
        kwargs["tools"] = [web_search_tool_for(make)]

    messages: list[dict] = [{"role": "user", "content": user_content}]
    response = None
    if needs_lookup:
        # The research search is bounded (bounded_search.run_search: aborted after
        # 120 s of silence, 5-minute budget, 4 billed requests; slow searches are
        # logged). If it stops, the ad is written without search: features are
        # named plainly and nothing is cached, so the next build researches them.
        from bounded_search import SearchUnavailable, run_search

        try:
            response = run_search(client, kwargs=kwargs, messages=messages, label=f"ad research {stock or ''}".strip(),
                                  budget_s=GENERATE_SEARCH_BUDGET_S, silence_s=GENERATE_SEARCH_SILENCE_S)
        except SearchUnavailable as exc:
            print(f"[adwriter] {stock}: web search stopped ({exc.reason}) - writing the ad without search: {exc}",
                  file=sys.stderr)
            return generate_ad(client, vehicle_data, system_prompt, needs_lookup=[], stock=stock, make=make)
    else:
        for _ in range(4):
            response = client.messages.create(messages=messages, **kwargs)
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        raise RuntimeError(
            f"Model declined to respond (category: {getattr(detail, 'category', 'unknown')})."
        )

    text = "".join(b.text for b in response.content if b.type == "text").strip()

    if response.stop_reason == "max_tokens":
        print(
            f"[adwriter] WARNING: response truncated at max_tokens for {stock} — "
            f"ad may be incomplete",
            file=sys.stderr,
        )
        # A truncated response that never reached its closing </ad> tag has no
        # complete ad to salvage — fail loudly instead of letting the fragile
        # tag/store-closer fallback in the extraction below silently ship
        # whatever partial fragment survives the reasoning filter.
        if not text.endswith("</ad>") and "<ad>" in text:
            print(
                "[adwriter] ERROR: truncated response missing </ad> tag — "
                "ad generation failed",
                file=sys.stderr,
            )
            raise ValueError(
                f"Ad generation truncated before completion for {stock}. "
                f"Increase MAX_TOKENS or reduce prompt length."
            )

    # Pull out the internal API-feedback block (appended after the ad) and drop
    # it from `text` before any ad-body extraction runs.
    feedback_block: str | None = None
    fb = _FEEDBACK_RE.search(text)
    if fb:
        feedback_block = fb.group(1).strip()
        text = (text[: fb.start()] + text[fb.end():]).strip()

    # Locate an explicit findings block, if Claude emitted one.
    m = _RESEARCH_RE.search(text)
    block_text = m.group(1) if m else ""

    # Primary extraction: explicit <ad>...</ad> tags (CRITICAL OUTPUT FORMAT in
    # SYSTEM_PROMPT). Anything outside the tags — findings block, narrated
    # research chatter, stray reasoning — is dropped unconditionally.
    tag_match = _AD_TAG_RE.search(text)
    if tag_match:
        ad_body_slice = tag_match.group(1).strip()
        if not block_text:
            block_text = text[: tag_match.start()].strip()
    else:
        # Fallback: no tags found (older prompt variant, or the model dropped
        # them). The ad is the four paragraphs ending in the store-closer.
        # Use the pure slice (guaranteed substring of `text`) to locate the
        # research preamble boundary — the reasoning-stripped ad_body below
        # may no longer be a literal substring of `text`, so it can't be used
        # with text.find().
        ad_body_slice = _slice_ad_body(text)
        if ad_body_slice == text and m:
            # store-closer not found — at least excise the explicit block
            ad_body_slice = (text[: m.start()] + text[m.end():]).strip()

        if not block_text:
            idx = text.find(ad_body_slice)
            block_text = text[:idx].strip() if idx > 0 else ""

    # The reasoning filter still runs as a safety net on the extracted
    # content, even though the tag path bypasses it as the primary extraction.
    ad_body = _strip_reasoning_sentences(ad_body_slice)

    if needs_lookup and block_text:
        try:
            _cache_research_findings(block_text, needs_lookup)
        except Exception as exc:  # noqa: BLE001 - caching must never break output
            print(f"[adwriter] feature cache write failed: {exc}", file=sys.stderr)
        try:
            _salvage_findings(
                block_text, _collect_search_urls(response), needs_lookup
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[adwriter] feature cache salvage failed: {exc}", file=sys.stderr)

    return ad_body, feedback_block


# --------------------------------------------------------------------------- #
# Branded-feature detection + feature/towing cache resolution
# --------------------------------------------------------------------------- #

# Proprietary branded feature names to research if not already cached.
_BRANDED_KEYWORDS = re.compile(
    r"\b(magic\s*(?:sky|vision|body)?|energizing|burmester|mbux|hyperscreen"
    r"|air\s*body\s*control|air\s*balance|airmatic|digital\s*light|guard\s*360"
    r"|keyless[- ]go|keyless[- ]start|pre[- ]?safe|distronic|manufaktur"
    r"|comand|parktronic|dynamic\s*select|4d\s*sound|nappa)\b",
    re.IGNORECASE,
)

# Short all-caps tokens that are NOT proprietary features.
_ALLCAPS_STOPWORDS = {
    "MERCEDES", "MERCEDES-BENZ", "BENZ", "AMG", "BMW", "AUDI", "USB", "LED",
    "USA", "NFC", "TPMS", "SUV", "GLC", "GLE", "GLS", "EQE", "EQS", "GT",
    "4MATIC", "AWD", "RWD", "FWD", "4WD", "V6", "V8", "HD", "AC", "AM", "FM",
    "CO2", "SIRIUSXM", "LTE", "PDI", "VIN", "MSRP", "CPO", "MB-TEX",
    "USB-C", "HANDS", "FREE", "ACCESS", "KEYLESS", "START",
}

_MAX_FEATURE_LOOKUPS = 10

# Paint descriptors that never name equipment. Deliberately narrow: "metallic",
# "pearl"/"pearlcoat", "tri-coat"/"tricoat", "clearcoat" as whole words.
_WB = "(?<![A-Za-z])"  # word-boundary helpers (avoid backslash escapes)
_WE = "(?![A-Za-z])"
_PAINT_NAME_RE = re.compile(
    _WB + "(metallic|pearl|pearlcoat|tri-?coat|clear-?coat)" + _WE, re.IGNORECASE
)
# A paint word alongside one of these is an equipment item, not a paint name.
_PAINT_EXCLUDE_RE = re.compile(
    _WB + "(film|protection|wheels?|trim|accents?|interior|seats?|leather|roof|"
    "mirrors?|pkg|package|badge|graphic)" + _WE,
    re.IGNORECASE,
)


def _is_paint_name(name: str) -> bool:
    return bool(_PAINT_NAME_RE.search(name)) and not _PAINT_EXCLUDE_RE.search(name)


def _looks_branded(name: str) -> bool:
    if not name:
        return False
    if _BRANDED_KEYWORDS.search(name):
        return True
    for m in re.finditer(r"\b([A-Z][A-Z0-9]{2,}(?:[ /-][A-Z][A-Z0-9]{2,})*)\b", name):
        tok = m.group(1)
        words = [w for w in re.split(r"[ /-]", tok) if w]
        if len(words) >= 2 and not all(w in _ALLCAPS_STOPWORDS for w in words):
            return True  # multi-word all-caps phrase, e.g. "DIGITAL LIGHT"
        if len(tok) >= 6 and tok not in _ALLCAPS_STOPWORDS:
            return True  # single long all-caps token, e.g. "PARKTRONIC"
    return False


def _split_ymm(year_make_model: str | None) -> tuple[int | None, str | None, str | None]:
    """'2023 Mercedes-Benz GLC 300' -> (2023, 'Mercedes-Benz', 'GLC 300')."""
    if not year_make_model:
        return None, None, None
    m = re.match(r"\s*((?:19|20)\d{2})\s+(\S+)\s*(.*)$", year_make_model)
    if not m:
        return None, None, None
    year = int(m.group(1))
    make = m.group(2)
    model = (m.group(3) or "").strip() or None
    return year, make, model


# A tire size (P/LT optional, "275/60R20", "255/45ZR19") stated in an ad must be
# printed in the sticker or the data package; the model looks sizes up on forums
# otherwise (CT23308A's "LT275/60R20", 10/5/2026).
_TIRE_SIZE_RE = re.compile(r"\b(?:P|LT)?(\d{3}/\d{2})\s?Z?R\s?(\d{2})\b", re.IGNORECASE)


def _tire_cores(text: str | None) -> set[str]:
    return {f"{a}R{b}" for a, b in _TIRE_SIZE_RE.findall(text or "")}


def unsupported_tire_sentences(ad_text: str, source_text: str) -> list[str]:
    """Sentences stating a tire size the sticker / data package doesn't print."""
    known = _tire_cores(source_text)
    return [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", ad_text or "")
        if _tire_cores(s) - known
    ]


def _drop_sentences(text: str, bad: list[str]) -> str:
    """`text` without the sentences in `bad` (whitespace-normalized match)."""
    drop = {_ws(b) for b in bad}
    paras = []
    for p in (text or "").split("\n\n"):
        units = [u for u in _split_sentences(p) if _ws(u) not in drop]
        paras.append(" ".join(u.strip() for u in units if u.strip()))
    return "\n\n".join(p for p in paras if p)


def _sticker_text_for(pkg: dict) -> str:
    v = pkg.get("vehicle") or {}
    stock = normalize_stock(pkg.get("stock_number") or v.get("stock_number") or "")
    vin = (_snapshot_vehicle(stock) if stock else {}).get("vin") or v.get("vin")
    return ((get_window_sticker(vin) or {}) if vin else {}).get("raw_text") or (pkg.get("msrp_data") or {}).get("raw_text") or ""


def fact_check_problems(text: str, pkg: dict) -> tuple[list[str], list[str]]:
    """(sentences with an unverified tow figure, sentences with a tire size not
    in the sticker / data package) — each gets one retry, then is removed."""
    tow = pkg.get("towing") or {}
    tow_bad = unverified_tow_sentences(text, tow.get("rating"))
    source = f"{_sticker_text_for(pkg)}\n{pkg.get('_data_package_text') or ''}"
    return tow_bad, unsupported_tire_sentences(text, source)


# --------------------------------------------------------------------------- #
# Data-package formatting
# --------------------------------------------------------------------------- #


def _usd(n) -> str:
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _yn(v) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return "unknown"


def _best_proof_point_line(pricing: dict) -> str:
    bp = (pricing or {}).get("best_proof_point")
    if not bp:
        return "none (no proof point has the current price below a benchmark)"
    return (
        f"{_usd(bp.get('gap'))} {bp.get('direction', 'below')} "
        f"{bp.get('label', bp.get('key', '?'))} (benchmark {_usd(bp.get('benchmark_price'))})"
    )


def _recon_included_summary(recon: dict) -> str:
    recon = recon or {}
    parts = []
    if recon.get("all_tires_replaced"):
        parts.append("all tires replaced")
    if recon.get("scheduled_service_done"):
        parts.append("scheduled A/B service completed")
    if recon.get("brake_service_done"):
        parts.append("brake pads + rotors replaced")
    for li in recon.get("line_items", []) or []:
        desc = li.get("description")
        if desc:
            parts.append(desc)
    return "; ".join(dict.fromkeys(parts)) if parts else "none"


def _carfax_highlights(cf: dict) -> str:
    if not cf:
        return "not available"
    if cf.get("error"):
        return f"ERROR — {cf['error']}"
    bits = []
    owners = cf.get("number_of_owners")
    if owners is not None:
        bits.append(f"{owners} owner{'s' if owners != 1 else ''}, {cf.get('owner_type') or 'type unknown'}")
    if cf.get("all_service_mercedes_benz"):
        bits.append("all service at authorized Mercedes-Benz dealers")
    if cf.get("low_mileage"):
        bits.append("low mileage")
    if cf.get("miles_per_year"):
        bits.append(f"{cf['miles_per_year']:,} mi/yr")
    clean = [
        label
        for key, label in (
            ("no_accidents", "no accidents"),
            ("no_structural_damage", "no structural damage"),
            ("no_total_loss", "no total loss"),
        )
        if cf.get(key)
    ]
    if clean:
        bits.append(", ".join(clean))
    ws = cf.get("warranty_status")
    if ws:
        w = f"warranty {ws}"
        if cf.get("warranty_months_remaining") or cf.get("warranty_miles_remaining"):
            w += (
                f" (~{cf.get('warranty_months_remaining') or '?'} mo / "
                f"{(cf.get('warranty_miles_remaining') or 0):,} mi remaining)"
            )
        w += f", claimable: {_yn(cf.get('warranty_claimable'))}"
        bits.append(w)
    if cf.get("titled_states"):
        bits.append("titled in " + ", ".join(cf["titled_states"]))
    return "; ".join(bits) if bits else "no notable highlights"


def format_data_package(pkg: dict) -> tuple[str, list[dict]]:
    """Render the aggregated package as a clear text block for the Claude prompt.

    Returns (text, needs_lookup). `needs_lookup` is the list of branded features
    and towing lookups that are NOT in feature_cache.db yet and should be
    web-searched during ad generation.
    """
    v = pkg.get("vehicle") or {}
    msrp = pkg.get("msrp_data") or {}
    pricing = pkg.get("pricing") or {}
    cf = pkg.get("carfax") or {}
    recon = pkg.get("recon") or {}

    year, make, model = _split_ymm(v.get("year_make_model"))
    brand = make or "Mercedes-Benz"
    trim = v.get("trim_body")
    needs_lookup: list[dict] = []

    lines: list[str] = []
    lines.append("=== VEHICLE (ACV MAX) ===")
    lines.append(f"Today's date: {date.today().isoformat()}")
    lines.append(f"Stock number: {v.get('stock_number', pkg.get('stock_number'))}  (prefix: {pkg.get('stock_prefix')})")
    lines.append(f"VIN: {v.get('vin', 'n/a')}")
    lines.append(f"Year / Make / Model: {v.get('year_make_model', 'n/a')}")
    lines.append(f"Trim / body: {v.get('trim_body', 'n/a')}")
    lines.append(f"Mileage: {(v.get('mileage') or 0):,}")
    lines.append(f"Exterior color: {v.get('exterior_color', 'n/a')}")
    if v.get("interior_color"):
        lines.append(f"Interior color: {v.get('interior_color')}")
    else:
        material = v.get("interior_seat_material")
        lines.append(
            "Interior color: UNAVAILABLE. Name no interior color. "
            + (
                f"Describe the seats by material only (sticker lists: {material})."
                if material
                else "Do not describe the interior color or seat material."
            )
        )
    lines.append(f"Certified: {_yn(v.get('certified'))}")
    lines.append(f"Status code: {v.get('status_code', 'n/a')}")
    lines.append(f"Days on lot: {v.get('days_on_lot', 'n/a')}")
    lines.append(f"ACV Max Price: {_usd(v.get('current_price'))}")
    lines.append(f"Doc Fee: {_usd(DEALER_DOC_FEE)}")
    lines.append(f"Advertised Price: {_usd(v.get('advertised_price'))} (used in ad copy)")
    pt = pkg.get("powertrain") or powertrain_for_package(pkg)
    lines.extend(data_package_lines(pt))

    lines.append("")
    lines.append(
        "STICKER_IS_PREDICTIVE: "
        + ("true" if pkg.get("sticker_is_predictive") else "false")
        + "  (true = AutoiPacket estimated build, not a manufacturer sticker)"
    )
    lines.append(
        "STICKER_PRICES_APPROXIMATE: "
        + ("true" if pkg.get("sticker_prices_approximate") else "false")
        + "  (true = equipment from ACV Max's options tab; no price in it is a factory figure)"
    )
    msrp_source = msrp.get("source")
    if msrp_source == "acvmax_options_tab":
        lines.append("=" * 48)
        lines.append("OPTION PACKAGES (from ACV Max — MSRP approximate)")
        lines.append("=" * 48)
        lines.append(f"Note: {msrp.get('msrp_note') or 'MSRP approximate — not the OEM window sticker'}")
        packages = msrp.get("selected_packages") or []
        if not packages:
            lines.append("  (no selected packages found)")
        for p in packages:
            # Prices are deliberately not shown: they are approximations, not
            # factory figures, and STICKER_PRICES_APPROXIMATE_RULE bars stating
            # them. Names and contents are still usable.
            price_txt = "price withheld: approximate, not a factory figure"
            lines.append(f"  - {p.get('code') or '?'}  {p.get('name') or ''}  ({price_txt})")
            if p.get("description"):
                lines.append(f"      {p['description']}")
        lines.append("Total MSRP: unavailable (do not state an MSRP or any package price)")
    else:
        lines.append("=== MSRP / WINDOW STICKER (AutoiPacket) ===")
        if msrp.get("error"):
            lines.append(f"UNAVAILABLE — {msrp['error']}")
        elif msrp_source == "unavailable_after_retries":
            lines.append(
                "UNAVAILABLE — AutoiPacket failed after 3 attempts; "
                "proceeding without MSRP data"
            )
        else:
            lines.append(f"Window sticker source: {msrp.get('source', 'autoipacket')}")
            lines.append(f"Base price: {_usd(msrp.get('base_price'))}")
            lines.append(f"Freight: {_usd(msrp.get('freight'))}")
            lines.append(f"Total MSRP: {_usd(msrp.get('total_msrp'))}")
            opts, standalone = dedupe_equipment_descriptors(
                msrp.get("option_packages"), msrp.get("standalone_options")
            )
            lines.append(f"Option packages ({len(opts)}):")
            lines.append(
                "Sub-items listed under each package are confirmed from the OEM "
                "window sticker. Use them verbatim — do not web search for "
                "package contents when sub_items are present. Standalone "
                "options are real equipment items but their package grouping "
                "was unclear — include them in the selling story as "
                "individual features."
            )
            for o in opts:
                name = o.get("name") or ""
                code = o.get("code") or "?"
                lines.append(f"  {name} ({code})...{_usd(o.get('price'))}")
                for si in o.get("sub_items") or []:
                    lines.append(f"    - {si.get('name') or ''}")

            if standalone:
                lines.append("")
                lines.append(
                    "STANDALONE OPTIONS (from window sticker — package "
                    "grouping unclear):"
                )
                for s in standalone:
                    code = (s.get("code") or "").ljust(5)
                    lines.append(f"  {code} {s.get('name') or ''}")

    # --- FEATURE CONTEXT: cache hits inline, cache misses -> needs_lookup ---
    # Skipped for the two no-MSRP fallback sources — there's no AutoiPacket
    # option-package data to run branded-feature lookups against.
    if not msrp.get("error") and msrp_source not in (
        "acvmax_options_tab",
        "unavailable_after_retries",
    ):
        # `opts` here is the same deduped list built above (this condition
        # exactly matches the branch that sets it).
        stds = msrp.get("standard_options") or []
        seen: set[str] = set()
        feature_context: list[str] = []
        for item in [*opts, *stds]:
            fname = re.sub(r"\s+", " ", (item.get("name") or "")).strip()
            key = fname.lower()
            if not fname or key in seen or not _looks_branded(fname):
                continue
            seen.add(key)
            if _is_paint_name(fname):
                continue  # plain color name — nothing to research
            cached = get_feature(brand, fname)
            if cached and cached.get("description"):
                feature_context.append(f"  - {fname}: {cached['description']}")
            elif len(needs_lookup) < _MAX_FEATURE_LOOKUPS:
                needs_lookup.append(
                    {"kind": "feature", "brand": brand, "feature_name": fname}
                )

        lines.append("")
        lines.append("=== FEATURE CONTEXT (pre-researched, use directly) ===")
        lines.extend(feature_context or ["  (none cached yet)"])

    # --- TOWING: towing.py's verified rating for this exact configuration ---
    lines.extend(towing_package_lines(pkg.get("towing")))

    # --- TRIM KNOWLEDGE: standard equipment + verified engine, non-MB only ---
    # Deliberately outside the branded-feature block above: it is keyed on
    # year/make/model/trim, not on sticker data, so it also applies to vehicles
    # whose MSRP came from the ACV Max options tab or is unavailable.
    if make and "mercedes" not in make.lower() and year and make and model:
        tk = get_trim_knowledge(year, make, model, trim, pt.get("class"))
        lines.append("")
        lines.append("=== TRIM KNOWLEDGE (pre-researched, use directly, do not search again) ===")
        if tk and tk.get("standard_equipment") and tk.get("engine_description"):
            lines.append(f"  Standard equipment: {tk['standard_equipment']}")
            if engine_problems(tk["engine_description"], pt.get("engine")):
                lines.append("  Engine: (cached research contradicts STICKER ENGINE — use the sticker's engine line only)")
            else:
                # Stored for reference only: engine, turbo, hybrid and cylinder wording comes
                # from ENGINE_SENTENCE / MILD_HYBRID_SENTENCE / the sticker's own words.
                lines.append("  Engine: (not for ad wording: describe the engine only through ENGINE_SENTENCE and the STICKER ENGINE line)")
        else:
            lines.append("  (none cached yet — search required, see FEATURES REQUIRING RESEARCH below)")
            needs_lookup.append({
                "kind": "trim_knowledge", "year": year, "make": make,
                "model": model, "trim": trim, "powertrain": pt.get("class"),
                "sticker_engine": (pt.get("engine") or {}).get("text"),
            })

    lines.append("")
    lines.append("=== PRICING PROOF POINTS (favorable only: advertised price BELOW benchmark) ===")
    lines.append(
        "Gaps below are already calculated against Advertised Price "
        "(ACV Max price + doc fee), not the ACV Max price alone."
    )
    lines.append("")
    lines.append("Raw favorable proof points (reference only — for transparency, not selection):")
    below = pricing.get("proof_points_below") or []
    if not below:
        lines.append("  None.")
    else:
        for p in below:
            lines.append(
                f"  - {p.get('label', p.get('key'))}: benchmark {_usd(p.get('benchmark_price'))}, "
                f"advertised price {_usd(p.get('gap'))} below"
            )

    lines.append("")
    lines.append("PROOF POINT SENTENCE (use verbatim — do not substitute a different proof point):")
    lines.append(pkg.get("proof_point_sentence") or "n/a")
    lines.append(
        "PROOF POINT TYPE: "
        + ("velocity_anchor — turn/scarcity signal, not a $1,000+ book/market gap"
           if pkg.get("proof_point_type") == "velocity_anchor" else "standard")
    )
    lines.append("")
    lines.append(
        "SCARCITY SENTENCE (use verbatim immediately after the PROOF POINT SENTENCE; "
        "if (omit), write no scarcity language):"
    )
    lines.append(pkg.get("scarcity_sentence") or "(omit)")

    lines.append("")
    lines.append("MSRP DEPRECIATION SENTENCE (include in paragraph two when present, omit if null):")
    lines.append(pkg.get("msrp_sentence") or "(omit — gap below threshold)")

    lines.append("")
    if v.get("status_code") == 13:
        lines.append(
            "WARRANTY SENTENCE (include verbatim in paragraph three, right after the services "
            "sentence, when present; omit if null):"
        )
    else:
        lines.append("WARRANTY SENTENCE (include in paragraph two when present, omit if null):")
    lines.append(pkg.get("warranty_sentence") or "(omit — no remaining factory warranty per Carfax)")
    if v.get("status_code") in (11, 12):
        lines.append("")
        lines.append(
            "FACTORY WARRANTY SENTENCE (include verbatim in paragraph three, right before the program warranty "
            "sentence, when present; omit if null — it is separate from the program warranty):"
        )
        lines.append(pkg.get("factory_warranty_sentence") or "(omit — no remaining factory warranty per Carfax)")

    lines.append("")
    lines.append("SHIPPING SENTENCE (include as final sentence of paragraph two when present, omit if null):")
    lines.append(pkg.get("shipping_sentence") or "(omit — shipping not triggered)")

    lines.append("")
    lines.append("AMG LINE DESCRIPTION (use verbatim when describing AMG Line package, omit if null):")
    lines.append(pkg.get("amg_line_description") or "(no AMG Line package on this vehicle)")

    lines.append("")
    lines.append("=== CARFAX ===")
    if cf.get("error"):
        lines.append(f"UNAVAILABLE — {cf['error']}")
    elif not cf:
        lines.append("UNAVAILABLE")
    else:
        lines.append(f"Number of owners: {cf.get('number_of_owners', 'n/a')}")
        lines.append(f"Owner type: {cf.get('owner_type', 'n/a')}")
        lines.extend(carfax_claim_lines(cf, (pkg.get("vehicle") or {}).get("year_make_model")))
        lines.append(f"Last reported odometer (Carfax): {(cf.get('last_reported_odometer') or 0):,}")
        lines.append(f"Miles per year: {(cf.get('miles_per_year') or 0):,}  (low mileage: {_yn(cf.get('low_mileage'))})")
        # Carfax's own warranty estimate and odometer cross-check are not shown:
        # the WARRANTY SENTENCE below is the only factory-warranty language.
        lines.append("Warranty: see WARRANTY SENTENCE (the only factory-warranty language for this ad)")
        lines.append(f"Titled in: {', '.join(cf.get('titled_states') or []) or 'n/a'}")

    lines.append("")
    lines.append("PROVENANCE_SENTENCE (use verbatim as paragraph one sentence two, omit if (omit)):")
    lines.append(pkg.get("provenance_sentence") or "(omit — no Carfax owner data)")

    lines.append("")
    lines.append("CARFAX SENTENCE (use verbatim in paragraph one sentence three, omit if null):")
    lines.append(pkg.get("carfax_sentence") or "(omit — no Carfax sentence)")

    lines.append("")
    lines.append("RECON_SENTENCE (use verbatim in paragraph one sentence four, omit if null):")
    lines.append(pkg.get("recon_sentence") or "(omit — no qualifying recon items)")

    lines.append("")
    lines.append(
        "RECON_FALLBACK_SENTENCE (use verbatim as the closing sentence of paragraph one, omit if null):"
    )
    lines.append(pkg.get("recon_fallback_sentence") or "null")

    lines.append("")
    lines.append(f"SELLER_COMMENTS: {pkg.get('seller_comments') or '(none)'}")

    # --- MARKET VELOCITY: days-supply / competitive-set signals from ACV Max.
    # Only rendered when the package actually carries this data — ACV Max
    # doesn't show the widget for every vehicle, and it must never be
    # fabricated (see MARKET VELOCITY DATA in SYSTEM_PROMPT).
    mv = pkg.get("market_velocity") or {}
    if any(
        mv.get(k) is not None
        for k in (
            "overall_market_days",
            "matching_market_days",
            "market_velocity_gap",
            "matching_count",
            "market_rank",
        )
    ):
        lines.append("")
        lines.append("=" * 48)
        lines.append("MARKET VELOCITY")
        lines.append("=" * 48)
        lines.append(f"Overall market days supply: {mv.get('overall_market_days', 'n/a')}")
        lines.append(f"Matching config days supply: {mv.get('matching_market_days', 'n/a')}")
        gap = mv.get("market_velocity_gap")
        if isinstance(gap, (int, float)) and gap > 0:
            lines.append(
                f"Velocity signal: This config sells {gap} days faster than market average"
            )
        lines.append(f"Market rank: {mv.get('market_rank', 'n/a')} of {mv.get('market_rank_of', 'n/a')}")

    # --- SCRAPER STATUS: cache_hit / scraped / failed per source, from
    # aggregator.aggregate()'s own vehicle_cache.db bookkeeping. Only rendered
    # when the package carries it — early-exit packages (gate failures,
    # incomplete recon) don't have it.
    ss = pkg.get("scraper_status")
    if ss:
        lines.append("")
        lines.append("=" * 48)
        lines.append("SCRAPER STATUS")
        lines.append("=" * 48)
        lines.append(f"ACV MAX: {ss.get('acvmax', 'n/a')}")
        lines.append(f"AutoiPacket: {ss.get('autoipacket', 'n/a')} (cache_hit / scraped / failed)")
        lines.append(f"ReconVision: {ss.get('reconvision', 'n/a')} (cache_hit / scraped / failed)")
        lines.append(f"Carfax: {ss.get('carfax', 'n/a')} (cache_hit / scraped / failed)")

    # --- DERIVED SIGNALS: peacock mode + calculated warranty remaining ---
    lines.append("")
    lines.append("=== DERIVED SIGNALS ===")
    hv_count = pkg.get("high_value_option_count")
    peacock = bool(pkg.get("peacock_mode"))
    lines.append(
        f"peacock_mode: {'true' if peacock else 'false'}"
        + (f"  ({hv_count} option package(s) over $1,000)" if hv_count is not None else "")
    )
    lines.append(
        "PEACOCK MODE: "
        + ("Yes" if peacock else "No")
        + (f" — {hv_count} options over $1,000 found" if hv_count is not None else "")
    )

    bwm = pkg.get("battery_warranty_remaining_months")
    bwi = pkg.get("battery_warranty_remaining_miles")
    if bwm is not None or bwi is not None:
        lines.append(f"battery_warranty_remaining_months: {bwm}")
        lines.append(f"battery_warranty_remaining_miles: {(bwi or 0):,}")
        if (bwm or 0) > 0 and (bwi or 0) > 0:
            lines.append(
                f"BATTERY WARRANTY REMAINING: {bwm} months / {bwi:,} miles"
            )

    lines.append("")
    lines.append("=== RECON (filtered to include-rule items only) ===")
    if pkg.get("recon_pending"):
        lines.append(
            "NOT YET AVAILABLE — this vehicle is still in reconditioning."
        )
        lines.append(
            "Do not reference any reconditioning, service, tire, or brake work "
            "in paragraph one, other than the RECON_FALLBACK_SENTENCE above "
            "when it is provided. Write paragraph one from provenance and "
            "Carfax only. The recon sentence will be added later."
        )
        return "\n".join(lines), needs_lookup
    lines.append(f"all_tires_replaced: {_yn(recon.get('all_tires_replaced'))}")
    lines.append(f"scheduled_service_done: {_yn(recon.get('scheduled_service_done'))}")
    lines.append(f"brake_service_done: {_yn(recon.get('brake_service_done'))}")
    li_items = recon.get("line_items") or []
    if li_items:
        lines.append("Included line items:")
        for li in li_items:
            lines.append(
                f"  - [{li.get('recon_reason', '')}] {li.get('description', '')} "
                f"(labor {li.get('labor_hours', '?')}h, parts {_usd(li.get('parts_cost'))}, "
                f"total {_usd(li.get('total_cost'))})"
            )
    else:
        lines.append("Included line items: none")

    return "\n".join(lines), needs_lookup


# --------------------------------------------------------------------------- #
# Source status / errors
# --------------------------------------------------------------------------- #


def source_status(pkg: dict) -> dict[str, tuple[str, str]]:
    """Map each scraper source -> ("ok"|"failed", detail)."""
    status: dict[str, tuple[str, str]] = {}

    recon = pkg.get("recon") or {}
    status["reconvision"] = (
        "ok",
        f"work order {recon.get('work_order_id', '?')}, "
        f"{len(recon.get('line_items') or [])} included item(s)",
    )
    v = pkg.get("vehicle") or {}
    status["acvmax_pricing"] = (
        "ok",
        f"{v.get('year_make_model', '?')} @ {_usd(v.get('current_price'))}",
    )

    cf = pkg.get("carfax")
    if not cf or cf.get("error"):
        status["carfax"] = ("failed", (cf or {}).get("error", "no carfax data returned"))
    else:
        status["carfax"] = ("ok", f"{cf.get('number_of_owners', '?')} owner(s)")

    msrp = pkg.get("msrp_data")
    if not msrp or msrp.get("error"):
        status["autoipacket"] = (
            "failed",
            (msrp or {}).get("error", "no window-sticker data returned"),
        )
    elif msrp.get("source") == "acvmax_options_tab":
        n = len(msrp.get("selected_packages") or [])
        status["autoipacket"] = (
            "ok",
            f"approximate MSRP only — ACV Max options tab fallback ({n} packages)",
        )
    elif msrp.get("source") == "unavailable_after_retries":
        status["autoipacket"] = ("ok", "no MSRP — proceeding after 3 failed AutoiPacket attempts")
    else:
        status["autoipacket"] = ("ok", f"total MSRP {_usd(msrp.get('total_msrp'))}")

    return status


# --------------------------------------------------------------------------- #
# Pipeline entry point
# --------------------------------------------------------------------------- #


# Scarcity / exclusivity wording the non-MB tiers must never contain. Python's own
# scarcity sentence (pkg["scarcity_sentence"]) is removed from the text first, so
# only wording Claude added can trip it. Word-bounded: "regional market listings"
# is fine, "in the region" is not.
_BANNED_SCARCITY_RE = re.compile(
    r"\b(?:rare|rarely|rarest|rarity|hard to find|one of the few|one of the only"
    r"|low[- ]volume|limited[- ]production|in the region)\b",
    re.IGNORECASE,
)


def find_banned_scarcity_phrases(ad_text: str, scarcity_sentence: str | None = None) -> list[str]:
    text = ad_text or ""
    if scarcity_sentence:
        text = text.replace(scarcity_sentence, " ")
    return [m.group(0).lower() for m in _BANNED_SCARCITY_RE.finditer(text)]


class BannedScarcityPhraseError(RuntimeError):
    """The ad still contained banned scarcity wording after one retry."""


# Sentences that must not survive in paragraph two: any with a banned word, plus
# the pre-count-backed velocity text ("Fewer than N comparable examples ...").
_LEGACY_SCARCITY_RE = re.compile(r"\bFewer than \d+ comparable examples are actively listed\b", re.I)


def _split_sentences(text: str) -> list[str]:
    protected = (text or "").replace("J.D.", "J\u2024D\u2024")
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z0-9"\u201c(])', protected.strip())
    return [p.replace("J\u2024D\u2024", "J.D.") for p in parts if p]


def strip_banned_sentences(text: str) -> tuple[str, list[str]]:
    """(text without every sentence that contains a banned scarcity word or the
    legacy "Fewer than N ..." velocity text, the removed sentences)."""
    kept: list[str] = []
    removed: list[str] = []
    for s in _split_sentences(text):
        (removed if (_BANNED_SCARCITY_RE.search(s) or _LEGACY_SCARCITY_RE.search(s)) else kept).append(s)
    return " ".join(kept), removed


def insert_scarcity_sentence(paragraph_two: str, sentence: str | None) -> str:
    """Put Python's scarcity sentence right after the proof-point sentence ("Current
    asking price is ..."), or at the end when there is none. No sentence, no change."""
    if not sentence:
        return paragraph_two
    parts = _split_sentences(paragraph_two)
    idx = next((i for i, s in enumerate(parts) if s.startswith("Current asking price is")), None)
    if idx is None:
        parts.append(sentence)
    else:
        parts.insert(idx + 1, sentence)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Required-sentence guard: every Python-built sentence must reach the ad verbatim
# --------------------------------------------------------------------------- #

# pkg keys of the sentences the pipeline writes itself. Paragraph one carries the
# provenance sentence (sentence two); paragraph two carries the rest.
REQUIRED_SENTENCE_KEYS = (
    "provenance_sentence",
    "carfax_sentence",
    "proof_point_sentence",
    "scarcity_sentence",
    "warranty_sentence",
    "shipping_sentence",
    "engine_sentence",
    "towing_sentence",
    "mild_hybrid_sentence",
    "factory_warranty_sentence",
)
# The factory-warranty sentence (Carfax estimate); warranty_sentence_date
# tracks only this one.
_FACTORY_WARRANTY_RE = re.compile(
    r"^(?:CARFAX estimates about \d+ months? remain on the original\b"
    r"|Original \S+(?: \S+)? (?:basic )?factory warranty has\b)",
    re.IGNORECASE,
)
# Hendrick Certified / Affordable program warranty sentence in paragraph
# three; the factory-warranty sentence goes right before it (after the
# services sentence on Hendrick Certified, the inspection sentence on
# Hendrick Affordable, which has no services sentence).
_PROGRAM_WARRANTY_RE = re.compile(r"^The Hendrick (?:Certified|Affordable) Limited Powertrain Warranty\b", re.IGNORECASE)
# Hendrick Certified / Affordable paragraph three's inspection sentence; the
# factory-warranty sentence goes right after it.
_INSPECTION_SENTENCE_RE = re.compile(r"\binspection performed by Hendrick-certified technicians before it is offered for sale\b", re.IGNORECASE)

# Paragraph-two sentence that names the engine / powertrain; MILD_HYBRID_SENTENCE
# goes right after it (or after the opening sentence when there is none).
_ENGINE_SENTENCE_RE = re.compile(
    r"\b(?:engine|powertrain|V-?(?:6|8|12)|inline[- ](?:four|six|4|6)|cylinder|\d\.\d[- ]?(?:liter|L)\b"
    r"|turbocharged|horsepower|hp\b)",
    re.IGNORECASE,
)

# The model's own engine sentence in a stored paragraph two (a displacement, or a
# horsepower / torque figure); a reprice swaps it for ENGINE_SENTENCE unless it
# also carries a towing figure (_TOW_FIGURE_RE), the only place that figure lives.
_OLD_ENGINE_SENTENCE_RE = re.compile(
    r"(?<![\d.])\d\.\d[- ]?(?:liter|litre|L)\b|\bhorsepower\b|\b\d{2,3}\s*hp\b|\blb-?ft\b", re.IGNORECASE
)
_TOW_FIGURE_RE = re.compile(r"\btow(?:s|ing)?\b[^.]*?\b\d{1,2},\d{3}\b|\b\d{1,2},\d{3}\s*(?:pounds|lbs?)\b[^.]*\btow", re.IGNORECASE)

# A price claim of the model's own: anything stating the asking price, or a dollar
# gap below a pricing benchmark. Dropped when Python's proof-point sentence has to
# be inserted, so the ad never carries two competing price statements.
_PRICE_CLAIM_RE = re.compile(
    r"\basking price\b|\$[\d,]+[^.]*?\bbelow\b.*?(?:Typical Listing Price|J\.?D\.? Power"
    r"|Kelley Blue Book|benchmark|active listings|market average)",
    re.IGNORECASE,
)
_DEPRECIATION_RE = re.compile(r"\bOriginal MSRP was\b|\bin depreciation\b", re.IGNORECASE)
# As-Is paragraph three's services sentence (both sub-categories); the
# factory-warranty sentence goes right after it.
_SERVICES_SENTENCE_RE = re.compile(r"\bservices were (?:completed|addressed) prior to delivery\b", re.IGNORECASE)
_WARRANTY_START_RE = re.compile(
    r"^(?:This vehicle carries an? .*warranty|The powertrain warranty runs through"
    r"|The High-Tech Warranty adds|Original \S+(?: \S+)? factory warranty (?:has|remains)"
    r"|CARFAX estimates about \d+ months? remain on the original)",
    re.IGNORECASE,
)
# As-Is paragraph-three lines the factory-warranty sentence replaces (a reprice
# of an older As-Is ad removes them).
_ASIS_OLD_WARRANTY_LINES_RE = re.compile(
    r"^(?:This vehicle is sold without dealer warranty or roadside assistance\."
    r"|The original manufacturer warranty remains active and transfers to the new owner\.)",
    re.IGNORECASE,
)


# Tool-call markup the model sometimes writes as plain text during the web-search
# loop (pretend <tool_call>/<tool_response> blocks with JSON payloads) instead of
# using the real server tool. The ad-body extraction cannot tell it from prose.
_TOOL_LEAK_RE = re.compile(
    r"</?\s*(?:tool_call|tool_response|tool_result|tool_use|function_calls?|function_results"
    r"|invoke|parameter|antml:[a-z_]+)\b"
    r"|\{\s*\"(?:name|query|result|results|type|input|content)\"\s*:"
    r"|\"\s*\}\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class LeakedToolOutputError(RuntimeError):
    """The generated ad still contained tool-call markup after one retry."""


def find_tool_output_leaks(text: str | None) -> list[str]:
    """Short excerpts of every tool-output fragment in `text` (empty when clean)."""
    t = text or ""
    return [t[max(0, m.start() - 20): m.end() + 20].replace("\n", " ") for m in _TOOL_LEAK_RE.finditer(t)]


def _ws(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def required_sentences_from(pkg: dict) -> dict[str, str]:
    """{key: sentence} for every Python-built sentence the package carries."""
    return {k: pkg[k].strip() for k in REQUIRED_SENTENCE_KEYS if (pkg.get(k) or "").strip()}


def missing_required_sentences(ad_text: str, required: dict[str, str]) -> list[str]:
    """Keys of the required sentences not in `ad_text` verbatim (whitespace-normalized)."""
    text = _ws(ad_text)
    return [k for k, s in required.items() if _ws(s) not in text]


def _units(paragraph: str, keep: list[str]) -> list[str]:
    """Sentences of `paragraph`, with each string in `keep` that is present kept
    whole as one unit (the shipping and velocity sentences span two sentences)."""
    spans = []
    for s in keep:
        m = re.search(r"\s+".join(map(re.escape, s.split())), paragraph)
        if m:
            spans.append((m.start(), m.end()))
    units: list[str] = []
    pos = 0
    for a, b in sorted(spans):
        if a < pos:
            continue
        units += _split_sentences(paragraph[pos:a])
        units.append(paragraph[a:b])
        pos = b
    units += _split_sentences(paragraph[pos:])
    return [u.strip() for u in units if u.strip()]


# A paragraph-one sentence that is the model's version of the CARFAX SENTENCE: a
# clean-history claim, or the miles-per-year comparison.
_CARFAX_COPY_RE = re.compile(
    r"clean (?:vehicle |carfax )?history|clean carfax|no accidents|"
    r"miles per year against the national average",
    re.IGNORECASE,
)


def insert_required_sentences(
    ad_text: str, required: dict[str, str], missing: list[str], *, status_code=None, stock: str = ""
) -> str:
    """Put each missing required sentence at its normal position, leaving the
    sentences already present where they are:
      provenance  -> paragraph one, sentence two
      proof point -> paragraph two, in place of the model's own price claim if it
                     wrote one (removed), else after the MSRP depreciation
                     sentence, else before the warranty / shipping sentences
      scarcity    -> right after the proof-point sentence
      warranty    -> before the shipping sentence (As-Is: before the depreciation
                     / proof-point sentences)
      shipping    -> last sentence of paragraph two
    Logs each insertion and each removed price claim."""
    paras = split_ad_paragraphs(ad_text)
    tag = f"[required] {stock}:" if stock else "[required]"

    if "provenance_sentence" in missing:
        units = _units(paras["paragraph_one"], [])
        units.insert(min(1, len(units)), required["provenance_sentence"])
        paras["paragraph_one"] = " ".join(units)
        print(f"{tag} inserted provenance sentence into paragraph one", file=sys.stderr)

    # CARFAX SENTENCE: paragraph one, right after the provenance sentence. The
    # model sometimes rewords it ("Carfax shows a clean vehicle history with no
    # accidents..."), which the reasoning filter then strips (T23151A, 10/7):
    # the verbatim sentence goes back in.
    if "carfax_sentence" in missing:
        prov = required.get("provenance_sentence")
        units = _units(paras["paragraph_one"], [s for s in (prov,) if s])
        # The model often writes its own near-copy ("Clean vehicle history. Averaging
        # 8,352 miles per year ..." for "Clean vehicle history, averaging ..."): those
        # sentences are dropped so the verbatim one doesn't stand beside a duplicate.
        mine = [i for i, u in enumerate(units) if _ws(u) != _ws(prov or "") and _CARFAX_COPY_RE.search(u)]
        for i in reversed(mine):
            print(f"{tag} replaced the model's own history sentence: {units[i]}", file=sys.stderr)
            del units[i]
        at = next((i + 1 for i, u in enumerate(units) if prov and _ws(u) == _ws(prov)), min(2, len(units)))
        units.insert(at, required["carfax_sentence"])
        paras["paragraph_one"] = " ".join(units)
        print(f"{tag} inserted Carfax sentence into paragraph one", file=sys.stderr)

    # Hendrick Certified / Affordable (11 / 12): the factory-warranty sentence
    # goes in paragraph three right before the program warranty sentence.
    if "factory_warranty_sentence" in missing:
        paras["paragraph_three"] = hendrick_paragraph_three_with_factory_warranty(
            paras["paragraph_three"], required["factory_warranty_sentence"]
        )
        print(f"{tag} inserted factory warranty sentence into paragraph three", file=sys.stderr)
        missing = [k for k in missing if k != "factory_warranty_sentence"]
    # As-Is (status 13): the factory-warranty sentence lives in paragraph
    # three, right after the services sentence.
    if status_code == 13 and "warranty_sentence" in missing:
        units = _units(paras["paragraph_three"], [])
        svc = next((i for i, u in enumerate(units) if _SERVICES_SENTENCE_RE.search(u)), None)
        units.insert(len(units) if svc is None else svc + 1, required["warranty_sentence"])
        paras["paragraph_three"] = " ".join(units)
        print(f"{tag} inserted warranty sentence into paragraph three", file=sys.stderr)
        missing = [k for k in missing if k != "warranty_sentence"]
    p2_keys = [k for k in ("proof_point_sentence", "scarcity_sentence", "warranty_sentence", "shipping_sentence", "engine_sentence", "towing_sentence", "mild_hybrid_sentence") if k in missing]
    if p2_keys:
        present = [s for k, s in required.items() if k not in missing and k not in ("provenance_sentence", "carfax_sentence")]
        units = _units(paras["paragraph_two"], present)
        is_req = lambda u: _ws(u) in {_ws(s) for s in required.values()}  # noqa: E731
        find = lambda key: next((i for i, u in enumerate(units) if key in required and _ws(u) == _ws(required[key])), None)  # noqa: E731

        if "proof_point_sentence" in p2_keys:
            claims = [i for i, u in enumerate(units) if not is_req(u) and _PRICE_CLAIM_RE.search(u)]
            for i in claims:
                print(f"{tag} removed the model's own price claim: {units[i]!r}", file=sys.stderr)
            at = claims[0] if claims else None
            units = [u for i, u in enumerate(units) if i not in claims]
            if at is None:
                dep = [i for i, u in enumerate(units) if _DEPRECIATION_RE.search(u)]
                tail = [i for i in (find("scarcity_sentence"), find("warranty_sentence"), find("shipping_sentence")) if i is not None]
                if dep:
                    at = dep[-1] + 1
                elif tail:
                    at = min(tail)
                else:
                    at = len(units)
            units.insert(min(at, len(units)), required["proof_point_sentence"])
        if "scarcity_sentence" in p2_keys:
            pp = find("proof_point_sentence")
            units.insert(len(units) if pp is None else pp + 1, required["scarcity_sentence"])
        if "warranty_sentence" in p2_keys:
            if status_code == 13:
                anchors = [i for i, u in enumerate(units) if _DEPRECIATION_RE.search(u)]
                pp = find("proof_point_sentence")
                at = anchors[0] if anchors else pp
            else:
                at = find("shipping_sentence")
            units.insert(len(units) if at is None else at, required["warranty_sentence"])
        if "shipping_sentence" in p2_keys:
            units.append(required["shipping_sentence"])
        if "engine_sentence" in p2_keys:
            # Right after paragraph two's opening sentence (the mild-hybrid
            # sentence, when it is also missing, then follows it).
            units.insert(min(1, len(units)), required["engine_sentence"])
        if "mild_hybrid_sentence" in p2_keys:
            # After ENGINE_SENTENCE when the ad has one, else after the model's
            # own engine / powertrain sentence.
            eng = find("engine_sentence")
            if eng is None:
                eng = next(
                    (i for i, u in enumerate(units) if not is_req(u) and _ENGINE_SENTENCE_RE.search(u)), None
                )
            units.insert((eng + 1) if eng is not None else min(1, len(units)), required["mild_hybrid_sentence"])
        if "towing_sentence" in p2_keys:
            # After the engine sentence (and the mild-hybrid sentence that
            # follows it), else after paragraph two's opening sentence.
            at = find("mild_hybrid_sentence")
            if at is None:
                at = find("engine_sentence")
            if at is None:
                at = next((i for i, u in enumerate(units) if not is_req(u) and _ENGINE_SENTENCE_RE.search(u)), None)
            units.insert((at + 1) if at is not None else min(1, len(units)), required["towing_sentence"])
        paras["paragraph_two"] = " ".join(units)
        for k in p2_keys:
            print(f"{tag} inserted {k.replace('_', ' ')} into paragraph two", file=sys.stderr)

    return "\n\n".join(p for p in paras.values() if p)


def powertrain_for(
    stock: str | None,
    vin: str | None = None,
    year_make_model: str | None = None,
    trim: str | None = None,
) -> dict:
    """Powertrain class + ELECTRIC_RANGE for one vehicle (powertrain.py),
    identified by the inventory snapshot's year/make/model/trim when the stock
    is in it, so generate, reprice and top-up share one range-cache key. Never
    raises: a failure gives class "unknown" and a flag."""
    snap = _snapshot_vehicle(normalize_stock(stock)) if stock else {}
    try:
        return vehicle_powertrain(
            snap.get("vin") or vin,
            snap.get("year_make_model") or year_make_model,
            snap.get("trim") or trim,
            body_style=snap.get("body_style"),
        )
    except Exception as exc:  # noqa: BLE001 - classification must never sink a build
        print(f"[powertrain] {stock}: classification failed — {exc}", file=sys.stderr)
        return {
            "class": UNKNOWN, "label": CLASS_LABELS[UNKNOWN], "source": "error",
            "signals": [], "override": None, "range": {},
            "flags": [f"powertrain classification failed ({exc}) — class unknown, no range stated"],
        }


def powertrain_for_package(pkg: dict) -> dict:
    v = pkg.get("vehicle") or {}
    return powertrain_for(
        pkg.get("stock_number") or v.get("stock_number"),
        v.get("vin"), v.get("year_make_model"), v.get("trim_body"),
    )


def strip_powertrain_claims(
    text: str, pt: dict, *, stock: str, label: str, protected: list[str] | None = None,
    existing: bool = False,
) -> tuple[str, list[str]]:
    """Remove every sentence breaking the powertrain rules (see powertrain.py)
    and log each one. Returns (text, [notes for the feedback block]).
    `existing` is copy written before this run (no model call to fix it): a
    sentence whose only problem is mild-hybrid wording the sticker doesn't
    print is kept and flagged rather than deleted."""
    new, removed = strip_violations(
        text, pt.get("class") or UNKNOWN, pt.get("range"), protected,
        sticker_mild=mild_wording_ok(pt), keep_mild_only=existing, engine=pt.get("engine"),
    )
    notes = []
    for sentence, problems in removed:
        kept = all(p.startswith("(kept) ") for p in problems)
        verb = "kept, needs a rewrite" if kept else "removed sentence"
        print(
            f"[{label}] {stock}: {verb} ({'; '.join(problems)}): {' '.join(sentence.split())}",
            file=sys.stderr,
        )
        what = "existing copy, kept" if kept else ("removed from existing copy" if existing else "removed after retry")
        notes.append(f"{what} ({'; '.join(problems)}): {' '.join(sentence.split())}")
    return new, notes


def _with_flags(feedback: str | None, flags: list[str]) -> str | None:
    if not flags:
        return feedback
    return ((feedback or "").rstrip() + "\n" + flags_block(flags)).strip()


def _generate_from_package(pkg: dict) -> tuple[str, str | None]:
    """(ad_copy, feedback_block) for one aggregated package. Every tier's ad is
    checked for tool-call markup leaked into the text, banned scarcity wording,
    and each Python-built sentence (REQUIRED_SENTENCE_KEYS) verbatim. Any problem
    gets one retry; after it, leaked markup raises LeakedToolOutputError and
    banned wording BannedScarcityPhraseError (callers log either and skip the
    vehicle), and a required sentence still missing is inserted at its normal
    position and logged."""
    stock = pkg.get("stock_number")
    vehicle = pkg.get("vehicle") or {}
    pt = pkg.get("powertrain") or powertrain_for_package(pkg)
    pkg["powertrain"] = pt
    if pt.get("mild_sentence"):
        pkg["mild_hybrid_sentence"] = pt["mild_sentence"]
    else:
        pkg.pop("mild_hybrid_sentence", None)
    if pt.get("engine_sentence"):
        pkg["engine_sentence"] = pt["engine_sentence"]
    else:
        pkg.pop("engine_sentence", None)
    if "towing" not in pkg:
        pkg["towing"] = towing_for_package(pkg)
    tow_sentence = (pkg["towing"] or {}).get("sentence")
    if tow_sentence:
        pkg["towing_sentence"] = tow_sentence
    else:
        pkg.pop("towing_sentence", None)
    required = required_sentences_from(pkg)
    protected = list(required.values())
    pt_flags = list(pt.get("flags") or [])
    tow = pkg["towing"] or {}
    if tow.get("triggered") and not tow.get("rating"):
        pt_flags.append(f"TOWING RATING NEEDS REVIEW — {tow.get('config_text')}: {tow.get('note')}")

    def _done(ad_copy: str, feedback: str | None) -> tuple[str, str | None]:
        return scrub_non_mb_tire_wording(
            ad_copy, vehicle.get("status_code"), vehicle.get("year_make_model"),
            stock=stock or "", label="adwriter",
        ), _with_flags(feedback, pt_flags)

    for attempt in (1, 2):
        ad_copy, feedback = _generate_once(pkg)
        leaks = find_tool_output_leaks(ad_copy)
        hits = find_banned_scarcity_phrases(ad_copy, pkg.get("scarcity_sentence"))
        missing = missing_required_sentences(ad_copy, required)
        pt_bad = check_claims(
            ad_copy, pt.get("class") or UNKNOWN, pt.get("range"), protected,
            sticker_mild=mild_wording_ok(pt), engine=pt.get("engine"),
        )
        tow_bad, tire_bad = fact_check_problems(ad_copy, pkg)
        hist_bad = history_claim_problems(ad_copy, pkg)
        if not leaks and not hits and not missing and not pt_bad and not tow_bad and not tire_bad and not hist_bad:
            return _done(ad_copy, feedback)
        problems = (
            ([f"tool output in the ad text {leaks[:2]}"] if leaks else [])
            + ([f"banned scarcity wording {sorted(set(hits))}"] if hits else [])
            + ([f"missing required sentence(s) {missing}"] if missing else [])
            + [f"powertrain: {'; '.join(p)}" for _, p in pt_bad]
            + ([f"unverified tow figure: {tow_bad}"] if tow_bad else [])
            + ([f"tire size not on the sticker or in the data package: {tire_bad}"] if tire_bad else [])
            + ([f"Carfax claim the report doesn't support: {hist_bad}"] if hist_bad else [])
        )
        if attempt == 1:
            print(f"[adwriter] {stock}: {'; '.join(problems)} - retrying once", file=sys.stderr)
            continue
        if leaks:
            print(f"[adwriter] {stock}: tool output still in the ad text after retry - skipping this vehicle", file=sys.stderr)
            raise LeakedToolOutputError(f"tool-call markup in the ad text after retry: {leaks[:2]}")
        if hits:
            print(
                f"[adwriter] {stock}: banned scarcity wording "
                f"{sorted(set(hits))} after retry - skipping this vehicle",
                file=sys.stderr,
            )
            raise BannedScarcityPhraseError(
                f"banned scarcity wording after retry: {sorted(set(hits))}"
            )
        if missing:
            print(f"[adwriter] {stock}: required sentence(s) {missing} still missing after retry - inserting", file=sys.stderr)
            ad_copy = insert_required_sentences(
                ad_copy, required, missing,
                status_code=vehicle.get("status_code"), stock=stock or "",
            )
        if pt_bad:
            ad_copy, notes = strip_powertrain_claims(
                ad_copy, pt, stock=stock or "", label="adwriter", protected=protected
            )
            pt_flags.extend(notes)
        for why, bad in (("unverified tow figure", tow_bad), ("tire size not on the sticker or in the data package", tire_bad)):
            if bad:
                ad_copy = _drop_sentences(ad_copy, bad)
                for s in bad:
                    print(f"[adwriter] {stock}: removed sentence ({why}): {s}", file=sys.stderr)
                    pt_flags.append(f"removed after retry ({why}): {s}")
        if hist_bad:
            ad_copy, notes = strip_unsupported_carfax_claims(ad_copy, pkg, stock=stock or "", label="adwriter")
            pt_flags.extend(notes)
    return _done(ad_copy, feedback)


def _generate_once(pkg: dict) -> tuple[str, str | None]:
    """(ad_copy, feedback_block) for one aggregated package."""
    formatted, needs_lookup = format_data_package(pkg)
    pkg["_data_package_text"] = formatted  # the tire-size check reads it
    v = pkg.get("vehicle") or {}
    system_prompt = _system_prompt_for(v.get("status_code"), pkg.get("stock_prefix"))
    if system_prompt == HENDRICK_AFFORDABLE_PROMPT:
        label = "HENDRICK_AFFORDABLE_PROMPT"
    elif system_prompt == HENDRICK_CERTIFIED_PROMPT:
        label = "HENDRICK_CERTIFIED_PROMPT"
    elif system_prompt == AS_IS_PROMPT:
        label = "AS_IS_PROMPT"
    elif system_prompt != SYSTEM_PROMPT:
        label = "SYSTEM_PROMPT + Z stock addendum"
    else:
        label = "SYSTEM_PROMPT"
    print(
        f"[adwriter] status_code={v.get('status_code')} prefix={pkg.get('stock_prefix')}"
        f" -> {label}"
    )
    if needs_lookup:
        print(f"[adwriter] {len(needs_lookup)} feature/towing lookup(s) need web search")
    client = anthropic.Anthropic(api_key=API_KEY)
    return generate_ad(
        client,
        formatted,
        system_prompt=system_prompt,
        needs_lookup=needs_lookup,
        stock=pkg.get("stock_number"),
        make=split_ymm(v.get("year_make_model"))[1],
    )


def run_adwriter(stock_number: str) -> str:
    """stock number -> aggregate -> format -> Claude -> finished ad copy.

    Returns the ad copy string, or a short 'recon not complete' message if the
    aggregator reports recon is still in progress.
    """
    pkg = aggregate(stock_number)

    if pkg.get("recon_complete") is False:
        return (
            f"Recon not complete for {pkg.get('stock_number', stock_number)}. "
            f"{pkg.get('note', 'Wait and retry.')}"
        )

    ad_copy, _feedback = _generate_from_package(pkg)
    return ad_copy


def run_adwriter_pre_recon(stock_number: str) -> str:
    """Pre-recon path: build and return an ad from ACV MAX + AutoiPacket + Carfax
    only, with ReconVision skipped. Paragraph one is written without any recon
    language; the vehicle is later topped up by update_recon()."""
    pkg = aggregate(stock_number, skip_recon=True)
    ad_copy, _feedback = _generate_from_package(pkg)
    return ad_copy


# --------------------------------------------------------------------------- #
# ad_history.json  —  the ad lifecycle store
# --------------------------------------------------------------------------- #
#
# {stock: {first_ad_date, last_ad_date, ad_count, last_price_at_write,
#          last_advertised_price, current_ad_text, paragraph_one,
#          paragraph_two, paragraph_three, paragraph_four, recon_included,
#          recon_pending, lifecycle_stage, last_verified,
#          verification_verdict, match_score, price_mismatch, last_feedback}}
#
# last_price_at_write   — raw ACV Max list price when the ad (or its reprice)
#   was written; drives reprice detection.
# last_advertised_price — last_price_at_write + DEALER_DOC_FEE ($899): the
#   "Current asking price is $X" figure in the ad copy. The verifier compares
#   it to the price on the live listing. Absent on entries written before it
#   existed (the verifier falls back to last_price_at_write + DEALER_DOC_FEE).
#
# last_feedback — the internal ===FEEDBACK=== block from the last ad generation
#   (CONFIDENCE / EQUIPMENT_TIER / PEACOCK_MODE / proof-point notes / FLAGS ...),
#   or null. Never posted; for human review only.
#
# lifecycle_stage:
#   "pre_recon"     — initial ad written before recon complete
#   "recon_updated" — paragraph one topped up after recon completed
#   "active"        — ad is current, nothing pending
#   "repriced"      — paragraph two rewritten after a price change
#
# verification fields (written by verifier.run_verification):
#   last_verified        — ISO date of the last hendrickcars.com check, or null
#   verification_verdict — "current" | "outdated" | "not_posted" | "not_found"
#   match_score           — 0-100 fuzzy match of stored copy vs the live VDP
#   price_mismatch        — {live_price, expected_price, checked_date} when the
#     direct price check caught a stale live price, else null
#   All four are reset to null whenever the ad text is rewritten
#   (record_ad(), reprice_ad()), since an old verdict no longer applies.
#
# absent fields (written by inventory_crawler.flag_absent_ad_history, from the
# orchestrator's crawl step; entries written before they existed just lack them):
#   absent_since  — ISO date the stock was first found missing from the crawl,
#     or null. While set, verifier._verification_due() returns False.
#   absent_reason — "sold" (crawl never saw it) | "wholesale" (seen, objective
#     no longer RETAIL) | "unmapped_status" (retail, status code not mapped),
#     or null. Both fields are cleared if the stock reappears in a crawl.

AD_HISTORY_PATH = Path(__file__).with_name("ad_history.json")


def _advertised_at_write(price: object) -> float | None:
    """last_advertised_price: the raw ACV Max list price plus DEALER_DOC_FEE —
    the "Current asking price is $X" figure the ad itself states. None when
    the list price is missing or unparseable."""
    try:
        return round(float(price) + DEALER_DOC_FEE, 2) if price is not None else None
    except (TypeError, ValueError):
        return None


def load_ad_history() -> dict:
    if not AD_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(AD_HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_ad_history(history: dict) -> None:
    AD_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")


def split_ad_paragraphs(ad_text: str) -> dict:
    """Split a four-paragraph ad on blank lines into paragraph_one .. paragraph_four.

    Fewer than four blocks -> the trailing keys are "". More than four -> blocks
    past the third are folded back into paragraph_four so nothing is lost.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (ad_text or "").strip()) if b.strip()]
    keys = ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")
    if len(blocks) > 4:
        blocks = blocks[:3] + ["\n\n".join(blocks[3:])]
    out = {k: "" for k in keys}
    for k, b in zip(keys, blocks):
        out[k] = b
    return out


def _recon_has_includeable(recon: dict) -> bool:
    """True if a filtered recon block carries at least one includeable positive
    signal (a flag or a kept line item)."""
    recon = recon or {}
    return bool(
        recon.get("all_tires_replaced")
        or recon.get("scheduled_service_done")
        or recon.get("brake_service_done")
        or recon.get("line_items")
    )


def normalize_stock(stock: str | None) -> str:
    """The one canonical form of a stock number — stripped, no leading '#',
    UPPERCASE — used as the ad_history.json key everywhere. Stock numbers are
    displayed and typed in uppercase throughout the UI; a key written in any
    other case (e.g. a lowercase stock typed into the Flask box and saved as-is)
    becomes a second, orphaned entry for the same vehicle that the orchestrator,
    verifier and reprice/recon-update paths never see."""
    return (stock or "").strip().lstrip("#").upper()


def rebuild_warranty_sentence(stock: str, pricing: dict, status: int | None) -> str | None:
    """The warranty sentence as it stands today: Carfax (cached) in-service
    date, the current odometer (fresh pricing, else the snapshot), today's
    date. Never reuses the sentence already in the ad."""
    from aggregator import build_warranty_sentence
    from vehicle_cache import get_vehicle

    snap = _snapshot_vehicle(stock)
    vin = pricing.get("vin") or snap.get("vin")
    row = get_vehicle(vin) if vin else None
    try:
        cf = json.loads(row["carfax_json"]) if row and row.get("carfax_json") else {}
    except (TypeError, ValueError):
        cf = {}
    mileage = pricing.get("mileage") if pricing.get("mileage") is not None else snap.get("mileage")
    ymm = snap.get("year_make_model") or pricing.get("year_make_model")
    return build_warranty_sentence(cf, {"mileage": mileage, "year_make_model": ymm}, status, ymm)


def rebuild_factory_warranty_sentence(stock: str, pricing: dict) -> str | None:
    """Statuses 11 / 12: today's factory-warranty sentence from the stored
    Carfax estimate and the current odometer."""
    from aggregator import factory_warranty_remaining, factory_warranty_sentence
    from vehicle_cache import get_vehicle

    snap = _snapshot_vehicle(stock)
    vin = pricing.get("vin") or snap.get("vin")
    row = get_vehicle(vin) if vin else None
    try:
        cf = json.loads(row["carfax_json"]) if row and row.get("carfax_json") else {}
    except (TypeError, ValueError):
        cf = {}
    mileage = pricing.get("mileage") if pricing.get("mileage") is not None else snap.get("mileage")
    ymm = snap.get("year_make_model") or pricing.get("year_make_model")
    return factory_warranty_sentence(factory_warranty_remaining(ymm, {"mileage": mileage}, cf))


def paragraph_three_with_factory_warranty(p3: str, sentence: str | None, anchor_re: re.Pattern) -> str:
    """Paragraph three with today's factory-warranty sentence right after the
    sentence `anchor_re` finds; any older factory-warranty sentence comes out
    first, and nothing is added when no sentence is built."""
    units = [u for u in _units(p3, []) if not _FACTORY_WARRANTY_RE.match(u)]
    if sentence:
        at = next((i for i, u in enumerate(units) if anchor_re.search(u)), None)
        units.insert((at + 1) if at is not None else min(1, len(units)), sentence)
    return " ".join(units)


def hendrick_paragraph_three_with_factory_warranty(p3: str, sentence: str | None) -> str:
    """Statuses 11 / 12: the factory-warranty sentence right before the
    program warranty sentence (after the services sentence on Hendrick
    Certified; after the inspection sentence on Hendrick Affordable). An older
    factory-warranty sentence comes out first; nothing is added when none is
    built."""
    units = [u for u in _units(p3, []) if not _FACTORY_WARRANTY_RE.match(u)]
    if sentence:
        prog = next((i for i, u in enumerate(units) if _PROGRAM_WARRANTY_RE.match(u)), None)
        if prog is None:
            after = next((i for i, u in enumerate(units) if _INSPECTION_SENTENCE_RE.search(u)), None)
            prog = (after + 1) if after is not None else min(1, len(units))
        units.insert(prog, sentence)
    return " ".join(units)


def asis_paragraph_three_with_warranty(p3: str, warranty: str | None) -> str:
    """As-Is paragraph three with today's factory-warranty sentence right
    after the services sentence: any older warranty sentence and the old
    "sold without dealer warranty" line come out; nothing is added when no
    sentence is built."""
    units = [
        u for u in _units(p3, [])
        if not _WARRANTY_START_RE.match(u) and not _ASIS_OLD_WARRANTY_LINES_RE.match(u)
    ]
    if warranty:
        svc = next((i for i, u in enumerate(units) if _SERVICES_SENTENCE_RE.search(u)), None)
        units.insert(len(units) if svc is None else svc + 1, warranty)
    return " ".join(units)


def stamp_mild_sentence(entry: dict, today: str | None = None) -> None:
    """mild_hybrid_sentence_first_date: the first day this ad carried
    MILD_HYBRID_SENTENCE (set once, never moved)."""
    if entry.get("mild_hybrid_sentence_first_date"):
        return
    if _ws(MILD_HYBRID_SENTENCE) in _ws(entry.get("current_ad_text")):
        entry["mild_hybrid_sentence_first_date"] = today or date.today().isoformat()


# Fields that describe whether the LIVE listing matches the stored ad text. Any
# change to current_ad_text makes them stale, so every writer of that text goes
# through set_current_ad_text() (or clear_verification()) and the verifier then
# sees an entry with no verdict, which it checks on every pass.
VERIFICATION_FIELDS = (
    "verification_verdict", "match_score", "last_verified", "price_mismatch",
    "identity_confirmed", "verification_note",
)


def clear_verification(entry: dict) -> None:
    """Forget the last live check (the site shows "unverified" until the next one)."""
    for f in VERIFICATION_FIELDS:
        entry[f] = None


def set_current_ad_text(entry: dict, text: str) -> bool:
    """The one way to store a new current_ad_text. If the text differs from what
    was stored, the verification fields are cleared. Returns True if it changed."""
    changed = (entry.get("current_ad_text") or "") != (text or "")
    entry["current_ad_text"] = text
    if changed:
        clear_verification(entry)
    return changed


def record_ad(
    history: dict,
    stock: str,
    *,
    ad_text: str,
    price,
    lifecycle_stage: str,
    recon_included: bool,
    recon_pending: bool,
    today: str | None = None,
    last_feedback: str | None = None,
) -> dict:
    """Write a freshly generated ad into `history` under the full schema. Bumps
    ad_count and last_ad_date; sets first_ad_date on the first write. The key
    is always normalize_stock(stock), whatever case the caller passed."""
    stock = normalize_stock(stock)
    today = today or date.today().isoformat()
    paras = split_ad_paragraphs(ad_text)
    entry = history.get(stock)
    if entry is None:
        entry = {"first_ad_date": today, "ad_count": 0}
    entry["last_ad_date"] = today
    entry["ad_count"] = int(entry.get("ad_count", 0)) + 1
    entry["last_price_at_write"] = price
    entry["last_advertised_price"] = _advertised_at_write(price)
    entry["current_ad_text"] = ad_text
    entry.update(paras)
    entry["recon_included"] = bool(recon_included)
    entry["recon_pending"] = bool(recon_pending)
    entry["lifecycle_stage"] = lifecycle_stage
    entry["last_feedback"] = last_feedback
    # a fresh ad body has not been verified against hendrickcars.com yet
    clear_verification(entry)
    # a regenerated ad starts clean: no removed-sentence tracking
    entry.pop("stale_phrases", None)
    entry.pop("generation_flag", None)
    stamp_mild_sentence(entry, today)
    # warranty_sentence_date: the day this ad's factory-warranty sentence was built.
    if any(_FACTORY_WARRANTY_RE.match(s) for s in _split_sentences(ad_text)):
        entry["warranty_sentence_date"] = today
    else:
        entry.pop("warranty_sentence_date", None)
    history[stock] = entry
    return entry


# --------------------------------------------------------------------------- #
# reprice — rewrite paragraph two only, from new pricing data
# --------------------------------------------------------------------------- #


def _paragraph(entry: dict, key: str) -> str:
    """Read a stored paragraph, falling back to a fresh split of current_ad_text
    for entries written under the old schema."""
    val = entry.get(key)
    if val:
        return val
    return split_ad_paragraphs(entry.get("current_ad_text", "")).get(key, "")


def _format_reprice_package(
    paragraph_two: str,
    advertised_price,
    proof_points_below: list,
    best_proof_point,
    proof_point_sentence: str | None = None,
    required_sentences: list[str] | None = None,
) -> str:
    lines = [
        "EXISTING PARAGRAPH TWO:",
        (paragraph_two or "").strip() or "(none on record)",
        "",
        "NEW PRICING DATA:",
        f"Advertised price: {_usd(advertised_price)}  (ACV Max price + $"
        f"{DEALER_DOC_FEE} dealer administrative fee — use this figure, not "
        f"the ACV Max price alone)",
        "Proof points where the advertised price is BELOW the benchmark "
        "(already calculated against the advertised price):",
    ]
    if not proof_points_below:
        lines.append("  none — do not use a pricing proof point")
    else:
        for p in proof_points_below:
            flag = "  [BEST — use this one]" if p.get("is_best") else ""
            lines.append(
                f"  - {p.get('label', p.get('key'))}: benchmark "
                f"{_usd(p.get('benchmark_price'))}, advertised price "
                f"{_usd(p.get('gap'))} below{flag}"
            )
    if best_proof_point:
        bp = best_proof_point
        lines += [
            "",
            f"Best proof point now: {bp.get('label', bp.get('key'))} — "
            f"{_usd(bp.get('gap'))} below benchmark {_usd(bp.get('benchmark_price'))}",
        ]
    if proof_point_sentence:
        lines += [
            "",
            "PROOF POINT SENTENCE (use verbatim in place of the existing price / "
            "proof-point sentence; do not write a price or benchmark claim of your own):",
            proof_point_sentence,
        ]
    if required_sentences:
        lines += ["", "REQUIRED SENTENCES (each must appear in the new paragraph two exactly as written):"]
        lines += [f"  - {r}" for r in required_sentences]
    lines += [
        "",
        "Rewrite paragraph two only. Keep all equipment and equipment "
        "descriptions identical. Update only dollar figures and proof-point "
        "language. Return only the new paragraph two text.",
    ]
    return "\n".join(lines)


class TruncatedGenerationError(RuntimeError):
    """A reprice response did not finish (stop_reason was not end_turn) even
    after a retry with double the cap. The old text is kept."""


REPRICE_MIN_TOKENS = 1500
_CAP_EXTRA_TOKENS = 400


def _estimate_tokens(client, text: str) -> int:
    """Token count of `text` for MODEL (free count_tokens endpoint); falls back to
    a conservative characters-per-token guess if the call fails."""
    try:
        return int(client.messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": text or " "}]
        ).input_tokens)
    except Exception:  # noqa: BLE001 - a sizing helper must never sink the call
        return int(len(text or "") / 2.5) + 1


def _output_cap(client, size_text: str, floor: int) -> int:
    """max_tokens for a paragraph rewrite: twice the existing paragraph's tokens
    plus 400, never below `floor`."""
    return max(floor, 2 * _estimate_tokens(client, size_text) + _CAP_EXTRA_TOKENS)


def flag_generation_problem(stock: str, kind: str, detail: str) -> None:
    """Record on the ad_history entry that a rewrite failed, so the Inventory
    page can show it. The ad text itself is left exactly as it was."""
    history = load_ad_history()
    entry = history.get(stock)
    if entry is None:
        return
    entry["generation_flag"] = {"kind": kind, "date": date.today().isoformat(), "detail": detail}
    save_ad_history(history)


def _capped_completion(client, *, stock: str, label: str, system: str, user: str, size_text: str, floor: int) -> str:
    """One paragraph-rewrite call with a size-based cap. A response that did not
    stop with end_turn (max_tokens, refusal, ...) is retried once with double
    the cap; if it still did not finish, the vehicle is flagged and
    TruncatedGenerationError is raised - a truncated paragraph is never returned."""
    cap = _output_cap(client, size_text, floor)
    last = None
    for attempt_cap in (cap, cap * 2):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=attempt_cap,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        last = resp.stop_reason
        if last == "end_turn":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if text:
                return text
            last = "empty"
        print(
            f"[{label}] {stock}: stop_reason={last!r} at max_tokens={attempt_cap}"
            + (" - retrying with double the cap" if attempt_cap == cap else " - giving up, keeping the old text"),
            file=sys.stderr,
        )
    flag_generation_problem(stock, f"{label}_incomplete", f"stop_reason {last!r} at max_tokens {cap * 2}")
    raise TruncatedGenerationError(
        f"{label}: response did not finish (stop_reason {last!r}) even at max_tokens {cap * 2}; old text kept, vehicle flagged"
    )


def _snapshot_vehicle(stock: str) -> dict:
    """A stock's row from last_inventory_snapshot.json, or {}."""
    try:
        data = json.loads(Path(__file__).with_name("last_inventory_snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for v in data.get("vehicles") or []:
        if str(v.get("stock_number") or "").strip().upper() == stock:
            return v
    return {}


def _snapshot_status(stock: str) -> int | None:
    """status_code for a stock from last_inventory_snapshot.json, or None."""
    return _snapshot_vehicle(stock).get("status_code")


# "manufacturer-recommended tires" is Mercedes-Benz CPO wording (status 10/16)
# and must never appear on a Hendrick Certified / Affordable / As-Is ad or on a
# non-Mercedes vehicle. Python builds tier-correct tire wording already; this
# catches the model (or an old stored ad) using the MB phrase anyway.
_MFR_RECOMMENDED_TIRES_RE = re.compile(r"\b(m)anufacturer[- ]recommended\s+(tires?)\b", re.IGNORECASE)


def _is_non_mb(status_code: int | None, year_make_model: str | None = None) -> bool:
    """True for a non-MB tier (11/12/13) or a vehicle whose make isn't Mercedes."""
    if status_code in NON_CPO_STATUS_CODES:
        return True
    m = re.match(r"\s*(?:19|20)\d{2}\s+(\S+)", year_make_model or "")
    return bool(m) and not m.group(1).lower().startswith("mercedes")


def scrub_non_mb_tire_wording(
    text: str,
    status_code: int | None,
    year_make_model: str | None = None,
    *,
    stock: str = "",
    label: str = "adwriter",
) -> str:
    """On a non-MB ad, replace "manufacturer-recommended tires" with "tires"
    (keeping a sentence-initial capital) and log it. Other text is unchanged."""
    if not text or not _is_non_mb(status_code, year_make_model):
        return text

    def _sub(m: re.Match) -> str:
        word = m.group(2)
        return word[:1].upper() + word[1:] if m.group(1) == "M" else word.lower()

    new, n = _MFR_RECOMMENDED_TIRES_RE.subn(_sub, text)
    if n:
        print(
            f"[{label}] {stock}: replaced 'manufacturer-recommended tires' with "
            f"'tires' ({n}x) - non-MB ad (status {status_code}, {year_make_model or 'make ?'})",
            file=sys.stderr,
        )
    return new


class OdometerMissingError(RuntimeError):
    """ACV Max's odometer for the car is 0 or missing: the reprice is refused
    and nothing is saved (the orchestrator lists it as an error)."""


def reprice_ad(stock_number: str, new_pricing_data: dict) -> str:
    """Rewrite paragraph two of an existing ad against fresh pricing data and
    return the reconstructed four-paragraph ad. Updates ad_history.json in place
    (current_ad_text, paragraph_two, last_price_at_write, lifecycle_stage).
    """
    stock = normalize_stock(stock_number)
    history = load_ad_history()
    entry = history.get(stock)
    if not entry:
        raise ValueError(f"No ad on record for {stock}; cannot reprice.")

    old_p2 = _paragraph(entry, "paragraph_two")
    if not old_p2:
        raise ValueError(f"No paragraph two stored for {stock}; cannot reprice.")

    pd = new_pricing_data or {}
    if "mileage" in pd and not (isinstance(pd.get("mileage"), (int, float)) and pd["mileage"] > 0):
        raise OdometerMissingError(
            f"{stock}: lot odometer is {pd.get('mileage')!r} in ACV Max (0 or missing) — reprice not saved"
        )
    # current_price is the raw ACV Max price — kept for last_price_at_write /
    # reprice detection below. advertised_price (current_price + DEALER_DOC_FEE)
    # is what goes into the ad-copy prompt; proof_points_below/best_proof_point
    # from fresh_pricing_data() are already calculated against it.
    current_price = pd.get("current_price")
    advertised_price = pd.get("advertised_price")
    status = pd.get("status_code")
    if status is None:
        status = _snapshot_status(stock)
    # Python owns scarcity wording: every sentence with a banned word (and any
    # legacy "Fewer than N ..." text) comes out before Claude sees the paragraph,
    # and Python's CURRENT scarcity sentence (or none) goes back in afterward.
    clean_p2, removed = strip_banned_sentences(old_p2)
    if removed:
        print(f"[reprice] {stock}: removed {len(removed)} scarcity sentence(s) from paragraph two")
    # Powertrain claims the paragraph should never have made (a range that is
    # not ELECTRIC_RANGE, type words that don't fit the class) come out before
    # Claude sees it: the reprice prompt keeps equipment sentences verbatim,
    # so a retry could never fix them.
    pt = powertrain_for(stock)
    clean_p2, pt_notes = strip_powertrain_claims(
        clean_p2 or old_p2, pt, stock=stock, label="reprice", existing=True
    )
    # Python-built sentences the new paragraph must carry verbatim: the fresh
    # proof-point sentence, plus the warranty and shipping sentences the old
    # paragraph already had (a reprice does not rebuild those). Scarcity is
    # inserted by Python below, never written by the model.
    from aggregator import SHIPPING_SENTENCE  # local: avoid widening the module surface

    required: dict[str, str] = {}
    if pd.get("proof_point_sentence"):
        required["proof_point_sentence"] = pd["proof_point_sentence"]
    # The warranty sentence is rebuilt from today's date and the current
    # odometer on every reprice; the old one never comes back as required
    # text. Status 13 carries it in paragraph three (handled below).
    clean_p2 = " ".join(
        u for u in _units(clean_p2 or old_p2, [SHIPPING_SENTENCE]) if not _WARRANTY_START_RE.match(u)
    )
    new_warranty = rebuild_warranty_sentence(stock, pd, status)
    if new_warranty and status != 13:
        required["warranty_sentence"] = new_warranty
    # 11 / 12: the factory sentence (paragraph three) is rebuilt too.
    new_factory = rebuild_factory_warranty_sentence(stock, pd) if status in (11, 12) else None
    if _ws(SHIPPING_SENTENCE) in _ws(old_p2):
        required["shipping_sentence"] = SHIPPING_SENTENCE
    if pt.get("mild_sentence") and _ws(pt["mild_sentence"]) in _ws(old_p2):
        required["mild_hybrid_sentence"] = pt["mild_sentence"]
    # ENGINE_SENTENCE (built from the sticker's printed engine / transmission)
    # replaces the model's own engine sentence: that one is dropped before the
    # rewrite (a sentence that also carries a towing figure is kept) and the
    # sticker-built one is required instead.
    if pt.get("engine_sentence"):
        required["engine_sentence"] = pt["engine_sentence"]
        kept, dropped = [], []
        for u in _units(clean_p2 or old_p2, [SHIPPING_SENTENCE]):
            own_engine = _ws(u) != _ws(pt["engine_sentence"]) and _OLD_ENGINE_SENTENCE_RE.search(u)
            (dropped if own_engine and not _TOW_FIGURE_RE.search(u) else kept).append(u)
        if dropped:
            print(f"[reprice] {stock}: replacing the old engine sentence(s) with ENGINE_SENTENCE: {dropped}")
            clean_p2 = " ".join(kept)
    # Tow figures and tire sizes: only a verified rating for this exact
    # configuration and sizes printed on the sticker survive (the reprice
    # "package" is the old paragraph itself, so the sticker is the source).
    tow_pkg = {"stock_number": stock, "vehicle": {}, "_data_package_text": "", "powertrain": pt}
    tow_pkg["towing"] = towing_for_package(tow_pkg)
    old_tow_bad, old_tire_bad = fact_check_problems(clean_p2 or old_p2, tow_pkg)
    tow_sentence = tow_pkg["towing"].get("sentence")
    if tow_sentence:
        # TOWING_SENTENCE (verified rating or override) is required; any other
        # sentence stating a tow figure goes, even one with the same figure,
        # so the figure is stated once, in Python's words.
        required["towing_sentence"] = tow_sentence
        old_tow_bad = list(dict.fromkeys(old_tow_bad + [
            s for s in _units(clean_p2 or old_p2, [SHIPPING_SENTENCE])
            if _ws(s) != _ws(tow_sentence) and _tow_figures_in(s)
        ]))
    if old_tow_bad or old_tire_bad:
        print(f"[reprice] {stock}: removing unverified tow / tire sentence(s) before the rewrite: {old_tow_bad + old_tire_bad}")
        clean_p2 = _drop_sentences(clean_p2 or old_p2, old_tow_bad + old_tire_bad)
        pt_notes.extend(f"removed (unverified tow figure / tire size): {s}" for s in old_tow_bad + old_tire_bad)
    data_block = _format_reprice_package(
        clean_p2 or old_p2,
        advertised_price,
        pd.get("proof_points_below") or [],
        pd.get("best_proof_point"),
        proof_point_sentence=required.get("proof_point_sentence"),
        required_sentences=[v for k, v in required.items() if k != "proof_point_sentence"],
    )

    client = anthropic.Anthropic(api_key=API_KEY)

    def _rewrite() -> str:
        return _capped_completion(
            client, stock=stock, label="reprice", system=reprice_prompt_for(status),
            user=data_block, size_text=clean_p2 or old_p2, floor=REPRICE_MIN_TOKENS,
        )

    # Sentences kept from the existing paragraph (mild-hybrid wording only)
    # come back verbatim from the rewrite; they are flagged above, not retried.
    kept_existing = [
        s for s, _ in check_claims(
            clean_p2, pt.get("class") or UNKNOWN, pt.get("range"),
            sticker_mild=mild_wording_ok(pt), engine=pt.get("engine"),
        )
    ]
    protected = list(required.values()) + kept_existing
    for attempt in (1, 2):
        new_p2 = _rewrite()
        hits = find_banned_scarcity_phrases(new_p2)
        missing = missing_required_sentences(new_p2, required)
        pt_bad = check_claims(
            new_p2, pt.get("class") or UNKNOWN, pt.get("range"), protected,
            sticker_mild=mild_wording_ok(pt), engine=pt.get("engine"),
        )
        tow_bad, tire_bad = fact_check_problems(new_p2, tow_pkg)
        if not hits and not missing and not pt_bad and not tow_bad and not tire_bad:
            break
        problems = ([f"banned scarcity wording {sorted(set(hits))}"] if hits else []) + (
            [f"missing required sentence(s) {missing}"] if missing else []
        ) + [f"powertrain: {'; '.join(p)}" for _, p in pt_bad] + (
            [f"unverified tow figure / tire size: {tow_bad + tire_bad}"] if tow_bad or tire_bad else []
        )
        if attempt == 1:
            print(f"[reprice] {stock}: {'; '.join(problems)} - retrying once", file=sys.stderr)
            continue
        if hits:
            print(f"[reprice] {stock}: banned scarcity wording {sorted(set(hits))} after retry - skipping", file=sys.stderr)
            raise BannedScarcityPhraseError(
                f"reprice: banned scarcity wording after retry: {sorted(set(hits))}"
            )
        if missing:
            print(f"[reprice] {stock}: required sentence(s) {missing} still missing after retry - inserting", file=sys.stderr)
            # the inserter works on a whole ad; give it paragraph two in the second slot
            new_p2 = split_ad_paragraphs(
                insert_required_sentences("-\n\n" + new_p2, required, missing, status_code=status, stock=stock)
            )["paragraph_two"]
        if pt_bad:
            new_p2, notes = strip_powertrain_claims(
                new_p2, pt, stock=stock, label="reprice", protected=protected
            )
            pt_notes.extend(notes)
        if tow_bad or tire_bad:
            new_p2 = _drop_sentences(new_p2, tow_bad + tire_bad)
            for s in tow_bad + tire_bad:
                print(f"[reprice] {stock}: removed sentence (unverified tow figure / tire size): {s}", file=sys.stderr)
                pt_notes.append(f"removed after retry (unverified tow figure / tire size): {s}")
    new_p2 = insert_scarcity_sentence(new_p2, pd.get("scarcity_sentence"))
    set_towing_review(entry, tow_pkg["towing"])

    # Non-MB tire wording is scrubbed from every paragraph, not just the new
    # one, so a reprice also repairs an older paragraph one.
    ymm = _snapshot_vehicle(stock).get("year_make_model")
    p1, new_p2, p3, p4 = (
        scrub_non_mb_tire_wording(p, status, ymm, stock=stock, label="reprice")
        for p in (
            _paragraph(entry, "paragraph_one"), new_p2,
            _paragraph(entry, "paragraph_three"), _paragraph(entry, "paragraph_four"),
        )
    )
    if status == 13:
        p3 = asis_paragraph_three_with_warranty(p3, new_warranty)
    elif status in (11, 12):
        p3 = hendrick_paragraph_three_with_factory_warranty(p3, new_factory)
    factory_now = new_warranty if status in (10, 16, 13) else new_factory
    if factory_now:
        entry["warranty_sentence_date"] = date.today().isoformat()
    else:
        entry.pop("warranty_sentence_date", None)
    # The paragraphs a reprice leaves alone get the same powertrain check
    # (no model call there, so offending sentences are removed and logged).
    p1, n1 = strip_powertrain_claims(p1, pt, stock=stock, label="reprice", existing=True)
    p3, n3 = strip_powertrain_claims(p3, pt, stock=stock, label="reprice", existing=True)
    p4, n4 = strip_powertrain_claims(p4, pt, stock=stock, label="reprice", existing=True)
    pt_notes.extend(n1 + n3 + n4)
    # Carfax claims the report doesn't support (clean history, all service at
    # authorized dealers): rewritten or removed in every paragraph, no model call.
    verdict = carfax_verdict_for_stock(stock)
    scrubbed = []
    for p in (p1, new_p2, p3, p4):
        new, n = strip_unsupported_carfax_claims(p, verdict, stock=stock, label="reprice") if p else (p, [])
        scrubbed.append(new)
        pt_notes.extend(n)
    p1, new_p2, p3, p4 = scrubbed
    if pt_notes or pt.get("flags"):
        entry["powertrain_flags"] = list(pt.get("flags") or []) + pt_notes
    else:
        entry.pop("powertrain_flags", None)
    full = "\n\n".join(x for x in (p1, new_p2, p3, p4) if x)

    entry["paragraph_one"] = p1
    entry["paragraph_two"] = new_p2
    entry["paragraph_three"] = p3
    entry["paragraph_four"] = p4
    set_current_ad_text(entry, full)
    entry["last_price_at_write"] = current_price
    entry["last_advertised_price"] = _advertised_at_write(current_price)
    entry["last_ad_date"] = date.today().isoformat()
    entry["lifecycle_stage"] = "repriced"
    # The live listing still shows the pre-reprice copy until it's re-posted, so
    # a verdict from before this rewrite no longer describes anything: clear it
    # (the site shows "unverified") and let the next verifier run re-check.
    clear_verification(entry)    # a reprice always re-checks, even if the words came out the same
    entry.pop("generation_flag", None)    # a completed rewrite clears an earlier failure flag
    stamp_mild_sentence(entry)
    history[stock] = entry
    save_ad_history(history)
    return full


def fresh_pricing_data(stock_number: str, *, headless: bool = True) -> dict:
    """Scrape just the ACV MAX pricing screen for one stock number and shape it
    into the minimal package reprice_ad() expects:
    {current_price, advertised_price, proof_points_below, best_proof_point,
    scarcity_sentence, proof_point_sentence}.

    current_price is the raw ACV Max price (for last_price_at_write / reprice
    detection); advertised_price is current_price + DEALER_DOC_FEE, and the
    proof points are calculated against it — see aggregator._pricing()."""
    from aggregator import _advertised_price, _pricing  # local: avoid widening the module surface
    from scraper import ACVMaxScraper

    with ACVMaxScraper(headless=headless) as ax:
        ax.login()
        pr = ax.scrape_pricing(stock_number)
    advertised = _advertised_price(pr)
    shaped = _pricing(pr.get("pricing_proof_points", []), advertised)
    shaped["current_price"] = pr.get("current_internet_price")
    shaped["advertised_price"] = advertised
    shaped["status_code"] = pr.get("status_code")
    shaped["mileage"] = pr.get("mileage")
    shaped["vin"] = pr.get("vin")
    shaped["year_make_model"] = pr.get("year_make_model")
    from aggregator import BUILD_STATUS_CODES, build_scarcity_sentence

    shaped["scarcity_sentence"] = (
        build_scarcity_sentence(pr.get("matching_count"), pr.get("search_distance"))
        if pr.get("status_code") in BUILD_STATUS_CODES
        else None
    )
    from aggregator import build_proof_point_sentence

    shaped["proof_point_sentence"], _ = build_proof_point_sentence(
        pr,
        advertised,
        current_price=pr.get("current_internet_price"),
        search_distance=pr.get("search_distance"),
    )
    return shaped


# --------------------------------------------------------------------------- #
# recon update — top up paragraph one once recon completes
# --------------------------------------------------------------------------- #


def append_recon_sentence(paragraph_one: str, recon_sentence: str) -> str:
    """`recon_sentence` appended to the end of paragraph one, unless it is
    already there (so a re-run never adds it twice)."""
    p1 = (paragraph_one or "").rstrip()
    if _ws(recon_sentence) in _ws(p1):
        return p1
    return f"{p1} {recon_sentence}" if p1 else recon_sentence


def swap_pending_recon_sentence(text: str, status_code: int | None) -> str | None:
    """Replace the tier's "currently undergoing ..." recon-pending sentence in
    `text` with the same tier's post-recon fallback sentence (the pair built in
    aggregator.build_recon_fallback_sentence). Returns the new text, or None
    when the tier is unmapped or the pending sentence isn't in `text`."""
    code = 10 if status_code == 16 else status_code
    pending = _RECON_PENDING_FALLBACK.get(code)
    done = _RECON_DONE_FALLBACK.get(code)
    if not pending or not done or pending not in (text or ""):
        return None
    return text.replace(pending, done, 1)


def strip_pending_recon_sentence(text: str, status_code: int | None = None) -> str:
    """Remove the "currently undergoing ..." recon-pending sentence (and one
    adjoining space) from `text`. With a known status_code only that tier's
    sentence is removed; with None every tier's pending sentence is tried (each
    is unique to its tier). Text without the sentence is returned unchanged."""
    code = 10 if status_code == 16 else status_code
    if code is None:
        candidates = list(_RECON_PENDING_FALLBACK.values())
    else:
        candidates = [_RECON_PENDING_FALLBACK.get(code)]
    out = text or ""
    for pending in candidates:
        if not pending or pending not in out:
            continue
        if f" {pending}" in out:
            out = out.replace(f" {pending}", "", 1)
        else:
            out = out.replace(pending, "", 1).lstrip(" ")
    return out


def _topup_powertrain_check(stock: str, entry: dict) -> None:
    """The recon top-up makes no model call, so the powertrain check here
    removes (and logs) any offending sentence from the stored paragraphs and
    the full ad text, and records the flags on the entry."""
    pt = powertrain_for(stock)
    notes: list[str] = []
    for key in ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four", "current_ad_text"):
        if entry.get(key):
            new, n = strip_powertrain_claims(
                entry[key], pt, stock=stock, label="update_recon", existing=True
            )
            if key == "current_ad_text":
                set_current_ad_text(entry, new)
            else:
                entry[key] = new
                notes.extend(n)
    if notes or pt.get("flags"):
        entry["powertrain_flags"] = list(pt.get("flags") or []) + notes
    else:
        entry.pop("powertrain_flags", None)
    stamp_mild_sentence(entry)
    _topup_carfax_check(stock, entry)


def _topup_carfax_check(stock: str, entry: dict) -> None:
    """No model call on a top-up: an unsupported clean-history / all-service
    claim is rewritten or removed (logged) in each stored paragraph, and the
    full ad text is rebuilt from them."""
    verdict = carfax_verdict_for_stock(stock)
    keys = ("paragraph_one", "paragraph_two", "paragraph_three", "paragraph_four")
    changed = False
    notes: list[str] = []
    for key in keys:
        if entry.get(key):
            new, n = strip_unsupported_carfax_claims(entry[key], verdict, stock=stock, label="update_recon")
            if new != entry[key]:
                entry[key], changed = new, True
                notes.extend(n)
    if changed:
        set_current_ad_text(entry, "\n\n".join(entry[k] for k in keys if entry.get(k)))
        entry["powertrain_flags"] = list(entry.get("powertrain_flags") or []) + notes


def update_recon(stock_number: str, status_code: int | None = None) -> str:
    """A pre-recon ad's reconditioning is now complete. Re-scrape ReconVision for
    this stock number, filter it, and:

      * a recon sentence     -> strip the "currently undergoing ..." sentence
        and append Python's recon sentence (build_recon_sentence(), the same
        one new builds use, tier wording included) to the end of paragraph one;
      * no recon sentence    -> (no includeable items, or none that make a
        sentence) swap the stale "currently undergoing ..." sentence for the
        tier's post-recon fallback sentence (text left unchanged and logged if
        the sentence isn't found or status_code is unknown).
    Either way recon_pending becomes False and lifecycle_stage "recon_updated",
    and the ad is persisted and returned. No model call.
    """
    stock = normalize_stock(stock_number)
    history = load_ad_history()
    entry = history.get(stock)
    if not entry:
        raise ValueError(f"No ad on record for {stock}; nothing to update.")
    if not entry.get("recon_pending"):
        return entry.get("current_ad_text", "")

    p1 = _paragraph(entry, "paragraph_one")
    p2 = _paragraph(entry, "paragraph_two")
    p3 = _paragraph(entry, "paragraph_three")
    p4 = _paragraph(entry, "paragraph_four")

    with ReconVisionScraper(headless=True) as rv:
        rv.login()
        recon_raw = rv.scrape_work_order(stock)
    recon_raw.pop("recon_image_bytes", None)
    snap = _snapshot_vehicle(stock)
    if status_code is None:
        status_code = snap.get("status_code")
    if status_code is None:
        print(
            f"[update_recon] {stock}: no status_code supplied — filtering with "
            f"the status-10 (MB CPO) rules"
        )
    filtered = _filter_recon(
        recon_raw.get("line_items", []),
        status_code if status_code is not None else 10,
    )
    recon_sentence = (
        build_recon_sentence(filtered, status_code, snap.get("mileage"))
        if _recon_has_includeable(filtered)
        else None
    )
    ymm = snap.get("year_make_model")

    if not recon_sentence:
        if _recon_has_includeable(filtered):
            print(f"[update_recon] {stock}: includeable recon but no recon sentence - using the post-recon fallback")
        new_p1 = swap_pending_recon_sentence(p1, status_code)
        new_full = swap_pending_recon_sentence(entry.get("current_ad_text") or "", status_code)
        if new_p1 is not None:
            entry["paragraph_one"] = scrub_non_mb_tire_wording(
                new_p1, status_code, ymm, stock=stock, label="update_recon"
            )
        if new_full is not None:
            set_current_ad_text(entry, scrub_non_mb_tire_wording(
                new_full, status_code, ymm, stock=stock, label="update_recon"
            ))
        if new_p1 is None and new_full is None:
            print(
                f"[update_recon] {stock}: no includeable recon, but the pending "
                f"sentence was not found for status_code={status_code!r} — "
                f"ad text left unchanged"
            )
        _topup_powertrain_check(stock, entry)
        entry["recon_pending"] = False
        entry["lifecycle_stage"] = "recon_updated"
        entry["last_ad_date"] = date.today().isoformat()
        history[stock] = entry
        save_ad_history(history)
        return entry.get("current_ad_text") or "\n\n".join(
            x for x in (p1, p2, p3, p4) if x
        )

    new_p1 = append_recon_sentence(strip_pending_recon_sentence(p1, status_code), recon_sentence)
    new_p1, p2, p3, p4 = (
        scrub_non_mb_tire_wording(p, status_code, ymm, stock=stock, label="update_recon")
        for p in (new_p1, p2, p3, p4)
    )
    print(f"[update_recon] {stock}: recon sentence appended to paragraph one: {recon_sentence}")

    full = "\n\n".join(x for x in (new_p1, p2, p3, p4) if x)
    entry["paragraph_one"] = new_p1
    entry["paragraph_two"] = p2
    entry["paragraph_three"] = p3
    entry["paragraph_four"] = p4
    set_current_ad_text(entry, full)
    _topup_powertrain_check(stock, entry)
    entry["recon_included"] = True
    entry["recon_pending"] = False
    entry.pop("generation_flag", None)
    entry["lifecycle_stage"] = "recon_updated"
    entry["last_ad_date"] = date.today().isoformat()
    history[stock] = entry
    save_ad_history(history)
    # The stored text: the powertrain / Carfax checks above may have edited it.
    return entry["current_ad_text"]


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465


def _send_gmail(subject: str, body: str) -> None:
    addr = getattr(credentials, "EMAIL_ADDRESS", "")
    password = getattr(credentials, "EMAIL_PASSWORD", "")
    if not addr or not password:
        raise RuntimeError(
            "EMAIL_ADDRESS / EMAIL_PASSWORD are not set in credentials.py "
            "(EMAIL_PASSWORD must be a Google App Password)."
        )
    recipient = getattr(credentials, "EMAIL_TO", "") or addr

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = recipient
    msg.set_content(body)

    # macOS python.org builds don't populate OpenSSL's default trust store, so a
    # plain ssl.create_default_context() fails to verify smtp.gmail.com. Prefer
    # `truststore` (ships with the anthropic SDK) -> macOS Keychain; then certifi;
    # then the default context.
    context = None
    try:
        import truststore

        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            context = ssl.create_default_context()

    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=context) as server:
        server.login(addr, password)
        server.send_message(msg)
    print(f"[email] sent '{subject}' to {recipient}")


def send_email(stock_number: str, data_package: dict, ad_copy: str) -> None:
    """Send the 'Ad Copy Ready' email: data summary followed by the ad copy."""
    v = data_package.get("vehicle") or {}
    msrp = data_package.get("msrp_data") or {}
    ymm = v.get("year_make_model") or "Vehicle"
    subject = f"Ad Copy Ready — {ymm} {stock_number}"

    summary = "\n".join(
        [
            "DATA SUMMARY",
            "-" * 60,
            f"Stock number:      {stock_number}  (prefix {data_package.get('stock_prefix', '?')})",
            f"VIN:               {v.get('vin', 'n/a')}",
            f"MSRP (total):      {_usd(msrp.get('total_msrp')) if not msrp.get('error') else 'unavailable'}",
            f"Current price:     {_usd(v.get('current_price'))}",
            f"Best proof point:  {_best_proof_point_line(data_package.get('pricing'))}",
            f"Recon included:    {_recon_included_summary(data_package.get('recon'))}",
            f"Carfax highlights: {_carfax_highlights(data_package.get('carfax'))}",
        ]
    )

    body = f"{summary}\n\n{'=' * 60}\nFINISHED AD COPY\n{'=' * 60}\n\n{ad_copy}\n"
    _send_gmail(subject, body)


def send_error_email(stock_number: str, data_package: dict) -> None:
    """Send the 'Ad Writer FAILED' email listing which sources failed / succeeded."""
    status = source_status(data_package)
    failed = {k: d for k, (s, d) in status.items() if s == "failed"}
    ok = {k: d for k, (s, d) in status.items() if s == "ok"}

    subject = f"Ad Writer FAILED — {stock_number}"
    lines = [
        f"Ad Writer could not complete a clean run for stock #{stock_number}.",
        "",
        "SOURCES THAT FAILED:",
    ]
    lines += [f"  - {name}: {detail}" for name, detail in failed.items()] or ["  (none)"]
    lines += ["", "SOURCES THAT SUCCEEDED:"]
    lines += [f"  - {name}: {detail}" for name, detail in ok.items()] or ["  (none)"]
    lines += [
        "",
        "No ad copy was generated because one or more data sources are missing.",
    ]
    _send_gmail(subject, "\n".join(lines))


def _print_ad(ad_copy: str) -> None:
    print("\n" + "=" * 60)
    print("FINISHED AD COPY")
    print("=" * 60 + "\n")
    print(ad_copy)


# --------------------------------------------------------------------------- #
# Daily report — one email covering every pipeline outcome
# --------------------------------------------------------------------------- #


def _lookup_vehicle_meta(stock_number: str) -> dict:
    """Light ACV MAX lookup for vehicles that never reached a full package
    (e.g. still waiting on recon). Returns year_make_model / days_on_lot /
    status_code, all best-effort."""
    from scraper import ACVMaxScraper  # local import: only needed for this path

    try:
        with ACVMaxScraper(headless=True) as ax:
            ax.login()
            pr = ax.scrape_pricing(stock_number)
        return {
            "year_make_model": pr.get("year_make_model"),
            "days_on_lot": pr.get("days_on_lot"),
            "status_code": pr.get("status_code"),
        }
    except Exception:  # noqa: BLE001 - the report should never crash on one car
        return {"year_make_model": None, "days_on_lot": None, "status_code": None}


def _classify_stock(stock_number: str) -> dict:
    """Run the pipeline for one stock number and bucket the outcome into a
    daily-report section (1-5). Never raises."""
    stock = normalize_stock(stock_number)
    print(f"[adwriter] processing {stock} ...")

    try:
        pkg = aggregate(stock)
    except Exception as exc:  # noqa: BLE001 - "Never raises": one bad car must
        # not sink the whole daily report (ScraperError, a Playwright timeout,
        # a browser crash, ...). Bucket it into the errors section.
        return {"section": 5, "stock": stock, "vehicle": {},
                "failed": {"pipeline": f"{type(exc).__name__}: {exc}"}}

    # SECTION 2 — recon still in progress (aggregate returned early).
    if pkg.get("recon_complete") is False:
        meta = _lookup_vehicle_meta(stock)
        return {"section": 2, "stock": stock, "note": pkg.get("note", ""), **meta}

    v = pkg.get("vehicle") or {}
    status = source_status(pkg)
    failed = {k: d for k, (s, d) in status.items() if s == "failed"}

    # SECTION 5 — a scraper errored; don't trust the rest / can't build the ad.
    if failed:
        return {"section": 5, "stock": stock, "vehicle": v, "failed": failed}

    sc = v.get("status_code")

    # SECTION 3 — certification not yet assigned in the system.
    if sc == 1 or sc is None:
        return {"section": 3, "stock": stock, "vehicle": v}

    # SECTION 4 — status code outside the known set; needs mapping.
    if sc not in POSTABLE_STATUS_CODES:
        return {"section": 4, "stock": stock, "vehicle": v}

    # SECTION 1 — everything checks out; generate the ad.
    try:
        ad_copy, feedback = _generate_from_package(pkg)
    except (anthropic.APIError, RuntimeError) as exc:
        return {"section": 5, "stock": stock, "vehicle": v,
                "failed": {"claude": str(exc)}}
    return {
        "section": 1, "stock": stock, "vehicle": v,
        "ad_copy": ad_copy, "feedback": feedback, "pkg": pkg,
    }


def _fmt_dol(v: dict) -> str:
    dol = v.get("days_on_lot")
    return f"{dol} days on lot" if dol not in (None, "n/a") else "days on lot unknown"


def _num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "n/a"


# ---- Email 1: "Ads Ready", grouped by lifecycle stage ------------------- #

# lifecycle_stage -> ("SECTION X — HEADING", uses_full_data_summary)
_ADS_READY_SECTIONS = [
    ("active", "SECTION A — NEW ADS (full pipeline, recon complete)", True),
    ("pre_recon", "SECTION B — PRE-RECON ADS (posted without recon, watching for completion)", True),
    ("recon_updated", "SECTION C — RECON UPDATES (recon just completed, paragraph one updated)", True),
    ("repriced", "SECTION D — REPRICED (pricing paragraph updated)", True),
]


def _hendrickcars_line(e: dict) -> str:
    """The 'HendrickCars.com: ...' line for an Ads Ready entry. The URL is
    looked up once per batch in orchestrator.py; a failed lookup is flagged
    separately from "not listed" so the two aren't confused."""
    hc_url = e.get("hendrickcars_url")
    if hc_url:
        return f"HendrickCars.com: {hc_url}"
    if e.get("hendrickcars_lookup_failed"):
        return "HendrickCars.com: (lookup failed — check the site manually)"
    return "HendrickCars.com: (not found — vehicle may not be live yet)"


def _ads_ready_full_block(e: dict, *, link: bool = True) -> list[str]:
    """Detailed per-vehicle block: data summary + finished ad copy. `link`
    False leaves out the HendrickCars.com line (a per-ad email goes out before
    the batch lookup runs)."""
    pkg = e.get("pkg") or {}
    v = e.get("vehicle") or {}
    msrp = pkg.get("msrp_data") or {}
    pricing = pkg.get("pricing") or {}
    cf = pkg.get("carfax") or {}
    recon = pkg.get("recon") or {}
    out: list[str] = []

    out.append("=" * 60)
    out.append(
        f"[{e['stock']}]  {v.get('year_make_model', 'unknown vehicle')}"
        f"  —  {_usd(v.get('current_price'))}"
    )
    out.append("=" * 60)
    if link:
        out.append(_hendrickcars_line(e))
    out.append("")
    out.append("DATA SUMMARY")
    out.append("-" * 60)
    if not pkg:
        # A recon top-up rewrites paragraph one from ReconVision alone; there
        # is no aggregated data package to summarize.
        out.append(f"  (no data package — {e.get('change_note') or 'this update did not re-aggregate the vehicle'})")
        out.extend(_tool_feedback_block(e))
        out += ["", "-" * 60, "FINISHED AD COPY", "-" * 60, e.get("ad_copy", ""), "", ""]
        return out

    out.append(f"Pricing proof point used: {_best_proof_point_line(pricing)}")

    out.append("Carfax highlights:")
    if cf.get("error"):
        out.append(f"  UNAVAILABLE — {cf['error']}")
    else:
        out.append(
            f"  Owners: {cf.get('number_of_owners', 'n/a')} "
            f"({cf.get('owner_type') or 'type unknown'})"
        )
        out.append(
            f"  Miles per year: {_num(cf.get('miles_per_year'))}"
            + ("  (low mileage)" if cf.get("low_mileage") else "")
        )
        out.append(
            "  Accident status: "
            + ("none reported to Carfax" if cf.get("no_accidents") else "see Carfax")
        )
        if cf.get("all_service_mercedes_benz"):
            svc = "all service at authorized Mercedes-Benz dealers"
        else:
            n = len(cf.get("service_facilities") or [])
            rc = cf.get("service_record_count")
            svc = f"{n} service facilit{'y' if n == 1 else 'ies'} on record"
            if rc:
                svc += f", {rc} service records"
        out.append(f"  Service history: {svc}")

    if msrp.get("error"):
        out.append("Window sticker source: unavailable")
        out.append(f"  {msrp['error']}")
    elif not msrp:
        out.append("Window sticker source: unavailable")
    else:
        out.append(f"Window sticker source: {msrp.get('source', 'autoipacket')}")
        out.append(f"  Total MSRP: {_usd(msrp.get('total_msrp'))}")
        opts = msrp.get("option_packages") or []
        out.append(f"  Option packages captured ({len(opts)}):")
        if not opts:
            out.append("    (none)")
        for o in opts:
            out.append(f"    - {o.get('name') or ''} ... {_usd(o.get('price'))}")

    if pkg.get("recon_pending"):
        out.append("Recon items included in copy: none — recon still pending")
        out.append("  paragraph one will be topped up once recon completes")
    else:
        inc = recon.get("line_items") or []
        flags = [
            label
            for key, label in (
                ("all_tires_replaced", "all tires replaced"),
                ("scheduled_service_done", "scheduled A/B service completed"),
                ("brake_service_done", "brake pads + rotors replaced"),
            )
            if recon.get(key)
        ]
        out.append("Recon items included in copy:")
        if flags or inc:
            for f in flags:
                out.append(f"  - {f}")
            for li in inc:
                out.append(
                    f"  - {li.get('description', '')} ({li.get('recon_reason', '')})"
                )
        else:
            out.append("  none")

        exc = recon.get("excluded_line_items") or []
        out.append("Recon items excluded from copy:")
        if exc:
            for li in exc:
                out.append(f"  - {li.get('description', '')} ({li.get('reason', '')})")
        else:
            out.append("  none")

    out.extend(_tool_feedback_block(e))

    out.append("")
    out.append("-" * 60)
    out.append("FINISHED AD COPY")
    out.append("-" * 60)
    out.append(e.get("ad_copy", ""))
    out.append("")
    out.append("")
    return out


def _tool_feedback_block(e: dict) -> list[str]:
    """The model's internal ===FEEDBACK=== block, for the email only — never
    part of the posted ad."""
    fb = (e.get("feedback") or "").strip()
    out = ["", "TOOL FEEDBACK (internal — not posted)", "-" * 60]
    out.extend(fb.splitlines() if fb else ["  (no feedback block returned)"])
    return out


def _ads_ready_compact_block(e: dict, *, link: bool = True) -> list[str]:
    """Compact per-vehicle block for updates (reprices): stock / vehicle /
    price, a short DATA SUMMARY (the change made and, with `pricing`, the proof
    point used), TOOL FEEDBACK, and the finished ad copy."""
    v = e.get("vehicle") or {}
    out: list[str] = []
    out.append("=" * 60)
    out.append(
        f"[{e['stock']}]  {v.get('year_make_model', 'unknown vehicle')}"
        f"  —  {_usd(v.get('current_price'))}"
    )
    out.append("=" * 60)
    if link:
        out.append(_hendrickcars_line(e))
    out.append("")
    out.append("DATA SUMMARY")
    out.append("-" * 60)
    out.append(f"Change made: {e.get('change_note', 'ad updated')}")
    if e.get("pricing"):
        out.append(f"Pricing proof point used: {_best_proof_point_line(e['pricing'])}")
    out.extend(_tool_feedback_block(e))
    out.append("")
    out.append("-" * 60)
    out.append("FINISHED AD COPY")
    out.append("-" * 60)
    out.append(e.get("ad_copy", ""))
    out.append("")
    out.append("")
    return out


def format_per_ad_email(e: dict) -> str:
    """Body of one per-ad email (new, pre-recon, recon update or reprice): the
    change made, then the same per-vehicle block the Ads Ready email builds —
    DATA SUMMARY, TOOL FEEDBACK and the finished ad (_ads_ready_full_block), or
    for a reprice the compact block (_ads_ready_compact_block). No
    HendrickCars.com line: it is looked up later, for the Ads Ready email."""
    render = _ads_ready_compact_block if e.get("lifecycle_stage") == "repriced" else _ads_ready_full_block
    return "\n".join([e.get("change_note") or "Ad updated.", ""] + render(e, link=False))


def _format_ads_ready_email(entries: list[dict]) -> str:
    groups: dict[str, list[dict]] = {stage: [] for stage, _, _ in _ADS_READY_SECTIONS}
    for e in entries:
        stage = e.get("lifecycle_stage") or "active"
        groups[stage if stage in groups else "active"].append(e)

    label_for = {
        "active": "new",
        "pre_recon": "pre-recon",
        "recon_updated": "recon update(s)",
        "repriced": "repriced",
    }
    summary_bits = [
        f"{len(groups[stage])} {label_for[stage]}"
        for stage, _, _ in _ADS_READY_SECTIONS
        if groups.get(stage)
    ]
    summary = ", ".join(summary_bits) or f"{len(entries)} vehicle(s)"

    out: list[str] = [
        f"MERCEDES-BENZ OF DURHAM — ADS READY  {date.today().isoformat()}",
        f"{summary}. Finished ad copy below, ready to post.",
        "",
    ]

    for stage, heading, full in _ADS_READY_SECTIONS:
        rows = groups.get(stage) or []
        out.append(heading + f"  ({len(rows)})")
        out.append("#" * 60)
        if not rows:
            out.append("(none)")
            out.append("")
            out.append("")
            continue
        render = _ads_ready_full_block if full else _ads_ready_compact_block
        for e in rows:
            out.extend(render(e))

    return "\n".join(out)


# ---- Email 2: "Action Required" (Sections 2-5) ------------------------- #


def _format_action_required_email(
    buckets: dict[int, list[dict]],
    pre_recon_watching: list[dict] | None = None,
) -> str:
    out: list[str] = [
        f"MERCEDES-BENZ OF DURHAM — ACTION REQUIRED  {date.today().isoformat()}",
        "",
    ]

    def section(title: str, rows: list[dict], render) -> None:
        out.append(title)
        out.append("=" * 60)
        if not rows:
            out.append("(none)")
        for e in rows:
            out.extend(render(e))
        out.append("")
        out.append("")

    def s2(e: dict) -> list[str]:
        return [
            f"  [{e['stock']}]  {e.get('year_make_model') or 'unknown vehicle'}"
            f"  —  {_fmt_dol(e)}",
            f"      reason: {e.get('note') or 'recon still in progress'}",
        ]

    def s3(e: dict) -> list[str]:
        v = e.get("vehicle") or {}
        sc = v.get("status_code")
        return [
            f"  [{e['stock']}]  {v.get('year_make_model') or 'unknown vehicle'}"
            f"  —  {_fmt_dol(v)}",
            f"      reason: certification not yet assigned in the system "
            f"(status code: {'none' if sc is None else sc})",
        ]

    def s4(e: dict) -> list[str]:
        v = e.get("vehicle") or {}
        return [
            f"  [{e['stock']}]  {v.get('year_make_model') or 'unknown vehicle'}"
            f"  —  {_fmt_dol(v)}",
            f"      reason: unknown status code {v.get('status_code')} — "
            f"needs mapping (not in 1 / 10 / 11 / 12 / 16)",
        ]

    def s5(e: dict) -> list[str]:
        v = e.get("vehicle") or {}
        fails = "; ".join(
            f"{src} — {detail}" for src, detail in (e.get("failed") or {}).items()
        )
        return [
            f"  [{e['stock']}]  {v.get('year_make_model') or 'unknown vehicle'}"
            f"  —  {_fmt_dol(v)}",
            f"      reason: FAILED: {fails}",
        ]

    def s_watch(e: dict) -> list[str]:
        days = e.get("days_since_initial_ad")
        age = f"{days} day{'s' if days != 1 else ''} since initial ad" if days is not None else "age unknown"
        return [
            f"  [{e['stock']}]  {e.get('year_make_model') or 'unknown vehicle'}"
            f"  —  {age}",
            "      reason: pre-recon ad is live; recon not yet complete "
            "(paragraph one will be topped up when it is)",
        ]

    section("SECTION 2 — WAITING ON RECON", buckets[2], s2)
    section("SECTION 3 — NEEDS CERTIFICATION ASSIGNED", buckets[3], s3)
    section("SECTION 4 — UNKNOWN STATUS CODE", buckets[4], s4)
    section("SECTION 5 — ERRORS", buckets[5], s5)
    section(
        "SECTION — PRE-RECON WATCHING", list(pre_recon_watching or []), s_watch
    )
    return "\n".join(out)


def run_daily_report(
    stock_numbers: list[str], *, send: bool = True
) -> dict[str, str | None]:
    """Run the pipeline for every stock number, classify each outcome, and build
    the two emails: "Ads Ready" (Section 1) and "Action Required" (Sections 2-5).

    Sends Email 1 only if there is >= 1 finished ad, and Email 2 only if there is
    >= 1 vehicle in Sections 2-5. Never sends an empty email. Returns
    {"ads_ready": body|None, "action_required": body|None}.
    """
    buckets: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: [], 5: []}
    for stock in stock_numbers:
        entry = _classify_stock(stock)
        buckets[entry["section"]].append(entry)

    today = date.today().isoformat()
    result: dict[str, str | None] = {"ads_ready": None, "action_required": None}

    def _safe_send(subject: str, body: str) -> None:
        if not send:
            return
        try:
            _send_gmail(subject, body)
        except Exception as exc:  # noqa: BLE001
            print(f"[email] could not send '{subject}': {exc}", file=sys.stderr)

    if buckets[1]:
        result["ads_ready"] = _format_ads_ready_email(buckets[1])
        if EMAIL_ADS_READY:
            _safe_send(f"Mercedes-Benz of Durham — Ads Ready {today}", result["ads_ready"])
        else:
            print("[email] Ads Ready is off (email_config.EMAIL_ADS_READY) - not sent")

    if any(buckets[s] for s in (2, 3, 4, 5)):
        result["action_required"] = _format_action_required_email(buckets)
        _safe_send(
            f"Mercedes-Benz of Durham — Action Required {today}",
            result["action_required"],
        )

    if not result["ads_ready"] and not result["action_required"]:
        print("[adwriter] nothing to report — no email sent.")
    return result


def _run_paste_mode() -> int:
    vehicle_data = read_vehicle_data()
    if not vehicle_data:
        print("No vehicle data provided. Nothing to do.", file=sys.stderr)
        return 1

    client = anthropic.Anthropic(api_key=API_KEY)
    try:
        ad_copy, feedback = generate_ad(client, vehicle_data)
    except anthropic.AuthenticationError:
        print("Authentication failed — check API_KEY.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("Rate limited. Wait a bit and try again.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"API error ({e.status_code}): {e.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("Network error — check your connection.", file=sys.stderr)
        return 1

    _print_ad(ad_copy)
    if feedback:
        print("\n" + "-" * 60)
        print("TOOL FEEDBACK (internal — not part of posted copy)")
        print("-" * 60)
        print(feedback)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the ad pipeline for one or more stock numbers. Sends an "
            "'Ads Ready' email for finished ads and an 'Action Required' email "
            "for everything that needs attention."
        )
    )
    parser.add_argument(
        "stock",
        nargs="*",
        help="one or more dealership stock numbers. Omit to read raw data from stdin.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="build and print the reports but do not send any email",
    )
    args = parser.parse_args(argv)

    if args.stock:
        result = run_daily_report(args.stock, send=not args.no_email)
        for name, body in result.items():
            if body:
                print(f"\n{'#' * 70}\n#  {name.upper().replace('_', ' ')} EMAIL\n{'#' * 70}\n")
                print(body)
        return 0
    return _run_paste_mode()


if __name__ == "__main__":
    raise SystemExit(main())
