"""billing.py with stubbed responses: one page, several pages (has_more /
next_page), 400, 401, 403, caching of successes only, cents to dollars, the
group_by[] array form, and the /cost page's error text. No network."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing as B  # noqa: E402

KEY = "sk-ant-admin-SECRET-KEY-VALUE"


def resp(status=200, body=None, text=None):
    r = SimpleNamespace(status_code=status, text=text or "")
    r.json = lambda: body if body is not None else (_ for _ in ()).throw(ValueError("no json"))
    return r


def cost_bucket(day, *items):
    return {"starting_at": f"2026-10-{day:02d}T00:00:00Z", "ending_at": f"2026-10-{day + 1:02d}T00:00:00Z",
            "results": [{"amount": a, "model": m, "cost_type": t} for a, m, t in items]}


S, H, W = ("1000.0", "claude-sonnet-4-6", "tokens"), ("200.5", "claude-haiku-4-5-20251001", "tokens"), ("50", None, "web_search")


class Stub:
    """requests.get stand-in: serves pages per URL and records each call."""

    def __init__(self, pages_by_url):
        self.pages, self.calls = pages_by_url, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, list(params), headers))
        queue = self.pages[url]
        return queue.pop(0) if isinstance(queue, list) else queue


def ok(buckets, more=False, nxt=None):
    return resp(200, {"data": buckets, "has_more": more, "next_page": nxt})


class Fetch(unittest.TestCase):
    def test_single_page(self):
        g = Stub({B.COST_URL: [ok([cost_bucket(1, S, H, W)])], B.USAGE_URL: [ok([])]})
        d = B.fetch_month(KEY, get=g)
        self.assertAlmostEqual(d["total"], 12.505)  # (1000 + 200.5 + 50) cents
        self.assertAlmostEqual(d["by_family"]["sonnet"], 10.0)
        self.assertAlmostEqual(d["by_family"]["haiku"], 2.005)
        self.assertAlmostEqual(d["by_family"]["web search"], 0.5)
        self.assertEqual(d["days"][0]["day"], "2026-10-01")
        self.assertEqual(len(g.calls), 2)

    def test_request_shape(self):
        g = Stub({B.COST_URL: [ok([])], B.USAGE_URL: [ok([])]})
        B.fetch_month(KEY, get=g)
        (curl, cparams, chead), (_, uparams, _h) = g.calls
        self.assertIn(("group_by[]", "description"), cparams)
        self.assertIn(("group_by[]", "model"), uparams)
        for p in (cparams, uparams):
            self.assertIn(("limit", 31), p)
            self.assertIn(("bucket_width", "1d"), p)
            self.assertTrue(any(k == "starting_at" and v.endswith("Z") for k, v in p))
            self.assertFalse(any(k == "group_by" for k, _ in p), "must use the array form group_by[]")
        self.assertEqual(chead["x-api-key"], KEY)

    def test_multiple_pages_followed_until_has_more_false(self):
        g = Stub({B.COST_URL: [ok([cost_bucket(1, S)], True, "page_A"), ok([cost_bucket(2, S)], True, "page_B"),
                               ok([cost_bucket(3, S)], False, None)],
                  B.USAGE_URL: [ok([])]})
        d = B.fetch_month(KEY, get=g)
        self.assertEqual([x["day"] for x in d["days"]], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertAlmostEqual(d["total"], 30.0)
        pages = [dict(c[1]).get("page") for c in g.calls if c[0] == B.COST_URL]
        self.assertEqual(pages, [None, "page_A", "page_B"])

    def test_usage_tokens_by_model(self):
        usage = [{"starting_at": "2026-10-01T00:00:00Z", "results": [
            {"model": "claude-sonnet-4-6", "uncached_input_tokens": 100, "output_tokens": 50},
            {"model": "claude-haiku-4-5-20251001", "uncached_input_tokens": 7}]}]
        g = Stub({B.COST_URL: [ok([])], B.USAGE_URL: [ok(usage)]})
        self.assertEqual(B.fetch_month(KEY, get=g)["tokens"], {"claude-sonnet-4-6": 150, "claude-haiku-4-5-20251001": 7})

    def test_other_family_and_bad_amount(self):
        p = B.parse_cost([cost_bucket(1, ("100", "claude-opus-5-5", "tokens"), ("x", "claude-sonnet-4-6", "tokens"))])
        self.assertAlmostEqual(p["by_family"]["other"], 1.0)
        self.assertEqual(p["by_family"]["sonnet"], 0.0)


class Errors(unittest.TestCase):
    def fail(self, status, msg, which=B.USAGE_URL):
        pages = {B.COST_URL: [ok([])], B.USAGE_URL: [ok([])]}
        pages[which] = resp(status, {"type": "error", "error": {"type": "x", "message": msg}})
        with self.assertRaises(B.BillingError) as cm:
            B.fetch_month(KEY, get=Stub(pages))
        return cm.exception

    def test_400_names_status_and_api_message_and_does_not_blame_the_key(self):
        e = self.fail(400, "Invalid parameter `group_by`. Use `group_by[]` for array parameters.")
        text = B.describe_error(e)
        self.assertIn("HTTP 400", text)
        self.assertIn("Invalid parameter `group_by`", text)
        self.assertIn("usage_report/messages", text)
        self.assertNotIn("ANTHROPIC_BILLING_COST_API_KEY", text)
        self.assertNotIn("Admin key", text)

    def test_401_and_403_blame_the_key(self):
        for status, msg in ((401, "invalid x-api-key"), (403, "Admin API key required")):
            text = B.describe_error(self.fail(status, msg, which=B.COST_URL))
            self.assertIn(f"HTTP {status}", text)
            self.assertIn(msg, text)
            self.assertIn("ANTHROPIC_BILLING_COST_API_KEY", text)

    def test_non_json_error_body(self):
        g = Stub({B.COST_URL: resp(502, None, text="Bad gateway")})
        with self.assertRaises(B.BillingError) as cm:
            B.fetch_all(B.COST_URL, [], KEY, get=g)
        self.assertIn("HTTP 502", B.describe_error(cm.exception))
        self.assertIn("Bad gateway", B.describe_error(cm.exception))

    def test_key_never_in_error_text(self):
        for status in (400, 401, 403):
            self.assertNotIn(KEY, B.describe_error(self.fail(status, "nope")))


class Cache(unittest.TestCase):
    def good(self):
        return Stub({B.COST_URL: [ok([cost_bucket(1, S)]), ok([cost_bucket(1, S)])], B.USAGE_URL: [ok([]), ok([])]})

    def test_success_cached_failure_not(self):
        cache = {"fetched_at": 0.0, "data": None}
        bad = Stub({B.COST_URL: resp(500, {"error": {"message": "overloaded"}})})
        r = B.get_billing(KEY, now_s=1000.0, get=bad, cache=cache)
        self.assertIsNone(r["data"])
        self.assertIn("HTTP 500", r["error"])
        self.assertIsNone(cache["data"], "a failure is never cached")
        g = self.good()
        r = B.get_billing(KEY, now_s=2000.0, get=g, cache=cache)
        self.assertIsNone(r["error"])
        n = len(g.calls)
        r2 = B.get_billing(KEY, now_s=2000.0 + 1800, get=g, cache=cache)  # inside the hour: no new calls
        self.assertEqual(len(g.calls), n)
        self.assertEqual(r2["fetched_at"], 2000.0)
        B.get_billing(KEY, now_s=2000.0 + 3700, get=g, cache=cache)  # past the hour: refetch
        self.assertGreater(len(g.calls), n)

    def test_failed_refresh_keeps_last_good_data_with_its_age(self):
        cache = {"fetched_at": 0.0, "data": None}
        B.get_billing(KEY, now_s=1000.0, get=self.good(), cache=cache)
        r = B.get_billing(KEY, now_s=1000.0 + 4000, get=Stub({B.COST_URL: resp(403, {"error": {"message": "no"}})}),
                          cache=cache)
        self.assertIn("HTTP 403", r["error"])
        self.assertIsNotNone(r["data"])
        self.assertEqual(r["fetched_at"], 1000.0)

    def test_missing_key(self):
        r = B.get_billing(None, cache={"fetched_at": 0.0, "data": None})
        self.assertIn("not set", r["error"])

    def test_age_text(self):
        self.assertEqual(B.age_text(1000.0, 1030.0), "just now")
        self.assertEqual(B.age_text(1000.0, 1000.0 + 600), "10 min ago")
        self.assertEqual(B.age_text(1000.0, 1000.0 + 3 * 3600 + 120), "3 h 2 min ago")
        self.assertIsNone(B.age_text(None))


class Page(unittest.TestCase):
    def test_cost_page_shows_error_and_data(self):
        import app as adapp
        from unittest import mock

        adapp.app.config["TESTING"] = True
        c = adapp.app.test_client()
        with c.session_transaction() as sess:
            sess["authed"] = True
        err = {"data": None, "fetched_at": None, "error": "Billing data unavailable: usage_report/messages returned HTTP 400: bad."}
        with mock.patch.object(adapp.billing, "get_billing", return_value=err):
            html = c.get("/cost").get_data(as_text=True)
        self.assertIn("HTTP 400: bad.", html)
        self.assertNotIn("check ANTHROPIC_BILLING_COST_API_KEY", html)
        data = B.parse_cost([cost_bucket(6, S, H, W), cost_bucket(7, S)])
        ok_ = {"data": {**data, "tokens": {}}, "fetched_at": 1.0, "error": None}
        with mock.patch.object(adapp.billing, "get_billing", return_value=ok_):
            html = c.get("/cost").get_data(as_text=True)
        for needle in ("Day (UTC)", "Web search", "2026-10-06", "2026-10-07", "$22.50"):
            self.assertIn(needle, html)
        # the relabelled cost-per-ad line (shown when this month has fresh ads)
        self.assertIn("rough estimate, includes testing", (Path(adapp.app.template_folder or "templates") / "cost.html"
                      if False else Path(__file__).resolve().parents[1] / "templates" / "cost.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
