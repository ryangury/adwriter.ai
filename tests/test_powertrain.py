"""Offline tests for powertrain class + ELECTRIC_RANGE + the post-generation check.

Nothing touches the project's data: feature_cache.DB_PATH points at a temp copy,
the override file is a temp file, fueleconomy.gov and the manufacturer search are
stubbed, and ad generation is stubbed (no Claude call, no email)."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\adwriter")
import feature_cache  # noqa: E402
import powertrain as P  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pt_test_", dir=Path(__file__).parent))
shutil.copy(r"C:\adwriter\feature_cache.db", TMP / "feature_cache.db")
feature_cache.DB_PATH = TMP / "feature_cache.db"
P.OVERRIDES_PATH = TMP / "powertrain_overrides.json"
feature_cache._connect().execute("DELETE FROM electric_range").connection.commit()

FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# --------------------------------------------------------------------------- #
# stubbed fueleconomy.gov: records as the data service returned them on
# 2026-10-04 (G 580 #48717, GLC350e #48674, S60 T8 #47503, Model X LR+ #43413,
# 2024 EQE 500 SUV #47849); the rest are labelled test fixtures.
# --------------------------------------------------------------------------- #
VEH = {
    "48717": {"id": "48717", "atvType": "EV", "range": "239"},
    "48674": {"id": "48674", "atvType": "Plug-in Hybrid", "rangeA": "54", "range": "380"},
    "40001": {"id": "40001", "atvType": "", "range": "0"},          # fixture: G550 gas
    "40002": {"id": "40002", "atvType": "", "range": "0"},          # fixture: GLC300 gas
    "47503": {"id": "47503", "atvType": "Plug-in Hybrid", "rangeA": "40", "range": "530"},
    "40003": {"id": "40003", "atvType": "", "range": "0"},          # fixture: S60 B5 gas
    "43413": {"id": "43413", "atvType": "EV", "range": "351"},
    "40004": {"id": "40004", "atvType": "EV", "range": "300"},      # fixture: Model X Long Range
    "40005": {"id": "40005", "atvType": "EV", "range": "280"},      # fixture: Model X Performance
    "47849": {"id": "47849", "atvType": "EV", "range": "282"},
    "40006": {"id": "40006", "atvType": "EV", "range": "290"},      # fixture: 2024 EQE 500 sedan
    "40007": {"id": "40007", "atvType": "EV", "range": "310"},      # fixture: conflict, 19in wheels
    "40008": {"id": "40008", "atvType": "EV", "range": "270"},      # fixture: conflict, 21in wheels
}
MODELS = {
    (2025, "Mercedes-Benz"): {
        "G 580 with EQ Technology": [("Auto (A2)", "48717")],
        "G550": [("Auto 9-spd", "40001")],
        "GLC300 4matic": [("Auto 9-spd", "40002")],
        "GLC350e 4matic with EQ Hybrid Tech": [("Auto 9-spd", "48674")],
    },
    (2024, "Volvo"): {"S60 T8 AWD Recharge": [("Auto (S8)", "47503")], "S60 B5": [("Auto (S8)", "40003")]},
    (2020, "Tesla"): {
        "Model X Long Range Plus": [("Auto (A1)", "43413")],
        "Model X Long Range": [("Auto (A1)", "40004")],
        "Model X Performance (20in Wheels)": [("Auto (A1)", "40005")],
    },
    (2023, "Mercedes-Benz"): {},
    (2024, "Mercedes-Benz"): {
        "EQE 500 4matic (SUV)": [("Auto (A1)", "47849")],
        "EQE 500 4matic": [("Auto (A1)", "40006")],
    },
    (2025, "Testmake"): {"Volt 500": [("19in wheels", "40007"), ("21in wheels", "40008")]},
}
CALLS = []


def fake_fetch(path, params=None):
    CALLS.append((path, params))
    params = params or {}
    if path == "menu/model":
        names = MODELS.get((int(params["year"]), params["make"]), {})
        return {"menuItem": [{"text": n, "value": n} for n in names]} if names else None
    if path == "menu/options":
        opts = MODELS.get((int(params["year"]), params["make"]), {}).get(params["model"], [])
        return {"menuItem": [{"text": t, "value": v} for t, v in opts]}
    return VEH.get(path)


def epa(*a, **k):
    return P.epa_lookup(*a, fetch=fake_fetch, **k)


MFR_CALLS = []


def mfr_none(year, make, model, trim, cls, domains):
    MFR_CALLS.append((year, make, model, trim, tuple(domains)))
    return {"miles": None, "note": f"manufacturer site has no {year} figure for this trim"}


def mfr_found(year, make, model, trim, cls, domains):
    MFR_CALLS.append((year, make, model, trim, tuple(domains)))
    return {"miles": 279, "matched_trim": f"{year} {model} {trim}", "url": f"https://www.{domains[0]}/x", "note": None}


def no_search(*a, **k):
    raise AssertionError("no lookup may run for this class")


# --------------------------------------------------------------------------- #
# 1. classification of the seven named cars (real cached sticker / recon text)
# --------------------------------------------------------------------------- #
snap = {v["stock_number"]: v for v in json.load(open(r"C:\adwriter\last_inventory_snapshot.json"))["vehicles"]}
expect = {
    "V23409A": P.PHEV,   # Volvo S60 Recharge
    "PM98154": P.PHEV,   # GLC 350e
    "P29369": P.BEV,     # G 580 (listed as "G 580e")
    "PM32120": P.BEV,    # 2023 EQE SUV
    "PS18127A": P.BEV,   # Tesla Model X
    "PM92080A": P.HYBRID,  # Tucson Hybrid
    "D23371A": P.HYBRID,   # Escape hybrid
}
cls_of = {}
for stock, want in expect.items():
    v = snap[stock]
    st, rc = P.cached_texts(v["vin"])
    r = P.classify(v["vin"], v["year_make_model"], v["trim"], sticker_text=st, recon_text=rc, overrides={})
    cls_of[stock] = r["class"]
    check(f"classify {stock} {v['year_make_model']} -> {P.CLASS_LABELS[want]}", r["class"] == want,
          f"got {r['class']} from {[s['signal'] for s in r['signals']]}")

check("a trailing 'e' alone is no signal (G 580e with no sticker / recon)",
      P.classify(None, "2025 Mercedes-Benz G-Class", "G 580e AWD", overrides={})["class"] == P.GAS)
conf = P.classify(None, "2024 Hyundai Tucson Hybrid", "N Line AWD",
                  sticker_text="EPA Fuel Economy and Environment Plug-In Hybrid Vehicle", overrides={})
check("disagreeing signals -> unknown + flag", conf["class"] == P.UNKNOWN and "disagree" in (conf["flag"] or ""))
P.set_override(snap["PM92080A"]["vin"], P.PHEV, "test")
ov = P.classify(snap["PM92080A"]["vin"], "2024 Hyundai Tucson Hybrid", "N Line AWD", overrides=P.load_overrides())
check("override wins", ov["class"] == P.PHEV and ov["source"] == "override")
P.set_override(snap["PM92080A"]["vin"], None)
check("override removed", P.load_overrides() == {})

# --------------------------------------------------------------------------- #
# 2. ELECTRIC_RANGE (EPA data service stubbed, manufacturer search stubbed)
# --------------------------------------------------------------------------- #
def rng(stock, mfr=mfr_none, **kw):
    v = snap[stock]
    y, mk, md = P.split_ymm(v["year_make_model"])
    st, _ = P.cached_texts(v["vin"])
    return P.electric_range(y, mk, md, v["trim"], cls_of[stock], body_style=v.get("body_style"),
                            extra_text=" ".join(st[:400].split()), epa=epa, manufacturer=mfr, **kw)


r = rng("P29369")
check("G 580 exact match -> EPA 239", r["miles"] == 239 and r["phrase"] == "EPA-estimated up to 239 miles of electric range",
      str(r))
check("G 580 matched trim recorded", "G 580 with EQ Technology" in (r["matched_trim"] or "") and "id=48717" in (r["url"] or ""))
r = rng("PM98154")
check("GLC 350e exact match -> EPA 54", r["miles"] == 54, str(r))
r = rng("V23409A")
check("Volvo S60 Recharge -> EPA 40 (data service, never the sticker's 41)", r["miles"] == 40, str(r))
r = rng("PS18127A")
check("Model X Long Range Plus (not Long Range) -> EPA 351", r["miles"] == 351, str(r))
MFR_CALLS.clear()
r = rng("PM32120")
check("2023 EQE SUV: year mismatch -> no EPA figure used", r["miles"] is None, str(r))
check("2023 EQE SUV: falls back to the manufacturer search", len(MFR_CALLS) == 1 and MFR_CALLS[0][4][0] == "mbusa.com")
check("2023 EQE SUV: flag names the other-year EPA entry and says it is not used",
      "2024 EQE 500 4matic (SUV)" in (r["flag"] or "") and "not used" in (r["flag"] or ""), r["flag"])
row = feature_cache.get_electric_range(2023, "Mercedes-Benz", "EQE", "EQE 500 AWD")
check("2023 EQE SUV: 'none' result cached per trim", row and row["source"] == "none")
MFR_CALLS.clear()
r = rng("PM32120")
check("cached trim is not searched again", r["cached"] and not MFR_CALLS and r["miles"] is None)
feature_cache._connect().execute("DELETE FROM electric_range WHERE model='EQE'").connection.commit()
r = rng("PM32120", mfr=mfr_found)
check("manufacturer fallback wording", r["phrase"] == "manufacturer-estimated up to 279 miles of electric range", str(r))

r = P.electric_range(2025, "Testmake", "Volt", "500 AWD", P.BEV, epa=epa, manufacturer=no_search, use_cache=False)
check("two conflicting EPA results -> omit and flag (no manufacturer search)",
      r["miles"] is None and "conflicting" in (r["flag"] or ""), str(r))
r = P.electric_range(2025, "Mercedes-Benz", "Unobtainium", "Z 999 AWD", P.BEV, epa=epa, manufacturer=mfr_none, use_cache=False)
check("no result at all -> omit and flag", r["miles"] is None and "omitted" in (r["flag"] or ""), str(r))
CALLS.clear()
for stock in ("PM92080A", "D23371A"):
    v = snap[stock]
    y, mk, md = P.split_ymm(v["year_make_model"])
    r = P.electric_range(y, mk, md, v["trim"], cls_of[stock], epa=no_search, manufacturer=no_search)
    check(f"{stock} standard hybrid: no search, no range, no flag", r["miles"] is None and not r["flag"])
check("standard hybrids made no fueleconomy.gov call", not CALLS)


def unavailable(*a, **k):
    raise P.RangeLookupUnavailable("fueleconomy.gov unreachable: test")


r = P.electric_range(2025, "Mercedes-Benz", "G-Class", "G 580e AWD", P.BEV, epa=unavailable, manufacturer=no_search, use_cache=False)
check("service down -> omitted this run, nothing cached", r["miles"] is None and "this run" in r["flag"])

# --------------------------------------------------------------------------- #
# 3. post-generation check
# --------------------------------------------------------------------------- #
g580 = {"miles": 239, "phrase": P.range_phrase(239, "epa")}
bad = ("The G 580e is a plug-in hybrid G-Class with a fully electric drive mode delivering up to 62 miles "
       "of EPA-rated electric-only range, making daily driving entirely gasoline-free.")
good = "It offers EPA-estimated up to 239 miles of electric range."
check("G 580: wrong range + plug-in + gasoline wording flagged", len(P.sentence_problems(bad, P.BEV, g580)) >= 3)
check("G 580: exact phrase passes", P.sentence_problems(good, P.BEV, g580) == [])
check("G 580: right number in other wording passes (the check compares the number)",
      P.sentence_problems("EPA-estimated range is up to 239 miles on a full charge.", P.BEV, g580) == [])
volvo = {"miles": 40, "source": "epa", "phrase": P.range_phrase(40, "epa")}
check("tolerance: 41 vs ELECTRIC_RANGE 40 passes (live Volvo sentence)",
      P.sentence_problems("EPA-rated electric-only range is 41 miles, meaning most daily driving never touches the fuel tank.", P.PHEV, volvo) == [])
check("tolerance: 39 vs 40 passes", P.sentence_problems("It offers up to 39 miles of electric range.", P.PHEV, volvo) == [])
check("tolerance: 42 vs 40 flagged", P.sentence_problems("It offers up to 42 miles of electric range.", P.PHEV, volvo) != [])
check("tolerance: G 580 62 vs 239 still flagged", any("differs" in p for p in P.sentence_problems(bad, P.BEV, g580)))
mfr = {"miles": 279, "source": "manufacturer", "phrase": P.range_phrase(279, "manufacturer")}
check("manufacturer figure called EPA is flagged",
      P.sentence_problems("It offers EPA-estimated up to 279 miles of electric range.", P.BEV, mfr) != [])
check("manufacturer phrase passes", P.sentence_problems(f"It offers {mfr['phrase']}.", P.BEV, mfr) == [])
mild_s = "The EQ Boost 48V mild-hybrid system is included, recovering energy during deceleration."
check("mild wording flagged on a mild hybrid whose sticker doesn't print it",
      P.sentence_problems(mild_s, P.MILD, {}, sticker_mild=False) == ["mild-hybrid wording the sticker does not print"])
check("mild wording passes when the sticker prints it (GV80 MHEV)",
      P.sentence_problems(mild_s, P.MILD, {}, sticker_mild=True) == [])
check("mild wording flagged on a gas car", P.sentence_problems(mild_s, P.GAS, {}) != [])
check("mild wording flagged on a battery-electric car even with sticker_mild",
      P.sentence_problems(mild_s, P.BEV, g580, sticker_mild=True) != [])
txt2 = f"Intro.\n\nEquipment includes Burmester sound, {mild_s[4:]} Rate sentence."
new2, rem2 = P.strip_violations(txt2, P.MILD, {}, sticker_mild=False)
check("new copy: mild-only sentence is stripped", "mild-hybrid" not in new2 and rem2)
new3, rem3 = P.strip_violations(txt2, P.MILD, {}, sticker_mild=False, keep_mild_only=True)
check("existing copy: mild-only sentence is kept and reported as kept",
      new3 == txt2 and rem3 and all(p.startswith("(kept) ") for p in rem3[0][1]))
new4, rem4 = P.strip_violations(f"{bad}\n\n{mild_s}", P.BEV, g580, keep_mild_only=True)
check("existing copy: a sentence with other problems is still removed", "62 miles" not in new4)
lines_mild = "\n".join(P.data_package_lines({"class": P.MILD, "label": "mild hybrid", "range": {}, "sticker_mild": False}))
check("data package tells the model not to call it a mild hybrid", "Do not call this vehicle a mild hybrid" in lines_mild)
lines_mild2 = "\n".join(P.data_package_lines({"class": P.MILD, "label": "mild hybrid", "range": {}, "sticker_mild": True}))
check("data package allows it when the sticker prints it", "Do not call this vehicle a mild hybrid" not in lines_mild2)
gv = snap["T22954A"]
st_gv, rc_gv = P.cached_texts(gv["vin"])
check("GV80 Coupe sticker prints MHEV -> sticker_mild True",
      P.classify(gv["vin"], gv["year_make_model"], gv["trim"], sticker_text=st_gv, recon_text=rc_gv, overrides={})["sticker_mild"])
zt = snap["ZT22862"]
st_zt, rc_zt = P.cached_texts(zt["vin"])
check("GLB 250 sticker only lists '48 Volt System' -> sticker_mild False",
      not P.classify(zt["vin"], zt["year_make_model"], zt["trim"], sticker_text=st_zt, recon_text=rc_zt, overrides={})["sticker_mild"])

# work-order status text + completeness rule (offline, synthetic rows)
import aggregator as AG  # noqa: E402
import scraper as SC  # noqa: E402
parse = SC.ReconVisionScraper._wo_status_text
check("status parse: Closed/Ready For Sale", parse("PMB4201190\nClosed/Ready\nFor\nSale\n09/21/2026\n3:07PM") == "Closed/Ready For Sale")
check("status parse: Mechanical Repairs", parse("PMB4149770 Mechanical Repairs 10/03/2026 12:11PM") == "Mechanical Repairs")
rows = [{"section": "Final Quality Control", "kind": "task", "completion_status": "Completed by Vendor Add PO", "completed": False},
        {"section": "Close RO", "kind": "task", "completion_status": "INCOMPLETE", "completed": False}]
check("rule: Closed/Ready For Sale -> complete", AG._recon_decision(rows, "Closed/Ready For Sale")[0] is True)
check("rule: exact text only ('Ready For Sale' alone does not count)", AG._recon_decision(rows, "Ready For Sale")[0] is False)
check("rule: 'Completed by Vendor Add PO' unchanged without the status", AG._recon_decision(rows, None)[0] is False)
check("rule: sent-back status (Mechanical Repairs) stays incomplete", AG._recon_decision(rows, "Mechanical Repairs")[0] is False)
check("rule: _recon_is_complete delegates", AG._recon_is_complete(rows, "Closed/Ready For Sale") is True and AG._recon_is_complete(rows) is False)
check("BEV: combustion engine claim flagged",
      P.sentence_problems("When the battery is depleted it transitions to the combustion powertrain.", P.BEV, g580) != [])
check("standard hybrid stating a range flagged",
      P.sentence_problems("It can travel 30 miles on electric power alone.", P.HYBRID, {}) != [])
check("standard hybrid: ordinary hybrid wording passes",
      P.sentence_problems("The Tucson Hybrid pairs a turbocharged 1.6-liter engine with an electric motor.", P.HYBRID, {}) == [])
check("plug-in with ELECTRIC_RANGE omitted: any range flagged",
      P.sentence_problems("EPA-estimated up to 40 miles of electric range.", P.PHEV, {}) != [])
check("warranty / mileage figures are not range claims",
      P.sentence_problems("The battery warranty runs 86 months and 77,536 miles, and it shows 22,464 miles.", P.BEV, g580) == [])
check("G-Class LOW RANGE is not a range claim",
      P.sentence_problems("LOW RANGE gearing and G-TURN come standard.", P.BEV, g580) == [])
check("gas car: 'non-hybrid' passes", P.sentence_problems("It is the top non-hybrid trim.", P.GAS, {}) == [])
prot = "The Hendrick Certified Limited Powertrain Warranty covers the engine, transmission, and drive axle."
text = f"Para one.\n\n{bad} {good} {prot}\n\nPara three."
new, removed = P.strip_violations(text, P.BEV, g580, protected=[prot])
check("strip removes only the bad sentence, keeps protected and good ones",
      len(removed) == 1 and good in new and prot in new and "62 miles" not in new and new.count("\n\n") == 2)

# --------------------------------------------------------------------------- #
# 4. generate path: one retry, then strip + log + flag (generation stubbed)
# --------------------------------------------------------------------------- #
import adwriter  # noqa: E402
adwriter_mod = sys.modules['adwriter']; adwriter_mod.towing_for_package = lambda pkg: {'triggered': False}  # offline: no live tow lookups

pt_g580 = {"class": P.BEV, "label": "battery-electric", "range": g580, "flags": [], "source": "signals"}
pkg = {"stock_number": "P29369", "vehicle": {"status_code": 10, "year_make_model": "2025 Mercedes-Benz G-Class"},
       "powertrain": pt_g580}
ad_bad = f"P1 sentence.\n\n{bad} Equipment sentence.\n\nP3.\n\nP4."
ad_good = f"P1 sentence.\n\n{good} Equipment sentence.\n\nP3.\n\nP4."
seq = []
orig = adwriter._generate_once
adwriter.required_sentences_from = lambda p: {}
adwriter._generate_once = lambda p: (seq.pop(0), "CONFIDENCE: HIGH")
seq[:] = [ad_bad, ad_good]
out, fb = adwriter._generate_from_package(pkg)
check("generate: retry fixes it", out == ad_good and "POWERTRAIN_FLAGS" not in (fb or ""))
seq[:] = [ad_bad, ad_bad]
out, fb = adwriter._generate_from_package(pkg)
check("generate: after the retry the sentence is stripped and flagged",
      "62 miles" not in out and "Equipment sentence." in out and "POWERTRAIN_FLAGS" in (fb or ""), fb)
pt_unknown = {"class": P.UNKNOWN, "label": "unknown", "range": {}, "source": "signals",
              "flags": ["powertrain signals disagree (...) — class unknown, no range stated"]}
seq[:] = [ad_good, ad_good]
out, fb = adwriter._generate_from_package(dict(pkg, powertrain=pt_unknown))
check("generate: unknown class -> range stripped and flag in feedback",
      "239 miles" not in out and "class unknown" in (fb or ""))
adwriter._generate_once = orig

lines = "\n".join(P.data_package_lines(dict(pt_g580)))
check("data package carries ELECTRIC_RANGE phrase", "ELECTRIC_RANGE: EPA-estimated up to 239 miles of electric range" in lines)
check("data package for a hybrid omits the range",
      "ELECTRIC_RANGE: (omit" in "\n".join(P.data_package_lines({"class": P.HYBRID, "label": "standard hybrid", "range": {}})))

# --------------------------------------------------------------------------- #
# 5. top-up path (no model call): strip in place, nothing saved
# --------------------------------------------------------------------------- #
adwriter.powertrain_for = lambda stock, *a, **k: pt_g580
entry = {"paragraph_two": f"{bad} Equipment sentence.", "current_ad_text": ad_bad}
adwriter._topup_powertrain_check("P29369", entry)
check("top-up: offending sentence removed from paragraph and full text, flag recorded",
      "62 miles" not in entry["paragraph_two"] and "62 miles" not in entry["current_ad_text"] and entry.get("powertrain_flags"))

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:" if FAIL else "ALL PASSED", FAIL if FAIL else "")
sys.exit(1 if FAIL else 0)
