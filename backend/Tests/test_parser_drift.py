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


def _listing_html(stay_payload: dict, extra_payload: dict = None) -> str:
    payload = {
        "niobeClientData": [
            [
                "stay",
                {
                    "data": {
                        "presentation": {
                            "stayProductDetailPage": stay_payload,
                        }
                    }
                },
            ]
        ]
    }
    if extra_payload:
        payload["niobeClientData"].append(["extra", extra_payload])
    raw = json_dumps(payload)
    return f'<html><script id="data-deferred-state-0" type="application/json">{raw}</script></html>'


def json_dumps(value):
    import json

    return json.dumps(value, ensure_ascii=False)


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

    def test_listing_parser_extracts_amenities_from_deferred_fallback(self) -> None:
        stay_payload = {
            "sections": {
                "metadata": {
                    "sharingConfig": {
                        "title": "Cottage in Roxbury · 4 bedrooms",
                        "propertyType": "Entire cottage",
                        "location": "Roxbury",
                    }
                },
                "sections": [
                    {
                        "sectionId": "AMENITIES_DEFAULT",
                        "section": {"__typename": "AmenitiesSection"},
                    },
                    {
                        "sectionId": "TITLE_DEFAULT",
                        "section": {"title": "Cottage in Roxbury"},
                    },
                ],
            }
        }
        extra_payload = {
            "data": {
                "node": {
                    "pdpPresentation": {
                        "amenities": {
                            "previewAmenitiesGroups": [
                                {
                                    "title": None,
                                    "amenities": [
                                        {"title": "Kitchen", "available": True},
                                        {"title": "Wifi", "available": True},
                                    ],
                                }
                            ],
                            "seeAllAmenitiesGroups": [
                                {
                                    "title": "Bathroom",
                                    "amenities": [
                                        {"title": "Bath", "available": True},
                                        {"title": "Hairdryer", "available": True},
                                        {"title": "Unavailable item", "available": False},
                                    ],
                                },
                                {
                                    "title": "Internet and office",
                                    "amenities": [{"title": "Wifi", "available": True}],
                                },
                            ],
                        }
                    }
                }
            }
        }
        capture = {
            "url": "https://www.airbnb.com/rooms/123",
            "html": _listing_html(stay_payload, extra_payload),
            "responses": [],
        }

        listing, reviews = parse_capture(capture, "123", capture["url"])

        self.assertEqual(reviews, [])
        self.assertEqual(
            listing.get("amenities"),
            [
                {"group": "Bathroom", "items": ["Bath", "Hairdryer"]},
                {"group": "Internet and office", "items": ["Wifi"]},
            ],
        )
        parser_meta = listing.get("parser_meta") or {}
        self.assertTrue(
            (parser_meta.get("fallbacks") or {}).get("amenities_from_deferred_state_scan")
        )
        self.assertEqual((parser_meta.get("signals") or {}).get("parsed_amenity_group_count"), 2)


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

    def test_search_parser_handles_nested_current_stays_search_shape(self) -> None:
        responses = [
            {
                "url": "https://www.airbnb.com/api/v3/StaysSearch?operationName=StaysSearch&currency=USD",
                "data": {
                    "data": {
                        "presentation": {
                            "staysSearch": {
                                "results": {
                                    "searchResults": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "listingCard": {
                                                        "propertyId": "987654",
                                                        "title": "Cabin in Phoenicia",
                                                        "subtitle": "Entire cabin",
                                                        "structuredDisplayPrice": {
                                                            "primaryLine": {
                                                                "price": "$1,400",
                                                                "accessibilityLabel": "$1,400 total for 7 nights",
                                                            },
                                                            "displayPriceStyle": "TOTAL",
                                                        },
                                                        "avgRatingLocalized": "4.96",
                                                        "structuredContent": {
                                                            "primaryLine": "Phoenicia",
                                                            "reviewSnippet": "128 reviews",
                                                        },
                                                        "location": {"lat": 42.08, "lng": -74.31},
                                                        "pictures": [{"url": "https://example.test/photo.jpg"}],
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                },
                                "mapResults": {
                                    "mapSearchResults": [
                                        {
                                            "propertyId": "987654",
                                            "title": "Cabin in Phoenicia",
                                            "location": {"lat": 42.08, "lng": -74.31},
                                        }
                                    ]
                                },
                            }
                        }
                    }
                },
            }
        ]

        listings, parser_meta = parse_search_from_responses_with_meta(
            responses,
            "https://www.airbnb.com/s/Phoenicia--NY/homes",
        )

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].get("id"), "987654")
        self.assertEqual(listings[0].get("title"), "Cabin in Phoenicia")
        self.assertEqual(listings[0].get("url"), "https://www.airbnb.com/rooms/987654")
        self.assertEqual(listings[0].get("rating"), 4.96)
        self.assertEqual(listings[0].get("review_count"), 128)
        self.assertEqual((listings[0].get("pricing") or {}).get("price_total"), 1400.0)
        self.assertFalse(parser_meta.get("drift_detected"))


if __name__ == "__main__":
    unittest.main()
