import time
from typing import Any, Dict, List, Optional, Tuple


def normalize_listing(listing: Dict[str, Any]) -> Dict[str, Any]:
    listing = dict(listing or {})

    listing_id = listing.get("id") or listing.get("listing_id")
    if listing_id:
        listing["id"] = str(listing_id)

    listing["source"] = (listing.get("source") or "").strip() or None
    listing["url"] = (listing.get("url") or "").strip() or None
    listing["title"] = _clean_text(listing.get("title"))
    listing["property_type"] = _clean_text(listing.get("property_type"))

    listing["description"] = _clean_text(listing.get("description"))
    listing["description_snippet"] = _clean_text(listing.get("description_snippet"))

    listing["capacity"] = _normalize_capacity(listing.get("capacity"))
    listing["location"] = _normalize_location(listing.get("location"))
    listing["host"] = _normalize_host(listing.get("host"))
    listing["reviews_summary"] = _normalize_reviews_summary(listing.get("reviews_summary"))
    listing["pricing"] = _normalize_pricing(listing.get("pricing"))
    listing["availability"] = _normalize_availability(listing.get("availability"))
    listing["review_mode"] = _clean_text(listing.get("review_mode"))
    listing["reviews_captured_count"] = _to_int(listing.get("reviews_captured_count"))
    listing["reviews_total_count"] = _to_int(listing.get("reviews_total_count"))
    listing["review_coverage"] = _to_float(listing.get("review_coverage"))
    listing["capture_stage"] = _clean_text(listing.get("capture_stage"))
    listing["capture_stages"] = _normalize_capture_stages(
        listing.get("capture_stages"),
        stage=listing.get("capture_stage"),
    )

    listing["amenities"] = _normalize_amenities(listing.get("amenities"))
    listing["sleeping_arrangements"] = _normalize_sleeping(listing.get("sleeping_arrangements"))
    listing["photos"] = _normalize_photos(listing.get("photos"))
    listing["house_rules"] = _normalize_string_list(listing.get("house_rules"))
    listing["safety_notes"] = _normalize_string_list(listing.get("safety_notes"))
    listing["cancellation_policy"] = _clean_text(listing.get("cancellation_policy"))

    listing["captured_at"] = listing.get("captured_at") or _now_iso()
    return listing


def validate_listing(listing: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    required_paths = [
        ("id", "missing listing id"),
        ("source", "missing source"),
        ("url", "missing url"),
    ]
    optional_paths = [
        ("title", "missing title"),
        ("property_type", "missing property_type"),
        ("location.name", "missing location name"),
        ("location.details.lat", "missing location lat"),
        ("location.details.lng", "missing location lng"),
        ("description", "missing description"),
        ("amenities", "missing amenities"),
        ("photos", "missing photos"),
        ("host.name", "missing host name"),
        ("reviews_summary.count", "missing reviews count"),
        ("reviews_summary.overall_rating", "missing overall rating"),
    ]

    for path, message in required_paths:
        if _get_path(listing, path) in (None, "", []):
            errors.append(message)

    for path, message in optional_paths:
        if _get_path(listing, path) in (None, "", []):
            warnings.append(message)

    quality_score = _quality_score(listing, [p for p, _ in optional_paths])

    return {
        "errors": errors,
        "warnings": warnings,
        "quality_score": quality_score,
    }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_capacity(value: Any) -> Dict[str, Optional[int]]:
    value = value or {}
    return {
        "guests": _to_int(value.get("guests")),
        "bedrooms": _to_int(value.get("bedrooms")),
        "beds": _to_int(value.get("beds")),
        "baths": _to_int(value.get("baths")),
    }


def _normalize_location(value: Any) -> Dict[str, Any]:
    value = value or {}
    details = value.get("details") or {}
    return {
        "name": _clean_text(value.get("name")),
        "details": {
            "title": _clean_text(details.get("title")),
            "subtitle": _clean_text(details.get("subtitle")),
            "address": _clean_text(details.get("address")),
            "address_title": _clean_text(details.get("address_title")),
            "lat": _to_float(details.get("lat")),
            "lng": _to_float(details.get("lng")),
            "summary": _normalize_string_list(details.get("summary")),
        },
    }


def _normalize_host(value: Any) -> Dict[str, Any]:
    value = value or {}
    return {
        "id": _clean_text(value.get("id")),
        "name": _clean_text(value.get("name")),
        "superhost": _to_bool(value.get("superhost")),
        "rating": _to_float(value.get("rating")),
        "review_count": _to_int(value.get("review_count")),
        "response_details": _normalize_string_list(value.get("response_details")),
    }


def _normalize_reviews_summary(value: Any) -> Dict[str, Any]:
    value = value or {}
    return {
        "overall_rating": _to_float(value.get("overall_rating")),
        "count": _to_int(value.get("count")),
        "category_ratings": value.get("category_ratings") or [],
        "distribution": value.get("distribution") or [],
    }


def _normalize_capture_stages(value: Any, *, stage: Any = None) -> Dict[str, bool]:
    stages = {
        "summary_ready": False,
        "reviews_lite_ready": False,
        "reviews_full_ready": False,
    }
    if isinstance(value, dict):
        for key in stages.keys():
            if key in value:
                stages[key] = bool(_to_bool(value.get(key)))

    stage_text = _clean_text(stage)
    if stage_text in stages:
        stages[stage_text] = True

    if stages["reviews_full_ready"]:
        stages["reviews_lite_ready"] = True
        stages["summary_ready"] = True
    elif stages["reviews_lite_ready"]:
        stages["summary_ready"] = True
    return stages


def _normalize_pricing(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "price_display": _clean_text(value.get("price_display")),
        "currency": _clean_text(value.get("currency")),
        "price_type": _clean_text(value.get("price_type")),
        "nights": _to_int(value.get("nights")),
        "price_total": _to_float(value.get("price_total")),
        "price_nightly": _to_float(value.get("price_nightly")),
        "price_total_usd": _to_float(value.get("price_total_usd")),
        "price_nightly_usd": _to_float(value.get("price_nightly_usd")),
        "fx_rate": _to_float(value.get("fx_rate")),
        "fx_timestamp": _clean_text(value.get("fx_timestamp")),
        "fx_source": _clean_text(value.get("fx_source")),
        "fx_stale": _to_bool(value.get("fx_stale")),
        "source": _clean_text(value.get("source")),
    }


def _normalize_availability(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"check_in": None, "check_out": None, "nights": None}
    return {
        "check_in": _clean_text(value.get("check_in")),
        "check_out": _clean_text(value.get("check_out")),
        "nights": _to_int(value.get("nights")),
    }


def _normalize_amenities(value: Any) -> List[Dict[str, Any]]:
    groups = value or []
    output: List[Dict[str, Any]] = []
    if not isinstance(groups, list):
        return output
    for group in groups:
        if not isinstance(group, dict):
            continue
        title = _clean_text(group.get("group") or group.get("title"))
        items = _normalize_string_list(group.get("items") or group.get("amenities"))
        if title or items:
            output.append({"group": title, "items": items})
    return output


def _normalize_sleeping(value: Any) -> List[Dict[str, Any]]:
    arrangements = value or []
    output: List[Dict[str, Any]] = []
    if not isinstance(arrangements, list):
        return output
    for entry in arrangements:
        if not isinstance(entry, dict):
            continue
        output.append(
            {
                "title": _clean_text(entry.get("title")),
                "subtitle": _clean_text(entry.get("subtitle")),
                "beds": _normalize_string_list(entry.get("beds")),
            }
        )
    return output


def _normalize_photos(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    photos: List[str] = []
    for item in value:
        text = _clean_text(item)
        if text:
            photos.append(text)
    return photos


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    output: List[str] = []
    for item in items:
        text = _clean_text(item)
        if text:
            output.append(text)
    return output


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _get_path(data: Dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _quality_score(listing: Dict[str, Any], optional_paths: List[str]) -> float:
    if not optional_paths:
        return 1.0
    present = 0
    for path in optional_paths:
        value = _get_path(listing, path)
        if value not in (None, "", []):
            present += 1
    return round(present / len(optional_paths), 2)
