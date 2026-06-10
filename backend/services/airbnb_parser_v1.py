import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .parser_drift import build_parser_meta, build_response_signature


SCRIPT_PATTERN = re.compile(r'<script[^>]+id="data-deferred-state-0"[^>]*>(.*?)</script>', re.S)
LISTING_PARSER_VERSION = "airbnb_listing_v1"


def parse_capture(capture: Dict[str, Any], listing_id: str, listing_url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    listing = _empty_listing(listing_id, listing_url)
    reviews: List[Dict[str, Any]] = []

    responses = capture.get("responses") or []
    html = capture.get("html")
    deferred_state = None
    stay_node = None
    section_ids: List[str] = []
    if html:
        deferred_state = _extract_deferred_state(html)
        stay_node = _find_stay_product_detail(deferred_state)
        if stay_node:
            section_ids = _extract_section_ids(stay_node)
        listing.update(
            parse_listing_from_html(
                html,
                listing_id,
                listing_url,
                _preparsed_node=stay_node,
                _deferred_state=deferred_state,
            )
        )

    review_telemetry: Dict[str, Any] = {}
    reviews = parse_reviews_from_responses(responses, listing_id, listing_url, telemetry=review_telemetry)
    pricing_from_responses = False
    if not _has_pricing(listing):
        pricing = _extract_pricing_from_responses(responses, listing_url)
        if pricing:
            pricing_from_responses = True
            listing["pricing"] = pricing
            availability = listing.get("availability") or {}
            if pricing.get("nights") and not availability.get("nights"):
                availability["nights"] = pricing.get("nights")
            listing["availability"] = availability

    review_response_count = _count_review_responses(responses)
    warnings: List[str] = []
    if html and not stay_node:
        warnings.append("missing_stay_product_detail_html_node")
    if review_response_count > 0 and not reviews:
        warnings.append("review_responses_without_parsed_reviews")
    if (
        int(review_telemetry.get("fallback_path_hits") or 0) > 0
        and int(review_telemetry.get("primary_path_hits") or 0) == 0
    ):
        warnings.append("reviews_primary_path_missing_used_fallback")
    amenities_from_fallback = bool(listing.pop("_amenities_from_fallback", False))

    listing["parser_meta"] = build_parser_meta(
        parser_version=LISTING_PARSER_VERSION,
        signature=build_response_signature(responses, mode="listing"),
        warnings=warnings,
        fallbacks={
            "pricing_from_responses": bool(pricing_from_responses),
            "review_fallback_scan_used": bool(review_telemetry.get("fallback_path_hits")),
            "amenities_from_deferred_state_scan": amenities_from_fallback,
        },
        signals={
            "section_ids": section_ids[:12],
            "review_response_count": review_response_count,
            "parsed_reviews_count": len(reviews),
            "primary_review_path_hits": int(review_telemetry.get("primary_path_hits") or 0),
            "fallback_review_path_hits": int(review_telemetry.get("fallback_path_hits") or 0),
            "parsed_amenity_group_count": len(listing.get("amenities") or []),
        },
    )

    return listing, reviews


def parse_listing_from_html(
    html: str,
    listing_id: str,
    listing_url: str,
    _preparsed_node: Optional[Dict[str, Any]] = None,
    _deferred_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    deferred_state = _deferred_state if isinstance(_deferred_state, dict) else _extract_deferred_state(html)
    node = _preparsed_node if isinstance(_preparsed_node, dict) else _find_stay_product_detail(deferred_state)
    if not node:
        return _empty_listing(listing_id, listing_url)

    sections = node.get("sections", {}) or {}
    section_list = sections.get("sections", []) or []
    by_id = {entry.get("sectionId"): entry.get("section") or {} for entry in section_list}

    metadata = sections.get("metadata") or {}
    share = metadata.get("sharingConfig") or {}

    title = _first_non_empty(by_id.get("TITLE_DEFAULT", {}).get("title"), share.get("title"))
    property_type = share.get("propertyType")
    location = share.get("location")
    capacity = {
        "guests": share.get("personCapacity"),
        "bedrooms": _parse_count_from_text(share.get("title"), "bedroom"),
        "beds": _parse_count_from_text(share.get("title"), "bed"),
        "baths": _parse_count_from_text(share.get("title"), "bath"),
    }

    description = _extract_description(by_id.get("DESCRIPTION_MODAL", {}))
    amenities = _extract_amenities(by_id.get("AMENITIES_DEFAULT", {}))
    amenities_from_fallback = False
    if not amenities:
        amenities = _extract_amenities_from_deferred_state(deferred_state)
        amenities_from_fallback = bool(amenities)
    sleeping = _extract_sleeping_arrangements(by_id.get("SLEEPING_ARRANGEMENT_WITH_IMAGES", {}))

    policies = by_id.get("POLICIES_DEFAULT", {}) or {}
    house_rules = _extract_titles(policies.get("houseRules") or [])
    safety_notes = _extract_titles(policies.get("previewSafetyAndProperties") or [])

    host_section = by_id.get("MEET_YOUR_HOST", {}) or {}
    host_card = host_section.get("cardData") or {}
    host = {
        "id": host_card.get("userId"),
        "name": host_card.get("name") or host_card.get("title"),
        "superhost": host_card.get("isSuperhost"),
        "rating": host_card.get("ratingAverage"),
        "review_count": host_card.get("ratingCount"),
        "response_details": host_section.get("hostDetails") or [],
    }

    reviews_section = by_id.get("REVIEWS_DEFAULT", {}) or {}
    reviews_summary = {
        "overall_rating": reviews_section.get("overallRating"),
        "count": reviews_section.get("overallCount"),
        "category_ratings": reviews_section.get("ratings") or [],
        "distribution": reviews_section.get("ratingDistribution") or [],
    }

    location_section = by_id.get("LOCATION_DEFAULT", {}) or {}
    location_details = {
        "title": location_section.get("title"),
        "subtitle": location_section.get("subtitle"),
        "address": location_section.get("address"),
        "address_title": location_section.get("addressTitle"),
        "lat": location_section.get("lat"),
        "lng": location_section.get("lng"),
        "summary": _extract_titles(location_section.get("summaryLocationDetails") or []),
    }

    photos = _extract_photos(by_id.get("PHOTO_TOUR_SCROLLABLE_MODAL", {}) or {})
    availability = _extract_availability_from_url(listing_url)
    pricing = _extract_pricing(node, sections, share, listing_url)
    if pricing.get("nights") and not availability.get("nights"):
        availability["nights"] = pricing.get("nights")
    if availability.get("nights") and not pricing.get("nights"):
        pricing["nights"] = availability.get("nights")
    pricing = _apply_nights_to_pricing(pricing)

    listing = {
        "id": listing_id,
        "source": "airbnb",
        "url": listing_url,
        "title": title,
        "property_type": property_type,
        "location": {
            "name": location,
            "details": location_details,
        },
        "capacity": capacity,
        "description": description,
        "amenities": amenities,
        "sleeping_arrangements": sleeping,
        "house_rules": house_rules,
        "cancellation_policy": policies.get("cancellationPolicyForDisplay"),
        "safety_notes": safety_notes,
        "host": host,
        "reviews_summary": reviews_summary,
        "photos": photos,
        "representative_photos": _select_representative_photos(photos),
        "pricing": pricing,
        "availability": availability,
        "captured_at": _now_iso(),
    }
    if amenities_from_fallback:
        listing["_amenities_from_fallback"] = True
    return listing


def parse_reviews_from_responses(
    responses: List[Dict[str, Any]],
    listing_id: str,
    listing_url: str,
    telemetry: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    reviews: List[Dict[str, Any]] = []
    seen = set()
    primary_path_hits = 0
    fallback_path_hits = 0
    fallback_candidate_count = 0
    for response in responses:
        data = response.get("data")
        if not isinstance(data, dict):
            continue

        candidates = _deep_get(data, "data.presentation.stayProductDetailPage.reviews.reviews")
        if isinstance(candidates, list):
            primary_path_hits += 1
            _collect_reviews(candidates, listing_id, listing_url, reviews, seen)

        if not candidates:
            fallback_candidates = _find_review_objects(data)
            if fallback_candidates:
                fallback_path_hits += 1
                fallback_candidate_count += len(fallback_candidates)
            _collect_reviews(fallback_candidates, listing_id, listing_url, reviews, seen)

    if isinstance(telemetry, dict):
        telemetry["primary_path_hits"] = primary_path_hits
        telemetry["fallback_path_hits"] = fallback_path_hits
        telemetry["fallback_candidate_count"] = fallback_candidate_count

    return reviews


def _collect_reviews(
    items: List[Dict[str, Any]],
    listing_id: str,
    listing_url: str,
    out: List[Dict[str, Any]],
    seen: set,
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        review_id = item.get("id") or item.get("reviewId") or ""
        text = (
            item.get("comments")
            or item.get("comment")
            or item.get("reviewText")
            or item.get("review_text")
            or item.get("text")
        )
        if not text:
            continue
        key = review_id or f"{text}-{item.get('createdAt')}-{item.get('reviewer')}"
        if key in seen:
            continue
        seen.add(key)
        reviewer = item.get("reviewer") or item.get("author") or {}
        out.append(
            {
                "id": review_id or None,
                "listing_id": listing_id,
                "rating": item.get("rating") or item.get("reviewRating"),
                "date": item.get("createdAt") or item.get("created_at"),
                "language": item.get("language"),
                "text": text,
                "reviewer": {
                    "name": reviewer.get("name") if isinstance(reviewer, dict) else None,
                    "location": reviewer.get("location") if isinstance(reviewer, dict) else None,
                },
                "source_url": listing_url,
                "captured_at": _now_iso(),
            }
        )


def _find_review_objects(node: Any) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        text_keys = {"comments", "comment", "reviewText", "review_text", "text"}
        meta_keys = {"rating", "reviewRating", "reviewer", "author", "createdAt", "created_at"}
        if text_keys.intersection(node.keys()) and meta_keys.intersection(node.keys()):
            results.append(node)
        for value in node.values():
            results.extend(_find_review_objects(value))
    elif isinstance(node, list):
        for item in node:
            results.extend(_find_review_objects(item))
    return results


def _extract_deferred_state(html: str) -> Optional[Dict[str, Any]]:
    match = SCRIPT_PATTERN.search(html or "")
    if not match:
        return None
    raw = match.group(1).strip()
    raw = raw.replace("&quot;", "\"")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_stay_product_detail(html: str) -> Optional[Dict[str, Any]]:
    return _find_stay_product_detail(_extract_deferred_state(html))


def _find_stay_product_detail(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    for entry in data.get("niobeClientData", []):
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        payload = entry[1]
        if not isinstance(payload, dict):
            continue
        presentation = (payload.get("data") or {}).get("presentation") or {}
        stay = presentation.get("stayProductDetailPage")
        if stay:
            return stay
    return None


def _has_pricing(listing: Dict[str, Any]) -> bool:
    pricing = listing.get("pricing")
    if not isinstance(pricing, dict):
        return False
    if pricing.get("price_total") is not None or pricing.get("price_nightly") is not None:
        return True
    return bool(pricing.get("price_display"))


def _score_pricing(pricing: Optional[Dict[str, Any]]) -> int:
    if not isinstance(pricing, dict):
        return 0
    score = 0
    for key in ("price_total", "price_nightly", "currency", "nights"):
        if pricing.get(key) not in (None, "", []):
            score += 1
    return score


def _extract_pricing_from_responses(responses: List[Dict[str, Any]], listing_url: str) -> Dict[str, Any]:
    currency_hint = _extract_currency_hint({}, listing_url)
    best: Dict[str, Any] = {}
    best_score = 0
    for response in responses or []:
        data = response.get("data")
        if not isinstance(data, dict):
            continue
        candidates: List[Any] = []
        stay = _deep_get(data, "data.presentation.stayProductDetailPage")
        if isinstance(stay, dict):
            candidates.append(stay)
        candidates.append(data)
        for node in candidates:
            structured, source = _find_structured_display_price(node)
            if not structured:
                continue
            pricing = _parse_structured_display_price(structured, source, currency_hint)
            if not pricing:
                continue
            pricing = _apply_nights_to_pricing(pricing)
            score = _score_pricing(pricing)
            if score > best_score:
                best = pricing
                best_score = score
    return best


def _extract_description(section: Dict[str, Any]) -> str:
    items = section.get("items") or []
    parts: List[str] = []
    for item in items:
        html = (item.get("html") or {}).get("htmlText")
        text = item.get("text") or item.get("title")
        if html:
            parts.append(_strip_html(html))
        elif text:
            parts.append(text)
    return "\n".join([p for p in parts if p]).strip()


def _extract_amenities(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    groups = section.get("seeAllAmenitiesGroups") or section.get("previewAmenitiesGroups") or []
    return _normalize_amenity_groups(groups)


def _extract_amenities_from_deferred_state(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for candidate in _find_amenity_containers(data):
        groups = candidate.get("seeAllAmenitiesGroups") or candidate.get("previewAmenitiesGroups") or []
        amenities = _normalize_amenity_groups(groups)
        if amenities:
            return amenities
    return []


def _find_amenity_containers(node: Any, depth: int = 14) -> List[Dict[str, Any]]:
    if depth <= 0:
        return []
    containers: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        has_groups = isinstance(node.get("seeAllAmenitiesGroups"), list) or isinstance(
            node.get("previewAmenitiesGroups"),
            list,
        )
        if has_groups:
            containers.append(node)
        for key, value in node.items():
            if key in {"loggingEventData", "loggingContext"}:
                continue
            containers.extend(_find_amenity_containers(value, depth - 1))
    elif isinstance(node, list):
        for item in node:
            containers.extend(_find_amenity_containers(item, depth - 1))
    return containers


def _normalize_amenity_groups(groups: Any) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not isinstance(groups, list):
        return results
    for group in groups:
        if not isinstance(group, dict):
            continue
        title = group.get("title")
        amenities = [
            item.get("title")
            for item in (group.get("amenities") or [])
            if isinstance(item, dict) and item.get("title") and item.get("available", True) is not False
        ]
        if title or amenities:
            results.append({"group": title, "items": amenities})
    return results


def _extract_sleeping_arrangements(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    arrangements = []
    for entry in section.get("arrangementDetails") or []:
        arrangements.append(
            {
                "title": entry.get("title"),
                "subtitle": entry.get("subtitle"),
                "beds": [bed.get("title") for bed in (entry.get("beds") or []) if bed.get("title")],
            }
        )
    return arrangements


PHOTO_URL_KEYS = (
    "baseUrl",
    "url",
    "originalPicture",
    "picture",
    "large",
    "xlPicture",
    "thumbnailUrl",
    "previewEncodedPng",
)

PHOTO_TEXT_KEYS = (
    "caption",
    "localizedCaption",
    "title",
    "accessibilityLabel",
    "imageType",
    "roomType",
    "roomTitle",
)

PHOTO_AREA_KEYWORDS = (
    ("bedroom", ("bedroom", "bed room", "queen bed", "king bed", "bunk", "sleeping")),
    ("kitchen", ("kitchen", "stove", "oven", "cooktop", "fridge", "refrigerator")),
    ("living room", ("living room", "lounge", "sofa", "couch", "fireplace")),
    ("bathroom", ("bathroom", "bath", "shower", "toilet", "tub")),
    ("outdoor", ("backyard", "yard", "deck", "patio", "porch", "terrace", "garden", "fire pit")),
    ("view", ("view", "mountain", "lake", "river", "creek", "sunset")),
    ("workspace", ("workspace", "desk", "office")),
    ("dining", ("dining", "table", "breakfast")),
    ("amenity", ("hot tub", "pool", "sauna", "grill", "bbq", "washer", "dryer")),
)


def _extract_photos(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    photos: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(section.get("mediaItems") or []):
        if not isinstance(item, dict):
            continue
        photo = _normalize_photo_item(item, index)
        url = photo.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        photos.append(photo)
    return photos


def _normalize_photo_item(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    url = _first_photo_url(item)
    caption = _photo_text(item.get("caption"))
    localized_caption = _photo_text(item.get("localizedCaption"))
    title = _photo_text(item.get("title"))
    image_type = _photo_text(item.get("imageType"))
    room_or_area = _first_non_empty(
        _photo_text(item.get("roomType")),
        _photo_text(item.get("roomTitle")),
        _infer_photo_area(item),
    )
    return {
        "url": url,
        "caption": caption,
        "localized_caption": localized_caption,
        "title": title,
        "image_type": image_type,
        "room_or_area": room_or_area,
        "position": index,
    }


def _first_photo_url(item: Dict[str, Any]) -> Optional[str]:
    for key in PHOTO_URL_KEYS:
        text = _photo_text(item.get(key))
        if text:
            return text
    return None


def _photo_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("text", "title", "caption", "localizedText", "value"):
            text = _photo_text(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, list):
        parts = [_photo_text(item) for item in value]
        text = " ".join(part for part in parts if part)
        return text or None
    text = str(value).strip()
    return text or None


def _infer_photo_area(item: Dict[str, Any]) -> Optional[str]:
    haystack = " ".join(
        text
        for key in PHOTO_TEXT_KEYS
        for text in [_photo_text(item.get(key))]
        if text
    ).lower()
    if not haystack:
        return None
    for area, keywords in PHOTO_AREA_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return area
    return None


def _select_representative_photos(photos: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    representatives: Dict[str, Dict[str, Any]] = {}
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        area = photo.get("room_or_area")
        if area and area not in representatives:
            representatives[area] = photo
    return representatives


def _extract_pricing(
    stay: Dict[str, Any],
    sections: Dict[str, Any],
    share: Dict[str, Any],
    listing_url: str,
) -> Dict[str, Any]:
    currency_hint = _extract_currency_hint(share, listing_url)
    structured, source = _find_structured_display_price(stay)
    if not structured:
        structured, source = _find_structured_display_price(sections)
    if structured:
        pricing = _parse_structured_display_price(structured, source, currency_hint)
        if pricing:
            return pricing

    share_price = None
    if isinstance(share, dict):
        share_price = share.get("price") or share.get("priceFormatted") or share.get("priceString")
    if share_price:
        return _parse_price_text(share_price, currency_hint, source="sharingConfig")

    return {}


def _find_structured_display_price(node: Any, depth: int = 6) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if depth <= 0:
        return None, None
    if isinstance(node, dict):
        for key in ("structuredStayDisplayPrice", "structuredDisplayPrice"):
            value = node.get(key)
            if isinstance(value, dict):
                return value, key
        if "primaryLine" in node and any(k in node for k in ("displayPriceStyle", "secondaryLine")):
            return node, "structuredDisplayPrice"
        for value in node.values():
            found, source = _find_structured_display_price(value, depth - 1)
            if found:
                return found, source
    elif isinstance(node, list):
        for item in node:
            found, source = _find_structured_display_price(item, depth - 1)
            if found:
                return found, source
    return None, None


def _parse_structured_display_price(
    structured: Dict[str, Any],
    source: Optional[str],
    currency_hint: Optional[str],
) -> Dict[str, Any]:
    primary = structured.get("primaryLine")
    price_text = None
    qualifier = None
    accessibility = None
    if isinstance(primary, dict):
        price_text = (
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
        price_text = structured.get("price") or structured.get("accessibilityLabel")
    display_style = structured.get("displayPriceStyle")
    combined = " ".join([str(x) for x in (price_text, qualifier, accessibility) if x])

    currency = _extract_currency_code(price_text) or _extract_currency_code(accessibility) or currency_hint
    value = _parse_price_value(price_text or accessibility)
    nights = _extract_nights(combined) or _extract_nights_from_explanation(structured)
    price_type = _infer_price_type(combined, display_style)

    pricing = {
        "price_display": price_text or accessibility,
        "currency": currency,
        "price_type": price_type,
        "nights": nights,
        "source": source or "structuredDisplayPrice",
    }
    if value is not None:
        if price_type == "nightly":
            pricing["price_nightly"] = value
        else:
            pricing["price_total"] = value
    return pricing


def _parse_price_text(
    text: str,
    currency_hint: Optional[str],
    *,
    source: str,
) -> Dict[str, Any]:
    value = _parse_price_value(text)
    currency = _extract_currency_code(text) or currency_hint
    price_type = _infer_price_type(text, None)
    pricing = {
        "price_display": text,
        "currency": currency,
        "price_type": price_type,
        "source": source,
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


def _extract_nights_from_explanation(structured: Dict[str, Any]) -> Optional[int]:
    explanation = structured.get("explanationData") or {}
    details = explanation.get("priceDetails") or []
    for group in details:
        for item in group.get("items") or []:
            description = item.get("description") or ""
            match = re.search(r"(\\d+)\\s*nights?\\s*x", description)
            if match:
                try:
                    return int(match.group(1))
                except Exception:
                    continue
    return None


def _infer_price_type(text: str, display_style: Optional[str]) -> Optional[str]:
    if display_style:
        style = str(display_style).upper()
        if "TOTAL" in style:
            return "total"
        if "PER_NIGHT" in style or "NIGHTLY" in style:
            return "nightly"
    if not text:
        return None
    lowered = text.lower()
    if "per night" in lowered or "/ night" in lowered or "nightly" in lowered:
        return "nightly"
    if "for" in lowered and "night" in lowered:
        return "total"
    if "total" in lowered:
        return "total"
    return None


def _extract_nights(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"(\\d+)\\s*night", text.lower())
    if match:
        try:
            return int(match.group(1))
        except Exception:
            return None
    return None


def _parse_price_value(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = str(text).replace(",", "").replace("\u00a0", " ")
    match = re.search(r"([0-9]+(?:\\.[0-9]+)?)", cleaned)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _extract_currency_hint(share: Dict[str, Any], listing_url: str) -> Optional[str]:
    if isinstance(share, dict):
        currency = share.get("currency")
        if currency:
            return str(currency).upper()
    try:
        query = parse_qs(urlparse(listing_url).query or "")
    except Exception:
        return None
    hint = query.get("currency", [None])[0]
    if hint:
        return str(hint).upper()
    return None


def _extract_currency_code(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = str(text)
    match = re.search(r"([A-Z]{3})", cleaned)
    if match:
        return match.group(1)
    if "$" in cleaned:
        return "USD"
    if "€" in cleaned:
        return "EUR"
    if "£" in cleaned:
        return "GBP"
    if "¥" in cleaned or "￥" in cleaned:
        return "JPY"
    return None


def _extract_availability_from_url(listing_url: str) -> Dict[str, Any]:
    availability = {"check_in": None, "check_out": None, "nights": None}
    if not listing_url:
        return availability
    try:
        query = parse_qs(urlparse(listing_url).query or "")
    except Exception:
        return availability
    check_in = (query.get("check_in") or query.get("checkin") or [None])[0]
    check_out = (query.get("check_out") or query.get("checkout") or [None])[0]
    availability["check_in"] = check_in
    availability["check_out"] = check_out
    availability["nights"] = _compute_nights(check_in, check_out)
    return availability


def _compute_nights(check_in: Optional[str], check_out: Optional[str]) -> Optional[int]:
    if not check_in or not check_out:
        return None
    try:
        start = datetime.strptime(check_in, "%Y-%m-%d").date()
        end = datetime.strptime(check_out, "%Y-%m-%d").date()
        delta = (end - start).days
        return delta if delta > 0 else None
    except Exception:
        return None


def _extract_titles(items: List[Dict[str, Any]]) -> List[str]:
    titles: List[str] = []
    for item in items:
        title = item.get("title") if isinstance(item, dict) else None
        if title:
            titles.append(title)
    return titles


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^<]+?>", "", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_count_from_text(value: Optional[str], label: str) -> Optional[int]:
    if not value:
        return None
    match = re.search(r"(\\d+)\\s+" + re.escape(label), value)
    if not match:
        match = re.search(r"(\\d+)\\s+" + re.escape(label) + "s", value)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def _deep_get(data: Dict[str, Any], path: str) -> Any:
    current = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _empty_listing(listing_id: str, listing_url: str) -> Dict[str, Any]:
    return {
        "id": listing_id,
        "source": "airbnb",
        "url": listing_url,
        "title": None,
        "captured_at": _now_iso(),
    }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _extract_section_ids(node: Dict[str, Any]) -> List[str]:
    section_ids: List[str] = []
    sections = ((node.get("sections") or {}).get("sections") or [])
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        value = entry.get("sectionId")
        if value:
            section_ids.append(str(value))
    return section_ids


def _count_review_responses(responses: List[Dict[str, Any]]) -> int:
    count = 0
    for response in responses or []:
        url = str(response.get("url") or "").lower()
        if "review" in url:
            count += 1
    return count
