import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from services.job_runner import (
        _capture_stage_from_stages as capture_stage_from_stages,
        _derive_capture_stages as derive_capture_stages,
        _expected_lite_review_target as expected_lite_review_target,
        _extract_capture_overrides as extract_job_runner_overrides,
        _should_retry_lite_capture_once as should_retry_lite_capture_once,
    )
except Exception:
    capture_stage_from_stages = None
    derive_capture_stages = None
    expected_lite_review_target = None
    extract_job_runner_overrides = None
    should_retry_lite_capture_once = None

try:
    from services.playwright_capture import PlaywrightCapture
except Exception:
    PlaywrightCapture = None

try:
    import app as app_module
except Exception:
    app_module = None

@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class ApiCaptureOverrideTests(unittest.TestCase):
    def test_listing_ingest_accepts_and_clamps_overrides(self) -> None:
        client = app_module.app.test_client()
        fake_job = {"job_id": "abc", "job_type": "listing_ingest", "status": "queued"}
        with patch.object(app_module.storage, "create_job", return_value=fake_job) as mocked_create_job:
            response = client.post(
                "/api/v1/listings/ingest",
                json={
                    "url": "https://www.airbnb.com/rooms/123",
                    "review_mode": "lite",
                    "capture_timeout_ms": 9999999,
                    "review_pagination_passes": 0,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked_create_job.call_count, 1)
        _, payload = mocked_create_job.call_args.args
        self.assertEqual(payload.get("capture_timeout_ms"), 600000)
        self.assertEqual(payload.get("review_pagination_passes"), 1)

    def test_search_accepts_timeout_but_not_review_pagination_override(self) -> None:
        client = app_module.app.test_client()
        fake_job = {"job_id": "abc", "job_type": "search", "status": "queued"}
        with patch.object(app_module.storage, "create_job", return_value=fake_job) as mocked_create_job:
            response = client.post(
                "/api/v1/search",
                json={
                    "location": "Keene, NY",
                    "capture_timeout_ms": 9000,
                    "review_pagination_passes": 12,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked_create_job.call_count, 1)
        _, payload = mocked_create_job.call_args.args
        self.assertEqual(payload.get("capture_timeout_ms"), 10000)
        self.assertNotIn("review_pagination_passes", payload)


class JobRunnerCaptureOverrideTests(unittest.TestCase):
    @unittest.skipIf(extract_job_runner_overrides is None, "Job runner dependencies are unavailable")
    def test_job_runner_override_sanitization(self) -> None:
        payload = {
            "capture_timeout_ms": "5000",
            "review_pagination_passes": "999",
        }
        out = extract_job_runner_overrides(payload)
        self.assertEqual(out.get("capture_timeout_ms"), 10000)
        self.assertEqual(out.get("review_pagination_passes"), 24)

    @unittest.skipIf(derive_capture_stages is None, "Job runner dependencies are unavailable")
    def test_capture_stage_progression_reaches_full(self) -> None:
        listing = {
            "title": "Cabin",
            "description": "Lake view",
            "pricing": {"price_total": 650.0},
        }
        stages = derive_capture_stages(
            listing,
            prior_listing=None,
            review_mode="full",
            reviews_captured=24,
            reviews_total=24,
        )
        self.assertTrue(stages.get("summary_ready"))
        self.assertTrue(stages.get("reviews_lite_ready"))
        self.assertTrue(stages.get("reviews_full_ready"))
        self.assertEqual(capture_stage_from_stages(stages), "reviews_full_ready")

    @unittest.skipIf(derive_capture_stages is None, "Job runner dependencies are unavailable")
    def test_capture_stage_is_monotonic_with_prior_listing(self) -> None:
        prior = {
            "capture_stages": {
                "summary_ready": True,
                "reviews_lite_ready": True,
                "reviews_full_ready": False,
            },
            "capture_stage": "reviews_lite_ready",
        }
        listing = {}
        stages = derive_capture_stages(
            listing,
            prior_listing=prior,
            review_mode="none",
            reviews_captured=0,
            reviews_total=20,
        )
        self.assertTrue(stages.get("summary_ready"))
        self.assertTrue(stages.get("reviews_lite_ready"))
        self.assertFalse(stages.get("reviews_full_ready"))
        self.assertEqual(capture_stage_from_stages(stages), "reviews_lite_ready")

    @unittest.skipIf(expected_lite_review_target is None, "Job runner dependencies are unavailable")
    def test_expected_lite_review_target_caps_to_total(self) -> None:
        self.assertEqual(expected_lite_review_target(24, 13, default_limit=24), 13)
        self.assertEqual(expected_lite_review_target(None, None, default_limit=24), 24)

    @unittest.skipIf(should_retry_lite_capture_once is None, "Job runner dependencies are unavailable")
    def test_should_retry_lite_capture_once_only_for_weak_lite_outcomes(self) -> None:
        should_retry = should_retry_lite_capture_once(
            review_mode="lite",
            review_only=False,
            reviews_captured=2,
            reviews_total=203,
            review_limit=24,
            capture_metrics={"review_response_count": 1},
            default_limit=24,
        )
        self.assertTrue(should_retry)

        should_not_retry = should_retry_lite_capture_once(
            review_mode="lite",
            review_only=False,
            reviews_captured=24,
            reviews_total=203,
            review_limit=24,
            capture_metrics={"review_response_count": 2},
            default_limit=24,
        )
        self.assertFalse(should_not_retry)

        moderate_under_capture = should_retry_lite_capture_once(
            review_mode="lite",
            review_only=False,
            reviews_captured=4,
            reviews_total=29,
            review_limit=24,
            capture_metrics={"review_response_count": 3},
            default_limit=24,
        )
        self.assertTrue(moderate_under_capture)

        low_total_low_upside_no_retry = should_retry_lite_capture_once(
            review_mode="lite",
            review_only=False,
            reviews_captured=2,
            reviews_total=5,
            review_limit=24,
            capture_metrics={"review_response_count": 1},
            default_limit=24,
        )
        self.assertFalse(low_total_low_upside_no_retry)

        no_reviews_no_retry = should_retry_lite_capture_once(
            review_mode="lite",
            review_only=False,
            reviews_captured=0,
            reviews_total=0,
            review_limit=24,
            capture_metrics={"review_response_count": 1},
            default_limit=24,
        )
        self.assertFalse(no_reviews_no_retry)

        low_total_fully_captured_no_retry = should_retry_lite_capture_once(
            review_mode="lite",
            review_only=False,
            reviews_captured=3,
            reviews_total=3,
            review_limit=24,
            capture_metrics={"review_response_count": 1},
            default_limit=24,
        )
        self.assertFalse(low_total_fully_captured_no_retry)


class PlaywrightCaptureOverrideTests(unittest.TestCase):
    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_override_resolution_with_reviews(self) -> None:
        capture = PlaywrightCapture(
            capture_timeout_ms=120000,
            review_wait_ms=5000,
            review_pagination_passes=6,
            review_page_wait_ms=1500,
        )
        out = capture._resolve_capture_overrides(
            {
                "capture_timeout_ms": 7000,
                "review_wait_ms": -1,
                "review_pagination_passes": 99,
                "review_page_wait_ms": 50000,
            },
            include_reviews=True,
        )
        self.assertEqual(out.get("capture_timeout_ms"), 10000)
        self.assertEqual(out.get("review_wait_ms"), 0)
        self.assertEqual(out.get("review_pagination_passes"), 24)
        self.assertEqual(out.get("review_page_wait_ms"), 10000)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_override_resolution_without_reviews_ignores_review_overrides(self) -> None:
        capture = PlaywrightCapture(
            capture_timeout_ms=120000,
            review_wait_ms=5000,
            review_pagination_passes=6,
            review_page_wait_ms=1500,
        )
        out = capture._resolve_capture_overrides(
            {
                "capture_timeout_ms": 110000,
                "review_wait_ms": 2000,
                "review_pagination_passes": 2,
                "review_page_wait_ms": 1200,
            },
            include_reviews=False,
        )
        self.assertEqual(out.get("capture_timeout_ms"), 110000)
        self.assertEqual(out.get("review_wait_ms"), 5000)
        self.assertEqual(out.get("review_pagination_passes"), 6)
        self.assertEqual(out.get("review_page_wait_ms"), 1500)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_navigation_and_lite_settle_budgets(self) -> None:
        capture = PlaywrightCapture(wait_after_load_ms=2500)
        self.assertEqual(capture._networkidle_grace_ms(), 2500)
        self.assertEqual(capture._review_settle_wait_ms("lite"), 400)
        self.assertEqual(capture._review_settle_wait_ms("full"), 2500)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_post_click_wait_budget_by_mode(self) -> None:
        capture = PlaywrightCapture(wait_after_load_ms=2500)
        self.assertEqual(capture._post_click_wait_ms("lite"), 400)
        self.assertEqual(capture._post_click_wait_ms("full"), 750)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_modal_wait_timeout_budget_by_mode(self) -> None:
        capture = PlaywrightCapture(wait_after_load_ms=2500)
        self.assertEqual(capture._review_modal_wait_timeout_ms("lite"), 2200)
        self.assertEqual(capture._review_modal_wait_timeout_ms("full"), 5000)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_should_skip_lite_modal_readiness_when_review_responses_sufficient(self) -> None:
        capture = PlaywrightCapture()
        responses = [
            {"url": "https://example.com/review?offset=0"},
            {"url": "https://example.com/reviews?offset=10"},
        ]
        self.assertTrue(capture._should_skip_lite_modal_readiness("lite", responses))
        self.assertFalse(capture._should_skip_lite_modal_readiness("full", responses))
        self.assertFalse(capture._should_skip_lite_modal_readiness("lite", [{"url": "https://example.com/review"}]))

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_listing_navigation_budget_helpers(self) -> None:
        capture = PlaywrightCapture(
            listing_navigation_wait_cap_ms=1800,
            listing_networkidle_fallback_ms=700,
        )
        self.assertEqual(capture._listing_navigation_wait_cap(2500), 1800)
        self.assertEqual(capture._listing_navigation_wait_cap(900), 900)
        self.assertEqual(capture._listing_networkidle_fallback_ms(2500), 700)
        self.assertEqual(capture._listing_networkidle_fallback_ms(600), 600)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_wait_for_response_threshold_immediate_when_target_met(self) -> None:
        capture = PlaywrightCapture()
        responses = [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]
        page = _DummyPage()
        out = asyncio.run(
            capture._wait_for_response_threshold(
                page,
                responses,
                target=2,
                max_wait_ms=1500,
            )
        )
        self.assertTrue(out.get("satisfied"))
        self.assertEqual(out.get("wait_ms"), 0)
        self.assertEqual(page.wait_calls, [])

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_wait_for_response_threshold_waits_until_target(self) -> None:
        capture = PlaywrightCapture(adaptive_wait_poll_ms=100)
        responses = []

        def on_wait(wait_calls, _ms):
            if len(wait_calls) == 1:
                responses.append({"url": "https://example.com/a"})
            if len(wait_calls) == 2:
                responses.append({"url": "https://example.com/b"})

        page = _DummyPage(on_wait=on_wait)
        out = asyncio.run(
            capture._wait_for_response_threshold(
                page,
                responses,
                target=2,
                max_wait_ms=800,
            )
        )
        self.assertTrue(out.get("satisfied"))
        self.assertGreaterEqual(out.get("wait_ms"), 100)
        self.assertGreaterEqual(len(page.wait_calls), 2)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_wait_for_response_threshold_times_out_when_not_met(self) -> None:
        capture = PlaywrightCapture(adaptive_wait_poll_ms=100)
        responses = []
        page = _DummyPage()
        out = asyncio.run(
            capture._wait_for_response_threshold(
                page,
                responses,
                target=2,
                max_wait_ms=250,
            )
        )
        self.assertFalse(out.get("satisfied"))
        self.assertEqual(out.get("wait_ms"), 250)

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_lite_pagination_skips_pulse_when_existing_review_responses_sufficient(self) -> None:
        capture = PlaywrightCapture(review_scroll_pulses=4, review_page_wait_ms=1500)
        responses = [
            {"url": "https://example.com/review?offset=0"},
            {"url": "https://example.com/reviews?offset=10"},
        ]
        page = _DummyPage()
        tracker = {"scroll_calls": 0}

        async def fake_scroll(_page):
            tracker["scroll_calls"] += 1
            return True

        capture._scroll_reviews_modal = fake_scroll  # type: ignore[method-assign]
        stats = asyncio.run(
            capture._paginate_reviews(
                page,
                responses,
                max_passes=0,
                review_page_wait_ms=1500,
            )
        )
        self.assertEqual(stats.get("stopped_reason"), "lite_mode_skip_existing_responses")
        self.assertEqual(stats.get("passes_executed"), 0)
        self.assertEqual(tracker["scroll_calls"], 0)
        self.assertEqual(page.wait_calls, [])

    @unittest.skipIf(PlaywrightCapture is None, "Playwright dependency is unavailable")
    def test_lite_pagination_single_pulse_when_review_responses_insufficient(self) -> None:
        capture = PlaywrightCapture(review_scroll_pulses=4, review_page_wait_ms=1500)
        responses = [{"url": "https://example.com/review?offset=0"}]
        page = _DummyPage()
        tracker = {"scroll_calls": 0}

        async def fake_scroll(_page):
            tracker["scroll_calls"] += 1
            return True

        capture._scroll_reviews_modal = fake_scroll  # type: ignore[method-assign]
        stats = asyncio.run(
            capture._paginate_reviews(
                page,
                responses,
                max_passes=0,
                review_page_wait_ms=1500,
            )
        )
        self.assertEqual(stats.get("stopped_reason"), "lite_mode_single_pulse")
        self.assertEqual(stats.get("passes_executed"), 0)
        self.assertEqual(tracker["scroll_calls"], 1)
        self.assertEqual(page.wait_calls, [capture._lite_pulse_wait_ms(1500)])


class _DummyPage:
    def __init__(self, on_wait=None) -> None:
        self.wait_calls = []
        self._on_wait = on_wait

    async def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)
        if callable(self._on_wait):
            self._on_wait(self.wait_calls, ms)


if __name__ == "__main__":
    unittest.main()
