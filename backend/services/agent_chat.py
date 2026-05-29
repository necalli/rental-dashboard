import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .storage import Storage
from .tavily_client import TavilyClient
from .personality_rag import PersonalityRagService

logger = logging.getLogger(__name__)


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _avg(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coerce_ratio(value: Any, default: float) -> float:
    parsed = _to_float(value)
    if parsed is None:
        parsed = float(default)
    return max(0.0, min(float(parsed), 1.0))


def _parse_tool_timeout_overrides(raw: Optional[str]) -> Dict[str, int]:
    if not raw:
        return {}
    output: Dict[str, int] = {}
    for part in str(raw).split(","):
        entry = str(part or "").strip()
        if not entry or ":" not in entry:
            continue
        key, value = entry.split(":", 1)
        name = str(key or "").strip()
        budget = _to_int(value, 0)
        if not name or budget <= 0:
            continue
        output[name] = budget
    return output


def _normalize_location(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if text else "n/a"
    if isinstance(value, dict):
        details = value.get("details") if isinstance(value.get("details"), dict) else {}
        coordinate = value.get("coordinate") if isinstance(value.get("coordinate"), dict) else {}
        candidates = [
            details.get("subtitle"),
            details.get("title"),
            value.get("name"),
            value.get("title"),
            value.get("label"),
            value.get("city"),
            coordinate.get("city"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return "n/a"


LISTING_URL_RE = re.compile(r"/rooms/([0-9]{6,})")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
RATING_RE = re.compile(r"\b([0-5](?:\.[0-9])?)\s*(?:/ ?5|stars?)\b", re.IGNORECASE)
RATING_COUNT_RE = re.compile(
    r"\b([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s+(?:reviews?|ratings?)\b",
    re.IGNORECASE,
)
PRICE_HINT_RE = re.compile(r"(\$[0-9][0-9,]*(?:\.[0-9]{2})?)")

RUN_PARAM_ALIASES: Dict[str, Tuple[str, ...]] = {
    "check_in": ("check_in", "checkin"),
    "check_out": ("check_out", "checkout"),
    "adults": ("adults",),
    "children": ("children",),
    "infants": ("infants",),
    "pets": ("pets",),
    "currency": ("currency", "pricing_currency"),
}


def _run_param_value(run_params: Dict[str, Any], key: str) -> Any:
    aliases = RUN_PARAM_ALIASES.get(str(key), (str(key),))
    for alias in aliases:
        if alias not in run_params:
            continue
        value = run_params.get(alias)
        if value is None or value == "":
            continue
        return value
    return None


class AgentChatOrchestrator:
    """
    MVP chat orchestrator.

    Current behavior:
    - Lightweight intent routing
    - Production tool wrappers using existing storage APIs
    - Session memory for short conversational continuity
    """

    def __init__(
        self,
        storage: Storage,
        max_history_messages: int = 20,
        *,
        tool_timeout_ms_default: Optional[int] = None,
        tool_timeout_overrides: Optional[Dict[str, int]] = None,
        tool_max_workers: Optional[int] = None,
        tavily_client: Optional[TavilyClient] = None,
        personality_rag: Optional[PersonalityRagService] = None,
    ) -> None:
        self.storage = storage
        self.tavily_client = tavily_client or TavilyClient()
        self.personality_rag = personality_rag or PersonalityRagService(storage)
        self.max_history_messages = max(2, int(max_history_messages or 20))
        env_default_timeout = _to_int(os.getenv("RENTAL_AGENT_TOOL_TIMEOUT_MS_DEFAULT"), 1500)
        self.tool_timeout_ms_default = max(100, int(tool_timeout_ms_default or env_default_timeout))
        env_overrides = _parse_tool_timeout_overrides(
            os.getenv("RENTAL_AGENT_TOOL_TIMEOUT_MS_OVERRIDES")
        )
        merged_overrides = dict(env_overrides)
        for key, value in (tool_timeout_overrides or {}).items():
            if not key:
                continue
            parsed = _to_int(value, 0)
            if parsed > 0:
                merged_overrides[str(key)] = parsed
        self.tool_timeout_overrides = merged_overrides
        env_tool_workers = _to_int(os.getenv("RENTAL_AGENT_TOOL_MAX_WORKERS"), 4)
        self.tool_max_workers = max(1, int(tool_max_workers or env_tool_workers))
        self._tool_executor = ThreadPoolExecutor(
            max_workers=self.tool_max_workers,
            thread_name_prefix="agent-tool",
        )
        self._sessions: Dict[str, List[Dict[str, Any]]] = {}

    def chat(self, *, session_id: Optional[str], message: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")

        sid = str(session_id or "").strip() or str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        started = time.monotonic()

        history = self._sessions.get(sid) or []
        history.append({"role": "user", "content": text, "ts": int(time.time())})
        history = history[-self.max_history_messages :]

        route = self._route_intent(text)
        intent = route.get("intent") or "general"
        entities = route.get("entities") if isinstance(route.get("entities"), dict) else {}
        tool_calls: List[Dict[str, Any]] = []
        warnings: List[str] = []
        citations: List[str] = []

        if intent == "pipeline_health":
            tool_args = {"limit": 60, "summary_limit": 240}
            tool_result = self._invoke_tool(
                tool_calls,
                warnings,
                "tool.metrics_jobs",
                tool_args,
                lambda: self._tool_metrics_jobs(**tool_args),
            )
            if tool_result is None:
                reply = "I couldn't load pipeline metrics right now. Please try again shortly."
            else:
                reply = self._render_pipeline_health_reply(tool_result)
                citations.append("/api/v1/metrics/jobs")
        elif intent == "jobs_list":
            tool_args = {"limit": 10}
            jobs = self._invoke_tool(
                tool_calls,
                warnings,
                "tool.jobs_list",
                tool_args,
                lambda: self._tool_jobs_list(**tool_args),
            )
            if jobs is None:
                reply = "I couldn't load jobs right now."
            else:
                reply = self._render_jobs_reply(jobs)
                citations.append("/api/v1/jobs")
        elif intent == "job_status":
            job_id = str(entities.get("job_id") or "").strip()
            if not job_id:
                reply = "Please provide a job id (uuid) so I can check status."
                warnings.append("job_id_missing")
            else:
                tool_args = {"job_id": job_id}
                job = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.job_get",
                    tool_args,
                    lambda: self._tool_job_get(**tool_args),
                )
                if not job:
                    reply = f"No job found for `{job_id}`."
                else:
                    reply = self._render_job_status_reply(job)
                    citations.append(f"/api/v1/jobs/{job_id}")
        elif intent == "search_runs_list":
            tool_args = {"limit": 8}
            runs = self._invoke_tool(
                tool_calls,
                warnings,
                "tool.search_runs_list",
                tool_args,
                lambda: self._tool_search_runs_list(**tool_args),
            )
            if runs is None:
                reply = "I couldn't load search runs right now."
            else:
                reply = self._render_search_runs_reply(runs)
                citations.append("/api/v1/search/runs")
        elif intent == "search_run_get":
            run_id = str(entities.get("run_id") or "").strip()
            if not run_id:
                reply = "Please provide a run id (uuid) to inspect."
                warnings.append("run_id_missing")
            else:
                tool_args = {"run_id": run_id}
                run = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.search_run_get",
                    tool_args,
                    lambda: self._tool_search_run_get(**tool_args),
                )
                if not run:
                    reply = f"No search run found for `{run_id}`."
                else:
                    reply = self._render_search_run_detail_reply(run)
                    citations.append(f"/api/v1/search/runs/{run_id}")
        elif intent == "search_listings_list":
            run_id = str(entities.get("run_id") or "").strip()
            if not run_id:
                reply = "Please provide a run id (uuid) so I can list its search results."
                warnings.append("run_id_missing")
            else:
                tool_args = {"run_id": run_id, "limit": 10}
                listings = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.search_listings_list",
                    tool_args,
                    lambda: self._tool_search_listings_list(**tool_args),
                )
                if listings is None:
                    reply = "I couldn't load search listings right now."
                else:
                    reply = self._render_search_listings_reply(run_id, listings)
                    citations.append(f"/api/v1/search/listings?run_id={run_id}&limit=10")
        elif intent == "listings_list":
            tool_args = {"limit": 8}
            listings = self._invoke_tool(
                tool_calls,
                warnings,
                "tool.listings_list",
                tool_args,
                lambda: self._tool_listings_list(**tool_args),
            )
            if listings is None:
                reply = "I couldn't load ingested listings right now."
            else:
                reply = self._render_listings_reply(listings)
                citations.append("/api/v1/listings?limit=8")
        elif intent == "listing_get":
            listing_id = str(entities.get("listing_id") or "").strip()
            if not listing_id:
                reply = "Please provide a listing id or Airbnb room URL."
                warnings.append("listing_id_missing")
            else:
                tool_args = {"listing_id": listing_id}
                listing = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.listing_get",
                    tool_args,
                    lambda: self._tool_listing_get(**tool_args),
                )
                if not listing:
                    reply = f"No ingested listing found for `{listing_id}`."
                else:
                    reply = self._render_listing_detail_reply(listing)
                    citations.append(f"/api/v1/listings/{listing_id}")
        elif intent == "reviews_list":
            listing_id = str(entities.get("listing_id") or "").strip()
            if not listing_id:
                reply = "Please provide a listing id or Airbnb room URL."
                warnings.append("listing_id_missing")
            else:
                tool_args = {"listing_id": listing_id, "limit": 8}
                reviews = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.reviews_list",
                    tool_args,
                    lambda: self._tool_reviews_list(**tool_args),
                )
                if reviews is None:
                    reply = "I couldn't load reviews right now."
                else:
                    reply = self._render_reviews_reply(listing_id, reviews)
                    citations.append(f"/api/v1/reviews?listing_id={listing_id}&limit=8")
        elif intent == "listing_summary_get":
            listing_id = str(entities.get("listing_id") or "").strip()
            if not listing_id:
                reply = "Please provide a listing id or Airbnb room URL."
                warnings.append("listing_id_missing")
            else:
                tool_args = {"listing_id": listing_id}
                summary = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.listing_summary_get",
                    tool_args,
                    lambda: self._tool_listing_summary_get(**tool_args),
                )
                if not summary:
                    reply = (
                        f"I don't see a stored summary for `{listing_id}` yet. "
                        "Run a listing summary generation first."
                    )
                else:
                    reply = self._render_listing_summary_reply(listing_id, summary)
                    citations.append(f"/api/v1/enrich/listings/{listing_id}/summary")
        elif intent == "trip_research":
            location = str(entities.get("location") or "").strip()
            if not location:
                reply = (
                    "Please provide a location for trip research. "
                    "Example: `find top things to do in Woodstock, NY on Tripadvisor`."
                )
                warnings.append("trip_research_location_missing")
            else:
                focus = self._infer_trip_focus(text)
                max_results = self._parse_int_range(
                    entities.get("max_results"),
                    minimum=3,
                    maximum=20,
                    fallback=8,
                )
                tool_args = {
                    "location": location,
                    "max_results": max_results,
                    "focus": focus,
                }
                result = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.trip_research_tavily",
                    tool_args,
                    lambda: self._tool_trip_research_tavily(**tool_args),
                )
                if not isinstance(result, dict):
                    reply = "I couldn't complete trip research right now."
                else:
                    reply = self._render_trip_research_reply(location, result)
                    citations.append("tavily:tripadvisor.com")
        elif intent == "search_create":
            location = str(entities.get("location") or "").strip()
            if not location:
                reply = "Please provide a location. Example: `queue search in Keene, NY`."
                warnings.append("search_location_missing")
            else:
                payload = {"location": location}
                tool_args = {"payload": payload}
                job = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.search_create",
                    tool_args,
                    lambda: self._tool_search_create(**tool_args),
                )
                if not job:
                    reply = "I couldn't queue the search job."
                else:
                    reply = (
                        f"Queued search job `{job.get('job_id')}` for `{location}`.\n"
                        "Check status with: `job status <job_id>`."
                    )
                    citations.append("/api/v1/search")
        elif intent == "listing_ingest_url":
            url = str(entities.get("url") or "").strip()
            if not url:
                reply = "Please provide an Airbnb listing URL to ingest."
                warnings.append("ingest_url_missing")
            else:
                payload = {"url": url, "include_reviews": True, "review_mode": "lite"}
                tool_args = {"payload": payload}
                job = self._invoke_tool(
                    tool_calls,
                    warnings,
                    "tool.listing_ingest_url",
                    tool_args,
                    lambda: self._tool_listing_ingest_url(**tool_args),
                )
                if not job:
                    reply = "I couldn't queue the listing ingest job."
                else:
                    reply = (
                        f"Queued listing ingest job `{job.get('job_id')}` for `{url}`.\n"
                        "Check status with: `job status <job_id>`."
                    )
                    citations.append("/api/v1/listings/ingest")
        elif intent == "personality_rag_context":
            query_text = str(entities.get("query") or text).strip()
            resolved_user_id = str(user_id or "").strip() or os.getenv("RENTAL_RAG_DEFAULT_USER_ID", "default-user").strip()
            tool_args = {"user_id": resolved_user_id, "query": query_text, "limit": 6}
            result = self._invoke_tool(
                tool_calls,
                warnings,
                "tool.personality_rag_context",
                tool_args,
                lambda: self._tool_personality_rag_context(**tool_args),
            )
            hits = result.get("hits") if isinstance(result, dict) else []
            if not hits:
                reply = (
                    "I couldn't find memory context for this request yet. "
                    "Upload past trip files or add manual memory notes first."
                )
            else:
                reply = self._render_personality_rag_context_reply(result)
                citations.append("/api/v1/memory/query")
        else:
            reply = (
                "I can help with rental workflow tasks. Try asking for pipeline health, "
                "recent jobs, listing details/reviews, search runs, or listing summary status."
            )

        history.append({"role": "assistant", "content": reply, "ts": int(time.time())})
        self._sessions[sid] = history[-self.max_history_messages :]

        latency_ms = int((time.monotonic() - started) * 1000)
        tool_failures = sum(1 for call in tool_calls if not bool(call.get("ok")))
        tool_timeouts = sum(1 for call in tool_calls if bool(call.get("timeout")))
        degraded = tool_failures > 0
        return {
            "session_id": sid,
            "trace_id": trace_id,
            "reply": reply,
            "citations": citations,
            "debug": {
                "intent": intent,
                "entities": entities,
                "tool_calls": tool_calls,
                "warnings": warnings,
                "latency_ms": latency_ms,
                "guardrails": {
                    "degraded": degraded,
                    "tool_call_count": len(tool_calls),
                    "tool_failure_count": tool_failures,
                    "tool_timeout_count": tool_timeouts,
                    "default_timeout_ms": self.tool_timeout_ms_default,
                },
            },
        }

    def _route_intent(self, message: str) -> Dict[str, Any]:
        lowered = message.lower()

        if any(token in lowered for token in ("queue search", "run search", "start search")):
            location = self._extract_search_location(message)
            return {"intent": "search_create", "entities": {"location": location}}

        if "ingest" in lowered:
            listing_url = self._extract_listing_url(message)
            if listing_url:
                return {"intent": "listing_ingest_url", "entities": {"url": listing_url}}

        if self._is_trip_research_prompt(lowered):
            location = self._extract_trip_research_location(message)
            max_results = self._extract_max_results(message)
            entities: Dict[str, Any] = {"location": location}
            if max_results is not None:
                entities["max_results"] = max_results
            return {"intent": "trip_research", "entities": entities}

        if any(
            token in lowered
            for token in (
                "past trips",
                "trip memory",
                "personality",
                "memory context",
                "travel preferences",
                "past itinerary",
            )
        ):
            return {
                "intent": "personality_rag_context",
                "entities": {"query": str(message or "").strip()},
            }

        if any(
            token in lowered
            for token in (
                "pipeline health",
                "metrics",
                "performance",
                "latency",
                "slow",
                "drift",
                "capture time",
            )
        ):
            return {"intent": "pipeline_health", "entities": {}}

        if any(token in lowered for token in ("job status", "status of job", "check job")):
            job_id = self._extract_uuid(message)
            return {"intent": "job_status", "entities": {"job_id": job_id}}

        if any(token in lowered for token in ("recent jobs", "list jobs", "show jobs")):
            return {"intent": "jobs_list", "entities": {}}

        if any(token in lowered for token in ("search listings", "listings for run", "run listings")):
            run_id = self._extract_uuid(message)
            return {"intent": "search_listings_list", "entities": {"run_id": run_id}}

        if any(token in lowered for token in ("search run", "run details")) and "search runs" not in lowered:
            run_id = self._extract_uuid(message)
            if run_id:
                return {"intent": "search_run_get", "entities": {"run_id": run_id}}

        if any(token in lowered for token in ("search runs", "recent runs", "list runs")):
            return {"intent": "search_runs_list", "entities": {}}

        if "listing summary" in lowered:
            listing_id = self._extract_listing_id(message)
            return {"intent": "listing_summary_get", "entities": {"listing_id": listing_id}}

        if "review" in lowered and any(token in lowered for token in ("listing", "for", "show", "get")):
            listing_id = self._extract_listing_id(message)
            return {"intent": "reviews_list", "entities": {"listing_id": listing_id}}

        if any(token in lowered for token in ("show listing", "listing details", "listing status", "get listing")):
            listing_id = self._extract_listing_id(message)
            return {"intent": "listing_get", "entities": {"listing_id": listing_id}}

        if any(token in lowered for token in ("ingested listings", "list listings", "recent listings")):
            return {"intent": "listings_list", "entities": {}}

        return {"intent": "general", "entities": {}}

    def _extract_uuid(self, message: str) -> Optional[str]:
        match = UUID_RE.search(str(message or ""))
        if not match:
            return None
        return str(match.group(0))

    def _extract_listing_url(self, message: str) -> Optional[str]:
        text = str(message or "")
        match = re.search(r"https?://[^\s]+", text)
        if not match:
            return None
        url = str(match.group(0)).strip().rstrip(").,]")
        if "/rooms/" not in url:
            return None
        return url

    def _extract_listing_id(self, message: str) -> Optional[str]:
        text = str(message or "")
        url_match = LISTING_URL_RE.search(text)
        if url_match:
            return str(url_match.group(1))
        labelled = re.search(r"(?:listing(?:\s+id)?|id)\s*[:#]?\s*([0-9]{6,})", text, re.IGNORECASE)
        if labelled:
            return str(labelled.group(1))
        lone = re.fullmatch(r"\s*([0-9]{6,})\s*", text)
        if lone:
            return str(lone.group(1))
        generic_ids = re.findall(r"\b([0-9]{6,})\b", text)
        if len(generic_ids) == 1:
            return str(generic_ids[0])
        return None

    def _extract_search_location(self, message: str) -> Optional[str]:
        text = str(message or "").strip()
        match = re.search(
            r"(?:queue|run|start)\s+search(?:\s+(?:in|for))?\s+(.+)$",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        location = str(match.group(1) or "").strip().strip("'\"")
        location = re.sub(r"[.?!]+$", "", location).strip()
        return location or None

    def _is_trip_research_prompt(self, lowered: str) -> bool:
        travel_tokens = (
            "tripadvisor",
            "things to do",
            "activities in",
            "itinerary",
            "travel ideas",
            "what to do in",
            "top rated activities",
        )
        return any(token in lowered for token in travel_tokens)

    def _extract_trip_research_location(self, message: str) -> Optional[str]:
        text = str(message or "").strip()
        pattern = re.compile(r"(?:in|for|around|near)\s+([A-Za-z0-9][A-Za-z0-9,\- '.]{1,80})", re.IGNORECASE)
        matches = pattern.findall(text)
        if matches:
            location = str(matches[-1]).strip().strip("'\"")
            location = re.sub(r"\s+on\s+tripadvisor.*$", "", location, flags=re.IGNORECASE).strip()
            location = re.sub(r"\s+tripadvisor.*$", "", location, flags=re.IGNORECASE).strip()
            location = re.sub(r"[.?!]+$", "", location).strip()
            return location or None
        return None

    def _extract_max_results(self, message: str) -> Optional[int]:
        text = str(message or "")
        match = re.search(r"\b(?:top|show|return|limit)\s+([0-9]{1,2})\b", text, re.IGNORECASE)
        if not match:
            return None
        return _to_int(match.group(1), 0)

    def _infer_trip_focus(self, message: str) -> List[str]:
        lowered = str(message or "").lower()
        focus: List[str] = []
        mapping = {
            "things_to_do": ("things to do", "activities"),
            "tours": ("tour", "guided"),
            "food": ("food", "restaurant", "eat"),
            "family_friendly": ("family", "kids", "children"),
            "outdoors": ("hike", "outdoor", "nature", "trail", "park"),
            "nightlife": ("nightlife", "bar", "night"),
            "itinerary_ideas": ("itinerary", "plan"),
        }
        for label, tokens in mapping.items():
            if any(token in lowered for token in tokens):
                focus.append(label)
        return focus

    def _invoke_tool(
        self,
        tool_calls: List[Dict[str, Any]],
        warnings: List[str],
        tool: str,
        args: Dict[str, Any],
        fn: Callable[[], Any],
    ) -> Any:
        started = time.monotonic()
        timeout_ms = self._tool_timeout_ms(tool)
        future = self._tool_executor.submit(fn)
        try:
            result = future.result(timeout=(float(timeout_ms) / 1000.0))
            tool_calls.append(
                {
                    "tool": tool,
                    "args": args,
                    "ok": True,
                    "timeout_ms_budget": timeout_ms,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return result
        except FuturesTimeoutError:
            future.cancel()
            warnings.append(f"{tool}_timeout")
            tool_calls.append(
                {
                    "tool": tool,
                    "args": args,
                    "ok": False,
                    "timeout": True,
                    "timeout_ms_budget": timeout_ms,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
            logger.warning("Agent tool timeout", extra={"tool": tool, "timeout_ms": timeout_ms})
            return None
        except Exception as exc:
            warnings.append(f"{tool}_failed")
            tool_calls.append(
                {
                    "tool": tool,
                    "args": args,
                    "ok": False,
                    "error": str(exc),
                    "timeout": False,
                    "timeout_ms_budget": timeout_ms,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
            logger.warning("Agent tool failed (%s): %s", tool, exc)
            return None

    def _tool_timeout_ms(self, tool: str) -> int:
        override = self.tool_timeout_overrides.get(str(tool))
        if override is not None:
            return max(100, int(override))
        catalog_entry = self._tool_catalog().get(str(tool) or "")
        if isinstance(catalog_entry, dict):
            catalog_timeout = _to_int(catalog_entry.get("timeout_ms"), 0)
            if catalog_timeout > 0:
                return max(100, int(catalog_timeout))
        return max(100, int(self.tool_timeout_ms_default))

    def _tool_jobs_list(self, *, limit: int = 10) -> List[Dict[str, Any]]:
        return self.storage.list_jobs(limit=max(1, int(limit or 10)))

    def _tool_job_get(self, *, job_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.get_job(job_id)

    def _tool_search_runs_list(self, *, limit: int = 8) -> List[Dict[str, Any]]:
        return self.storage.list_search_runs(limit=max(1, int(limit or 8)))

    def _tool_search_run_get(self, *, run_id: str) -> Optional[Dict[str, Any]]:
        runs = self.storage.list_search_runs(limit=200)
        return next((item for item in runs if str(item.get("run_id") or "") == str(run_id)), None)

    def _tool_search_listings_list(self, *, run_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.storage.list_search_listings(run_id, limit=max(1, int(limit or 10)))

    def _tool_listings_list(self, *, limit: int = 8) -> List[Dict[str, Any]]:
        return self.storage.list_listings(limit=max(1, int(limit or 8)))

    def _tool_listing_get(self, *, listing_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.get_listing(listing_id)

    def _tool_reviews_list(self, *, listing_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        return self.storage.list_reviews(listing_id, limit=max(1, int(limit or 8)))

    def _tool_listing_summary_get(self, *, listing_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.get_latest_enrichment(listing_id, "listing_summary")

    def _tool_search_create(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.storage.create_job("search", payload)

    def _tool_listing_ingest_url(self, *, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.storage.create_job("listing_ingest", payload)

    def _apply_run_params_to_listing_url(self, url: str, run_params: Dict[str, Any]) -> str:
        raw_url = str(url or "").strip()
        if not raw_url:
            return raw_url
        parsed = urlparse(raw_url)
        existing_pairs = parse_qsl(parsed.query, keep_blank_values=False)
        query_map: Dict[str, str] = {str(key): str(value) for key, value in existing_pairs}
        for key in ("check_in", "check_out", "adults", "children", "infants", "pets", "currency"):
            value = _run_param_value(run_params, key)
            if value is None:
                continue
            value_text = str(value).strip()
            if not value_text:
                continue
            query_map[key] = value_text
        new_query = urlencode(query_map)
        return urlunparse(parsed._replace(query=new_query))

    def _tool_search_ingest_listings(
        self,
        *,
        run_id: str,
        listing_ids: List[str],
        review_mode: Optional[str] = None,
        review_limit: Optional[int] = None,
        review_only: Optional[bool] = None,
        include_reviews: Optional[bool] = None,
        force: Optional[bool] = None,
    ) -> Dict[str, Any]:
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            return {"error": "run_id is required", "code": "validation_error"}
        if not isinstance(listing_ids, list):
            return {"error": "listing_ids must be a list", "code": "validation_error"}
        normalized_ids = [str(value or "").strip() for value in listing_ids if str(value or "").strip()]
        deduped_ids: List[str] = []
        seen = set()
        for listing_id in normalized_ids:
            if listing_id in seen:
                continue
            seen.add(listing_id)
            deduped_ids.append(listing_id)
        if not deduped_ids:
            return {"error": "listing_ids is required", "code": "validation_error"}

        runs = self.storage.list_search_runs(limit=500)
        run = next((item for item in runs if str(item.get("run_id") or "") == resolved_run_id), None)
        run_params = run.get("params") if isinstance(run, dict) and isinstance(run.get("params"), dict) else {}
        listings = self.storage.list_search_listings(resolved_run_id, limit=5000)
        by_id = {
            str(item.get("id") or item.get("listing_id") or "").strip(): item
            for item in listings
            if str(item.get("id") or item.get("listing_id") or "").strip()
        }

        jobs: List[Dict[str, Any]] = []
        missing: List[str] = []
        for listing_id in deduped_ids:
            listing = by_id.get(listing_id)
            if not isinstance(listing, dict):
                missing.append(listing_id)
                continue
            raw_url = str(listing.get("url") or "").strip()
            if not raw_url:
                raw_url = f"https://www.airbnb.com/rooms/{listing_id}"
            capture_url = self._apply_run_params_to_listing_url(raw_url, run_params)
            job_payload: Dict[str, Any] = {"url": capture_url}
            for key in ("check_in", "check_out", "adults", "children", "infants", "pets", "currency"):
                value = _run_param_value(run_params, key)
                if value is None:
                    continue
                value_text = str(value).strip()
                if value_text:
                    job_payload[key] = value

            normalized_review_mode = str(review_mode or "").strip().lower()
            if normalized_review_mode in {"lite", "full"}:
                job_payload["review_mode"] = normalized_review_mode
            elif include_reviews is not None:
                job_payload["include_reviews"] = bool(include_reviews)
            else:
                job_payload["include_reviews"] = True

            if review_only is not None:
                job_payload["review_only"] = bool(review_only)
            if review_limit is not None:
                job_payload["review_limit"] = self._parse_int_range(review_limit, minimum=1, maximum=200, fallback=24)
            if _to_bool(force, False):
                job_payload["force"] = True

            job = self.storage.create_job("listing_ingest", job_payload)
            jobs.append(job)

        return {
            "run_id": resolved_run_id,
            "jobs": jobs,
            "missing": missing,
            "run_params_applied": {
                key: _run_param_value(run_params, key)
                for key in ("check_in", "check_out", "adults", "children", "infants", "pets", "currency")
                if _run_param_value(run_params, key) is not None
            },
        }

    def _tool_personality_rag_context(
        self,
        *,
        user_id: Optional[str],
        query: str,
        limit: int = 6,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.personality_rag.query_context(
            user_id=user_id,
            query=query,
            limit=self._parse_int_range(limit, minimum=1, maximum=12, fallback=6),
            tags=tags or [],
        )

    def _tool_personality_rag_upsert(
        self,
        *,
        user_id: Optional[str],
        title: str,
        text: str,
        tags: Optional[List[str]] = None,
        trip_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {"source": "tool_upsert"}
        if trip_id:
            metadata["trip_id"] = str(trip_id)
        return self.personality_rag.upsert_memory_text(
            user_id=user_id,
            title=str(title or "").strip() or "Manual memory",
            text=str(text or ""),
            tags=tags or [],
            metadata=metadata,
        )

    def _tool_trip_research_tavily(
        self,
        *,
        location: str,
        max_results: int = 8,
        focus: Optional[List[str]] = None,
        trip_dates: Optional[Dict[str, Any]] = None,
        party_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cleaned_location = str(location or "").strip()
        if not cleaned_location:
            return {"location": "", "activities": [], "warning": "location_missing"}

        domains = self._trip_research_include_domains()
        query = self._build_trip_research_query(
            location=cleaned_location,
            focus=focus or [],
            trip_dates=trip_dates or {},
            party_profile=party_profile or {},
        )
        depth = str(os.getenv("RENTAL_TRIP_RESEARCH_SEARCH_DEPTH", "advanced") or "advanced").strip()
        limit = self._parse_int_range(
            max_results,
            minimum=3,
            maximum=max(3, _to_int(os.getenv("RENTAL_TRIP_RESEARCH_MAX_RESULTS_MAX"), 20)),
            fallback=max(3, _to_int(os.getenv("RENTAL_TRIP_RESEARCH_MAX_RESULTS_DEFAULT"), 8)),
        )
        raw = self.tavily_client.search(
            query,
            max_results=limit,
            search_depth=depth,
            include_domains=domains,
        )
        activities = self._normalize_trip_research_results(raw.get("results"))
        ranked = self._rank_trip_research_activities(activities)[:limit]
        return {
            "location": cleaned_location,
            "query": query,
            "activities": ranked,
            "result_count": len(ranked),
            "warning": raw.get("warning"),
        }

    def _comparison_reviews_total(self, listing: Dict[str, Any]) -> Optional[int]:
        total = (
            _to_int(listing.get("reviews_total_count"), 0)
            or _to_int((listing.get("reviews_summary") or {}).get("count"), 0)
            or _to_int((listing.get("host") or {}).get("review_count"), 0)
        )
        if total <= 0:
            return None
        return int(total)

    def _comparison_coverage_snapshot(
        self,
        listing: Dict[str, Any],
        reviews: List[Dict[str, Any]],
        review_limit: int,
    ) -> Dict[str, Any]:
        limit = max(1, int(review_limit or 24))
        reviews_list = reviews or []
        captured = _to_int(listing.get("reviews_captured_count"), len(reviews_list))
        captured = max(0, int(captured))
        total = self._comparison_reviews_total(listing)
        target = min(limit, total) if total else limit
        target = max(1, int(target))
        coverage = min(float(captured) / float(target), 1.0)
        return {
            "captured": captured,
            "total": total,
            "target": target,
            "coverage": round(coverage, 4),
        }

    def _comparison_coverage_violations(
        self,
        listings: List[Dict[str, Any]],
        reviews_by_listing: Dict[str, List[Dict[str, Any]]],
        *,
        review_limit: int,
        min_review_coverage: float,
    ) -> List[Dict[str, Any]]:
        violations: List[Dict[str, Any]] = []
        threshold = max(0.0, min(float(min_review_coverage), 1.0))
        for listing in listings:
            listing_id = str(listing.get("id") or listing.get("listing_id") or "").strip()
            reviews = reviews_by_listing.get(listing_id) or []
            snapshot = self._comparison_coverage_snapshot(listing, reviews, review_limit)
            coverage = float(snapshot.get("coverage") or 0.0)
            if coverage + 1e-9 >= threshold:
                continue
            target = int(snapshot.get("target") or 1)
            required_captured = min(target, max(1, int(target * threshold + 0.9999)))
            violations.append(
                {
                    "listing_id": listing_id,
                    "title": listing.get("title"),
                    "captured": int(snapshot.get("captured") or 0),
                    "total": snapshot.get("total"),
                    "target": target,
                    "coverage": round(coverage, 4),
                    "required_coverage": round(threshold, 4),
                    "required_captured": required_captured,
                    "reason": "coverage_below_threshold",
                }
            )
        return violations

    def _tool_listing_compare_create(
        self,
        *,
        listing_ids: List[str],
        sync: Optional[bool] = None,
        force: Optional[bool] = None,
        model: Optional[str] = None,
        review_limit: Optional[int] = None,
        require_min_coverage: Optional[bool] = None,
        min_review_coverage: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not isinstance(listing_ids, list):
            return {"error": "listing_ids must be a list", "code": "validation_error"}
        normalized_ids = [str(item).strip() for item in listing_ids if str(item).strip()]
        deduped_ids: List[str] = []
        seen = set()
        for listing_id in normalized_ids:
            if listing_id in seen:
                continue
            seen.add(listing_id)
            deduped_ids.append(listing_id)
        if len(deduped_ids) < 2:
            return {"error": "listing_ids must include at least 2 items", "code": "validation_error"}
        if len(deduped_ids) > 6:
            return {"error": "listing_ids must include at most 6 items", "code": "validation_error"}

        limit = self._parse_int_range(
            review_limit,
            minimum=1,
            maximum=50,
            fallback=24,
        )
        require_coverage = (
            _to_bool(require_min_coverage, _to_bool(os.getenv("RENTAL_COMPARE_REQUIRE_MIN_COVERAGE_DEFAULT"), False))
            if require_min_coverage is not None
            else _to_bool(os.getenv("RENTAL_COMPARE_REQUIRE_MIN_COVERAGE_DEFAULT"), False)
        )
        coverage_threshold = _coerce_ratio(
            min_review_coverage,
            _coerce_ratio(os.getenv("RENTAL_COMPARE_MIN_COVERAGE_DEFAULT"), 0.5),
        )

        listings: List[Dict[str, Any]] = []
        missing: List[str] = []
        reviews_by_listing: Dict[str, List[Dict[str, Any]]] = {}
        for listing_id in deduped_ids:
            listing = self.storage.get_listing(listing_id)
            if not listing:
                missing.append(listing_id)
                continue
            listings.append(listing)
            reviews_by_listing[listing_id] = self.storage.list_reviews(listing_id, limit=limit)
        if missing:
            return {"error": "Listings not found", "code": "not_found", "missing": missing}

        if require_coverage:
            violations = self._comparison_coverage_violations(
                listings,
                reviews_by_listing,
                review_limit=limit,
                min_review_coverage=coverage_threshold,
            )
            if violations:
                return {
                    "error": "Comparison blocked by minimum review coverage policy.",
                    "code": "comparison_coverage_blocked",
                    "policy": {
                        "require_min_coverage": True,
                        "min_review_coverage": round(coverage_threshold, 4),
                        "review_limit": int(limit),
                    },
                    "violations": violations,
                    "suggested_action": "Fetch full reviews for listed listings and retry.",
                }

        sync_mode = _to_bool(sync, True) if sync is not None else True
        if not sync_mode:
            payload = {
                "listing_ids": deduped_ids,
                "kind": "listing_comparison",
                "model": model,
                "review_limit": int(limit),
                "require_min_coverage": bool(require_coverage),
                "min_review_coverage": float(coverage_threshold),
            }
            job = self.storage.create_job("listing_compare", payload)
            return {"status": "queued", "job": job}

        try:
            from .llm_enrichment import (
                COMPARISON_PROMPT_VERSION,
                build_comparison_request,
                generate_listing_comparison,
            )
        except Exception as exc:
            return {
                "error": "listing_comparison_dependencies_unavailable",
                "code": "dependency_error",
                "details": str(exc),
            }

        model_name, input_hash = build_comparison_request(listings, reviews_by_listing, model=model)
        compare_key = f"compare:{input_hash}"
        if not _to_bool(force, False):
            existing = self.storage.get_enrichment_by_hash(
                compare_key,
                "listing_comparison",
                model_name,
                COMPARISON_PROMPT_VERSION,
                input_hash,
            )
            if existing:
                return {"status": "cached", "summary": existing}

        payload = {
            "listing_ids": deduped_ids,
            "kind": "listing_comparison",
            "model": model_name,
            "input_hash": input_hash,
            "review_limit": int(limit),
            "require_min_coverage": bool(require_coverage),
            "min_review_coverage": float(coverage_threshold),
        }
        job = self.storage.create_job("listing_compare", payload)
        self.storage.update_job(job["job_id"], status="running")
        try:
            output = generate_listing_comparison(listings, reviews_by_listing, model=model_name)
            enrichment_id = self.storage.add_enrichment(
                compare_key,
                "listing_comparison",
                model_name,
                COMPARISON_PROMPT_VERSION,
                input_hash,
                output,
            )
            self.storage.update_job(job["job_id"], status="complete", result_ref=enrichment_id)
            return {"status": "complete", "summary": output, "job": job}
        except Exception as exc:
            self.storage.update_job(job["job_id"], status="failed", error=str(exc))
            return {"error": str(exc), "code": "comparison_generation_failed"}

    def _parse_int_range(
        self,
        value: Any,
        *,
        minimum: int,
        maximum: int,
        fallback: int,
    ) -> int:
        parsed = _to_int(value, fallback)
        return max(int(minimum), min(int(maximum), int(parsed)))

    def _trip_research_include_domains(self) -> List[str]:
        configured = str(os.getenv("RENTAL_TRIP_RESEARCH_INCLUDE_DOMAINS", "tripadvisor.com") or "").strip()
        if not configured:
            return ["tripadvisor.com"]
        domains = [item.strip().lower() for item in configured.split(",") if item.strip()]
        return domains or ["tripadvisor.com"]

    def _build_trip_research_query(
        self,
        *,
        location: str,
        focus: List[str],
        trip_dates: Dict[str, Any],
        party_profile: Dict[str, Any],
    ) -> str:
        focus_map = {
            "things_to_do": "things to do",
            "tours": "tours",
            "food": "food and restaurants",
            "family_friendly": "family friendly",
            "outdoors": "outdoor activities",
            "nightlife": "nightlife",
            "itinerary_ideas": "itinerary ideas",
        }
        focus_labels = [focus_map.get(item, item.replace("_", " ")) for item in focus if str(item).strip()]
        focus_text = ", ".join(focus_labels) if focus_labels else "things to do"

        date_tokens: List[str] = []
        check_in = str(trip_dates.get("check_in") or "").strip()
        check_out = str(trip_dates.get("check_out") or "").strip()
        if check_in:
            date_tokens.append(f"check-in {check_in}")
        if check_out:
            date_tokens.append(f"check-out {check_out}")
        date_text = f" for {' and '.join(date_tokens)}" if date_tokens else ""

        adults = _to_int(party_profile.get("adults"), 0)
        children = _to_int(party_profile.get("children"), 0)
        party_text = ""
        if adults > 0 or children > 0:
            party_text = f" for group (adults {max(0, adults)}, children {max(0, children)})"

        return (
            f"Tripadvisor {focus_text} in {location}{date_text}{party_text}. "
            "Prefer top-rated options with rating and review count."
        )

    def _normalize_trip_research_results(self, results: Any) -> List[Dict[str, Any]]:
        if not isinstance(results, list):
            return []
        output: List[Dict[str, Any]] = []
        seen = set()
        for row in results:
            item = row if isinstance(row, dict) else {}
            url = str(item.get("url") or "").strip()
            parsed_domain = ""
            if url:
                try:
                    parsed_domain = str(urlparse(url).netloc or "").lower()
                except Exception:
                    parsed_domain = ""
            if parsed_domain and "tripadvisor." not in parsed_domain:
                continue

            title = str(item.get("title") or "").strip()
            summary = str(item.get("content") or item.get("snippet") or "").strip()
            merged = f"{title} {summary}".strip()
            rating = self._extract_rating(merged)
            rating_count = self._extract_rating_count(merged)
            price_hint = self._extract_price_hint(merged)
            category = self._infer_activity_category(merged)
            name = self._clean_activity_name(title) or title or "Untitled activity"
            dedupe_key = (url or name).lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            output.append(
                {
                    "name": name,
                    "category": category,
                    "rating": rating,
                    "rating_count": rating_count,
                    "price_hint": price_hint,
                    "source_url": url,
                    "source": "tripadvisor" if "tripadvisor." in parsed_domain else "web",
                    "summary": summary,
                }
            )
        return output

    def _rank_trip_research_activities(self, activities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def _score(item: Dict[str, Any]) -> Any:
            rating = item.get("rating")
            rating_count = item.get("rating_count")
            has_rating = 1 if rating is not None else 0
            has_count = 1 if rating_count is not None else 0
            normalized_rating = float(rating) if rating is not None else -1.0
            normalized_count = int(rating_count) if rating_count is not None else -1
            return (
                -has_rating,
                -normalized_rating,
                -has_count,
                -normalized_count,
                str(item.get("name") or "").lower(),
            )

        return sorted(activities, key=_score)

    def _extract_rating(self, text: str) -> Optional[float]:
        match = RATING_RE.search(str(text or ""))
        if not match:
            return None
        return _to_float(match.group(1))

    def _extract_rating_count(self, text: str) -> Optional[int]:
        match = RATING_COUNT_RE.search(str(text or ""))
        if not match:
            return None
        value = str(match.group(1)).replace(",", "")
        parsed = _to_int(value, -1)
        return parsed if parsed >= 0 else None

    def _extract_price_hint(self, text: str) -> Optional[str]:
        match = PRICE_HINT_RE.search(str(text or ""))
        if not match:
            return None
        return str(match.group(1))

    def _infer_activity_category(self, text: str) -> str:
        lowered = str(text or "").lower()
        if any(token in lowered for token in ("hike", "trail", "park", "mountain", "outdoor")):
            return "outdoors"
        if any(token in lowered for token in ("tour", "museum", "sightseeing", "landmark")):
            return "tours"
        if any(token in lowered for token in ("restaurant", "food", "eat", "brewery", "winery")):
            return "food"
        if any(token in lowered for token in ("family", "kids", "children")):
            return "family_friendly"
        if any(token in lowered for token in ("bar", "nightlife", "night club")):
            return "nightlife"
        return "things_to_do"

    def _clean_activity_name(self, title: str) -> str:
        text = str(title or "").strip()
        if not text:
            return ""
        cleaned = re.sub(r"\s*-\s*Tripadvisor.*$", "", text, flags=re.IGNORECASE).strip()
        return cleaned

    def _tool_search_create_args(
        self,
        *,
        location: str,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
        adults: Optional[int] = None,
        children: Optional[int] = None,
        infants: Optional[int] = None,
        pets: Optional[int] = None,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        room_type: Optional[str] = None,
        amenities: Optional[List[str]] = None,
        flexible_cancellation: Optional[bool] = None,
        min_bedrooms: Optional[int] = None,
        min_beds: Optional[int] = None,
        min_bathrooms: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"location": str(location or "").strip()}
        if check_in:
            payload["check_in"] = str(check_in).strip()
        if check_out:
            payload["check_out"] = str(check_out).strip()
        payload["adults"] = self._parse_int_range(adults, minimum=1, maximum=16, fallback=1)
        payload["children"] = self._parse_int_range(children, minimum=0, maximum=10, fallback=0)
        payload["infants"] = self._parse_int_range(infants, minimum=0, maximum=10, fallback=0)
        payload["pets"] = self._parse_int_range(pets, minimum=0, maximum=10, fallback=0)
        if min_price is not None:
            payload["min_price"] = self._parse_int_range(min_price, minimum=0, maximum=100000, fallback=0)
        if max_price is not None:
            payload["max_price"] = self._parse_int_range(max_price, minimum=0, maximum=100000, fallback=0)
        if room_type:
            payload["room_type"] = str(room_type).strip()
        if isinstance(amenities, list):
            payload["amenities"] = [str(item).strip() for item in amenities if str(item).strip()]
        if flexible_cancellation is not None:
            payload["flexible_cancellation"] = _to_bool(flexible_cancellation, False)
        if min_bedrooms is not None:
            payload["min_bedrooms"] = self._parse_int_range(min_bedrooms, minimum=1, maximum=20, fallback=1)
        if min_beds is not None:
            payload["min_beds"] = self._parse_int_range(min_beds, minimum=1, maximum=20, fallback=1)
        if min_bathrooms is not None:
            payload["min_bathrooms"] = self._parse_int_range(min_bathrooms, minimum=1, maximum=20, fallback=1)
        return self._tool_search_create(payload=payload)

    def _tool_listing_ingest_url_args(
        self,
        *,
        url: str,
        review_mode: Optional[str] = None,
        review_limit: Optional[int] = None,
        review_only: Optional[bool] = None,
        force: Optional[bool] = None,
        include_reviews: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"url": str(url or "").strip()}
        mode = str(review_mode or "lite").strip().lower()
        if mode in {"lite", "full", "none"}:
            payload["review_mode"] = mode
        payload["include_reviews"] = _to_bool(include_reviews, True)
        if review_limit is not None:
            payload["review_limit"] = self._parse_int_range(
                review_limit,
                minimum=1,
                maximum=200,
                fallback=24,
            )
        if review_only is not None:
            payload["review_only"] = _to_bool(review_only, False)
        if force is not None:
            payload["force"] = _to_bool(force, False)
        return self._tool_listing_ingest_url(payload=payload)

    def _tool_catalog(self) -> Dict[str, Dict[str, Any]]:
        return {
            "tool.metrics_jobs": {
                "description": "Get recent pipeline metrics and aggregated summary.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        "summary_limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        "job_type": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "handler": self._tool_metrics_jobs,
                "citation": lambda args: "/api/v1/metrics/jobs",
            },
            "tool.jobs_list": {
                "description": "List recent jobs and their status.",
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                    "required": [],
                    "additionalProperties": False,
                },
                "handler": self._tool_jobs_list,
                "citation": lambda args: "/api/v1/jobs",
            },
            "tool.job_get": {
                "description": "Get status and result reference for a specific job.",
                "input_schema": {
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
                "handler": self._tool_job_get,
                "citation": lambda args: f"/api/v1/jobs/{str(args.get('job_id') or '').strip()}",
            },
            "tool.search_runs_list": {
                "description": "List recent search runs.",
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                    "required": [],
                    "additionalProperties": False,
                },
                "handler": self._tool_search_runs_list,
                "citation": lambda args: "/api/v1/search/runs",
            },
            "tool.search_run_get": {
                "description": "Get details for one search run id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
                "handler": self._tool_search_run_get,
                "citation": lambda args: f"/api/v1/search/runs/{str(args.get('run_id') or '').strip()}",
            },
            "tool.search_listings_list": {
                "description": "List search listings for a search run.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
                "handler": self._tool_search_listings_list,
                "citation": lambda args: (
                    f"/api/v1/search/listings?run_id={str(args.get('run_id') or '').strip()}"
                    f"&limit={self._parse_int_range(args.get('limit'), minimum=1, maximum=500, fallback=10)}"
                ),
            },
            "tool.listings_list": {
                "description": "List recent ingested listings.",
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                    "required": [],
                    "additionalProperties": False,
                },
                "handler": self._tool_listings_list,
                "citation": lambda args: (
                    f"/api/v1/listings?limit={self._parse_int_range(args.get('limit'), minimum=1, maximum=200, fallback=8)}"
                ),
            },
            "tool.listing_get": {
                "description": "Get one ingested listing by listing id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"listing_id": {"type": "string"}},
                    "required": ["listing_id"],
                    "additionalProperties": False,
                },
                "handler": self._tool_listing_get,
                "citation": lambda args: f"/api/v1/listings/{str(args.get('listing_id') or '').strip()}",
            },
            "tool.reviews_list": {
                "description": "Get stored reviews for one listing id.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "listing_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["listing_id"],
                    "additionalProperties": False,
                },
                "handler": self._tool_reviews_list,
                "citation": lambda args: (
                    f"/api/v1/reviews?listing_id={str(args.get('listing_id') or '').strip()}"
                    f"&limit={self._parse_int_range(args.get('limit'), minimum=1, maximum=200, fallback=8)}"
                ),
            },
            "tool.listing_summary_get": {
                "description": "Get the latest stored listing summary enrichment.",
                "input_schema": {
                    "type": "object",
                    "properties": {"listing_id": {"type": "string"}},
                    "required": ["listing_id"],
                    "additionalProperties": False,
                },
                "handler": self._tool_listing_summary_get,
                "citation": lambda args: f"/api/v1/enrich/listings/{str(args.get('listing_id') or '').strip()}/summary",
            },
            "tool.listing_compare_create": {
                "description": (
                    "Create a listing comparison using stored listings and reviews. "
                    "Defaults to synchronous generation for interactive chat."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "listing_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 6,
                        },
                        "sync": {"type": "boolean"},
                        "force": {"type": "boolean"},
                        "model": {"type": "string"},
                        "review_limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        "require_min_coverage": {"type": "boolean"},
                        "min_review_coverage": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["listing_ids"],
                    "additionalProperties": False,
                },
                "handler": self._tool_listing_compare_create,
                "citation": lambda args: "/api/v1/enrich/compare",
                "timeout_ms": max(3000, _to_int(os.getenv("RENTAL_LISTING_COMPARE_TOOL_TIMEOUT_MS"), 90000)),
            },
            "tool.search_create": {
                "description": "Queue a search capture job.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "check_in": {"type": "string"},
                        "check_out": {"type": "string"},
                        "adults": {"type": "integer", "minimum": 1, "maximum": 16},
                        "children": {"type": "integer", "minimum": 0, "maximum": 10},
                        "infants": {"type": "integer", "minimum": 0, "maximum": 10},
                        "pets": {"type": "integer", "minimum": 0, "maximum": 10},
                        "min_price": {"type": "integer", "minimum": 0, "maximum": 100000},
                        "max_price": {"type": "integer", "minimum": 0, "maximum": 100000},
                        "room_type": {"type": "string"},
                        "amenities": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                        "flexible_cancellation": {"type": "boolean"},
                        "min_bedrooms": {"type": "integer", "minimum": 1, "maximum": 20},
                        "min_beds": {"type": "integer", "minimum": 1, "maximum": 20},
                        "min_bathrooms": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["location"],
                    "additionalProperties": False,
                },
                "handler": self._tool_search_create_args,
                "citation": lambda args: "/api/v1/search",
            },
            "tool.listing_ingest_url": {
                "description": "Queue a listing ingest job by Airbnb listing URL.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "review_mode": {"type": "string"},
                        "review_limit": {"type": "integer", "minimum": 1, "maximum": 200},
                        "review_only": {"type": "boolean"},
                        "force": {"type": "boolean"},
                        "include_reviews": {"type": "boolean"},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                "handler": self._tool_listing_ingest_url_args,
                "citation": lambda args: "/api/v1/listings/ingest",
            },
            "tool.search_ingest_listings": {
                "description": (
                    "Queue listing ingest jobs from search results by run_id + listing_ids. "
                    "Automatically carries run check-in/out and guest parameters so pricing context is preserved."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "listing_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 20,
                        },
                        "review_mode": {"type": "string", "enum": ["lite", "full"]},
                        "review_limit": {"type": "integer", "minimum": 1, "maximum": 200},
                        "review_only": {"type": "boolean"},
                        "include_reviews": {"type": "boolean"},
                        "force": {"type": "boolean"},
                    },
                    "required": ["run_id", "listing_ids"],
                    "additionalProperties": False,
                },
                "handler": self._tool_search_ingest_listings,
                "citation": lambda args: "/api/v1/search/ingest",
            },
            "tool.personality_rag_context": {
                "description": (
                    "Retrieve personality and past-trip memory context from uploaded/manual memory records. "
                    "Use for planning and recommendation prompts."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "handler": self._tool_personality_rag_context,
                "citation": lambda args: "/api/v1/memory/query",
                "timeout_ms": max(500, _to_int(os.getenv("RENTAL_RAG_CONTEXT_TOOL_TIMEOUT_MS"), 4000)),
            },
            "tool.personality_rag_upsert": {
                "description": "Store or update personality/trip memory text for future retrieval.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "title": {"type": "string"},
                        "text": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "trip_id": {"type": "string"},
                    },
                    "required": ["title", "text"],
                    "additionalProperties": False,
                },
                "handler": self._tool_personality_rag_upsert,
                "citation": lambda args: "/api/v1/memory/upsert",
                "timeout_ms": max(1000, _to_int(os.getenv("RENTAL_RAG_UPSERT_TOOL_TIMEOUT_MS"), 8000)),
            },
            "tool.trip_research_tavily": {
                "description": (
                    "Research Tripadvisor-style activities for a location via Tavily web search, "
                    "then return normalized results ranked by rating and rating count."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "trip_dates": {
                            "type": "object",
                            "properties": {
                                "check_in": {"type": "string"},
                                "check_out": {"type": "string"},
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                        "party_profile": {
                            "type": "object",
                            "properties": {
                                "adults": {"type": "integer", "minimum": 0, "maximum": 16},
                                "children": {"type": "integer", "minimum": 0, "maximum": 10},
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                        "max_results": {"type": "integer", "minimum": 3, "maximum": 20},
                        "focus": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "things_to_do",
                                    "tours",
                                    "food",
                                    "family_friendly",
                                    "outdoors",
                                    "nightlife",
                                    "itinerary_ideas",
                                ],
                            },
                        },
                    },
                    "required": ["location"],
                    "additionalProperties": False,
                },
                "handler": self._tool_trip_research_tavily,
                "citation": lambda args: "tavily:tripadvisor.com",
                "timeout_ms": max(1500, _to_int(os.getenv("RENTAL_TRIP_RESEARCH_TOOL_TIMEOUT_MS"), 12000)),
            },
        }

    def get_tool_definitions(self, tool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        catalog = self._tool_catalog()
        selected: List[str]
        if tool_names:
            selected = [name for name in tool_names if name in catalog]
        else:
            selected = sorted(catalog.keys())
        output: List[Dict[str, Any]] = []
        for name in selected:
            entry = catalog.get(name) or {}
            output.append(
                {
                    "name": name,
                    "description": entry.get("description") or "",
                    "input_schema": entry.get("input_schema") or {"type": "object", "properties": {}},
                }
            )
        return output

    def get_tool_citation(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        catalog = self._tool_catalog()
        entry = catalog.get(str(tool_name) or "")
        if not entry:
            return None
        citation = entry.get("citation")
        if callable(citation):
            try:
                result = citation(args or {})
            except Exception:
                return None
            if result:
                return str(result)
            return None
        if citation:
            return str(citation)
        return None

    def execute_tool(
        self,
        tool_name: str,
        tool_input: Optional[Dict[str, Any]],
        *,
        tool_calls: List[Dict[str, Any]],
        warnings: List[str],
    ) -> Any:
        catalog = self._tool_catalog()
        entry = catalog.get(str(tool_name) or "")
        if not entry:
            warnings.append("unknown_tool")
            tool_calls.append(
                {
                    "tool": str(tool_name),
                    "args": tool_input if isinstance(tool_input, dict) else {},
                    "ok": False,
                    "error": "unknown_tool",
                    "latency_ms": 0,
                }
            )
            return {"error": "unknown tool"}
        args = tool_input if isinstance(tool_input, dict) else {}
        schema = entry.get("input_schema") if isinstance(entry.get("input_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if properties:
            allowed = set(str(name) for name in properties.keys())
            filtered = {key: value for key, value in args.items() if str(key) in allowed}
            if len(filtered) != len(args):
                warnings.append(f"{tool_name}_args_sanitized")
            args = filtered
        handler = entry.get("handler")
        if not callable(handler):
            warnings.append("tool_handler_missing")
            tool_calls.append(
                {
                    "tool": str(tool_name),
                    "args": args,
                    "ok": False,
                    "error": "tool handler missing",
                    "latency_ms": 0,
                }
            )
            return {"error": "tool handler missing"}
        return self._invoke_tool(
            tool_calls,
            warnings,
            str(tool_name),
            args,
            lambda: handler(**args),
        )

    def _tool_metrics_jobs(
        self,
        *,
        limit: int = 60,
        summary_limit: int = 240,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        metrics = self.storage.list_job_metrics(
            limit=max(1, int(limit or 60)),
            job_type=job_type,
            status=status,
        )
        summary_source = self.storage.list_job_metrics(
            limit=max(1, int(summary_limit or 240)),
            job_type=job_type,
            status=status,
        )
        summary = self._summarize_job_metrics(summary_source)
        return {"metrics": metrics, "summary": summary}

    def _render_jobs_reply(self, jobs: List[Dict[str, Any]]) -> str:
        if not jobs:
            return "No jobs found."
        shown = jobs[:8]
        lines: List[str] = [f"Recent jobs ({len(shown)} shown):"]
        for job in shown:
            lines.append(
                f"- {job.get('job_type')} | {job.get('status')} | {job.get('job_id')}"
            )
        return "\n".join(lines)

    def _render_job_status_reply(self, job: Dict[str, Any]) -> str:
        return (
            "Job status:\n"
            f"- Job: {job.get('job_id')}\n"
            f"- Type: {job.get('job_type')}\n"
            f"- Status: {job.get('status')}\n"
            f"- Result ref: {job.get('result_ref') or 'n/a'}\n"
            f"- Error: {job.get('error') or 'none'}"
        )

    def _render_search_runs_reply(self, runs: List[Dict[str, Any]]) -> str:
        if not runs:
            return "No search runs found."
        shown = runs[:6]
        lines: List[str] = [f"Recent search runs ({len(shown)} shown):"]
        for run in shown:
            params = run.get("params") if isinstance(run.get("params"), dict) else {}
            location = params.get("location") or params.get("query") or "unknown"
            lines.append(f"- {run.get('run_id')} | location={location}")
        return "\n".join(lines)

    def _render_search_run_detail_reply(self, run: Dict[str, Any]) -> str:
        params = run.get("params") if isinstance(run.get("params"), dict) else {}
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        return (
            "Search run detail:\n"
            f"- Run: {run.get('run_id')}\n"
            f"- Location: {params.get('location') or 'n/a'}\n"
            f"- Captured URL: {result.get('captured_url') or 'n/a'}\n"
            f"- Listing count: {result.get('listing_count') if result.get('listing_count') is not None else 'n/a'}"
        )

    def _render_search_listings_reply(self, run_id: str, listings: List[Dict[str, Any]]) -> str:
        if not listings:
            return f"No listings found for run `{run_id}`."
        shown = listings[:8]
        lines: List[str] = [f"Search listings for run `{run_id}` ({len(shown)} shown):"]
        for listing in shown:
            lines.append(
                f"- {listing.get('id') or listing.get('listing_id')} | "
                f"{listing.get('title') or 'Untitled'} | "
                f"{listing.get('location') or 'unknown location'}"
            )
        return "\n".join(lines)

    def _render_listings_reply(self, listings: List[Dict[str, Any]]) -> str:
        if not listings:
            return "No ingested listings found."
        shown = listings[:8]
        lines: List[str] = [f"Ingested listings ({len(shown)} shown):"]
        for listing in shown:
            listing_id = listing.get("id") or listing.get("listing_id")
            lines.append(
                f"- {listing_id} | {listing.get('title') or 'Untitled'} | "
                f"stage={listing.get('capture_stage') or 'unknown'}"
            )
        return "\n".join(lines)

    def _render_listing_detail_reply(self, listing: Dict[str, Any]) -> str:
        listing_id = listing.get("id") or listing.get("listing_id")
        pricing = listing.get("pricing") if isinstance(listing.get("pricing"), dict) else {}
        location = _normalize_location(listing.get("location"))
        return (
            "Listing detail:\n"
            f"- Listing: {listing_id}\n"
            f"- Title: {listing.get('title') or 'n/a'}\n"
            f"- Location: {location}\n"
            f"- Capture stage: {listing.get('capture_stage') or 'unknown'}\n"
            f"- Reviews captured/total: {listing.get('reviews_captured_count') or 0}/"
            f"{listing.get('reviews_total_count') if listing.get('reviews_total_count') is not None else 'n/a'}\n"
            f"- Price: {pricing.get('price_total') or listing.get('price') or 'n/a'}"
        )

    def _render_reviews_reply(self, listing_id: str, reviews: List[Dict[str, Any]]) -> str:
        if not reviews:
            return f"No stored reviews found for listing `{listing_id}`."
        shown = reviews[:5]
        lines: List[str] = [f"Recent reviews for `{listing_id}` ({len(shown)} shown):"]
        for review in shown:
            author = review.get("reviewer_name") or review.get("author") or "Unknown author"
            rating = review.get("rating")
            text = (
                review.get("text")
                or review.get("comment")
                or review.get("body")
                or ""
            )
            snippet = str(text).strip().replace("\n", " ")
            if len(snippet) > 90:
                snippet = snippet[:87] + "..."
            rating_label = f"rating={rating}" if rating is not None else "rating=n/a"
            lines.append(f"- {author} | {rating_label} | {snippet or '[no text]'}")
        return "\n".join(lines)

    def _render_listing_summary_reply(self, listing_id: str, summary: Dict[str, Any]) -> str:
        if not isinstance(summary, dict):
            return f"Summary for `{listing_id}` is not in expected format."
        for key in ("summary", "executive_summary", "analysis", "overview"):
            value = summary.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if len(text) > 500:
                    text = text[:497] + "..."
                return f"Stored listing summary for `{listing_id}`:\n{text}"
        rendered = json.dumps(summary, ensure_ascii=True)
        if len(rendered) > 500:
            rendered = rendered[:497] + "..."
        return f"Stored listing summary payload for `{listing_id}`:\n{rendered}"

    def _render_trip_research_reply(self, location: str, payload: Dict[str, Any]) -> str:
        warning = str(payload.get("warning") or "").strip()
        activities = payload.get("activities") if isinstance(payload.get("activities"), list) else []
        if warning == "tavily_api_key_missing":
            return (
                "Trip research tool is configured but disabled because Tavily API key is missing.\n"
                "Set `RENTAL_TAVILY_API_KEY` (or `TAVILY_API_KEY`) and retry."
            )
        if not activities:
            details = f" ({warning})" if warning else ""
            return f"I couldn't find Tripadvisor-style activities for `{location}`{details}."

        shown = activities[:8]
        lines: List[str] = [f"Top activity candidates for `{location}` ({len(shown)} shown):"]
        for item in shown:
            rating = item.get("rating")
            rating_count = item.get("rating_count")
            rating_text = f"{rating}/5" if rating is not None else "n/a"
            count_text = f"{rating_count} reviews" if rating_count is not None else "reviews n/a"
            lines.append(
                f"- {item.get('name') or 'Untitled'} | {item.get('category') or 'things_to_do'} | "
                f"rating {rating_text} ({count_text})"
            )
            if item.get("source_url"):
                lines.append(f"  {item.get('source_url')}")
        if warning:
            lines.append(f"Note: {warning}")
        return "\n".join(lines)

    def _render_personality_rag_context_reply(self, payload: Dict[str, Any]) -> str:
        hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
        if not hits:
            return "No memory context found."
        lines: List[str] = ["Memory context matches:"]
        for index, item in enumerate(hits[:5], start=1):
            citation = item.get("citation") if isinstance(item.get("citation"), dict) else {}
            title = str(citation.get("title") or "Untitled memory").strip()
            filename = str(citation.get("filename") or "").strip()
            label = f"{title} ({filename})" if filename else title
            score = item.get("score")
            text = str(item.get("text") or "").strip().replace("\n", " ")
            if len(text) > 220:
                text = text[:217] + "..."
            lines.append(f"{index}. {label} | score={score}")
            lines.append(f"   {text}")
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        tags = profile.get("top_tags") if isinstance(profile.get("top_tags"), list) else []
        if tags:
            tag_text = ", ".join(
                [f"{str(item.get('tag') or '').strip()} ({int(item.get('count') or 0)})" for item in tags[:5]]
            )
            lines.append(f"Top memory tags: {tag_text}")
        return "\n".join(lines)

    def _summarize_job_metrics(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "count": len(items),
            "by_status": {},
            "by_job_type": {},
            "averages": {},
        }
        if not items:
            return out

        per_status: Dict[str, int] = {}
        per_job_type: Dict[str, Dict[str, Any]] = {}
        all_capture_ms: List[float] = []
        all_job_ms: List[float] = []
        all_nav_ms: List[float] = []
        all_parse_ms: List[float] = []
        all_persist_ms: List[float] = []
        drift_detected = 0

        for item in items:
            status = str(item.get("status") or "unknown")
            job_type = str(item.get("job_type") or "unknown")
            per_status[status] = int(per_status.get(status, 0) or 0) + 1

            metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
            parser_drift = metrics.get("parser_drift") if isinstance(metrics.get("parser_drift"), dict) else {}
            if parser_drift.get("drift_detected"):
                drift_detected += 1

            capture_duration_ms = _to_float(metrics.get("capture_duration_ms"))
            job_total_ms = _to_float(metrics.get("job_total_ms"))
            parse_ms = _to_float(metrics.get("parse_ms"))
            persist_ms = _to_float(metrics.get("persist_ms"))
            capture_timings = metrics.get("capture_timings") if isinstance(metrics.get("capture_timings"), dict) else {}
            navigation_ms = _to_float(capture_timings.get("navigation_ms"))

            bucket = per_job_type.setdefault(
                job_type,
                {
                    "count": 0,
                    "capture_duration_ms": [],
                    "job_total_ms": [],
                    "navigation_ms": [],
                    "parse_ms": [],
                    "persist_ms": [],
                },
            )
            bucket["count"] = int(bucket.get("count") or 0) + 1
            if capture_duration_ms is not None:
                bucket["capture_duration_ms"].append(capture_duration_ms)
                all_capture_ms.append(capture_duration_ms)
            if job_total_ms is not None:
                bucket["job_total_ms"].append(job_total_ms)
                all_job_ms.append(job_total_ms)
            if navigation_ms is not None:
                bucket["navigation_ms"].append(navigation_ms)
                all_nav_ms.append(navigation_ms)
            if parse_ms is not None:
                bucket["parse_ms"].append(parse_ms)
                all_parse_ms.append(parse_ms)
            if persist_ms is not None:
                bucket["persist_ms"].append(persist_ms)
                all_persist_ms.append(persist_ms)

        out["by_status"] = per_status
        out["by_job_type"] = {
            key: {
                "count": value.get("count"),
                "avg_capture_duration_ms": _avg(value.get("capture_duration_ms") or []),
                "avg_job_total_ms": _avg(value.get("job_total_ms") or []),
                "avg_navigation_ms": _avg(value.get("navigation_ms") or []),
                "avg_parse_ms": _avg(value.get("parse_ms") or []),
                "avg_persist_ms": _avg(value.get("persist_ms") or []),
            }
            for key, value in per_job_type.items()
        }
        out["averages"] = {
            "avg_capture_duration_ms": _avg(all_capture_ms),
            "avg_job_total_ms": _avg(all_job_ms),
            "avg_navigation_ms": _avg(all_nav_ms),
            "avg_parse_ms": _avg(all_parse_ms),
            "avg_persist_ms": _avg(all_persist_ms),
        }
        out["parser_drift_detected_count"] = drift_detected
        return out

    def _render_pipeline_health_reply(self, payload: Dict[str, Any]) -> str:
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        count = int(summary.get("count") or 0)
        by_status = summary.get("by_status") if isinstance(summary.get("by_status"), dict) else {}
        averages = summary.get("averages") if isinstance(summary.get("averages"), dict) else {}
        drift_count = int(summary.get("parser_drift_detected_count") or 0)

        complete = int(by_status.get("complete") or 0)
        failed = int(by_status.get("failed") or 0)
        running = int(by_status.get("running") or 0)
        queued = int(by_status.get("queued") or 0)

        capture_ms = averages.get("avg_capture_duration_ms")
        nav_ms = averages.get("avg_navigation_ms")
        parse_ms = averages.get("avg_parse_ms")
        persist_ms = averages.get("avg_persist_ms")

        return (
            "Pipeline health snapshot:\n"
            f"- Jobs analyzed: {count}\n"
            f"- Status: complete={complete}, failed={failed}, running={running}, queued={queued}\n"
            f"- Avg capture: {capture_ms if capture_ms is not None else 'n/a'} ms\n"
            f"- Avg navigation: {nav_ms if nav_ms is not None else 'n/a'} ms\n"
            f"- Avg parse/persist: "
            f"{parse_ms if parse_ms is not None else 'n/a'} / "
            f"{persist_ms if persist_ms is not None else 'n/a'} ms\n"
            f"- Parser drift detections: {drift_count}"
        )
