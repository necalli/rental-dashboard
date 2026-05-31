import base64
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .parser_drift import build_parser_meta, build_response_signature

SEARCH_PARSER_VERSION = "airbnb_search_v1"


def parse_search_from_responses(
    responses: List[Dict[str, Any]],
    search_url: str,
) -> List[Dict[str, Any]]:
    listings, _ = parse_search_from_responses_with_meta(responses, search_url)
    return listings


def parse_search_from_responses_with_meta(
    responses: List[Dict[str, Any]],
    search_url: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    currency_hint = None
    generic_candidate_count = 0
    stays_search_candidate_count = 0
    for response in responses:
        if not currency_hint:
            currency_hint = _extract_currency_from_response_url(response.get("url"))
        data = response.get("data")
        generic = _collect_candidates(data, depth=14)
        from_stays_search = _collect_stays_search_candidates(data)
        generic_candidate_count += len(generic)
        stays_search_candidate_count += len(from_stays_search)
        candidates.extend(generic)
        candidates.extend(from_stays_search)

    listings: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        summary = _normalize_candidate(item, search_url, currency_hint)
        if not summary:
            continue
        listing_id = summary.get("id")
        if listing_id:
            existing = listings.get(listing_id)
            if existing:
                listings[listing_id] = _merge_listing(existing, summary)
            else:
                listings[listing_id] = summary
    normalized = list(listings.values())

    warnings: List[str] = []
    if responses and not candidates:
        warnings.append("no_candidates_found")
    if candidates and not normalized:
        warnings.append("no_listings_normalized")
    if generic_candidate_count == 0 and stays_search_candidate_count == 0 and responses:
        warnings.append("search_candidate_paths_missing")

    parser_meta = build_parser_meta(
        parser_version=SEARCH_PARSER_VERSION,
        signature=build_response_signature(responses, mode="search"),
        warnings=warnings,
        fallbacks={
            "used_generic_candidates": bool(generic_candidate_count > 0),
            "used_stays_search_candidates": bool(stays_search_candidate_count > 0),
        },
        signals={
            "response_count": len(responses or []),
            "candidate_count": len(candidates),
            "generic_candidate_count": generic_candidate_count,
            "stays_search_candidate_count": stays_search_candidate_count,
            "normalized_listing_count": len(normalized),
        },
    )
    return normalized, parser_meta


def _collect_candidates(node: Any, depth: int) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    if depth <= 0:
        return output
    if isinstance(node, dict):
        for wrapper_key in ("node", "item", "result", "listingCard"):
            wrapped = node.get(wrapper_key)
            if isinstance(wrapped, dict):
                output.extend(_collect_candidates(wrapped, depth - 1))
        if isinstance(node.get("listing"), dict):
            output.append(node)
        elif isinstance(node.get("stay"), dict):
            output.append(node)
        elif isinstance(node.get("stayListing"), dict):
            output.append(node)
        elif isinstance(node.get("demandStayListing"), dict):
            output.append(node)
        elif "listingId" in node or "stayListingId" in node:
            output.append(node)
        elif "propertyId" in node and (
            "structuredDisplayPrice" in node
            or "pricingQuote" in node
            or "title" in node
            or "nameLocalized" in node
        ):
            output.append(node)
        for value in node.values():
            output.extend(_collect_candidates(value, depth - 1))
    elif isinstance(node, list):
        for item in node[:500]:
            output.extend(_collect_candidates(item, depth - 1))
    return output


def _normalize_candidate(
    item: Dict[str, Any],
    search_url: str,
    currency_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    listing = None
    if isinstance(item.get("node"), dict):
        item = item.get("node") or item
    elif isinstance(item.get("item"), dict):
        item = item.get("item") or item
    elif isinstance(item.get("result"), dict):
        item = item.get("result") or item
    elif isinstance(item.get("listingCard"), dict):
        item = item.get("listingCard") or item
    for key in ("listing", "stayListing", "stay"):
        if isinstance(item.get(key), dict):
            listing = item.get(key)
            break
    search_listing = _extract_search_result_listing(item)
    listing = listing or search_listing or item

    listing_id = (
        listing.get("id")
        or listing.get("listingId")
        or listing.get("stayListingId")
        or item.get("listingId")
        or item.get("stayListingId")
        or item.get("roomId")
        or item.get("propertyId")
    )
    if not listing_id and search_listing:
        listing_id = search_listing.get("id")
    if not listing_id:
        listing_id = _extract_search_result_listing_id(item)
    listing_id = _normalize_listing_id(listing_id)
    if listing_id is None:
        return None
    listing_id = str(listing_id)

    title = (
        listing.get("name")
        or listing.get("title")
        or listing.get("publicTitle")
        or item.get("name")
        or item.get("title")
        or _extract_search_result_title(item)
    )

    property_type = (
        listing.get("roomType")
        or listing.get("propertyType")
        or listing.get("roomTypeCategory")
        or _extract_search_result_property_type(item, title)
    )

    location = (
        listing.get("localizedCity")
        or listing.get("localizedCityName")
        or listing.get("city")
        or _safe_location_value(listing.get("location"))
        or _extract_search_result_location(item, title)
    )

    lat, lng = _extract_lat_lng(listing)
    rating = (
        listing.get("avgRating")
        or listing.get("starRating")
        or listing.get("reviewScore")
        or item.get("avgRating")
        or _extract_search_result_rating(item)
    )
    review_count = (
        listing.get("reviewsCount")
        or listing.get("reviewCount")
        or item.get("reviewCount")
        or _extract_search_result_review_count(item)
    )

    pricing = _extract_search_pricing(item, currency_hint)
    price = _extract_price(item) or _extract_price(listing) or _extract_search_result_price(item)
    currency = (
        _extract_currency(item)
        or _extract_currency(listing)
        or _extract_search_result_currency(item, price)
        or currency_hint
    )
    if currency_hint and isinstance(pricing, dict) and not pricing.get("currency"):
        pricing["currency"] = currency_hint
    image = _extract_photo(listing) or _extract_photo(item)
    url = (
        listing.get("pdpUrl")
        or listing.get("listingUrl")
        or listing.get("url")
        or item.get("url")
    )
    if url and url.startswith("/"):
        url = f"https://www.airbnb.com{url}"
    if not url:
        url = _extract_search_result_url(item)

    return {
        "id": listing_id,
        "source": "airbnb",
        "search_url": search_url,
        "url": url,
        "title": _clean_text(title),
        "property_type": _clean_text(property_type),
        "location": _clean_text(location),
        "lat": lat,
        "lng": lng,
        "rating": _to_float(rating),
        "review_count": _to_int(review_count),
        "price": _clean_text(price),
        "currency": _clean_text(currency),
        "pricing": pricing,
        "image": _clean_text(image),
        "captured_at": _now_iso(),
    }


def _extract_lat_lng(listing: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    lat = listing.get("lat") or listing.get("listingLat")
    lng = listing.get("lng") or listing.get("listingLng")
    coord = listing.get("coordinate") or listing.get("coordinates")
    if isinstance(coord, dict):
        lat = lat or coord.get("latitude") or coord.get("lat")
        lng = lng or coord.get("longitude") or coord.get("lng")
    location = listing.get("location")
    if isinstance(location, dict):
        lat = lat or location.get("lat") or location.get("latitude")
        lng = lng or location.get("lng") or location.get("longitude")
        coord = location.get("coordinate")
        if isinstance(coord, dict):
            lat = lat or coord.get("latitude") or coord.get("lat")
            lng = lng or coord.get("longitude") or coord.get("lng")
    return _to_float(lat), _to_float(lng)


def _extract_price(item: Dict[str, Any]) -> Optional[str]:
    pricing = item.get("pricingQuote") or {}
    if isinstance(pricing, dict):
        display = pricing.get("structuredStayDisplayPrice") or {}
        if isinstance(display, dict):
            primary = display.get("primaryLine") or {}
            price = primary.get("price") or primary.get("accessibilityLabel")
            if price:
                return price
        if isinstance(pricing.get("displayPrice"), dict):
            price = pricing.get("displayPrice", {}).get("price")
            if price:
                return price
        price = pricing.get("price") or pricing.get("rate") or pricing.get("priceString")
        if price:
            return price
    return item.get("price") or item.get("priceString")


def _extract_currency(item: Dict[str, Any]) -> Optional[str]:
    pricing = item.get("pricingQuote") or {}
    if isinstance(pricing, dict):
        currency = pricing.get("currency") or pricing.get("ratePlanCurrency")
        if currency:
            return currency
    return item.get("currency")


def _extract_photo(listing: Dict[str, Any]) -> Optional[str]:
    for key in ("contextualPictures", "pictures", "photos", "image", "pictureUrls"):
        pics = listing.get(key)
        if isinstance(pics, list) and pics:
            first = pics[0]
            if isinstance(first, dict):
                return first.get("picture") or first.get("url") or first.get("thumbnailUrl")
            if isinstance(first, str):
                return first
    return None


def _collect_stays_search_candidates(data: Any) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    if not isinstance(data, dict):
        return output
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(root, dict):
        return output
    stays = root.get("staysSearch") or root.get("staysSearchResults")
    presentation = root.get("presentation")
    if not stays and isinstance(presentation, dict):
        stays = presentation.get("staysSearch")
    if isinstance(stays, dict):
        output.extend(_collect_result_items(stays, depth=8))

    for stays_node in _collect_named_values(
        root,
        {"staysSearch", "staysSearchResults"},
        depth=12,
    ):
        output.extend(_collect_result_items(stays_node, depth=10))

    for key in ("searchResults", "searchResultsV2", "mapResults", "results"):
        items = root.get(key)
        if isinstance(items, list):
            output.extend(items)
        elif isinstance(items, dict):
            output.extend(_collect_result_items(items, depth=8))
    return output


def _collect_named_values(node: Any, names: set[str], depth: int) -> List[Any]:
    output: List[Any] = []
    if depth <= 0:
        return output
    if isinstance(node, dict):
        for key, value in node.items():
            if key in names and isinstance(value, (dict, list)):
                output.append(value)
            if isinstance(value, (dict, list)):
                output.extend(_collect_named_values(value, names, depth - 1))
    elif isinstance(node, list):
        for item in node[:500]:
            if isinstance(item, (dict, list)):
                output.extend(_collect_named_values(item, names, depth - 1))
    return output


def _collect_result_items(node: Any, depth: int) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    if depth <= 0:
        return output
    if isinstance(node, list):
        for item in node[:500]:
            if isinstance(item, dict):
                if _looks_like_search_result_item(item):
                    output.append(item)
                output.extend(_collect_result_items(item, depth - 1))
        return output
    if not isinstance(node, dict):
        return output
    if _looks_like_search_result_item(node):
        output.append(node)
    for key in (
        "searchResults",
        "searchResultsV2",
        "mapResults",
        "mapSearchResults",
        "mapSearchResultsV2",
        "staysInViewport",
        "results",
        "resultItems",
        "resultSections",
        "items",
        "listingItems",
        "edges",
        "sections",
        "cards",
        "cardSections",
        "itemSections",
        "exploreTabs",
        "listingCards",
    ):
        value = node.get(key)
        if isinstance(value, (dict, list)):
            output.extend(_collect_result_items(value, depth - 1))
    for wrapper_key in ("node", "item", "result", "listingCard", "card"):
        value = node.get(wrapper_key)
        if isinstance(value, dict):
            output.extend(_collect_result_items(value, depth - 1))
    return output


def _looks_like_search_result_item(item: Dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if any(isinstance(item.get(key), dict) for key in ("listing", "stayListing", "stay", "demandStayListing")):
        return True
    if "listingId" in item or "stayListingId" in item:
        return True
    if "roomId" in item and ("title" in item or "name" in item or "structuredDisplayPrice" in item):
        return True
    if "propertyId" in item and (
        "structuredDisplayPrice" in item
        or "pricingQuote" in item
        or "title" in item
        or "nameLocalized" in item
    ):
        return True
    return False


def _extract_search_result_listing(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    listing = item.get("demandStayListing")
    if isinstance(listing, dict):
        return listing
    return None


def _extract_search_result_listing_id(item: Dict[str, Any]) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    listing_id = item.get("propertyId")
    if not listing_id:
        listing_id = item.get("roomId")
    if listing_id:
        return str(listing_id)
    listing = item.get("demandStayListing")
    if isinstance(listing, dict):
        raw = listing.get("id")
        if raw:
            return str(raw)
    return None


def _extract_search_result_title(item: Dict[str, Any]) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    if title:
        return str(title)
    name_localized = item.get("nameLocalized")
    if isinstance(name_localized, dict):
        value = name_localized.get("localizedStringWithTranslationPreference")
        if value:
            return str(value)
    listing = item.get("demandStayListing")
    if isinstance(listing, dict):
        description = listing.get("description")
        if isinstance(description, dict):
            name = description.get("name")
            if isinstance(name, dict):
                value = name.get("localizedStringWithTranslationPreference")
                if value:
                    return str(value)
    return None


def _extract_search_result_property_type(item: Dict[str, Any], title: Optional[str]) -> Optional[str]:
    if title and " in " in title:
        return title.split(" in ", 1)[0].strip() or None
    if not isinstance(item, dict):
        return None
    subtitle = item.get("subtitle")
    if subtitle:
        return str(subtitle)
    return None


def _extract_search_result_location(item: Dict[str, Any], title: Optional[str]) -> Optional[str]:
    if title and " in " in title:
        return title.split(" in ", 1)[1].strip() or None
    if not isinstance(item, dict):
        return None
    structured = item.get("structuredContent")
    if isinstance(structured, dict):
        primary = structured.get("primaryLine")
        if primary:
            return str(primary)
    subtitle = item.get("subtitle")
    if subtitle:
        return str(subtitle)
    return None


def _extract_search_result_rating(item: Dict[str, Any]) -> Optional[float]:
    if not isinstance(item, dict):
        return None
    rating = item.get("avgRatingLocalized") or item.get("avgRatingA11yLabel")
    return _to_float(rating)


def _extract_search_result_price(item: Dict[str, Any]) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    structured = item.get("structuredDisplayPrice")
    if isinstance(structured, dict):
        primary = structured.get("primaryLine")
        if isinstance(primary, dict):
            value = (
                primary.get("discountedPrice")
                or primary.get("price")
                or primary.get("accessibilityLabel")
                or primary.get("originalPrice")
            )
            if value:
                return str(value)
        if primary:
            return str(primary)
    return None


def _extract_search_result_review_count(item: Dict[str, Any]) -> Optional[int]:
    if not isinstance(item, dict):
        return None
    structured = item.get("structuredContent")
    if isinstance(structured, dict):
        snippet = structured.get("reviewSnippet")
        if snippet:
            return _to_int(snippet)
    return None


def _extract_search_pricing(item: Dict[str, Any], currency_hint: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    pricing = None
    currency_hint = None
    pricing_quote = item.get("pricingQuote") or {}
    if isinstance(pricing_quote, dict):
        currency_hint = pricing_quote.get("currency") or pricing_quote.get("ratePlanCurrency") or currency_hint
        structured = pricing_quote.get("structuredStayDisplayPrice") or pricing_quote.get("structuredDisplayPrice")
        if isinstance(structured, dict):
            pricing = _parse_structured_display_price(structured, currency_hint)
    if not pricing:
        structured = item.get("structuredDisplayPrice")
        if isinstance(structured, dict):
            pricing = _parse_structured_display_price(structured, currency_hint)
    if not pricing:
        price_text = _extract_search_result_price(item) or _extract_price(item)
        if price_text:
            pricing = _parse_price_text(price_text, currency_hint)
    if not pricing:
        return {}
    if currency_hint and not pricing.get("currency"):
        pricing["currency"] = currency_hint

    overrides = item.get("listingParamOverrides") or {}
    nights = _compute_nights(overrides.get("checkin"), overrides.get("checkout"))
    if nights and not pricing.get("nights"):
        pricing["nights"] = nights
    return _apply_nights_to_pricing(pricing)


def _parse_structured_display_price(
    structured: Dict[str, Any],
    currency_hint: Optional[str] = None,
) -> Dict[str, Any]:
    primary = structured.get("primaryLine")
    price_text = None
    qualifier = None
    accessibility = None
    if isinstance(primary, dict):
        price_text = _coerce_price_text(
            primary.get("discountedPrice")
            or primary.get("price")
            or primary.get("originalPrice")
            or primary.get("accessibilityLabel")
        )
        qualifier = primary.get("qualifier") or primary.get("trailing") or primary.get("qualifierLine")
        accessibility = primary.get("accessibilityLabel")
    elif isinstance(primary, str):
        price_text = primary
    if not price_text:
        price_text = _coerce_price_text(structured.get("price") or structured.get("accessibilityLabel"))
    display_style = structured.get("displayPriceStyle")
    combined = " ".join([str(x) for x in (price_text, qualifier, accessibility) if x])

    currency = _extract_currency_code(price_text or "") or _extract_currency_code(accessibility or "") or currency_hint
    value = _parse_price_value(price_text or accessibility)
    nights = _extract_nights(combined)
    price_type = _infer_price_type(combined, display_style)

    pricing = {
        "price_display": price_text or accessibility,
        "currency": currency,
        "price_type": price_type,
        "nights": nights,
        "source": "structuredDisplayPrice",
    }
    if value is not None:
        if price_type == "nightly":
            pricing["price_nightly"] = value
        else:
            pricing["price_total"] = value
    return pricing


def _parse_price_text(text: str, currency_hint: Optional[str] = None) -> Dict[str, Any]:
    value = _parse_price_value(text)
    currency = _extract_currency_code(text) or currency_hint
    price_type = _infer_price_type(text, None)
    pricing = {
        "price_display": text,
        "currency": currency,
        "price_type": price_type,
        "source": "priceText",
    }
    if value is not None:
        if price_type == "nightly":
            pricing["price_nightly"] = value
        else:
            pricing["price_total"] = value
    return pricing


def _apply_nights_to_pricing(pricing: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(pricing, dict):
        return {}
    pricing = dict(pricing)
    nights = pricing.get("nights")
    if nights is not None:
        try:
            nights = int(nights)
        except Exception:
            nights = None
    if not nights:
        return pricing

    price_total = pricing.get("price_total")
    price_nightly = pricing.get("price_nightly")
    price_type = pricing.get("price_type")

    if price_type == "nightly":
        if price_nightly is None and price_total is not None:
            price_nightly = price_total
        if price_total is None and price_nightly is not None:
            price_total = round(float(price_nightly) * nights, 2)
    else:
        if price_total is None and price_nightly is not None:
            price_total = round(float(price_nightly) * nights, 2)
        if price_nightly is None and price_total is not None:
            price_nightly = round(float(price_total) / nights, 2)
        if not price_type:
            price_type = "total"

    pricing["price_total"] = price_total
    pricing["price_nightly"] = price_nightly
    pricing["price_type"] = price_type
    pricing["nights"] = nights
    return pricing


def _coerce_price_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("price", "formatted", "priceString", "display", "amountFormatted", "amount", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
            if isinstance(candidate, (int, float)):
                return str(candidate)
    return str(value)


def _extract_currency_from_response_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        currency = params.get("currency", [None])[0]
        if currency:
            return str(currency).upper()
    except Exception:
        return None
    return None


def _parse_price_value(text: Optional[str]) -> Optional[float]:
    if not text or not isinstance(text, str):
        return None
    cleaned = text.replace("\u00a0", " ")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", cleaned.replace(",", ""))
    if match:
        try:
            return float(match.group(1))
        except Exception:
            return None
    return None


def _extract_nights(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"(\d+)\s+nights?", text.lower())
    if match:
        return _to_int(match.group(1))
    return None


def _infer_price_type(text: str, display_style: Optional[str]) -> str:
    if display_style:
        style = str(display_style).upper()
        if "TOTAL" in style:
            return "total"
        if "PER_NIGHT" in style or "NIGHTLY" in style:
            return "nightly"
    lowered = (text or "").lower()
    if "per night" in lowered or "/ night" in lowered or "nightly" in lowered:
        return "nightly"
    if "for" in lowered and "night" in lowered:
        return "total"
    if "total" in lowered:
        return "total"
    return "total"


def _compute_nights(checkin: Optional[str], checkout: Optional[str]) -> Optional[int]:
    if not checkin or not checkout:
        return None
    try:
        start = time.strptime(checkin, "%Y-%m-%d")
        end = time.strptime(checkout, "%Y-%m-%d")
        start_ts = time.mktime(start)
        end_ts = time.mktime(end)
        nights = int((end_ts - start_ts) / 86400)
        if nights <= 0:
            return None
        return nights
    except Exception:
        return None


def _extract_search_result_currency(item: Dict[str, Any], price: Optional[str]) -> Optional[str]:
    if price and isinstance(price, str):
        code = _extract_currency_code(price)
        if code:
            return code
    structured = item.get("structuredDisplayPrice") if isinstance(item, dict) else None
    if isinstance(structured, dict):
        primary = structured.get("primaryLine")
        if isinstance(primary, dict):
            for key in ("discountedPrice", "originalPrice", "accessibilityLabel"):
                value = primary.get(key)
                if isinstance(value, str):
                    code = _extract_currency_code(value)
                    if code:
                        return code
    return None


def _extract_search_result_url(item: Dict[str, Any]) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    listing_id = _normalize_listing_id(_extract_search_result_listing_id(item))
    if listing_id:
        return f"https://www.airbnb.com/rooms/{listing_id}"
    return None


def _normalize_listing_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if text.isdigit():
        return text
    decoded = _decode_airbnb_id(text)
    if decoded:
        return decoded
    return text


def _decode_airbnb_id(value: Any) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
        except Exception:
            return None
    match = re.search(r"(\d+)", decoded)
    if match:
        return match.group(1)
    return None


def _extract_currency_code(text: str) -> Optional[str]:
    if not text:
        return None
    cleaned = text.replace("\u00a0", " ")
    match = re.search(r"([A-Z]{3})", cleaned)
    if match:
        return match.group(1)
    return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _merge_listing(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)
    keys = [
        "url",
        "title",
        "property_type",
        "location",
        "lat",
        "lng",
        "rating",
        "review_count",
        "price",
        "currency",
        "image",
    ]
    for key in keys:
        if merged.get(key) in (None, "", []):
            value = incoming.get(key)
            if value not in (None, "", []):
                merged[key] = value
    incoming_pricing = incoming.get("pricing")
    if incoming_pricing:
        current_pricing = merged.get("pricing")
        if not current_pricing or _score_pricing(incoming_pricing) > _score_pricing(current_pricing):
            merged["pricing"] = incoming_pricing
    if _score_listing(incoming) > _score_listing(existing):
        merged["captured_at"] = incoming.get("captured_at") or merged.get("captured_at")
    return merged


def _score_listing(listing: Dict[str, Any]) -> int:
    score = 0
    for key in (
        "url",
        "title",
        "property_type",
        "location",
        "lat",
        "lng",
        "rating",
        "review_count",
        "price",
        "currency",
        "image",
    ):
        if listing.get(key) not in (None, "", []):
            score += 1
    return score


def _score_pricing(pricing: Dict[str, Any]) -> int:
    score = 0
    for key in ("price_total", "price_nightly", "currency", "nights"):
        if pricing.get(key) not in (None, "", []):
            score += 1
    return score


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _safe_location_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "localizedName", "city", "title"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            match = re.search(r"(\d+)", value.replace(",", ""))
            if match:
                return int(match.group(1))
        return int(value)
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
            if match:
                return float(match.group(1))
        return float(value)
    except Exception:
        return None
