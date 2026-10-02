"""status_codes.py -- the one definition of ACV Max inventory status codes.

Every module that needs "which statuses get an ad", "which statuses does the
crawler keep", or "what is this status called" derives it from here, so the
sets cannot drift apart (they did: the crawler's mapped set omitted 13 while the
orchestrator, ad router and tier mapping all handled it).

    1   Not certified      in stock, certification not yet assigned: tracked, never run
    10  MB CPO             Mercedes-Benz Certified Pre-Owned
    11  Hendrick Certified
    12  Hendrick Affordable
    13  As-Is
    16  Courtesy (MB CPO)

Adding a new code means editing this file only (plus a prompt/tier rule if it
needs its own ad framing).
"""

# Pricing-gate groups (aggregator): MB CPO needs a window sticker and ACV Max
# proof points; the rest need only ACV Max pricing.
MB_CPO_STATUS_CODES = frozenset({10, 16})
NON_CPO_STATUS_CODES = frozenset({11, 12, 13})

# Statuses that get an ad (orchestrator build/recon-update/reprice queues, the
# daily-report "postable" test, time-to-post clock, sticker-warmup priority).
BUILD_STATUS_CODES = MB_CPO_STATUS_CODES | NON_CPO_STATUS_CODES

# Status 1 is kept in the snapshot (it is in stock) but never gets an ad.
NEEDS_CERTIFICATION_STATUS = 1

# What the crawler keeps as retail. Anything else is dropped as "unmapped".
MAPPED_STATUS_CODES = BUILD_STATUS_CODES | {NEEDS_CERTIFICATION_STATUS}

# Statuses whose warranty/"certified" claim the ad may make. Deliberately NOT
# the build set: Hendrick Affordable (12) and As-Is (13) are not certified.
CERTIFIED_STATUS_CODES = frozenset({10, 11, 16})

STATUS_LABELS = {
    1: "Not certified",
    10: "MB CPO",
    11: "Hendrick Certified",
    12: "Hendrick Affordable",
    13: "As-Is",
    16: "Courtesy (MB CPO)",
}

assert set(STATUS_LABELS) == set(MAPPED_STATUS_CODES), "a status needs a label"
assert CERTIFIED_STATUS_CODES <= BUILD_STATUS_CODES
