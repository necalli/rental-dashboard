import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .geo_suggest import suggest_locations


MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

AMENITY_KEYWORDS = {
    "hot tub": "hot tub",
    "jacuzzi": "hot tub",
    "wifi": "wifi",
    "wi-fi": "wifi",
    "kitchen": "kitchen",
    "parking": "free parking",
    "free parking": "free parking",
    "pool": "pool",
    "washer": "washer",
    "dryer": "dryer",
    "air conditioning": "air conditioning",
    "ac": "air conditioning",
    "fireplace": "fireplace",
    "workspace": "dedicated workspace",
    "dedicated workspace": "dedicated workspace",
    "waterfront": "waterfront",
    "lakefront": "waterfront",
    "beachfront": "beachfront",
    "ev charger": "ev charger",
    "crib": "crib",
    "gym": "gym",
}

ROOM_TYPES = {
    "entire place": "Entire home/apt",
    "entire home": "Entire home/apt",
    "entire apartment": "Entire home/apt",
    "entire cabin": "Entire home/apt",
    "private room": "Private room",
    "shared room": "Shared room",
}

UNSUPPORTED_HINTS = (
    "best",
    "compare",
    "ingest",
    "capture",
    "summarize",
    "summary",
    "analyze",
)


@dataclass
class SearchAssistResult:
    status: str
    intent: Dict[str, Any]
    message: str
    job: Optional[Dict[str, Any]] = None
    unsupported_or_uncertain_requests: Optional[List[str]] = None
    soft_preferences: Optional[List[str]] = None
    location_candidates: Optional[List[Dict[str, Any]]] = None
    clarification_question: Optional[str] = None
    confidence: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "status": self.status,
            "intent": self.intent,
            "message": self.message,
            "unsupported_or_uncertain_requests": self.unsupported_or_uncertain_requests or [],
            "soft_preferences": self.soft_preferences or [],
            "confidence": round(float(self.confidence), 3),
        }
        if self.job is not None:
            payload["job"] = self.job
        if self.location_candidates:
            payload["location_candidates"] = self.location_candidates
        if self.clarification_question:
            payload["clarification_question"] = self.clarification_question
        return payload


class SearchAssistService:
    def __init__(self, storage) -> None:
        self.storage = storage

    def assist(
        self,
        prompt: str,
        *,
        queue: bool = True,
        parsed_intent: Optional[Dict[str, Any]] = None,
        parsed_status: Optional[str] = None,
        parsed_message: Optional[str] = None,
        parsed_unsupported: Optional[List[str]] = None,
        parsed_soft_preferences: Optional[List[str]] = None,
        parsed_confidence: Optional[float] = None,
        location_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        text = str(prompt or "").strip()
        if isinstance(parsed_intent, dict):
            intent, unsupported, soft_preferences, confidence = self._normalize_external_intent(
                parsed_intent,
                parsed_unsupported or [],
                parsed_soft_preferences or [],
                parsed_confidence,
            )
        else:
            intent, unsupported, confidence = self.parse_intent(text)
            soft_preferences = []
            self._normalize_price_basis(intent, clean_notes=unsupported)
        status_hint = str(parsed_status or "").strip().lower()
        if status_hint == "rejected":
            return SearchAssistResult(
                status="rejected",
                intent=intent,
                message=parsed_message or "This search bar only runs rental listing searches.",
                unsupported_or_uncertain_requests=unsupported,
                soft_preferences=soft_preferences,
                confidence=min(confidence, 0.45),
            ).as_dict()
        if location_override:
            intent["location"] = str(location_override).strip()
            intent["location_resolution"] = {
                "source": "user_confirmation",
                "input": str(location_override).strip(),
                "resolved_label": str(location_override).strip(),
                "auto_selected": False,
            }
        else:
            location_resolution = self.resolve_location(intent.get("location"))
            if location_resolution.get("status") == "clarification_needed":
                return SearchAssistResult(
                    status="clarification_needed",
                    intent=intent,
                    message=location_resolution.get("message")
                    or "Please confirm which location you want to search.",
                    unsupported_or_uncertain_requests=unsupported,
                    soft_preferences=soft_preferences,
                    location_candidates=location_resolution.get("candidates") or [],
                    clarification_question=location_resolution.get("question"),
                    confidence=min(confidence, 0.7),
                ).as_dict()
            resolved_label = location_resolution.get("resolved_label")
            if resolved_label:
                intent["location"] = resolved_label
            if location_resolution:
                intent["location_resolution"] = location_resolution

        validation = self.validate_intent(text, intent)
        unsupported = list(dict.fromkeys([*unsupported, *validation.get("unsupported", [])]))
        if validation.get("error"):
            return SearchAssistResult(
                status="clarification_needed",
                intent=intent,
                message=parsed_message if status_hint == "clarification_needed" and parsed_message else validation["error"],
                unsupported_or_uncertain_requests=unsupported,
                soft_preferences=soft_preferences,
                clarification_question=parsed_message if status_hint == "clarification_needed" else None,
                confidence=min(confidence, 0.45),
            ).as_dict()
        if not queue:
            return SearchAssistResult(
                status="ready",
                intent=intent,
                message="Search intent parsed and ready to queue.",
                unsupported_or_uncertain_requests=unsupported,
                soft_preferences=soft_preferences,
                confidence=confidence,
            ).as_dict()
        if soft_preferences:
            intent["soft_preferences"] = soft_preferences
        job = self.storage.create_job("search", intent)
        return SearchAssistResult(
            status="queued",
            intent=intent,
            message=self._queued_message(intent),
            job=job,
            unsupported_or_uncertain_requests=unsupported,
            soft_preferences=soft_preferences,
            confidence=confidence,
        ).as_dict()

    def resolve_location(self, location: Any) -> Dict[str, Any]:
        raw = str(location or "").strip()
        if not raw:
            return {}
        candidates = self._location_suggestions(raw)
        if not candidates:
            return {
                "source": "geoapify",
                "input": raw,
                "resolved_label": raw,
                "auto_selected": False,
                "status": "unresolved",
            }
        auto = self._auto_select_location(raw, candidates)
        if auto:
            resolved_label = self._search_label_for_location(raw, auto)
            return {
                "source": "geoapify",
                "input": raw,
                "resolved_label": resolved_label,
                "selected_candidate": auto,
                "candidate_count": len(candidates),
                "auto_selected": True,
                "status": "resolved",
            }
        return {
            "source": "geoapify",
            "input": raw,
            "status": "clarification_needed",
            "message": f"Please confirm which {raw} you want to search.",
            "question": "Choose a location to run the search.",
            "candidates": candidates[:5],
            "candidate_count": len(candidates),
            "auto_selected": False,
        }

    def _location_suggestions(self, raw: str) -> List[Dict[str, Any]]:
        try:
            suggestions = suggest_locations(raw)
        except Exception:
            return []
        if not isinstance(suggestions, list):
            return []
        output = []
        for item in suggestions[:5]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            output.append(
                {
                    "label": label,
                    "city": item.get("city"),
                    "state": item.get("state"),
                    "country": item.get("country"),
                    "lat": item.get("lat"),
                    "lng": item.get("lng"),
                    "type": item.get("type"),
                }
            )
        return output

    def _auto_select_location(self, raw: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if len(candidates) == 1:
            return candidates[0]
        raw_norm = self._normalize_location_token(raw)
        if not raw_norm:
            return None
        exact = []
        for candidate in candidates:
            city = self._normalize_location_token(candidate.get("city"))
            label = self._normalize_location_token(str(candidate.get("label") or "").split(",", 1)[0])
            if raw_norm and raw_norm in {city, label}:
                exact.append(candidate)
        if len(exact) == 1:
            return exact[0]
        if "," in raw:
            return candidates[0]
        return None

    def _search_label_for_location(self, raw: str, candidate: Dict[str, Any]) -> str:
        label = str(candidate.get("label") or raw).strip()
        first = label.split(",", 1)[0].strip()
        state = str(candidate.get("state") or "").strip()
        country = str(candidate.get("country") or "").strip()
        raw_has_region = "," in str(raw or "")
        if first and state and not raw_has_region:
            if country.lower() in {"", "united states", "united states of america", "usa"}:
                return f"{first}, {state}"
        return label or str(raw or "").strip()

    def _normalize_location_token(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _normalize_external_intent(
        self,
        intent: Dict[str, Any],
        unsupported: List[str],
        soft_preferences: List[str],
        confidence: Optional[float],
    ) -> Tuple[Dict[str, Any], List[str], List[str], float]:
        normalized: Dict[str, Any] = {}
        allowed = {
            "location",
            "check_in",
            "check_out",
            "adults",
            "children",
            "infants",
            "pets",
            "min_price",
            "max_price",
            "min_price_nightly",
            "max_price_nightly",
            "min_price_total",
            "max_price_total",
            "price_basis",
            "price_basis_assumed",
            "room_type",
            "amenities",
            "flexible_cancellation",
            "min_bedrooms",
            "min_beds",
            "min_bathrooms",
        }
        for key, value in intent.items():
            if key not in allowed or value in (None, "", []):
                continue
            normalized[key] = value
        normalized.setdefault("adults", 1)
        normalized.setdefault("children", 0)
        normalized.setdefault("infants", 0)
        normalized.setdefault("pets", 0)
        if isinstance(normalized.get("amenities"), list):
            normalized["amenities"] = [
                str(item).strip() for item in normalized.get("amenities") or [] if str(item).strip()
            ][:20]
        self._normalize_price_basis(normalized, clean_notes=unsupported)
        clean_unsupported = [str(item).strip() for item in unsupported or [] if str(item).strip()]
        clean_soft = [str(item).strip() for item in soft_preferences or [] if str(item).strip()]
        clean_soft = list(dict.fromkeys(clean_soft))[:20]
        try:
            score = float(confidence if confidence is not None else 0.82)
        except Exception:
            score = 0.82
        return normalized, clean_unsupported, clean_soft, max(0.0, min(score, 0.98))

    def parse_intent(self, prompt: str) -> Tuple[Dict[str, Any], List[str], float]:
        text = str(prompt or "").strip()
        lowered = text.lower()
        intent: Dict[str, Any] = {}
        unsupported: List[str] = []
        confidence = 0.35

        location = self._extract_location(text)
        if location:
            intent["location"] = location
            confidence += 0.3

        check_in, check_out = self._extract_dates(text)
        if check_in:
            intent["check_in"] = check_in
            confidence += 0.1
        if check_out:
            intent["check_out"] = check_out
            confidence += 0.1

        counts = self._extract_guest_counts(lowered)
        intent.update(counts)
        if counts:
            confidence += 0.08
        intent.setdefault("adults", 1)
        intent.setdefault("children", 0)
        intent.setdefault("infants", 0)
        intent.setdefault("pets", 0)

        prices = self._extract_prices(lowered)
        intent.update(prices)
        if prices:
            confidence += 0.05

        room_type = self._extract_room_type(lowered)
        if room_type:
            intent["room_type"] = room_type

        bedrooms = self._extract_count_before(lowered, "bedroom")
        beds = self._extract_count_before(lowered, "bed")
        bathrooms = self._extract_count_before(lowered, "bath")
        if bedrooms:
            intent["min_bedrooms"] = bedrooms
        if beds:
            intent["min_beds"] = beds
        if bathrooms:
            intent["min_bathrooms"] = bathrooms

        amenities = self._extract_amenities(lowered)
        if amenities:
            intent["amenities"] = amenities
        if "flexible cancellation" in lowered or "free cancellation" in lowered:
            intent["flexible_cancellation"] = True

        if any(word in lowered for word in UNSUPPORTED_HINTS):
            unsupported.append(
                "This search bar only creates listing searches; comparison, analysis, and ingestion are handled elsewhere."
            )
        custom_requests = self._extract_unmapped_requests(lowered, amenities)
        unsupported.extend(custom_requests)

        return intent, unsupported, min(confidence, 0.95)

    def _normalize_price_basis(self, intent: Dict[str, Any], *, clean_notes: List[str]) -> None:
        check_in = intent.get("check_in")
        check_out = intent.get("check_out")
        nights = self._compute_nights(check_in, check_out)
        basis = str(intent.get("price_basis") or "").strip().lower()

        max_total = self._to_positive_int(intent.get("max_price_total"))
        min_total = self._to_positive_int(intent.get("min_price_total"))
        max_nightly = self._to_positive_int(intent.get("max_price_nightly"))
        min_nightly = self._to_positive_int(intent.get("min_price_nightly"))
        max_legacy = self._to_positive_int(intent.get("max_price"))
        min_legacy = self._to_positive_int(intent.get("min_price"))
        has_price_amount = any(
            value is not None
            for value in (max_total, min_total, max_nightly, min_nightly, max_legacy, min_legacy)
        )
        if basis == "nightly":
            if max_nightly is None and max_legacy is not None:
                max_nightly = max_legacy
                intent["max_price_nightly"] = max_nightly
            if min_nightly is None and min_legacy is not None:
                min_nightly = min_legacy
                intent["min_price_nightly"] = min_nightly
        elif basis == "total":
            if max_total is None and max_legacy is not None:
                max_total = max_legacy
                intent["max_price_total"] = max_total
            if min_total is None and min_legacy is not None:
                min_total = min_legacy
                intent["min_price_total"] = min_total

        if max_total is not None:
            intent["max_price"] = max_total
            basis = "total"
        elif max_nightly is not None:
            intent["max_price"] = max_nightly * nights
            basis = "nightly"

        if min_total is not None:
            intent["min_price"] = min_total
            basis = "total"
        elif min_nightly is not None:
            intent["min_price"] = min_nightly * nights
            basis = "nightly"

        if basis in {"nightly", "total"}:
            intent["price_basis"] = basis
        if max_nightly is not None or min_nightly is not None or max_total is not None or min_total is not None:
            intent["price_filter"] = {
                "basis": intent.get("price_basis") or basis or "unknown",
                "nights": nights,
                "input_min_nightly": min_nightly,
                "input_max_nightly": max_nightly,
                "input_min_total": min_total,
                "input_max_total": max_total,
                "derived_min_total": intent.get("min_price"),
                "derived_max_total": intent.get("max_price"),
            }
        if has_price_amount and str(intent.get("price_basis") or "").strip().lower() == "unknown":
            clean_notes.append("Price basis was unclear; confirm whether the amount is nightly or total if results look too broad or narrow.")

    def validate_intent(self, prompt: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        if not str(prompt or "").strip():
            return {"error": "Describe the listing search you want to run."}
        if not intent.get("location"):
            return {
                "error": "I need a destination before I can queue a listing search.",
                "unsupported": [],
            }
        check_in = intent.get("check_in")
        check_out = intent.get("check_out")
        if bool(check_in) != bool(check_out):
            return {"error": "Please provide both check-in and check-out dates."}
        if check_in and check_out and str(check_out) <= str(check_in):
            return {"error": "Check-out must be after check-in."}
        price_error = self._price_clarification_error(intent)
        if price_error:
            return {"error": price_error}
        return {"unsupported": []}

    def _price_clarification_error(self, intent: Dict[str, Any]) -> Optional[str]:
        basis = str(intent.get("price_basis") or "").strip().lower()
        assumed = str(intent.get("price_basis_assumed") or "").strip().lower()
        has_basis_specific_price = any(
            self._to_positive_int(intent.get(key)) is not None
            for key in ("min_price_nightly", "max_price_nightly", "min_price_total", "max_price_total")
        )
        has_legacy_price = any(
            self._to_positive_int(intent.get(key)) is not None
            for key in ("min_price", "max_price")
        )
        if not has_basis_specific_price and not has_legacy_price:
            return None
        if basis in {"nightly", "total"} and assumed != "nightly":
            return None
        return "Do you mean the price amount is per night or total for the stay?"

    def _queued_message(self, intent: Dict[str, Any]) -> str:
        parts = [str(intent.get("location") or "destination")]
        if intent.get("check_in") and intent.get("check_out"):
            parts.append(f"{intent['check_in']} to {intent['check_out']}")
        adults = intent.get("adults")
        if adults:
            parts.append(f"{adults} adult{'s' if int(adults) != 1 else ''}")
        if int(intent.get("children") or 0) > 0:
            parts.append(f"{intent['children']} children")
        if int(intent.get("pets") or 0) > 0:
            parts.append("pets")
        return f"AI search queued for {', '.join(parts)}."

    def _extract_location(self, prompt: str) -> Optional[str]:
        text = re.sub(r"\s+", " ", prompt).strip()
        patterns = [
            r"\b(?:in|near|around)\s+(.+?)(?:\s+(?:from|for|with|under|below|between|on|during|january|february|march|april|may|june|july|august|september|october|november|december)\b|$)",
            r"\b(?:search|find|look for|show me)\s+(?:.+?)\s+(?:in|near|around)\s+(.+?)(?:\s+(?:from|for|with|under|below|between|on|during)\b|$)",
            r"^(.+?)\s+(?:from\s+)?(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                location = self._clean_location(match.group(1))
                if location:
                    return location
        return None

    def _clean_location(self, value: str) -> str:
        text = str(value or "").strip(" ,.;")
        text = re.sub(r"\b(?:cabin|cabins|home|homes|house|houses|apartment|apartments|listing|listings|rental|rentals)\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ,.;")
        return text

    def _extract_dates(self, prompt: str) -> Tuple[Optional[str], Optional[str]]:
        iso = re.findall(r"\b(20[0-9]{2}-[01][0-9]-[0-3][0-9])\b", prompt)
        if len(iso) >= 2:
            return iso[0], iso[1]

        month_names = "|".join(MONTHS.keys())
        range_re = re.compile(
            rf"\b({month_names})\.?\s+([0-9]{{1,2}})\s*(?:-|to|through|until)\s*(?:(?:({month_names})\.?\s+)?)?([0-9]{{1,2}})(?:,\s*(20[0-9]{{2}}))?",
            re.IGNORECASE,
        )
        match = range_re.search(prompt)
        if not match:
            return None, None
        start_month = MONTHS[match.group(1).lower().rstrip(".")]
        start_day = int(match.group(2))
        end_month = MONTHS[(match.group(3) or match.group(1)).lower().rstrip(".")]
        end_day = int(match.group(4))
        year = int(match.group(5) or time.strftime("%Y"))
        if (end_month, end_day) < (start_month, start_day):
            end_year = year + 1
        else:
            end_year = year
        return (
            f"{year:04d}-{start_month:02d}-{start_day:02d}",
            f"{end_year:04d}-{end_month:02d}-{end_day:02d}",
        )

    def _extract_guest_counts(self, lowered: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for key, aliases in {
            "adults": ("adult", "adults", "guest", "guests", "people"),
            "children": ("child", "children", "kid", "kids"),
            "infants": ("infant", "infants", "baby", "babies"),
        }.items():
            for alias in aliases:
                match = re.search(rf"\b([0-9]{{1,2}})\s+{alias}\b", lowered)
                if match:
                    out[key] = max(0, min(int(match.group(1)), 16 if key == "adults" else 10))
                    break
        if re.search(r"\b(?:dog|dogs|pet|pets|pet-friendly|dog-friendly)\b", lowered):
            pet_match = re.search(r"\b([0-9]{1,2})\s+(?:dogs?|pets?)\b", lowered)
            out["pets"] = max(1, min(int(pet_match.group(1)), 10)) if pet_match else 1
        return out

    def _extract_prices(self, lowered: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        under = re.search(
            r"\b(?:under|below|less than|max(?:imum)?(?:\s+price)?(?:\s+is)?)\s*\$?([0-9][0-9,]*)",
            lowered,
        )
        if under:
            value = int(under.group(1).replace(",", ""))
            if self._mentions_total_price(lowered):
                out["max_price_total"] = value
                out["price_basis"] = "total"
            elif self._mentions_nightly_price(lowered):
                out["max_price_nightly"] = value
                out["price_basis"] = "nightly"
            else:
                out["max_price"] = value
                out["price_basis"] = "unknown"
        over = re.search(
            r"\b(?:over|above|min(?:imum)?(?:\s+price)?(?:\s+is)?)\s*\$?([0-9][0-9,]*)",
            lowered,
        )
        if over:
            value = int(over.group(1).replace(",", ""))
            if self._mentions_total_price(lowered):
                out["min_price_total"] = value
                out["price_basis"] = "total"
            elif self._mentions_nightly_price(lowered):
                out["min_price_nightly"] = value
                out["price_basis"] = "nightly"
            else:
                out["min_price"] = value
                out["price_basis"] = "unknown"
        between = re.search(r"\bbetween\s*\$?([0-9][0-9,]*)\s*(?:and|-|to)\s*\$?([0-9][0-9,]*)", lowered)
        if between:
            first = int(between.group(1).replace(",", ""))
            second = int(between.group(2).replace(",", ""))
            if self._mentions_total_price(lowered):
                out["min_price_total"] = min(first, second)
                out["max_price_total"] = max(first, second)
                out["price_basis"] = "total"
            elif self._mentions_nightly_price(lowered):
                out["min_price_nightly"] = min(first, second)
                out["max_price_nightly"] = max(first, second)
                out["price_basis"] = "nightly"
            else:
                out["min_price"] = min(first, second)
                out["max_price"] = max(first, second)
                out["price_basis"] = "unknown"
        return out

    def _mentions_nightly_price(self, lowered: str) -> bool:
        return bool(re.search(r"\b(per\s+night|nightly|/night|a\s+night|per\s+nite)\b", lowered))

    def _mentions_total_price(self, lowered: str) -> bool:
        return bool(re.search(r"\b(total|all[-\s]?in|for\s+the\s+(?:stay|week|trip)|whole\s+(?:stay|week|trip))\b", lowered))

    def _compute_nights(self, check_in: Any, check_out: Any) -> int:
        try:
            start = time.strptime(str(check_in), "%Y-%m-%d")
            end = time.strptime(str(check_out), "%Y-%m-%d")
            nights = int((time.mktime(end) - time.mktime(start)) / 86400)
            return max(1, nights)
        except Exception:
            return 1

    def _to_positive_int(self, value: Any) -> Optional[int]:
        try:
            if value in (None, "", []):
                return None
            parsed = int(float(value))
            return parsed if parsed > 0 else None
        except Exception:
            return None

    def _extract_room_type(self, lowered: str) -> Optional[str]:
        for needle, value in ROOM_TYPES.items():
            if needle in lowered:
                return value
        return None

    def _extract_count_before(self, lowered: str, noun: str) -> Optional[int]:
        match = re.search(rf"\b([0-9]{{1,2}})\s+{noun}s?\b", lowered)
        if not match:
            return None
        return max(1, min(int(match.group(1)), 20))

    def _extract_amenities(self, lowered: str) -> List[str]:
        amenities: List[str] = []
        for needle, value in AMENITY_KEYWORDS.items():
            if re.search(rf"\b{re.escape(needle)}\b", lowered):
                amenities.append(value)
        return sorted(dict.fromkeys(amenities))

    def _extract_unmapped_requests(self, lowered: str, amenities: List[str]) -> List[str]:
        hints = []
        for phrase in ("mountain view", "secluded", "walkable", "ski in", "ski out", "water view"):
            if phrase in lowered and phrase not in amenities:
                hints.append(f"'{phrase}' may not map to a supported Airbnb search filter and may require manual review.")
        return hints
