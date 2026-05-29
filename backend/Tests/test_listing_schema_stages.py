import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.listing_schema import normalize_listing, validate_listing


class ListingSchemaStageTests(unittest.TestCase):
    def test_normalize_capture_stages_from_stage_text(self) -> None:
        listing = normalize_listing(
            {
                "id": "123",
                "source": "airbnb",
                "url": "https://www.airbnb.com/rooms/123",
                "capture_stage": "reviews_lite_ready",
            }
        )
        stages = listing.get("capture_stages") or {}
        self.assertTrue(stages.get("summary_ready"))
        self.assertTrue(stages.get("reviews_lite_ready"))
        self.assertFalse(stages.get("reviews_full_ready"))

    def test_normalize_capture_stages_hierarchy_from_object(self) -> None:
        listing = normalize_listing(
            {
                "id": "123",
                "source": "airbnb",
                "url": "https://www.airbnb.com/rooms/123",
                "capture_stages": {"reviews_full_ready": True},
            }
        )
        stages = listing.get("capture_stages") or {}
        self.assertTrue(stages.get("summary_ready"))
        self.assertTrue(stages.get("reviews_lite_ready"))
        self.assertTrue(stages.get("reviews_full_ready"))

    def test_validate_listing_warns_when_requested_reviews_not_captured(self) -> None:
        listing = normalize_listing(
            {
                "id": "123",
                "source": "airbnb",
                "url": "https://www.airbnb.com/rooms/123",
                "title": "Cabin",
                "review_mode": "lite",
                "reviews_total_count": 35,
                "reviews_captured_count": 0,
            }
        )
        validation = validate_listing(listing)
        self.assertIn("missing captured reviews", validation.get("warnings") or [])


if __name__ == "__main__":
    unittest.main()
