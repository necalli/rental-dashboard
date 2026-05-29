import logging
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .airbnb_parser_v1 import parse_capture
from .airbnb_search_parser_v1 import parse_search_from_responses_with_meta
from .parser_drift import compact_parser_meta
from .playwright_capture import PlaywrightCapture
from .rate_limiter import RateLimiter
from .job_support import CaptureAccessPolicy, CapturePayloadStore, JobMetricRecorder
from .listing_schema import normalize_listing, validate_listing
from .search_schema import normalize_search_listing, validate_search_listing
from .llm_enrichment import (
    COMPARISON_PROMPT_VERSION,
    PROMPT_VERSION as LLM_PROMPT_VERSION,
    build_comparison_request,
    build_summary_request,
    generate_listing_comparison,
    generate_listing_summary,
)
from .fx_rates import get_fx_rate
from .storage import Storage


LISTING_ID_PATTERN = re.compile(r"/rooms/(\d+)")
LOGGER = logging.getLogger("rental.job_runner")


def _coerce_int_range(value: Any, minimum: int, maximum: int) -> Optional[int]:
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed < int(minimum):
        return int(minimum)
    if parsed > int(maximum):
        return int(maximum)
    return int(parsed)


def _coerce_float_range(value: Any, minimum: float, maximum: float) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed < float(minimum):
        return float(minimum)
    if parsed > float(maximum):
        return float(maximum)
    return float(parsed)


def _normalize_lite_capture_strategy(value: Any) -> Optional[str]:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"adaptive", "normal"} else None


def _extract_capture_overrides(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    timeout_ms = _coerce_int_range(payload.get("capture_timeout_ms"), 10000, 600000)
    if timeout_ms is not None:
        out["capture_timeout_ms"] = timeout_ms
    review_pagination_passes = _coerce_int_range(payload.get("review_pagination_passes"), 1, 24)
    if review_pagination_passes is not None:
        out["review_pagination_passes"] = review_pagination_passes
    review_wait_ms = _coerce_int_range(payload.get("review_wait_ms"), 0, 60000)
    if review_wait_ms is not None:
        out["review_wait_ms"] = review_wait_ms
    review_page_wait_ms = _coerce_int_range(payload.get("review_page_wait_ms"), 250, 10000)
    if review_page_wait_ms is not None:
        out["review_page_wait_ms"] = review_page_wait_ms
    lite_strategy = _normalize_lite_capture_strategy(payload.get("lite_capture_strategy"))
    if lite_strategy:
        out["lite_capture_strategy"] = lite_strategy
    lite_adaptive_max_pulses = _coerce_int_range(payload.get("lite_adaptive_max_pulses"), 1, 12)
    if lite_adaptive_max_pulses is not None:
        out["lite_adaptive_max_pulses"] = lite_adaptive_max_pulses
    return out


def _expected_lite_review_target(
    review_limit: Any,
    reviews_total: Any,
    default_limit: int = 24,
) -> int:
    expected = _to_int(review_limit) or int(default_limit)
    total = _to_int(reviews_total)
    if total and total > 0:
        expected = min(expected, int(total))
    return max(1, int(expected))


def _should_retry_lite_capture_once(
    *,
    review_mode: str,
    review_only: bool,
    reviews_captured: Any,
    reviews_total: Any,
    review_limit: Any,
    capture_metrics: Optional[Dict[str, Any]] = None,
    default_limit: int = 24,
) -> bool:
    if (review_mode or "").strip().lower() != "lite":
        return False
    # Retry only for normal lite ingest flow to avoid adding latency to explicit review-only actions.
    if bool(review_only):
        return False
    captured = max(0, _to_int(reviews_captured) or 0)
    expected = _expected_lite_review_target(review_limit, reviews_total, default_limit=default_limit)
    total = _to_int(reviews_total)
    if total is not None and total >= 0:
        known_target = max(0, min(int(total), int(expected)))
        # If total is known and we've already captured up to that small total, do not retry.
        if captured >= known_target:
            return False
        # Skip retry when the upside is too small (adds latency without meaningful gain).
        remaining = max(0, known_target - captured)
        if known_target <= 6 or remaining <= 3:
            return False
    if captured >= expected:
        return False
    # Trigger only for clearly weak outcomes relative to expected lite target.
    min_expected = min(8, expected)
    low_watermark = max(3, int(expected * 0.3))
    if captured >= max(min_expected, low_watermark):
        return False
    review_response_count = 0
    if isinstance(capture_metrics, dict):
        review_response_count = max(0, _to_int(capture_metrics.get("review_response_count")) or 0)
    weak_capture_threshold = max(2, int(expected * 0.2))
    # Retry on weak parsed review yield or nearly no review responses captured.
    return captured <= weak_capture_threshold or review_response_count <= 1


def _build_conservative_retry_overrides(capture_overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(capture_overrides or {})
    # Conservative retry disables adaptive listing nav for this one attempt.
    merged["adaptive_listing_navigation"] = False
    if _normalize_lite_capture_strategy(merged.get("lite_capture_strategy")) != "normal":
        merged["lite_capture_strategy"] = "adaptive"
        merged["lite_adaptive_max_pulses"] = max(
            int(merged.get("lite_adaptive_max_pulses") or 0),
            6,
        )
        merged["review_page_wait_ms"] = max(int(merged.get("review_page_wait_ms") or 0), 1800)
        merged["review_wait_ms"] = max(int(merged.get("review_wait_ms") or 0), 8000)
    return merged


def _comparison_reviews_total(listing: Dict[str, Any]) -> Optional[int]:
    total = (
        _to_int(listing.get("reviews_total_count"))
        or _to_int((listing.get("reviews_summary") or {}).get("count"))
        or _to_int((listing.get("host") or {}).get("review_count"))
    )
    if total is None or total <= 0:
        return None
    return int(total)


def _comparison_coverage_snapshot(
    listing: Dict[str, Any],
    reviews: List[Dict[str, Any]],
    *,
    review_limit: int,
) -> Dict[str, Any]:
    limit = max(1, int(review_limit or 24))
    captured = _to_int(listing.get("reviews_captured_count"))
    if captured is None:
        captured = len(reviews or [])
    captured = max(0, int(captured))
    total = _comparison_reviews_total(listing)
    target = min(limit, total) if total else limit
    target = max(1, int(target))
    coverage = min(float(captured) / float(target), 1.0)
    return {
        "captured": captured,
        "total": total,
        "target": target,
        "coverage": coverage,
    }


def _comparison_coverage_violations(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
    *,
    review_limit: int,
    min_review_coverage: float,
) -> List[Dict[str, Any]]:
    violations: List[Dict[str, Any]] = []
    threshold = max(0.0, min(float(min_review_coverage), 1.0))
    for listing in listings:
        listing_id = str(listing.get("id") or listing.get("listing_id") or "").strip()
        snapshot = _comparison_coverage_snapshot(
            listing,
            reviews_by_listing.get(listing_id) or [],
            review_limit=review_limit,
        )
        coverage = float(snapshot.get("coverage") or 0.0)
        if coverage + 1e-9 >= threshold:
            continue
        violations.append(
            {
                "listing_id": listing_id,
                "title": listing.get("title"),
                "captured": int(snapshot.get("captured") or 0),
                "total": snapshot.get("total"),
                "target": int(snapshot.get("target") or 1),
                "coverage": round(coverage, 4),
                "required_coverage": round(threshold, 4),
            }
        )
    return violations


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _has_summary_payload(listing: Dict[str, Any]) -> bool:
    if not isinstance(listing, dict):
        return False
    pricing = listing.get("pricing")
    has_pricing = isinstance(pricing, dict) and any(
        pricing.get(key) not in (None, "", [])
        for key in ("price_total", "price_nightly", "price_display", "currency")
    )
    has_location = bool(listing.get("location"))
    has_photos = isinstance(listing.get("photos"), list) and len(listing.get("photos") or []) > 0
    has_amenities = isinstance(listing.get("amenities"), list) and len(listing.get("amenities") or []) > 0
    return any(
        [
            bool(listing.get("title")),
            bool(listing.get("description")),
            bool(listing.get("property_type")),
            has_location,
            has_photos,
            has_amenities,
            has_pricing,
        ]
    )


def _extract_capture_stages(listing: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    stages = {
        "summary_ready": False,
        "reviews_lite_ready": False,
        "reviews_full_ready": False,
    }
    if not isinstance(listing, dict):
        return stages
    source = listing.get("capture_stages")
    if isinstance(source, dict):
        for key in stages.keys():
            if key in source:
                stages[key] = _coerce_bool(source.get(key))
    stage = str(listing.get("capture_stage") or "").strip().lower()
    if stage in {"summary_ready", "reviews_lite_ready", "reviews_full_ready"}:
        stages[stage] = True
    if stages["reviews_full_ready"]:
        stages["reviews_lite_ready"] = True
        stages["summary_ready"] = True
    elif stages["reviews_lite_ready"]:
        stages["summary_ready"] = True
    return stages


def _derive_capture_stages(
    listing: Dict[str, Any],
    prior_listing: Optional[Dict[str, Any]],
    *,
    review_mode: str,
    reviews_captured: Optional[int],
    reviews_total: Optional[int],
) -> Dict[str, bool]:
    prior = _extract_capture_stages(prior_listing)
    captured = max(0, _to_int(reviews_captured) or 0)
    total = _to_int(reviews_total)
    summary_ready = _has_summary_payload(listing)
    lite_ready = review_mode in {"lite", "full"} and captured > 0
    full_ready = False
    if review_mode == "full" and captured > 0:
        if total and total > 0:
            full_ready = captured >= total
        else:
            full_ready = True

    stages = {
        "summary_ready": bool(prior.get("summary_ready")) or summary_ready,
        "reviews_lite_ready": bool(prior.get("reviews_lite_ready")) or lite_ready,
        "reviews_full_ready": bool(prior.get("reviews_full_ready")) or full_ready,
    }
    if stages["reviews_full_ready"]:
        stages["reviews_lite_ready"] = True
        stages["summary_ready"] = True
    elif stages["reviews_lite_ready"]:
        stages["summary_ready"] = True
    return stages


def _capture_stage_from_stages(stages: Dict[str, Any]) -> str:
    if stages.get("reviews_full_ready"):
        return "reviews_full_ready"
    if stages.get("reviews_lite_ready"):
        return "reviews_lite_ready"
    if stages.get("summary_ready"):
        return "summary_ready"
    return "capture_pending"


def _extract_listing_id(url: str) -> str:
    if not url:
        return ""
    match = LISTING_ID_PATTERN.search(url)
    if match:
        return match.group(1)
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return ""
    return path.replace("/", "_")


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _url_path(url: str) -> str:
    try:
        return urlparse(url).path.strip("/").lower()
    except Exception:
        return ""


def _is_airbnb_url(url: str) -> bool:
    host = _url_host(url)
    return host == "airbnb.com" or host.endswith(".airbnb.com")


def _is_airbnb_search_url(url: str) -> bool:
    path = _url_path(url)
    return _is_airbnb_url(url) and (path == "s" or path.startswith("s/"))


def _has_airbnb_room_id(url: str) -> bool:
    return bool(LISTING_ID_PATTERN.search(url or ""))


def _has_listing_detail_signal(listing: Dict[str, Any]) -> bool:
    location = listing.get("location") if isinstance(listing.get("location"), dict) else {}
    photos = listing.get("photos") if isinstance(listing.get("photos"), list) else []
    return any(
        [
            listing.get("title"),
            listing.get("description"),
            listing.get("description_snippet"),
            listing.get("property_type"),
            location.get("name"),
            photos,
        ]
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - float(started)) * 1000)


def _extract_review_offset(url: str) -> Optional[int]:
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


def _extract_review_limit(url: str) -> Optional[int]:
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


def _extract_review_offsets_from_responses(responses: List[Dict[str, Any]]) -> List[int]:
    offsets = set()
    for resp in responses:
        url = (resp.get("url") or "").lower()
        if "review" not in url:
            continue
        offset = _extract_review_offset(resp.get("url") or "")
        if offset is not None:
            offsets.add(offset)
    return sorted(offsets)


def _extract_review_max_span(responses: List[Dict[str, Any]]) -> int:
    max_span = 0
    for resp in responses:
        url = resp.get("url") or ""
        if "review" not in url.lower():
            continue
        offset = _extract_review_offset(url)
        limit = _extract_review_limit(url)
        if offset is None or limit is None:
            continue
        max_span = max(max_span, offset + limit)
    return max_span


def _extract_review_total_from_responses(responses: List[Dict[str, Any]]) -> Optional[int]:
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
        found = _walk(resp.get("data"), 6)
        if found is not None:
            best_count = max(best_count or 0, found)
    return best_count


def _review_capture_metrics(capture: Dict[str, Any]) -> Dict[str, Any]:
    responses = capture.get("responses") or []
    review_responses = [
        resp for resp in responses if "review" in (resp.get("url") or "").lower()
    ]
    offsets = _extract_review_offsets_from_responses(responses)
    max_span = _extract_review_max_span(responses)
    total = _extract_review_total_from_responses(responses)
    return {
        "response_count": len(responses),
        "review_response_count": len(review_responses),
        "offsets": offsets,
        "max_span": max_span,
        "total": total,
    }


def _set_query_value(query: Dict[str, List[str]], key: str, value: Any) -> None:
    if value is None or value == "":
        return
    query[key] = [str(value)]


def _payload_has_pricing_params(payload: Dict[str, Any]) -> bool:
    keys = (
        "check_in",
        "check_out",
        "checkin",
        "checkout",
        "adults",
        "children",
        "infants",
        "pets",
        "currency",
        "pricing_currency",
    )
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return True
    return False


def _url_has_booking_params(url: str) -> bool:
    if not url:
        return False
    try:
        query = parse_qs(urlparse(url).query or "")
    except Exception:
        return False
    for key in ("check_in", "check_out", "checkin", "checkout", "adults", "currency"):
        if query.get(key):
            return True
    return False


class JobRunner:
    def __init__(
        self,
        storage: Storage,
        capture: Optional[PlaywrightCapture] = None,
        rate_limiter: Optional[RateLimiter] = None,
        allowed_domains: Optional[List[str]] = None,
        capture_ttl_seconds: int = 0,
        include_reviews_default: bool = False,
        review_mode_default: str = "lite",
        review_limit_default: int = 24,
        capture_log_metrics: bool = False,
    ) -> None:
        self.storage = storage
        self.capture = capture or PlaywrightCapture()
        self.rate_limiter = rate_limiter or RateLimiter(0)
        self.allowed_domains = [d.lower() for d in (allowed_domains or [])]
        self.access_policy = CaptureAccessPolicy(self.allowed_domains)
        self.capture_store = CapturePayloadStore(storage)
        self.metric_recorder = JobMetricRecorder(storage, LOGGER)
        self.capture_ttl_seconds = max(0, int(capture_ttl_seconds))
        self.include_reviews_default = bool(include_reviews_default)
        review_mode_default = (review_mode_default or "").strip().lower() or "full"
        if review_mode_default not in {"none", "lite", "full"}:
            review_mode_default = "full"
        self.review_mode_default = review_mode_default
        self.review_limit_default = max(1, int(review_limit_default or 24))
        self.capture_log_metrics = bool(capture_log_metrics)

    def _record_job_metric(
        self,
        job_id: str,
        job_type: str,
        status: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.metric_recorder.record(job_id, job_type, status, metrics or {})

    def process_job(self, job: Dict[str, Any]) -> bool:
        job_type = job.get("job_type")
        job_id = job.get("job_id")
        payload = job.get("payload") or {}
        started = time.monotonic()
        metric_payload: Dict[str, Any] = {
            "job_id": job_id,
            "job_type": job_type,
        }

        try:
            LOGGER.info("Starting job %s (%s)", job_id, job_type)
            if job_type == "listing_ingest":
                metric_payload.update(self._process_listing(job_id, payload))
            elif job_type == "search":
                metric_payload.update(self._process_search(job_id, payload))
            elif job_type == "listing_enrich":
                metric_payload.update(self._process_listing_enrich(job_id, payload))
            elif job_type == "listing_compare":
                metric_payload.update(self._process_listing_compare(job_id, payload))
            else:
                self.storage.update_job(job_id, status="failed", error="Unknown job type")
                LOGGER.warning("Unknown job type: %s", job_type)
                metric_payload["job_total_ms"] = _elapsed_ms(started)
                self._record_job_metric(str(job_id or ""), str(job_type or "unknown"), "failed", metric_payload)
                return False
        except Exception as exc:
            self.storage.update_job(job_id, status="failed", error=str(exc))
            metric_payload["job_total_ms"] = _elapsed_ms(started)
            metric_payload["error"] = str(exc)
            self._record_job_metric(str(job_id or ""), str(job_type or "unknown"), "failed", metric_payload)
            LOGGER.exception("Job failed: %s", job_id)
            return False
        else:
            metric_payload["job_total_ms"] = _elapsed_ms(started)
            self._record_job_metric(str(job_id or ""), str(job_type or "unknown"), "complete", metric_payload)
            LOGGER.info("Finished job %s (%s)", job_id, job_type)
            return True

    def _store_capture(self, key: str, capture: Dict[str, Any]) -> List[str]:
        return self.capture_store.store(key, capture)

    def _log_capture_metrics(self, label: str, capture: Dict[str, Any]) -> None:
        if not self.capture_log_metrics:
            return

        timings = capture.get("timings") or {}
        if isinstance(timings, dict) and timings:
            ordered = [
                "total_ms",
                "browser_setup_ms",
                "resource_blocking_setup_ms",
                "navigation_ms",
                "review_modal_open_ms",
                "review_pagination_ms",
                "review_capture_ms",
                "html_capture_ms",
                "debug_capture_ms",
                "cleanup_ms",
            ]
            parts = []
            for key in ordered:
                value = timings.get(key)
                if value is not None:
                    parts.append(f"{key}={value}ms")
            for key, value in sorted(timings.items()):
                if key not in ordered:
                    parts.append(f"{key}={value}ms")
            LOGGER.info("Capture timings (%s): %s", label, ", ".join(parts) if parts else "n/a")

        resource_stats = capture.get("resource_blocking") or {}
        if isinstance(resource_stats, dict) and resource_stats.get("enabled"):
            LOGGER.info(
                "Resource blocking (%s): blocked=%s continued=%s by_type=%s by_pattern=%s",
                label,
                resource_stats.get("blocked_total", 0),
                resource_stats.get("continued_total", 0),
                resource_stats.get("blocked_by_type", {}),
                resource_stats.get("blocked_by_pattern", {}),
            )

    def _listing_has_pricing(self, listing: Dict[str, Any]) -> bool:
        pricing = listing.get("pricing")
        if not isinstance(pricing, dict):
            return False
        if pricing.get("price_total") is not None or pricing.get("price_nightly") is not None:
            return True
        if listing.get("price") is not None or listing.get("price_usd") is not None:
            return True
        return False

    def _build_pricing_url(self, url: str, payload: Dict[str, Any]) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        query = parse_qs(parsed.query or "")

        check_in = payload.get("check_in") or payload.get("checkin")
        check_out = payload.get("check_out") or payload.get("checkout")
        adults = payload.get("adults")
        children = payload.get("children")
        infants = payload.get("infants")
        pets = payload.get("pets")
        currency = payload.get("currency") or payload.get("pricing_currency")

        _set_query_value(query, "check_in", check_in)
        _set_query_value(query, "check_out", check_out)
        _set_query_value(query, "adults", adults)
        _set_query_value(query, "children", children)
        _set_query_value(query, "infants", infants)
        _set_query_value(query, "pets", pets)
        _set_query_value(query, "currency", currency and str(currency).upper())

        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _process_listing(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        job_metrics: Dict[str, Any] = {"kind": "listing_ingest"}
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        if not self._is_allowed_url(url):
            raise ValueError("url domain is not allowed")
        if _is_airbnb_search_url(url):
            raise ValueError(
                "listing_ingest requires an Airbnb listing URL. Use New search for Airbnb /s/... search URLs."
            )
        if _is_airbnb_url(url) and not _has_airbnb_room_id(url):
            raise ValueError("listing_ingest requires an Airbnb /rooms/{id} listing URL.")

        listing_id = _extract_listing_id(url) or job_id
        include_reviews = payload.get("include_reviews")
        review_mode = (payload.get("review_mode") or "").strip().lower()
        review_limit = payload.get("review_limit")
        review_only = payload.get("review_only")
        if review_only is None:
            review_only = False
        if review_mode and review_mode not in {"none", "lite", "full"}:
            review_mode = ""
        if include_reviews is None and review_mode:
            include_reviews = review_mode != "none"
        if include_reviews is None:
            include_reviews = self.include_reviews_default
        if not review_mode:
            review_mode = self.review_mode_default if include_reviews else "none"
        if review_mode == "none":
            include_reviews = False
            review_only = False
        else:
            include_reviews = True

        require_pricing = _payload_has_pricing_params(payload) or _url_has_booking_params(url)
        if review_only:
            require_pricing = False

        capture_url = self._build_pricing_url(url, payload)
        capture_overrides = _extract_capture_overrides(payload)
        if review_limit is None:
            review_limit = self.review_limit_default
        if review_mode == "lite" and review_limit:
            capture_overrides["lite_review_target"] = int(review_limit)
        job_metrics["capture_overrides"] = capture_overrides

        if not payload.get("force"):
            cached = self._check_cached_listing(
                listing_id,
                review_mode=review_mode,
                require_pricing=require_pricing,
            )
            if cached:
                self.storage.update_job(job_id, status="complete", result_ref=listing_id)
                LOGGER.info("Skipped capture for %s (cached)", listing_id)
                job_metrics.update(
                    {
                        "listing_id": listing_id,
                        "cached_hit": True,
                        "review_mode": review_mode,
                        "require_pricing": bool(require_pricing),
                    }
                )
                return job_metrics

        self.rate_limiter.wait()
        capture = self.capture.capture_listing(
            capture_url,
            include_reviews=bool(include_reviews),
            review_mode=review_mode,
            review_only=bool(review_only),
            capture_overrides=capture_overrides or None,
        )
        capture_timings = capture.get("timings") if isinstance(capture.get("timings"), dict) else {}
        review_flow = capture.get("review_flow") if isinstance(capture.get("review_flow"), dict) else {}
        job_metrics.update(
            {
                "listing_id": listing_id,
                "cached_hit": False,
                "review_mode": review_mode,
                "require_pricing": bool(require_pricing),
                "capture_url": capture_url,
                "capture_duration_ms": capture.get("duration_ms"),
                "capture_timings": capture_timings,
                "capture_response_count": len(capture.get("responses") or []),
                "capture_error_count": len(capture.get("errors") or []),
                "review_flow": review_flow,
            }
        )
        self._log_capture_metrics(f"listing:{listing_id}", capture)
        raw_ids = self._store_capture(listing_id, capture)
        job_metrics["raw_payload_count"] = len(raw_ids)
        metrics = _review_capture_metrics(capture) if include_reviews else {}
        job_metrics["capture_completeness"] = metrics
        if include_reviews:
            LOGGER.info(
                "Review metrics for %s: responses=%s review_responses=%s offsets=%s max_span=%s total=%s",
                listing_id,
                metrics.get("response_count"),
                metrics.get("review_response_count"),
                metrics.get("offsets"),
                metrics.get("max_span"),
                metrics.get("total"),
            )

        parse_started = time.monotonic()
        listing, reviews = parse_capture(capture, listing_id, capture_url)
        parse_ms = _elapsed_ms(parse_started)
        if review_mode == "lite" and review_limit and reviews:
            reviews = reviews[: int(review_limit)]

        reviews_total = (
            metrics.get("total")
            or (listing.get("reviews_summary") or {}).get("count")
            or (listing.get("host") or {}).get("review_count")
        )
        reviews_captured = len(reviews or [])
        first_reviews_captured = int(reviews_captured)

        retry_attempted = False
        retry_used = False
        retry_reason = None
        retry_reviews_captured = 0
        retry_reviews_total = 0
        retry_capture_ms = 0
        retry_parse_ms = 0
        if _should_retry_lite_capture_once(
            review_mode=review_mode,
            review_only=bool(review_only),
            reviews_captured=reviews_captured,
            reviews_total=reviews_total,
            review_limit=review_limit,
            capture_metrics=metrics,
            default_limit=self.review_limit_default,
        ):
            retry_attempted = True
            retry_reason = "lite_review_capture_low_coverage"
            retry_overrides = _build_conservative_retry_overrides(capture_overrides)
            LOGGER.info("Retrying lite capture once for %s with conservative nav settings", listing_id)
            self.rate_limiter.wait()
            retry_capture_started = time.monotonic()
            retry_capture = self.capture.capture_listing(
                capture_url,
                include_reviews=bool(include_reviews),
                review_mode=review_mode,
                review_only=bool(review_only),
                capture_overrides=retry_overrides,
            )
            retry_capture_ms = _to_int(retry_capture.get("duration_ms")) or _elapsed_ms(retry_capture_started)
            self._log_capture_metrics(f"listing_retry:{listing_id}", retry_capture)
            retry_raw_ids = self._store_capture(listing_id, retry_capture)
            raw_ids.extend(retry_raw_ids)
            retry_metrics = _review_capture_metrics(retry_capture) if include_reviews else {}
            retry_parse_started = time.monotonic()
            retry_listing, retry_reviews = parse_capture(retry_capture, listing_id, capture_url)
            retry_parse_ms = _elapsed_ms(retry_parse_started)
            parse_ms += retry_parse_ms
            if review_mode == "lite" and review_limit and retry_reviews:
                retry_reviews = retry_reviews[: int(review_limit)]
            retry_reviews_total = (
                retry_metrics.get("total")
                or (retry_listing.get("reviews_summary") or {}).get("count")
                or (retry_listing.get("host") or {}).get("review_count")
                or 0
            )
            retry_reviews_captured = len(retry_reviews or [])
            retry_review_response_count = _to_int(retry_metrics.get("review_response_count")) or 0
            base_review_response_count = _to_int(metrics.get("review_response_count")) or 0
            if (
                retry_reviews_captured > reviews_captured
                or (
                    retry_reviews_captured == reviews_captured
                    and retry_review_response_count > base_review_response_count
                )
            ):
                retry_used = True
                capture = retry_capture
                metrics = retry_metrics
                listing = retry_listing
                reviews = retry_reviews
                reviews_total = retry_reviews_total or reviews_total
                reviews_captured = retry_reviews_captured
                capture_timings = capture.get("timings") if isinstance(capture.get("timings"), dict) else {}
                review_flow = capture.get("review_flow") if isinstance(capture.get("review_flow"), dict) else {}
                job_metrics.update(
                    {
                        "capture_duration_ms": capture.get("duration_ms"),
                        "capture_timings": capture_timings,
                        "capture_response_count": len(capture.get("responses") or []),
                        "capture_error_count": len(capture.get("errors") or []),
                        "review_flow": review_flow,
                    }
                )
                job_metrics["capture_completeness"] = metrics
        parser_meta = listing.get("parser_meta") if isinstance(listing.get("parser_meta"), dict) else {}
        job_metrics["parse_ms"] = parse_ms
        job_metrics["raw_payload_count"] = len(raw_ids)
        if parser_meta:
            job_metrics["parser_drift"] = compact_parser_meta(parser_meta)
        job_metrics["lite_retry"] = {
            "attempted": bool(retry_attempted),
            "used_retry_result": bool(retry_used),
            "reason": retry_reason,
            "first_reviews_captured": int(first_reviews_captured),
            "retry_reviews_captured": int(retry_reviews_captured or 0),
            "retry_reviews_total": int(retry_reviews_total or 0),
            "retry_capture_ms": int(retry_capture_ms or 0),
            "retry_parse_ms": int(retry_parse_ms or 0),
        }
        listing["raw_payload_refs"] = raw_ids
        existing_listing = None
        if capture.get("review_only"):
            # Avoid clobbering existing listing details when in review-only mode.
            if not listing.get("title") and not listing.get("description") and not listing.get("photos"):
                existing_listing = self.storage.get_listing(listing_id)
                if existing_listing:
                    existing_listing["raw_payload_refs"] = raw_ids
                    listing = existing_listing
            else:
                existing_listing = self.storage.get_listing(listing_id)
        reviews_total = (
            metrics.get("total")
            or (listing.get("reviews_summary") or {}).get("count")
            or (listing.get("host") or {}).get("review_count")
            or reviews_total
        )
        reviews_captured = len(reviews or [])
        if existing_listing:
            existing_captured = existing_listing.get("reviews_captured_count")
            if existing_captured:
                reviews_captured = max(int(existing_captured), reviews_captured or 0)
        prior_listing = existing_listing or self.storage.get_listing(listing_id)
        capture_stages = _derive_capture_stages(
            listing,
            prior_listing,
            review_mode=review_mode,
            reviews_captured=reviews_captured,
            reviews_total=reviews_total,
        )
        listing["capture_stages"] = capture_stages
        listing["capture_stage"] = _capture_stage_from_stages(capture_stages)
        listing["review_mode"] = review_mode
        listing["reviews_captured_count"] = reviews_captured or None
        listing["reviews_total_count"] = reviews_total or None
        if reviews_total and reviews_captured:
            listing["review_coverage"] = round(min(reviews_captured / reviews_total, 1.0), 3)
        persist_started = time.monotonic()
        listing = normalize_listing(listing)
        listing = _apply_fx_pricing(listing)
        listing["validation"] = validate_listing(listing)
        job_metrics["listing_detail_signal"] = bool(_has_listing_detail_signal(listing))
        if not bool(review_only) and not _has_listing_detail_signal(listing):
            raise ValueError(
                "Listing capture completed but no listing details were parsed. "
                "Check that the URL is a listing detail page and retry with force if needed."
            )
        self.storage.upsert_listing(listing)
        inserted = 0
        if reviews:
            inserted = self.storage.add_reviews(listing_id, reviews)
            LOGGER.info("Stored %s reviews for %s", inserted, listing_id)
        persist_ms = _elapsed_ms(persist_started)
        self.storage.update_job(job_id, status="complete", result_ref=listing_id)
        job_metrics.update(
            {
                "persist_ms": persist_ms,
                "reviews_inserted": int(inserted),
                "reviews_captured_count": reviews_captured or 0,
                "reviews_total_count": reviews_total or 0,
                "capture_stage": listing.get("capture_stage"),
                "capture_stages": listing.get("capture_stages"),
            }
        )
        return job_metrics

    def _process_search(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        job_metrics: Dict[str, Any] = {"kind": "search"}
        search_url = payload.get("search_url")
        if search_url and not self._is_allowed_url(search_url):
            raise ValueError("search_url domain is not allowed")

        capture_overrides = _extract_capture_overrides(payload)
        job_metrics["capture_overrides"] = capture_overrides
        self.rate_limiter.wait()
        capture = self.capture.capture_search(payload, capture_overrides=capture_overrides or None)
        capture_timings = capture.get("timings") if isinstance(capture.get("timings"), dict) else {}
        job_metrics.update(
            {
                "capture_duration_ms": capture.get("duration_ms"),
                "capture_timings": capture_timings,
                "capture_response_count": len(capture.get("responses") or []),
                "capture_error_count": len(capture.get("errors") or []),
                "captured_url": capture.get("url"),
            }
        )
        self._log_capture_metrics(f"search:{capture.get('url') or job_id}", capture)
        raw_ids = self._store_capture(job_id, capture)
        job_metrics["raw_payload_count"] = len(raw_ids)

        parse_started = time.monotonic()
        listings, parser_meta = parse_search_from_responses_with_meta(
            capture.get("responses") or [],
            capture.get("url"),
        )
        normalized: List[Dict[str, Any]] = []
        for listing in listings:
            listing = normalize_search_listing(listing)
            listing = _apply_fx_pricing(listing)
            listing["validation"] = validate_search_listing(listing)
            normalized.append(listing)
        job_metrics["parse_ms"] = _elapsed_ms(parse_started)
        if parser_meta:
            job_metrics["parser_drift"] = compact_parser_meta(parser_meta)
        result = {
            "search_url": capture.get("url"),
            "response_count": len(capture.get("responses", [])),
            "listing_count": len(normalized),
            "captured_at": _now_iso(),
            "parser_meta": parser_meta or {},
        }
        persist_started = time.monotonic()
        run_id = self.storage.add_search_run(payload, result, raw_ids)
        if normalized:
            self.storage.add_search_listings(run_id, normalized)
        job_metrics["persist_ms"] = _elapsed_ms(persist_started)
        self.storage.update_job(job_id, status="complete", result_ref=run_id)
        job_metrics.update(
            {
                "run_id": run_id,
                "listing_count": len(normalized),
            }
        )
        return job_metrics

    def _process_listing_enrich(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {"kind": "listing_enrich"}
        listing_id = str(payload.get("listing_id") or "").strip()
        if not listing_id:
            raise ValueError("listing_id is required")
        kind = payload.get("kind") or "listing_summary"
        if kind != "listing_summary":
            raise ValueError(f"Unsupported enrichment kind: {kind}")

        listing = self.storage.get_listing(listing_id)
        if not listing:
            raise ValueError("Listing not found")
        reviews = self.storage.list_reviews(listing_id, limit=200)
        model = payload.get("model")
        model, input_hash = build_summary_request(listing, reviews, model=model)
        if payload.get("input_hash") and payload.get("input_hash") != input_hash:
            LOGGER.info("Listing %s input hash changed since enqueue", listing_id)

        llm_started = time.monotonic()
        output = generate_listing_summary(listing, reviews, model=model)
        llm_ms = _elapsed_ms(llm_started)
        persist_started = time.monotonic()
        enrichment_id = self.storage.add_enrichment(
            listing_id,
            kind,
            model,
            LLM_PROMPT_VERSION,
            input_hash,
            output,
        )
        persist_ms = _elapsed_ms(persist_started)
        self.storage.update_job(job_id, status="complete", result_ref=enrichment_id)
        metrics.update(
            {
                "listing_id": listing_id,
                "kind": kind,
                "model": model,
                "reviews_used": len(reviews),
                "llm_ms": llm_ms,
                "persist_ms": persist_ms,
            }
        )
        return metrics

    def _process_listing_compare(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {"kind": "listing_compare"}
        listing_ids = payload.get("listing_ids") or []
        review_limit = _coerce_int_range(payload.get("review_limit"), 1, 50) or 24
        require_min_coverage = _coerce_bool(payload.get("require_min_coverage"))
        min_review_coverage = _coerce_float_range(payload.get("min_review_coverage"), 0.0, 1.0)
        if min_review_coverage is None:
            min_review_coverage = 0.5
        if not isinstance(listing_ids, list) or len(listing_ids) < 2:
            raise ValueError("listing_ids must include at least 2 items")
        listing_ids = [str(value).strip() for value in listing_ids if str(value).strip()]
        if len(listing_ids) < 2:
            raise ValueError("listing_ids must include at least 2 items")

        listings = []
        reviews_by_listing: Dict[str, Any] = {}
        for listing_id in listing_ids:
            listing = self.storage.get_listing(listing_id)
            if not listing:
                raise ValueError(f"Listing not found: {listing_id}")
            listings.append(listing)
            reviews_by_listing[str(listing_id)] = self.storage.list_reviews(
                listing_id,
                limit=max(1, int(review_limit)),
            )

        if require_min_coverage:
            violations = _comparison_coverage_violations(
                listings,
                reviews_by_listing,
                review_limit=int(review_limit),
                min_review_coverage=float(min_review_coverage),
            )
            if violations:
                raise ValueError(
                    "Comparison blocked by minimum review coverage policy: "
                    + json.dumps({"violations": violations}, ensure_ascii=False)
                )

        model = payload.get("model")
        model, input_hash = build_comparison_request(listings, reviews_by_listing, model=model)
        compare_key = f"compare:{input_hash}"

        llm_started = time.monotonic()
        output = generate_listing_comparison(listings, reviews_by_listing, model=model)
        llm_ms = _elapsed_ms(llm_started)
        persist_started = time.monotonic()
        enrichment_id = self.storage.add_enrichment(
            compare_key,
            "listing_comparison",
            model,
            COMPARISON_PROMPT_VERSION,
            input_hash,
            output,
        )
        persist_ms = _elapsed_ms(persist_started)
        self.storage.update_job(job_id, status="complete", result_ref=enrichment_id)
        metrics.update(
            {
                "listing_ids_count": len(listing_ids),
                "model": model,
                "review_limit": int(review_limit),
                "require_min_coverage": bool(require_min_coverage),
                "min_review_coverage": float(min_review_coverage),
                "llm_ms": llm_ms,
                "persist_ms": persist_ms,
            }
        )
        return metrics

    def _is_allowed_url(self, url: str) -> bool:
        return self.access_policy.is_allowed_url(url)

    def _check_cached_listing(
        self,
        listing_id: str,
        review_mode: str,
        require_pricing: bool = False,
    ) -> bool:
        if self.capture_ttl_seconds <= 0:
            return False
        listing = self.storage.get_listing(listing_id)
        if not listing:
            return False
        captured_at = listing.get("captured_at")
        captured_ts = _parse_iso_timestamp(captured_at)
        if captured_ts is None:
            return False
        if (time.time() - captured_ts) > self.capture_ttl_seconds:
            return False
        review_mode = (review_mode or "none").lower()
        if review_mode != "none":
            reviews = self.storage.list_reviews(listing_id, limit=1)
            if not reviews:
                return False
            if review_mode == "full":
                if listing.get("review_mode") != "full":
                    return False
                total = listing.get("reviews_total_count")
                captured = listing.get("reviews_captured_count")
                if total and captured and int(captured) < int(total):
                    return False
        if require_pricing and not self._listing_has_pricing(listing):
            return False
        return True


_PRICE_NUMERIC_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")


def _coerce_price_from_display(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("\u00a0", " ")
    match = _PRICE_NUMERIC_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _apply_fx_pricing(listing: Dict[str, Any]) -> Dict[str, Any]:
    pricing = listing.get("pricing")
    if not isinstance(pricing, dict):
        return listing
    currency = (pricing.get("currency") or listing.get("currency") or "").upper()
    if not currency:
        return listing
    fx = get_fx_rate(currency, "USD")
    if not fx:
        return listing
    rate = fx.get("rate")
    if not rate:
        return listing

    pricing["currency"] = currency
    price_total = pricing.get("price_total")
    price_nightly = pricing.get("price_nightly")
    if price_total is None and price_nightly is None:
        derived = _coerce_price_from_display(pricing.get("price_display"))
        price_type = str(pricing.get("price_type") or "").strip().lower()
        if derived is not None:
            if price_type == "nightly":
                price_nightly = derived
                pricing["price_nightly"] = derived
            else:
                price_total = derived
                pricing["price_total"] = derived
    if price_total is not None:
        try:
            pricing["price_total_usd"] = round(float(price_total) * float(rate), 2)
        except Exception:
            pass
    if price_nightly is not None:
        try:
            pricing["price_nightly_usd"] = round(float(price_nightly) * float(rate), 2)
        except Exception:
            pass
    pricing["fx_rate"] = float(rate)
    pricing["fx_timestamp"] = fx.get("as_of")
    pricing["fx_source"] = fx.get("source")
    pricing["fx_stale"] = bool(fx.get("stale"))
    listing["pricing"] = pricing
    listing["price_usd_total"] = pricing.get("price_total_usd")
    listing["price_usd_nightly"] = pricing.get("price_nightly_usd")
    listing["price_usd"] = pricing.get("price_total_usd") or pricing.get("price_nightly_usd")
    return listing


def _parse_iso_timestamp(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        return parsed.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None
