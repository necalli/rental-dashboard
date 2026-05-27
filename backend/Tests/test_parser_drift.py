import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.airbnb_parser_v1 import parse_capture
from services.airbnb_search_parser_v1 import (
    parse_search_from_responses,
    parse_search_from_responses_with_meta,
)


class ListingParserDriftTests(unittest.TestCase):
    def test_listing_parser_meta_flags_missing_review_extraction(self) -> None:
        capture = {
            "url": "https://www.airbnb.com/rooms/123",
            "html": "<html><body>no deferred state here</body></html>",
            "responses": [
                {
                    "url": "https://www.airbnb.com/api/v3/StaysPdpReviewsQuery?operationName=StaysPdpReviewsQuery",
                    "data": {"data": {"presentation": {"foo": "bar"}}},
                }
            ],
        }

        listing, reviews = parse_capture(capture, "123", capture["url"])
        self.assertEqual(reviews, [])
        parser_meta = listing.get("parser_meta") or {}
        self.assertEqual(parser_meta.get("parser_version"), "airbnb_listing_v1")
        self.assertTrue(parser_meta.get("drift_detected"))
        warnings = parser_meta.get("warnings") or []
        self.assertIn("missing_stay_product_detail_html_node", warnings)
        self.assertIn("review_responses_without_parsed_reviews", warnings)
        signature = (parser_meta.get("schema_signature") or {}).get("hash")
        self.assertTrue(isinstance(signature, str) and len(signature) >= 8)


class SearchParserDriftTests(unittest.TestCase):
    def test_search_parser_meta_and_backward_compatibility(self) -> None:
        responses = [
            {
                "url": "https://www.airbnb.com/api/v3/StaysSearch?operationName=StaysSearch&currency=USD",
                "data": {
                    "data": {
                        "staysSearch": {
                            "results": {
                                "searchResults": [
                                    {
                                        "listing": {
                                            "id": "123",
                                            "name": "Cabin stay",
                                            "pdpUrl": "/rooms/123",
                                            "localizedCity": "Keene",
                                            "lat": 44.2,
                                            "lng": -73.8,
                                        }
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ]

        listings, parser_meta = parse_search_from_responses_with_meta(
            responses,
            "https://www.airbnb.com/s/Keene/homes",
        )
        legacy = parse_search_from_responses(responses, "https://www.airbnb.com/s/Keene/homes")

        self.assertEqual(len(listings), 1)
        self.assertEqual(len(legacy), 1)
        self.assertEqual(listings[0].get("id"), "123")
        self.assertEqual(parser_meta.get("parser_version"), "airbnb_search_v1")
        self.assertFalse(parser_meta.get("drift_detected"))

    def test_search_parser_meta_warns_when_no_candidates(self) -> None:
        listings, parser_meta = parse_search_from_responses_with_meta(
            [{"url": "https://www.airbnb.com/api/v3/unknown", "data": {"data": {"foo": "bar"}}}],
            "https://www.airbnb.com/s/nowhere/homes",
        )
        self.assertEqual(listings, [])
        warnings = parser_meta.get("warnings") or []
        self.assertIn("no_candidates_found", warnings)


if __name__ == "__main__":
    unittest.main()
