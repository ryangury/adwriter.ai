"""Offline tests (WORKTREE): factory-warranty sentence from Carfax's own estimate.
Reads the worktree's copies of the data; saves nothing; no API call."""
import copy
import json
import re
import sqlite3
import sys
from datetime import date, timedelta

WT = r"C:\adwriter"
sys.path.insert(0, WT)
import adwriter as A  # noqa: E402
import aggregator as AG  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def report(line, statuses, as_of, odometers, basic_header=True):
    cols = "".join(f"\n\t\nWarranty {s}" for s in statuses)
    # real Carfax layout: one label, then one value per owner column (oldest first)
    reads = ("\n\n\nLast reported odometer reading" + "".join(f"\n\t\n{o}" for o in odometers) + "\nDetailed History") if odometers else ""
    return {"raw_text": (f"Owner 1{reads}\n{'Basic Warranty' if basic_header else ''}\n{line}{cols}\nTitle History\n"
                         f"This CARFAX Vehicle History Report is based only on information supplied to CARFAX and "
                         f"available as of {as_of} at 9:31:51 PM (CDT).")}


today = date.today()
asof = (today - timedelta(days=8)).strftime("%m/%d/%y")
volvo = report("Original warranty estimated to have 15 months or 34,501 miles remaining. Confirm with dealer.",
               ["Active", "Active"], asof, ["5,880", "15,499"])
e = AG.carfax_warranty_estimate(volvo)
check("estimate: N 15, M 34,501, LAST odometer reading 15,499 (text), term 50,000",
      e["ok"] and e["months_at_report"] == 15 and e["miles_at_report"] == 34501 and e["last_odometer"] == 15499
      and e["odometer_source"] == "report text" and e["term_miles"] == 50000, e)
fw = AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 19878}, volvo)
s = AG.factory_warranty_sentence(fw)
check("Volvo wording (row label 'Basic Warranty' -> basic)",
      s == "CARFAX estimates about 15 months remain on the original Volvo basic warranty, or about 30,100 miles at the current odometer, whichever comes first.", s)
older = report("Original warranty estimated to have 15 months or 34,501 miles remaining.", ["Active"],
               (today - timedelta(days=70)).strftime("%m/%d/%y"), ["15,499"])
fw2 = AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 19878}, older)
check("months = N minus whole months since the report date (70 days -> 13)", fw2["months"] == 15 - AG._months_between(today - timedelta(days=70), today), fw2["months"])
check("report over 60 days old is flagged (sentence still built)", fw2["ok"] and any("days old" in f for f in fw2["flags"]), fw2["flags"])
check("stored field used only when the text has no reading",
      AG.carfax_warranty_estimate(dict(report("Original warranty estimated to have 15 months or 34,501 miles remaining.", ["Active"], asof, []), last_reported_odometer=15499))["odometer_source"] == "stored field")
odd = report("Original warranty estimated to have 15 months or 34,501 miles remaining.", ["Active"], asof, ["15,400"])
fw3 = AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 19878}, odd)
check("derived term not round (49,901): no sentence, flagged", not fw3["ok"] and "not a round number" in fw3["reason"] and fw3["flags"], fw3["reason"])
exp = report("Original warranty estimated to have expired.", ["Expired", "Expired"], asof, ["90,000"])
check("Carfax says expired: no sentence", not AG.factory_warranty_remaining("2020 Volvo XC90", {"mileage": 95000}, exp)["ok"])
check("no Basic Warranty line: no sentence", AG.carfax_warranty_estimate({"raw_text": "nothing"})["reason"] == "Carfax report has no warranty estimate row")
nodate = {"raw_text": "Basic Warranty\nOriginal warranty estimated to have 15 months or 34,501 miles remaining.\nWarranty Active\nLast reported odometer reading\n15,499"}
r = AG.carfax_warranty_estimate(nodate)
check("no report date: no sentence, flagged", not r["ok"] and "not found" in r["reason"] and r["flags"], r)
short = report("Original warranty estimated to have 2 months or 30,000 miles remaining.", ["Active"], asof, ["20,000"])
check("under 3 months: no sentence", "under 3" in (AG.factory_warranty_remaining("2023 Volvo S60", {"mileage": 21000}, short)["reason"] or ""))
check("0 miles left: no sentence", not AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 50000}, volvo)["ok"])
check("miles rounded down to 100", AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 19801}, volvo)["miles"] == 30100)
latest_exp = report("Original warranty estimated to have 15 months or 34,501 miles remaining.", ["Active", "Expired"], asof, ["15,499"])
s = AG.factory_warranty_sentence(AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 19878}, latest_exp))
check("no transfer clause in any case", s and s.endswith("whichever comes first.") and "transfer" not in s, s)
nolabel = report("Original warranty estimated to have 15 months or 34,501 miles remaining.", ["Active"], asof, ["15,499"])
nolabel["raw_text"] = nolabel["raw_text"].replace("Basic Warranty", "Manufacturer Warranty")
e2 = AG.carfax_warranty_estimate(nolabel)
s = AG.factory_warranty_sentence(AG.factory_warranty_remaining("2024 Volvo S60", {"mileage": 19878}, nolabel))
check("row label not 'Basic Warranty': no 'basic' (label parsed)", e2["label"] == "Manufacturer Warranty"
      and s == "CARFAX estimates about 15 months remain on the original Volvo warranty, or about 30,100 miles at the current odometer, whichever comes first.", s)
check("row label 'Basic Warranty' parsed", AG.carfax_warranty_estimate(volvo)["label"] == "Basic Warranty")
check("two-word make", AG._warranty_make("2023 Land Rover Defender 110") == "Land Rover")
check("odometer 0 blocks the build (gate kept)", AG._odometer_gate("X", "X", 10, {"mileage": 0})["failed_source"] == "odometer")
check("below-Carfax flag kept", AG.odometer_flags({"mileage": 9000}, {"last_reported_odometer": 9600})[0]["kind"] == "below_carfax")

# placement --------------------------------------------------------------------
ws = "CARFAX estimates about 27 months remain on the original Hyundai basic warranty, or about 47,500 miles at the current odometer, whichever comes first."
p3_11 = ("Every Hendrick Certified vehicle passes a comprehensive 260-point inspection performed by Hendrick-certified "
         "technicians before it is offered for sale. Tires and brakes must be above half-life. The Hendrick Certified "
         "Limited Powertrain Warranty covers the engine.")
ad = f"P1.\n\nP2 sentence.\n\n{p3_11}\n\nCloser."
out = A.split_ad_paragraphs(A.insert_required_sentences(ad, {"factory_warranty_sentence": ws}, ["factory_warranty_sentence"], status_code=11, stock="T"))
check("11 guard: after the services sentence, before the program warranty sentence",
      "Tires and brakes must be above half-life. " + ws + " The Hendrick Certified Limited" in out["paragraph_three"]
      and out["paragraph_two"] == "P2 sentence.", out["paragraph_three"])
p3_12 = ("Every Hendrick Affordable vehicle passes a thorough 178-point inspection performed by Hendrick-certified technicians "
         "before it is offered for sale. The Hendrick Affordable Limited Powertrain Warranty covers the engine.")
out12 = A.hendrick_paragraph_three_with_factory_warranty(p3_12, ws)
check("12: no services sentence, so right before the program warranty (after inspection)",
      "before it is offered for sale. " + ws + " The Hendrick Affordable Limited" in out12, out12)
old_text = out["paragraph_three"].replace(ws, "Original Hyundai factory warranty has about 27 months and about 47,500 miles remaining, whichever comes first, and transfers to the new owner.")
re_p3 = A.hendrick_paragraph_three_with_factory_warranty(old_text, ws.replace("27", "24"))
check("11/12 reprice rebuild replaces an older-wording factory sentence",
      "Original Hyundai factory warranty" not in re_p3 and re_p3.count("CARFAX estimates") == 1 and "about 24 months" in re_p3)
check("factory sentence starts the date stamp; program sentences don't",
      A._FACTORY_WARRANTY_RE.match(ws) and not A._FACTORY_WARRANTY_RE.match("The powertrain warranty runs through January 1, 2032."))
check("reprice recognizes the new wording as a warranty sentence", bool(A._WARRANTY_START_RE.match(ws)))
asis_p3 = ("Before this vehicle was offered for sale, our service team completed a thorough 260-point inspection. "
           "All overdue manufacturer-recommended services were completed prior to delivery. "
           "This vehicle is sold without dealer warranty or roadside assistance. A CARFAX Vehicle History Report is included with every purchase.")
out13 = A.asis_paragraph_three_with_warranty(asis_p3, ws)
check("As-Is: after the services sentence, 'sold without' line removed",
      "prior to delivery. " + ws + " A CARFAX" in out13 and "sold without" not in out13, out13)
h = {}
A.record_ad(h, "PX", ad_text="P1.\n\nThe powertrain warranty runs through January 1, 2032.\n\nP3.\n\nCloser.", price=1,
            lifecycle_stage="active", recon_included=True, recon_pending=False, today="2026-10-04")
check("program warranty alone sets no warranty_sentence_date", "warranty_sentence_date" not in h["PX"])
A.record_ad(h, "PX", ad_text=f"P1.\n\nP2.\n\nInspection done. {ws}\n\nCloser.", price=1,
            lifecycle_stage="active", recon_included=True, recon_pending=False, today="2026-10-04")
check("factory sentence sets warranty_sentence_date", h["PX"].get("warranty_sentence_date") == "2026-10-04")

# inventory note at 30 days ------------------------------------------------------
import app as APP  # noqa: E402
fake = copy.deepcopy(APP.load_ad_history())
fake["P14497"]["warranty_sentence_date"] = (today - timedelta(days=31)).isoformat()
fake["PM53873"]["warranty_sentence_date"] = (today - timedelta(days=29)).isoformat()
APP.load_ad_history = lambda: fake
APP.app.config["TESTING"] = True
cl = APP.app.test_client()
with cl.session_transaction() as sess:
    sess["authed"] = True
html = cl.get("/inventory").get_data(as_text=True)
check("inventory: note at 31 days, not at 29", html.count("(over 30 days); reprice to refresh") == 1, html.count("over 30 days"))

# the five cars ----------------------------------------------------------------------
snap = {v["stock_number"]: v for v in json.load(open(WT + r"\last_inventory_snapshot.json"))["vehicles"]}
con = sqlite3.connect(f"file:{WT.replace(chr(92), '/')}/vehicle_cache.db?mode=ro", uri=True)


def cf_for(stock):
    return json.loads(con.execute("select carfax_json from vehicle_data where vin=?", (snap[stock]["vin"],)).fetchone()[0])


print("\n=== final sentences ===")
for stock in ("V23409A", "PM90574A", "P21297", "PM93165"):
    v = snap[stock]
    fw = AG.factory_warranty_remaining(v["year_make_model"], {"mileage": v["mileage"]}, cf_for(stock))
    est = fw["estimate"]
    print(f"{stock} ({v['year_make_model']}, status {v['status_code']}, lot {v['mileage']:,}): Carfax {est['months_at_report']} mo / "
          f"{est['miles_at_report']:,} mi as of {est['as_of']}, last odometer {est['last_odometer']:,} ({est['odometer_source']}), "
          f"term {est['term_miles']:,}, owners {'/'.join(est['statuses'])}")
    print("   -> " + (AG.factory_warranty_sentence(fw) or f"none: {fw['reason']}"))

print("\n=== rendered paragraph three ===")
from system_prompt_as_is import AS_IS_PROMPT  # noqa: E402
from system_prompt_hendrick_affordable import HENDRICK_AFFORDABLE_PROMPT  # noqa: E402
from system_prompt_hendrick_certified import HENDRICK_CERTIFIED_PROMPT  # noqa: E402


def fixed(prompt, heading):
    return re.search(heading + r'.*?\n"((?:Every|Before) .*?)"\n', prompt, re.S).group(1)


def sent_for(stock):
    v = snap[stock]
    return AG.factory_warranty_sentence(AG.factory_warranty_remaining(v["year_make_model"], {"mileage": v["mileage"]}, cf_for(stock)))


cert = fixed(HENDRICK_CERTIFIED_PROMPT, "PARAGRAPH THREE — HENDRICK CERTIFIED WARRANTY BLOCK")
print(f"\nHendrick Certified — PM90574A {snap['PM90574A']['year_make_model']}:\n" + cert.replace("[FACTORY WARRANTY SENTENCE] ", sent_for("PM90574A") + " "))
twelves = [s for s, v in snap.items() if v["status_code"] == 12]
live12 = [s for s in twelves if sent_for(s)]
print(f"\nstatus-12 cars with a live Carfax estimate: {live12 or 'none'} (of {len(twelves)})")
aff = fixed(HENDRICK_AFFORDABLE_PROMPT, "PARAGRAPH THREE — HENDRICK AFFORDABLE WARRANTY BLOCK")
print("\nHendrick Affordable — ILLUSTRATION ONLY (no status-12 car qualifies today; DT23358A's estimate used):\n"
      + aff.replace("[FACTORY WARRANTY SENTENCE] ", sent_for("DT23358A") + " "))
asis = fixed(AS_IS_PROMPT, "INSPECTED SUB-CATEGORY:\n")
print(f"\nAs-Is — V23409A {snap['V23409A']['year_make_model']} (inspected sub-category):\n"
      + asis.replace("[WARRANTY SENTENCE] ", sent_for("V23409A") + " "))

print()
print("FAILED:" if FAIL else "ALL PASSED", FAIL if FAIL else "")
sys.exit(1 if FAIL else 0)
