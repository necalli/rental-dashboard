import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.search_assist import SearchAssistService
from services.search_builder import build_airbnb_search_url


class _Storage:
    def __init__(self) -> None:
        self.jobs = []

    def create_job(self, job_type, payload):
        job = {"job_id": "job-1", "job_type": job_type, "status": "queued"}
        self.jobs.append((job_type, payload))
        return job


class SearchAssistTests(unittest.TestCase):
    def test_parse_and_queue_rich_search_prompt(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        result = service.assist(
            "Cabin near Phoenicia July 18-25, 2026 for 2 adults and 1 dog with wifi and parking under $500"
        )
        self.assertEqual(result.get("status"), "queued")
        self.assertEqual(len(storage.jobs), 1)
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("location"), "Phoenicia")
        self.assertEqual(payload.get("check_in"), "2026-07-18")
        self.assertEqual(payload.get("check_out"), "2026-07-25")
        self.assertEqual(payload.get("adults"), 2)
        self.assertEqual(payload.get("pets"), 1)
        self.assertEqual(payload.get("max_price"), 500)
        self.assertIn("wifi", payload.get("amenities") or [])
        self.assertIn("free parking", payload.get("amenities") or [])

    def test_invalid_prompt_does_not_queue(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        result = service.assist("Something quiet with a hot tub")
        self.assertEqual(result.get("status"), "clarification_needed")
        self.assertEqual(storage.jobs, [])

    def test_search_url_includes_extended_filters(self) -> None:
        url = build_airbnb_search_url(
            {
                "location": "Phoenicia",
                "min_price": 100,
                "max_price": 400,
                "room_type": "Entire home/apt",
                "amenities": ["hot tub", "wifi"],
                "min_bedrooms": 2,
                "min_beds": 3,
                "min_bathrooms": 2,
                "flexible_cancellation": True,
            }
        )
        self.assertIn("price_min=100", url)
        self.assertIn("price_max=400", url)
        self.assertIn("room_types%5B%5D=Entire+home%2Fapt", url)
        self.assertIn("amenities%5B%5D=hot+tub", url)
        self.assertIn("amenities%5B%5D=wifi", url)
        self.assertIn("min_bedrooms=2", url)
        self.assertIn("min_beds=3", url)
        self.assertIn("min_bathrooms=2", url)
        self.assertIn("flexible_cancellation=true", url)


if __name__ == "__main__":
    unittest.main()
