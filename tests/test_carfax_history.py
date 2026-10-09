"""carfax_history: text-first accident / damage / service verdicts, the
claim scrub (Python sentence and the model's paragraph-one variant), and
build_carfax_sentence's no-claim rule. Synthetic reports in the stored
raw_text shape (newline-separated fields). Offline; nothing saved."""
import sys
import unittest

import _paths  # noqa: F401  (repo root first on sys.path)
import carfax_history as H  # noqa: E402
from aggregator import build_carfax_sentence  # noqa: E402

ROWS = {
    "Total Loss": "No Issues Reported", "Structural Damage": "No Issues Reported",
    "Airbag Deployment": "No Issues Reported", "Odometer Check": "No Issues Indicated",
    "Accident / Damage": "No Issues Reported",
}
DEALER = ["Mercedes-Benz of Raleigh", "Raleigh, North Carolina", "Raleigh, NC", "Raleigh, North Carolina", "919-876-5444"]


def report(badge=None, rows=None, history=(), brands="No Problem"):
    """A report: header (optional badge), summary rows, title rows, then
    Detailed History rows given as (date, miles, [fields...])."""
    r = dict(ROWS, **(rows or {}))
    out = ["Print", "2025 MERCEDES-BENZ GLC GLC 300 4MATIC", "Vehicle Details"]
    if badge:
        out += badge.split("|")
    out += ["CARFAX VALUE", "$52,120"]
    for k, v in r.items():
        out += [k, "Some explanatory text.", v]
    out += ["Damage Brands", "Salvage", "Junk", "Guaranteed", brands, "Odometer Brands", "Not Actual Mileage",
            "Guaranteed", "No Problem"]
    out += ["Detailed History", "Owner 1", "Date", "Mileage", "Source", "Comments"]
    for d, mi, fields in history:
        out += [d, mi] + list(fields)
    out += ["Have Questions?"]
    return "\n".join(out)


GOOD_SERVICE = [
    ("09/26/2025", "13", DEALER + ["Vehicle serviced", "Pre-delivery inspection completed"]),
    ("08/03/2026", "5,812", DEALER + ["Vehicle serviced", "Maintenance inspection completed", "Oil and filter changed"]),
]


class HistoryTests(unittest.TestCase):
    def test_clean(self):
        h = H.carfax_history(report(history=GOOD_SERVICE), "Mercedes-Benz")
        self.assertTrue(h["parsed"])
        self.assertTrue(h["clean"], h["reasons"])
        self.assertTrue(h["service_ok"], h["service_reasons"])

    def test_damage_row_with_a_clean_summary_row_is_not_clean(self):
        # P51460 / PS47294 / V23409A: summary reads "No Issues Reported", the detailed history lists damage.
        hist = GOOD_SERVICE + [("02/26/2026", "6,100", ["Damage reported: minor damage", "Damage to front"])]
        h = H.carfax_history(report(history=hist), "Mercedes-Benz")
        self.assertFalse(h["clean"])
        self.assertEqual((h["accident_count"], h["damage_count"]), (0, 1))
        self.assertEqual(h["events"][0]["severity"], "minor")

    def test_accident_row(self):
        hist = [("08/11/2026", "25,000", ["Accident reported", "Damage to left side"])]
        h = H.carfax_history(report(badge="ACCIDENT|Accident Reported", rows={"Accident / Damage": "Accident Reported"},
                                    history=hist), "Mercedes-Benz")
        self.assertFalse(h["clean"])
        self.assertEqual(h["accident_count"], 1)
        self.assertEqual(h["badge"]["accident"], "Accident Reported")

    def test_badge_or_row_alone_is_not_clean(self):
        self.assertFalse(H.carfax_history(report(badge="DAMAGE|Damage Reported"))["clean"])
        self.assertFalse(H.carfax_history(report(rows={"Structural Damage": "Damage Reported"}))["clean"])
        self.assertFalse(H.carfax_history(report(brands="Salvage"))["clean"])

    def test_unparsed_text_makes_no_claim(self):
        for raw in (None, "", "Print\nVehicle Details\nsomething else"):
            h = H.carfax_history(raw, "Mercedes-Benz")
            self.assertFalse(h["parsed"])
            self.assertFalse(h["clean"])
            self.assertFalse(h["service_ok"])


class ServiceTests(unittest.TestCase):
    def svc(self, hist, make="Mercedes-Benz"):
        return H.carfax_history(report(history=hist), make)

    def test_one_record_fails(self):
        self.assertFalse(self.svc(GOOD_SERVICE[1:])["service_ok"])

    def test_only_pre_delivery_and_delivery_day_fails(self):
        hist = [GOOD_SERVICE[0], ("10/06/2025", "16", DEALER + ["Vehicle serviced", "Maintenance inspection completed"])]
        self.assertFalse(self.svc(hist)["service_ok"])

    def test_maintenance_described_counts_even_under_1000_miles(self):
        hist = [GOOD_SERVICE[0], ("08/03/2026", "58", DEALER + ["Vehicle serviced", "Maintenance inspection completed"])]
        self.assertTrue(self.svc(hist)["service_ok"])

    def test_independent_shop_fails_and_state_inspection_is_ignored(self):
        shop = ["Jiffy Lube", "Cary, North Carolina", "Cary, NC", "Cary, North Carolina", "919-461-0510"]
        self.assertFalse(self.svc(GOOD_SERVICE + [("03/17/2026", "6,975", shop + ["Vehicle serviced", "Oil and filter changed"])])["service_ok"])
        h = self.svc(GOOD_SERVICE + [("03/17/2026", "6,975", shop + ["Vehicle serviced", "Safety inspection performed"])])
        self.assertTrue(h["service_ok"], h["service_reasons"])

    def test_facility_after_a_stray_location_line(self):
        row = ["Burlington, North Carolina", "Strickland Brothers 10 Minute Oil Change", "Burlington, North Carolina",
               "Burlington, NC"]
        self.assertEqual(H._facility(" | ".join(row)), "Strickland Brothers 10 Minute Oil Change")

    def test_carfax_promo_row_is_not_a_record(self):
        promo = ("09/20/2026", "not reported", ["CARFAX Car Care", "Manufacturer Recommended Maintenance Schedules"])
        self.assertEqual(len(self.svc([promo])["service"]), 0)

    def test_dealer_by_make_and_alias(self):
        self.assertTrue(H.dealer_matches("Hendrick Honda", "Honda"))
        self.assertFalse(H.dealer_matches("West Herr BMW MINI Buffalo", "Mercedes-Benz"))
        self.assertTrue(H.dealer_matches("RBM of Atlanta, Inc", "Mercedes-Benz"))


class ScrubTests(unittest.TestCase):
    def test_python_sentence(self):
        p = ("One owner, personal use confirmed by Carfax. Clean vehicle history, with all service performed at "
             "authorized Mercedes-Benz dealers. Averaging 9,946 miles per year.")
        r = H.scrub_claims(p, history_clean=False, service_ok=False)
        self.assertEqual(r["text"], "One owner, personal use confirmed by Carfax. Averaging 9,946 miles per year.")
        self.assertEqual(len(r["removed"]), 1)
        # Clean history but the service records don't hold up: only the service clause goes.
        r = H.scrub_claims(p, history_clean=True, service_ok=False)
        self.assertIn("Clean vehicle history. Averaging", r["text"])
        self.assertNotIn("authorized", r["text"])

    def test_history_with_mileage(self):
        r = H.scrub_claims("Clean vehicle history, averaging 4,346 miles per year against the national average.", False, True)
        self.assertEqual(r["text"], "Averaging 4,346 miles per year against the national average.")

    def test_model_variant_in_paragraph_one(self):
        p = "Two owners, personal use with clean vehicle history confirmed by Carfax. Reconditioning complete."
        r = H.scrub_claims(p, False, True)
        self.assertEqual(r["text"], "Two owners, personal use. Reconditioning complete.")

    def test_unknown_shape_is_a_hand_edit(self):
        r = H.scrub_claims("Buy with confidence: no accidents ever, says the report.", False, True)
        self.assertEqual(r["manual"], ["Buy with confidence: no accidents ever, says the report."])

    def test_dangling_reference_flagged(self):
        r = H.scrub_claims("Clean vehicle history. That clean history makes it a rare find.", False, True)
        self.assertEqual(r["dangling"], ["That clean history makes it a rare find."])

    def test_claim_problems(self):
        self.assertEqual(H.claim_problems("A clean Carfax. Nice wheels.", False, True), ["A clean Carfax."])
        self.assertEqual(H.claim_problems("A clean Carfax. Nice wheels.", True, True), [])


class SentenceTests(unittest.TestCase):
    def cf(self, raw):
        return H.apply_text_history({"raw_text": raw, "miles_per_year": 6000, "number_of_owners": 1}, "Mercedes-Benz")

    def test_damage_gets_no_history_claim(self):
        hist = GOOD_SERVICE + [("02/26/2026", "6,100", ["Damage reported: minor damage"])]
        s = build_carfax_sentence(self.cf(report(history=hist)), 10, make="Mercedes-Benz")
        self.assertNotRegex(s or "", r"(?i)clean|accident|damage")
        self.assertIn("6,000 miles per year", s)

    def test_clean_gets_the_claim(self):
        s = build_carfax_sentence(self.cf(report(history=GOOD_SERVICE)), 10, make="Mercedes-Benz")
        self.assertIn("Clean vehicle history", s)

    def test_vision_cannot_override_text(self):
        hist = [("06/11/2025", "20,000", ["Accident reported: minor damage"])]
        cf = {"raw_text": report(history=hist), "no_accidents": True, "accident_count": 0}  # vision's answer
        cf = H.apply_text_history(cf, "Mercedes-Benz")
        self.assertFalse(cf["no_accidents"])
        self.assertEqual(cf["accident_count"], 1)
        self.assertFalse(cf["clean_history"])


if __name__ == "__main__":
    unittest.main()
