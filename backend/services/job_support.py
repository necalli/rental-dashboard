import logging
from typing import Any, Dict, List
from urllib.parse import urlparse

from .raw_store import write_raw_payload
from .storage import Storage


class JobMetricRecorder:
    def __init__(self, storage: Storage, logger: logging.Logger) -> None:
        self.storage = storage
        self.logger = logger

    def record(
        self,
        job_id: str,
        job_type: str,
        status: str,
        metrics: Dict[str, Any],
    ) -> None:
        try:
            self.storage.add_job_metric(job_id, job_type, status, metrics or {})
        except Exception:
            self.logger.exception("Failed to persist job metrics for %s (%s)", job_id, job_type)


class CapturePayloadStore:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def store(self, key: str, capture: Dict[str, Any]) -> List[str]:
        raw_ids: List[str] = []

        html = capture.get("html")
        if html:
            path = write_raw_payload("page_html", key, {"url": capture.get("url"), "html": html})
            raw_id = self.storage.add_raw_payload("page_html", path, {"url": capture.get("url")})
            raw_ids.append(raw_id)

        for response in capture.get("responses", []):
            payload = {
                "url": response.get("url"),
                "status": response.get("status"),
                "content_type": response.get("content_type"),
                "data": response.get("data"),
            }
            path = write_raw_payload("network_json", key, payload)
            raw_id = self.storage.add_raw_payload(
                "network_json",
                path,
                {"url": response.get("url"), "status": response.get("status")},
            )
            raw_ids.append(raw_id)

        errors = capture.get("errors") or []
        if errors:
            path = write_raw_payload("capture_errors", key, {"errors": errors})
            raw_id = self.storage.add_raw_payload("capture_errors", path, {})
            raw_ids.append(raw_id)

        debug = capture.get("debug")
        if debug:
            path = write_raw_payload("capture_debug", key, debug)
            raw_id = self.storage.add_raw_payload("capture_debug", path, {})
            raw_ids.append(raw_id)

        return raw_ids


class CaptureAccessPolicy:
    def __init__(self, allowed_domains: List[str]) -> None:
        self.allowed_domains = [domain.lower() for domain in (allowed_domains or [])]

    def is_allowed_url(self, url: str) -> bool:
        if not self.allowed_domains:
            return True
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            return False
        host = host.lower()
        return any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains)
