from typing import Any, Dict
from urllib.parse import urlencode


BASE_URL = "https://www.airbnb.com"


def build_airbnb_search_url(params: Dict[str, Any]) -> str:
    location = (params.get("location") or "").strip()
    if not location:
        raise ValueError("location is required")

    query: Dict[str, Any] = {}
    if params.get("place_id"):
        query["place_id"] = params.get("place_id")
    if params.get("check_in"):
        query["checkin"] = params.get("check_in")
    if params.get("check_out"):
        query["checkout"] = params.get("check_out")
    if params.get("adults"):
        query["adults"] = params.get("adults")
    if params.get("children"):
        query["children"] = params.get("children")
    if params.get("infants"):
        query["infants"] = params.get("infants")
    if params.get("pets"):
        query["pets"] = params.get("pets")
    if params.get("min_price"):
        query["price_min"] = params.get("min_price")
    if params.get("max_price"):
        query["price_max"] = params.get("max_price")
    if params.get("room_type"):
        query["room_types[]"] = params.get("room_type")
    if params.get("min_bedrooms"):
        query["min_bedrooms"] = params.get("min_bedrooms")
    if params.get("min_beds"):
        query["min_beds"] = params.get("min_beds")
    if params.get("min_bathrooms"):
        query["min_bathrooms"] = params.get("min_bathrooms")
    amenities = params.get("amenities") or []
    if isinstance(amenities, list) and amenities:
        query["amenities[]"] = amenities
    if params.get("flexible_cancellation"):
        query["flexible_cancellation"] = "true"

    query_string = urlencode(query, doseq=True)
    return f"{BASE_URL}/s/{location}/homes?{query_string}" if query_string else f"{BASE_URL}/s/{location}/homes"
