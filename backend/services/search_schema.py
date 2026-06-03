import time
from typing import Any, Dict, List, Optional


def normalize_search_listing(listing: Dict[str, Any]) -> Dict[str, Any]:
    listing = dict(listing or {})

    listing_id = listing.get("id") or listing.get("listing_id")
    if listing_id:
        listing["id"] = str(listing_id)

    listing["source"] = _clean_text(listing.get("source"))
    listing["search_url"] = _clean_text(listing.get("search_url"))
    listing["url"] = _clean_text(listing.get("url"))
    listing["title"] = _clean_text(listing.get("title"))
    listing["property_type"] = _clean_text(listing.get("property_type"))
    listing["location"] = _clean_text(listing.get("location"))
    listing["lat"] = _to_float(listing.get("lat"))
    listing["lng"] = _to_float(listing.get("lng"))
    listing["rating"] = _to_float(listing.get("rating"))
    listing["review_count"] = _to_int(listing.get("review_count"))
    listing["price"] = _clean_text(listing.get("price"))
    listing["currency"] = _clean_text(listing.get("currency"))
    listing["pricing"] = _normalize_pricing(listing.get("pricing"))
    listing["image"] = _clean_text(listing.get("image"))
    listing["date_context"] = _normalize_date_context(listing.get("date_context"))
    listing["captured_at"] = listing.get("captured_at") or _now_iso()
    return listing


def validate_search_listing(listing: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    required_paths = [
        ("id", "missing listing id"),
        ("source", "missing source"),
        ("search_url", "missing search_url"),
    ]
    optional_paths = [
        ("url", "missing url"),
        ("title", "missing title"),
        ("property_type", "missing property_type"),
        ("location", "missing location"),
        ("lat", "missing lat"),
        ("lng", "missing lng"),
        ("price", "missing price"),
        ("currency", "missing currency"),
        ("image", "missing image"),
        ("rating", "missing rating"),
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


def _normalize_date_context(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    requested = _normalize_date_pair(value.get("requested_dates"))
    listing_dates = _normalize_date_pair(value.get("listing_dates"))
    output: Dict[str, Any] = {
        "date_search_mode": _clean_text(value.get("date_search_mode")),
        "date_match_type": _clean_text(value.get("date_match_type")),
    }
    if requested:
        output["requested_dates"] = requested
    if listing_dates:
        output["listing_dates"] = listing_dates
    return {key: item for key, item in output.items() if item not in (None, "", [], {})}


def _normalize_date_pair(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    output: Dict[str, str] = {}
    for source_key, target_key in (("check_in", "check_in"), ("check_out", "check_out")):
        text = _clean_text(value.get(source_key))
        if text:
            output[target_key] = text
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
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
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
