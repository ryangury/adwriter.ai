"""Mild-hybrid lot report (evidence per car), the 9 live ads, and offline tests of
MILD_HYBRID_SENTENCE on a car whose sticker has only the 48-volt line. Read-only:
no lookups, nothing saved, no Claude call."""
import json
import re
import sys

sys.path.insert(0, r"C:\adwriter")
import powertrain as P  # noqa: E402

snap = {v["stock_number"]: v for v in json.load(open(r"C:\adwriter\last_inventory_snapshot.json"))["vehicles"]}
hist = json.load(open(r"C:\adwriter\ad_history.json", encoding="utf-8"))


def info_for(stock):
    v = snap[stock]
    st, rc = P.cached_texts(v["vin"])
    info = P.classify(v["vin"], v["year_make_model"], v["trim"], sticker_text=st, recon_text=rc, overrides={})
    _, make, _ = P.split_ymm(v["year_make_model"])
    info["mild_sentence"] = P.mild_sentence_for(info, make)
    return v, st, info


print("=== MERCEDES-BENZ CARS: 48-VOLT EVIDENCE PER CAR ===")
rows = []
for stock, v in snap.items():
    y, mk, md = P.split_ymm(v["year_make_model"])
    if (mk or "").lower() != "mercedes-benz":
        continue
    v, st, info = info_for(stock)
    if info["class"] in (P.PHEV, P.BEV):
        continue
    if not st.strip():
        ev = "no sticker cached"
    elif info["sticker_48v"]:
        m = re.search(r"[A-Z0-9]{3}\s*-\s*48[- ]?V(?:olt)?\s+system", st, re.I)
        ev = f'sticker line "{" ".join(m.group(0).split()) if m else info["sticker_48v"]}"'
    else:
        ev = "sticker has no 48-volt line"
    rows.append((md, y, v["trim"], stock, info["class"], ev, "YES" if info["mild_sentence"] else "no"))
for r in sorted(rows):
    print(f"{r[0]:9} {r[1]} {r[2]:18} {r[3]:9} {r[4]:11} sentence={r[6]:3}  {r[5]}")
print("sentence gate passes:", sum(r[6] == "YES" for r in rows), "of", len(rows))

print("\n=== THE 9 LIVE ADS WITH MILD-HYBRID WORDING ===")
NINE = ["ZT22862", "ZT22965", "P36138", "PM47578", "P16783", "PM88699", "P21297", "T23359A", "ZT22912A"]
TERM = re.compile(r"\bmild[- ]hybrid\b|\bMHEV\b", re.I)
for s in NINE:
    v, st, info = info_for(s)
    ad = hist[s]["current_ad_text"]
    n = len(TERM.findall(ad))
    has48 = bool(info["sticker_48v"])
    twice = has48 and n >= 1
    print(f"{s:9} {v['year_make_model']} {v['trim']:16} 48-volt line: {'yes' if has48 else 'NOTHING'}"
          f" | term in current ad: {n}x | adding the sentence to this text as-is: "
          f"{'term twice' if twice else ('-' if not has48 else 'once')}")

print("\n=== TESTS: car with only the 48-volt line ===")
FAIL = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAIL.append(name)


v, st, info = info_for("ZT22862")   # 2026 GLB 250: "B01 - 48 Volt System", no "mild hybrid" term
pt = dict(info, range={})
check("ZT22862: class mild hybrid, 48-volt line, term not printed", info["class"] == P.MILD and info["sticker_48v"] and not info["sticker_mild"])
check("ZT22862: sentence gate passes", info["mild_sentence"] == P.MILD_HYBRID_SENTENCE)
check("Python sentence passes the term / type / range checks on its own",
      P.check_claims(P.MILD_HYBRID_SENTENCE, P.MILD, {}, sticker_mild=P.mild_wording_ok(pt)) == [])
claude = "The EQ Boost 48V mild-hybrid system is included, recovering energy during deceleration."
check("Claude's own mild-hybrid sentence is flagged", P.check_claims(claude, P.MILD, {}, sticker_mild=P.mild_wording_ok(pt)) != [])
ad = (f"Opening sentence.\n\nThe GLB 250 is powered by a turbocharged 2.0-liter four-cylinder engine. "
      f"{P.MILD_HYBRID_SENTENCE} {claude} Equipment sentence.\n\nP3.\n\nP4.")
new, removed = P.strip_violations(ad, P.MILD, {}, sticker_mild=P.mild_wording_ok(pt))
check("strip: Claude's use removed, Python sentence kept, term appears once",
      claude not in new and P.MILD_HYBRID_SENTENCE in new and len(TERM.findall(new)) == 1)
check("gate: a gas Mercedes with no 48-volt line gets no sentence",
      P.mild_sentence_for({"class": P.GAS, "sticker_48v": None}, "Mercedes-Benz") is None)
check("gate: non-Mercedes mild hybrid (GV80 Coupe) gets no sentence",
      P.mild_sentence_for({"class": P.MILD, "sticker_48v": "48V system"}, "Genesis") is None)
check("gate: mild class without the sticker line gets no sentence",
      P.mild_sentence_for({"class": P.MILD, "sticker_48v": None}, "Mercedes-Benz") is None)
gv_v, gv_st, gv = info_for("T22954A")
check("GV80 Coupe (sticker prints MHEV, non-MB): own wording still allowed", P.mild_wording_ok(dict(gv, mild_sentence=None)))

import adwriter  # noqa: E402
adwriter_mod = sys.modules['adwriter']; adwriter_mod.towing_for_package = lambda pkg: {'triggered': False}  # offline: no live tow lookups

req = {"mild_hybrid_sentence": P.MILD_HYBRID_SENTENCE}
p2_engine = "Opening line.\n\nThe GLB 250 is powered by a turbocharged 2.0-liter four-cylinder engine. It adds Burmester sound. Last."
out = adwriter.split_ad_paragraphs(adwriter.insert_required_sentences(p2_engine, req, ["mild_hybrid_sentence"]))["paragraph_two"]
check("placement: right after the engine sentence",
      out.startswith("The GLB 250 is powered by a turbocharged 2.0-liter four-cylinder engine. " + P.MILD_HYBRID_SENTENCE))
p2_none = "Opening line.\n\nThis GLB is finished in Polar White. It adds Burmester sound. Last."
out = adwriter.split_ad_paragraphs(adwriter.insert_required_sentences(p2_none, req, ["mild_hybrid_sentence"]))["paragraph_two"]
check("placement: after the opening sentence when there is no engine sentence",
      out.startswith("This GLB is finished in Polar White. " + P.MILD_HYBRID_SENTENCE))
check("required-sentence guard sees it missing",
      adwriter.missing_required_sentences("No sentence here.", req) == ["mild_hybrid_sentence"])
e = {"current_ad_text": f"x. {P.MILD_HYBRID_SENTENCE}"}
adwriter.stamp_mild_sentence(e, "2026-10-04")
adwriter.stamp_mild_sentence(e, "2026-10-09")
check("first-included date set once and never moved", e.get("mild_hybrid_sentence_first_date") == "2026-10-04")
e2 = {"current_ad_text": "no sentence"}
adwriter.stamp_mild_sentence(e2, "2026-10-04")
check("no date when the ad lacks the sentence", "mild_hybrid_sentence_first_date" not in e2)

# generate path (generation stubbed): Claude omits the sentence and writes its own -> retry,
# then the sentence is inserted and Claude's wording stripped
seq = []
adwriter._generate_once = lambda p: (seq.pop(0), "CONFIDENCE: HIGH")
pkg = {"stock_number": "ZT22862", "vehicle": {"status_code": 10, "year_make_model": "2026 Mercedes-Benz GLB"},
       "powertrain": dict(pt, mild_sentence=P.MILD_HYBRID_SENTENCE, flags=[])}
bad_ad = f"Opening sentence.\n\nThe GLB 250 is powered by a turbocharged 2.0-liter four-cylinder engine. {claude} Equipment sentence.\n\nP3.\n\nP4."
seq[:] = [bad_ad, bad_ad]
out, fb = adwriter._generate_from_package(pkg)
check("generate: after the retry the sentence is inserted after the engine sentence and Claude's use stripped",
      P.MILD_HYBRID_SENTENCE in out and claude not in out and len(TERM.findall(out)) == 1
      and "engine. " + P.MILD_HYBRID_SENTENCE in out)

print()
print("FAILED:" if FAIL else "ALL PASSED", FAIL if FAIL else "")
sys.exit(1 if FAIL else 0)
