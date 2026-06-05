import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.comparison_memory import build_comparison_memory_query, compact_memory_context
from services.llm_enrichment import build_comparison_input, build_comparison_request


class ComparisonMemoryTests(unittest.TestCase):
    def test_memory_context_is_omitted_when_disabled(self) -> None:
        listings = [{"id": "a", "title": "Cabin A"}]
        payload = build_comparison_input(listings, {"a": []}, memory_context=None)
        self.assertIn("listings", payload)
        self.assertNotIn("trip_memory_context", payload)

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
