import re
from typing import Any, Dict, List, Tuple


WORD_RE = re.compile(r"[a-z0-9]+")


def apply_preference_alignment(
    listings: List[Dict[str, Any]],
    payload: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    requested = _requested_preferences(payload)
    if not requested:
        return listings, {
            "requested": [],
            "scored_count": 0,
            "matched_any_count": 0,
        }

    scored: List[Dict[str, Any]] = []
    matched_any_count = 0
    for index, listing in enumerate(listings):
        listing = dict(listing or {})
        alignment = score_listing_preferences(listing, requested)
        alignment["rank"] = index + 1
        listing["preference_alignment"] = alignment
        if alignment.get("matched_count", 0) > 0:
            matched_any_count += 1
        scored.append(listing)

    scored.sort(
        key=lambda item: (
            -float((item.get("preference_alignment") or {}).get("score") or 0.0),
            -int((item.get("preference_alignment") or {}).get("matched_count") or 0),
            _rank_price(item),
            int((item.get("preference_alignment") or {}).get("rank") or 0),
        )
    )
    for index, listing in enumerate(scored):
        alignment = listing.get("preference_alignment") or {}
        alignment["rank"] = index + 1
        listing["preference_alignment"] = alignment

    return scored, {
        "requested": [item["label"] for item in requested],
        "scored_count": len(scored),
        "matched_any_count": matched_any_count,
    }


def score_listing_preferences(
    listing: Dict[str, Any],
    requested: List[Dict[str, Any]],
) -> Dict[str, Any]:
    matched: List[str] = []
    missing: List[str] = []
    unknown: List[str] = []
    matched_weight = 0.0
    total_weight = 0.0
    searchable = _searchable_text(listing)
    amenities_text, has_structured_amenities = _amenities_text(listing)

    for pref in requested:
        label = pref["label"]
        kind = pref["kind"]
        weight = float(pref.get("weight") or 1.0)
        total_weight += weight
        if kind == "amenity":
            if _phrase_match(label, amenities_text) or _phrase_match(label, searchable):
                matched.append(label)
                matched_weight += weight
            elif has_structured_amenities:
                missing.append(label)
            else:
                unknown.append(label)
            continue

        if _phrase_match(label, searchable):
            matched.append(label)
            matched_weight += weight
        else:
            unknown.append(label)

    score = matched_weight / total_weight if total_weight else 0.0
    return {
        "score": round(score, 3),
        "matched": matched,
        "missing": missing,
        "unknown": unknown,
        "matched_count": len(matched),
        "requested_count": len(requested),
    }


def _requested_preferences(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    requested: List[Dict[str, Any]] = []
    seen = set()

    def add(kind: str, value: Any, weight: float) -> None:
        label = _clean_label(value)
        if not label:
            return
        key = (kind, _normalize(label))
        if key in seen:
            return
        seen.add(key)
        requested.append({"kind": kind, "label": label, "weight": weight})

    for amenity in payload.get("amenities") if isinstance(payload.get("amenities"), list) else []:
        add("amenity", amenity, 2.0)

    for preference in (
        payload.get("soft_preferences") if isinstance(payload.get("soft_preferences"), list) else []
    ):
        add("soft", preference, 1.0)

    room_type = payload.get("room_type")
    if room_type:
        add("soft", room_type, 1.0)

    return requested


def _clean_label(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _normalize(value: Any) -> str:
    return " ".join(WORD_RE.findall(str(value or "").lower()))


def _phrase_match(needle: str, haystack: str) -> bool:
    needle_norm = _normalize(needle)
    haystack_norm = _normalize(haystack)
    if not needle_norm or not haystack_norm:
        return False
    return f" {needle_norm} " in f" {haystack_norm} "


def _searchable_text(listing: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("title", "property_type", "location", "description", "subtitle"):
        value = listing.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    return " ".join(parts)


def _amenities_text(listing: Dict[str, Any]) -> Tuple[str, bool]:
    amenities = listing.get("amenities")
    if not isinstance(amenities, list) or not amenities:
        return "", False
    parts: List[str] = []
    for amenity in amenities:
        if isinstance(amenity, str):
            parts.append(amenity)
        elif isinstance(amenity, dict):
            for key in ("group", "name", "title", "label", "text"):
                if amenity.get(key):
                    parts.append(str(amenity.get(key)))
            items = amenity.get("items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        for key in ("name", "title", "label", "text"):
                            if item.get(key):
                                parts.append(str(item.get(key)))
    return " ".join(parts), True


def _rank_price(listing: Dict[str, Any]) -> float:
    pricing = listing.get("pricing") if isinstance(listing.get("pricing"), dict) else {}
    for key in ("price_total_usd", "price_nightly_usd", "price_total", "price_nightly"):
        value = pricing.get(key)
        try:
            if value is not None:
                return float(value)
        except Exception:
            continue
    return float("inf")
