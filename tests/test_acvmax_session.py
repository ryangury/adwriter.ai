"""Stub tests for the ACV MAX session fixes (no browser, no network, no email)."""
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, r"C:\adwriter")
import scraper  # noqa: E402
from scraper import (  # noqa: E402
    AcvMaxRunAbort,
    AcvMaxSessionExpiredError,
    ACVMaxScraper,
    LoginError,
    PricingNotFoundError,
)


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies
        self.cleared = 0

    def cookies(self):
        return list(self._cookies)

    def clear_cookies(self):
        self.cleared += 1
        self._cookies = []


def tgc(expires):
    return {"name": "TGC", "domain": "auth.firstlook.biz", "expires": expires}


def make_scraper(cookies):
    ax = ACVMaxScraper(headless=True, use_saved_session=True)
    ax._context = FakeContext(cookies)
    ax.page = mock.MagicMock()
    ax.calls = []
    ax._cas_login = lambda *, force: ax.calls.append(("cas_login", force))
    ax._select_dealership = lambda: ax.calls.append(("select",))
    ax._save_session = lambda: ax.calls.append(("save",))
    return ax


class TgcTests(unittest.TestCase):
    def _assert_fresh_login(self, cookies):
        ax = make_scraper(cookies)
        ax.login()
        self.assertEqual(ax._context.cleared, 1, "cookies cleared before the fresh login")
        self.assertEqual(ax.calls, [("cas_login", True), ("select",), ("save",)])
        ax.page.goto.assert_not_called()  # is_ready() stopped at the TGC check

    def test_expired_tgc_triggers_fresh_login(self):
        self._assert_fresh_login([tgc(time.time() - 60)])

    def test_missing_tgc_triggers_fresh_login(self):
        # what an expired TGC actually looks like: the browser drops it on load
        self._assert_fresh_login([{"name": "_ga", "domain": ".firstlook.biz", "expires": time.time() + 9e6}])

    def test_tgc_expiring_within_6h_triggers_fresh_login(self):
        self._assert_fresh_login([tgc(time.time() + 2 * 3600)])

    def test_valid_tgc_passes_the_check(self):
        ax = make_scraper([tgc(time.time() + 7 * 86400)])
        self.assertIsNone(ax._tgc_problem())


class FrameDetectionTests(unittest.TestCase):
    def test_cas_url_is_login(self):
        f = mock.MagicMock(url="https://auth.firstlook.biz/cas/login?service=x")
        self.assertTrue(scraper._is_cas_login_frame(f))

    def test_password_input_is_login(self):
        f = mock.MagicMock(url="https://max.firstlook.biz/other")
        f.locator.return_value.count.return_value = 1
        self.assertTrue(scraper._is_cas_login_frame(f))

    def test_normal_frame_is_not_login(self):
        f = mock.MagicMock(url="https://max.firstlook.biz/merchandising/PricingAnalysis/pingone")
        f.locator.return_value.count.return_value = 0
        self.assertFalse(scraper._is_cas_login_frame(f))
        self.assertFalse(scraper._is_cas_login_frame(None))

    def test_attach_raises_session_expired_on_login_frame(self):
        ax = make_scraper([])
        login_frame = mock.MagicMock(url="https://auth.firstlook.biz/cas/login?service=PricingAnalysis")
        handle = mock.MagicMock()
        handle.content_frame.return_value = login_frame
        ax.page.wait_for_selector.return_value = handle
        ax.page.frames = [login_frame]
        ax._dump_debug = lambda *a, **k: None
        with self.assertRaises(AcvMaxSessionExpiredError):
            ax._attach_merchandising_frame(
                "123", "merchandising/PricingAnalysis",
                error_cls=PricingNotFoundError, no_iframe_tag="a", no_frame_tag="b",
            )

    def test_attach_other_failure_keeps_old_error(self):
        ax = make_scraper([])
        blank = mock.MagicMock(url="about:blank")
        blank.locator.return_value.count.return_value = 0
        handle = mock.MagicMock()
        handle.content_frame.return_value = blank
        ax.page.wait_for_selector.return_value = handle
        ax.page.frames = [blank]
        ax._dump_debug = lambda *a, **k: None
        with self.assertRaises(PricingNotFoundError) as cm:
            ax._attach_merchandising_frame(
                "123", "merchandising/PricingAnalysis",
                error_cls=PricingNotFoundError, no_iframe_tag="a", no_frame_tag="b",
            )
        self.assertNotIsInstance(cm.exception, AcvMaxSessionExpiredError)


class RetryTests(unittest.TestCase):
    def _scraper(self, outcomes, relogin_exc=None):
        ax = make_scraper([])
        seq = list(outcomes)
        ax.relogins = 0

        def once(vid):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def relogin():
            ax.relogins += 1
            if relogin_exc:
                raise relogin_exc

        ax._open_pricing_once = once
        ax._relogin = relogin
        return ax

    def test_login_form_on_first_frame_triggers_one_relogin(self):
        ax = self._scraper([AcvMaxSessionExpiredError("form"), ("frame", "url")])
        self.assertEqual(ax.open_pricing("1"), ("frame", "url"))
        self.assertEqual(ax.relogins, 1)

    def test_second_login_form_aborts(self):
        ax = self._scraper([AcvMaxSessionExpiredError("form"), AcvMaxSessionExpiredError("form")])
        with self.assertRaises(AcvMaxRunAbort):
            ax.open_pricing("1")
        self.assertEqual(ax.relogins, 1)

    def test_failed_relogin_aborts(self):
        ax = self._scraper([AcvMaxSessionExpiredError("form")], relogin_exc=LoginError("bad creds"))
        with self.assertRaises(AcvMaxRunAbort):
            ax.open_pricing("1")

    def test_ordinary_frame_failure_is_not_retried(self):
        ax = self._scraper([PricingNotFoundError("never attached")])
        with self.assertRaises(PricingNotFoundError):
            ax.open_pricing("1")
        self.assertEqual(ax.relogins, 0)

    def test_abort_is_not_a_scraper_error(self):
        self.assertFalse(issubclass(AcvMaxRunAbort, scraper.ScraperError))


class PropagationTests(unittest.TestCase):
    def test_ctr_loop_lets_abort_through(self):
        import ctr_warmup
        acv = mock.MagicMock()
        acv.scrape_pricing.side_effect = AcvMaxRunAbort("signed out")
        with self.assertRaises(AcvMaxRunAbort):
            ctr_warmup.capture_durham_ctr(acv, [{"stock_number": "X1"}], dry_run=True)

    def test_run_aborts_with_alert(self):
        import orchestrator
        sent = []
        with mock.patch.object(orchestrator, "acquire_scraper_lock"), \
             mock.patch.object(orchestrator, "release_lock_if_owned", return_value=False), \
             mock.patch.object(orchestrator, "_run_inner", side_effect=AcvMaxRunAbort("frame still shows login")), \
             mock.patch.object(orchestrator, "_send_gmail", side_effect=lambda s, b: sent.append(s)):
            rc = orchestrator.run()
        self.assertEqual(rc, 1)
        self.assertEqual(sent, ["ACV Max scraping broken: frame still shows login"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
