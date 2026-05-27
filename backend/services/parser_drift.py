import hashlib
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse


def build_response_signature(
    responses: List[Dict[str, Any]],
    *,
    mode: str,
) -> Dict[str, Any]:
    operation_names: set[str] = set()
    endpoint_tokens: set[str] = set()
    signals: Dict[str, bool] = {
        "has_stay_product_detail": False,
        "has_reviews_path": False,
        "has_stays_search": False,
        "has_search_results": False,
    }

    for response in responses or []:
        url = str(response.get("url") or "")
        operation = _extract_operation_name(url)
        if operation:
            operation_names.add(operation)
        endpoint = _extract_endpoint_token(url)
        if endpoint:
            endpoint_tokens.add(endpoint)

        data = response.get("data")
        if not isinstance(data, dict):
            continue
        if _path_exists(data, ("data", "presentation", "stayProductDetailPage")):
            signals["has_stay_product_detail"] = True
        if _path_exists(data, ("data", "presentation", "stayProductDetailPage", "reviews", "reviews")):
            signals["has_reviews_path"] = True
        if _path_exists(data, ("data", "staysSearch")) or _path_exists(data, ("data", "presentation", "staysSearch")):
            signals["has_stays_search"] = True
        if (
            _path_exists(data, ("data", "staysSearch", "results", "searchResults"))
            or _path_exists(data, ("data", "staysSearch", "searchResults"))
            or _path_exists(data, ("data", "presentation", "staysSearch", "results", "searchResults"))
        ):
            signals["has_search_results"] = True

    tokens: List[str] = [f"mode:{(mode or '').strip().lower() or 'unknown'}"]
    tokens.extend(f"op:{name}" for name in sorted(operation_names)[:12])
    tokens.extend(f"ep:{name}" for name in sorted(endpoint_tokens)[:12])
    tokens.extend(
        f"sig:{key}"
        for key, present in sorted(signals.items())
        if bool(present)
    )
    digest = hashlib.sha1("|".join(tokens).encode("utf-8")).hexdigest()
    return {
        "hash": digest,
        "token_count": len(tokens),
        "operation_names": sorted(operation_names)[:8],
        "endpoint_tokens": sorted(endpoint_tokens)[:8],
        "signals": signals,
    }


def build_parser_meta(
    *,
    parser_version: str,
    signature: Dict[str, Any],
    warnings: Optional[Iterable[str]] = None,
    fallbacks: Optional[Dict[str, Any]] = None,
    signals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    warning_list = [str(item).strip() for item in (warnings or []) if str(item).strip()]
    # Preserve warning order while deduplicating.
    warning_list = list(dict.fromkeys(warning_list))
    return {
        "parser_version": str(parser_version or "").strip() or "unknown",
        "schema_signature": signature or {},
        "drift_detected": bool(warning_list),
        "warnings": warning_list,
        "fallbacks": fallbacks or {},
        "signals": signals or {},
    }


def compact_parser_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    signature = meta.get("schema_signature") if isinstance(meta.get("schema_signature"), dict) else {}
    return {
        "parser_version": meta.get("parser_version"),
        "drift_detected": bool(meta.get("drift_detected")),
        "warnings": list(meta.get("warnings") or []),
        "signature_hash": signature.get("hash"),
        "fallbacks": meta.get("fallbacks") or {},
        "signals": meta.get("signals") or {},
    }


def _extract_operation_name(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        query = parse_qs(urlparse(url).query or "")
    except Exception:
        return None
    op = (query.get("operationName") or [None])[0]
    if op:
        return str(op)
    return None


def _extract_endpoint_token(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        path = (urlparse(url).path or "").strip("/")
    except Exception:
        return None
    if not path:
        return None
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    # Use only the trailing endpoint shape token to avoid overfitting on full paths.
    tail = parts[-1].lower()
    if tail in {"graphql", "search", "stays", "pdp", "api", "v2", "v3"}:
        return tail
    if len(parts) >= 2:
        return f"{parts[-2].lower()}:{tail}"
    return tail


def _path_exists(data: Dict[str, Any], path: Sequence[str]) -> bool:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True
