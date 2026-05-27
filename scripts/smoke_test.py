import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = os.getenv("RENTAL_API_BASE", "http://localhost:5002").rstrip("/")
LISTING_URL = os.getenv("RENTAL_TEST_LISTING_URL", "").strip()
SEARCH_LOCATION = os.getenv("RENTAL_TEST_SEARCH_LOCATION", "").strip()
TIMEOUT_SECONDS = int(os.getenv("RENTAL_TEST_TIMEOUT", "300"))
LISTING_TIMEOUT_SECONDS = int(os.getenv("RENTAL_TEST_LISTING_TIMEOUT", TIMEOUT_SECONDS))
SEARCH_TIMEOUT_SECONDS = int(os.getenv("RENTAL_TEST_SEARCH_TIMEOUT", TIMEOUT_SECONDS))
POLL_INTERVAL = float(os.getenv("RENTAL_TEST_POLL_INTERVAL", "2"))
HEALTH_TIMEOUT_SECONDS = int(os.getenv("RENTAL_TEST_HEALTH_TIMEOUT", "30"))
INCLUDE_REVIEWS = os.getenv("RENTAL_TEST_INCLUDE_REVIEWS", "").strip().lower() in {"1", "true", "yes", "y"}
SKIP_LISTING = os.getenv("RENTAL_TEST_SKIP_LISTING", "").strip().lower() in {"1", "true", "yes", "y"}
SKIP_SEARCH = os.getenv("RENTAL_TEST_SKIP_SEARCH", "").strip().lower() in {"1", "true", "yes", "y"}


def _request_json(method: str, path: str, payload=None):
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {body}")
    except URLError as exc:
        raise RuntimeError(f"Request failed: {exc}")

    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def _wait_for_health():
    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    last_error = None
    while time.time() < deadline:
        try:
            return _request_json("GET", "/health")
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"API health check failed after {HEALTH_TIMEOUT_SECONDS}s: {last_error}")


def _poll_job(job_id: str, timeout_seconds: int):
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        payload = _request_json("GET", f"/api/v1/jobs/{job_id}")
        job = payload.get("job") or {}
        status = job.get("status")
        last_status = status
        print(f"job {job_id} status={status}")
        if status in {"complete", "failed"}:
            return job
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"Timed out waiting for job (last status={last_status})")


def main():
    health = _wait_for_health()
    print("health", health)

    if not LISTING_URL and not SEARCH_LOCATION:
        print("Set RENTAL_TEST_LISTING_URL or RENTAL_TEST_SEARCH_LOCATION to run jobs.")
        return 0

    if LISTING_URL and not SKIP_LISTING:
        print("Submitting listing ingest job...")
        job_payload = _request_json(
            "POST",
            "/api/v1/listings/ingest",
            {"url": LISTING_URL, "include_reviews": INCLUDE_REVIEWS},
        )
        job = job_payload.get("job") or {}
        job_id = job.get("job_id")
        if not job_id:
            raise RuntimeError("No job_id returned for listing ingest")
        job = _poll_job(job_id, LISTING_TIMEOUT_SECONDS)
        if job.get("status") != "complete":
            raise RuntimeError(f"Listing job failed: {job}")
        listing_id = job.get("result_ref")
        if listing_id:
            listing = _request_json("GET", f"/api/v1/listings/{listing_id}")
            listing_payload = listing.get("listing") or {}
            raw_refs = listing_payload.get("raw_payload_refs") or []
            print("listing", listing_id, "raw_payload_refs", len(raw_refs))

    if SEARCH_LOCATION and not SKIP_SEARCH:
        print("Submitting search job...")
        job_payload = _request_json("POST", "/api/v1/search", {"location": SEARCH_LOCATION})
        job = job_payload.get("job") or {}
        job_id = job.get("job_id")
        if not job_id:
            raise RuntimeError("No job_id returned for search")
        job = _poll_job(job_id, SEARCH_TIMEOUT_SECONDS)
        if job.get("status") != "complete":
            raise RuntimeError(f"Search job failed: {job}")
        print("search run", job.get("result_ref"))

    print("Smoke test completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
