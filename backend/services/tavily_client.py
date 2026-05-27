import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)


class TavilyClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.api_key = str(api_key or os.getenv("RENTAL_TAVILY_API_KEY") or os.getenv("TAVILY_API_KEY") or "").strip()
        self.base_url = str(
            base_url or os.getenv("RENTAL_TAVILY_BASE_URL") or "https://api.tavily.com/search"
        ).strip()
        self.timeout_seconds = max(
            3,
            int(timeout_seconds or _to_int(os.getenv("RENTAL_TAVILY_TIMEOUT_SECONDS"), 15)),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        search_depth: str = "basic",
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        text = str(query or "").strip()
        if not text:
            return {"results": [], "warning": "empty_query", "error": "empty_query"}
        if not self.enabled:
            return {
                "results": [],
                "warning": "tavily_api_key_missing",
                "error": "Tavily API key missing",
            }

        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": text,
            "search_depth": str(search_depth or "basic"),
            "max_results": max(1, min(int(max_results or 10), 20)),
            "include_answer": False,
        }
        if include_domains:
            payload["include_domains"] = [str(item).strip() for item in include_domains if str(item).strip()]
        if exclude_domains:
            payload["exclude_domains"] = [str(item).strip() for item in exclude_domains if str(item).strip()]

        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=float(self.timeout_seconds)) as res:
                raw = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            return {
                "results": [],
                "warning": "tavily_http_error",
                "error": f"http_{exc.code}",
                "details": body[:400] if body else "",
            }
        except urllib.error.URLError as exc:
            return {
                "results": [],
                "warning": "tavily_connection_error",
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "results": [],
                "warning": "tavily_runtime_error",
                "error": str(exc),
            }

        results = raw.get("results") if isinstance(raw.get("results"), list) else []
        return {"results": results, "warning": None, "error": None}
