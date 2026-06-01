import sys
import unittest
from pathlib import Path
from unittest.mock import patch


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
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist(
                "Cabin near Phoenicia July 18-25, 2026 for 2 adults and 1 dog with wifi and parking under $500 per night"
            )
        self.assertEqual(result.get("status"), "queued")
        self.assertEqual(len(storage.jobs), 1)
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("location"), "Phoenicia")
        self.assertEqual(payload.get("check_in"), "2026-07-18")
        self.assertEqual(payload.get("check_out"), "2026-07-25")
        self.assertEqual(payload.get("adults"), 2)
        self.assertEqual(payload.get("pets"), 1)
        self.assertEqual(payload.get("max_price_nightly"), 500)
        self.assertEqual(payload.get("max_price"), 3500)
        self.assertEqual((payload.get("price_filter") or {}).get("basis"), "nightly")
        self.assertIn("wifi", payload.get("amenities") or [])
        self.assertIn("free parking", payload.get("amenities") or [])

    def test_unqualified_price_requires_clarification(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist("Home in Keene NY for 6 people, pet friendly. Max price is $2500")
        self.assertEqual(result.get("status"), "clarification_needed")
        self.assertEqual(storage.jobs, [])
        self.assertIn("per night or total", result.get("message") or "")

    def test_unknown_price_basis_without_price_does_not_block_search(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist(
                "Home in Keene NY for 6 people, pet friendly",
                parsed_intent={
                    "location": "Keene NY",
                    "adults": 6,
                    "pets": 1,
                    "price_basis": "unknown",
                },
                parsed_status="ready",
            )
        self.assertEqual(result.get("status"), "queued")
        self.assertEqual(len(storage.jobs), 1)
        self.assertNotIn("Price basis", " ".join(result.get("unsupported_or_uncertain_requests") or []))

    def test_home_phrase_is_soft_preference_not_room_type(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist(
                "Home in Keene NY for 6 people, pet friendly, from 7-20-26 to 7-27-26",
                parsed_intent={
                    "location": "Keene, NY",
                    "check_in": "2026-07-20",
                    "check_out": "2026-07-27",
                    "adults": 6,
                    "pets": 1,
                    "room_type": "Entire home/apt",
                },
                parsed_status="ready",
                parsed_soft_preferences=["home"],
            )
        self.assertEqual(result.get("status"), "queued")
        _, payload = storage.jobs[0]
        self.assertNotIn("room_type", payload)
        self.assertEqual(payload.get("soft_preferences"), ["home"])

    def test_explicit_entire_home_keeps_room_type(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist(
                "Entire home in Keene NY for 6 people, pet friendly, from 7-20-26 to 7-27-26",
                parsed_intent={
                    "location": "Keene, NY",
                    "check_in": "2026-07-20",
                    "check_out": "2026-07-27",
                    "adults": 6,
                    "pets": 1,
                    "room_type": "Entire home/apt",
                },
                parsed_status="ready",
            )
        self.assertEqual(result.get("status"), "queued")
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("room_type"), "Entire home/apt")

    def test_invalid_prompt_does_not_queue(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist("Something quiet with a hot tub")
        self.assertEqual(result.get("status"), "clarification_needed")
        self.assertEqual(storage.jobs, [])

    def test_destination_first_prompt_parses_location(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist("Phoenicia July 18-25, 2026, 4 adults, dog friendly")
        self.assertEqual(result.get("status"), "queued")
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("location"), "Phoenicia")
        self.assertEqual(payload.get("check_in"), "2026-07-18")
        self.assertEqual(payload.get("check_out"), "2026-07-25")
        self.assertEqual(payload.get("adults"), 4)
        self.assertEqual(payload.get("pets"), 1)

    def test_geoapify_auto_selects_unambiguous_location(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        suggestions = [
            {
                "label": "Phoenicia, NY, United States",
                "city": "Phoenicia",
                "state": "New York",
                "country": "United States",
                "lat": 42.083,
                "lng": -74.315,
                "type": "city",
            }
        ]
        with patch("services.search_assist.suggest_locations", return_value=suggestions):
            result = service.assist("Phoenicia July 18-25, 2026")
        self.assertEqual(result.get("status"), "queued")
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("location"), "Phoenicia, New York")
        self.assertTrue((payload.get("location_resolution") or {}).get("auto_selected"))

    def test_geoapify_ambiguous_location_requests_confirmation(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        suggestions = [
            {"label": "Springfield, IL, United States", "city": "Springfield", "state": "Illinois"},
            {"label": "Springfield, MA, United States", "city": "Springfield", "state": "Massachusetts"},
        ]
        with patch("services.search_assist.suggest_locations", return_value=suggestions):
            result = service.assist("Springfield July 18-25, 2026")
        self.assertEqual(result.get("status"), "clarification_needed")
        self.assertEqual(storage.jobs, [])
        self.assertEqual(len(result.get("location_candidates") or []), 2)

    def test_location_override_queues_confirmed_candidate(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        suggestions = [
            {"label": "Springfield, IL, United States", "city": "Springfield", "state": "Illinois"},
            {"label": "Springfield, MA, United States", "city": "Springfield", "state": "Massachusetts"},
        ]
        with patch("services.search_assist.suggest_locations", return_value=suggestions):
            result = service.assist(
                "Springfield July 18-25, 2026",
                location_override="Springfield, MA, United States",
            )
        self.assertEqual(result.get("status"), "queued")
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("location"), "Springfield, MA, United States")

    def test_external_model_soft_preferences_are_preserved(self) -> None:
        storage = _Storage()
        service = SearchAssistService(storage)
        with patch("services.search_assist.suggest_locations", return_value=[]):
            result = service.assist(
                "Find a secluded cabin near Phoenicia July 18-25 under $400 per night",
                parsed_intent={
                    "location": "Phoenicia",
                    "check_in": "2026-07-18",
                    "check_out": "2026-07-25",
                    "adults": 2,
                    "max_price_nightly": 400,
                    "price_basis": "nightly",
                },
                parsed_status="ready",
                parsed_soft_preferences=["secluded", "cabin"],
                parsed_confidence=0.9,
            )
        self.assertEqual(result.get("status"), "queued")
        self.assertEqual(result.get("soft_preferences"), ["secluded", "cabin"])
        _, payload = storage.jobs[0]
        self.assertEqual(payload.get("soft_preferences"), ["secluded", "cabin"])
        self.assertEqual(payload.get("max_price"), 2800)
        self.assertEqual((payload.get("price_filter") or {}).get("input_max_nightly"), 400)

    def test_search_url_includes_safe_extended_filters(self) -> None:
        url = build_airbnb_search_url(
            {
                "location": "Phoenicia, NY",
                "min_price": 100,
                "max_price": 400,
                "room_type": "cabin",
                "amenities": ["hot tub", "wifi"],
                "min_bedrooms": 2,
                "min_beds": 3,
                "min_bathrooms": 2,
                "flexible_cancellation": True,
            }
        )
        self.assertIn("/s/Phoenicia--NY/homes", url)
        self.assertIn("refinement_paths%5B%5D=%2Fhomes", url)
        self.assertIn("query=Phoenicia%2C+NY", url)
        self.assertIn("search_mode=regular_search", url)
        self.assertIn("price_min=100", url)
        self.assertIn("price_max=400", url)
        self.assertIn("selected_filter_order%5B%5D=price_max%3A400", url)
        self.assertNotIn("room_types", url)
        self.assertNotIn("amenities", url)
        self.assertIn("min_bedrooms=2", url)
        self.assertIn("min_beds=3", url)
        self.assertIn("min_bathrooms=2", url)
        self.assertIn("flexible_cancellation=true", url)

    def test_search_url_converts_nightly_price_to_total_cap(self) -> None:
        url = build_airbnb_search_url(
            {
                "location": "Phoenicia, NY",
                "check_in": "2026-07-18",
                "check_out": "2026-07-25",
                "max_price_nightly": 400,
            }
        )
        self.assertIn("price_max=2800", url)
        self.assertIn("price_filter_num_nights=7", url)
        self.assertIn("selected_filter_order%5B%5D=price_max%3A2800", url)

    @patch.dict("os.environ", {"RENTAL_SEARCH_ENABLE_TEXT_AMENITY_FILTERS": "true"})
    def test_search_url_can_opt_into_text_amenity_filters(self) -> None:
        url = build_airbnb_search_url(
            {
                "location": "Phoenicia",
                "room_type": "Entire home/apt",
                "amenities": ["hot tub", "wifi"],
            }
        )
        self.assertIn("room_types%5B%5D=Entire+home%2Fapt", url)
        self.assertIn("amenities%5B%5D=hot+tub", url)
        self.assertIn("amenities%5B%5D=wifi", url)


if __name__ == "__main__":
    unittest.main()
