"""Offline tests: every sticker package of $750+ named in paragraph two carries its price (one retry, then a
Python-built price sentence after the sentence naming it), and a non-MB paragraph two keeps a standard-equipment
sentence when TRIM KNOWLEDGE lists the trim's standard equipment. Stubbed generation; nothing saved, no API call."""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import _paths  # noqa: F401  (repo root first on sys.path)
import feature_cache  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pkgprice_test_"))
shutil.copy(_paths.DATA / "feature_cache.db", TMP / "feature_cache.db")
feature_cache.DB_PATH = TMP / "feature_cache.db"
import adwriter  # noqa: E402

adwriter.towing_for_package = lambda pkg: {"triggered": False}   # offline: no live tow lookups
adwriter.required_sentences_from = lambda p: {}

FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


snap = {v["stock_number"]: v for v in json.load(open(str(_paths.DATA / "last_inventory_snapshot.json")))["vehicles"]}
cache = sqlite3.connect(str(_paths.DATA / "vehicle_cache.db"))

EXPLORER_KEY = (2023, "Ford", "Explorer", "ST - 4WD", "gas")
SILVERADO_KEY = (2024, "Chevrolet", "Silverado 1500", 'RST - 4WD Crew Cab 147"', "diesel")
EXPLORER_PACKAGES = [
    {"code": None, "name": "EQUIPMENT GROUP 401A", "price": 5890.0},
    {"code": None, "name": "50 STATE EMISSIONS", "price": 0.0},
    {"code": None, "name": "TWIN PANEL MOONROOF", "price": 1695.0},
    {"code": None, "name": "4G LTE WI-FI HOTSPOT CREDIT", "price": -20.0},
    {"code": None, "name": "TECHNOLOGY PACKAGE", "price": 995.0},
    {"code": None, "name": "Roof-Rail Crossbars", "price": 350.0, "dealer_installed": True},
]
SILVERADO_PACKAGES = [
    {"code": None, "name": "ENG: DURAMAX 3.0L TURBO-DIESEL", "price": 2390.0},
    {"code": None, "name": "CONVENIENCE PACKAGE II", "price": 1340.0},
    {"code": None, "name": '20" HIGH GLOSS BLACK PAINTED ALUMINUM WHEELS', "price": 1100.0},
    {"code": None, "name": "ASSIST STEPS", "price": 950.0, "dealer_installed": True},
    {"code": None, "name": "Z71 OFF-ROAD PACKAGE", "price": 750.0},
    {"code": None, "name": "PROTECTION PACKAGE", "price": 685.0},
]

EXPLORER_P2 = (
    "Power comes from the 3.0L EcoBoost V6 engine with a 10-speed automatic transmission. "
    "This vehicle was optioned with the ST High Package, Equipment Group 401A, at $5,890 from the factory. "
    "That package adds heated and ventilated front captain's chairs and a 360-degree camera. "
    "The Twin Panel Moonroof, added at $1,695, spans most of the cabin roofline. "
    "The Technology Package at $995 upgrades the infotainment setup to a 10.1-inch touchscreen. "
    "Standard equipment on the ST also includes 4WD, a 12.3-inch digital instrument cluster, a heated sport steering "
    "wheel, tri-zone climate control, wireless Apple CarPlay and Android Auto, and a power liftgate. "
    "Original MSRP was $62,510."
)
# The saved Sonnet 5.5 side-by-side paragraph two for CT23308A (2026-10-11): Duramax named without its $2,390, and no
# standard-equipment sentence.
SILVERADO_55_P2 = (
    "This Silverado pairs the RST trim with the diesel powertrain buyers look for in a daily-driver truck. "
    "Power comes from the 3.0L Duramax turbo-diesel with a 10-speed automatic transmission. "
    "The Z71 Off-Road Package ($750) is joined by skid plates, hill descent control, twin tube shocks and a 2-speed "
    "AutoTrac transfer case. Black 20\" high gloss painted aluminum wheels ($1,100) and the Dark Essentials Package "
    "($420) with black nameplates set the look. Assist steps ($950) make getting into the cab easier, and the MultiFlex "
    "tailgate ($445) adds versatility at the bed. Inside, you get Convenience Package II ($1,340), front bucket seats "
    "with a center console ($620), a Bose premium sound system, a universal home remote and a rear sliding power window. "
    "Original MSRP was $65,890."
)
SILVERADO_STD = ("The RST brings a 13.4-inch infotainment touchscreen with Google Built-in, a 12.3-inch digital gauge "
                 "cluster, heated front seats and dual-zone automatic climate control as standard equipment.")


def ad(p2):
    return f"Opening paragraph.\n\n{p2}\n\nWarranty paragraph.\n\nCloser paragraph."


def pkg(packages, key, **kw):
    return dict({"msrp_data": {"option_packages": packages}, "_trim_knowledge_key": key}, **kw)


print("=== package prices ===")
ex = pkg(EXPLORER_PACKAGES, EXPLORER_KEY)
check("Explorer: all three $750+ packages named with their prices", adwriter.package_price_missing(ad(EXPLORER_P2), ex) == [])
check("Explorer: Roof-Rail Crossbars ($350) is under the line, never required",
      all(m["name"] != "Roof-Rail Crossbars" for m in adwriter.package_coverage_missing(ad(EXPLORER_P2), ex)))
bare = (EXPLORER_P2.replace(", at $5,890 from the factory", "").replace(", added at $1,695,", "")
        .replace(" at $995", ""))
miss = adwriter.package_price_missing(ad(bare), ex)
check("Explorer with the prices removed: 401A, moonroof and Technology flagged",
      [m["name"] for m in miss] == ["EQUIPMENT GROUP 401A", "TWIN PANEL MOONROOF", "TECHNOLOGY PACKAGE"], miss)
fixed = adwriter.insert_package_prices(ad(bare), miss)
p2 = adwriter.split_ad_paragraphs(fixed)["paragraph_two"]
check("inserted right after the sentence naming each package",
      "Group 401A. The Equipment Group 401A was a $5,890 factory option. That package adds" in p2
      and "roofline. The Twin Panel Moonroof was a $1,695 factory option." in p2
      and "touchscreen. The Technology Package was a $995 factory option. Standard equipment" in p2, p2)
check("after the insert nothing is missing", adwriter.package_price_missing(fixed, ex) == [])
check("other paragraphs untouched", fixed.startswith("Opening paragraph.\n\n") and fixed.endswith("\n\nCloser paragraph."))
check("approximate (ACV Max) prices: no price required",
      adwriter.package_price_missing(ad(bare), dict(ex, sticker_prices_approximate=True)) == []
      and adwriter.package_price_missing(ad(bare), dict(ex, msrp_data={"option_packages": EXPLORER_PACKAGES,
                                                                       "source": "acvmax_options_tab"})) == [])
check("predictive sticker: no price required", adwriter.package_price_missing(ad(bare), dict(ex, sticker_is_predictive=True)) == [])

sv = pkg(SILVERADO_PACKAGES, SILVERADO_KEY)
miss = adwriter.package_price_missing(ad(SILVERADO_55_P2), sv)
check("Silverado 5.5: only the Duramax ($2,390) is named without its price",
      [(m["name"], m["price"]) for m in miss] == [("ENG: DURAMAX 3.0L TURBO-DIESEL", 2390.0)], miss)
check("price sentence wording", adwriter.package_price_sentence(miss[0]) == "The Duramax 3.0L Turbo-Diesel was a $2,390 factory option.")
check("dealer-installed, plural wording",
      adwriter.package_price_sentence({"name": "ASSIST STEPS", "price": 950.0, "dealer_installed": True})
      == "The Assist Steps were a $950 dealer-installed option.")
check("price in the next sentence counts",
      adwriter.package_price_missing(ad("The Convenience Package II adds Bose audio. It was $1,340 new."),
                                     pkg([SILVERADO_PACKAGES[1]], None)) == [])
check("a price in an unrelated sentence does not count",
      adwriter.package_price_missing(ad("The Convenience Package II adds Bose audio. Bose is great. Original MSRP was $1,340."),
                                     pkg([SILVERADO_PACKAGES[1]], None)) != [])

print("=== standard equipment ===")
check("Explorer stored text has its standard-equipment sentence", adwriter.standard_equipment_missing(ad(EXPLORER_P2), ex) is None)
no_std = " ".join(s for s in adwriter._split_sentences(EXPLORER_P2) if not s.startswith("Standard equipment"))
check("Explorer without it: missing", bool(adwriter.standard_equipment_missing(ad(no_std), ex)))
check("Silverado 5.5: missing (no sentence says standard)", bool(adwriter.standard_equipment_missing(ad(SILVERADO_55_P2), sv)))
check("Silverado 5.5 plus a standard-equipment sentence: present",
      adwriter.standard_equipment_missing(ad(SILVERADO_55_P2 + " " + SILVERADO_STD), sv) is None)
check("no TRIM KNOWLEDGE (MB, or nothing cached): not required",
      adwriter.standard_equipment_missing(ad(no_std), dict(ex, _trim_knowledge_key=None)) is None
      and adwriter.standard_equipment_missing(ad(no_std), dict(ex, _trim_knowledge_key=(2023, "Ford", "Explorer", "XLT", "gas"))) is None)

print("=== generate: retry, then insert ===")


def run(replies, package):
    seq, calls = list(replies), []
    adwriter._generate_once = lambda p: (calls.append(1) or seq.pop(0), "CONFIDENCE: HIGH")
    full = dict(package, stock_number="CT23308A", towing={"triggered": False},
                vehicle={"status_code": 11, "year_make_model": "2024 Chevrolet Silverado 1500"},
                powertrain={"class": "diesel", "range": {}, "flags": [], "engine": None})
    out, fb = adwriter._generate_from_package(full)
    return out, fb, len(calls)


good = SILVERADO_55_P2.replace("turbo-diesel with", "turbo-diesel ($2,390) with") + " " + SILVERADO_STD
out, fb, n = run([ad(good)], sv)
check("all prices and the standard sentence: one call, nothing added", n == 1 and out == ad(good) and "PACKAGE PRICE" not in fb, fb)
out, fb, n = run([ad(SILVERADO_55_P2), ad(good)], sv)
check("first reply misses: retried once, the retry's text kept", n == 2 and out == ad(good), fb)
out, fb, n = run([ad(SILVERADO_55_P2), ad(SILVERADO_55_P2)], sv)
check("still missing after the retry: Duramax price inserted after its sentence, flagged",
      n == 2 and "transmission. The Duramax 3.0L Turbo-Diesel was a $2,390 factory option. The Z71" in out
      and "PACKAGE PRICE inserted after retry: The Duramax 3.0L Turbo-Diesel was a $2,390 factory option." in fb, fb)
check("still no standard-equipment sentence after the retry: flagged, nothing invented",
      "STANDARD EQUIPMENT (warning only)" in fb and "standard equipment" not in adwriter.split_ad_paragraphs(out)["paragraph_two"].lower(), fb)

print("=== standard-equipment retry message ===")
note = adwriter.standard_equipment_retry_note(ex, adwriter.standard_equipment_missing(ad(no_std), ex))
check("Explorer: the message names the gap and asks for 'Standard equipment on the ST also includes' with 3+ items",
      "no standard-equipment sentence" in note and 'beginning "Standard equipment on the ST also includes"' in note
      and "at least 3 items" in note, note)
check("Explorer: the cached list is given, item by item", "  - 12.3-inch digital instrument cluster" in note and "  - 4WD" in note, note)
OLD_EXPLORER = ("2023 Ford Explorer ST 4WD standard equipment: twin-turbocharged 3.0L EcoBoost V6 (exclusive to ST and above vs. "
                "2.3L I-4 on lower trims), 4WD, sport-tuned suspension, 12.3-inch digital instrument cluster")
raw_note = adwriter.standard_equipment_retry_note(ex, OLD_EXPLORER)
check("an uncleaned list: engine and trim-comparison wording is left out of the message",
      "EcoBoost" not in raw_note and "2.3L" not in raw_note and "  - sport-tuned suspension" in raw_note, raw_note)
note = adwriter.standard_equipment_retry_note(sv, adwriter.standard_equipment_missing(ad(SILVERADO_55_P2), sv))
check("Silverado 5.5: 'Standard equipment on the RST also includes', its cached list, no 'NOT standard' clause",
      'beginning "Standard equipment on the RST also includes"' in note and "  - 13.4-inch infotainment touchscreen with Google Built-in" in note
      and "Bose" not in note and "NOT" not in note, note)

seen = []


def run_seen(replies, package):
    seq = list(replies)

    def once(p):
        seen.append(p.get("_retry_note"))
        return seq.pop(0), "CONFIDENCE: HIGH"
    adwriter._generate_once = once
    full = dict(package, stock_number="CT23308A", towing={"triggered": False},
                vehicle={"status_code": 11, "year_make_model": "2024 Chevrolet Silverado 1500"},
                powertrain={"class": "diesel", "range": {}, "flags": [], "engine": None})
    out, fb = adwriter._generate_from_package(full)
    return out, fb, full


seen.clear()
fixed55 = SILVERADO_55_P2.replace("turbo-diesel with", "turbo-diesel ($2,390) with")
out, fb, full = run_seen([ad(fixed55), ad(fixed55 + " " + SILVERADO_STD)], sv)
check("Silverado 5.5: first call has no message, the retry carries it, the retry's sentence is kept",
      seen[0] is None and "Standard equipment on the RST also includes" in (seen[1] or "")
      and SILVERADO_STD in out and "STANDARD EQUIPMENT" not in fb, (seen, fb))
check("the message is cleared after the build", "_retry_note" not in full)
seen.clear()
out, fb, full = run_seen([ad(fixed55), ad(fixed55)], sv)
check("still missing after the one retry: flagged", "STANDARD EQUIPMENT (warning only)" in fb and len(seen) == 2, fb)
seen.clear()
ex_bare = dict(ex)
out, fb, full = run_seen([ad(no_std), ad(EXPLORER_P2)], ex_bare)
check("Explorer: the retry carries 'Standard equipment on the ST also includes'",
      "Standard equipment on the ST also includes" in (seen[1] or "") and out == ad(EXPLORER_P2), seen)
seen.clear()
out, fb, full = run_seen([ad(SILVERADO_55_P2 + " " + SILVERADO_STD), ad(good)], sv)
check("a retry for another reason (a missing price) carries no standard-equipment message", seen == [None, None], seen)

print("=== non-MB prompts: $750 package prices ===")
for name in ("AS_IS_PROMPT", "HENDRICK_AFFORDABLE_PROMPT", "HENDRICK_CERTIFIED_PROMPT"):
    text = getattr(adwriter, name)
    check(f"{name}: states prices of $750+ options, names smaller ones without a price",
          "State the original price of any package or option that was $750 or more" in text
          and "Name smaller options without a price" in text and "$400" not in text)
check("MB prompt: still $750", "$750 or more at time of sale" in adwriter.SYSTEM_PROMPT)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:" if FAIL else "ALL PASSED", FAIL if FAIL else "")
sys.exit(1 if FAIL else 0)
