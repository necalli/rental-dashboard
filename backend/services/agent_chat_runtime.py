import asyncio
import inspect
import json
import os
import queue
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from .agent_chat import AgentChatOrchestrator
from .agent_skills import enabled_tool_names, load_skill_packages, skill_system_prompt
from .search_assist import SearchAssistService
from .storage import Storage

try:
    from claude_agent_sdk import (
        AgentDefinition,
        AssistantMessage as SdkAssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage as SdkResultMessage,
        TextBlock as SdkTextBlock,
        create_sdk_mcp_server,
        tool as sdk_tool,
    )
    try:
        from claude_agent_sdk import HookMatcher
    except Exception:
        HookMatcher = None  # type: ignore[assignment]
    CLAUDE_AGENT_SDK_AVAILABLE = True
    CLAUDE_AGENT_SDK_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - optional dependency
    AgentDefinition = None  # type: ignore[assignment]
    SdkAssistantMessage = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment]
    ClaudeSDKClient = None  # type: ignore[assignment]
    HookMatcher = None  # type: ignore[assignment]
    SdkResultMessage = None  # type: ignore[assignment]
    SdkTextBlock = None  # type: ignore[assignment]
    create_sdk_mcp_server = None  # type: ignore[assignment]
    sdk_tool = None  # type: ignore[assignment]
    CLAUDE_AGENT_SDK_AVAILABLE = False
    CLAUDE_AGENT_SDK_IMPORT_ERROR = str(exc)

URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+",
    flags=re.UNICODE,
)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
LISTING_URL_RE = re.compile(r"/rooms/([0-9]{6,})", re.IGNORECASE)
SDK_BUILTIN_TOOL_NAMES = {
    "Agent",
    "AskUserQuestion",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Read",
    "Skill",
    "StructuredOutput",
    "Task",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
}


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(fallback)


def _parse_csv_tokens(raw: Any) -> List[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    output: List[str] = []
    seen = set()
    for part in text.split(","):
        value = str(part or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _text_snippet(value: Any, limit: int = 600) -> str:
    raw: str
    if value is None:
        return ""
    if isinstance(value, bytes):
        raw = value.decode("utf-8", errors="ignore")
    else:
        raw = str(value)
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _exception_with_stdio_debug(exc: BaseException, *, limit: int = 600) -> str:
    seen = set()
    segments: List[str] = []

    def _append_from_exception(err: BaseException) -> None:
        marker = id(err)
        if marker in seen:
            return
        seen.add(marker)
        segments.append(f"{type(err).__name__}: {_text_snippet(err, limit=limit)}")

        for attr in ("returncode", "exit_code", "code"):
            if hasattr(err, attr):
                value = getattr(err, attr, None)
                if value is not None:
                    segments.append(f"{attr}={value}")
                    break

        for attr in ("cmd", "command"):
            if hasattr(err, attr):
                value = _text_snippet(getattr(err, attr, None), limit=limit)
                if value:
                    segments.append(f"{attr}={value}")
                    break

        stderr_text = ""
        for attr in ("stderr", "error_output"):
            if hasattr(err, attr):
                stderr_text = _text_snippet(getattr(err, attr, None), limit=limit)
                if stderr_text:
                    break
        if stderr_text:
            segments.append(f"stderr={stderr_text}")

        stdout_text = ""
        for attr in ("stdout", "output"):
            if hasattr(err, attr):
                stdout_text = _text_snippet(getattr(err, attr, None), limit=limit)
                if stdout_text:
                    break
        if stdout_text:
            segments.append(f"stdout={stdout_text}")

        payload = getattr(err, "args", ())
        if isinstance(payload, tuple):
            for item in payload:
                if isinstance(item, dict):
                    stderr_v = _text_snippet(item.get("stderr"), limit=limit)
                    stdout_v = _text_snippet(item.get("stdout"), limit=limit)
                    command_v = _text_snippet(item.get("command") or item.get("cmd"), limit=limit)
                    if command_v:
                        segments.append(f"args.command={command_v}")
                    if stderr_v:
                        segments.append(f"args.stderr={stderr_v}")
                    if stdout_v:
                        segments.append(f"args.stdout={stdout_v}")

        cause = getattr(err, "__cause__", None)
        if isinstance(cause, BaseException):
            _append_from_exception(cause)
        context = getattr(err, "__context__", None)
        if isinstance(context, BaseException):
            _append_from_exception(context)

    _append_from_exception(exc)
    unique: List[str] = []
    dedupe = set()
    for item in segments:
        if item in dedupe:
            continue
        dedupe.add(item)
        unique.append(item)
    return " | ".join(unique)


class ClaudeSkillRuntime:
    def __init__(self, orchestrator: AgentChatOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.api_key = os.getenv("RENTAL_CLAUDE_API_KEY", "").strip()
        self.model = os.getenv("RENTAL_CLAUDE_MODEL", "claude-sonnet-4-6").strip()
        self.base_url = os.getenv("RENTAL_CLAUDE_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
        self.timeout_seconds = max(5, int(os.getenv("RENTAL_CLAUDE_TIMEOUT_SECONDS", "45") or 45))
        self.max_turns = max(1, int(os.getenv("RENTAL_CLAUDE_MAX_TURNS", "5") or 5))
        self.max_tokens = max(256, int(os.getenv("RENTAL_CLAUDE_MAX_TOKENS", "900") or 900))
        self.max_history_messages = max(2, int(os.getenv("RENTAL_AGENT_SESSION_HISTORY", "12") or 12))
        self.fanout_enabled = _to_bool(os.getenv("RENTAL_AGENT_FANOUT_ENABLED"), False)
        self.fanout_max_workers = max(1, _to_int(os.getenv("RENTAL_AGENT_FANOUT_MAX_WORKERS"), 3))
        self.fanout_timeout_ms = max(250, _to_int(os.getenv("RENTAL_AGENT_FANOUT_TIMEOUT_MS"), 10000))
        self.clean_response_style = _to_bool(os.getenv("RENTAL_AGENT_CLEAN_RESPONSE_STYLE"), True)
        self.skills = load_skill_packages()
        self.default_enabled_tools = enabled_tool_names(self.skills)
        self.default_system_prompt = skill_system_prompt(self.skills)
        self.rag_skill_enabled = _to_bool(os.getenv("RENTAL_RAG_SKILL_ENABLED"), True)
        self.rag_skill_scope = str(os.getenv("RENTAL_RAG_SKILL_SCOPE", "planning") or "planning").strip().lower()
        self.personality_context_tool = "tool.personality_rag_context"
        self.personality_upsert_tool = "tool.personality_rag_upsert"
        self._sessions: Dict[str, List[Dict[str, Any]]] = {}
        self._claude_to_internal: Dict[str, str] = {}
        self.background_job_ttl_seconds = max(
            300,
            _to_int(os.getenv("RENTAL_AGENT_BACKGROUND_JOB_TTL_SECONDS"), 7200),
        )
        self._session_scope: Dict[str, Dict[str, Any]] = {}
        self._session_background_jobs: Dict[str, Dict[str, Any]] = {}
        self._session_state_loaded: Dict[str, float] = {}

    def _storage_get_agent_session_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        storage = getattr(self.orchestrator, "storage", None)
        getter = getattr(storage, "get_agent_session_state", None) if storage is not None else None
        if not callable(getter):
            return None
        try:
            value = getter(session_id)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _storage_upsert_agent_session_state(self, session_id: str, payload: Dict[str, Any]) -> None:
        storage = getattr(self.orchestrator, "storage", None)
        setter = getattr(storage, "upsert_agent_session_state", None) if storage is not None else None
        if not callable(setter):
            return
        try:
            setter(session_id, payload)
        except Exception:
            return

    def _load_session_state(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        if sid in self._session_state_loaded:
            return
        payload = self._storage_get_agent_session_state(sid)
        self._session_state_loaded[sid] = time.time()
        if not isinstance(payload, dict):
            return

        history = payload.get("history")
        if isinstance(history, list):
            normalized_history = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip()
                content = item.get("content")
                if not role:
                    continue
                normalized_history.append({"role": role, "content": content})
            if normalized_history:
                self._sessions[sid] = normalized_history[-self.max_history_messages :]

        session_scope = payload.get("session_scope")
        if isinstance(session_scope, dict):
            self._session_scope[sid] = dict(session_scope)

        background_jobs = payload.get("background_jobs")
        if isinstance(background_jobs, dict):
            self._session_background_jobs[sid] = dict(background_jobs)

        sdk_session = str(payload.get("sdk_session_id") or "").strip()
        if sdk_session and hasattr(self, "_sdk_sessions"):
            sdk_sessions = getattr(self, "_sdk_sessions", None)
            if isinstance(sdk_sessions, dict):
                sdk_sessions[sid] = sdk_session

    def _persist_session_state(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        payload: Dict[str, Any] = {
            "history": self._sessions.get(sid) if isinstance(self._sessions.get(sid), list) else [],
            "session_scope": self._session_scope.get(sid) if isinstance(self._session_scope.get(sid), dict) else {},
            "background_jobs": self._session_background_jobs.get(sid)
            if isinstance(self._session_background_jobs.get(sid), dict)
            else {},
        }
        if hasattr(self, "_sdk_sessions"):
            sdk_sessions = getattr(self, "_sdk_sessions", None)
            if isinstance(sdk_sessions, dict):
                payload["sdk_session_id"] = str(sdk_sessions.get(sid) or "").strip()
        self._storage_upsert_agent_session_state(sid, payload)

    def _remember_session_scope(self, session_id: str, tool_scope_debug: Dict[str, Any]) -> None:
        sid = str(session_id or "").strip()
        if not sid or not isinstance(tool_scope_debug, dict):
            return
        selected_ids = tool_scope_debug.get("selected_skill_ids")
        selected_names = tool_scope_debug.get("selected_skill_names")
        self._session_scope[sid] = {
            "selected_skill_ids": selected_ids if isinstance(selected_ids, list) else [],
            "selected_skill_names": selected_names if isinstance(selected_names, list) else [],
            "selection_source": str(tool_scope_debug.get("selection_source") or "").strip() or "unknown",
            "updated_at": time.time(),
        }

    def _get_session_scope(self, session_id: str) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        value = self._session_scope.get(sid)
        if not isinstance(value, dict):
            return None
        return value

    def _is_terminal_job_status(self, status: Any) -> bool:
        value = str(status or "").strip().lower()
        return value in {"complete", "completed", "failed", "cancelled", "canceled"}

    def _looks_like_uuid(self, value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(UUID_RE.fullmatch(text))

    def _get_background_job_state(self, session_id: str, *, create: bool = False) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        state = self._session_background_jobs.get(sid)
        if isinstance(state, dict):
            created_at = _to_float(state.get("created_at"), 0.0)
            updated_at = _to_float(state.get("updated_at"), created_at)
            if updated_at > 0 and (time.time() - updated_at) > float(self.background_job_ttl_seconds):
                self._session_background_jobs.pop(sid, None)
                state = None
        if not isinstance(state, dict) and create:
            now = time.time()
            state = {
                "created_at": now,
                "updated_at": now,
                "latest_search_job_id": "",
                "latest_search_run_id": "",
                "jobs": {},
            }
            self._session_background_jobs[sid] = state
        if not isinstance(state, dict):
            return None
        jobs = state.get("jobs")
        if not isinstance(jobs, dict):
            state["jobs"] = {}
        return state

    def _remember_background_jobs(self, session_id: str, tool_results: List[Dict[str, Any]]) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        jobs = self._extract_post_loop_jobs(tool_results)
        if not jobs:
            return
        state = self._get_background_job_state(sid, create=True)
        if not isinstance(state, dict):
            return
        state_jobs = state.get("jobs")
        if not isinstance(state_jobs, dict):
            state_jobs = {}
            state["jobs"] = state_jobs

        now = time.time()
        for item in jobs:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("job_id") or "").strip()
            if not job_id:
                continue
            status = str(item.get("status") or "").strip().lower() or "queued"
            job_type = str(item.get("job_type") or "").strip().lower()
            tool_name = str(item.get("tool") or "").strip()
            existing = state_jobs.get(job_id) if isinstance(state_jobs.get(job_id), dict) else {}
            run_id = str(item.get("run_id") or existing.get("run_id") or "").strip()
            state_jobs[job_id] = {
                "job_id": job_id,
                "job_type": job_type or str(existing.get("job_type") or "").strip(),
                "tool": tool_name or str(existing.get("tool") or "").strip(),
                "status": status,
                "run_id": run_id,
                "updated_at": now,
            }
            if job_type == "search" or tool_name == "tool.search_create":
                state["latest_search_job_id"] = job_id
                if run_id:
                    state["latest_search_run_id"] = run_id
        state["updated_at"] = now

    def _background_debug_snapshot(self, session_id: str) -> Dict[str, Any]:
        state = self._get_background_job_state(session_id)
        if not isinstance(state, dict):
            return {
                "latest_search_job_id": "",
                "latest_search_run_id": "",
                "tracked_job_count": 0,
            }
        jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
        return {
            "latest_search_job_id": str(state.get("latest_search_job_id") or "").strip(),
            "latest_search_run_id": str(state.get("latest_search_run_id") or "").strip(),
            "tracked_job_count": len(jobs),
        }

    def _refresh_background_search_job(
        self,
        *,
        session_id: str,
        job_id: str,
        tool_calls: List[Dict[str, Any]],
        warnings: List[str],
        citations: List[str],
    ) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        target = str(job_id or "").strip()
        if not sid or not target:
            return None
        state = self._get_background_job_state(sid, create=True)
        if not isinstance(state, dict):
            return None
        state_jobs = state.get("jobs")
        if not isinstance(state_jobs, dict):
            state_jobs = {}
            state["jobs"] = state_jobs

        tool_input = {"job_id": target}
        job = self.orchestrator.execute_tool(
            "tool.job_get",
            tool_input,
            tool_calls=tool_calls,
            warnings=warnings,
        )
        citation = self.orchestrator.get_tool_citation("tool.job_get", tool_input)
        if citation and citation not in citations:
            citations.append(citation)
        if not isinstance(job, dict):
            return None

        status = str(job.get("status") or "").strip().lower()
        job_type = str(job.get("job_type") or "").strip().lower()
        result_ref = str(job.get("result_ref") or "").strip()
        run_id = result_ref if (job_type == "search" and self._looks_like_uuid(result_ref)) else ""
        now = time.time()
        current = state_jobs.get(target) if isinstance(state_jobs.get(target), dict) else {}
        merged = {
            "job_id": target,
            "job_type": job_type or str(current.get("job_type") or "").strip(),
            "tool": str(current.get("tool") or "").strip(),
            "status": status or str(current.get("status") or "").strip().lower() or "unknown",
            "run_id": run_id or str(current.get("run_id") or "").strip(),
            "updated_at": now,
        }
        state_jobs[target] = merged
        state["updated_at"] = now
        if merged.get("job_type") == "search":
            state["latest_search_job_id"] = target
            if merged.get("run_id"):
                state["latest_search_run_id"] = str(merged.get("run_id"))
        return merged

    def _resolve_search_run_id_from_background(
        self,
        *,
        session_id: str,
        requested_run_id: str,
        tool_calls: List[Dict[str, Any]],
        warnings: List[str],
        citations: List[str],
    ) -> str:
        sid = str(session_id or "").strip()
        if not sid:
            return str(requested_run_id or "").strip()
        state = self._get_background_job_state(sid)
        if not isinstance(state, dict):
            return str(requested_run_id or "").strip()
        state_jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
        requested = str(requested_run_id or "").strip()

        if requested and requested in state_jobs:
            entry = state_jobs.get(requested) if isinstance(state_jobs.get(requested), dict) else {}
            run_id = str(entry.get("run_id") or "").strip()
            if not run_id:
                refreshed = self._refresh_background_search_job(
                    session_id=sid,
                    job_id=requested,
                    tool_calls=tool_calls,
                    warnings=warnings,
                    citations=citations,
                )
                run_id = str((refreshed or {}).get("run_id") or "").strip()
            if run_id:
                self._append_warning_once(warnings, "search_job_id_promoted_to_run_id")
                return run_id
            status = str((entry or {}).get("status") or "").strip().lower()
            if status in {"queued", "running"}:
                self._append_warning_once(warnings, "search_job_pending_completion")
            return requested

        if requested:
            return requested

        latest_run = str(state.get("latest_search_run_id") or "").strip()
        if latest_run:
            self._append_warning_once(warnings, "search_run_id_reused_from_session")
            return latest_run

        latest_job = str(state.get("latest_search_job_id") or "").strip()
        if latest_job:
            refreshed = self._refresh_background_search_job(
                session_id=sid,
                job_id=latest_job,
                tool_calls=tool_calls,
                warnings=warnings,
                citations=citations,
            )
            run_id = str((refreshed or {}).get("run_id") or "").strip()
            if run_id:
                self._append_warning_once(warnings, "search_run_id_reused_from_session")
                return run_id
            status = str((refreshed or {}).get("status") or "").strip().lower()
            if status in {"queued", "running"}:
                self._append_warning_once(warnings, "search_job_pending_completion")
        return requested

    def _rewrite_tool_input_from_background_context(
        self,
        *,
        session_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_calls: List[Dict[str, Any]],
        warnings: List[str],
        citations: List[str],
    ) -> Dict[str, Any]:
        internal_tool = str(tool_name or "").strip()
        if internal_tool == "tool.listing_ingest_url":
            return self._rewrite_listing_ingest_tool_input_from_background_context(
                session_id=session_id,
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                tool_calls=tool_calls,
                warnings=warnings,
                citations=citations,
            )
        if internal_tool not in {"tool.search_listings_list", "tool.search_run_get", "tool.search_ingest_listings"}:
            return tool_input if isinstance(tool_input, dict) else {}
        updated = dict(tool_input or {})
        current_run_id = str(updated.get("run_id") or "").strip()
        resolved_run_id = self._resolve_search_run_id_from_background(
            session_id=session_id,
            requested_run_id=current_run_id,
            tool_calls=tool_calls,
            warnings=warnings,
            citations=citations,
        )
        if resolved_run_id and resolved_run_id != current_run_id:
            updated["run_id"] = resolved_run_id
        return updated

    def _extract_listing_id_from_url(self, url: Any) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        match = LISTING_URL_RE.search(text)
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    def _run_param_value(self, run_params: Dict[str, Any], key: str) -> Any:
        aliases: Dict[str, Tuple[str, ...]] = {
            "check_in": ("check_in", "checkin"),
            "check_out": ("check_out", "checkout"),
            "adults": ("adults",),
            "children": ("children",),
            "infants": ("infants",),
            "pets": ("pets",),
            "currency": ("currency", "pricing_currency"),
        }
        names = aliases.get(str(key), (str(key),))
        for name in names:
            if name not in run_params:
                continue
            value = run_params.get(name)
            if value is None or value == "":
                continue
            return value
        return None

    def _rewrite_listing_ingest_tool_input_from_background_context(
        self,
        *,
        session_id: str,
        tool_input: Dict[str, Any],
        tool_calls: List[Dict[str, Any]],
        warnings: List[str],
        citations: List[str],
    ) -> Dict[str, Any]:
        updated = dict(tool_input or {})
        if any(
            updated.get(key) not in (None, "")
            for key in (
                "check_in",
                "check_out",
                "checkin",
                "checkout",
                "adults",
                "children",
                "infants",
                "pets",
                "currency",
                "pricing_currency",
            )
        ):
            return updated

        url = str(updated.get("url") or "").strip()
        if not url:
            return updated

        run_id = self._resolve_search_run_id_from_background(
            session_id=session_id,
            requested_run_id="",
            tool_calls=tool_calls,
            warnings=warnings,
            citations=citations,
        )
        if not run_id:
            return updated

        storage = getattr(self.orchestrator, "storage", None)
        list_runs = getattr(storage, "list_search_runs", None) if storage is not None else None
        list_listings = getattr(storage, "list_search_listings", None) if storage is not None else None
        if not callable(list_runs) or not callable(list_listings):
            return updated

        try:
            runs = list_runs(limit=500)
        except Exception:
            return updated
        run = next(
            (
                item
                for item in (runs or [])
                if isinstance(item, dict) and str(item.get("run_id") or "").strip() == run_id
            ),
            None,
        )
        run_params = run.get("params") if isinstance(run, dict) and isinstance(run.get("params"), dict) else {}
        if not run_params:
            return updated

        listing_id = self._extract_listing_id_from_url(url)
        if listing_id:
            try:
                candidates = list_listings(run_id, limit=5000)
            except Exception:
                candidates = []
            matched = False
            for item in candidates or []:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("id") or item.get("listing_id") or "").strip()
                if candidate_id and candidate_id == listing_id:
                    matched = True
                    break
            if not matched:
                return updated

        applied = 0
        for key in ("check_in", "check_out", "adults", "children", "infants", "pets", "currency"):
            value = self._run_param_value(run_params, key)
            if value is None or value == "":
                continue
            updated[key] = value
            applied += 1
        if applied > 0:
            self._append_warning_once(warnings, "listing_ingest_url_enriched_with_latest_run_params")
        return updated

    def _append_warning_once(self, warnings: List[str], warning: str) -> None:
        value = str(warning or "").strip()
        if not value:
            return
        if value not in warnings:
            warnings.append(value)

    def _select_enabled_tools_model_first(self) -> Tuple[List[str], Dict[str, Any]]:
        scoped_skills = [skill for skill in self.skills if bool(skill.get("enabled"))]
        selected = enabled_tool_names(scoped_skills)
        if not selected:
            selected = list(self.default_enabled_tools or [])

        personality_present = self.personality_context_tool in selected or self.personality_upsert_tool in selected
        allow_context = bool(self.rag_skill_enabled)
        allow_upsert = bool(self.rag_skill_enabled and self.rag_skill_scope not in {"planning"})
        if personality_present:
            if not allow_context and self.personality_context_tool in selected:
                selected = [tool for tool in selected if tool != self.personality_context_tool]
            if not allow_upsert and self.personality_upsert_tool in selected:
                selected = [tool for tool in selected if tool != self.personality_upsert_tool]

        selected_skill_ids = [str(skill.get("skill_id") or "").strip() for skill in scoped_skills if skill.get("skill_id")]
        selected_skill_names = [str(skill.get("name") or "").strip() for skill in scoped_skills if skill.get("name")]
        return selected, {
            "selected_skill_count": len(selected_skill_ids),
            "selected_skill_ids": selected_skill_ids,
            "selected_skill_names": selected_skill_names,
            "selected_tool_count": len(selected),
            "selected_tools": selected,
            "selection_source": "model_first",
            "rag_skill_enabled": bool(self.rag_skill_enabled),
            "rag_skill_scope": self.rag_skill_scope,
            "rag_personality_tools_available": personality_present,
            "allow_personality_context": allow_context,
            "allow_personality_upsert": allow_upsert,
        }

    def _fanout_debug_template(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.fanout_enabled),
            "attempted": False,
            "planned_branch_count": 0,
            "completed_branch_count": 0,
            "failed_branch_count": 0,
            "timeout_branch_count": 0,
            "max_workers": int(self.fanout_max_workers),
            "timeout_ms": int(self.fanout_timeout_ms),
            "branches": [],
        }

    def _build_fanout_plan(
        self,
        *,
        message: str,
        tool_results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        del message
        if not self.fanout_enabled:
            return []

        already_called = {
            str(item.get("tool") or "").strip()
            for item in (tool_results or [])
            if isinstance(item, dict) and str(item.get("tool") or "").strip()
        }
        ops_tools = {"tool.metrics_jobs", "tool.jobs_list", "tool.search_runs_list"}
        if not (already_called & ops_tools):
            return []
        has_search_signal = any(
            tool_name.startswith("tool.search_") or tool_name in {"tool.listing_ingest_url"}
            for tool_name in already_called
        )

        plan: List[Dict[str, Any]] = []
        if "tool.metrics_jobs" not in already_called:
            plan.append(
                {
                    "branch_id": "ops_metrics",
                    "branch_type": "ops_snapshot",
                    "workflow": "ops_snapshot",
                    "tool": "tool.metrics_jobs",
                    "input": {"limit": 60, "summary_limit": 240},
                }
            )
        if "tool.jobs_list" not in already_called:
            plan.append(
                {
                    "branch_id": "ops_jobs",
                    "branch_type": "ops_snapshot",
                    "workflow": "ops_snapshot",
                    "tool": "tool.jobs_list",
                    "input": {"limit": 10},
                }
            )
        if has_search_signal and "tool.search_runs_list" not in already_called:
            plan.append(
                {
                    "branch_id": "ops_runs",
                    "branch_type": "ops_snapshot",
                    "workflow": "ops_snapshot",
                    "tool": "tool.search_runs_list",
                    "input": {"limit": 6},
                }
            )

        # Keep fan-out strictly parallel; skip single-branch overhead.
        if len(plan) < 2:
            return []
        return plan

    def _execute_fanout_plan(self, plan: List[Dict[str, Any]]) -> Dict[str, Any]:
        debug = self._fanout_debug_template()
        if not self.fanout_enabled or not isinstance(plan, list) or not plan:
            return debug

        debug["attempted"] = True
        debug["planned_branch_count"] = len(plan)
        timeout_seconds = float(self.fanout_timeout_ms) / 1000.0

        def _run_branch(item: Dict[str, Any]) -> Dict[str, Any]:
            branch_id = str(item.get("branch_id") or str(uuid.uuid4()))
            branch_type = str(item.get("branch_type") or "").strip()
            workflow = str(item.get("workflow") or "").strip()
            tool_name = str(item.get("tool") or "").strip()
            tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
            local_calls: List[Dict[str, Any]] = []
            local_warnings: List[str] = []
            result = self.orchestrator.execute_tool(
                tool_name,
                tool_input,
                tool_calls=local_calls,
                warnings=local_warnings,
            )
            call_meta = local_calls[-1] if local_calls else {}
            citation = self.orchestrator.get_tool_citation(tool_name, tool_input)
            return {
                "branch_id": branch_id,
                "branch_type": branch_type,
                "workflow": workflow,
                "tool": tool_name,
                "input": tool_input,
                "result": result,
                "citation": citation,
                "tool_calls": local_calls,
                "warnings": local_warnings,
                "ok": bool(call_meta.get("ok")),
                "timeout": bool(call_meta.get("timeout")),
                "latency_ms": call_meta.get("latency_ms"),
            }

        branches: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(self.fanout_max_workers, len(plan))) as executor:
            future_to_branch = {executor.submit(_run_branch, item): item for item in plan}
            for future, seed in future_to_branch.items():
                branch_id = str((seed or {}).get("branch_id") or str(uuid.uuid4()))
                try:
                    branch = future.result(timeout=timeout_seconds)
                except FuturesTimeoutError:
                    branch = {
                        "branch_id": branch_id,
                        "branch_type": str((seed or {}).get("branch_type") or "").strip(),
                        "workflow": str((seed or {}).get("workflow") or "").strip(),
                        "tool": str((seed or {}).get("tool") or "").strip(),
                        "input": (seed or {}).get("input") if isinstance((seed or {}).get("input"), dict) else {},
                        "result": None,
                        "citation": None,
                        "tool_calls": [],
                        "warnings": ["fanout_branch_timeout"],
                        "ok": False,
                        "timeout": True,
                        "latency_ms": self.fanout_timeout_ms,
                    }
                except Exception as exc:
                    branch = {
                        "branch_id": branch_id,
                        "branch_type": str((seed or {}).get("branch_type") or "").strip(),
                        "workflow": str((seed or {}).get("workflow") or "").strip(),
                        "tool": str((seed or {}).get("tool") or "").strip(),
                        "input": (seed or {}).get("input") if isinstance((seed or {}).get("input"), dict) else {},
                        "result": None,
                        "citation": None,
                        "tool_calls": [],
                        "warnings": [f"fanout_branch_failed:{exc}"],
                        "ok": False,
                        "timeout": False,
                        "latency_ms": 0,
                    }
                branches.append(branch)

        debug["branches"] = branches
        debug["completed_branch_count"] = sum(1 for branch in branches if bool(branch.get("ok")))
        debug["failed_branch_count"] = sum(1 for branch in branches if not bool(branch.get("ok")))
        debug["timeout_branch_count"] = sum(1 for branch in branches if bool(branch.get("timeout")))
        return debug

    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(self, *, session_id: Optional[str], message: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")
        if not self.is_available():
            raise RuntimeError("claude runtime unavailable: missing RENTAL_CLAUDE_API_KEY")

        sid = str(session_id or "").strip() or str(uuid.uuid4())
        self._load_session_state(sid)
        resolved_user_id = str(user_id or "").strip() or os.getenv("RENTAL_RAG_DEFAULT_USER_ID", "default-user").strip()
        trace_id = str(uuid.uuid4())
        history = self._sessions.get(sid) or []
        history.append({"role": "user", "content": text})
        history = history[-self.max_history_messages :]

        messages = self._history_to_messages(history)
        warnings: List[str] = []
        fanout_debug = self._fanout_debug_template()
        router_debug: Dict[str, Any] = {
            "enabled": False,
            "attempted": False,
            "status": "model_first",
        }
        selected_tools, tool_scope_debug = self._select_enabled_tools_model_first()
        tools = self._claude_tool_definitions(selected_tools or None)
        selected_skill_ids = tool_scope_debug.get("selected_skill_ids")
        system_prompt = skill_system_prompt(
            self.skills,
            selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else None,
        )

        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        citations: List[str] = []
        reply: Optional[str] = None

        for _ in range(self.max_turns):
            response = self._call_claude(messages=messages, tools=tools, system_prompt=system_prompt)
            blocks = response.get("content") if isinstance(response.get("content"), list) else []
            tool_use_blocks = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
            text_blocks = [
                str(block.get("text") or "").strip()
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text_blocks = [item for item in text_blocks if item]

            if tool_use_blocks:
                messages.append({"role": "assistant", "content": blocks})
                tool_result_blocks: List[Dict[str, Any]] = []
                for block in tool_use_blocks:
                    tool_name = str(block.get("name") or "").strip()
                    internal_tool_name = self._claude_to_internal.get(tool_name, tool_name)
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if internal_tool_name in {self.personality_context_tool, self.personality_upsert_tool}:
                        tool_input = dict(tool_input)
                        tool_input.setdefault("user_id", resolved_user_id)
                        if internal_tool_name == self.personality_context_tool:
                            query_text = str(tool_input.get("query") or "").strip()
                            if not query_text:
                                tool_input["query"] = text
                    tool_input = self._rewrite_tool_input_from_background_context(
                        session_id=sid,
                        tool_name=internal_tool_name,
                        tool_input=tool_input if isinstance(tool_input, dict) else {},
                        tool_calls=tool_calls,
                        warnings=warnings,
                        citations=citations,
                    )
                    result = self.orchestrator.execute_tool(
                        internal_tool_name,
                        tool_input,
                        tool_calls=tool_calls,
                        warnings=warnings,
                    )
                    tool_results.append({"tool": internal_tool_name, "result": result})
                    citation = self.orchestrator.get_tool_citation(internal_tool_name, tool_input)
                    if citation and citation not in citations:
                        citations.append(citation)
                    result_text = self._tool_result_text(result)
                    latest = tool_calls[-1] if tool_calls else {}
                    is_error = not bool(latest.get("ok"))
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": str(block.get("id") or ""),
                            "content": result_text,
                            "is_error": bool(is_error),
                        }
                    )
                messages.append({"role": "user", "content": tool_result_blocks})
                continue

            if text_blocks:
                reply = "\n".join(text_blocks).strip()
                break

        if not reply:
            warnings.append("claude_no_final_text")
            fallback = self._render_post_loop_finalizer(tool_results)
            if fallback:
                reply = fallback
                warnings.append("post_loop_finalizer_used")
            else:
                reply = (
                    "I couldn't complete the agent reasoning loop cleanly. "
                    "Please retry with a narrower request."
                )
        else:
            reply = self._apply_post_tool_guardrails(reply, tool_results, warnings)
            reply = self._apply_execution_claim_guardrails(reply, tool_results, warnings)

        if self.fanout_enabled:
            plan = self._build_fanout_plan(message=text, tool_results=tool_results)
            fanout_debug = self._execute_fanout_plan(plan)
            for branch in fanout_debug.get("branches") or []:
                if not isinstance(branch, dict):
                    continue
                branch_calls = branch.get("tool_calls") if isinstance(branch.get("tool_calls"), list) else []
                branch_warnings = branch.get("warnings") if isinstance(branch.get("warnings"), list) else []
                for call in branch_calls:
                    if isinstance(call, dict):
                        tool_calls.append(call)
                for warning in branch_warnings:
                    value = str(warning or "").strip()
                    if value:
                        warnings.append(value)
                citation = str(branch.get("citation") or "").strip()
                if citation and citation not in citations:
                    citations.append(citation)
                if str(branch.get("tool") or "").strip():
                    tool_results.append({"tool": str(branch.get("tool")), "result": branch.get("result")})
            reply = self._apply_fanout_enrichment(reply, fanout_debug, warnings)
        reply = self._apply_reply_style_guardrails(reply, warnings)
        self._remember_background_jobs(sid, tool_results)
        self._remember_session_scope(sid, tool_scope_debug)

        history.append({"role": "assistant", "content": reply})
        self._sessions[sid] = history[-self.max_history_messages :]
        self._persist_session_state(sid)

        failure_count = sum(1 for call in tool_calls if not bool(call.get("ok")))
        timeout_count = sum(1 for call in tool_calls if bool(call.get("timeout")))
        return {
            "session_id": sid,
            "trace_id": trace_id,
            "reply": reply,
            "citations": citations,
            "debug": {
                "intent": "llm_tool_orchestration",
                "entities": {},
                "tool_calls": tool_calls,
                "warnings": warnings,
                "latency_ms": None,
                "runtime": "claude",
                "model": self.model,
                "skills": [
                    skill.get("skill_id")
                    for skill in self.skills
                    if skill.get("enabled")
                ],
                "tool_scope": {**tool_scope_debug, "rag_user_id": resolved_user_id},
                "skill_router": router_debug,
                "fanout": fanout_debug,
                "background": self._background_debug_snapshot(sid),
                "guardrails": {
                    "degraded": failure_count > 0,
                    "tool_call_count": len(tool_calls),
                    "tool_failure_count": failure_count,
                    "tool_timeout_count": timeout_count,
                    "default_timeout_ms": self.orchestrator.tool_timeout_ms_default,
                },
            },
        }

    def stream_chat(
        self,
        *,
        session_id: Optional[str],
        message: str,
        user_id: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")
        if not self.is_available():
            if CLAUDE_AGENT_SDK_AVAILABLE:
                raise RuntimeError("claude agent sdk runtime unavailable: missing RENTAL_CLAUDE_API_KEY")
            raise RuntimeError(
                f"claude agent sdk runtime unavailable: missing claude_agent_sdk package ({CLAUDE_AGENT_SDK_IMPORT_ERROR})"
            )

        sid = str(session_id or "").strip() or str(uuid.uuid4())
        self._load_session_state(sid)
        resolved_user_id = str(user_id or "").strip() or os.getenv("RENTAL_RAG_DEFAULT_USER_ID", "default-user").strip()
        trace_id = str(uuid.uuid4())
        history = self._sessions.get(sid) or []
        history.append({"role": "user", "content": text})
        history = history[-self.max_history_messages :]

        warnings: List[str] = []
        fanout_debug = self._fanout_debug_template()
        router_debug: Dict[str, Any] = {
            "enabled": False,
            "attempted": False,
            "status": "sdk_model_first",
        }
        selected_tools, tool_scope_debug = self._select_enabled_tools_model_first()
        selected_skill_ids = tool_scope_debug.get("selected_skill_ids")
        if hasattr(self, "_sdk_system_prompt"):
            system_prompt = self._sdk_system_prompt(  # type: ignore[attr-defined]
                selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else []
            )
        else:
            system_prompt = skill_system_prompt(
                self.skills,
                selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else None,
            )

        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        citations: List[str] = []
        reply: Optional[str] = None
        sdk_meta: Dict[str, Any] = {}

        event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        marker = "__sdk_stream_done__"
        worker_result: Dict[str, Any] = {}

        def _emit(item: Dict[str, Any]) -> None:
            if not isinstance(item, dict):
                return
            event_queue.put(item)

        def _worker() -> None:
            try:
                payload = asyncio.run(
                    self._run_sdk_agent(
                        sid=sid,
                        message=text,
                        system_prompt=system_prompt,
                        selected_tools=selected_tools or [],
                        selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else [],
                        resolved_user_id=resolved_user_id,
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        warnings=warnings,
                        citations=citations,
                        event_sink=_emit,
                    )
                )
                worker_result["payload"] = payload
            except Exception as exc:
                worker_result["error"] = exc
            finally:
                event_queue.put({"event": marker})

        thread = threading.Thread(target=_worker, name="agent-sdk-stream", daemon=True)
        thread.start()

        while True:
            item = event_queue.get()
            if not isinstance(item, dict):
                continue
            if str(item.get("event") or "") == marker:
                break
            yield item

        if worker_result.get("error") is not None:
            detail = _exception_with_stdio_debug(worker_result["error"])
            warnings.append("agent_sdk_stream_failed")
            yield {"event": "error", "error": f"claude_agent_sdk_runtime_error:{detail}"}
            fallback = self._render_post_loop_finalizer(tool_results)
            if fallback:
                reply = fallback
                warnings.append("post_loop_finalizer_used")
            else:
                reply = (
                    "I couldn't complete the agent reasoning loop cleanly. "
                    "Please retry with a narrower request."
                )
        else:
            payload = worker_result.get("payload")
            if isinstance(payload, dict):
                reply = str(payload.get("reply") or "").strip() or None
                sdk_meta = payload.get("sdk_meta") if isinstance(payload.get("sdk_meta"), dict) else {}

            if not reply:
                warnings.append("claude_no_final_text")
                fallback = self._render_post_loop_finalizer(tool_results)
                if fallback:
                    reply = fallback
                    warnings.append("post_loop_finalizer_used")
                else:
                    reply = (
                        "I couldn't complete the agent reasoning loop cleanly. "
                        "Please retry with a narrower request."
                    )
            else:
                reply = self._apply_post_tool_guardrails(reply, tool_results, warnings)

        if self.fanout_enabled:
            plan = self._build_fanout_plan(message=text, tool_results=tool_results)
            fanout_debug = self._execute_fanout_plan(plan)
            for branch in fanout_debug.get("branches") or []:
                if not isinstance(branch, dict):
                    continue
                branch_calls = branch.get("tool_calls") if isinstance(branch.get("tool_calls"), list) else []
                branch_warnings = branch.get("warnings") if isinstance(branch.get("warnings"), list) else []
                for call in branch_calls:
                    if isinstance(call, dict):
                        tool_calls.append(call)
                for warning in branch_warnings:
                    value = str(warning or "").strip()
                    if value:
                        warnings.append(value)
                citation = str(branch.get("citation") or "").strip()
                if citation and citation not in citations:
                    citations.append(citation)
                if str(branch.get("tool") or "").strip():
                    tool_results.append({"tool": str(branch.get("tool")), "result": branch.get("result")})
            reply = self._apply_fanout_enrichment(reply, fanout_debug, warnings)
        reply = self._apply_reply_style_guardrails(reply or "", warnings)
        self._remember_background_jobs(sid, tool_results)
        self._remember_session_scope(sid, tool_scope_debug)

        history.append({"role": "assistant", "content": reply})
        self._sessions[sid] = history[-self.max_history_messages :]
        self._persist_session_state(sid)

        failure_count = sum(1 for call in tool_calls if not bool(call.get("ok")))
        timeout_count = sum(1 for call in tool_calls if bool(call.get("timeout")))
        final_response = {
            "session_id": sid,
            "trace_id": trace_id,
            "reply": reply,
            "citations": citations,
            "debug": {
                "intent": "llm_tool_orchestration",
                "entities": {},
                "tool_calls": tool_calls,
                "warnings": warnings,
                "latency_ms": None,
                "runtime": "claude_agent_sdk",
                "model": self.sdk_model,
                "sdk": sdk_meta,
                "sdk_config": {
                    "permission_mode": self.sdk_permission_mode,
                    "hooks_enabled": bool(self.sdk_hooks_enabled),
                    "hooks_mode": str(self.sdk_hooks_mode or ""),
                    "structured_output_enabled": bool(self.sdk_structured_output_enabled),
                    "max_budget_usd": self.sdk_max_budget_usd,
                    "model_first_routing": bool(getattr(self, "sdk_model_first_routing", False)),
                    "resume_enabled": bool(getattr(self, "sdk_resume_enabled", False)),
                    "continue_conversation_enabled": bool(getattr(self, "sdk_continue_conversation_enabled", False)),
                    "stream_passthrough_enabled": bool(getattr(self, "sdk_stream_passthrough_enabled", False)),
                    "subagents_enabled": bool(getattr(self, "sdk_subagents_enabled", False)),
                    "subagent_model": str(getattr(self, "sdk_subagent_model", "") or ""),
                    "allowed_builtins": list(getattr(self, "sdk_allowed_builtins", []) or []),
                    "tools_preset": str(getattr(self, "sdk_tools_preset", "") or ""),
                    "system_prompt_preset": str(getattr(self, "sdk_system_prompt_preset", "") or ""),
                    "setting_sources": list(getattr(self, "sdk_setting_sources", []) or []),
                    "system_prompt_mode": str(getattr(self, "sdk_system_prompt_mode", "") or ""),
                    "native_skills_enabled": bool(self.sdk_native_skills_enabled),
                    "native_skills_sync_enabled": bool(self.sdk_native_skills_sync_enabled),
                    "native_skills_dir": str(self.sdk_native_skills_dir),
                    "native_skills_sync": dict(self._sdk_native_skills_sync_meta),
                },
                "skills": [
                    skill.get("skill_id")
                    for skill in self.skills
                    if skill.get("enabled")
                ],
                "tool_scope": {**tool_scope_debug, "rag_user_id": resolved_user_id},
                "skill_router": router_debug,
                "fanout": fanout_debug,
                "background": self._background_debug_snapshot(sid),
                "guardrails": {
                    "degraded": failure_count > 0,
                    "tool_call_count": len(tool_calls),
                    "tool_failure_count": failure_count,
                    "tool_timeout_count": timeout_count,
                    "default_timeout_ms": self.orchestrator.tool_timeout_ms_default,
                },
            },
        }
        yield {"event": "done", "response": final_response}

    def _normalize_url_key(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = urlparse(text)
            host = str(parsed.netloc or "").lower().strip()
            path = str(parsed.path or "").rstrip("/")
            if not host:
                return text.lower()
            return f"{host}{path}".lower()
        except Exception:
            return text.lower()

    def _extract_trip_research_activities(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for item in reversed(tool_results):
            if str(item.get("tool") or "") != "tool.trip_research_tavily":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            activities = result.get("activities") if isinstance(result.get("activities"), list) else []
            normalized = [entry for entry in activities if isinstance(entry, dict)]
            if normalized:
                return normalized
        return []

    def _extract_listing_comparison(self, tool_results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for item in reversed(tool_results):
            if str(item.get("tool") or "") != "tool.listing_compare_create":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            summary = result.get("summary") if isinstance(result.get("summary"), dict) else None
            if summary:
                return summary
        return None

    def _extract_post_loop_listing_details(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        details: List[Dict[str, Any]] = []
        for item in tool_results:
            if str(item.get("tool") or "") != "tool.listing_get":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            if result:
                details.append(result)
        return details

    def _extract_post_loop_jobs(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in tool_results:
            tool_name = str(item.get("tool") or "").strip()
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            if not result:
                continue
            if tool_name == "tool.search_ingest_listings":
                jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
                run_id = str(result.get("run_id") or "").strip()
                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    job_id = str(job.get("job_id") or "").strip()
                    if not job_id:
                        continue
                    out.append(
                        {
                            "tool": tool_name,
                            "job_id": job_id,
                            "job_type": str(job.get("job_type") or "").strip(),
                            "status": str(job.get("status") or "").strip() or "queued",
                            "run_id": run_id,
                        }
                    )
                continue
            job_id = str(result.get("job_id") or "").strip()
            if not job_id:
                continue
            if tool_name not in {"tool.search_create", "tool.listing_ingest_url", "tool.listing_compare_create"}:
                continue
            out.append(
                {
                    "tool": tool_name,
                    "job_id": job_id,
                    "job_type": str(result.get("job_type") or "").strip(),
                    "status": str(result.get("status") or "").strip() or "queued",
                    "run_id": str(result.get("run_id") or "").strip(),
                }
            )
        return out

    def _extract_post_loop_listing_candidates(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for item in reversed(tool_results):
            tool_name = str(item.get("tool") or "").strip()
            if tool_name not in {"tool.search_listings_list", "tool.listings_list"}:
                continue
            result = item.get("result") if isinstance(item.get("result"), list) else []
            candidates = [entry for entry in result if isinstance(entry, dict)]
            if candidates:
                return candidates[:5]
        return []

    def _render_grounded_listing_compare_reply(self, summary: Dict[str, Any]) -> str:
        lines: List[str] = []
        headline = str(summary.get("summary") or "").strip()
        if headline:
            lines.append("## Listing Comparison")
            lines.append(headline)
            lines.append("")

        listing_notes = summary.get("listing_notes") if isinstance(summary.get("listing_notes"), list) else []
        title_by_id: Dict[str, str] = {}
        for item in listing_notes:
            if not isinstance(item, dict):
                continue
            listing_id = str(item.get("listing_id") or "").strip()
            title = str(item.get("title") or "").strip()
            if listing_id and title:
                title_by_id[listing_id] = title

        winner = summary.get("winner") if isinstance(summary.get("winner"), dict) else {}
        winner_id = str(winner.get("listing_id") or "").strip()
        winner_reason = str(winner.get("reason") or "").strip()
        lines.append("## Winner")
        if winner_id:
            winner_title = title_by_id.get(winner_id)
            if winner_title:
                lines.append(f"- `{winner_id}` ({winner_title})")
            else:
                lines.append(f"- `{winner_id}`")
        else:
            lines.append("- No clear winner selected")
        if winner_reason:
            lines.append(f"- Reason: {winner_reason}")
        lines.append("")

        sections = summary.get("sections") if isinstance(summary.get("sections"), list) else []
        if sections:
            lines.append("## Category Breakdown")
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_name = str(section.get("section") or "Section").strip()
                lines.append(f"### {section_name}")
                section_winner = str(section.get("winner_listing_id") or "").strip()
                if section_winner:
                    section_title = title_by_id.get(section_winner)
                    if section_title:
                        lines.append(f"- Winner: `{section_winner}` ({section_title})")
                    else:
                        lines.append(f"- Winner: `{section_winner}`")
                notes = section.get("notes") if isinstance(section.get("notes"), list) else []
                for note in notes:
                    text = str(note or "").strip()
                    if text:
                        lines.append(f"- {text}")
                lines.append("")

        if listing_notes:
            lines.append("## Listing Notes")
            for item in listing_notes:
                if not isinstance(item, dict):
                    continue
                listing_id = str(item.get("listing_id") or "").strip()
                title = str(item.get("title") or "").strip()
                header = f"{title} (`{listing_id}`)" if title else f"`{listing_id}`"
                lines.append(f"### {header}")

                pros = item.get("pros") if isinstance(item.get("pros"), list) else []
                if pros:
                    lines.append("- Pros:")
                    for text in pros:
                        value = str(text or "").strip()
                        if value:
                            lines.append(f"  - {value}")

                cons = item.get("cons") if isinstance(item.get("cons"), list) else []
                if cons:
                    lines.append("- Cons:")
                    for text in cons:
                        value = str(text or "").strip()
                        if value:
                            lines.append(f"  - {value}")

                watchouts = item.get("watchouts") if isinstance(item.get("watchouts"), list) else []
                if watchouts:
                    lines.append("- Watchouts:")
                    for text in watchouts:
                        value = str(text or "").strip()
                        if value:
                            lines.append(f"  - {value}")
                lines.append("")

        tradeoffs = summary.get("tradeoffs") if isinstance(summary.get("tradeoffs"), list) else []
        if tradeoffs:
            lines.append("## Tradeoffs")
            for item in tradeoffs:
                text = str(item or "").strip()
                if text:
                    lines.append(f"- {text}")
            lines.append("")

        confidence = str(summary.get("confidence") or "").strip()
        if confidence:
            lines.append(f"Confidence: {confidence}")

        return "\n".join(lines).strip()

    def _render_grounded_trip_reply(self, activities: List[Dict[str, Any]]) -> str:
        shown = activities[:5]
        lines: List[str] = ["Here are top activity options based on grounded trip-research results:"]
        for index, item in enumerate(shown, start=1):
            rating = item.get("rating")
            rating_count = item.get("rating_count")
            rating_text = f"{rating}/5" if rating is not None else "n/a"
            count_text = f"{rating_count} reviews" if rating_count is not None else "reviews n/a"
            lines.append(
                f"{index}. {item.get('name') or 'Untitled'}"
                f" ({item.get('category') or 'things_to_do'}, rating {rating_text}, {count_text})"
            )
            source_url = str(item.get("source_url") or "").strip()
            if source_url:
                lines.append(f"   {source_url}")
        return "\n".join(lines)

    def _render_post_loop_finalizer(self, tool_results: List[Dict[str, Any]]) -> str:
        comparison = self._extract_listing_comparison(tool_results)
        if comparison:
            return self._render_grounded_listing_compare_reply(comparison)

        activities = self._extract_trip_research_activities(tool_results)
        if activities:
            return self._render_grounded_trip_reply(activities)

        listing_details = self._extract_post_loop_listing_details(tool_results)
        if listing_details:
            shown = listing_details[:2]
            lines: List[str] = [
                "I completed the workflow but the final assistant narrative did not finalize. "
                "Here are grounded listing details:"
            ]
            for item in shown:
                listing_id = str(item.get("id") or item.get("listing_id") or "").strip() or "unknown"
                title = str(item.get("title") or "Untitled").strip()
                location_text = str(item.get("location") or "n/a")
                lines.append(f"- `{listing_id}` | {title} | {location_text}")
            lines.append("Tell me if you want me to ingest these listings or compare them now.")
            return "\n".join(lines).strip()

        jobs = self._extract_post_loop_jobs(tool_results)
        if jobs:
            lines = [
                "I executed your request, but the final assistant narrative did not finalize. "
                "Completed actions:"
            ]
            for job in jobs[:5]:
                tool_name = str(job.get("tool") or "").strip()
                label = "search job" if tool_name == "tool.search_create" else "listing ingest job"
                if tool_name == "tool.listing_compare_create":
                    label = "comparison job"
                lines.append(
                    f"- {label}: `{job.get('job_id')}` "
                    f"(type={job.get('job_type') or 'n/a'}, status={job.get('status') or 'n/a'})"
                )
            lines.append("I can check these job statuses or continue with details once they complete.")
            return "\n".join(lines).strip()

        candidates = self._extract_post_loop_listing_candidates(tool_results)
        if candidates:
            lines = [
                "I completed retrieval but the final narrative did not finalize. "
                "Candidate listings returned:"
            ]
            for item in candidates:
                listing_id = str(item.get("id") or item.get("listing_id") or "").strip() or "unknown"
                title = str(item.get("title") or "Untitled").strip()
                location = str(item.get("location") or "n/a")
                lines.append(f"- `{listing_id}` | {title} | {location}")
            lines.append("Tell me which two listings you want to inspect or ingest.")
            return "\n".join(lines).strip()

        return ""

    def _apply_post_tool_guardrails(
        self,
        reply: str,
        tool_results: List[Dict[str, Any]],
        warnings: List[str],
    ) -> str:
        comparison = self._extract_listing_comparison(tool_results)
        if comparison:
            warnings.append("listing_compare_grounded_render")
            return self._render_grounded_listing_compare_reply(comparison)

        activities = self._extract_trip_research_activities(tool_results)
        if not activities:
            return reply

        allowed_urls = {
            str(item.get("source_url") or "").strip()
            for item in activities
            if str(item.get("source_url") or "").strip()
        }
        if not allowed_urls:
            return reply
        allowed_keys = {self._normalize_url_key(value) for value in allowed_urls}
        found_urls = [match.group(0).rstrip(".,;") for match in URL_RE.finditer(str(reply or ""))]
        if not found_urls:
            return reply

        invalid_urls: List[str] = []
        for value in found_urls:
            key = self._normalize_url_key(value)
            if key and key not in allowed_keys:
                invalid_urls.append(value)

        if not invalid_urls:
            return reply

        warnings.append("trip_research_link_grounding_rewrite")
        return self._render_grounded_trip_reply(activities)

    def _tool_call_succeeded_for(self, tool_results: List[Dict[str, Any]], names: set) -> bool:
        for item in tool_results:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool") or "").strip()
            if tool_name not in names:
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            if tool_name == "tool.search_ingest_listings":
                jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
                if any(isinstance(job, dict) and str(job.get("job_id") or "").strip() for job in jobs):
                    return True
                continue
            job_id = str(result.get("job_id") or "").strip()
            if job_id:
                return True
            status = str(result.get("status") or "").strip().lower()
            if status in {"queued", "complete", "completed", "running"}:
                return True
        return False

    def _render_missing_ingest_execution_reply(self, tool_results: List[Dict[str, Any]]) -> str:
        candidates = self._extract_post_loop_listing_candidates(tool_results)
        if len(candidates) >= 2:
            first = candidates[0]
            second = candidates[1]
            first_id = str(first.get("id") or first.get("listing_id") or "").strip()
            second_id = str(second.get("id") or second.get("listing_id") or "").strip()
            return (
                "I shortlisted listings, but ingestion did not execute yet. "
                f"If you want, I can ingest these two now: `{first_id}` and `{second_id}`."
            ).strip()
        return (
            "I identified candidates, but ingestion did not execute yet. "
            "If you want, I can run ingestion now."
        )

    def _apply_execution_claim_guardrails(
        self,
        reply: str,
        tool_results: List[Dict[str, Any]],
        warnings: List[str],
    ) -> str:
        lowered = str(reply or "").lower()
        ingest_claim = (
            "ingesting" in lowered
            or "ingest queued" in lowered
            or "running ingest" in lowered
            or "ingest job" in lowered
        )
        if ingest_claim:
            ingest_done = self._tool_call_succeeded_for(
                tool_results,
                {"tool.search_ingest_listings", "tool.listing_ingest_url"},
            )
            if not ingest_done:
                warnings.append("side_effect_claim_rewrite_missing_ingest_execution")
                return self._render_missing_ingest_execution_reply(tool_results)

        search_claim = "search queued" in lowered or "queued search job" in lowered
        if search_claim:
            search_done = self._tool_call_succeeded_for(tool_results, {"tool.search_create"})
            if not search_done:
                warnings.append("side_effect_claim_rewrite_missing_search_execution")
                return (
                    "I prepared the search request, but no search queue action was executed yet. "
                    "If you want, I can queue the search now."
                )
        return reply

    def _render_fanout_ops_snapshot(self, fanout_debug: Dict[str, Any]) -> str:
        branches = fanout_debug.get("branches") if isinstance(fanout_debug.get("branches"), list) else []
        if not branches:
            return ""

        ops_branches = [
            branch
            for branch in branches
            if isinstance(branch, dict) and str(branch.get("branch_type") or "") == "ops_snapshot"
        ]
        if not ops_branches:
            return ""

        metrics_payload: Optional[Dict[str, Any]] = None
        jobs_payload: Optional[List[Dict[str, Any]]] = None
        runs_payload: Optional[List[Dict[str, Any]]] = None

        for branch in ops_branches:
            if not bool(branch.get("ok")):
                continue
            tool_name = str(branch.get("tool") or "").strip()
            result = branch.get("result")
            if tool_name == "tool.metrics_jobs" and isinstance(result, dict):
                metrics_payload = result
            elif tool_name == "tool.jobs_list" and isinstance(result, list):
                jobs_payload = [item for item in result if isinstance(item, dict)]
            elif tool_name == "tool.search_runs_list" and isinstance(result, list):
                runs_payload = [item for item in result if isinstance(item, dict)]

        lines: List[str] = []
        if metrics_payload:
            summary = metrics_payload.get("summary") if isinstance(metrics_payload.get("summary"), dict) else {}
            by_status = summary.get("by_status") if isinstance(summary.get("by_status"), dict) else {}
            lines.append("Parallel ops snapshot:")
            lines.append(
                "- Metrics status: "
                f"complete={int(by_status.get('complete') or 0)}, "
                f"failed={int(by_status.get('failed') or 0)}, "
                f"running={int(by_status.get('running') or 0)}, "
                f"queued={int(by_status.get('queued') or 0)}"
            )

        if jobs_payload is not None:
            status_counts: Dict[str, int] = {}
            for job in jobs_payload:
                status = str(job.get("status") or "unknown").strip().lower() or "unknown"
                status_counts[status] = int(status_counts.get(status, 0) or 0) + 1
            if not lines:
                lines.append("Parallel ops snapshot:")
            lines.append(
                "- Recent jobs: "
                f"total={len(jobs_payload)}, "
                f"running={int(status_counts.get('running') or 0)}, "
                f"queued={int(status_counts.get('queued') or 0)}, "
                f"failed={int(status_counts.get('failed') or 0)}"
            )

        if runs_payload is not None:
            if not lines:
                lines.append("Parallel ops snapshot:")
            lines.append(f"- Recent search runs returned: {len(runs_payload)}")

        if len(lines) <= 1:
            return ""
        return "\n".join(lines).strip()

    def _apply_fanout_enrichment(
        self,
        reply: str,
        fanout_debug: Dict[str, Any],
        warnings: List[str],
    ) -> str:
        if not isinstance(fanout_debug, dict) or not bool(fanout_debug.get("attempted")):
            return reply
        addendum = self._render_fanout_ops_snapshot(fanout_debug)
        if not addendum:
            return reply
        warnings.append("fanout_ops_snapshot_attached")
        base = str(reply or "").strip()
        if not base:
            return addendum
        return f"{base}\n\n{addendum}".strip()

    def _apply_reply_style_guardrails(self, reply: str, warnings: List[str]) -> str:
        text = str(reply or "")
        if not self.clean_response_style:
            return text
        cleaned = text.replace("\ufe0f", "").replace("\u200d", "")
        cleaned = EMOJI_RE.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()
        if cleaned != text:
            warnings.append("style_emoji_stripped")
        return cleaned

    def _history_to_messages(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for item in history:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            if not content:
                continue
            messages.append({"role": role, "content": [{"type": "text", "text": content}]})
        return messages[-self.max_history_messages :]

    def _tool_result_text(self, result: Any) -> str:
        try:
            payload = json.dumps(result, ensure_ascii=True)
        except Exception:
            payload = str(result)
        if len(payload) > 6000:
            return payload[:5997] + "..."
        return payload

    def _claude_tool_name(self, internal_name: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_-]", "_", str(internal_name or "").strip())
        value = re.sub(r"_+", "_", value).strip("_")
        if not value:
            value = "tool"
        return value[:64]

    def _claude_tool_definitions(self, tool_names: Optional[List[str]]) -> List[Dict[str, Any]]:
        base_defs = self.orchestrator.get_tool_definitions(tool_names=tool_names)
        output: List[Dict[str, Any]] = []
        used_names = set()
        self._claude_to_internal = {}
        for item in base_defs:
            internal_name = str(item.get("name") or "").strip()
            if not internal_name:
                continue
            candidate = self._claude_tool_name(internal_name)
            resolved = candidate
            suffix = 1
            while resolved in used_names:
                suffix += 1
                base = candidate[: max(1, 64 - len(str(suffix)) - 1)]
                resolved = f"{base}_{suffix}"
            used_names.add(resolved)
            self._claude_to_internal[resolved] = internal_name
            output.append(
                {
                    "name": resolved,
                    "description": str(item.get("description") or ""),
                    "input_schema": item.get("input_schema") or {"type": "object", "properties": {}},
                }
            )
        return output

    def _call_claude(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/messages"
        body = {
            "model": self.model,
            "max_tokens": int(max_tokens or self.max_tokens),
            "system": str(system_prompt or self.default_system_prompt),
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as res:
                payload = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"claude_http_error:{exc.code}:{raw}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"claude_connection_error:{exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"claude_runtime_error:{exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("claude_invalid_payload")
        return payload

    def parse_search_assist_prompt(self, prompt: str) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("claude_api_key_missing")
        text = str(prompt or "").strip()
        if not text:
            return {
                "status": "clarification_needed",
                "intent": {},
                "message": "Describe the listing search you want to run.",
                "unsupported_or_uncertain_requests": [],
                "confidence": 0.0,
            }
        system_prompt = (
            "You parse a single rental-listing search-bar prompt into a JSON object. "
            "This workflow is constrained to Airbnb-style listing searches only. "
            "Do not answer conversationally. Return JSON only, with no markdown. "
            "Schema: {\"status\":\"ready|clarification_needed|rejected\","
            "\"intent\":{\"location\":string,\"check_in\":YYYY-MM-DD,\"check_out\":YYYY-MM-DD,"
            "\"adults\":int,\"children\":int,\"infants\":int,\"pets\":int,"
            "\"min_price\":int,\"max_price\":int,\"room_type\":string,"
            "\"amenities\":[string],\"flexible_cancellation\":bool,"
            "\"min_bedrooms\":int,\"min_beds\":int,\"min_bathrooms\":int},"
            "\"message\":string,\"unsupported_or_uncertain_requests\":[string],\"confidence\":number}. "
            "Use status rejected for unrelated prompts, analysis/comparison requests, ingestion/capture requests, "
            "or prompts that are not asking to find rentals. "
            "Use clarification_needed when the prompt is search-related but missing a destination or has impossible dates. "
            "For destination-first prompts like 'Phoenicia July 18-25', set location to 'Phoenicia'. "
            "Do not invent dates. If year is omitted, use the current year unless that date range has already passed; "
            "then use next year. Current date: 2026-05-31. "
            "Default adults to 1 and children/infants/pets to 0 only when a search is otherwise valid. "
            "Keep amenities as plain labels such as 'hot tub' or 'wifi'."
        )
        response = self._call_claude(
            messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
            tools=[],
            system_prompt=system_prompt,
            max_tokens=700,
        )
        raw = self._extract_text_from_claude_response(response)
        payload = self._parse_json_object(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("claude_search_assist_invalid_json")
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"ready", "clarification_needed", "rejected"}:
            payload["status"] = "clarification_needed"
            payload["message"] = "I could not confidently parse that as a listing search."
        if not isinstance(payload.get("intent"), dict):
            payload["intent"] = {}
        if not isinstance(payload.get("unsupported_or_uncertain_requests"), list):
            payload["unsupported_or_uncertain_requests"] = []
        return payload

    def _extract_text_from_claude_response(self, response: Dict[str, Any]) -> str:
        blocks = response.get("content") if isinstance(response.get("content"), list) else []
        text_blocks = [
            str(block.get("text") or "").strip()
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join([item for item in text_blocks if item]).strip()

    def _parse_json_object(self, raw: str) -> Optional[Dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else None
        except Exception:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None


class ClaudeAgentSdkRuntime(ClaudeSkillRuntime):
    def __init__(self, orchestrator: AgentChatOrchestrator) -> None:
        super().__init__(orchestrator)
        self.sdk_model = str(os.getenv("RENTAL_AGENT_SDK_MODEL", self.model) or self.model).strip()
        self.sdk_permission_mode = str(
            os.getenv("RENTAL_AGENT_SDK_PERMISSION_MODE", "default") or "default"
        ).strip()
        alias_raw = str(os.getenv("RENTAL_AGENT_SDK_MCP_SERVER_ALIAS", "rental_ops") or "rental_ops").strip()
        alias = re.sub(r"[^a-zA-Z0-9_-]", "_", alias_raw).strip("_")
        self.sdk_server_alias = alias or "rental_ops"
        self.sdk_resume_enabled = _to_bool(os.getenv("RENTAL_AGENT_SDK_RESUME_ENABLED"), True)
        self.sdk_disallow_builtins = _to_bool(os.getenv("RENTAL_AGENT_SDK_DISALLOW_BUILTINS"), True)
        self.sdk_allowed_builtins = [
            item
            for item in _parse_csv_tokens(os.getenv("RENTAL_AGENT_SDK_ALLOWED_BUILTINS", ""))
            if item in SDK_BUILTIN_TOOL_NAMES
        ]
        self.sdk_hooks_enabled = _to_bool(os.getenv("RENTAL_AGENT_SDK_HOOKS_ENABLED"), True)
        self.sdk_hooks_mode = str(os.getenv("RENTAL_AGENT_SDK_HOOKS_MODE", "observability") or "observability").strip().lower()
        self.sdk_structured_output_enabled = _to_bool(os.getenv("RENTAL_AGENT_SDK_STRUCTURED_OUTPUT_ENABLED"), True)
        self.sdk_model_first_routing = _to_bool(os.getenv("RENTAL_AGENT_SDK_MODEL_FIRST_ROUTING"), True)
        self.sdk_hook_timeout_seconds = max(5, _to_int(os.getenv("RENTAL_AGENT_SDK_HOOK_TIMEOUT_SECONDS"), 45))
        self.sdk_continue_conversation_enabled = _to_bool(
            os.getenv("RENTAL_AGENT_SDK_CONTINUE_CONVERSATION_ENABLED"),
            True,
        )
        self.sdk_stream_passthrough_enabled = _to_bool(
            os.getenv("RENTAL_AGENT_SDK_STREAM_PASSTHROUGH_ENABLED"),
            True,
        )
        self.sdk_subagents_enabled = _to_bool(os.getenv("RENTAL_AGENT_SDK_SUBAGENTS_ENABLED"), True)
        self.sdk_subagent_model = str(os.getenv("RENTAL_AGENT_SDK_SUBAGENT_MODEL", "haiku") or "haiku").strip()
        raw_budget = _to_float(os.getenv("RENTAL_AGENT_SDK_MAX_BUDGET_USD"), 0.0)
        self.sdk_max_budget_usd = raw_budget if raw_budget > 0 else None
        self.sdk_cli_path = str(os.getenv("RENTAL_AGENT_SDK_CLI_PATH", "") or "").strip()
        self.sdk_native_skills_enabled = _to_bool(os.getenv("RENTAL_AGENT_SDK_NATIVE_SKILLS_ENABLED"), True)
        self.sdk_native_skills_sync_enabled = _to_bool(
            os.getenv("RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_ENABLED"),
            True,
        )
        self.sdk_tools_preset = str(os.getenv("RENTAL_AGENT_SDK_TOOLS_PRESET", "claude_code") or "claude_code").strip()
        self.sdk_system_prompt_preset = str(
            os.getenv("RENTAL_AGENT_SDK_SYSTEM_PROMPT_PRESET", "claude_code") or "claude_code"
        ).strip()
        self.sdk_system_prompt_mode = str(
            os.getenv("RENTAL_AGENT_SDK_SYSTEM_PROMPT_MODE", "preset") or "preset"
        ).strip().lower()
        self.sdk_system_prompt_append = str(os.getenv("RENTAL_AGENT_SDK_SYSTEM_PROMPT_APPEND", "") or "").strip()
        raw_setting_sources = str(
            os.getenv("RENTAL_AGENT_SDK_SETTING_SOURCES", "project" if self.sdk_native_skills_enabled else "")
            or ""
        ).strip()
        self.sdk_setting_sources = [
            str(item or "").strip()
            for item in raw_setting_sources.split(",")
            if str(item or "").strip()
        ]
        self.sdk_native_skills_sync_interval_seconds = max(
            5,
            _to_int(os.getenv("RENTAL_AGENT_SDK_NATIVE_SKILLS_SYNC_INTERVAL_SECONDS"), 30),
        )
        raw_native_dir = str(
            os.getenv("RENTAL_AGENT_SDK_NATIVE_SKILLS_DIR", ".claude/skills") or ".claude/skills"
        ).strip()
        native_dir = Path(raw_native_dir)
        if not native_dir.is_absolute():
            native_dir = Path(__file__).resolve().parents[2] / native_dir
        self.sdk_native_skills_dir = native_dir
        self._sdk_native_skills_last_sync_at = 0.0
        self._sdk_native_skills_sync_meta: Dict[str, Any] = {
            "attempted": False,
            "ok": True,
            "copied_count": 0,
            "unchanged_count": 0,
            "target_dir": str(self.sdk_native_skills_dir),
            "error": "",
        }
        self._sdk_sessions: Dict[str, str] = {}
        if self.sdk_native_skills_enabled and self.sdk_native_skills_sync_enabled:
            try:
                self._sync_native_skills_to_project_dir(force=True)
            except Exception as exc:
                self._sdk_native_skills_sync_meta.update(
                    {
                        "attempted": True,
                        "ok": False,
                        "error": _text_snippet(exc, limit=240),
                    }
                )

    def is_available(self) -> bool:
        return bool(self.api_key) and bool(CLAUDE_AGENT_SDK_AVAILABLE)

    def _sdk_structured_output_format(
        self,
        *,
        selected_skill_ids: List[str],
    ) -> Optional[Dict[str, Any]]:
        if not self.sdk_structured_output_enabled:
            return None
        skill_set = {str(item or "").strip() for item in (selected_skill_ids or []) if str(item or "").strip()}
        if not skill_set:
            return None
        return {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "workflow": {"type": "string"},
                    "answer": {"type": "string"},
                    "next_action": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["answer"],
                "additionalProperties": True,
            },
        }

    def _sdk_setting_sources(self) -> Optional[List[str]]:
        if not self.sdk_setting_sources:
            return None
        return list(self.sdk_setting_sources)

    def _sdk_system_prompt(self, *, selected_skill_ids: List[str]) -> Any:
        mode = str(self.sdk_system_prompt_mode or "preset").strip().lower()
        append_text = str(self.sdk_system_prompt_append or "").strip()
        if mode in {"legacy", "skills"}:
            return skill_system_prompt(
                self.skills,
                selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else None,
            )
        if mode in {"none", "off"}:
            return append_text or None
        if mode == "text":
            return append_text or None
        payload: Dict[str, Any] = {
            "type": "preset",
            "preset": self.sdk_system_prompt_preset or "claude_code",
        }
        if append_text:
            payload["append"] = append_text
        return payload

    def _sdk_disallowed_builtin_tools(self) -> List[str]:
        if not self.sdk_disallow_builtins:
            return []
        disallowed = [
            "Bash",
            "Read",
            "Write",
            "Edit",
            "WebSearch",
            "Agent",
        ]
        if not self.sdk_subagents_enabled:
            disallowed.append("Task")
        if not self.sdk_native_skills_enabled:
            disallowed.append("Skill")
        explicitly_allowed = {item for item in self.sdk_allowed_builtins if item in SDK_BUILTIN_TOOL_NAMES}
        return [item for item in disallowed if item not in explicitly_allowed]

    def _sdk_allowed_builtin_tools(self) -> List[str]:
        allowed: List[str] = []
        for item in self.sdk_allowed_builtins:
            value = str(item or "").strip()
            if not value or value not in SDK_BUILTIN_TOOL_NAMES:
                continue
            if value not in allowed:
                allowed.append(value)
        return allowed

    def _sdk_resume_options(self, sid: str) -> Dict[str, Any]:
        if not self.sdk_resume_enabled:
            return {}
        resume_session_id = str(self._sdk_sessions.get(str(sid or "").strip()) or "").strip()
        if not resume_session_id:
            return {}
        options: Dict[str, Any] = {"resume": resume_session_id}
        if self.sdk_continue_conversation_enabled:
            options["continue_conversation"] = True
        return options

    def _sdk_event_public_payload(self, event: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "event": "sdk_event",
            "type": str(type(event).__name__),
        }
        for attr in ("subtype", "session_id", "tool_name", "tool_use_id", "parent_tool_use_id"):
            value = getattr(event, attr, None)
            if value is not None and str(value).strip():
                payload[attr] = str(value).strip()
        raw = _text_snippet(event, limit=900)
        if raw:
            payload["raw"] = raw
        return payload

    def _sdk_skill_subagents(
        self,
        *,
        selected_skill_ids: List[str],
        internal_to_allowed_tool: Dict[str, str],
    ) -> Dict[str, Any]:
        if not self.sdk_subagents_enabled:
            return {}
        enabled = [item for item in self.skills if bool(item.get("enabled"))]
        if not enabled:
            return {}
        wanted = {str(value or "").strip() for value in (selected_skill_ids or []) if str(value or "").strip()}
        scoped = [item for item in enabled if not wanted or str(item.get("skill_id") or "").strip() in wanted]
        if not scoped:
            return {}

        output: Dict[str, Any] = {}
        for skill in scoped:
            skill_id = str(skill.get("skill_id") or "").strip()
            if not skill_id:
                continue
            description = str(skill.get("description") or "").strip() or f"Specialist for {skill_id}"
            instruction = str(skill.get("instruction") or "").strip()
            prompt_lines = [
                f"You are the specialist agent for `{skill_id}`.",
                "Follow the skill instructions exactly and ask concise clarifying questions when ids are missing.",
            ]
            if instruction:
                prompt_lines.append("")
                prompt_lines.append("Skill instructions:")
                prompt_lines.append(instruction)
            prompt_text = "\n".join(prompt_lines).strip()

            tools: List[str] = []
            for tool_name in skill.get("tools") if isinstance(skill.get("tools"), list) else []:
                internal_name = str(tool_name or "").strip()
                mapped = internal_to_allowed_tool.get(internal_name)
                if mapped and mapped not in tools:
                    tools.append(mapped)
            if self.sdk_native_skills_enabled and "Skill" not in tools:
                tools.append("Skill")
            if not tools:
                continue

            name = re.sub(r"[^a-zA-Z0-9_-]", "-", skill_id).strip("-") or skill_id
            definition_kwargs: Dict[str, Any] = {
                "description": description,
                "tools": tools,
                "prompt": prompt_text,
            }
            if self.sdk_subagent_model:
                definition_kwargs["model"] = self.sdk_subagent_model
            if AgentDefinition is not None:
                try:
                    output[name] = AgentDefinition(**definition_kwargs)
                    continue
                except Exception:
                    pass
            output[name] = definition_kwargs
        return output

    def _sync_native_skills_to_project_dir(self, *, force: bool = False) -> Dict[str, Any]:
        meta = {
            "attempted": False,
            "ok": True,
            "copied_count": 0,
            "unchanged_count": 0,
            "file_copied_count": 0,
            "file_unchanged_count": 0,
            "target_dir": str(self.sdk_native_skills_dir),
            "error": "",
        }
        if not self.sdk_native_skills_enabled or not self.sdk_native_skills_sync_enabled:
            self._sdk_native_skills_sync_meta = meta
            return meta

        now = time.time()
        if (
            not force
            and self._sdk_native_skills_last_sync_at > 0
            and (now - self._sdk_native_skills_last_sync_at) < float(self.sdk_native_skills_sync_interval_seconds)
        ):
            return dict(self._sdk_native_skills_sync_meta)

        meta["attempted"] = True
        source_skills = load_skill_packages(os.getenv("RENTAL_AGENT_SKILLS_DIR", "backend/agent_skills"))
        enabled_skills = [item for item in source_skills if bool(item.get("enabled"))]
        self.sdk_native_skills_dir.mkdir(parents=True, exist_ok=True)

        for skill in enabled_skills:
            src_path_raw = str(skill.get("path") or "").strip()
            skill_id = str(skill.get("skill_id") or "").strip() or Path(src_path_raw).parent.name
            if not src_path_raw or not skill_id:
                continue
            src_path = Path(src_path_raw)
            if not src_path.exists() or not src_path.is_file():
                continue
            src_skill_dir = src_path.parent
            if not src_skill_dir.exists() or not src_skill_dir.is_dir():
                continue
            dst_dir = self.sdk_native_skills_dir / skill_id
            dst_dir.mkdir(parents=True, exist_ok=True)
            skill_changed = False
            source_files = [path for path in sorted(src_skill_dir.rglob("*")) if path.is_file()]
            for source_file in source_files:
                rel_path = source_file.relative_to(src_skill_dir)
                target_file = dst_dir / rel_path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                unchanged = False
                if target_file.exists() and target_file.is_file():
                    try:
                        unchanged = source_file.read_bytes() == target_file.read_bytes()
                    except Exception:
                        unchanged = False
                if unchanged:
                    meta["file_unchanged_count"] = int(meta["file_unchanged_count"]) + 1
                    continue
                shutil.copyfile(source_file, target_file)
                meta["file_copied_count"] = int(meta["file_copied_count"]) + 1
                skill_changed = True

            if skill_changed:
                meta["copied_count"] = int(meta["copied_count"]) + 1
            else:
                meta["unchanged_count"] = int(meta["unchanged_count"]) + 1

        self._sdk_native_skills_last_sync_at = now
        self._sdk_native_skills_sync_meta = meta
        return meta

    def _build_sdk_mcp_tools(
        self,
        *,
        session_id: str,
        selected_tools: List[str],
        message: str,
        resolved_user_id: str,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        warnings: List[str],
        citations: List[str],
    ) -> Tuple[List[Any], Dict[str, str], List[str], Dict[str, str]]:
        if not CLAUDE_AGENT_SDK_AVAILABLE or sdk_tool is None:
            raise RuntimeError("claude_agent_sdk_unavailable")
        tool_defs = self.orchestrator.get_tool_definitions(tool_names=selected_tools or None)
        output_tools: List[Any] = []
        sdk_to_internal: Dict[str, str] = {}
        allowed_tools: List[str] = []
        internal_to_allowed_tool: Dict[str, str] = {}
        used_names = set()

        for item in tool_defs:
            internal_name = str(item.get("name") or "").strip()
            if not internal_name:
                continue
            candidate = self._claude_tool_name(internal_name)
            resolved = candidate
            suffix = 1
            while resolved in used_names:
                suffix += 1
                base = candidate[: max(1, 64 - len(str(suffix)) - 1)]
                resolved = f"{base}_{suffix}"
            used_names.add(resolved)
            sdk_to_internal[resolved] = internal_name

            description = str(item.get("description") or "").strip() or f"Execute {internal_name}"
            input_schema = item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {}
            if not input_schema:
                input_schema = {"type": "object", "properties": {}}

            def _factory(
                *,
                sdk_name: str,
                internal_tool_name: str,
                sdk_description: str,
                sdk_input_schema: Dict[str, Any],
            ) -> Any:
                @sdk_tool(sdk_name, sdk_description, sdk_input_schema)
                async def _wrapped(args: Any) -> Dict[str, Any]:
                    tool_input = args if isinstance(args, dict) else {}
                    if internal_tool_name in {self.personality_context_tool, self.personality_upsert_tool}:
                        tool_input = dict(tool_input)
                        tool_input.setdefault("user_id", resolved_user_id)
                        if internal_tool_name == self.personality_context_tool:
                            query_text = str(tool_input.get("query") or "").strip()
                            if not query_text:
                                tool_input["query"] = message
                    tool_input = self._rewrite_tool_input_from_background_context(
                        session_id=session_id,
                        tool_name=internal_tool_name,
                        tool_input=tool_input if isinstance(tool_input, dict) else {},
                        tool_calls=tool_calls,
                        warnings=warnings,
                        citations=citations,
                    )
                    local_calls: List[Dict[str, Any]] = []
                    local_warnings: List[str] = []
                    result = self.orchestrator.execute_tool(
                        internal_tool_name,
                        tool_input,
                        tool_calls=local_calls,
                        warnings=local_warnings,
                    )
                    tool_results.append({"tool": internal_tool_name, "result": result})
                    for call in local_calls:
                        if isinstance(call, dict):
                            tool_calls.append(call)
                    for warning in local_warnings:
                        value = str(warning or "").strip()
                        if value:
                            warnings.append(value)
                    citation = self.orchestrator.get_tool_citation(internal_tool_name, tool_input)
                    if citation and citation not in citations:
                        citations.append(citation)
                    latest = local_calls[-1] if local_calls else {}
                    is_error = not bool(latest.get("ok"))
                    return {
                        "content": [{"type": "text", "text": self._tool_result_text(result)}],
                        "is_error": bool(is_error),
                    }

                return _wrapped

            output_tools.append(
                _factory(
                    sdk_name=resolved,
                    internal_tool_name=internal_name,
                    sdk_description=description,
                    sdk_input_schema=input_schema,
                )
            )
            allowed_name = f"mcp__{self.sdk_server_alias}__{resolved}"
            allowed_tools.append(allowed_name)
            internal_to_allowed_tool[internal_name] = allowed_name
        return output_tools, sdk_to_internal, allowed_tools, internal_to_allowed_tool

    async def _run_sdk_agent(
        self,
        *,
        sid: str,
        message: str,
        system_prompt: Any,
        selected_tools: List[str],
        selected_skill_ids: List[str],
        resolved_user_id: str,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        warnings: List[str],
        citations: List[str],
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        if not CLAUDE_AGENT_SDK_AVAILABLE or ClaudeAgentOptions is None or ClaudeSDKClient is None:
            raise RuntimeError(f"claude_agent_sdk_import_error:{CLAUDE_AGENT_SDK_IMPORT_ERROR or 'missing_dependency'}")
        if create_sdk_mcp_server is None:
            raise RuntimeError("claude_agent_sdk_missing_mcp_server")

        sdk_tools, sdk_to_internal, allowed_mcp_tools, internal_to_allowed_tool = self._build_sdk_mcp_tools(
            session_id=sid,
            selected_tools=selected_tools,
            message=message,
            resolved_user_id=resolved_user_id,
            tool_calls=tool_calls,
            tool_results=tool_results,
            warnings=warnings,
            citations=citations,
        )

        def _resolve_internal_tool_name(tool_name: str) -> Optional[str]:
            value = str(tool_name or "").strip()
            prefix = f"mcp__{self.sdk_server_alias}__"
            if value.startswith(prefix):
                return sdk_to_internal.get(value[len(prefix) :])
            return sdk_to_internal.get(value)

        hooks_config = None
        if self.sdk_hooks_enabled and HookMatcher is not None:
            try:
                tool_matcher = f"mcp__{self.sdk_server_alias}__*"

                async def _hook_post_tool_use(input_data: Dict[str, Any], tool_use_id: str, context: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
                    tool_name = str(input_data.get("tool_name") or "").strip()
                    internal_name = _resolve_internal_tool_name(tool_name) or tool_name
                    tool_response = input_data.get("tool_response")
                    is_error = bool(isinstance(tool_response, dict) and tool_response.get("is_error"))
                    if is_error:
                        warnings.append(f"agent_sdk_post_tool_error:{internal_name}")
                    if event_sink is not None:
                        event_sink(
                            {
                                "event": "tool_result",
                                "tool": internal_name,
                                "ok": not is_error,
                            }
                        )
                    return {}

                async def _hook_post_tool_use_failure(input_data: Dict[str, Any], tool_use_id: str, context: Dict[str, Any]) -> Dict[str, Any]:  # noqa: ARG001
                    tool_name = str(input_data.get("tool_name") or "").strip()
                    internal_name = _resolve_internal_tool_name(tool_name) or tool_name
                    warnings.append(f"agent_sdk_tool_failure:{internal_name}")
                    if event_sink is not None:
                        event_sink(
                            {
                                "event": "tool_failure",
                                "tool": internal_name,
                            }
                        )
                    if self.sdk_hooks_mode == "guidance":
                        return {
                            "hookSpecificOutput": {
                                "hookEventName": "PostToolUseFailure",
                                "additionalContext": (
                                    f"Tool `{internal_name}` failed. "
                                    "Retry once with narrower arguments or ask for missing identifiers."
                                ),
                            }
                        }
                    return {}

                hooks_config = {
                    "PostToolUse": [HookMatcher(matcher=tool_matcher, hooks=[_hook_post_tool_use], timeout=self.sdk_hook_timeout_seconds)],
                    "PostToolUseFailure": [
                        HookMatcher(matcher=tool_matcher, hooks=[_hook_post_tool_use_failure], timeout=self.sdk_hook_timeout_seconds)
                    ],
                }
            except Exception as exc:
                hooks_config = None
                warnings.append("agent_sdk_hooks_init_failed")
                warnings.append(f"agent_sdk_hooks_init_error:{_text_snippet(exc, limit=200)}")
        elif self.sdk_hooks_enabled:
            warnings.append("agent_sdk_hooks_unavailable")

        effective_permission_mode = self.sdk_permission_mode or "default"
        is_root = False
        try:
            is_root = bool(hasattr(os, "geteuid") and os.geteuid() == 0)
        except Exception:
            is_root = False
        if is_root and str(effective_permission_mode).strip() == "bypassPermissions":
            effective_permission_mode = "default"
            warnings.append("agent_sdk_permission_mode_downgraded_for_root")

        try:
            sync_meta = self._sync_native_skills_to_project_dir(force=False)
        except Exception as exc:
            sync_meta = {
                "attempted": True,
                "ok": False,
                "copied_count": 0,
                "unchanged_count": 0,
                "target_dir": str(self.sdk_native_skills_dir),
                "error": _text_snippet(exc, limit=240),
            }
            self._sdk_native_skills_sync_meta = sync_meta
        if bool(sync_meta.get("attempted")) and not bool(sync_meta.get("ok", True)):
            self._append_warning_once(warnings, "agent_sdk_native_skills_sync_failed")
            error_text = str(sync_meta.get("error") or "").strip()
            if error_text:
                self._append_warning_once(warnings, f"agent_sdk_native_skills_sync_error:{error_text}")

        options_kwargs: Dict[str, Any] = {
            "model": self.sdk_model,
            "max_turns": self.max_turns,
            "permission_mode": effective_permission_mode,
            "cwd": str(Path(__file__).resolve().parents[2]),
            "env": {"ANTHROPIC_API_KEY": self.api_key},
            "mcp_servers": {
                self.sdk_server_alias: create_sdk_mcp_server(
                    name="rental-dashboard-tools",
                    version="1.0.0",
                    tools=sdk_tools,
                )
            },
            "allowed_tools": list(allowed_mcp_tools),
        }
        subagents = self._sdk_skill_subagents(
            selected_skill_ids=selected_skill_ids,
            internal_to_allowed_tool=internal_to_allowed_tool,
        )
        if subagents:
            options_kwargs["agents"] = subagents
            if "Task" not in options_kwargs["allowed_tools"]:
                options_kwargs["allowed_tools"].append("Task")
        if self.sdk_tools_preset:
            options_kwargs["tools"] = {"type": "preset", "preset": self.sdk_tools_preset}
        if system_prompt is not None:
            if isinstance(system_prompt, str):
                if system_prompt.strip():
                    options_kwargs["system_prompt"] = system_prompt.strip()
            else:
                options_kwargs["system_prompt"] = system_prompt
        if self.sdk_native_skills_enabled and "Skill" not in options_kwargs["allowed_tools"]:
            options_kwargs["allowed_tools"].append("Skill")
        for builtin_tool in self._sdk_allowed_builtin_tools():
            if builtin_tool not in options_kwargs["allowed_tools"]:
                options_kwargs["allowed_tools"].append(builtin_tool)
        setting_sources = self._sdk_setting_sources()
        if setting_sources:
            options_kwargs["setting_sources"] = setting_sources
        if self.sdk_max_budget_usd is not None:
            options_kwargs["max_budget_usd"] = float(self.sdk_max_budget_usd)
        structured_output = self._sdk_structured_output_format(selected_skill_ids=selected_skill_ids)
        if structured_output:
            options_kwargs["output_format"] = structured_output
        if event_sink is not None:
            options_kwargs["include_partial_messages"] = True
        sdk_stderr_lines: List[str] = []

        def _sdk_stderr(line: str) -> None:
            value = _text_snippet(line, limit=500)
            if not value:
                return
            sdk_stderr_lines.append(value)
            # Keep bounded to avoid large payloads.
            if len(sdk_stderr_lines) > 40:
                del sdk_stderr_lines[: len(sdk_stderr_lines) - 40]

        options_kwargs["stderr"] = _sdk_stderr
        if hooks_config is not None:
            options_kwargs["hooks"] = hooks_config
        disallowed_builtins = self._sdk_disallowed_builtin_tools()
        if disallowed_builtins:
            options_kwargs["disallowed_tools"] = disallowed_builtins
        supported_option_names: Optional[set] = None
        try:
            signature = inspect.signature(ClaudeAgentOptions.__init__)
            supported_option_names = {
                str(name)
                for name in signature.parameters.keys()
                if str(name) and str(name) != "self"
            }
        except Exception:
            supported_option_names = None

        if self.sdk_cli_path:
            cli_path = Path(self.sdk_cli_path)
            if cli_path.exists() and cli_path.is_dir():
                warnings.append("agent_sdk_cli_path_is_directory")
            else:
                cli_options = (
                    "cli_path",
                    "path_to_claude_code_executable",
                    "path_to_claude_executable",
                    "claude_cli_path",
                    "executable_path",
                )
                injected = False
                for candidate in cli_options:
                    if supported_option_names is None or candidate in supported_option_names:
                        options_kwargs[candidate] = self.sdk_cli_path
                        injected = True
                        break
                if not injected:
                    warnings.append("agent_sdk_cli_path_option_unsupported")
        session_options = self._sdk_resume_options(sid)
        resumed_from_session_id = str(session_options.get("resume") or "").strip() if session_options else ""
        if session_options:
            options_kwargs.update(session_options)

        filtered_options = dict(options_kwargs)
        if supported_option_names is not None:
            dropped = sorted(name for name in filtered_options.keys() if name not in supported_option_names)
            if dropped:
                warnings.append(f"agent_sdk_options_unsupported:{','.join(dropped)}")
            filtered_options = {key: value for key, value in filtered_options.items() if key in supported_option_names}
        options = ClaudeAgentOptions(**filtered_options)

        assistant_snapshots: List[str] = []
        last_snapshot = ""
        sdk_meta: Dict[str, Any] = {}
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt=message)
                async for event in client.receive_response():
                    if event_sink is not None and self.sdk_stream_passthrough_enabled:
                        event_sink(self._sdk_event_public_payload(event))
                    if SdkAssistantMessage is not None and isinstance(event, SdkAssistantMessage):
                        content = event.content if isinstance(event.content, list) else []
                        text_parts: List[str] = []
                        for block in content:
                            if SdkTextBlock is not None and isinstance(block, SdkTextBlock):
                                piece = str(block.text or "")
                                if piece:
                                    text_parts.append(piece)
                            else:
                                block_type = str(getattr(block, "type", type(block).__name__) or "").strip()
                                block_name = str(getattr(block, "name", "") or "").strip()
                                if event_sink is not None and block_type:
                                    event_sink(
                                        {
                                            "event": "assistant_block",
                                            "block_type": block_type,
                                            "name": block_name,
                                        }
                                    )
                        snapshot = "".join(text_parts).strip()
                        if snapshot and snapshot != last_snapshot:
                            if event_sink is not None:
                                delta = snapshot
                                if last_snapshot and snapshot.startswith(last_snapshot):
                                    delta = snapshot[len(last_snapshot) :]
                                if delta:
                                    event_sink({"event": "assistant_delta", "text": delta})
                                if self.sdk_stream_passthrough_enabled:
                                    event_sink({"event": "assistant_snapshot", "text": snapshot})
                            assistant_snapshots.append(snapshot)
                            last_snapshot = snapshot
                        continue
                    if SdkResultMessage is not None and isinstance(event, SdkResultMessage):
                        session_value = str(getattr(event, "session_id", "") or "").strip()
                        if session_value:
                            self._sdk_sessions[sid] = session_value
                        result_text = str(getattr(event, "result", "") or "").strip()
                        structured_output = getattr(event, "structured_output", None)
                        if isinstance(structured_output, dict):
                            answer = str(structured_output.get("answer") or "").strip()
                            if answer and not assistant_snapshots:
                                assistant_snapshots.append(answer)
                        if result_text and not assistant_snapshots:
                            assistant_snapshots.append(result_text)
                        subtype_value = str(getattr(event, "subtype", "") or "").strip()
                        if subtype_value == "error_max_structured_output_retries":
                            warnings.append("agent_sdk_structured_output_retry_exhausted")
                        sdk_meta = {
                            "session_id": session_value,
                            "duration_ms": getattr(event, "duration_ms", None),
                            "duration_api_ms": getattr(event, "duration_api_ms", None),
                            "num_turns": getattr(event, "num_turns", None),
                            "total_cost_usd": getattr(event, "total_cost_usd", None),
                            "is_error": bool(getattr(event, "is_error", False)),
                            "subtype": getattr(event, "subtype", None),
                            "structured_output": structured_output if isinstance(structured_output, dict) else None,
                            "resume_enabled": bool(self.sdk_resume_enabled),
                            "resumed_from_session_id": resumed_from_session_id,
                            "continue_conversation_enabled": bool(self.sdk_continue_conversation_enabled),
                            "subagents_enabled": bool(self.sdk_subagents_enabled and bool(subagents)),
                            "subagent_count": len(subagents),
                        }
                        if event_sink is not None:
                            event_sink({"event": "sdk_result", "sdk": sdk_meta})
        except Exception as exc:
            stderr_blob = " || ".join(sdk_stderr_lines[-8:]).strip()
            if stderr_blob:
                raise RuntimeError(f"agent_sdk_stderr:{stderr_blob}") from exc
            raise
        final_reply = ""
        if assistant_snapshots:
            final_reply = str(assistant_snapshots[-1] or "").strip()
        return {"reply": final_reply, "sdk_meta": sdk_meta}

    def chat(self, *, session_id: Optional[str], message: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message is required")
        if not self.is_available():
            if CLAUDE_AGENT_SDK_AVAILABLE:
                raise RuntimeError("claude agent sdk runtime unavailable: missing RENTAL_CLAUDE_API_KEY")
            raise RuntimeError(
                f"claude agent sdk runtime unavailable: missing claude_agent_sdk package ({CLAUDE_AGENT_SDK_IMPORT_ERROR})"
            )

        sid = str(session_id or "").strip() or str(uuid.uuid4())
        self._load_session_state(sid)
        resolved_user_id = str(user_id or "").strip() or os.getenv("RENTAL_RAG_DEFAULT_USER_ID", "default-user").strip()
        trace_id = str(uuid.uuid4())
        history = self._sessions.get(sid) or []
        history.append({"role": "user", "content": text})
        history = history[-self.max_history_messages :]

        warnings: List[str] = []
        fanout_debug = self._fanout_debug_template()
        router_debug: Dict[str, Any] = {
            "enabled": False,
            "attempted": False,
            "status": "sdk_model_first",
        }
        selected_tools, tool_scope_debug = self._select_enabled_tools_model_first()
        selected_skill_ids = tool_scope_debug.get("selected_skill_ids")
        system_prompt = self._sdk_system_prompt(
            selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else []
        )

        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        citations: List[str] = []
        reply: Optional[str] = None
        sdk_meta: Dict[str, Any] = {}

        try:
            payload = asyncio.run(
                self._run_sdk_agent(
                    sid=sid,
                    message=text,
                    system_prompt=system_prompt,
                    selected_tools=selected_tools or [],
                    selected_skill_ids=selected_skill_ids if isinstance(selected_skill_ids, list) else [],
                    resolved_user_id=resolved_user_id,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    warnings=warnings,
                    citations=citations,
                )
            )
        except Exception as exc:
            detail = _exception_with_stdio_debug(exc)
            raise RuntimeError(f"claude_agent_sdk_runtime_error:{detail}") from exc

        if isinstance(payload, dict):
            reply = str(payload.get("reply") or "").strip() or None
            sdk_meta = payload.get("sdk_meta") if isinstance(payload.get("sdk_meta"), dict) else {}

        if not reply:
            warnings.append("claude_no_final_text")
            fallback = self._render_post_loop_finalizer(tool_results)
            if fallback:
                reply = fallback
                warnings.append("post_loop_finalizer_used")
            else:
                reply = (
                    "I couldn't complete the agent reasoning loop cleanly. "
                    "Please retry with a narrower request."
                )
        else:
            reply = self._apply_post_tool_guardrails(reply, tool_results, warnings)
            reply = self._apply_execution_claim_guardrails(reply, tool_results, warnings)

        if self.fanout_enabled:
            plan = self._build_fanout_plan(message=text, tool_results=tool_results)
            fanout_debug = self._execute_fanout_plan(plan)
            for branch in fanout_debug.get("branches") or []:
                if not isinstance(branch, dict):
                    continue
                branch_calls = branch.get("tool_calls") if isinstance(branch.get("tool_calls"), list) else []
                branch_warnings = branch.get("warnings") if isinstance(branch.get("warnings"), list) else []
                for call in branch_calls:
                    if isinstance(call, dict):
                        tool_calls.append(call)
                for warning in branch_warnings:
                    value = str(warning or "").strip()
                    if value:
                        warnings.append(value)
                citation = str(branch.get("citation") or "").strip()
                if citation and citation not in citations:
                    citations.append(citation)
                if str(branch.get("tool") or "").strip():
                    tool_results.append({"tool": str(branch.get("tool")), "result": branch.get("result")})
            reply = self._apply_fanout_enrichment(reply, fanout_debug, warnings)
        reply = self._apply_reply_style_guardrails(reply, warnings)
        self._remember_background_jobs(sid, tool_results)
        self._remember_session_scope(sid, tool_scope_debug)

        history.append({"role": "assistant", "content": reply})
        self._sessions[sid] = history[-self.max_history_messages :]
        self._persist_session_state(sid)

        failure_count = sum(1 for call in tool_calls if not bool(call.get("ok")))
        timeout_count = sum(1 for call in tool_calls if bool(call.get("timeout")))
        return {
            "session_id": sid,
            "trace_id": trace_id,
            "reply": reply,
            "citations": citations,
            "debug": {
                "intent": "llm_tool_orchestration",
                "entities": {},
                "tool_calls": tool_calls,
                "warnings": warnings,
                "latency_ms": None,
                "runtime": "claude_agent_sdk",
                "model": self.sdk_model,
                "sdk": sdk_meta,
                "sdk_config": {
                    "permission_mode": self.sdk_permission_mode,
                    "hooks_enabled": bool(self.sdk_hooks_enabled),
                    "hooks_mode": str(self.sdk_hooks_mode or ""),
                    "structured_output_enabled": bool(self.sdk_structured_output_enabled),
                    "max_budget_usd": self.sdk_max_budget_usd,
                    "model_first_routing": bool(self.sdk_model_first_routing),
                    "resume_enabled": bool(self.sdk_resume_enabled),
                    "continue_conversation_enabled": bool(self.sdk_continue_conversation_enabled),
                    "stream_passthrough_enabled": bool(self.sdk_stream_passthrough_enabled),
                    "subagents_enabled": bool(self.sdk_subagents_enabled),
                    "subagent_model": str(self.sdk_subagent_model or ""),
                    "allowed_builtins": list(self.sdk_allowed_builtins or []),
                    "tools_preset": str(self.sdk_tools_preset or ""),
                    "system_prompt_preset": str(self.sdk_system_prompt_preset or ""),
                    "setting_sources": list(self.sdk_setting_sources or []),
                    "system_prompt_mode": str(self.sdk_system_prompt_mode or ""),
                    "native_skills_enabled": bool(self.sdk_native_skills_enabled),
                    "native_skills_sync_enabled": bool(self.sdk_native_skills_sync_enabled),
                    "native_skills_dir": str(self.sdk_native_skills_dir),
                    "native_skills_sync": dict(self._sdk_native_skills_sync_meta),
                },
                "skills": [
                    skill.get("skill_id")
                    for skill in self.skills
                    if skill.get("enabled")
                ],
                "tool_scope": {**tool_scope_debug, "rag_user_id": resolved_user_id},
                "skill_router": router_debug,
                "fanout": fanout_debug,
                "background": self._background_debug_snapshot(sid),
                "guardrails": {
                    "degraded": failure_count > 0,
                    "tool_call_count": len(tool_calls),
                    "tool_failure_count": failure_count,
                    "tool_timeout_count": timeout_count,
                    "default_timeout_ms": self.orchestrator.tool_timeout_ms_default,
                },
            },
        }


class AgentChatRuntime:
    def __init__(self, storage: Storage) -> None:
        self.local = AgentChatOrchestrator(storage=storage)
        self.search_assist_service = SearchAssistService(storage=storage)
        self.runtime = str(os.getenv("RENTAL_AGENT_RUNTIME", "deterministic") or "deterministic").strip().lower()
        self.claude = ClaudeSkillRuntime(self.local)
        self.agent_sdk = ClaudeAgentSdkRuntime(self.local)

    def search_assist(
        self,
        *,
        prompt: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        queue: bool = True,
        location_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        parse_payload: Dict[str, Any] = {}
        warnings: List[str] = []
        assist_runtime = str(os.getenv("RENTAL_SEARCH_ASSIST_RUNTIME", "claude") or "claude").strip().lower()
        use_claude = assist_runtime in {"claude", "llm", "model"} and bool(self.claude.api_key)
        if use_claude:
            try:
                parse_payload = self.claude.parse_search_assist_prompt(prompt)
            except Exception as exc:
                parse_payload = {}
                warnings.append("search_assist_claude_parse_failed")
                warnings.append(f"search_assist_claude_parse_error:{_text_snippet(exc, limit=180)}")
        result = self.search_assist_service.assist(
            prompt,
            queue=queue,
            parsed_intent=parse_payload.get("intent") if isinstance(parse_payload.get("intent"), dict) else None,
            parsed_status=parse_payload.get("status"),
            parsed_message=parse_payload.get("message"),
            parsed_unsupported=parse_payload.get("unsupported_or_uncertain_requests")
            if isinstance(parse_payload.get("unsupported_or_uncertain_requests"), list)
            else None,
            parsed_confidence=parse_payload.get("confidence"),
            location_override=location_override,
        )
        debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
        debug.update(
            {
                "runtime": "claude_search_assist" if parse_payload else "deterministic_search_assist",
                "session_id": session_id,
                "user_id": user_id,
                "tool_allowlist": ["tool.search_create"],
                "warnings": warnings,
            }
        )
        if parse_payload:
            debug["model_status"] = parse_payload.get("status")
        result["debug"] = debug
        return result

    def chat(self, *, session_id: Optional[str], message: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        if self.runtime in {"agent_sdk", "claude_agent_sdk"} and self.agent_sdk.is_available():
            try:
                return self.agent_sdk.chat(session_id=session_id, message=message, user_id=user_id)
            except Exception as exc:
                fallback = self.local.chat(session_id=session_id, message=message, user_id=user_id)
                debug = fallback.get("debug") if isinstance(fallback.get("debug"), dict) else {}
                warnings = list(debug.get("warnings") or [])
                warnings.append("agent_sdk_runtime_failed_fallback_deterministic")
                debug["warnings"] = warnings
                debug["runtime"] = "deterministic_fallback"
                debug["agent_sdk_error"] = _exception_with_stdio_debug(exc)
                fallback["debug"] = debug
                return fallback
        if self.runtime in {"agent_sdk", "claude_agent_sdk"} and not self.agent_sdk.is_available():
            fallback = self.local.chat(session_id=session_id, message=message, user_id=user_id)
            debug = fallback.get("debug") if isinstance(fallback.get("debug"), dict) else {}
            warnings = list(debug.get("warnings") or [])
            warnings.append("agent_sdk_runtime_unavailable_fallback_deterministic")
            if not CLAUDE_AGENT_SDK_AVAILABLE:
                warnings.append("agent_sdk_missing_dependency")
                debug["agent_sdk_import_error"] = CLAUDE_AGENT_SDK_IMPORT_ERROR
            debug["warnings"] = warnings
            debug["runtime"] = "deterministic_fallback"
            fallback["debug"] = debug
            return fallback
        if self.runtime == "claude" and self.claude.is_available():
            try:
                return self.claude.chat(session_id=session_id, message=message, user_id=user_id)
            except Exception as exc:
                fallback = self.local.chat(session_id=session_id, message=message, user_id=user_id)
                debug = fallback.get("debug") if isinstance(fallback.get("debug"), dict) else {}
                warnings = list(debug.get("warnings") or [])
                warnings.append("claude_runtime_failed_fallback_deterministic")
                debug["warnings"] = warnings
                debug["runtime"] = "deterministic_fallback"
                debug["claude_error"] = str(exc)
                fallback["debug"] = debug
                return fallback
        fallback = self.local.chat(session_id=session_id, message=message, user_id=user_id)
        debug = fallback.get("debug") if isinstance(fallback.get("debug"), dict) else {}
        debug["runtime"] = "deterministic"
        fallback["debug"] = debug
        return fallback

    def stream_chat(
        self,
        *,
        session_id: Optional[str],
        message: str,
        user_id: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        if self.runtime in {"agent_sdk", "claude_agent_sdk"} and self.agent_sdk.is_available():
            try:
                for item in self.agent_sdk.stream_chat(session_id=session_id, message=message, user_id=user_id):
                    if isinstance(item, dict):
                        yield item
                return
            except Exception as exc:
                fallback = self.local.chat(session_id=session_id, message=message, user_id=user_id)
                debug = fallback.get("debug") if isinstance(fallback.get("debug"), dict) else {}
                warnings = list(debug.get("warnings") or [])
                warnings.append("agent_sdk_runtime_failed_fallback_deterministic")
                debug["warnings"] = warnings
                debug["runtime"] = "deterministic_fallback"
                debug["agent_sdk_error"] = _exception_with_stdio_debug(exc)
                fallback["debug"] = debug
                yield {"event": "warning", "warning": "agent_sdk_runtime_failed_fallback_deterministic"}
                yield {"event": "done", "response": fallback}
                return
        if self.runtime in {"agent_sdk", "claude_agent_sdk"} and not self.agent_sdk.is_available():
            fallback = self.local.chat(session_id=session_id, message=message, user_id=user_id)
            debug = fallback.get("debug") if isinstance(fallback.get("debug"), dict) else {}
            warnings = list(debug.get("warnings") or [])
            warnings.append("agent_sdk_runtime_unavailable_fallback_deterministic")
            if not CLAUDE_AGENT_SDK_AVAILABLE:
                warnings.append("agent_sdk_missing_dependency")
                debug["agent_sdk_import_error"] = CLAUDE_AGENT_SDK_IMPORT_ERROR
            debug["warnings"] = warnings
            debug["runtime"] = "deterministic_fallback"
            fallback["debug"] = debug
            yield {"event": "warning", "warning": "agent_sdk_runtime_unavailable_fallback_deterministic"}
            yield {"event": "done", "response": fallback}
            return
        if self.runtime == "claude" and self.claude.is_available():
            try:
                response = self.claude.chat(session_id=session_id, message=message, user_id=user_id)
            except Exception as exc:
                response = self.local.chat(session_id=session_id, message=message, user_id=user_id)
                debug = response.get("debug") if isinstance(response.get("debug"), dict) else {}
                warnings = list(debug.get("warnings") or [])
                warnings.append("claude_runtime_failed_fallback_deterministic")
                debug["warnings"] = warnings
                debug["runtime"] = "deterministic_fallback"
                debug["claude_error"] = str(exc)
                response["debug"] = debug
            yield {"event": "done", "response": response}
            return
        response = self.local.chat(session_id=session_id, message=message, user_id=user_id)
        debug = response.get("debug") if isinstance(response.get("debug"), dict) else {}
        debug["runtime"] = "deterministic"
        response["debug"] = debug
        yield {"event": "done", "response": response}
