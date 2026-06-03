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

    def list_search_runs(self, limit=200):
        return [
            {
                "run_id": "run-1",
                "params": {
                    "location": "Catskill, NY",
                    "check_in": "2026-07-19",
                    "check_out": "2026-07-26",
                    "amenities": ["hot tub", "wifi"],
                    "soft_preferences": ["cottage"],
                },
            }
        ]

    def list_search_listings(self, run_id, limit=5000):
        return [
            {
                "id": "listing-1",
                "url": "https://www.airbnb.com/rooms/123",
                "date_context": {
                    "date_search_mode": "flexible",
                    "date_match_type": "flexible_alternate",
                    "requested_dates": {
                        "check_in": "2026-07-19",
                        "check_out": "2026-07-26",
                    },
                    "listing_dates": {
                        "check_in": "2026-07-26",
                        "check_out": "2026-07-30",
                    },
                },
            }
        ]

    def create_job(self, job_type, payload):
        job = {"job_id": "job-1", "job_type": job_type, "payload": payload, "status": "queued"}
        self.created_jobs.append(job)
        return job


@unittest.skipIf(app_module is None, "Flask app dependencies are unavailable")
class SearchIngestPreferenceTests(unittest.TestCase):
    def test_search_ingest_carries_preference_context(self) -> None:
        client = app_module.app.test_client()
        fake_storage = _Storage()
        original_storage = app_module.app.config["storage"]
        app_module.app.config["storage"] = fake_storage
        self.addCleanup(lambda: app_module.app.config.__setitem__("storage", original_storage))

        response = client.post(
            "/api/v1/search/ingest",
            json={
                "run_id": "run-1",
                "listing_ids": ["listing-1"],
                "review_mode": "lite",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake_storage.created_jobs), 1)
        payload = fake_storage.created_jobs[0]["payload"]
        self.assertEqual(
            payload.get("preference_context"),
            {
                "amenities": ["hot tub", "wifi"],
                "soft_preferences": ["cottage"],
            },
        )
        self.assertEqual(payload.get("check_in"), "2026-07-26")
        self.assertEqual(payload.get("check_out"), "2026-07-30")
        self.assertTrue((payload.get("search_date_context") or {}).get("used_listing_dates"))


if __name__ == "__main__":
    unittest.main()
