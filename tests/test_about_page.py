"""The About page: no em or en dashes anywhere (template, generated schedule
table, rendered page), and it needs the same login as every other page.
Offline; renders through Flask's test client."""
import re
import sys
import unittest
from pathlib import Path

import _paths
ROOT = _paths.ROOT
sys.path.insert(0, str(ROOT))
import app as adapp  # noqa: E402

DASH_RE = re.compile("[\u2013\u2014]|&[mn]dash;|&#(?:8211|8212|x201[34]);", re.I)


class AboutPageTests(unittest.TestCase):
    def test_no_em_or_en_dash_in_the_templates(self):
        for name in ("about.html", "_about_schedule.html"):
            text = (ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertEqual(DASH_RE.findall(text), [], name)

    def test_rendered_page(self):
        adapp.app.config["TESTING"] = True
        client = adapp.app.test_client()
        r = client.get("/about")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"About This Tool</h1>", r.data)  # signed out: the login page, not the content
        with client.session_transaction() as sess:
            sess["authed"] = True
        html = client.get("/about").get_data(as_text=True)
        self.assertIn("About This Tool</h1>", html)
        self.assertIn("Last updated: October 6, 2026", html)
        self.assertIn("Schedule checked:", html)
        self.assertIn('href="/about">About</a>', html)
        self.assertEqual(DASH_RE.findall(html), [])


if __name__ == "__main__":
    unittest.main()
