import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.comparison_memory import build_comparison_memory_query, compact_memory_context
from services.llm_enrichment import build_comparison_input, build_comparison_request, build_photo_fit_input


class ComparisonMemoryTests(unittest.TestCase):
    def test_memory_context_is_omitted_when_disabled(self) -> None:
        listings = [{"id": "a", "title": "Cabin A"}]
        payload = build_comparison_input(listings, {"a": []}, memory_context=None)
        self.assertIn("listings", payload)
        self.assertNotIn("trip_memory_context", payload)

    def test_photo_fit_input_uses_representative_photos(self) -> None:
        listing = {
            "id": "a",
            "title": "Cabin A",
            "photos": [
                {"url": "https://example.test/bedroom-2.jpg", "room_or_area": "bedroom"},
                {"url": "https://example.test/unlabeled.jpg"},
            ],
            "representative_photos": {
                "kitchen": {"url": "https://example.test/kitchen.jpg", "caption": "Open kitchen"},
                "bedroom": {"url": "https://example.test/bedroom.jpg", "caption": "Queen bedroom"},
            },
        }

        payload = build_photo_fit_input(listing, max_images=1)

        self.assertEqual(payload["photo_count"], 2)
        self.assertEqual(payload["area_counts"], {"bedroom": 1, "Unlabeled": 1})
        self.assertEqual(
            payload["selected_photos"],
            [
                {
                    "area": "kitchen",
                    "url": "https://example.test/kitchen.jpg",
                    "caption": "Open kitchen",
                    "position": None,
                }
            ],
        )

    def test_comparison_input_can_include_cached_photo_fit(self) -> None:
        listings = [{"id": "a", "title": "Cabin A"}]
        payload = build_comparison_input(
            listings,
            {"a": []},
            photo_fit_by_listing={
                "a": {
                    "visual_summary": "Bright shared spaces.",
                    "visual_strengths": ["Large dining area"],
                    "visual_concerns": [],
                    "photo_confidence": "medium",
                    "analyzed_photo_count": 4,
                }
            },
        )

        self.assertEqual(
            payload["listings"][0]["photo_fit"]["visual_summary"],
            "Bright shared spaces.",
        )

    def test_enabled_memory_context_changes_comparison_hash(self) -> None:
        listings = [{"id": "a", "title": "Cabin A"}, {"id": "b", "title": "Cabin B"}]
        reviews = {"a": [], "b": []}
        _, base_hash = build_comparison_request(listings, reviews)
        _, memory_hash = build_comparison_request(
            listings,
            reviews,
            memory_context={
                "enabled": True,
                "hits": [{"citation_index": 1, "text": "Past trips favored quiet cabins."}],
                "citations": [{"citation_index": 1, "title": "Catskills notes"}],
            },
        )
        self.assertNotEqual(base_hash, memory_hash)

    def test_compact_memory_context_assigns_citation_indexes(self) -> None:
        context = compact_memory_context(
            {
                "user_id": "rental-dashboard",
                "query": "quiet cabin",
                "hits": [
                    {
                        "score": 0.87,
                        "text": "We liked quiet wooded places near hiking trails.",
                        "citation": {
                            "memory_id": "m1",
                            "title": "Trip notes",
                            "filename": "notes.txt",
                            "source_type": "upload",
                        },
                    }
                ],
            }
        )
        self.assertTrue(context["enabled"])
        self.assertEqual(context["hits"][0]["citation_index"], 1)
        self.assertEqual(context["citations"][0]["title"], "Trip notes")

    def test_memory_query_includes_listing_features_and_focus(self) -> None:
        query = build_comparison_memory_query(
            [
                {
                    "title": "Cabin in Phoenicia",
                    "property_type": "Cabin",
                    "location": "Phoenicia",
                    "description": "Quiet stay near hiking.",
                    "amenities": [{"group": "Outdoor", "items": ["Hot tub"]}],
                }
            ],
            focus="prioritize quiet trails",
        )
        self.assertIn("Personalization focus", query)
        self.assertIn("Cabin in Phoenicia", query)
        self.assertIn("Outdoor: Hot tub", query)


if __name__ == "__main__":
    unittest.main()
