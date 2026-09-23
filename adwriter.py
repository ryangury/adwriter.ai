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
from aggregator import DEALER_DOC_FEE, _filter_recon, aggregate, dedupe_equipment_descriptors
from feature_cache import (
    get_feature,
    get_towing,
    get_trim_knowledge,
    save_feature,
    save_towing,
    save_trim_knowledge,
)
from recon_update_prompt import RECON_UPDATE_SYSTEM_PROMPT
from reprice_prompt import REPRICE_SYSTEM_PROMPT
from scraper import ReconVisionScraper, ScraperError
from shared_prompt_constants import (
    API_FEEDBACK_BLOCK,
    PACKAGE_CONTENT_VERIFICATION_RULE,
    RECON_FALLBACK_RULE,
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
POSTABLE_STATUS_CODES = {10, 11, 12, 13, 16}

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

TOWING CAPACITY RULE: Any vehicle with a trailer hitch in the option packages must state the rated towing capacity as a specific number in the ad. Never use generic language like increased towing capacity alone. Always pair it with the actual number. Example: rated for 7,700 lbs of towing capacity with the factory trailer hitch installed.

FEATURE CONTEXT section in the data package contains pre-researched descriptions for features already in the cache. Use these descriptions directly without searching again.

---

PARAGRAPH ONE — CERTIFICATION AND PROVENANCE

This paragraph establishes trust and specifics. Use the pre-built sentences from the data package:

- Sentence 1: no pre-built sentence exists for this one — build it yourself. Lead with Mercedes-Benz Certified Pre-Owned status, and include year, model, mileage, exterior color, interior color, and VIN.
- Sentence 2: PROVENANCE SENTENCE — use verbatim. Do not derive provenance from raw data. Do not decode the stock number.
- Sentence 3: CARFAX SENTENCE — use verbatim, omit if null. Do not derive accident history or service history from raw Carfax fields.
- Sentence 4: RECON SENTENCE — use verbatim, omit if null. Do not interpret raw recon line items yourself.

{RECON_FALLBACK_RULE}

---

PARAGRAPH TWO — THE SELLING STORY (VARIABLE)

This is the only paragraph that changes meaningfully from vehicle to vehicle. Build the selling story around whatever is most compelling about this specific unit. Priority order for what to lead with:

1. Powertrain story — if the vehicle is a PHEV (plug-in hybrid) like a GLC 350e or GLE 350e, lead with the electric range and real-world fuel cost benefit. Always state the EPA electric-only range in miles as a specific number.
2. Equipment and packages — if the vehicle is heavily optioned, lead with the most desirable packages. Name them specifically. Reference original MSRP vs current price when the MSRP DEPRECIATION SENTENCE is present in the data package — see MSRP DEPRECIATION below.

PACKAGE PRICING: State the original price of a named package or option when it was $750 or more at time of sale and the price is present in the OPTION PACKAGES data (not the MSRP APPROXIMATE fallback — see that rule separately). Format: "The [Package Name] adds [contents] at $[price]." Skip stating a price for individual options under $750, but always name the feature regardless of price. This does not override the MSRP DEPRECIATION rule — package prices and the overall MSRP depreciation sentence are separate, both can appear in the same ad.

{PACKAGE_CONTENT_VERIFICATION_RULE}
3. Rarity or combination story — if this color, trim, and equipment combination is rare in the CPO market, say so using this exact phrase: "buyers who want this specific combination rarely find it in the certified pre-owned market." Never make broader exclusivity claims you cannot support.
4. Pricing proof point — see PRICING below.

PRICING: Use the PROOF POINT SENTENCE from the data package verbatim. Do not recalculate or substitute.

Never list the same feature or package content twice in paragraph two, even if it appears in multiple places in the source data. If an item was already named inside a package description, do not list it again in the additional equipment sentence.

MSRP UNAVAILABLE RULE: When the data package shows MSRP as unavailable with no package data at all, omit the MSRP anchor sentence entirely. Do not estimate or fabricate an MSRP. Lead with equipment and proof point instead.

MSRP APPROXIMATE RULE: When msrp_note indicates approximate pricing, mention package names and contents but do not state specific dollar amounts for packages — the prices are approximate and may not reflect the original window sticker. State original MSRP is unavailable for this vehicle.

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

Velocity and scarcity narrative (comparable units turning over faster than the overall market, or a thin comparable set) is already built into the PROOF POINT SENTENCE when PROOF POINT TYPE is velocity_anchor — see PRICING above. Use it verbatim like any other proof point sentence. Do not construct your own velocity or scarcity sentence from the raw matching_market_days / overall_market_days / matching_count fields, and do not apply your own day-count or scarcity thresholds — those decisions are pre-made by build_proof_point_sentence() in the data pipeline.

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
- PHEV electric range (the value proposition requires explanation)
- Any package where the name does not describe the contents

TIER 2 — Name with brief context. Buyers mostly understand these but a short clause adds meaningful value:
- Panorama Sunroof — mention if it spans both rows
- Burmester — one descriptive word is enough (premium, reference-grade)
- Surround View Camera — "360-degree camera coverage" adds clarity
- Towing capacity — state the specific number, that IS the explanation
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

The GLE 350 is Mercedes-Benz's best-selling SUV and this unit is built around a color and equipment combination that stands apart from the typical inventory in this segment. Twilight Blue Metallic over Bahia Brown and Black interior is a pairing that most GLE buyers never consider and immediately appreciate in person. The AMG Line Exterior Package at $3,150 adds full AMG body styling, AMG-specific exterior treatment, and sport brake system with Mercedes-Benz lettering — giving this GLE a visual presence that the standard model does not have. Buyers who want this specific look rarely find it available in the certified pre-owned market. Additional equipment includes Panorama Sunroof, 21" AMG Multispoke Wheels, Trailer Hitch with Increased Towing Capacity, Surround View Camera, Power Driver and Passenger Seats with Memory, Rear Side Airbags, Brushed Aluminum Trim, Winter Package, Heated Steering Wheel, and Power Folding Mirrors. Original MSRP was $71,550. Current asking price is $47,615 Certified Pre-Owned (includes $899 dealer administrative fee), $3,910 below J.D. Power and $1,075 below typical listing price on comparable units.

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
    carrying a bare first-person "I " (ad copy is always third person — see
    the ABSOLUTE RULE against first person in every SYSTEM_PROMPT variant,
    and note this checks for a literal "I " substring, not a \\bI\\b word
    boundary: the fixed store-closer paragraph's "I-40" would otherwise
    false-positive, since a hyphen also counts as a word boundary), and any
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

        first_person_count = sum(1 for s in sentences if "I " in s)
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
            if "I " in s:
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


def _research_instructions(needs_lookup: list[dict]) -> str:
    items = []
    for n in needs_lookup:
        if n.get("kind") == "towing":
            items.append(
                f"TOWING rating for {n.get('year')} {n.get('make')} {n.get('model')}"
                f" {n.get('trim') or ''}".strip()
            )
        elif n.get("kind") == "trim_knowledge":
            items.append(
                f"STANDARD EQUIPMENT AND ENGINE SPECS for {n.get('year')} "
                f"{n.get('make')} {n.get('model')} {n.get('trim') or ''}".strip()
            )
        else:
            items.append(n.get("feature_name", ""))
    listed = "; ".join(x for x in items if x)
    return (
        f"\n\nFEATURES REQUIRING RESEARCH: {listed}\n"
        "For each item above: search for it, then write a 1-2 sentence plain "
        "English buyer-facing description of what it does and why a buyer would "
        "want it. Use those descriptions when you write the ad. (A TOWING or "
        "STANDARD EQUIPMENT AND ENGINE SPECS item is answered with its own "
        "line format below instead of a description.)\n\n"
        "When research is done you MUST output the findings block below BEFORE "
        "the ad. This block is the ONLY text allowed before paragraph one — do "
        "not write any sentence, preamble, or status note ('Now I have "
        "everything needed', 'The towing capacity is confirmed', etc.) before or "
        "instead of it. A program parses this block: the exact delimiter lines "
        "and the ' :: ' separators are mandatory. Do not paraphrase it into "
        "prose and do not skip it.\n\n"
        "===RESEARCH===\n"
        "<feature name> :: <1-2 sentence description> :: <source URL>\n"
        "(one such line per feature above; then, only if a TOWING item is "
        "listed above, exactly one line:)\n"
        "TOWING :: <rated pounds, digits only> :: <required package name or none> :: <source URL>\n"
        "(and, only if a STANDARD EQUIPMENT AND ENGINE SPECS item is listed "
        "above, exactly one line:)\n"
        "TRIM :: <comma-separated list of standard equipment on this trim> :: "
        "<engine description: configuration, cylinder count, displacement, "
        "horsepower, torque — verified via search, not memory> :: <source URL>\n"
        "===END RESEARCH===\n\n"
        "Then one blank line, then the ad starting at paragraph one. Do not "
        "repeat or mention the findings block inside the ad."
    )


def _cache_research_findings(block_text: str, needs_lookup: list[dict]) -> None:
    features = [n for n in needs_lookup if n.get("kind") == "feature"]
    towings = [n for n in needs_lookup if n.get("kind") == "towing"]
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
                    equip, engine_desc, url,
                )
            continue
        if parts[0].upper() == "TOWING":
            rating = None
            m = re.search(r"[\d,]+", parts[1])
            if m:
                try:
                    rating = int(m.group(0).replace(",", ""))
                except ValueError:
                    rating = None
            pkg_name = parts[2] if len(parts) > 2 and parts[2].lower() not in ("none", "n/a", "") else None
            url = parts[3] if len(parts) > 3 else None
            tw = towings[0] if towings else None
            if tw and rating and tw.get("year") and tw.get("make") and tw.get("model"):
                save_towing(
                    tw["year"], tw["make"], tw["model"], rating,
                    trim=tw.get("trim"),
                    package_required=bool(pkg_name),
                    package_name=pkg_name,
                    source_url=url,
                )
            continue

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
    parse left behind; save_feature/save_towing are upserts so re-writing is safe.
    """
    if not region:
        return
    url = search_urls[0] if search_urls else None

    towings = [n for n in needs_lookup if n.get("kind") == "towing"]
    if towings:
        tw = towings[0]
        mt = re.search(r"([\d,]{4,})\s*(?:lbs|lb\.?|pounds)", region, re.IGNORECASE)
        if mt and tw.get("year") and tw.get("make") and tw.get("model"):
            try:
                rating = int(mt.group(1).replace(",", ""))
            except ValueError:
                rating = None
            if rating:
                mp = re.search(
                    r"with (?:the )?(?:optional |available )?"
                    r"([A-Z][\w &/+-]*?(?:package|hitch|trailer[\w ]*))",
                    region,
                    re.IGNORECASE,
                )
                pkg_name = mp.group(1).strip() if mp else None
                save_towing(
                    tw["year"], tw["make"], tw["model"], rating,
                    trim=tw.get("trim"),
                    package_required=bool(pkg_name),
                    package_name=pkg_name,
                    source_url=url,
                )

    # Trim knowledge: an engine description pulled from prose. Partial by
    # design (no equipment list), so it still reads as a cache miss next time
    # and gets a full lookup — but a verified engine spec is worth keeping.
    # Never overwrites an engine description the structured TRIM line just saved.
    trims = [n for n in needs_lookup if n.get("kind") == "trim_knowledge"]
    if trims:
        tk = trims[0]
        if tk.get("year") and tk.get("make") and tk.get("model"):
            existing = get_trim_knowledge(tk["year"], tk["make"], tk["model"], tk.get("trim"))
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
                        None, engine_desc, url,
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
        "max_tokens": 3000 if needs_lookup else MAX_TOKENS,
        "system": system_prompt,
    }
    if needs_lookup:
        user_content += _research_instructions(needs_lookup)
        kwargs["tools"] = [WEB_SEARCH_TOOL]

    messages: list[dict] = [{"role": "user", "content": user_content}]
    response = None
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


def _looks_towing(name: str) -> bool:
    return bool(re.search(r"trailer hitch|\btowing\b|\btow\b", name or "", re.IGNORECASE))


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
    lines.append(f"Interior color: {v.get('interior_color', 'n/a')}")
    lines.append(f"Certified: {_yn(v.get('certified'))}")
    lines.append(f"Status code: {v.get('status_code', 'n/a')}")
    lines.append(f"Days on lot: {v.get('days_on_lot', 'n/a')}")
    lines.append(f"ACV Max Price: {_usd(v.get('current_price'))}")
    lines.append(f"Doc Fee: {_usd(DEALER_DOC_FEE)}")
    lines.append(f"Advertised Price: {_usd(v.get('advertised_price'))} (used in ad copy)")

    lines.append("")
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
            price = p.get("approx_msrp")
            price_txt = f"approx. {_usd(price)}" if price is not None else "approx. price unavailable"
            lines.append(f"  - {p.get('code') or '?'}  {p.get('name') or ''}  ({price_txt})")
            if p.get("description"):
                lines.append(f"      {p['description']}")
        total = msrp.get("total_msrp")
        if total is not None:
            lines.append(
                f"Approximate total MSRP: {_usd(total)}  (sum of matched "
                f"package prices only — likely incomplete, not a real OEM MSRP)"
            )
        else:
            lines.append("Approximate total MSRP: unavailable — no package prices matched")
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

        # --- TOWING: cache hit -> inline number, miss -> needs_lookup ---
        if any(_looks_towing(o.get("name") or "") for o in opts):
            tw = get_towing(year, make, model, trim) if year and make and model else None
            lines.append("")
            if tw and tw.get("tow_rating_lbs"):
                pkg_txt = (
                    f" (with {tw['package_name']})"
                    if tw.get("package_required") and tw.get("package_name")
                    else ""
                )
                lines.append(
                    f"TOWING CAPACITY: {tw['tow_rating_lbs']:,} lbs{pkg_txt}"
                )
            else:
                lines.append(
                    "TOWING CAPACITY: not cached — research required "
                    "(vehicle has a trailer hitch)"
                )
                needs_lookup.append(
                    {
                        "kind": "towing",
                        "year": year,
                        "make": make,
                        "model": model,
                        "trim": trim,
                    }
                )

    # --- TRIM KNOWLEDGE: standard equipment + verified engine, non-MB only ---
    # Deliberately outside the branded-feature block above: it is keyed on
    # year/make/model/trim, not on sticker data, so it also applies to vehicles
    # whose MSRP came from the ACV Max options tab or is unavailable.
    if make and "mercedes" not in make.lower() and year and make and model:
        tk = get_trim_knowledge(year, make, model, trim)
        lines.append("")
        lines.append("=== TRIM KNOWLEDGE (pre-researched, use directly, do not search again) ===")
        if tk and tk.get("standard_equipment") and tk.get("engine_description"):
            lines.append(f"  Standard equipment: {tk['standard_equipment']}")
            lines.append(f"  Engine: {tk['engine_description']}")
        else:
            lines.append("  (none cached yet — search required, see FEATURES REQUIRING RESEARCH below)")
            needs_lookup.append({
                "kind": "trim_knowledge", "year": year, "make": make,
                "model": model, "trim": trim,
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
    lines.append("MSRP DEPRECIATION SENTENCE (include in paragraph two when present, omit if null):")
    lines.append(pkg.get("msrp_sentence") or "(omit — gap below threshold)")

    lines.append("")
    lines.append("WARRANTY SENTENCE (include in paragraph two when present, omit if null):")
    lines.append(pkg.get("warranty_sentence") or "(omit — warranty not claimable or not applicable)")

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
        lines.append(f"All service at authorized Mercedes-Benz dealers: {_yn(cf.get('all_service_mercedes_benz'))}")
        lines.append(f"Last reported odometer (Carfax): {(cf.get('last_reported_odometer') or 0):,}")
        lines.append(f"Miles per year: {(cf.get('miles_per_year') or 0):,}  (low mileage: {_yn(cf.get('low_mileage'))})")
        lines.append(
            f"No accidents: {_yn(cf.get('no_accidents'))} | "
            f"No structural damage: {_yn(cf.get('no_structural_damage'))} | "
            f"No total loss: {_yn(cf.get('no_total_loss'))}"
        )
        lines.append(
            f"Warranty: {cf.get('warranty_status', 'unknown')} "
            f"(est. {cf.get('warranty_months_remaining', '?')} months / "
            f"{(cf.get('warranty_miles_remaining') or 0):,} miles remaining)"
        )
        lines.append(
            f"Warranty claimable: {_yn(cf.get('warranty_claimable'))}  "
            f"[odometer cross-check: {cf.get('warranty_current', 'unknown')}"
            + (f"; {cf['warranty_note']}" if cf.get("warranty_note") else "")
            + "]"
        )
        lines.append(f"Titled in: {', '.join(cf.get('titled_states') or []) or 'n/a'}")

    lines.append("")
    lines.append("PROVENANCE SENTENCE (use verbatim in paragraph one sentence two):")
    lines.append(pkg.get("provenance_sentence") or "n/a")

    lines.append("")
    lines.append("CARFAX SENTENCE (use verbatim in paragraph one sentence three, omit if null):")
    lines.append(pkg.get("carfax_sentence") or "(omit — no Carfax sentence)")

    lines.append("")
    lines.append("RECON SENTENCE (use verbatim in paragraph one sentence four, omit if null):")
    lines.append(pkg.get("recon_sentence") or "(omit — no qualifying recon items)")

    lines.append("")
    lines.append(
        "RECON_FALLBACK_SENTENCE (use verbatim as the closing sentence of paragraph one, omit if null):"
    )
    lines.append(pkg.get("recon_fallback_sentence") or "null")

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
        lines.append(
            f"Matching units in market: {mv.get('matching_count', 'n/a')} "
            f"(within {mv.get('search_distance', 'n/a')} miles)"
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


def _generate_from_package(pkg: dict) -> tuple[str, str | None]:
    """(ad_copy, feedback_block) for one aggregated package."""
    formatted, needs_lookup = format_data_package(pkg)
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
#          current_ad_text, paragraph_one, paragraph_two, paragraph_three,
#          paragraph_four, recon_included, recon_pending, lifecycle_stage,
#          last_verified, verification_verdict, match_score, last_feedback}}
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
#   match_score          — 0-100 fuzzy match of stored copy vs the live VDP

AD_HISTORY_PATH = Path(__file__).with_name("ad_history.json")


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
    entry["current_ad_text"] = ad_text
    entry.update(paras)
    entry["recon_included"] = bool(recon_included)
    entry["recon_pending"] = bool(recon_pending)
    entry["lifecycle_stage"] = lifecycle_stage
    entry["last_feedback"] = last_feedback
    # a fresh ad body has not been verified against hendrickcars.com yet
    entry["last_verified"] = None
    entry["verification_verdict"] = None
    entry["match_score"] = None
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
    paragraph_two: str, advertised_price, proof_points_below: list, best_proof_point
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
    lines += [
        "",
        "Rewrite paragraph two only. Keep all equipment and equipment "
        "descriptions identical. Update only dollar figures and proof-point "
        "language. Return only the new paragraph two text.",
    ]
    return "\n".join(lines)


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
    # current_price is the raw ACV Max price — kept for last_price_at_write /
    # reprice detection below. advertised_price (current_price + DEALER_DOC_FEE)
    # is what goes into the ad-copy prompt; proof_points_below/best_proof_point
    # from fresh_pricing_data() are already calculated against it.
    current_price = pd.get("current_price")
    advertised_price = pd.get("advertised_price")
    data_block = _format_reprice_package(
        old_p2,
        advertised_price,
        pd.get("proof_points_below") or [],
        pd.get("best_proof_point"),
    )

    client = anthropic.Anthropic(api_key=API_KEY)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=REPRICE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": data_block}],
    )
    new_p2 = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not new_p2:
        new_p2 = old_p2

    p1 = _paragraph(entry, "paragraph_one")
    p3 = _paragraph(entry, "paragraph_three")
    p4 = _paragraph(entry, "paragraph_four")
    full = "\n\n".join(x for x in (p1, new_p2, p3, p4) if x)

    entry["paragraph_two"] = new_p2
    entry["current_ad_text"] = full
    entry["last_price_at_write"] = current_price
    entry["last_ad_date"] = date.today().isoformat()
    entry["lifecycle_stage"] = "repriced"
    history[stock] = entry
    save_ad_history(history)
    return full


def fresh_pricing_data(stock_number: str, *, headless: bool = True) -> dict:
    """Scrape just the ACV MAX pricing screen for one stock number and shape it
    into the minimal package reprice_ad() expects:
    {current_price, advertised_price, proof_points_below, best_proof_point}.

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
    return shaped


# --------------------------------------------------------------------------- #
# recon update — top up paragraph one once recon completes
# --------------------------------------------------------------------------- #


def _format_recon_update_package(paragraph_one: str, filtered_recon: dict) -> str:
    lines = [
        "EXISTING PARAGRAPH ONE:",
        (paragraph_one or "").strip() or "(none on record)",
        "",
        "NEW RECON LINE ITEMS (already filtered to includeable positive signals):",
        f"all_tires_replaced: {_yn(filtered_recon.get('all_tires_replaced'))}",
        f"scheduled_service_done: {_yn(filtered_recon.get('scheduled_service_done'))}",
        f"brake_service_done: {_yn(filtered_recon.get('brake_service_done'))}",
    ]
    items = filtered_recon.get("line_items") or []
    if items:
        lines.append("Included line items:")
        for li in items:
            lines.append(
                f"  - [{li.get('recon_reason', '')}] {li.get('description', '')}"
            )
    else:
        lines.append("Included line items: none")
    lines += [
        "",
        "Add one or two sentences to the END of paragraph one describing this "
        "recon work. Keep every existing sentence unchanged. Return only the "
        "updated paragraph one text.",
    ]
    return "\n".join(lines)


def update_recon(stock_number: str) -> str:
    """A pre-recon ad's reconditioning is now complete. Re-scrape ReconVision for
    this stock number, filter it, and:

      * no includeable items -> mark recon_pending False, lifecycle_stage
        "recon_updated", return the existing ad unchanged;
      * includeable items    -> ask Claude to top up paragraph one, reconstruct
        the ad, persist it, and return it.
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

    from aggregator import _apply_recon_vision  # local: avoid widening the module surface

    with ReconVisionScraper(headless=True) as rv:
        rv.login()
        recon_raw = rv.scrape_work_order(stock)
    recon_raw.pop("recon_image_bytes", None)
    recon_raw = _apply_recon_vision(recon_raw, recon_raw.get("vin"))
    filtered = _filter_recon(recon_raw.get("line_items", []))

    if not _recon_has_includeable(filtered):
        entry["recon_pending"] = False
        entry["lifecycle_stage"] = "recon_updated"
        entry["last_ad_date"] = date.today().isoformat()
        history[stock] = entry
        save_ad_history(history)
        return entry.get("current_ad_text") or "\n\n".join(
            x for x in (p1, p2, p3, p4) if x
        )

    data_block = _format_recon_update_package(p1, filtered)
    client = anthropic.Anthropic(api_key=API_KEY)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=RECON_UPDATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": data_block}],
    )
    new_p1 = "".join(b.text for b in resp.content if b.type == "text").strip() or p1

    full = "\n\n".join(x for x in (new_p1, p2, p3, p4) if x)
    entry["paragraph_one"] = new_p1
    entry["current_ad_text"] = full
    entry["recon_included"] = True
    entry["recon_pending"] = False
    entry["lifecycle_stage"] = "recon_updated"
    entry["last_ad_date"] = date.today().isoformat()
    history[stock] = entry
    save_ad_history(history)
    return full


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


def _ads_ready_full_block(e: dict) -> list[str]:
    """Detailed per-vehicle block: data summary + finished ad copy."""
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
    out.append(_hendrickcars_line(e))
    out.append("")
    out.append("DATA SUMMARY")
    out.append("-" * 60)

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


def _ads_ready_compact_block(e: dict) -> list[str]:
    """Compact per-vehicle block for updates: stock / vehicle / price / the
    specific change made + the finished ad copy."""
    v = e.get("vehicle") or {}
    out: list[str] = []
    out.append("=" * 60)
    out.append(
        f"[{e['stock']}]  {v.get('year_make_model', 'unknown vehicle')}"
        f"  —  {_usd(v.get('current_price'))}"
    )
    out.append("=" * 60)
    out.append(_hendrickcars_line(e))
    out.append(f"Change made: {e.get('change_note', 'ad updated')}")
    if e.get("feedback"):
        out.extend(_tool_feedback_block(e))
    out.append("")
    out.append("-" * 60)
    out.append("FINISHED AD COPY")
    out.append("-" * 60)
    out.append(e.get("ad_copy", ""))
    out.append("")
    out.append("")
    return out


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
        _safe_send(f"Mercedes-Benz of Durham — Ads Ready {today}", result["ads_ready"])

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
