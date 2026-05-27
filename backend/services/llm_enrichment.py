import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


PROMPT_VERSION = "listing_summary_v1"
COMPARISON_PROMPT_VERSION = "listing_comparison_v2"
DEFAULT_MODEL = os.getenv("RENTAL_LLM_MODEL", "gpt-5-mini")
DEFAULT_TIMEOUT = int(os.getenv("RENTAL_LLM_TIMEOUT", "60"))
DEFAULT_REASONING_EFFORT = os.getenv("RENTAL_LLM_REASONING_EFFORT", "").strip().lower()
DEFAULT_MAX_OUTPUT_TOKENS = os.getenv("RENTAL_LLM_MAX_OUTPUT_TOKENS", "").strip()
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


class ListingComparison(BaseModel):
    summary: str
    winner: ComparisonWinner
    sections: List[ComparisonSection] = []
    listing_notes: List[ComparisonListingNotes] = []
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


def build_comparison_input(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
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
    return {"listings": payload}


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
            "tradeoffs": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string"},
        },
        "required": [
            "summary",
            "winner",
            "sections",
            "listing_notes",
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


def _call_openai_structured(model: str, system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("RENTAL_LLM_API_KEY is not set")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [{"type": "input_text", "text": user}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "listing_summary",
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


def generate_listing_comparison(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    model = model or DEFAULT_MODEL
    input_data = build_comparison_input(listings, reviews_by_listing)
    label_map = {
        str(item.get("listing", {}).get("id") or ""): item.get("listing", {}).get("label")
        for item in input_data.get("listings", [])
    }
    system_prompt = (
        "You are a rental listing analyst. Return ONLY valid JSON that matches the provided schema. "
        "Use only the provided listing data. If no clear winner, set winner.listing_id to null."
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
        f"LABELS: {json.dumps(label_map, ensure_ascii=False)}\n\nDATA:\n{json.dumps(input_data, ensure_ascii=False)}"
    )
    output = _call_openai_structured(model, system_prompt, user_prompt, _comparison_json_schema())
    validated = ListingComparison.model_validate(output)
    return validated.model_dump()


def build_summary_request(
    listing: Dict[str, Any], reviews: List[Dict[str, Any]], model: Optional[str] = None
) -> Tuple[str, str]:
    model = model or DEFAULT_MODEL
    input_data = build_listing_summary_input(listing, reviews)
    input_hash = build_input_hash(input_data)
    return model, input_hash


def build_comparison_request(
    listings: List[Dict[str, Any]],
    reviews_by_listing: Dict[str, List[Dict[str, Any]]],
    model: Optional[str] = None,
) -> Tuple[str, str]:
    model = model or DEFAULT_MODEL
    input_data = build_comparison_input(listings, reviews_by_listing)
    input_hash = build_input_hash(input_data)
    return model, input_hash
