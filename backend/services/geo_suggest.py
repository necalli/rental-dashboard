import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Tuple

DEFAULT_BASE_URL = "https://api.geoapify.com/v1/geocode/autocomplete"
DEFAULT_LIMIT = 6
DEFAULT_LANG = "en"
DEFAULT_TIMEOUT = 8
DEFAULT_CACHE_SECONDS = 86400

_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def _get_env_int(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, str(fallback)))
    except Exception:
        return fallback


def _get_env_str(name: str, fallback: str) -> str:
    return os.getenv(name, fallback) or fallback


def suggest_locations(query: str) -> List[Dict[str, Any]]:
    text = (query or "").strip()
    if len(text) < 3:
        return []

    cache_seconds = _get_env_int("RENTAL_GEOAPIFY_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)
    cache_key = text.lower()
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < cache_seconds:
        return cached[1]

    api_key = os.getenv("RENTAL_GEOAPIFY_API_KEY")
    if not api_key:
        return []

    base_url = _get_env_str("RENTAL_GEOAPIFY_BASE_URL", DEFAULT_BASE_URL)
    limit = _get_env_int("RENTAL_GEOAPIFY_LIMIT", DEFAULT_LIMIT)
    lang = _get_env_str("RENTAL_GEOAPIFY_LANG", DEFAULT_LANG)
    country_codes = os.getenv("RENTAL_GEOAPIFY_COUNTRY_CODES")
    timeout = _get_env_int("RENTAL_GEOAPIFY_TIMEOUT", DEFAULT_TIMEOUT)

    params = {
        "text": text,
        "format": "json",
        "limit": max(1, min(limit, 20)),
        "lang": lang,
        "apiKey": api_key,
    }
    if country_codes:
        params["filter"] = f"countrycode:{country_codes}"

    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception:
        return []

    results = []
    entries = payload.get("results")
    if entries is None:
        entries = []
        for item in payload.get("features", []):
            props = item.get("properties") or {}
            entries.append(props)
    for props in entries:
        label = props.get("formatted") or props.get("name")
        if not label:
            continue
        results.append(
            {
                "label": label,
                "lat": props.get("lat"),
                "lng": props.get("lon"),
                "city": props.get("city"),
                "state": props.get("state"),
                "country": props.get("country"),
                "place_id": props.get("place_id"),
                "type": props.get("result_type"),
            }
        )

    _CACHE[cache_key] = (time.time(), results)
    return results
