"""Pass 0 (fueleconomy.gov): matching without a model "designation", on RECORDED EPA responses
(tests/fixtures/epa_recorded.json: what the data service returned on 2026-10-10), and the make tables that
the range lookups use (manufacturer domains, direct-sale makes, dealer name patterns). Offline."""
import json
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401  (repo root first on sys.path; temp cost log)
import powertrain as P  # noqa: E402

REC = json.loads((Path(__file__).parent / "fixtures" / "epa_recorded.json").read_text(encoding="utf-8"))["responses"]
CALLS = []


def key(path, params):
    return path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))


def replay(path, params=None):
    """The recorded service: a request that was not recorded is a test failure, not a silent None."""
    k = key(path, params)
    CALLS.append(k)
    if k not in REC:
        raise AssertionError(f"unrecorded EPA request {k}")
    return REC[k]


def lookup(year, make, model, trim, cls, body=None, extra=""):
    return P.epa_lookup(year, make, model, trim, cls, body_style=body, extra_text=extra, fetch=replay)


class Positives(unittest.TestCase):
    def check(self, miles, *args, **kw):
        r = lookup(*args, **kw)
        self.assertEqual((r["status"], r.get("miles")), ("match", miles), r)
        self.assertIn("fueleconomy.gov/feg/Find.do?action=sbs&id=", r["url"])
        return r

    def test_rav4_prime_has_no_designation_and_still_matches(self):
        self.assertEqual(P.designations("RAV4 Prime SE"), set(), "no letter+2-3 digit designation in the name")
        r = self.check(42, 2022, "Toyota", "RAV4 Prime", "SE", P.PHEV, "SUV")
        self.assertIn("RAV4 Prime", r["matched_trim"])

    def test_wrangler_4xe(self):
        r = self.check(22, 2023, "Jeep", "Wrangler", "Unlimited Sahara 4xe", P.PHEV, "SUV")
        self.assertIn("4xe", r["matched_trim"])

    def test_glc_350e(self):
        self.check(54, 2025, "Mercedes-Benz", "GLC", "GLC 350e 4MATIC SUV", P.PHEV, "SUV")

    def test_s60_t8(self):
        self.check(40, 2024, "Volvo", "S60", "Recharge Plug-In Hybrid T8 AWD", P.PHEV, "Sedan")

    def test_g_580(self):
        self.check(239, 2025, "Mercedes-Benz", "G-Class", "G 580 with EQ Technology", P.BEV, "SUV")

    def test_g_580e_as_the_dealer_system_writes_it(self):
        self.check(239, 2025, "Mercedes-Benz", "G-Class", "G 580e AWD", P.BEV, "SUV")

    def test_model_x_long_range_plus_beats_long_range(self):
        self.check(351, 2020, "Tesla", "Model X", "Long Range Plus", P.BEV, "SUV")
        self.check(328, 2020, "Tesla", "Model X", "Long Range", P.BEV, "SUV")

    def test_e_53_hybrid_wagon_and_sedan(self):
        r = self.check(41, 2026, "Mercedes-Benz", "E-Class", "AMG E 53 Hybrid 4MATIC+ Wagon", P.PHEV, "Wagon")
        self.assertIn("station wagon", r["matched_trim"])
        self.check(44, 2026, "Mercedes-Benz", "E-Class", "AMG E 53 Hybrid 4MATIC+", P.PHEV, "Sedan")

    def test_the_body_can_come_from_the_trim_when_the_snapshot_has_none(self):
        self.check(41, 2026, "Mercedes-Benz", "E-Class", "AMG E 53 Hybrid 4MATIC+ Wagon", P.PHEV, None)


class MustNotMatch(unittest.TestCase):
    def test_a_gas_wrangler_is_not_the_4xe(self):
        r = lookup(2023, "Jeep", "Wrangler", "Unlimited Sahara", P.PHEV, "SUV")
        self.assertEqual(r["status"], "none", r)

    def test_a_non_prime_rav4_is_not_the_prime(self):
        r = lookup(2022, "Toyota", "RAV4", "XLE Premium", P.PHEV, "SUV")
        self.assertEqual(r["status"], "none", r)

    def test_only_plug_in_and_electric_records_count(self):
        # the car says Wrangler and the PHEV class; 'Wrangler 4dr 4WD' fits the words but its record is not a plug-in
        r = lookup(2023, "Jeep", "Wrangler", "Unlimited Sahara", P.PHEV, "SUV")
        self.assertNotEqual(r.get("miles"), 22)

    def test_a_year_with_no_entry_is_none_and_names_the_other_years(self):
        r = lookup(2023, "Mercedes-Benz", "EQE", "EQE 500 4MATIC SUV", P.BEV, "SUV")
        self.assertEqual(r["status"], "none")
        self.assertIn("2024 EQE 500 4matic (SUV)", r["note"])
        self.assertIn("not used", r["note"])

    def test_an_unknown_body_leaves_two_candidates_and_is_ambiguous(self):
        r = lookup(2026, "Mercedes-Benz", "E-Class", "AMG E 53 Hybrid 4MATIC+", P.PHEV, None)
        self.assertEqual(r["status"], "ambiguous", r)
        self.assertIn("[44] mi", r["note"])
        self.assertIn("[41] mi", r["note"])
        self.assertNotIn("miles", r)

    def test_sticker_text_alone_never_makes_a_match(self):
        # The sticker may supply a secondary word (the Volvo's "T8" is only there), but at least one distinctive word
        # has to come from the car's own model / trim: a "Zorp" whose sticker mentions "RAV4 Prime" is not one.
        r = lookup(2022, "Toyota", "Zorp", "XLE", P.PHEV, "SUV", extra="traded in against a RAV4 Prime prime time")
        self.assertEqual(r["status"], "none", r)


class Synthetic(unittest.TestCase):
    """Small hand-made EPA menus for the drivetrain filter and disagreeing records."""

    def fake(self, names, records):
        def fetch(path, params=None):
            if path == "menu/model":
                return {"menuItem": [{"text": n, "value": n} for n in names]}
            if path == "menu/options":
                return {"menuItem": [{"text": t, "value": v} for t, v in names[params["model"]]]}
            return records.get(path)
        return fetch

    def test_the_records_drivetrain_must_agree_with_the_car(self):
        names = {"Zed 400": [("rwd", "1"), ("awd", "2")]}
        recs = {"1": {"id": "1", "atvType": "EV", "range": "300", "drive": "Rear-Wheel Drive"},
                "2": {"id": "2", "atvType": "EV", "range": "270", "drive": "All-Wheel Drive"}}
        f = self.fake(names, recs)
        awd = P.epa_lookup(2025, "Zedmake", "Zed", "400 AWD", P.BEV, fetch=f)
        rwd = P.epa_lookup(2025, "Zedmake", "Zed", "400 RWD", P.BEV, fetch=f)
        unknown = P.epa_lookup(2025, "Zedmake", "Zed", "400", P.BEV, fetch=f)
        self.assertEqual((awd["miles"], rwd["miles"]), (270, 300))
        self.assertEqual(unknown["status"], "ambiguous", "two records, two ranges, no drivetrain to choose by")

    def test_records_that_disagree_are_ambiguous_not_conflict(self):
        names = {"Volt 500": [("19in", "1"), ("21in", "2")]}
        recs = {"1": {"id": "1", "atvType": "EV", "range": "310"}, "2": {"id": "2", "atvType": "EV", "range": "270"}}
        r = P.epa_lookup(2025, "Testmake", "Volt", "500 AWD", P.BEV, fetch=self.fake(names, recs))
        self.assertEqual(r["status"], "ambiguous")
        self.assertIn("conflicting ranges", r["note"])

    def test_an_ambiguous_epa_answer_falls_through_to_the_manufacturer_search(self):
        called = []

        def epa(*a, **k):
            return {"status": "ambiguous", "note": "fueleconomy.gov 2026: ambiguous - more than one candidate fits (a; b)"}

        def mfr(year, make, model, trim, cls, domains):
            called.append(domains)
            return {"miles": 41, "matched_trim": "2026 E 53", "url": "https://www.mbusa.com/x", "note": None}

        r = P.electric_range(2026, "Mercedes-Benz", "E-Class", "AMG E 53", P.PHEV, epa=epa, manufacturer=mfr, use_cache=False)
        self.assertEqual((r["miles"], r["source"], len(called)), (41, "manufacturer", 1))

    def test_ambiguous_with_no_other_source_is_omitted_with_the_reason(self):
        epa = mock.Mock(return_value={"status": "ambiguous", "note": "fueleconomy.gov 2025: ambiguous - conflicting ranges (x)"})
        mfr = mock.Mock(return_value={"miles": None, "note": "manufacturer site has no 2025 figure for this trim"})
        r = P.electric_range(2025, "Volvo", "S60", "T8", P.PHEV, epa=epa, manufacturer=mfr, use_cache=False)
        self.assertIsNone(r["miles"])
        self.assertIn("ambiguous", r["flag"])


class MakeTables(unittest.TestCase):
    def test_buick_and_ram_have_a_manufacturer_domain(self):
        self.assertEqual(P.MANUFACTURER_DOMAINS["buick"], ["buick.com"])
        self.assertEqual(P.MANUFACTURER_DOMAINS["ram"], ["ramtrucks.com"])

    def test_direct_sale_makes_have_no_dealers_like_tesla(self):
        for make in ("Tesla", "Lucid", "Polestar", "Rivian"):
            self.assertFalse(P.has_franchised_dealers(make), make)
            self.assertEqual(P.dealer_pattern(make), "", make)
        for make in ("Volvo", "Mercedes-Benz", "Ford", "Genesis"):
            self.assertTrue(P.has_franchised_dealers(make), make)

    def test_dealer_name_patterns_for_bmw_genesis_honda_hyundai_mazda(self):
        import re

        for make, host in (("BMW", "bmwofraleigh.com"), ("Genesis", "genesisofdurham.com"), ("Honda", "hondaofdurham.com"),
                           ("Hyundai", "hyundaiofcary.com"), ("Mazda", "mazdaofdurham.com")):
            self.assertTrue(re.search(P.dealer_pattern(make), host, re.I), make)
            self.assertFalse(re.search(P.dealer_pattern(make), "randomblog.com", re.I), make)

    def test_a_make_with_no_pattern_falls_back_to_its_own_name(self):
        import re

        self.assertTrue(re.search(P.dealer_pattern("Kia"), "kiaofdurham.com", re.I))
        self.assertEqual(P.make_key("Land Rover"), "land")
        self.assertEqual(P.make_key("Mercedes-Benz"), "mercedes-benz")

    def test_every_range_domain_make_either_has_a_dealer_pattern_or_is_covered_by_the_fallback(self):
        for k in P.DIRECT_SALE_MAKES:
            self.assertIn(k, P.MANUFACTURER_DOMAINS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
