import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.preference_scoring import apply_preference_alignment, score_listing_preferences


class PreferenceScoringTests(unittest.TestCase):
    def test_scores_search_card_matches_and_unknown_detail_preferences(self) -> None:
        alignment = score_listing_preferences(
            {
                "title": "Home in Catskill",
                "property_type": "Home",
                "location": "Catskill",
            },
            [
                {"kind": "soft", "label": "home", "weight": 1.0},
                {"kind": "amenity", "label": "hot tub", "weight": 2.0},
            ],
        )

        self.assertEqual(alignment["matched"], ["home"])
        self.assertEqual(alignment["unknown"], ["hot tub"])
        self.assertEqual(alignment["missing"], [])
        self.assertEqual(alignment["matched_count"], 1)
        self.assertEqual(alignment["requested_count"], 2)
        self.assertAlmostEqual(alignment["score"], 0.333)

    def test_structured_amenities_can_mark_missing(self) -> None:
        alignment = score_listing_preferences(
            {
                "title": "Cabin in Catskill",
                "amenities": [{"group": "Bathroom", "items": ["Bath"]}],
            },
            [{"kind": "amenity", "label": "hot tub", "weight": 2.0}],
        )

        self.assertEqual(alignment["matched"], [])
        self.assertEqual(alignment["unknown"], [])
        self.assertEqual(alignment["missing"], ["hot tub"])

    def test_applies_alignment_and_ranks_matches_first(self) -> None:
        listings = [
            {"id": "1", "title": "Apartment in Catskill", "pricing": {"price_total_usd": 1000}},
            {"id": "2", "title": "Home in Catskill", "pricing": {"price_total_usd": 1200}},
            {"id": "3", "title": "Home with hot tub", "pricing": {"price_total_usd": 1500}},
        ]

        scored, summary = apply_preference_alignment(
            listings,
            {"amenities": ["hot tub"], "soft_preferences": ["home"]},
        )

        self.assertEqual([item["id"] for item in scored], ["3", "2", "1"])
        self.assertEqual(summary["requested"], ["hot tub", "home"])
        self.assertEqual(summary["scored_count"], 3)
        self.assertEqual(summary["matched_any_count"], 2)
        self.assertEqual(scored[0]["preference_alignment"]["matched"], ["hot tub", "home"])


if __name__ == "__main__":
    unittest.main()
