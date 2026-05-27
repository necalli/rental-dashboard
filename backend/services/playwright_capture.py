import asyncio
import base64
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .config import RAW_DIR
from .search_builder import build_airbnb_search_url


REVIEW_URL_HINTS = ("review", "reviews")


class PlaywrightCapture:
    """
    Playwright-based capture that collects JSON network responses and page HTML.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        navigation_timeout_ms: int = 30000,
        wait_after_load_ms: int = 2500,
        capture_html: bool = True,
        response_domain_allowlist: Optional[List[str]] = None,
        response_url_allowlist: Optional[List[str]] = None,
        max_responses: int = 200,
        capture_timeout_ms: int = 120000,
        review_scroll_steps: int = 4,
        review_scroll_pulses: int = 4,
        review_wait_ms: int = 5000,
        review_pagination_passes: int = 6,
        review_page_wait_ms: int = 1500,
        review_only: bool = False,
        debug: bool = False,
        debug_screenshots: bool = False,
        block_resources: bool = False,
        blocked_resource_types: Optional[List[str]] = None,
        blocked_url_patterns: Optional[List[str]] = None,
        adaptive_search_navigation: bool = True,
        adaptive_search_html_wait: bool = True,
        search_response_target: int = 10,
        adaptive_wait_poll_ms: int = 120,
        search_networkidle_fallback_ms: int = 300,
        adaptive_listing_navigation: bool = False,
        listing_response_target: int = 12,
        listing_navigation_wait_cap_ms: int = 1800,
        listing_networkidle_fallback_ms: int = 700,
    ) -> None:
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.wait_after_load_ms = wait_after_load_ms
        self.capture_html = capture_html
        self.response_domain_allowlist = [d.lower() for d in (response_domain_allowlist or [])]
        self.response_url_allowlist = [p.lower() for p in (response_url_allowlist or [])]
        self.max_responses = max(1, int(max_responses))
        self.capture_timeout_ms = max(10000, int(capture_timeout_ms))
        self.review_scroll_steps = max(1, int(review_scroll_steps))
        self.review_scroll_pulses = max(1, int(review_scroll_pulses))
        self.review_wait_ms = max(0, int(review_wait_ms))
        self.review_pagination_passes = max(1, int(review_pagination_passes))
        self.review_page_wait_ms = max(250, int(review_page_wait_ms))
        self.review_only = bool(review_only)
        self.debug = bool(debug)
        self.debug_screenshots = bool(debug_screenshots)
        self.block_resources = bool(block_resources)
        default_blocked_types = ["image", "media", "font"]
        self.blocked_resource_types = {
            value.strip().lower()
            for value in (blocked_resource_types or default_blocked_types)
            if value and value.strip()
        }
        self.blocked_url_patterns = [
            value.strip().lower() for value in (blocked_url_patterns or []) if value and value.strip()
        ]
        self.adaptive_search_navigation = bool(adaptive_search_navigation)
        self.adaptive_search_html_wait = bool(adaptive_search_html_wait)
        self.search_response_target = max(1, int(search_response_target))
        self.adaptive_wait_poll_ms = max(50, int(adaptive_wait_poll_ms))
        self.search_networkidle_fallback_ms = max(100, int(search_networkidle_fallback_ms))
        self.adaptive_listing_navigation = bool(adaptive_listing_navigation)
        self.listing_response_target = max(1, int(listing_response_target))
        self.listing_navigation_wait_cap_ms = max(300, int(listing_navigation_wait_cap_ms))
        self.listing_networkidle_fallback_ms = max(100, int(listing_networkidle_fallback_ms))
        self._debug_review_click_target: Optional[str] = None
        self._debug_pagination: List[Dict[str, Any]] = []
        self._debug_translation_attempts: int = 0
        self._debug_translation_closed: Optional[bool] = None
        self._debug_translation_close_clicked: Optional[bool] = None
        self._debug_translation_close_method: Optional[str] = None
        self._debug_screenshots: List[str] = []
        self._capture_key: Optional[str] = None
        self._debug_last_scroll_target: Optional[Dict[str, Any]] = None

    def _coerce_override_int(
        self,
        value: Any,
        *,
        minimum: int,
        maximum: int,
    ) -> Optional[int]:
        try:
            parsed = int(value)
        except Exception:
            return None
        if parsed < int(minimum):
            return int(minimum)
        if parsed > int(maximum):
            return int(maximum)
        return int(parsed)

    def _coerce_override_bool(self, value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
            return None
        if isinstance(value, (int, float)):
            return bool(value)
        return None

    def _resolve_capture_overrides(
        self,
        capture_overrides: Optional[Dict[str, Any]],
        *,
        include_reviews: bool,
    ) -> Dict[str, Any]:
        resolved: Dict[str, Any] = {
            "capture_timeout_ms": int(self.capture_timeout_ms),
            "review_wait_ms": int(self.review_wait_ms),
            "review_pagination_passes": int(self.review_pagination_passes),
            "review_page_wait_ms": int(self.review_page_wait_ms),
            "adaptive_listing_navigation": bool(self.adaptive_listing_navigation),
            "listing_response_target": int(self.listing_response_target),
            "listing_navigation_wait_cap_ms": int(self.listing_navigation_wait_cap_ms),
            "listing_networkidle_fallback_ms": int(self.listing_networkidle_fallback_ms),
        }
        if not isinstance(capture_overrides, dict):
            return resolved

        timeout_ms = self._coerce_override_int(
            capture_overrides.get("capture_timeout_ms"),
            minimum=10000,
            maximum=600000,
        )
        if timeout_ms is not None:
            resolved["capture_timeout_ms"] = timeout_ms

        if include_reviews:
            review_wait_ms = self._coerce_override_int(
                capture_overrides.get("review_wait_ms"),
                minimum=0,
                maximum=60000,
            )
            if review_wait_ms is not None:
                resolved["review_wait_ms"] = review_wait_ms

            review_pagination_passes = self._coerce_override_int(
                capture_overrides.get("review_pagination_passes"),
                minimum=1,
                maximum=24,
            )
            if review_pagination_passes is not None:
                resolved["review_pagination_passes"] = review_pagination_passes

            review_page_wait_ms = self._coerce_override_int(
                capture_overrides.get("review_page_wait_ms"),
                minimum=250,
                maximum=10000,
            )
            if review_page_wait_ms is not None:
                resolved["review_page_wait_ms"] = review_page_wait_ms

        adaptive_listing_navigation = self._coerce_override_bool(
            capture_overrides.get("adaptive_listing_navigation")
        )
        if adaptive_listing_navigation is not None:
            resolved["adaptive_listing_navigation"] = adaptive_listing_navigation

        listing_response_target = self._coerce_override_int(
            capture_overrides.get("listing_response_target"),
            minimum=1,
            maximum=100,
        )
        if listing_response_target is not None:
            resolved["listing_response_target"] = listing_response_target

        listing_navigation_wait_cap_ms = self._coerce_override_int(
            capture_overrides.get("listing_navigation_wait_cap_ms"),
            minimum=300,
            maximum=10000,
        )
        if listing_navigation_wait_cap_ms is not None:
            resolved["listing_navigation_wait_cap_ms"] = listing_navigation_wait_cap_ms

        listing_networkidle_fallback_ms = self._coerce_override_int(
            capture_overrides.get("listing_networkidle_fallback_ms"),
            minimum=100,
            maximum=5000,
        )
        if listing_networkidle_fallback_ms is not None:
            resolved["listing_networkidle_fallback_ms"] = listing_networkidle_fallback_ms

        return resolved

    def _networkidle_grace_ms(self) -> int:
        # Keep a short post-navigation grace period so slow background requests do not dominate navigation time.
        return max(500, min(3000, int(self.wait_after_load_ms)))

    def _is_search_capture(self, capture_kind: str) -> bool:
        return (capture_kind or "").strip().lower() == "search"

    def _is_listing_capture(self, capture_kind: str) -> bool:
        return (capture_kind or "").strip().lower() == "listing"

    def _listing_navigation_wait_cap(self, network_grace_ms: int) -> int:
        network_grace_ms = max(100, int(network_grace_ms))
        return max(300, min(network_grace_ms, int(self.listing_navigation_wait_cap_ms)))

    def _listing_networkidle_fallback_ms(self, network_grace_ms: int) -> int:
        network_grace_ms = max(100, int(network_grace_ms))
        return max(100, min(network_grace_ms, int(self.listing_networkidle_fallback_ms)))

    async def _wait_for_response_threshold(
        self,
        page,
        responses: List[Dict[str, Any]],
        *,
        target: int,
        max_wait_ms: int,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        target = max(1, int(target or 1))
        max_wait_ms = max(0, int(max_wait_ms or 0))
        if len(responses) >= target or max_wait_ms <= 0:
            return {
                "satisfied": len(responses) >= target,
                "wait_ms": 0,
                "observed": int(len(responses)),
                "target": int(target),
            }
        elapsed_ms = 0
        while elapsed_ms < max_wait_ms:
            remaining = max_wait_ms - elapsed_ms
            sleep_ms = min(max(50, self.adaptive_wait_poll_ms), remaining)
            await page.wait_for_timeout(sleep_ms)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if len(responses) >= target:
                return {
                    "satisfied": True,
                    "wait_ms": int(elapsed_ms),
                    "observed": int(len(responses)),
                    "target": int(target),
                }
        return {
            "satisfied": False,
            "wait_ms": int(max_wait_ms),
            "observed": int(len(responses)),
            "target": int(target),
        }

    def _review_settle_wait_ms(self, review_mode: str) -> int:
        review_mode = (review_mode or "none").lower()
        if review_mode == "lite":
            return max(200, min(400, int(self.wait_after_load_ms / 2)))
        return max(500, int(self.wait_after_load_ms))

    def _post_click_wait_ms(self, review_mode: str) -> int:
        review_mode = (review_mode or "none").lower()
        return 400 if review_mode == "lite" else 750

    def _lite_min_review_responses_before_pulse(self) -> int:
        # In lite mode, if we already captured multiple review responses, avoid extra pulse latency.
        return 2

    def _lite_pulse_wait_ms(self, page_wait_ms: int) -> int:
        # Lite mode should use a shorter settle window than full pagination passes.
        return max(150, min(500, int(page_wait_ms / 3)))

    def _review_modal_wait_timeout_ms(self, review_mode: str) -> int:
        review_mode = (review_mode or "none").lower()
        return 2200 if review_mode == "lite" else 5000

    def _should_skip_lite_modal_readiness(self, review_mode: str, responses: List[Dict[str, Any]]) -> bool:
        review_mode = (review_mode or "none").lower()
        if review_mode != "lite":
            return False
        return self._count_review_responses(responses) >= self._lite_min_review_responses_before_pulse()

    def capture_listing(
        self,
        url: str,
        *,
        include_reviews: bool = False,
        review_mode: Optional[str] = None,
        review_only: Optional[bool] = None,
        capture_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        effective_review_only = review_only if review_only is not None else (self.review_only and include_reviews)
        effective_review_mode = (review_mode or ("full" if include_reviews else "none")).lower()
        if not include_reviews:
            effective_review_mode = "none"
        resolved_overrides = self._resolve_capture_overrides(
            capture_overrides,
            include_reviews=bool(include_reviews),
        )
        return self._run_capture(
            url,
            self._capture(
                url,
                include_reviews=include_reviews,
                review_only=effective_review_only,
                review_mode=effective_review_mode,
                capture_kind="listing",
                capture_overrides=resolved_overrides,
            ),
            capture_timeout_ms=resolved_overrides.get("capture_timeout_ms"),
        )

    def capture_search(
        self,
        params: Dict[str, Any],
        *,
        capture_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        search_url = params.get("search_url") or build_airbnb_search_url(params)
        resolved_overrides = self._resolve_capture_overrides(capture_overrides, include_reviews=False)
        return self._run_capture(
            search_url,
            self._capture(
                search_url,
                include_reviews=False,
                review_only=False,
                review_mode="none",
                capture_kind="search",
                capture_overrides=resolved_overrides,
            ),
            capture_timeout_ms=resolved_overrides.get("capture_timeout_ms"),
        )

    def capture_reviews(self, listing_id: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = params.get("url")
        if not url:
            raise ValueError("url is required for review capture")
        review_mode = (params.get("review_mode") or "full").lower()
        payload = self._run_capture(
            url,
            self._capture(
                url,
                include_reviews=True,
                review_only=True,
                review_mode=review_mode,
                capture_kind="listing",
                capture_overrides=None,
            ),
        )
        return payload.get("responses", [])

    def _run_capture(
        self,
        url: str,
        coro,
        *,
        capture_timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        start = time.monotonic()
        timeout_ms = self._coerce_override_int(capture_timeout_ms, minimum=10000, maximum=600000)
        if timeout_ms is None:
            timeout_ms = int(self.capture_timeout_ms)
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout=timeout_ms / 1000))
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "url": url,
                "duration_ms": duration_ms,
                "timings": {"total_ms": duration_ms},
                "html": None,
                "responses": [],
                "errors": [f"Capture timed out after {timeout_ms}ms"],
                "debug": None,
                "resource_blocking": {
                    "enabled": self.block_resources,
                    "blocked_total": 0,
                    "continued_total": 0,
                    "blocked_by_type": {},
                    "blocked_by_pattern": {},
                    "configured_types": sorted(self.blocked_resource_types),
                    "configured_patterns": list(self.blocked_url_patterns),
                },
            }

    async def _capture(
        self,
        url: str,
        include_reviews: bool,
        review_only: bool,
        review_mode: str,
        capture_kind: str = "listing",
        capture_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        start = time.monotonic()
        timings: Dict[str, int] = {}
        review_flow: Dict[str, Any] = {}
        resolved_overrides = self._resolve_capture_overrides(
            capture_overrides,
            include_reviews=bool(include_reviews),
        )
        adaptive_listing_navigation = bool(
            resolved_overrides.get("adaptive_listing_navigation", self.adaptive_listing_navigation)
        )
        listing_response_target = int(
            resolved_overrides.get("listing_response_target", self.listing_response_target)
        )
        listing_navigation_wait_cap_ms = int(
            resolved_overrides.get("listing_navigation_wait_cap_ms", self.listing_navigation_wait_cap_ms)
        )
        listing_networkidle_fallback_ms = int(
            resolved_overrides.get(
                "listing_networkidle_fallback_ms",
                self.listing_networkidle_fallback_ms,
            )
        )
        review_wait_ms = int(resolved_overrides.get("review_wait_ms", self.review_wait_ms))
        review_pagination_passes = int(
            resolved_overrides.get("review_pagination_passes", self.review_pagination_passes)
        )
        review_page_wait_ms = int(resolved_overrides.get("review_page_wait_ms", self.review_page_wait_ms))

        def _mark_step(step_name: str, step_start: float) -> None:
            timings[step_name] = int((time.monotonic() - step_start) * 1000)

        self._capture_key = self._extract_capture_key(url)
        self._debug_screenshots = []
        responses: List[Dict[str, Any]] = []
        seen_response_urls: set = set()
        html: Optional[str] = None
        errors: List[str] = []
        debug_info: Optional[Dict[str, Any]] = None
        review_event = asyncio.Event() if include_reviews else None
        self._debug_review_click_target = None
        self._debug_pagination = []
        self._debug_translation_attempts = 0
        self._debug_translation_closed = None
        self._debug_translation_close_clicked = None
        self._debug_translation_close_method = None
        self._debug_last_scroll_target = None
        resource_stats: Dict[str, Any] = {
            "enabled": self.block_resources,
            "blocked_total": 0,
            "continued_total": 0,
            "blocked_by_type": {},
            "blocked_by_pattern": {},
            "configured_types": sorted(self.blocked_resource_types),
            "configured_patterns": list(self.blocked_url_patterns),
        }

        review_mode = (review_mode or "none").lower()

        browser_setup_start = time.monotonic()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless)
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(self.navigation_timeout_ms)
            _mark_step("browser_setup_ms", browser_setup_start)

            if self.block_resources:
                blocking_setup_start = time.monotonic()

                async def handle_route(route, request) -> None:
                    request_url = (request.url or "").lower()
                    request_type = (request.resource_type or "").lower()

                    blocked = False
                    blocked_pattern = None
                    if request_type in self.blocked_resource_types:
                        blocked = True
                        resource_stats["blocked_by_type"][request_type] = (
                            resource_stats["blocked_by_type"].get(request_type, 0) + 1
                        )
                    else:
                        for pattern in self.blocked_url_patterns:
                            if pattern and pattern in request_url:
                                blocked = True
                                blocked_pattern = pattern
                                break
                        if blocked_pattern:
                            resource_stats["blocked_by_pattern"][blocked_pattern] = (
                                resource_stats["blocked_by_pattern"].get(blocked_pattern, 0) + 1
                            )

                    if blocked:
                        resource_stats["blocked_total"] += 1
                        await route.abort()
                    else:
                        resource_stats["continued_total"] += 1
                        await route.continue_()

                await page.route("**/*", handle_route)
                _mark_step("resource_blocking_setup_ms", blocking_setup_start)

            async def handle_response(response):
                if len(responses) >= self.max_responses:
                    return
                content_type = response.headers.get("content-type", "")
                if (
                    "application/json" not in content_type
                    and "application/graphql" not in content_type
                    and not content_type.endswith("+json")
                ):
                    return
                if not self._allow_response_url(response.url):
                    return
                if review_only and not self._looks_like_review_url(response.url):
                    return
                if response.url in seen_response_urls:
                    return
                seen_response_urls.add(response.url)
                try:
                    data = await response.json()
                except Exception:
                    return
                if review_only and not self._looks_like_review_payload(data):
                    return
                if review_event and not review_event.is_set():
                    if self._looks_like_review_url(response.url) or self._looks_like_review_payload(data):
                        review_event.set()
                responses.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "content_type": content_type,
                        "data": data,
                    }
                )

            page.on("response", handle_response)

            navigation_start = time.monotonic()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                network_grace_ms = self._networkidle_grace_ms()
                if self.adaptive_search_navigation and self._is_search_capture(capture_kind):
                    nav_wait = await self._wait_for_response_threshold(
                        page,
                        responses,
                        target=self.search_response_target,
                        max_wait_ms=network_grace_ms,
                    )
                    timings["navigation_wait_ms"] = int(nav_wait.get("wait_ms") or 0)
                    timings["navigation_response_count_after_wait"] = int(nav_wait.get("observed") or 0)
                    if not nav_wait.get("satisfied"):
                        fallback_ms = min(network_grace_ms, int(self.search_networkidle_fallback_ms))
                        try:
                            await page.wait_for_load_state("networkidle", timeout=fallback_ms)
                        except PlaywrightTimeoutError:
                            pass
                        timings["navigation_networkidle_fallback_ms"] = int(fallback_ms)
                elif adaptive_listing_navigation and self._is_listing_capture(capture_kind):
                    listing_wait_cap_ms = max(
                        300,
                        min(int(network_grace_ms), int(listing_navigation_wait_cap_ms)),
                    )
                    nav_wait = await self._wait_for_response_threshold(
                        page,
                        responses,
                        target=listing_response_target,
                        max_wait_ms=listing_wait_cap_ms,
                    )
                    timings["listing_navigation_wait_ms"] = int(nav_wait.get("wait_ms") or 0)
                    timings["listing_navigation_response_count_after_wait"] = int(
                        nav_wait.get("observed") or 0
                    )
                    if not nav_wait.get("satisfied"):
                        fallback_ms = max(
                            100,
                            min(int(network_grace_ms), int(listing_networkidle_fallback_ms)),
                        )
                        try:
                            await page.wait_for_load_state("networkidle", timeout=fallback_ms)
                        except PlaywrightTimeoutError:
                            pass
                        timings["listing_navigation_networkidle_fallback_ms"] = int(fallback_ms)
                else:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=network_grace_ms)
                    except PlaywrightTimeoutError:
                        pass
            except PlaywrightTimeoutError:
                try:
                    await page.goto(url)
                except Exception as exc:
                    errors.append(str(exc))
            _mark_step("navigation_ms", navigation_start)

            if self.debug_screenshots:
                await self._save_debug_screenshot(page, "page_loaded")

            if include_reviews and review_mode != "none":
                review_capture_start = time.monotonic()
                try:
                    review_flow = await self._trigger_review_load(
                        page,
                        responses,
                        review_mode,
                        post_click_wait_ms=self._post_click_wait_ms(review_mode),
                        review_pagination_passes=review_pagination_passes,
                        review_page_wait_ms=review_page_wait_ms,
                    )
                    modal_open_ms = review_flow.get("modal_open_ms")
                    pagination_ms = review_flow.get("pagination_ms")
                    if isinstance(modal_open_ms, (int, float)):
                        timings["review_modal_open_ms"] = int(modal_open_ms)
                    if isinstance(pagination_ms, (int, float)):
                        timings["review_pagination_ms"] = int(pagination_ms)
                    if review_event and review_wait_ms:
                        await asyncio.wait_for(review_event.wait(), timeout=review_wait_ms / 1000)
                except asyncio.TimeoutError:
                    errors.append("No review network responses detected before timeout")
                except Exception as exc:
                    errors.append(str(exc))
                _mark_step("review_capture_ms", review_capture_start)

            if self.capture_html and not review_only:
                html_capture_start = time.monotonic()
                try:
                    html_wait_ms = int(self.wait_after_load_ms)
                    if self.adaptive_search_html_wait and self._is_search_capture(capture_kind):
                        html_wait = await self._wait_for_response_threshold(
                            page,
                            responses,
                            target=self.search_response_target,
                            max_wait_ms=html_wait_ms,
                        )
                        html_wait_ms = int(html_wait.get("wait_ms") or 0)
                        timings["html_wait_ms"] = int(html_wait_ms)
                        timings["html_response_count_after_wait"] = int(html_wait.get("observed") or 0)
                    if html_wait_ms > 0:
                        await page.wait_for_timeout(html_wait_ms)
                    html = await page.content()
                except Exception as exc:
                    errors.append(str(exc))
                _mark_step("html_capture_ms", html_capture_start)

            if self.debug:
                debug_capture_start = time.monotonic()
                try:
                    debug_info = await self._collect_debug(page)
                except Exception as exc:
                    errors.append(f"Debug capture failed: {exc}")
                _mark_step("debug_capture_ms", debug_capture_start)

            cleanup_start = time.monotonic()
            await context.close()
            await browser.close()
            _mark_step("cleanup_ms", cleanup_start)

        duration_ms = int((time.monotonic() - start) * 1000)
        timings["total_ms"] = duration_ms
        if debug_info is not None:
            debug_info["response_count"] = len(responses)
            debug_info["response_url_samples"] = [resp["url"] for resp in responses[:10]]
            debug_info["review_response_urls"] = [
                resp["url"] for resp in responses if self._looks_like_review_url(resp["url"])
            ][:10]
            debug_info["pagination_passes"] = self._debug_pagination
            debug_info["review_mode"] = review_mode
            debug_info["screenshot_paths"] = list(self._debug_screenshots)
            debug_info["review_offsets"] = self._extract_review_offsets_from_responses(responses)
            debug_info["review_total_count"] = self._extract_review_total_from_responses(responses)
            debug_info["review_max_offset_plus_limit"] = self._extract_review_max_span(responses)
            debug_info["modal_scroll_target"] = self._debug_last_scroll_target
            debug_info["capture_overrides"] = resolved_overrides
            debug_info["review_flow"] = review_flow
        return {
            "url": url,
            "duration_ms": duration_ms,
            "timings": timings,
            "html": html,
            "responses": responses,
            "errors": errors,
            "debug": debug_info,
            "review_flow": review_flow,
            "review_only": review_only,
            "review_mode": review_mode,
            "resource_blocking": resource_stats,
        }

    async def _trigger_review_load(
        self,
        page,
        responses: List[Dict[str, Any]],
        review_mode: str,
        *,
        post_click_wait_ms: Optional[int] = None,
        review_pagination_passes: Optional[int] = None,
        review_page_wait_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        review_before = self._count_review_responses(responses)
        flow: Dict[str, Any] = {
            "modal_opened": False,
            "review_responses_before": int(review_before),
            "review_responses_after": int(review_before),
            "modal_open_ms": 0,
            "pagination_ms": 0,
            "duration_ms": 0,
            "pagination": {},
        }
        if review_mode == "none":
            return flow
        post_click_wait_ms = self._coerce_override_int(post_click_wait_ms, minimum=200, maximum=5000)
        if post_click_wait_ms is None:
            post_click_wait_ms = self._post_click_wait_ms(review_mode)
        modal_open_start = time.monotonic()
        lite_fast_ready_path = False
        await page.wait_for_timeout(200)
        await self._scroll_to_reviews(page)
        if self.debug_screenshots:
            await self._save_debug_screenshot(page, "review_before_click")
        clicked = await self._click_reviews_button(page, post_click_wait_ms=post_click_wait_ms)
        if not clicked:
            clicked = await self._click_review_triggers(page, post_click_wait_ms=post_click_wait_ms)
        if clicked:
            try:
                await page.wait_for_selector(
                    '[role="dialog"], [aria-modal="true"]',
                    timeout=self._review_modal_wait_timeout_ms(review_mode),
                )
            except Exception:
                pass
            if self.debug_screenshots:
                await self._save_debug_screenshot(page, "review_after_click")
            lite_fast_ready_path = self._should_skip_lite_modal_readiness(review_mode, responses)
            if not lite_fast_ready_path:
                await self._resolve_translation_modal(page)
                if self.debug_screenshots:
                    await self._save_debug_screenshot(page, "review_after_translation")
                await self._resolve_guest_favorite_modal(page)
                if self.debug_screenshots:
                    await self._save_debug_screenshot(page, "review_after_guest_favorite")
                ready = await self._wait_for_reviews_modal_ready(page, review_mode=review_mode)
                if self.debug_screenshots:
                    await self._save_debug_screenshot(page, "review_after_ready")
                should_retry_ready = review_mode == "full"
                if not ready and should_retry_ready:
                    await self._resolve_translation_modal(page)
                    await self._resolve_guest_favorite_modal(page)
                    await self._wait_for_reviews_modal_ready(page, review_mode=review_mode)
                    if self.debug_screenshots:
                        await self._save_debug_screenshot(page, "review_after_retry")
            elif self.debug_screenshots:
                await self._save_debug_screenshot(page, "review_fast_path_ready")
        try:
            modal_count = await page.locator('[role="dialog"], [aria-modal="true"]').count()
            flow["modal_opened"] = bool(
                clicked and (modal_count > 0 or lite_fast_ready_path)
            )
        except Exception:
            flow["modal_opened"] = bool(clicked)
        flow["modal_open_ms"] = int((time.monotonic() - modal_open_start) * 1000)
        settle_wait_ms = self._review_settle_wait_ms(review_mode)
        if lite_fast_ready_path:
            settle_wait_ms = min(settle_wait_ms, 250)
        if settle_wait_ms > 0:
            await page.wait_for_timeout(settle_wait_ms)
        max_passes = 0 if review_mode == "lite" else None
        pagination_start = time.monotonic()
        pagination = await self._paginate_reviews(
            page,
            responses,
            max_passes=max_passes,
            review_pagination_passes=review_pagination_passes,
            review_page_wait_ms=review_page_wait_ms,
        )
        flow["pagination_ms"] = int((time.monotonic() - pagination_start) * 1000)
        flow["pagination"] = pagination
        flow["review_responses_after"] = int(self._count_review_responses(responses))
        flow["duration_ms"] = int((time.monotonic() - started) * 1000)
        return flow

    async def _wait_for_reviews_modal_ready(self, page, *, review_mode: str = "full") -> bool:
        review_mode = (review_mode or "none").lower()
        total_budget_ms = 2200 if review_mode == "lite" else 8000
        per_selector_ms = 600 if review_mode == "lite" else 2500
        deadline = time.monotonic() + (total_budget_ms / 1000)

        try:
            modal = await self._pick_reviews_modal(page)
            if modal and await modal.count() > 0:
                if await modal.locator("[data-testid*='review'], article, li").count() > 0:
                    return True
        except Exception:
            pass

        selectors = [
            'input[placeholder*="review" i]',
            'input[aria-label*="review" i]',
            'button:has-text("Most recent")',
            'button:has-text("Most relevant")',
            'button:has-text("Newest")',
            '[data-testid*="review"]',
        ]
        for selector in selectors:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            timeout_ms = max(250, min(per_selector_ms, remaining_ms))
            try:
                modal = await self._pick_reviews_modal(page)
                if not modal:
                    await page.wait_for_selector(f'[role="dialog"] {selector}', timeout=timeout_ms)
                    return True
                await modal.locator(selector).first.wait_for(timeout=timeout_ms)
                return True
            except Exception:
                continue
        return False

    async def _resolve_translation_modal(self, page) -> None:
        attempts = 0
        while attempts < 2:
            attempts += 1
            self._debug_translation_attempts = attempts
            try:
                translation_modals = await self._find_translation_modals(page)
                if not translation_modals:
                    self._debug_translation_closed = True
                    return
                for modal in translation_modals:
                    self._debug_translation_close_clicked = False
                    close = modal.locator("button[aria-label*='close' i], button:has-text('Close')")
                    if await close.count() > 0:
                        try:
                            await close.first.click(timeout=2000, force=True)
                            self._debug_translation_close_clicked = True
                            self._debug_translation_close_method = "locator_click"
                            await page.wait_for_timeout(500)
                        except Exception:
                            self._debug_translation_close_clicked = False
                    if not self._debug_translation_close_clicked:
                        try:
                            await page.keyboard.press("Escape")
                            self._debug_translation_close_method = "escape"
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass
                    if not self._debug_translation_close_clicked:
                        try:
                            clicked = await page.evaluate(
                                """
                                (modalEl) => {
                                  const btn = modalEl.querySelector('button[aria-label="Close"]');
                                  if (!btn) return false;
                                  btn.click();
                                  return true;
                                }
                                """,
                                await modal.element_handle(),
                            )
                            if clicked:
                                self._debug_translation_close_clicked = True
                                self._debug_translation_close_method = "js_click"
                                await page.wait_for_timeout(400)
                        except Exception:
                            pass

                remaining = await self._find_translation_modals(page)
                self._debug_translation_closed = not bool(remaining)

                await self._scroll_to_reviews(page)
                clicked = await self._click_reviews_button(page)
                if not clicked:
                    await self._click_review_triggers(page)
            except Exception:
                self._debug_translation_closed = False
                return
        if self._debug_translation_closed is None:
            self._debug_translation_closed = False

    async def _resolve_guest_favorite_modal(self, page) -> None:
        try:
            modal = await self._pick_reviews_modal(page, include_guest_favorite=True)
            if not modal:
                return
            text = ""
            try:
                text = (await modal.inner_text()).lower()
            except Exception:
                text = ""
            if "guest favorite" in text or "guest favourite" in text or "reviews from past guests" in text:
                await self._close_modal(page, modal)
                await page.wait_for_timeout(300)
                await self._open_reviews_from_section(page)
        except Exception:
            return

    async def _close_modal(self, page, modal=None) -> None:
        modal = modal or page.locator('[role="dialog"], [aria-modal="true"]').first
        if await modal.count() == 0:
            return
        selectors = [
            "button[aria-label*='close' i]",
            "button[aria-label*='dismiss' i]",
            "button:has-text('Close')",
            "button:has-text('Cancel')",
        ]
        for selector in selectors:
            close = modal.locator(selector)
            if await close.count() > 0:
                try:
                    await close.first.click(timeout=2000)
                    await page.wait_for_timeout(300)
                    return
                except Exception:
                    pass
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
        except Exception:
            pass

    async def _find_translation_modals(self, page) -> List[Any]:
        modals = []
        locator = page.locator('[role="dialog"], [aria-modal="true"]')
        count = await locator.count()
        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                pass
            try:
                text = (await candidate.inner_text()).lower()
            except Exception:
                text = ""
            if "translation settings" in text or "translation on" in text:
                modals.append(candidate)
        return modals

    async def _pick_reviews_modal(self, page, include_guest_favorite: bool = False):
        locator = page.locator('[role="dialog"], [aria-modal="true"]')
        count = await locator.count()
        best = None
        best_score = -1
        for idx in range(count):
            candidate = locator.nth(idx)
            score = 0
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                pass
            try:
                text = (await candidate.inner_text()).lower()
            except Exception:
                text = ""
            if "translation settings" in text or "translation on" in text:
                continue
            if "review" in text:
                score += 2
            if include_guest_favorite and ("guest favorite" in text or "guest favourite" in text):
                score += 1
            try:
                if await candidate.locator(
                    "input[placeholder*='review' i], input[aria-label*='review' i]"
                ).count() > 0:
                    score += 4
            except Exception:
                pass
            try:
                if await candidate.locator("[data-testid*='review'], article, li").count() > 0:
                    score += 3
            except Exception:
                pass
            if score > best_score:
                best = candidate
                best_score = score
        return best

    async def _open_reviews_from_section(self, page) -> None:
        script = """
        () => {
          const inModal = (el) => !!el.closest('[role="dialog"], [aria-modal="true"]');
          const textOf = (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const isShowAll = (el) => {
            const text = textOf(el);
            return text.includes('show all') && text.includes('review');
          };
          const candidates = Array.from(document.querySelectorAll('button, a, div[role=\"button\"]'));
          const howLinks = candidates.filter(el => textOf(el).includes('how reviews work') && !inModal(el));
          for (const link of howLinks) {
            const section = link.closest('section') || link.parentElement;
            if (!section) continue;
            const buttons = Array.from(section.querySelectorAll('button, a, div[role=\"button\"]'));
            const target = buttons.find(isShowAll);
            if (target) {
              target.click();
              return (target.textContent || '').trim();
            }
          }
          const visible = candidates.filter(el => !inModal(el) && isShowAll(el));
          if (!visible.length) return null;
          visible.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
          visible[0].click();
          return (visible[0].textContent || '').trim();
        }
        """
        try:
            clicked_text = await page.evaluate(script)
            if clicked_text:
                self._debug_review_click_target = clicked_text
                await page.wait_for_timeout(500)
        except Exception:
            return

    async def _paginate_reviews(
        self,
        page,
        responses: List[Dict[str, Any]],
        *,
        max_passes: Optional[int] = None,
        review_pagination_passes: Optional[int] = None,
        review_page_wait_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        response_count_before = self._count_review_responses(responses)
        stats: Dict[str, Any] = {
            "passes_executed": 0,
            "stopped_reason": "not_started",
            "response_count_before": int(response_count_before),
            "response_count_after": int(response_count_before),
            "duration_ms": 0,
        }
        page_wait_ms = self._coerce_override_int(review_page_wait_ms, minimum=250, maximum=10000)
        if page_wait_ms is None:
            page_wait_ms = int(self.review_page_wait_ms)
        if max_passes is None:
            max_passes = self._coerce_override_int(review_pagination_passes, minimum=1, maximum=24)
            if max_passes is None:
                max_passes = int(self.review_pagination_passes)
        if max_passes <= 0:
            if response_count_before >= self._lite_min_review_responses_before_pulse():
                stats["passes_executed"] = 0
                stats["stopped_reason"] = "lite_mode_skip_existing_responses"
                stats["response_count_after"] = int(response_count_before)
                stats["duration_ms"] = int((time.monotonic() - started) * 1000)
                return stats
            lite_wait_ms = self._lite_pulse_wait_ms(page_wait_ms)
            for pulse in range(min(1, self.review_scroll_pulses)):
                await self._scroll_reviews_modal(page)
                await page.wait_for_timeout(lite_wait_ms)
                if self.debug_screenshots:
                    await self._save_debug_screenshot(page, f"review_lite_p{pulse}")
            stats["passes_executed"] = 0
            stats["stopped_reason"] = "lite_mode_single_pulse"
            stats["response_count_after"] = int(self._count_review_responses(responses))
            stats["duration_ms"] = int((time.monotonic() - started) * 1000)
            return stats
        no_new = 0
        stopped_reason = "max_passes_reached"
        for idx in range(max_passes):
            stats["passes_executed"] = idx + 1
            previous = self._count_review_responses(responses)
            offsets_before = self._extract_review_offsets_from_responses(responses)
            total_count = self._extract_review_total_from_responses(responses)
            max_span_before = self._extract_review_max_span(responses)
            moved_any = False
            for pulse in range(self.review_scroll_pulses):
                moved = await self._scroll_reviews_modal(page)
                moved_any = moved_any or moved
                await self._click_more_reviews(page)
                await page.wait_for_timeout(max(250, int(page_wait_ms / 2)))
                if self.debug_screenshots:
                    await self._save_debug_screenshot(page, f"review_paginate_{idx}_p{pulse}")
            await page.wait_for_timeout(page_wait_ms)
            current = self._count_review_responses(responses)
            offsets_after = self._extract_review_offsets_from_responses(responses)
            max_span_after = self._extract_review_max_span(responses)
            if self.debug:
                self._debug_pagination.append(
                    {
                        "pass": idx,
                        "before": previous,
                        "after": current,
                        "offsets_before": offsets_before,
                        "offsets_after": offsets_after,
                        "max_span_before": max_span_before,
                        "max_span_after": max_span_after,
                        "total_count": total_count,
                        "moved_any": moved_any,
                    }
                )
            if total_count and max_span_after >= total_count:
                stopped_reason = "covered_total_count"
                break
            if current == previous and offsets_after == offsets_before:
                no_new += 1
                if not moved_any:
                    stopped_reason = "no_scroll_progress"
                    break
                if no_new >= 2:
                    stopped_reason = "offset_saturation"
                    break
            else:
                no_new = 0
        stats["stopped_reason"] = stopped_reason
        stats["response_count_after"] = int(self._count_review_responses(responses))
        stats["duration_ms"] = int((time.monotonic() - started) * 1000)
        return stats

    async def _scroll_reviews_modal(self, page) -> bool:
        modal = await self._pick_reviews_modal(page)
        if modal and await modal.count() > 0:
            target = modal
            try:
                await target.hover()
            except Exception:
                pass
            for _ in range(2):
                await page.mouse.wheel(0, 1200)
                await page.wait_for_timeout(200)
        try:
            info = await self._resolve_modal_scroll_target(page, do_scroll=True)
            if info:
                self._debug_last_scroll_target = info
                before = info.get("scrollTopBefore", 0) or 0
                after = info.get("scrollTopAfter", 0) or 0
                return after > before + 2
        except Exception:
            return False
        return False

    async def _resolve_modal_scroll_target(self, page, *, do_scroll: bool) -> Optional[Dict[str, Any]]:
        modal = await self._pick_reviews_modal(page)
        handle = await modal.element_handle() if modal else None
        payload = {"doScroll": bool(do_scroll), "modalEl": handle}
        return await page.evaluate(
            """
            ({ doScroll, modalEl }) => {
              const modal = modalEl || document.querySelector('[role="dialog"], [aria-modal="true"]');
              if (!modal) {
                if (doScroll) {
                  window.scrollTo(0, document.body.scrollHeight);
                }
                return null;
              }

              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 40 && rect.height > 40;
              };

              const scrollableCandidates = Array.from(modal.querySelectorAll('*'))
                .filter(el => el.scrollHeight > el.clientHeight + 6 && visible(el));

              let best = null;
              let bestScore = -Infinity;
              for (const el of scrollableCandidates) {
                let score = 0;
                const style = window.getComputedStyle(el);
                const overflowY = style ? style.overflowY : '';
                if (['auto', 'scroll'].includes(overflowY)) score += 2;
                if (el.querySelector('input[placeholder*="review" i], input[aria-label*="review" i]')) score += 4;
                if (el.querySelector('[data-testid*="review"], article, li')) score += 4;
                const text = (el.textContent || '').toLowerCase();
                if (text.includes('review')) score += 1;
                score += Math.min(5, el.clientHeight / 200);
                if (score > bestScore) {
                  bestScore = score;
                  best = el;
                }
              }

              const target = best || modal;
              const before = target.scrollTop;
              if (doScroll) {
                const step = Math.max(240, Math.floor(target.clientHeight * 0.85));
                target.scrollTop = Math.min(target.scrollTop + step, target.scrollHeight);
                target.dispatchEvent(new Event('scroll', { bubbles: true }));
              }
              const after = target.scrollTop;

              return {
                tag: target.tagName,
                role: target.getAttribute('role'),
                aria: target.getAttribute('aria-label'),
                className: (target.className || '').toString().slice(0, 120),
                clientHeight: target.clientHeight,
                scrollHeight: target.scrollHeight,
                scrollTopBefore: before,
                scrollTopAfter: after,
                usedFallback: !best,
                candidateCount: scrollableCandidates.length
              };
            }
            """,
            payload,
        )

    async def _scroll_to_reviews(self, page) -> None:
        heading = page.get_by_role("heading", name=re.compile("review", re.I))
        try:
            if await heading.count() > 0:
                await heading.first.scroll_into_view_if_needed()
                await page.wait_for_timeout(350)
                return
        except Exception:
            pass

        for step in range(self.review_scroll_steps):
            fraction = (step + 1) / self.review_scroll_steps
            await page.evaluate(
                "(fraction) => window.scrollTo(0, document.body.scrollHeight * fraction)",
                fraction,
            )
            await page.wait_for_timeout(350)

    async def _click_reviews_button(self, page, *, post_click_wait_ms: Optional[int] = None) -> bool:
        wait_ms = self._coerce_override_int(post_click_wait_ms, minimum=200, maximum=5000)
        if wait_ms is None:
            wait_ms = self._post_click_wait_ms("full")
        scripted_click = """
        () => {
          const candidates = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
          const blocked = ['guest favorite', 'guest favourite', 'how reviews work'];
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const toText = (el) => {
            const text = normalize(el.textContent || '').toLowerCase();
            const aria = normalize(el.getAttribute('aria-label') || '').toLowerCase();
            return `${text} ${aria}`.trim();
          };
          const findShowAll = (root) => {
            if (!root) return null;
            const local = Array.from(root.querySelectorAll('button, a, div[role="button"]'));
            const matches = local.filter(el => {
              const hay = toText(el);
              if (!hay) return false;
              if (blocked.some(b => hay.includes(b))) return false;
              return hay.includes('show all') && hay.includes('review');
            });
            if (!matches.length) return null;
            matches.sort((a, b) => (b.textContent || '').length - (a.textContent || '').length);
            return matches[0];
          };

          // Prefer the "Show all reviews" button near the "How reviews work" link.
          const how = candidates.find(el => toText(el).includes('how reviews work'));
          if (how) {
            const section = how.closest('section') || how.parentElement;
            const target = findShowAll(section);
            if (target) {
              target.click();
              return normalize(target.textContent || '');
            }
          }

          // Prefer a "Reviews" section if present.
          const reviewsHeading = Array.from(document.querySelectorAll('h2,h3,h4')).find(el => toText(el).includes('reviews'));
          if (reviewsHeading) {
            const section = reviewsHeading.closest('section') || reviewsHeading.parentElement;
            const target = findShowAll(section);
            if (target) {
              target.click();
              return normalize(target.textContent || '');
            }
          }

          const matches = [];
          for (const el of candidates) {
            const text = normalize(el.textContent || '').toLowerCase();
            const aria = normalize(el.getAttribute('aria-label') || '').toLowerCase();
            if (blocked.some(b => text.includes(b) || aria.includes(b))) continue;
            const hay = `${text} ${aria}`;
            if (hay.includes('show all') && hay.includes('review')) {
              matches.push(el);
            }
          }
          if (!matches.length) return null;
          matches.sort((a, b) => (b.textContent || '').length - (a.textContent || '').length);
          matches[0].click();
          return (matches[0].textContent || '').trim();
        }
        """
        try:
            clicked_text = await page.evaluate(scripted_click)
            if clicked_text:
                self._debug_review_click_target = clicked_text
                await page.wait_for_timeout(wait_ms)
                return True
        except Exception:
            pass

        preferred_selectors = [
            "button:has-text('Show all')",
            "button:has-text('See all')",
            "button:has-text('View all')",
            "a:has-text('Show all')",
            "a:has-text('See all')",
            "a:has-text('View all')",
            "div[role='button']:has-text('Show all')",
            "div[role='button']:has-text('See all')",
            "div[role='button']:has-text('View all')",
        ]
        for selector in preferred_selectors:
            locator = page.locator(selector)
            try:
                if await locator.count() > 0:
                    target = locator.first
                    await target.scroll_into_view_if_needed()
                    try:
                        self._debug_review_click_target = await target.inner_text()
                    except Exception:
                        self._debug_review_click_target = None
                    await target.click(timeout=3000)
                    await page.wait_for_timeout(wait_ms)
                    return True
            except Exception:
                continue

        preferred_role_patterns = [
            ("button", re.compile(r"(show|see|view).*(review|reviews)", re.I)),
            ("link", re.compile(r"(show|see|view).*(review|reviews)", re.I)),
        ]
        for role, pattern in preferred_role_patterns:
            locator = page.get_by_role(role, name=pattern)
            try:
                if await locator.count() > 0:
                    target = locator.first
                    await target.scroll_into_view_if_needed()
                    try:
                        self._debug_review_click_target = await target.inner_text()
                    except Exception:
                        self._debug_review_click_target = None
                    await target.click(timeout=3000)
                    await page.wait_for_timeout(wait_ms)
                    return True
            except Exception:
                continue

        role_buttons = page.get_by_role("button", name=re.compile("review", re.I))
        try:
            if await role_buttons.count() > 0:
                total = await role_buttons.count()
                for idx in range(total):
                    target = role_buttons.nth(idx)
                    text = ""
                    try:
                        text = (await target.inner_text()).strip()
                    except Exception:
                        text = ""
                    lowered = text.lower()
                    if lowered and (
                        "guest favourite" in lowered
                        or "guest favorite" in lowered
                        or "how reviews work" in lowered
                    ):
                        continue
                    if lowered and not re.search(r"(show|see|view)", lowered):
                        continue
                    await target.scroll_into_view_if_needed()
                    self._debug_review_click_target = text or None
                    await target.click(timeout=3000)
                    await page.wait_for_timeout(wait_ms)
                    return True
        except Exception:
            return False

        role_links = page.get_by_role("link", name=re.compile("review", re.I))
        try:
            if await role_links.count() > 0:
                total = await role_links.count()
                for idx in range(total):
                    target = role_links.nth(idx)
                    text = ""
                    try:
                        text = (await target.inner_text()).strip()
                    except Exception:
                        text = ""
                    lowered = text.lower()
                    if lowered and (
                        "guest favourite" in lowered
                        or "guest favorite" in lowered
                        or "how reviews work" in lowered
                    ):
                        continue
                    if lowered and not re.search(r"(show|see|view)", lowered):
                        continue
                    await target.scroll_into_view_if_needed()
                    self._debug_review_click_target = text or None
                    await target.click(timeout=3000)
                    await page.wait_for_timeout(wait_ms)
                    return True
        except Exception:
            return False

        candidates = [
            "button:has-text('Show all reviews')",
            "button:has-text('See all reviews')",
            "button:has-text('reviews')",
            "a:has-text('Show all reviews')",
            "a:has-text('See all reviews')",
            "a:has-text('reviews')",
            "a[href*='review']",
            "a[href*='reviews']",
            "div[role='button']:has-text('reviews')",
        ]
        for selector in candidates:
            locator = page.locator(selector)
            if await locator.count() == 0:
                continue
            first = locator.first
            try:
                await first.scroll_into_view_if_needed()
                try:
                    self._debug_review_click_target = await first.inner_text()
                except Exception:
                    self._debug_review_click_target = None
                await first.click(timeout=3000)
                await page.wait_for_timeout(wait_ms)
                return True
            except Exception:
                continue
        return False

    async def _click_review_triggers(self, page, *, post_click_wait_ms: Optional[int] = None) -> bool:
        wait_ms = self._coerce_override_int(post_click_wait_ms, minimum=200, maximum=5000)
        if wait_ms is None:
            wait_ms = self._post_click_wait_ms("full")
        script = """
        () => {
          const needles = ['review', 'reviews'];
          const preferred = ['show all', 'see all', 'view all'];
          const blocked = ['guest favorite', 'guest favourite', 'how reviews work'];
          const candidates = Array.from(document.querySelectorAll('button, a, div[role="button"]'));
          for (const el of candidates) {
            const text = (el.textContent || '').toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            if (blocked.some(b => text.includes(b) || aria.includes(b))) {
              continue;
            }
            if (preferred.some(p => text.includes(p) || aria.includes(p))) {
              if (needles.some(n => text.includes(n) || aria.includes(n))) {
                el.click();
                return true;
              }
            }
          }
          for (const el of candidates) {
            const text = (el.textContent || '').toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            if (blocked.some(b => text.includes(b) || aria.includes(b))) {
              continue;
            }
            if (needles.some(n => text.includes(n) || aria.includes(n))) {
              el.click();
              return true;
            }
          }
          return false;
        }
        """
        try:
            clicked = await page.evaluate(script)
        except Exception:
            return False
        if clicked:
            await page.wait_for_timeout(wait_ms)
        return bool(clicked)

    async def _click_more_reviews(self, page) -> None:
        selectors = [
            "button:has-text('Show more reviews')",
            "button:has-text('More reviews')",
            "button:has-text('Show more')",
            "button:has-text('More')",
            "a:has-text('Show more reviews')",
            "a:has-text('More reviews')",
        ]
        modal = page.locator('[role="dialog"], [aria-modal="true"]')
        for selector in selectors:
            locator = modal.locator(selector)
            if await locator.count() == 0:
                locator = page.locator(selector)
                if await locator.count() == 0:
                    continue
            first = locator.first
            try:
                await first.scroll_into_view_if_needed()
                await first.click(timeout=3000)
                await page.wait_for_timeout(750)
                return
            except Exception:
                continue

    def _allow_response_url(self, url: str) -> bool:
        if not url:
            return False
        lowered = url.lower()
        if self.response_url_allowlist:
            if any(pattern in lowered for pattern in self.response_url_allowlist):
                return True
        if not self.response_domain_allowlist:
            return True
        hostname = urlparse(url).hostname or ""
        hostname = hostname.lower()
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in self.response_domain_allowlist)

    def _looks_like_review_url(self, url: str) -> bool:
        lowered = url.lower()
        return any(hint in lowered for hint in REVIEW_URL_HINTS)

    def _extract_capture_key(self, url: str) -> str:
        if not url:
            return "unknown"
        match = re.search(r"/rooms/(\d+)", url)
        if match:
            return match.group(1)
        try:
            path = urlparse(url).path.strip("/")
        except Exception:
            return "unknown"
        return path.replace("/", "_") or "unknown"

    def _safe_label(self, label: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", label or "")
        return cleaned.strip("_") or "step"

    async def _save_debug_screenshot(self, page, label: str) -> None:
        if not self.debug_screenshots:
            return
        key = self._capture_key or "unknown"
        base_dir = os.path.join(RAW_DIR, "capture_screens", key)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        unique = uuid.uuid4().hex[:8]
        safe_label = self._safe_label(label)
        filename = f"{timestamp}_{safe_label}_{unique}.png"
        path_out = os.path.join(base_dir, filename)
        try:
            await page.screenshot(path=path_out, full_page=True)
            self._debug_screenshots.append(path_out)
        except Exception:
            return

    def _extract_review_offset(self, url: str) -> Optional[int]:
        try:
            query = parse_qs(urlparse(url).query or "")
            variables = query.get("variables", [None])[0]
            if not variables:
                return None
            payload = json.loads(variables)
            offset = payload.get("pdpReviewsRequest", {}).get("offset")
            if offset is None:
                return None
            return int(offset)
        except Exception:
            return None

    def _extract_review_limit(self, url: str) -> Optional[int]:
        try:
            query = parse_qs(urlparse(url).query or "")
            variables = query.get("variables", [None])[0]
            if not variables:
                return None
            payload = json.loads(variables)
            limit = payload.get("pdpReviewsRequest", {}).get("limit")
            if limit is None:
                return None
            return int(limit)
        except Exception:
            return None

    def _extract_review_offsets_from_responses(self, responses: List[Dict[str, Any]]) -> List[int]:
        offsets: List[int] = []
        for resp in responses:
            url_value = resp.get("url", "")
            if not self._looks_like_review_url(url_value):
                continue
            offset = self._extract_review_offset(url_value)
            if offset is not None:
                offsets.append(offset)
        return sorted(set(offsets))

    def _extract_review_max_span(self, responses: List[Dict[str, Any]]) -> int:
        max_span = 0
        for resp in responses:
            url_value = resp.get("url", "")
            if not self._looks_like_review_url(url_value):
                continue
            offset = self._extract_review_offset(url_value)
            limit = self._extract_review_limit(url_value)
            if offset is None or limit is None:
                continue
            max_span = max(max_span, offset + limit)
        return max_span

    def _extract_review_total_from_responses(self, responses: List[Dict[str, Any]]) -> Optional[int]:
        keys = {"reviewcount", "reviewscount", "overallcount", "totalcount"}

        def _walk(node: Any, depth: int) -> Optional[int]:
            if depth <= 0:
                return None
            if isinstance(node, dict):
                best = None
                for key, value in node.items():
                    key_lower = str(key).lower()
                    if key_lower in keys and isinstance(value, (int, float)):
                        best = int(value)
                    found = _walk(value, depth - 1)
                    if found is not None:
                        best = max(best or 0, found)
                return best
            if isinstance(node, list):
                best = None
                for item in node[:50]:
                    found = _walk(item, depth - 1)
                    if found is not None:
                        best = max(best or 0, found)
                return best
            return None

        best_count = None
        for resp in responses:
            data = resp.get("data")
            found = _walk(data, 6)
            if found is not None:
                best_count = max(best_count or 0, found)
        return best_count

    def _count_review_responses(self, responses: List[Dict[str, Any]]) -> int:
        return sum(1 for resp in responses if self._looks_like_review_url(resp.get("url", "")))

    def _looks_like_review_payload(self, payload: Any) -> bool:
        def _walk(obj: Any, depth: int) -> bool:
            if depth <= 0:
                return False
            if isinstance(obj, dict):
                for key, value in obj.items():
                    key_text = str(key).lower()
                    if "review" in key_text or key_text in ("comment", "comments"):
                        return True
                    if _walk(value, depth - 1):
                        return True
                return False
            if isinstance(obj, list):
                for item in obj[:50]:
                    if _walk(item, depth - 1):
                        return True
            return False

        return _walk(payload, 4)

    async def _collect_debug(self, page) -> Dict[str, Any]:
        debug: Dict[str, Any] = {}
        modal_locator = page.locator('[role="dialog"], [aria-modal="true"]')
        debug["modal_count"] = await modal_locator.count()
        heading_locator = page.get_by_role("heading", name=re.compile("review", re.I))
        debug["review_heading_count"] = await heading_locator.count()

        button_locator = page.locator('button:has-text("review"), a:has-text("review")')
        try:
            texts = await button_locator.all_text_contents()
            debug["review_button_texts"] = [text.strip() for text in texts if text.strip()][:10]
        except Exception:
            debug["review_button_texts"] = []

        if debug["modal_count"]:
            debug["modal_candidates"] = []
            count = await modal_locator.count()
            for idx in range(count):
                candidate = modal_locator.nth(idx)
                try:
                    text = (await candidate.inner_text())[:200]
                except Exception:
                    text = ""
                try:
                    has_search = await candidate.locator(
                        "input[placeholder*='review' i], input[aria-label*='review' i]"
                    ).count()
                except Exception:
                    has_search = 0
                try:
                    review_cards = await candidate.locator("[data-testid*='review'], article, li").count()
                except Exception:
                    review_cards = 0
                debug["modal_candidates"].append(
                    {
                        "text_snippet": text,
                        "has_search_input": has_search,
                        "review_card_count": review_cards,
                    }
                )
            modal = await self._pick_reviews_modal(page, include_guest_favorite=True)
            modal = modal or modal_locator.first
            try:
                modal_html = await modal.inner_html()
                debug["modal_html_snippet"] = modal_html[:2000]
            except Exception:
                debug["modal_html_snippet"] = None
            try:
                debug["review_more_button_count"] = await modal.locator(
                    "button:has-text('Show more reviews'), a:has-text('Show more reviews'), "
                    "button:has-text('More reviews'), a:has-text('More reviews')"
                ).count()
            except Exception:
                debug["review_more_button_count"] = None
            try:
                debug["modal_has_search_input"] = await modal.locator(
                    "input[placeholder*='review' i], input[aria-label*='review' i]"
                ).count()
            except Exception:
                debug["modal_has_search_input"] = None
            try:
                debug["modal_review_card_count"] = await modal.locator(
                    "[data-testid*='review'], article, li"
                ).count()
            except Exception:
                debug["modal_review_card_count"] = None
            try:
                debug["modal_text_snippet"] = (
                    (await modal.inner_text())[:400]
                )
            except Exception:
                debug["modal_text_snippet"] = None
            try:
                buttons = await modal.locator("button").all()
                labels = []
                for btn in buttons[:10]:
                    try:
                        label = await btn.get_attribute("aria-label")
                    except Exception:
                        label = None
                    try:
                        text = (await btn.inner_text()).strip()
                    except Exception:
                        text = ""
                    if label or text:
                        labels.append({"text": text, "aria": label})
                debug["modal_button_labels"] = labels
            except Exception:
                debug["modal_button_labels"] = []
            try:
                snippet = (debug.get("modal_text_snippet") or "").lower()
                debug["modal_is_translation"] = (
                    "translation settings" in snippet or "translation on" in snippet
                )
            except Exception:
                debug["modal_is_translation"] = None
            debug["translation_close_clicked"] = self._debug_translation_close_clicked
            debug["translation_close_method"] = self._debug_translation_close_method
            try:
                debug["modal_is_guest_favorite"] = (
                    "guest favorite" in snippet
                    or "guest favourite" in snippet
                    or "reviews from past guests" in snippet
                )
            except Exception:
                debug["modal_is_guest_favorite"] = None
            debug["translation_close_attempts"] = self._debug_translation_attempts
            debug["translation_closed"] = self._debug_translation_closed
            try:
                debug["modal_review_scroll_target"] = await self._resolve_modal_scroll_target(
                    page,
                    do_scroll=False,
                )
            except Exception:
                debug["modal_review_scroll_target"] = None
            try:
                debug["modal_scrollables"] = await page.evaluate(
                    """
                    () => {
                      const modal = document.querySelector('[role="dialog"], [aria-modal="true"]');
                      if (!modal) return [];
                      const scrollables = Array.from(modal.querySelectorAll('*'))
                        .filter(el => el.scrollHeight > el.clientHeight);
                      return scrollables.slice(0, 8).map(el => ({
                        tag: el.tagName,
                        role: el.getAttribute('role'),
                        aria: el.getAttribute('aria-label'),
                        className: (el.className || '').toString().slice(0, 120),
                        clientHeight: el.clientHeight,
                        scrollHeight: el.scrollHeight,
                        scrollTop: el.scrollTop
                      }));
                    }
                    """
                )
            except Exception:
                debug["modal_scrollables"] = []

        try:
            screenshot = await page.screenshot(full_page=True)
            debug["screenshot_base64"] = base64.b64encode(screenshot).decode("ascii")
        except Exception:
            debug["screenshot_base64"] = None

        debug["review_click_target"] = self._debug_review_click_target
        return debug
