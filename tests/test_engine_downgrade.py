"""Offline tests: sticker engine facts (stubbed generation). Nothing saved, no API call.
(The recon downgrade checks that lived here moved to tests/test_recon_latch.py.)"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import _paths  # noqa: F401  (repo root first on sys.path)
import adwriter  # noqa: E402
adwriter_mod = sys.modules['adwriter']; adwriter_mod.towing_for_package = lambda pkg: {'triggered': False}  # offline: no live tow lookups
import aggregator as AG  # noqa: E402
import powertrain as P  # noqa: E402
import scraper as S  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


snap = _paths.snapshot()        # live snapshot + tests/fixtures/pinned_vehicles.json (pinned wins)

# Cars the tests are pinned to that may be sold (and so leave the live snapshot)
# are read from a saved copy of their snapshot rows instead.
for _s, _v in json.load(open(str(_paths.ROOT / "tests" / "fixtures" / "pinned_vehicles.json"), encoding="utf-8")).items():
    snap.setdefault(_s, _v)


def pt_for(stock):
    v = snap[stock]
    st, rc = P.cached_texts(v["vin"])
    info = P.classify(v["vin"], v["year_make_model"], v["trim"], sticker_text=st, recon_text=rc, overrides={})
    return dict(info, range={}, flags=[], mild_sentence=None)


def run_generate(stock, replies):
    pt = pt_for(stock)
    seq = list(replies)
    calls = []
    adwriter._generate_once = lambda p: (calls.append(1) or seq.pop(0), "CONFIDENCE: HIGH")
    pkg = {"stock_number": stock, "vehicle": {"status_code": snap[stock]["status_code"],
                                             "year_make_model": snap[stock]["year_make_model"]}, "powertrain": pt, "towing": {"triggered": False}}
    out, fb = adwriter._generate_from_package(pkg)
    return out, fb, len(calls), pt


adwriter.required_sentences_from = lambda p: {}


def ad(p2):
    return f"Opening paragraph.\n\n{p2} Equipment sentence.\n\nWarranty paragraph.\n\nCloser paragraph."


print("=== engine facts ===")
pt = pt_for("CT23308A")
check("CT23308A classed diesel from the sticker engine line", pt["class"] == P.DIESEL and pt["engine"]["fuel"] == "diesel")
v8 = "The 10-speed automatic transmission confirms V8 power under the hood."
out, fb, n, _ = run_generate("CT23308A", [ad(v8), ad(v8)])
check("CT23308A 'V8' reply: retried once, then stripped and flagged",
      n == 2 and "V8" not in out and "Equipment sentence." in out and "POWERTRAIN_FLAGS" in (fb or ""), fb)

eco = "Power comes from a 2.0-liter turbocharged inline 4-cylinder EcoBoost engine producing 250 horsepower, paired with an 8-speed automatic."
out, fb, n, _ = run_generate("D23371A", [ad(eco), ad(eco)])
check("D23371A '2.0-liter EcoBoost' reply: retried once, then stripped", n == 2 and "2.0-liter" not in out, fb)

good = "Power comes from a 3.0-liter Duramax turbo-diesel paired with a 10-speed automatic."
out, fb, n, _ = run_generate("CT23308A", [ad(good)])
check("correct reply (3.0-liter Duramax turbo-diesel) passes first time", n == 1 and good in out and "POWERTRAIN_FLAGS" not in (fb or ""), fb)
good2 = "The Escape Platinum pairs a 2.5-liter four-cylinder with an electric motor."
out, fb, n, _ = run_generate("D23371A", [ad(good2)])
check("correct reply (2.5-liter four-cylinder hybrid) passes first time", n == 1 and good2 in out, fb)

lower = ("The Explorer ST is powered by a twin-turbocharged 3.0-liter EcoBoost V6 that produces 400 horsepower, "
         "stepping well past the 2.3-liter four-cylinder found in lower trims.")
out, fb, n, _ = run_generate("P58889A", [ad(lower), ad(lower)])
check("P58889A '2.3-liter found in lower trims': stripped after the retry", n == 2 and "2.3-liter" not in out, fb)
check("P58889A: the correct 3.0-liter V6 alone passes",
      P.engine_problems("A twin-turbocharged 3.0-liter EcoBoost V6 produces 400 horsepower.", pt_for("P58889A")["engine"]) == [])

check("no engine on the sticker (Mercedes GLE 350): nothing checked",
      pt_for("P14497")["engine"] is None and P.engine_problems("A 2.0-liter four-cylinder.", None) == [])
check("diesel: gasoline wording flagged", P.engine_problems("A gasoline engine.", pt["engine"]) != [])
check("gas sticker: diesel wording flagged", P.engine_problems("The Duramax diesel pulls hard.", pt_for("P58889A")["engine"]) != [])
check("cylinder count contradiction flagged (Gladiator V6 sticker, ad says V8)",
      P.engine_problems("Its V8 is smooth.", pt_for("XH08745B")["engine"]) != [])
enc = "Under the hood is a 1.3-liter turbocharged inline 3-cylinder engine."
probs = P.engine_problems(enc, pt_for("XH51984A")["engine"])
check("Encore GX: unprinted layout is 'unsupported', not a contradiction", probs and all(p.startswith(P.ENGINE_UNPRINTED_LAYOUT) for p in probs))
txt = f"Para.\n\n{enc} More."
new, rem = P.strip_violations(txt, P.GAS, {}, keep_mild_only=True, engine=pt_for("XH51984A")["engine"])
check("existing copy keeps the unsupported-layout sentence and flags it", new == txt and rem and rem[0][1][0].startswith("(kept) "))
new, rem = P.strip_violations(f"Para.\n\n{v8} More.", P.DIESEL, {}, keep_mild_only=True, engine=pt["engine"])
check("existing copy: CT23308A's V8 sentence is kept+flagged (unprinted layout), never silently passed",
      rem and "V8" in new and rem[0][1][0].startswith("(kept) "))
lines = "\n".join(P.data_package_lines(dict(pt, label="diesel")))
check("data block carries the sticker engine line as authoritative",
      "STICKER ENGINE (authoritative): ENG: DURAMAX 3.0L TURBO-DIESEL" in lines)
txt_r = adwriter._research_instructions([{"kind": "trim_knowledge", "year": 2024, "make": "Chevrolet", "model": "Silverado 1500",
                                          "trim": "RST", "powertrain": P.DIESEL, "sticker_engine": pt["engine"]["text"]}])
check("trim research is told the sticker engine is authoritative", "DURAMAX 3.0L TURBO-DIESEL" in txt_r and "authoritative" in txt_r)

# The recon two-check downgrade was removed on 2026-10-11 (latched recon): see tests/test_recon_latch.py.
print()
print("FAILED:" if FAIL else "ALL PASSED", FAIL if FAIL else "")
sys.exit(1 if FAIL else 0)
