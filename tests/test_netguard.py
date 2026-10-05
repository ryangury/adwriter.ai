"""The network guard itself: an API call and a plain HTTP request are blocked,
and the attempt is recorded even when the caller swallows the error.
# netguard: expect-blocked
"""
import socket
import sys
import unittest
import urllib.request

sys.path.insert(0, r"C:\adwriter")


class NetguardTests(unittest.TestCase):
    def test_guard_is_installed(self):
        self.assertTrue(hasattr(socket, "NetworkBlocked"), "tests must run through tests/run_tests.py")

    def test_http_is_blocked(self):
        with self.assertRaises(OSError):
            urllib.request.urlopen("https://example.com", timeout=5)

    def test_anthropic_call_is_blocked_even_when_swallowed(self):
        import towing

        cfg = towing.vehicle_config("2024 Chevrolet Silverado 1500", "RST 4WD", "Truck",
                                    "CREW CAB SHORT BED ENG: DURAMAX 3.0L TURBO-DIESEL TRAILERING PACKAGE")
        with self.assertRaises(towing.TowLookupUnavailable):
            towing.lookup(cfg)  # the SDK's connection error becomes TowLookupUnavailable


if __name__ == "__main__":
    unittest.main(verbosity=2)
