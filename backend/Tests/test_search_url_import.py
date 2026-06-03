import os
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["RENTAL_AGENT_RUNTIME"] = "deterministic"

try:
    import app as app_module
except Exception:
    app_module = None


class _Storage:
    def __init__(self) -> None:
        self.created_jobs = []

    def create_job(self, job_type, payload):
        job = {"job_id": "job-1", "job_type": job_type, "payload": payload, "status": "queued"}
        self.created_jobs.append(job)
        return job


@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class SearchUrlImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app_module.app.test_client()
        self.fake_storage = _Storage()
        self.original_storage = app_module.app.config["storage"]
        app_module.app.config["storage"] = self.fake_storage

    def tearDown(self) -> None:
        app_module.app.config["storage"] = self.original_storage

    def test_import_search_url_queues_search_job(self) -> None:
        url = (
            "https://www.airbnb.com/s/Woodstock--NY/homes?"
            "adults=6&checkin=2026-07-19&checkout=2026-07-26&pets=1&"
            "price_max=2200&min_bedrooms=3&query=Woodstock%2C%20NY&place_id=abc&"
            "flexible_trip_lengths%5B%5D=one_week"
        )

        response = self.client.post("/api/v1/search/url", json={"search_url": url})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_storage.created_jobs), 1)
        job = self.fake_storage.created_jobs[0]
        self.assertEqual(job["job_type"], "search")
        payload = job["payload"]
        self.assertEqual(payload["search_url"], url)
        self.assertEqual(payload["search_import_source"], "airbnb_url")
        self.assertEqual(payload["location"], "Woodstock, NY")
        self.assertEqual(payload["check_in"], "2026-07-19")
        self.assertEqual(payload["check_out"], "2026-07-26")
        self.assertEqual(payload["adults"], "6")
        self.assertEqual(payload["pets"], "1")
        self.assertEqual(payload["max_price"], "2200")
        self.assertEqual(payload["min_bedrooms"], "3")
        self.assertTrue(payload["flexible_date_search"])
        self.assertEqual(payload["date_search_mode"], "flexible")
        self.assertEqual(
            payload["flexible_date_params"],
            {"flexible_trip_lengths[]": "one_week"},
        )

    def test_import_search_url_rejects_listing_url(self) -> None:
        response = self.client.post(
            "/api/v1/search/url",
            json={"search_url": "https://www.airbnb.com/rooms/123"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake_storage.created_jobs, [])

    def test_import_search_url_rejects_non_airbnb_url(self) -> None:
        response = self.client.post(
            "/api/v1/search/url",
            json={"search_url": "https://example.com/s/Woodstock/homes"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fake_storage.created_jobs, [])


if __name__ == "__main__":
    unittest.main()
