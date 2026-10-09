"""tow_refresh's tow-clause cut (PS29810's sentence) and the implied cab / bed
for single-body trucks (Jeep Gladiator). Offline; nothing saved."""
import sys
import unittest

import _paths  # noqa: F401  (repo root first on sys.path)
import tow_refresh as R  # noqa: E402
import towing as T  # noqa: E402

PS29810 = ("The $1,900 Off-Road Package adds a fully variable all-wheel-drive system with a genuine low-range "
           "transmission, downhill speed regulation, and DYNAMIC SELECT off-road modes, and the factory Trailer "
           "Hitch enables a rated towing capacity of 7,716 pounds.")


class ClauseTests(unittest.TestCase):
    def test_ps29810_keeps_the_off_road_package(self):
        rest, how = R.strip_tow_clause(PS29810)
        self.assertEqual(how, "clause")
        self.assertEqual(rest, "The $1,900 Off-Road Package adds a fully variable all-wheel-drive system with a genuine "
                               "low-range transmission, downhill speed regulation, and DYNAMIC SELECT off-road modes.")
        self.assertEqual(T.tow_figures(rest), [])

    def test_shared_subject_and_trailing_clause(self):
        self.assertEqual(R.strip_tow_clause("Honda's i-VTM4 all-wheel drive is standard on this trim and rated to tow up to 5,000 pounds."),
                         ("Honda's i-VTM4 all-wheel drive is standard on this trim.", "clause"))
        self.assertEqual(R.strip_tow_clause("A front trunk adds practical storage, and the factory tow rating on this model is 5,000 pounds."),
                         ("A front trunk adds practical storage.", "clause"))

    def test_all_towing_sentence_goes_whole(self):
        for s in ("The factory Trailer Hitch is rated for 7,700 lbs of towing capacity.",
                  "A factory Trailer Hitch with Increased Towing Capacity is rated for 7,700 lbs, making this GLS "
                  "genuinely capable of pulling a boat, horse trailer, or camper."):
            self.assertEqual(R.strip_tow_clause(s), (None, "whole"))

    def test_no_clean_split_is_a_hand_edit(self):
        s = "Rated to tow 7,700 lbs with the factory hitch; the cabin seats seven adults in three rows."
        self.assertEqual(R.strip_tow_clause(s)[1], "manual")
        e = {"paragraph_one": "P1.", "paragraph_two": f"Opening. {s} Closing.", "paragraph_three": "P3.", "paragraph_four": "P4."}
        edit, removed = R._edit(e, "remove", None)
        self.assertEqual(edit["after"], edit["before"], "skipped: nothing changes")
        self.assertEqual(edit["manual"], [s])
        self.assertEqual(removed, [])

    def test_edit_cuts_only_the_clause_and_logs_the_old_sentence(self):
        e = {"paragraph_one": "P1.", "paragraph_two": f"Opening. {PS29810} Closing.", "paragraph_three": "P3.", "paragraph_four": "P4."}
        edit, removed = R._edit(e, "replace", "It is rated to tow up to 7,700 lbs.")
        p2 = edit["after"]["paragraph_two"]
        self.assertIn("DYNAMIC SELECT off-road modes. It is rated to tow up to 7,700 lbs. Closing.", p2)
        self.assertNotIn("7,716", p2)
        self.assertEqual(removed, [PS29810], "the original sentence becomes a stale phrase")


class ImpliedConfigTests(unittest.TestCase):
    def test_gladiator_cab_and_bed_implied(self):
        c = T.vehicle_config("2021 Jeep Gladiator", "Willys 4WD", "Truck",
                             "JEEP GLADIATOR WILLYS 4X4\nEngine: 3.6L V6 24V VVT Engine w/ESS")
        self.assertEqual((c["cab"], c["bed"], c["missing"]), ("crew", "short", []))
        self.assertIn("Stellantis", c["implied_cab_bed"])

    def test_printed_values_win_and_other_trucks_are_not_implied(self):
        c = T.vehicle_config("2022 Ram 2500", "Limited 4WD", "Truck", "RAM 2500 LIMITED CREW CAB 4X4\nEngine: 6.4L V8 Heavy Duty HEMI MDS Engine")
        self.assertEqual(c["missing"], ["bed"])
        self.assertIsNone(c["implied_cab_bed"])
        self.assertIsNone(T.implied_cab_bed("Chevrolet", "Silverado 1500"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
