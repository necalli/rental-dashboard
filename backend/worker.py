import logging
import os
import socket
import time

from services.job_runner import JobRunner
from services.playwright_capture import PlaywrightCapture
from services.rate_limiter import RateLimiter
from services.storage import Storage


def _parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _parse_csv(value: str) -> list:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_worker_id() -> str:
    host = socket.gethostname() or "worker-host"
    return f"{host}-{os.getpid()}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

    poll_interval = float(os.getenv("RENTAL_WORKER_POLL_INTERVAL", "2"))
    worker_id = str(os.getenv("RENTAL_WORKER_ID") or "").strip() or _default_worker_id()
    heartbeat_seconds = max(0, int(os.getenv("RENTAL_WORKER_HEARTBEAT_SECONDS", "30")))
    stale_job_seconds = max(0, int(os.getenv("RENTAL_WORKER_STALE_JOB_SECONDS", "900")))
    max_job_attempts = max(1, int(os.getenv("RENTAL_WORKER_MAX_JOB_ATTEMPTS", "3")))
    worker_job_types = _parse_csv(os.getenv("RENTAL_WORKER_JOB_TYPES", ""))
    headless = _parse_bool(os.getenv("RENTAL_PLAYWRIGHT_HEADLESS", "true"), True)
    timeout_ms = int(os.getenv("RENTAL_PLAYWRIGHT_TIMEOUT_MS", "30000"))
    wait_after_ms = int(os.getenv("RENTAL_PLAYWRIGHT_WAIT_MS", "2500"))
    capture_html = _parse_bool(os.getenv("RENTAL_CAPTURE_HTML", "true"), True)
    max_responses = int(os.getenv("RENTAL_CAPTURE_MAX_RESPONSES", "200"))
    capture_timeout_ms = int(os.getenv("RENTAL_CAPTURE_TIMEOUT_MS", "120000"))
    allowed_domains = _parse_csv(os.getenv("RENTAL_CAPTURE_ALLOWLIST", "airbnb.com"))
    response_domains = _parse_csv(os.getenv("RENTAL_CAPTURE_RESPONSE_DOMAINS", "airbnb.com"))
    response_url_allowlist = _parse_csv(os.getenv("RENTAL_CAPTURE_RESPONSE_URL_ALLOWLIST", ""))
    capture_ttl = int(os.getenv("RENTAL_CAPTURE_TTL_SECONDS", "86400"))
    min_interval_ms = int(os.getenv("RENTAL_CAPTURE_MIN_INTERVAL_MS", "1500"))
    error_backoff_ms = int(os.getenv("RENTAL_CAPTURE_ERROR_BACKOFF_MS", "5000"))
    include_reviews_default = _parse_bool(os.getenv("RENTAL_CAPTURE_INCLUDE_REVIEWS", "false"), False)
    review_mode_default = os.getenv("RENTAL_CAPTURE_REVIEW_MODE", "lite").strip().lower() or "lite"
    review_limit_default = int(os.getenv("RENTAL_CAPTURE_REVIEW_LIMIT", "24"))
    review_scroll_steps = int(os.getenv("RENTAL_CAPTURE_REVIEW_SCROLL_STEPS", "4"))
    review_scroll_pulses = int(os.getenv("RENTAL_CAPTURE_REVIEW_SCROLL_PULSES", "4"))
    review_wait_ms = int(os.getenv("RENTAL_CAPTURE_REVIEW_WAIT_MS", "5000"))
    review_pagination_passes = int(os.getenv("RENTAL_CAPTURE_REVIEW_PAGINATION_PASSES", "6"))
    review_page_wait_ms = int(os.getenv("RENTAL_CAPTURE_REVIEW_PAGE_WAIT_MS", "1500"))
    capture_debug = _parse_bool(os.getenv("RENTAL_CAPTURE_DEBUG", "false"), False)
    capture_block_resources = _parse_bool(os.getenv("RENTAL_CAPTURE_BLOCK_RESOURCES", "false"), False)
    blocked_resource_types = _parse_csv(os.getenv("RENTAL_CAPTURE_BLOCKED_RESOURCE_TYPES", "image,media,font"))
    blocked_url_patterns = _parse_csv(os.getenv("RENTAL_CAPTURE_BLOCKED_URL_PATTERNS", ""))
    adaptive_search_navigation = _parse_bool(os.getenv("RENTAL_CAPTURE_ADAPTIVE_SEARCH_NAV", "true"), True)
    adaptive_search_html_wait = _parse_bool(os.getenv("RENTAL_CAPTURE_ADAPTIVE_SEARCH_HTML", "true"), True)
    search_response_target = int(os.getenv("RENTAL_CAPTURE_SEARCH_RESPONSE_TARGET", "10"))
    adaptive_wait_poll_ms = int(os.getenv("RENTAL_CAPTURE_ADAPTIVE_WAIT_POLL_MS", "120"))
    search_networkidle_fallback_ms = int(os.getenv("RENTAL_CAPTURE_SEARCH_NETWORKIDLE_FALLBACK_MS", "300"))
    adaptive_listing_navigation = _parse_bool(os.getenv("RENTAL_CAPTURE_ADAPTIVE_LISTING_NAV", "false"), False)
    listing_response_target = int(os.getenv("RENTAL_CAPTURE_LISTING_RESPONSE_TARGET", "12"))
    listing_navigation_wait_cap_ms = int(os.getenv("RENTAL_CAPTURE_LISTING_NAV_WAIT_CAP_MS", "1800"))
    listing_networkidle_fallback_ms = int(
        os.getenv("RENTAL_CAPTURE_LISTING_NETWORKIDLE_FALLBACK_MS", "700")
    )
    capture_log_metrics = _parse_bool(os.getenv("RENTAL_CAPTURE_LOG_METRICS", "false"), False)
    review_only_env = os.getenv("RENTAL_CAPTURE_REVIEW_ONLY")
    if review_only_env is None or review_only_env == "":
        review_only = capture_debug
    else:
        review_only = _parse_bool(review_only_env, False)
    capture_debug_screenshots = _parse_bool(os.getenv("RENTAL_CAPTURE_DEBUG_SCREENSHOTS", "false"), False)

    storage = Storage()
    capture = PlaywrightCapture(
        headless=headless,
        navigation_timeout_ms=timeout_ms,
        wait_after_load_ms=wait_after_ms,
        capture_html=capture_html,
        response_domain_allowlist=response_domains,
        response_url_allowlist=response_url_allowlist,
        max_responses=max_responses,
        capture_timeout_ms=capture_timeout_ms,
        review_scroll_steps=review_scroll_steps,
        review_scroll_pulses=review_scroll_pulses,
        review_wait_ms=review_wait_ms,
        review_pagination_passes=review_pagination_passes,
        review_page_wait_ms=review_page_wait_ms,
        review_only=review_only,
        debug=capture_debug,
        debug_screenshots=capture_debug_screenshots,
        block_resources=capture_block_resources,
        blocked_resource_types=blocked_resource_types,
        blocked_url_patterns=blocked_url_patterns,
        adaptive_search_navigation=adaptive_search_navigation,
        adaptive_search_html_wait=adaptive_search_html_wait,
        search_response_target=search_response_target,
        adaptive_wait_poll_ms=adaptive_wait_poll_ms,
        search_networkidle_fallback_ms=search_networkidle_fallback_ms,
        adaptive_listing_navigation=adaptive_listing_navigation,
        listing_response_target=listing_response_target,
        listing_navigation_wait_cap_ms=listing_navigation_wait_cap_ms,
        listing_networkidle_fallback_ms=listing_networkidle_fallback_ms,
    )
    rate_limiter = RateLimiter(min_interval_ms=min_interval_ms)
    runner = JobRunner(
        storage,
        capture,
        rate_limiter=rate_limiter,
        allowed_domains=allowed_domains,
        capture_ttl_seconds=capture_ttl,
        include_reviews_default=include_reviews_default,
        review_mode_default=review_mode_default,
        review_limit_default=review_limit_default,
        capture_log_metrics=capture_log_metrics,
    )

    logging.info(
        "Worker started id=%s poll_interval=%.2fs job_types=%s heartbeat_seconds=%s",
        worker_id,
        poll_interval,
        worker_job_types or ["*"],
        heartbeat_seconds,
    )
    processed_count = 0
    success_count = 0
    failure_count = 0
    idle_poll_count = 0
    last_heartbeat = time.monotonic()

    while True:
        if stale_job_seconds > 0:
            recovered = storage.recover_stale_jobs(
                stale_after_seconds=stale_job_seconds,
                max_attempts=max_job_attempts,
            )
            if recovered:
                logging.warning("Worker id=%s recovered %s stale running job(s)", worker_id, recovered)

        job = storage.claim_next_job(job_types=worker_job_types or None, worker_id=worker_id)
        if job:
            processed_count += 1
            logging.info(
                "Worker id=%s claimed job_id=%s job_type=%s",
                worker_id,
                job.get("job_id"),
                job.get("job_type"),
            )
            storage.heartbeat_job(str(job.get("job_id") or ""), worker_id=worker_id)
            success = runner.process_job(job)
            if success:
                success_count += 1
            else:
                failure_count += 1
            if not success and error_backoff_ms > 0:
                time.sleep(error_backoff_ms / 1000)
        else:
            idle_poll_count += 1
            time.sleep(poll_interval)

        if heartbeat_seconds > 0 and (time.monotonic() - last_heartbeat) >= heartbeat_seconds:
            counts = storage.get_job_status_counts()
            logging.info(
                "Worker heartbeat id=%s processed=%s success=%s failed=%s idle_polls=%s queue=%s",
                worker_id,
                processed_count,
                success_count,
                failure_count,
                idle_poll_count,
                counts,
            )
            last_heartbeat = time.monotonic()


if __name__ == "__main__":
    main()
