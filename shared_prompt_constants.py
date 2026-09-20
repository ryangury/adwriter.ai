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

WEB_SEARCH_NON_MB_BLOCK = """\
WEB SEARCH FOR NON-MERCEDES VEHICLES
For any non-Mercedes-Benz vehicle, before writing paragraph two, fire a web search to verify:
- What equipment is standard on this specific trim level (not just the model)
- What equipment is exclusive to this trim vs lower trims
- What packages actually exist for this model year and trim
- Any powertrain specification you are about to state: cylinder count, displacement, engine configuration (inline vs V), and horsepower/torque figures. Do not state these from memory, even when confident. Small-displacement turbocharged engines are frequently 3-cylinder rather than 4-cylinder, and stating the wrong cylinder count is a factual error a knowledgeable buyer or a fact-checking search engine will catch immediately.

Never attribute standard trim-level equipment to a package unless a package is explicitly named on the window sticker with a price. Never invent package names. If Highway Driving Assist is standard on the Calligraphy trim, it is not a package — it is standard equipment and should be mentioned as a trim differentiator, not a package add-on.

The question to answer before writing: "What does this specific trim have that lower trims do not?" That is the selling story. Base equipment that comes on every model is not a differentiator."""

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

STORE_CLOSER_PARAGRAPH = (
    "Mercedes-Benz of Durham is the number one Certified Pre-Owned Mercedes-Benz "
    "dealer in the Triangle, with 4.9 stars across 4,000-plus Google reviews, part "
    "of the Hendrick Automotive Group. Our pricing is researched daily against live "
    "market data so you can buy with confidence and skip the back-and-forth. Find "
    "us at the Hendrick Automotive Mall on Kentington Drive in Durham, serving "
    "Raleigh, Cary, Chapel Hill, Wake Forest, and the entire Research Triangle "
    "region. Six stores, nine brands, just minutes from Southpoint Mall and I-40."
)
