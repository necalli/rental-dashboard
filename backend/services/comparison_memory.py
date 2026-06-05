import re
from typing import Any, Dict, List, Optional


def _clean_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _flatten_amenities(listing: Dict[str, Any], limit: int = 12) -> List[str]:
    output: List[str] = []
    for group in listing.get("amenities") or []:
        if not isinstance(group, dict):
            continue
        group_name = _clean_text(group.get("group") or group.get("name"), 60)
        for item in group.get("items") or []:
            label = item
            if isinstance(item, dict):
                label = item.get("name") or item.get("title") or item.get("label") or item.get("text")
            label_text = _clean_text(label, 80)
            if label_text:
                output.append(f"{group_name}: {label_text}" if group_name else label_text)
            if len(output) >= limit:
                return output
    return output


def build_comparison_memory_query(
    listings: List[Dict[str, Any]],
    *,
    focus: Optional[str] = None,
) -> str:
    parts: List[str] = []
    focus_text = _clean_text(focus, 500)
    if focus_text:
        parts.append(f"Personalization focus: {focus_text}")
    for listing in listings[:6]:
        location = listing.get("location")
        if isinstance(location, dict):
            location = location.get("name")
        listing_parts = [
            _clean_text(listing.get("title"), 120),
            _clean_text(listing.get("property_type"), 80),
            _clean_text(location, 80),
            _clean_text(listing.get("description"), 260),
        ]
        amenities = _flatten_amenities(listing)
        if amenities:
            listing_parts.append("Amenities: " + ", ".join(amenities))
        joined = " | ".join([item for item in listing_parts if item])
        if joined:
            parts.append(joined)
    if not parts:
        parts.append("Rental listing comparison for future trip planning preferences.")
    return "\n".join(parts)


def compact_memory_context(rag_result: Optional[Dict[str, Any]], *, limit: int = 6) -> Dict[str, Any]:
    if not isinstance(rag_result, dict):
        return {"enabled": False, "hits": [], "citations": [], "profile": {"top_tags": []}}
    hits = []
    citations = []
    for index, hit in enumerate((rag_result.get("hits") or [])[: max(1, int(limit or 6))], start=1):
        citation = hit.get("citation") if isinstance(hit.get("citation"), dict) else {}
        compact_citation = {
            "citation_index": index,
            "memory_id": citation.get("memory_id"),
            "title": citation.get("title"),
            "filename": citation.get("filename"),
            "source_type": citation.get("source_type"),
            "tags": citation.get("tags") or [],
            "created_at": citation.get("created_at"),
        }
        hits.append(
            {
                "citation_index": index,
                "score": hit.get("score"),
                "text": _clean_text(hit.get("text"), 900),
                "citation": compact_citation,
            }
        )
        citations.append(compact_citation)
    return {
        "enabled": True,
        "user_id": rag_result.get("user_id"),
        "query": rag_result.get("query"),
        "tags_applied": rag_result.get("tags_applied") or [],
        "tag_filter_relaxed": bool(rag_result.get("tag_filter_relaxed")),
        "hits": hits,
        "citations": citations,
        "profile": rag_result.get("profile") or {"top_tags": []},
    }
