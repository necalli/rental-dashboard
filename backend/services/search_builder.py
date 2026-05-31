import os
from datetime import datetime
from typing import Any, Dict
from urllib.parse import quote, urlencode


BASE_URL = "https://www.airbnb.com"
AIRBNB_ROOM_TYPES = {"Entire home/apt", "Private room", "Shared room"}


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _location_path(location: str) -> str:
    parts = [part.strip() for part in str(location or "").split(",") if part.strip()]
    if len(parts) >= 2:
        return quote(f"{parts[0]}--{parts[1]}", safe="")
    return quote(str(location or "").strip(), safe="")


def _nights(check_in: Any, check_out: Any) -> int:
    try:
        start = datetime.strptime(str(check_in), "%Y-%m-%d")
        end = datetime.strptime(str(check_out), "%Y-%m-%d")
        return max(1, int((end - start).days))
    except Exception:
        return 1


def build_airbnb_search_url(params: Dict[str, Any]) -> str:
    location = (params.get("location") or "").strip()
    if not location:
        raise ValueError("location is required")

    selected_filters = []
    query: Dict[str, Any] = {
        "refinement_paths[]": ["/homes"],
        "query": location,
        "search_mode": "regular_search",
        "channel": "EXPLORE",
        "source": "structured_search_input_header",
    }
    if params.get("place_id"):
        query["place_id"] = params.get("place_id")
    if params.get("check_in"):
        query["checkin"] = params.get("check_in")
    if params.get("check_out"):
        query["checkout"] = params.get("check_out")
    if params.get("check_in") or params.get("check_out"):
        query["date_picker_type"] = "calendar"
    if params.get("adults"):
        query["adults"] = params.get("adults")
    if params.get("children"):
        query["children"] = params.get("children")
    if params.get("infants"):
        query["infants"] = params.get("infants")
    if params.get("pets"):
        query["pets"] = params.get("pets")
        selected_filters.append(f"pets:{params.get('pets')}")
    if params.get("min_price"):
        query["price_min"] = params.get("min_price")
        selected_filters.append(f"price_min:{params.get('min_price')}")
    if params.get("max_price"):
        query["price_max"] = params.get("max_price")
        query["price_filter_input_type"] = "2"
        query["price_filter_num_nights"] = _nights(params.get("check_in"), params.get("check_out"))
        selected_filters.append(f"price_max:{params.get('max_price')}")

    room_type = params.get("room_type")
    if room_type in AIRBNB_ROOM_TYPES:
        query["room_types[]"] = room_type

    if params.get("min_bedrooms"):
        query["min_bedrooms"] = params.get("min_bedrooms")
        selected_filters.append(f"min_bedrooms:{params.get('min_bedrooms')}")
    if params.get("min_beds"):
        query["min_beds"] = params.get("min_beds")
        selected_filters.append(f"min_beds:{params.get('min_beds')}")
    if params.get("min_bathrooms"):
        query["min_bathrooms"] = params.get("min_bathrooms")
        selected_filters.append(f"min_bathrooms:{params.get('min_bathrooms')}")

    amenities = params.get("amenities") or []
    if _truthy_env("RENTAL_SEARCH_ENABLE_TEXT_AMENITY_FILTERS") and isinstance(amenities, list) and amenities:
        query["amenities[]"] = amenities

    if params.get("flexible_cancellation"):
        query["flexible_cancellation"] = "true"
        selected_filters.append("flexible_cancellation:true")
    if selected_filters:
        query["selected_filter_order[]"] = selected_filters
        query["update_selected_filters"] = "true"

    query_string = urlencode(query, doseq=True)
    location_path = _location_path(location)
    return f"{BASE_URL}/s/{location_path}/homes?{query_string}" if query_string else f"{BASE_URL}/s/{location_path}/homes"
