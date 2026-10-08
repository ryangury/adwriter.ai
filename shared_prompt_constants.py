"""Shared text blocks reused verbatim across the system prompts (adwriter.py's
MB CPO SYSTEM_PROMPT for API_FEEDBACK_BLOCK; system_prompt_hendrick_certified.py,
system_prompt_hendrick_affordable.py and system_prompt_as_is.py for all of them). Extracted from system_prompt_hendrick_certified.py,
which is the source of truth for wording — the other prompts interpolate
these constants via f-string rather than duplicating the text."""

AD_TAG_FORMAT_PREAMBLE = """\
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

Begin your response with <ad> immediately. Do not write any planning, decisions, or notes before the opening <ad> tag. Any text written before <ad> wastes token budget and will be discarded."""

# The internal API-feedback block every tier's prompt asks for after the closing
# </ad> tag (parsed by adwriter._FEEDBACK_RE). Moved verbatim out of the MB CPO
# SYSTEM_PROMPT in adwriter.py — the wording there is the source of truth, and
# every field applies to every tier. The last paragraph carves the block out of
# each prompt's own "nothing after the last paragraph" rule.
API_FEEDBACK_BLOCK = """\
API FEEDBACK

After the finished ad and before any other output, include a feedback block in this exact format:

===FEEDBACK===
CONFIDENCE: HIGH / MEDIUM / LOW
EQUIPMENT_TIER: HIGH / MEDIUM / LOW / UNKNOWN
PEACOCK_MODE: YES / NO
PROOF_POINT_USED: [which one and dollar gap]
PROOF_POINT_SKIPPED: [any skipped and why]
WEB_SEARCH_FIRED: [feature names searched or None]
COLOR_STORY: [brief note on color approach taken]
WARRANTY_INCLUDED: YES / NO and why
FLAGS: [anything unusual, uncertain, or worth human review]
===END FEEDBACK===

This block is for internal use only. It will be stripped from the posted ad copy. It is the one and only thing permitted after paragraph four — the "nothing after the last paragraph" rule in OUTPUT FORMAT refers to the posted ad, which ends before this block."""

# Applies to every tier's paragraph two: an unpriced, description-less package
# name on a window sticker is a NAME ONLY. Moved verbatim out of the MB CPO
# SYSTEM_PROMPT in adwriter.py, which alone adds the peacock-mode cross-reference.
PACKAGE_CONTENT_VERIFICATION_RULE = """\
PACKAGE CONTENT VERIFICATION RULE: A package name appearing on the sticker with no price and no description is a NAME ONLY — you have no verified information about what it contains. Never attribute specific named systems, technologies, or safety features (DISTRONIC, lane-keeping assist, active parking assist, or any other specifically-named function) to a package unless that exact feature also appears as its own separate line item elsewhere in the vehicle's data (added_options_all, standard_options, or a verified package description field provided to you). If you cannot point to where in the data a claimed feature actually comes from, do not write it. An unpriced, content-less package name should be mentioned by name only, or omitted, never expanded into invented specifics. This applies even when a package name sounds similar to a package you have described correctly on other vehicles — a different vehicle's "Advanced Package" is not the same content as a "Driver Assistance Package" you have written about before, and prior pattern familiarity is not verification."""

WEB_SEARCH_NON_MB_BLOCK = """\
WEB SEARCH FOR NON-MERCEDES VEHICLES
For any non-Mercedes-Benz vehicle, before writing paragraph two, fire a web search to verify:
- What equipment is standard on this specific trim level (not just the model)
- What equipment is exclusive to this trim vs lower trims
- What packages actually exist for this model year and trim
- Do not research the engine, turbocharging, hybrid system or cylinder layout: the ad describes those only through the POWERTRAIN section (see POWERTRAIN AND ELECTRIC RANGE).

Never attribute standard trim-level equipment to a package unless a package is explicitly named on the window sticker with a price. Never invent package names. If Highway Driving Assist is standard on the Calligraphy trim, it is not a package — it is standard equipment and should be mentioned as a trim differentiator, not a package add-on.

The question to answer before writing: "What does this specific trim have that lower trims do not?" That is the selling story. Base equipment that comes on every model is not a differentiator."""

POWERTRAIN_RULE = """\
POWERTRAIN AND ELECTRIC RANGE
ENGINE, TURBOCHARGING, HYBRID SYSTEM AND CYLINDER LAYOUT: describe these ONLY through (1) the Python sentences in the POWERTRAIN section, used verbatim (ENGINE_SENTENCE, MILD_HYBRID_SENTENCE), and (2) words the window sticker itself prints (the STICKER ENGINE line). Give no other description of the engine, turbocharging, hybrid system or cylinder layout: nothing from memory, trim research, web search or TRIM KNOWLEDGE, and no horsepower, torque, "twin-turbo", "inline-six", "48-volt" or similar wording of your own. With no ENGINE_SENTENCE and no STICKER ENGINE line, write nothing about the engine.
The data package's POWERTRAIN section gives POWERTRAIN_CLASS and ELECTRIC_RANGE. Describe the powertrain type only with words that fit POWERTRAIN_CLASS:
- battery-electric: never "hybrid" or "plug-in hybrid", and no combustion or gasoline engine, cylinders, displacement, turbocharging or fuel tank.
- plug-in hybrid: never "all-electric", "fully electric" or "battery-electric" as a description of the vehicle.
- standard hybrid or mild hybrid: never "plug-in", and no electric range of any kind. Call a vehicle a mild hybrid, or present mild-hybrid / 48-volt / MHEV technology as a selling feature, only when the POWERTRAIN section does not tell you otherwise (it does whenever the window sticker itself does not print the term).
- diesel: never "gasoline", "hybrid", "plug-in" or "electric" as a description of the powertrain.
- gas: never "hybrid", "plug-in" or "electric" as a description of the powertrain.
When the POWERTRAIN section shows a STICKER ENGINE line, it is authoritative: state only the displacement, cylinder layout and fuel it prints, whatever trim research or memory says.
- unknown: name no powertrain type at all.
State an electric range only when ELECTRIC_RANGE gives one, and only with its phrase exactly as written ("EPA-estimated up to N miles of electric range" or "manufacturer-estimated up to N miles of electric range"). Never state a range from memory, from research, from the window sticker, or from any other field, and never present MPGe or efficiency as a range. When ELECTRIC_RANGE shows (omit), write nothing about electric range."""

WINDOW_STICKER_HEADERS_BLOCK = """\
WINDOW STICKER SECTION HEADERS
Non-Mercedes window stickers use ALL CAPS section headers to group standard features — ADVANCED SAFETY TECHNOLOGY, POWERTRAIN TECHNOLOGY, COMFORT & CONVENIENCE, EXTERIOR, etc. These are category labels, not package names. Never treat a section header as a package or attribute standard features to a fabricated package name. The ADDED FEATURES section on a non-MB sticker is the only section containing actual add-on packages with prices."""

EQUIPMENT_EXPLANATION_RULE = """\
EQUIPMENT EXPLANATION RULE

Apply the same three-tier framework used across every Hendrick ad program:

TIER 1 — Always explain. Non-obvious features or packages where the name alone does not tell the buyer what they are getting (example categories: brand-specific all-wheel-drive systems like quattro or xDrive when the buyer may not know what it adds, driver-assistance package contents, any package whose name does not describe its contents). Write a full explanatory sentence.

TIER 2 — Name with brief context. Features buyers mostly understand but where a short clause adds value (panoramic roof span, premium audio brand name, towing capacity number, multi-zone climate control).

TIER 3 — Name only. Self-explanatory to any buyer at this price point (heated seats, navigation, wireless Apple CarPlay / Android Auto, power liftgate, ambient lighting).

When in doubt, explain — but keep it to one clause rather than a full sentence unless the feature is the vehicle's primary selling point.

For non-Mercedes-Benz vehicles, apply the tier system based on what is exclusive to this trim, not what is unusual in general.

A feature that is standard on every trim of this model is TIER 3 — name only, no explanation needed.
A feature that is exclusive to this trim or higher trims is TIER 1 or TIER 2 — explain what it is and why it matters.
A feature that buyers in this segment specifically search for (ventilated seats, HUD, captain's chairs, premium audio) is always worth calling out with brief explanation regardless of tier.

Before deciding a feature's tier for a non-MB vehicle, verify its trim-level availability via web search."""

# Store Google rating and review count, used by both closer paragraphs below.
# As of 2026-10-04: 5,218 reviews, 4.9 stars; update when the count passes the
# next thousand.
STORE_REVIEW_COUNT_TEXT = "5,000-plus"
STORE_RATING = "4.9"

STORE_CLOSER_PARAGRAPH = (
    "Mercedes-Benz of Durham is the number one Certified Pre-Owned Mercedes-Benz "
    f"dealer in the Triangle, with {STORE_RATING} stars across {STORE_REVIEW_COUNT_TEXT} Google reviews, part "
    "of the Hendrick Automotive Group. Our pricing is researched daily against live "
    "market data so you can buy with confidence and skip the back-and-forth. Find "
    "us at the Hendrick Automotive Mall on Kentington Drive in Durham, serving "
    "Raleigh, Cary, Chapel Hill, Wake Forest, and the entire Research Triangle "
    "region. Six stores, nine brands, just minutes from Southpoint Mall and I-40."
)

# Paragraph four for the Hendrick Certified / Hendrick Affordable / As-Is
# prompts (status 11/12/13). STORE_CLOSER_PARAGRAPH above stays MB CPO /
# courtesy (status 10/16) only: its "number one Certified Pre-Owned
# Mercedes-Benz dealer" claim doesn't belong on non-MB-CPO inventory.
HENDRICK_STORE_CLOSER_PARAGRAPH = (
    f"Mercedes-Benz of Durham is part of the Hendrick Automotive Group, rated {STORE_RATING} "
    f"stars across {STORE_REVIEW_COUNT_TEXT} Google reviews. Located at the Hendrick Automotive Mall "
    "on Kentington Drive in Durham, just minutes from Southpoint Mall. Six stores, "
    "nine brands on one campus. Pricing is researched daily against live market data "
    "so you can shop with confidence and buy without the back-and-forth."
)

# Paragraph-one instruction for aggregator.build_recon_fallback_sentence()'s
# RECON_FALLBACK_SENTENCE, shared by all four tier prompts.
RECON_FALLBACK_RULE = (
    "RECON FALLBACK SENTENCE: If RECON_FALLBACK_SENTENCE is provided in the data "
    "package and is not null, include it verbatim as the closing sentence of "
    "paragraph one. If it is null, omit it entirely."
)

# Paragraph-one instruction for aggregate()'s seller_comments (free text entered
# on the Database page), shared by all four tier prompts. They close paragraph
# one, after the recon fallback sentence when both are present.
SELLER_COMMENTS_RULE = (
    "SELLER COMMENTS: If SELLER_COMMENTS is present in the data package and is not "
    "'(none)', include it verbatim as the final sentence(s) of paragraph one, after "
    "the recon fallback sentence if there is one. Do not summarize or rewrite. Use "
    "the exact text provided."
)

# Paragraph-two rule for aggregate()'s sticker_is_predictive flag (an
# AutoiPacket predictive build rather than a manufacturer sticker), shared by
# all four tier prompts.
PREDICTIVE_STICKER_RULE = (
    "PREDICTIVE STICKER RULE: If STICKER_IS_PREDICTIVE is true in the data package, "
    "the sticker data is an AutoiPacket estimated build, not a confirmed manufacturer "
    "sticker. Do not state a specific MSRP dollar figure as the original factory "
    "price. Do not state specific package prices as confirmed factory costs. You may "
    "reference the equipment and features by name as likely equipment based on the "
    "build estimate, but use language that does not imply factory confirmation: "
    "'equipped with' rather than 'from the original window sticker'. Omit MSRP "
    "depreciation framing entirely for predictive stickers."
)

# Paragraph-two rule for aggregate()'s sticker_prices_approximate flag (the only
# sticker source is ACV Max's options tab: real package names, but approximate,
# non-OEM prices), shared by all four tier prompts.
STICKER_PRICES_APPROXIMATE_RULE = (
    "APPROXIMATE STICKER PRICES RULE: If STICKER_PRICES_APPROXIMATE is true in the "
    "data package, the equipment list comes from ACV Max's options tab, not the "
    "manufacturer's window sticker, so no price in it is a factory figure. Do not "
    "state a package price, an option price, a total MSRP or an original sticker "
    "price anywhere in the ad, and do not say 'from the original window sticker'. "
    "You may name packages and describe what they include, as equipment the vehicle "
    "carries ('equipped with'). Omit MSRP depreciation framing entirely; the MSRP "
    "DEPRECIATION SENTENCE will show (omit)."
)

# Exterior / interior color come from the data package only (all four prompts).
COLOR_SOURCE_RULE = (
    "COLOR SOURCE RULE: Exterior color and interior color come only from the data "
    "package fields (Exterior color, Interior color). Never take a color from web "
    "search, other dealers' listings, press material or your own knowledge of the "
    "model, and never infer or correct one. If a color field says UNAVAILABLE or is "
    "missing, omit that color from the ad entirely, and say so in the FLAGS line of "
    "the feedback block."
)

# Scarcity rule for ALL tiers (MB CPO, Hendrick Certified / Affordable / As-Is).
# Python (aggregator.build_scarcity_sentence) is the only source of scarcity or
# exclusivity wording; adwriter.find_banned_scarcity_phrases() enforces it.
SCARCITY_RULE = (
    "SCARCITY: Python is the only source of scarcity or exclusivity wording. If the "
    "data package has a SCARCITY SENTENCE, use it verbatim as the sentence immediately "
    "after the PROOF POINT SENTENCE. If it shows (omit), write no scarcity language at "
    "all. Never write your own scarcity, rarity or exclusivity wording about how few "
    "comparable vehicles exist, how hard this one is to find, or how unusual its "
    "combination is, never derive it from market counts or the search radius, and never "
    "use the words \"rare\", \"rarely\", \"rarest\", \"rarity\", \"hard to find\", "
    "\"one of the few\", \"one of the only\", \"low-volume\", \"low volume\", \"limited "
    "production\", \"limited-production\" or \"in the region\" anywhere in the ad."
)
NON_MB_SCARCITY_RULE = SCARCITY_RULE  # name the three non-MB prompts import

# Paragraph-one recon rule for aggregator.build_recon_sentence()'s
# RECON_SENTENCE, used by the Hendrick Certified / Affordable / As-Is prompts
# (the MB CPO prompt carries its own "Sentence 4: RECON SENTENCE" line).
RECON_SENTENCE_RULE = (
    "RECON SENTENCE: If RECON_SENTENCE is provided in the data package, use it "
    "verbatim as the recon portion of paragraph one. Do not rewrite, summarize, or "
    "recount tire quantities. Do not add 'manufacturer-recommended' to non-MB tire "
    "language. Use the sentence exactly as given."
)

# Paragraph-one sentence-two rule for aggregator.build_provenance_sentence()'s
# PROVENANCE_SENTENCE, shared by all four tier prompts.
PROVENANCE_RULE = (
    "PROVENANCE SENTENCE: Use PROVENANCE_SENTENCE from the data package verbatim "
    "as the second sentence of paragraph one. Do not infer owner count from the "
    "stock number prefix. Do not modify this sentence. If it shows (omit), leave "
    "sentence two out."
)
