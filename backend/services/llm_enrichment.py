import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


PROMPT_VERSION = "listing_summary_v1"
PHOTO_FIT_PROMPT_VERSION = "listing_photo_fit_v1"
COMPARISON_PROMPT_VERSION = "listing_comparison_v4"
DEFAULT_MODEL = os.getenv("RENTAL_LLM_MODEL", "gpt-5-mini")
DEFAULT_TIMEOUT = int(os.getenv("RENTAL_LLM_TIMEOUT", "60"))
DEFAULT_REASONING_EFFORT = os.getenv("RENTAL_LLM_REASONING_EFFORT", "").strip().lower()
DEFAULT_MAX_OUTPUT_TOKENS = os.getenv("RENTAL_LLM_MAX_OUTPUT_TOKENS", "").strip()
PHOTO_FIT_MAX_IMAGES = int(os.getenv("RENTAL_PHOTO_FIT_MAX_IMAGES", "10") or 10)
PHOTO_FIT_INCLUDE_UNLABELED = os.getenv("RENTAL_PHOTO_FIT_INCLUDE_UNLABELED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
PHOTO_FIT_MAX_UNLABELED = int(os.getenv("RENTAL_PHOTO_FIT_MAX_UNLABELED", "4") or 4)
PHOTO_FIT_IMAGE_DETAIL = os.getenv("RENTAL_PHOTO_FIT_IMAGE_DETAIL", "low").strip().lower() or "low"
API_KEY = os.getenv("RENTAL_LLM_API_KEY", "")
BASE_URL = os.getenv("RENTAL_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")


class ReviewTheme(BaseModel):
    theme: str
    sentiment: str = Field(..., description="positive | negative | mixed | neutral")
    evidence: str


class ListingSummary(BaseModel):
    summary: str
    highlights: List[str] = []
    constraints: List[str] = []
    pros: List[str] = []
    cons: List[str] = []
    risks: List[str] = []
    best_for: List[str] = []
    review_themes: List[ReviewTheme] = []
    coverage_note: Optional[str] = None
    confidence: str = Field(..., description="low | medium | high")


class PhotoAreaObservation(BaseModel):
    area: str
    observations: List[str] = []
    strengths: List[str] = []
    concerns: List[str] = []
    confidence: str = Field(..., description="low | medium | high")


class ListingPhotoFit(BaseModel):
    visual_summary: str
    visual_strengths: List[str] = []
    visual_concerns: List[str] = []
    area_observations: List[PhotoAreaObservation] = []
    photo_confidence: str = Field(..., description="low | medium | high")
    analyzed_photo_count: int
    coverage_note: Optional[str] = None


class ComparisonWinner(BaseModel):
    listing_id: Optional[str] = None
    reason: str


class ComparisonSection(BaseModel):
    section: str
    winner_listing_id: Optional[str] = None
    notes: List[str] = []


class ComparisonListingNotes(BaseModel):
    listing_id: str
    title: Optional[str] = None
    pros: List[str] = []
    cons: List[str] = []
    watchouts: List[str] = []


class ComparisonPersonalizedFit(BaseModel):
    listing_id: str
    title: Optional[str] = None
    matches: List[str] = []
    mismatches: List[str] = []
    memory_basis: List[int] = []


class ComparisonMemoryCitation(BaseModel):
    citation_index: int
    title: Optional[str] = None
    filename: Optional[str] = None
    source_type: Optional[str] = None
    memory_id: Optional[str] = None
    note: Optional[str] = None


class ComparisonVisualFit(BaseModel):
    listing_id: str
    title: Optional[str] = None
    visual_strengths: List[str] = []
    visual_concerns: List[str] = []
    photo_based_confidence: str = Field(..., description="low | medium | high")
    evidence: List[str] = []


class ListingComparison(BaseModel):
    summary: str
    winner: ComparisonWinner
    sections: List[ComparisonSection] = []
    listing_notes: List[ComparisonListingNotes] = []
    visual_fit: List[ComparisonVisualFit] = []
    personalized_fit: List[ComparisonPersonalizedFit] = []
    memory_citations: List[ComparisonMemoryCitation] = []
    memory_context_note: Optional[str] = None
    tradeoffs: List[str] = []
    confidence: str = Field(..., description="low | medium | high")


def _truncate(text: str, limit: int = 500) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text[:limit]


def _flatten_amenities(listing: Dict[str, Any], limit: int = 40) -> List[str]:
    amenities = listing.get("amenities") or []
    flattened: List[str] = []
    for group in amenities:
        group_name = None
        if isinstance(group, dict):
            group_name = group.get("group") or group.get("name")
            items = group.get("items") or []
        else:
            items = []
        for item in items:
            label = None
            if isinstance(item, dict):
                label = item.get("name") or item.get("title") or item.get("label") or item.get("text")
            elif isinstance(item, str):
                label = item
            if label:
                flattened.append(f"{group_name}: {label}" if group_name else label)
            if len(flattened) >= limit:
                return flattened
    return flattened


def _clean_photo_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("text", "title", "caption", "localizedText", "value"):
            text = _clean_photo_text(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, list):
        text = " ".join(part for part in (_clean_photo_text(item) for item in value) if part)
        return text or None
    text = str(value).strip()
    return text or None


def _photo_url(photo: Any) -> Optional[str]:
    if isinstance(photo, str):
        return photo.strip() or None
    if not isinstance(photo, dict):
        return None
    for key in ("url", "baseUrl", "originalPicture", "picture", "large", "xlPicture", "thumbnailUrl"):
        text = _clean_photo_text(photo.get(key))
        if text:
            return text
    return None


def _photo_area(photo: Any) -> str:
    if not isinstance(photo, dict):
        return "Unlabeled"
    return _clean_photo_text(photo.get("room_or_area") or photo.get("roomType") or photo.get("roomTitle")) or "Unlabeled"


def _photo_caption(photo: Any) -> Optional[str]:
    if not isinstance(photo, dict):
        return None
    return _clean_photo_text(
        photo.get("localized_caption") or photo.get("localizedCaption") or photo.get("caption") or photo.get("title")
    )


def _photo_area_counts(listing: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    photos = listing.get("photos") if isinstance(listing.get("photos"), list) else []
    for photo in photos:
        if not _photo_url(photo):
            continue
        area = _photo_area(photo)
        counts[area] = counts.get(area, 0) + 1
    return counts


def _select_photo_fit_photos(
    listing: Dict[str, Any],
    max_images: Optional[int] = None,
    *,
    include_unlabeled: Optional[bool] = None,
    max_unlabeled: Optional[int] = None,
) -> List[Dict[str, Any]]:
    max_images = max(1, int(max_images or PHOTO_FIT_MAX_IMAGES or 10))
    include_unlabeled = PHOTO_FIT_INCLUDE_UNLABELED if include_unlabeled is None else bool(include_unlabeled)
    max_unlabeled = max(0, int(max_unlabeled if max_unlabeled is not None else PHOTO_FIT_MAX_UNLABELED))
    selected: List[Dict[str, Any]] = []
    seen_urls = set()
    representatives = listing.get("representative_photos")
    if isinstance(representatives, dict):
        source = list(representatives.items())
    else:
        source = []

    for area, photo in source:
        url = _photo_url(photo)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        selected.append(
            {
                "area": str(area or _photo_area(photo)),
                "url": url,
                "caption": _photo_caption(photo),
                "position": photo.get("position") if isinstance(photo, dict) else None,
                "source": "representative",
            }
        )
        if len(selected) >= max_images:
            return selected

    photos = listing.get("photos") if isinstance(listing.get("photos"), list) else []
    seen_areas = {item["area"] for item in selected}
    for photo in photos:
        url = _photo_url(photo)
        area = _photo_area(photo)
        if not url or url in seen_urls or area in seen_areas or area == "Unlabeled":
            continue
        seen_urls.add(url)
        seen_areas.add(area)
        selected.append(
            {
                "area": area,
                "url": url,
                "caption": _photo_caption(photo),
                "position": photo.get("position") if isinstance(photo, dict) else None,
                "source": "metadata_labeled",
            }
        )
        if len(selected) >= max_images:
            return selected
    if include_unlabeled and max_unlabeled > 0 and len(selected) < max_images:
        unlabeled = [
            photo
            for photo in photos
            if _photo_url(photo) and _photo_url(photo) not in seen_urls and _photo_area(photo) == "Unlabeled"
        ]
        remaining = min(max_images - len(selected), max_unlabeled, len(unlabeled))
        if remaining > 0:
            if remaining == 1:
                indexes = [0]
            else:
                span = max(1, len(unlabeled) - 1)
                indexes = sorted({round(i * span / (remaining - 1)) for i in range(remaining)})
                cursor = 0
                while len(indexes) < remaining and cursor < len(unlabeled):
                    if cursor not in indexes:
                        indexes.append(cursor)
                    cursor += 1
                indexes = sorted(indexes[:remaining])
            for index in indexes:
                photo = unlabeled[index]
                url = _photo_url(photo)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                selected.append(
                    {
                        "area": "Unlabeled",
                        "url": url,
                        "caption": _photo_caption(photo),
                        "position": photo.get("position") if isinstance(photo, dict) else None,
                        "source": "unlabeled_fallback",
                    }
                )
                if len(selected) >= max_images:
                    return selected
    return selected


def build_listing_summary_input(listing: Dict[str, Any], reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    location = listing.get("location")
    location_name = None
    if isinstance(location, dict):
        location_name = location.get("name")
    elif isinstance(location, str):
        location_name = location

    pricing: Dict[str, Any] = {}
    pricing_src = listing.get("pricing")
    if isinstance(pricing_src, dict):
        pricing = dict(pricing_src)

    pricing_payload = {
        "price_display": pricing.get("price_display"),
        "price_type": pricing.get("price_type"),
        "nights": pricing.get("nights"),
        "currency": pricing.get("currency") or listing.get("currency"),
        "price_total": pricing.get("price_total"),
        "price_nightly": pricing.get("price_nightly"),
        "price_total_usd": pricing.get("price_total_usd") or listing.get("price_usd_total"),
        "price_nightly_usd": pricing.get("price_nightly_usd") or listing.get("price_usd_nightly"),
        "price_usd": listing.get("price_usd")
        or pricing.get("price_total_usd")
        or pricing.get("price_nightly_usd"),
        "fx_rate": pricing.get("fx_rate"),
        "fx_source": pricing.get("fx_source"),
        "fx_timestamp": pricing.get("fx_timestamp"),
        "source": pricing.get("source"),
    }

    review_samples = []
    for review in (reviews or [])[:50]:
        review_samples.append(
            {
                "rating": review.get("rating"),
                "date": review.get("date"),
                "text": _truncate(review.get("text") or "", 400),
            }
        )

    return {
        "listing": {
            "id": listing.get("id") or listing.get("listing_id"),
            "title": listing.get("title"),
            "property_type": listing.get("property_type"),
            "location": location_name,
            "lat": (location.get("details") or {}).get("lat") if isinstance(location, dict) else None,
            "lng": (location.get("details") or {}).get("lng") if isinstance(location, dict) else None,
            "description": _truncate(listing.get("description") or "", 1200),
            "house_rules": _truncate(listing.get("house_rules") or "", 600),
            "cancellation_policy": listing.get("cancellation_policy"),
            "host": listing.get("host"),
            "reviews_summary": listing.get("reviews_summary"),
            "amenities": _flatten_amenities(listing),
            "pricing": pricing_payload,
            "review_coverage": {
                "mode": listing.get("review_mode"),
                "captured_count": listing.get("reviews_captured_count"),
                "total_count": listing.get("reviews_total_count"),
            },
        },
        "reviews": review_samples,
    }


def build_photo_fit_input(
    listing: Dict[str, Any],
    *,
    max_images: Optional[int] = None,
    include_unlabeled: Optional[bool] = None,
    max_unlabeled: Optional[int] = None,
    image_detail: Optional[str] = None,
) -> Dict[str, Any]:
    summary_input = build_listing_summary_input(listing, [])
    photos = listing.get("photos") if isinstance(listing.get("photos"), list) else []
    selected = _select_photo_fit_photos(
        listing,
        max_images=max_images,
        include_unlabeled=include_unlabeled,
        max_unlabeled=max_unlabeled,
    )
    resolved_max_images = max(1, int(max_images or PHOTO_FIT_MAX_IMAGES or 10))
    resolved_include_unlabeled = (
        PHOTO_FIT_INCLUDE_UNLABELED if include_unlabeled is None else bool(include_unlabeled)
    )
    resolved_max_unlabeled = max(
        0,
        int(max_unlabeled if max_unlabeled is not None else PHOTO_FIT_MAX_UNLABELED),
    )
    resolved_detail = (image_detail or PHOTO_FIT_IMAGE_DETAIL or "low").strip().lower()
    if resolved_detail not in {"low", "high", "auto"}:
        resolved_detail = "low"
    return {
        "listing": summary_input.get("listing") or {},
        "photo_count": len(photos),
        "area_counts": _photo_area_counts(listing),
        "selected_photos": selected,
        "selection_policy": {
            "source": "representative_then_labeled_then_unlabeled_fallback",
            "max_images": resolved_max_images,
            "include_unlabeled": resolved_include_unlabeled,
            "max_unlabeled": resolved_max_unlabeled,
            "image_detail": resolved_detail,
        },
    }


def build_comparison_input(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
    memory_context: Optional[Dict[str, Any]] = None,
    photo_fit_by_listing: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    payload: List[Dict[str, Any]] = []
    for listing in listings:
        listing_id = listing.get("id") or listing.get("listing_id")
        review_samples = []
        for review in (reviews_by_listing.get(str(listing_id)) or [])[:25]:
            review_samples.append(
                {
                    "rating": review.get("rating"),
                    "date": review.get("date"),
                    "text": _truncate(review.get("text") or "", 300),
                }
            )
        payload.append(
            {
                "listing": {
                    **(build_listing_summary_input(listing, []).get("listing") or {}),
                    "label": listing.get("title") or listing.get("name") or str(listing_id or "Listing"),
                },
                "reviews": review_samples,
            }
        )
        photo_fit = (
            photo_fit_by_listing.get(str(listing_id))
            if isinstance(photo_fit_by_listing, dict)
            else None
        )
        if isinstance(photo_fit, dict):
            payload[-1]["photo_fit"] = {
                "visual_summary": photo_fit.get("visual_summary"),
                "visual_strengths": photo_fit.get("visual_strengths") or [],
                "visual_concerns": photo_fit.get("visual_concerns") or [],
                "area_observations": photo_fit.get("area_observations") or [],
                "photo_confidence": photo_fit.get("photo_confidence"),
                "analyzed_photo_count": photo_fit.get("analyzed_photo_count"),
                "coverage_note": photo_fit.get("coverage_note"),
            }
    output: Dict[str, Any] = {"listings": payload}
    if isinstance(memory_context, dict) and memory_context.get("enabled"):
        output["trip_memory_context"] = memory_context
    return output


def build_input_hash(input_data: Dict[str, Any]) -> str:
    payload = json.dumps(input_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summary_json_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "highlights": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "pros": {"type": "array", "items": {"type": "string"}},
            "cons": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "best_for": {"type": "array", "items": {"type": "string"}},
            "review_themes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "theme": {"type": "string"},
                        "sentiment": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["theme", "sentiment", "evidence"],
                },
            },
            "coverage_note": {"type": ["string", "null"]},
            "confidence": {"type": "string"},
        },
        "required": [
            "summary",
            "highlights",
            "constraints",
            "pros",
            "cons",
            "risks",
            "best_for",
            "review_themes",
            "coverage_note",
            "confidence",
        ],
    }


def _photo_fit_json_schema() -> Dict[str, Any]:
    area_observation_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "area": {"type": "string"},
            "observations": {"type": "array", "items": {"type": "string"}},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "concerns": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string"},
        },
        "required": ["area", "observations", "strengths", "concerns", "confidence"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "visual_summary": {"type": "string"},
            "visual_strengths": {"type": "array", "items": {"type": "string"}},
            "visual_concerns": {"type": "array", "items": {"type": "string"}},
            "area_observations": {"type": "array", "items": area_observation_schema},
            "photo_confidence": {"type": "string"},
            "analyzed_photo_count": {"type": "integer"},
            "coverage_note": {"type": ["string", "null"]},
        },
        "required": [
            "visual_summary",
            "visual_strengths",
            "visual_concerns",
            "area_observations",
            "photo_confidence",
            "analyzed_photo_count",
            "coverage_note",
        ],
    }


def _comparison_json_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "winner": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "listing_id": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["listing_id", "reason"],
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "section": {"type": "string"},
                        "winner_listing_id": {"type": ["string", "null"]},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["section", "winner_listing_id", "notes"],
                },
            },
            "listing_notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "listing_id": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "pros": {"type": "array", "items": {"type": "string"}},
                        "cons": {"type": "array", "items": {"type": "string"}},
                        "watchouts": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["listing_id", "title", "pros", "cons", "watchouts"],
                },
            },
            "visual_fit": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "listing_id": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "visual_strengths": {"type": "array", "items": {"type": "string"}},
                        "visual_concerns": {"type": "array", "items": {"type": "string"}},
                        "photo_based_confidence": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "listing_id",
                        "title",
                        "visual_strengths",
                        "visual_concerns",
                        "photo_based_confidence",
                        "evidence",
                    ],
                },
            },
            "personalized_fit": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "listing_id": {"type": "string"},
                        "title": {"type": ["string", "null"]},
                        "matches": {"type": "array", "items": {"type": "string"}},
                        "mismatches": {"type": "array", "items": {"type": "string"}},
                        "memory_basis": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["listing_id", "title", "matches", "mismatches", "memory_basis"],
                },
            },
            "memory_citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "citation_index": {"type": "integer"},
                        "title": {"type": ["string", "null"]},
                        "filename": {"type": ["string", "null"]},
                        "source_type": {"type": ["string", "null"]},
                        "memory_id": {"type": ["string", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["citation_index", "title", "filename", "source_type", "memory_id", "note"],
                },
            },
            "memory_context_note": {"type": ["string", "null"]},
            "tradeoffs": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string"},
        },
        "required": [
            "summary",
            "winner",
            "sections",
            "listing_notes",
            "visual_fit",
            "personalized_fit",
            "memory_citations",
            "memory_context_note",
            "tradeoffs",
            "confidence",
        ],
    }


def _extract_response_text(payload: Dict[str, Any]) -> str:
    output = payload.get("output") or []
    for item in output:
        for content in item.get("content", []) or []:
            if isinstance(content, dict):
                if "text" in content:
                    return content.get("text") or ""
                if "output_text" in content:
                    return content.get("output_text") or ""
    return payload.get("output_text") or ""


def _reasoning_payload(model: str) -> Optional[Dict[str, Any]]:
    if not DEFAULT_REASONING_EFFORT:
        return None
    effort = DEFAULT_REASONING_EFFORT
    if effort == "none" and not (model.startswith("gpt-5.1") or model.startswith("gpt-5.2")):
        return None
    return {"effort": effort}


def _parse_max_output_tokens() -> Optional[int]:
    if not DEFAULT_MAX_OUTPUT_TOKENS:
        return None
    try:
        value = int(DEFAULT_MAX_OUTPUT_TOKENS)
    except Exception:
        return None
    return value if value > 0 else None


def _call_openai_structured(
    model: str,
    system: str,
    user: str,
    schema: Dict[str, Any],
    *,
    schema_name: str = "listing_summary",
    user_content: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("RENTAL_LLM_API_KEY is not set")
    content = user_content or [{"type": "input_text", "text": user}]
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
    }
    reasoning = _reasoning_payload(model)
    if reasoning:
        payload["reasoning"] = reasoning
    max_output_tokens = _parse_max_output_tokens()
    if max_output_tokens:
        payload["max_output_tokens"] = max_output_tokens
    req = urllib.request.Request(
        f"{BASE_URL}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8") if exc.fp else ""
        raise RuntimeError(f"OpenAI API error {exc.code}: {details or exc.reason}") from exc
    data = json.loads(raw or "{}")
    content = _extract_response_text(data)
    if not content:
        raise RuntimeError("LLM returned empty content")
    try:
        return json.loads(content)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse LLM JSON: {exc}") from exc


def generate_listing_summary(
    listing: Dict[str, Any],
    reviews: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    model = model or DEFAULT_MODEL
    input_data = build_listing_summary_input(listing, reviews)
    system_prompt = (
        "You are a rental listing analyst. Return ONLY valid JSON that matches the provided schema. "
        "Be concise and factual. Do not invent details."
    )
    user_prompt = (
        "Summarize the listing and review data below. Identify key highlights, constraints, "
        "pros/cons, risks, and who this listing is best for. Extract recurring review themes. "
        "Use pricing when available (prefer USD fields if present) and clarify whether the price is total "
        "or nightly and for how many nights. "
        "If review coverage is partial, include a short coverage_note explaining how many reviews were used.\n\n"
        f"DATA:\n{json.dumps(input_data, ensure_ascii=False)}"
    )
    output = _call_openai_structured(model, system_prompt, user_prompt, _summary_json_schema())
    validated = ListingSummary.model_validate(output)
    return validated.model_dump()


def generate_listing_photo_fit(
    listing: Dict[str, Any],
    *,
    model: Optional[str] = None,
    max_images: Optional[int] = None,
    include_unlabeled: Optional[bool] = None,
    max_unlabeled: Optional[int] = None,
    image_detail: Optional[str] = None,
) -> Dict[str, Any]:
    model = model or DEFAULT_MODEL
    input_data = build_photo_fit_input(
        listing,
        max_images=max_images,
        include_unlabeled=include_unlabeled,
        max_unlabeled=max_unlabeled,
        image_detail=image_detail,
    )
    selected_photos = input_data.get("selected_photos") or []
    if not selected_photos:
        raise ValueError("No representative listing photos are available for analysis.")

    detail = (image_detail or PHOTO_FIT_IMAGE_DETAIL or "low").strip().lower()
    if detail not in {"low", "high", "auto"}:
        detail = "low"
    system_prompt = (
        "You are a rental listing visual analyst. Return ONLY valid JSON that matches the provided schema. "
        "Use only the supplied images and metadata. Do not infer amenities or conditions that are not visible. "
        "Clearly separate visual evidence from factual listing claims."
    )
    user_prompt = (
        "Analyze the representative listing photos for visual fit. Focus on visible room quality, group comfort, "
        "kitchen/dining usability, bedroom/sleeping plausibility, bathroom condition, outdoor value, views, workspace, "
        "and visible concerns. Treat captions and area labels as hints, not proof. If image coverage is limited, "
        "state that in coverage_note and lower confidence. Some selected photos may be marked source=unlabeled_fallback; "
        "for those, infer the likely area only when it is visually clear, otherwise describe the area as unknown.\n\n"
        f"LISTING_AND_PHOTO_METADATA:\n{json.dumps(input_data, ensure_ascii=False)}"
    )
    user_content: List[Dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
    for index, photo in enumerate(selected_photos, 1):
        label = {
            "index": index,
            "area": photo.get("area"),
            "caption": photo.get("caption"),
            "position": photo.get("position"),
            "source": photo.get("source"),
        }
        user_content.append({"type": "input_text", "text": f"Photo {index} metadata: {json.dumps(label, ensure_ascii=False)}"})
        image_part = {
            "type": "input_image",
            "image_url": photo.get("url"),
            "detail": detail,
        }
        user_content.append(image_part)

    output = _call_openai_structured(
        model,
        system_prompt,
        user_prompt,
        _photo_fit_json_schema(),
        schema_name="listing_photo_fit",
        user_content=user_content,
    )
    validated = ListingPhotoFit.model_validate(output)
    return validated.model_dump()


def generate_listing_comparison(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
    *,
    memory_context: Optional[Dict[str, Any]] = None,
    photo_fit_by_listing: Optional[Dict[str, Dict[str, Any]]] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    model = model or DEFAULT_MODEL
    input_data = build_comparison_input(
        listings,
        reviews_by_listing,
        memory_context=memory_context,
        photo_fit_by_listing=photo_fit_by_listing,
    )
    label_map = {
        str(item.get("listing", {}).get("id") or ""): item.get("listing", {}).get("label")
        for item in input_data.get("listings", [])
    }
    system_prompt = (
        "You are a rental listing analyst. Return ONLY valid JSON that matches the provided schema. "
        "Use only the provided listing data and optional trip memory context. If no clear winner, set winner.listing_id to null. "
        "Do not invent listing facts from memory; use memory only to assess personal fit."
    )
    user_prompt = (
        "Compare the listings below. Provide structured sections (e.g., Location, Value, Amenities, Reviews, "
        "Host/Check-in, Risks). Include an explicit section on amenity gaps and what each listing lacks "
        "relative to others. Name a winner and explain why. Be concise and factual.\n\n"
        "When pricing is available, include it in the Value section (prefer USD fields if present) and note "
        "whether the price is total or nightly and for how many nights. If pricing is missing, state that "
        "value comparisons are based on reviews and amenities only.\n\n"
        "When writing prose, refer to listings by their label (title). Do not use raw IDs in prose; "
        "only use IDs in winner.listing_id or section.winner_listing_id fields.\n\n"
        "If trip_memory_context.enabled is true and hits are present, add a Personalized fit assessment for each listing. "
        "Memory can indicate preferences, past dislikes, and travel style, but it must not override listing facts. "
        "Use memory_basis citation_index values for any memory-grounded personalized_fit point. "
        "Populate memory_citations from the provided trip_memory_context.citations that you used. "
        "Keep the Summary, Winner, Key tradeoffs, Sections, and Listing notes grounded in listing/review data. "
        "If no memory context is present, return empty personalized_fit and memory_citations arrays and set memory_context_note to null. "
        "If memory context is enabled but sparse or not useful, set memory_context_note to a short explanation.\n\n"
        "If any listing includes photo_fit, populate visual_fit with a separate photo-based assessment for those listings. "
        "Use photo_fit only as visual evidence. Do not claim non-visible amenities as facts, and keep visual concerns separate "
        "from review/listing concerns. If no photo_fit is present, return an empty visual_fit array.\n\n"
        f"LABELS: {json.dumps(label_map, ensure_ascii=False)}\n\nDATA:\n{json.dumps(input_data, ensure_ascii=False)}"
    )
    output = _call_openai_structured(
        model,
        system_prompt,
        user_prompt,
        _comparison_json_schema(),
        schema_name="listing_comparison",
    )
    validated = ListingComparison.model_validate(output)
    return validated.model_dump()


def build_summary_request(
    listing: Dict[str, Any], reviews: List[Dict[str, Any]], model: Optional[str] = None
) -> Tuple[str, str]:
    model = model or DEFAULT_MODEL
    input_data = build_listing_summary_input(listing, reviews)
    input_hash = build_input_hash(input_data)
    return model, input_hash


def build_photo_fit_request(
    listing: Dict[str, Any],
    model: Optional[str] = None,
    *,
    max_images: Optional[int] = None,
    include_unlabeled: Optional[bool] = None,
    max_unlabeled: Optional[int] = None,
    image_detail: Optional[str] = None,
) -> Tuple[str, str]:
    model = model or DEFAULT_MODEL
    input_data = build_photo_fit_input(
        listing,
        max_images=max_images,
        include_unlabeled=include_unlabeled,
        max_unlabeled=max_unlabeled,
        image_detail=image_detail,
    )
    input_hash = build_input_hash(input_data)
    return model, input_hash


def build_comparison_request(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
    memory_context: Optional[Dict[str, Any]] = None,
    photo_fit_by_listing: Optional[Dict[str, Dict[str, Any]]] = None,
    model: Optional[str] = None,
) -> Tuple[str, str]:
    model = model or DEFAULT_MODEL
    input_data = build_comparison_input(
        listings,
        reviews_by_listing,
        memory_context=memory_context,
        photo_fit_by_listing=photo_fit_by_listing,
    )
    input_hash = build_input_hash(input_data)
    return model, input_hash
