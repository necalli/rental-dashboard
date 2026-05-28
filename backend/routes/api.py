import json
import math
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from werkzeug.local import LocalProxy

from services.geo_suggest import suggest_locations
from services.llm_enrichment import (
    COMPARISON_PROMPT_VERSION,
    PROMPT_VERSION as LLM_PROMPT_VERSION,
    build_comparison_request,
    build_summary_request,
    generate_listing_comparison,
    generate_listing_summary,
)

api_bp = Blueprint("api", __name__)
storage = LocalProxy(lambda: current_app.config["storage"])
agent_chat = LocalProxy(lambda: current_app.config["agent_chat"])
personality_rag = LocalProxy(lambda: current_app.config["personality_rag"])


def _payload() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _url_path(url: str) -> str:
    try:
        return urlparse(url).path.strip("/").lower()
    except Exception:
        return ""


def _is_airbnb_url(url: str) -> bool:
    host = _url_host(url)
    return host == "airbnb.com" or host.endswith(".airbnb.com")


def _is_airbnb_search_url(url: str) -> bool:
    path = _url_path(url)
    return _is_airbnb_url(url) and (path == "s" or path.startswith("s/"))


def _has_airbnb_room_id(url: str) -> bool:
    return "/rooms/" in f"/{_url_path(url)}"


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


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
        parsed = default
    return max(0.0, min(float(parsed), 1.0))


def _avg(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _split_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = raw
    else:
        parts = str(raw).replace("\n", ",").split(",")
    output: List[str] = []
    seen = set()
    for item in parts:
        value = str(item or "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _summarize_job_metrics(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "count": len(items),
        "by_status": {},
        "by_job_type": {},
        "averages": {},
    }
    if not items:
        return summary

    overall_total_ms: List[float] = []
    overall_capture_ms: List[float] = []
    overall_nav_ms: List[float] = []
    overall_parse_ms: List[float] = []
    overall_persist_ms: List[float] = []
    overall_pagination_ms: List[float] = []

    per_type: Dict[str, Dict[str, Any]] = {}
    per_status: Dict[str, int] = {}

    for item in items:
        status = str(item.get("status") or "unknown")
        job_type = str(item.get("job_type") or "unknown")
        per_status[status] = int(per_status.get(status, 0) or 0) + 1

        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        job_total_ms = _to_float(metrics.get("job_total_ms"))
        capture_duration_ms = _to_float(metrics.get("capture_duration_ms"))
        parse_ms = _to_float(metrics.get("parse_ms"))
        persist_ms = _to_float(metrics.get("persist_ms"))
        timings = metrics.get("capture_timings") if isinstance(metrics.get("capture_timings"), dict) else {}
        nav_ms = _to_float(timings.get("navigation_ms"))
        pagination_ms = _to_float(timings.get("review_pagination_ms"))

        bucket = per_type.setdefault(
            job_type,
            {
                "count": 0,
                "job_total_ms": [],
                "capture_duration_ms": [],
                "navigation_ms": [],
                "parse_ms": [],
                "persist_ms": [],
                "review_pagination_ms": [],
            },
        )
        bucket["count"] = int(bucket["count"] or 0) + 1

        if job_total_ms is not None:
            overall_total_ms.append(job_total_ms)
            bucket["job_total_ms"].append(job_total_ms)
        if capture_duration_ms is not None:
            overall_capture_ms.append(capture_duration_ms)
            bucket["capture_duration_ms"].append(capture_duration_ms)
        if nav_ms is not None:
            overall_nav_ms.append(nav_ms)
            bucket["navigation_ms"].append(nav_ms)
        if parse_ms is not None:
            overall_parse_ms.append(parse_ms)
            bucket["parse_ms"].append(parse_ms)
        if persist_ms is not None:
            overall_persist_ms.append(persist_ms)
            bucket["persist_ms"].append(persist_ms)
        if pagination_ms is not None:
            overall_pagination_ms.append(pagination_ms)
            bucket["review_pagination_ms"].append(pagination_ms)

    out_types: Dict[str, Any] = {}
    for job_type, bucket in per_type.items():
        out_types[job_type] = {
            "count": bucket["count"],
            "avg_job_total_ms": _avg(bucket["job_total_ms"]),
            "avg_capture_duration_ms": _avg(bucket["capture_duration_ms"]),
            "avg_navigation_ms": _avg(bucket["navigation_ms"]),
            "avg_parse_ms": _avg(bucket["parse_ms"]),
            "avg_persist_ms": _avg(bucket["persist_ms"]),
            "avg_review_pagination_ms": _avg(bucket["review_pagination_ms"]),
        }

    summary["by_status"] = per_status
    summary["by_job_type"] = out_types
    summary["averages"] = {
        "avg_job_total_ms": _avg(overall_total_ms),
        "avg_capture_duration_ms": _avg(overall_capture_ms),
        "avg_navigation_ms": _avg(overall_nav_ms),
        "avg_parse_ms": _avg(overall_parse_ms),
        "avg_persist_ms": _avg(overall_persist_ms),
        "avg_review_pagination_ms": _avg(overall_pagination_ms),
    }
    return summary


def _coerce_int_range(value: Any, minimum: int, maximum: int) -> Any:
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed < int(minimum):
        return int(minimum)
    if parsed > int(maximum):
        return int(maximum)
    return parsed


def _extract_capture_overrides(
    payload: Dict[str, Any],
    *,
    include_review_controls: bool = True,
) -> Dict[str, int]:
    out: Dict[str, int] = {}
    timeout_ms = _coerce_int_range(payload.get("capture_timeout_ms"), 10000, 600000)
    if timeout_ms is not None:
        out["capture_timeout_ms"] = int(timeout_ms)
    if include_review_controls:
        pagination_passes = _coerce_int_range(payload.get("review_pagination_passes"), 1, 24)
        if pagination_passes is not None:
            out["review_pagination_passes"] = int(pagination_passes)
    return out


def _run_param_value(run_params: Dict[str, Any], key: str) -> Any:
    aliases: Dict[str, List[str]] = {
        "check_in": ["check_in", "checkin"],
        "check_out": ["check_out", "checkout"],
        "adults": ["adults"],
        "children": ["children"],
        "infants": ["infants"],
        "pets": ["pets"],
        "currency": ["currency", "pricing_currency"],
    }
    candidates = aliases.get(str(key), [str(key)])
    for candidate in candidates:
        if candidate not in run_params:
            continue
        value = run_params.get(candidate)
        if value is None or value == "":
            continue
        return value
    return None


def _comparison_reviews_total(listing: Dict[str, Any]) -> Optional[int]:
    total = (
        _to_int(listing.get("reviews_total_count"))
        or _to_int((listing.get("reviews_summary") or {}).get("count"))
        or _to_int((listing.get("host") or {}).get("review_count"))
    )
    if total is None or total <= 0:
        return None
    return int(total)


def _comparison_coverage_snapshot(
    listing: Dict[str, Any],
    reviews: List[Dict[str, Any]],
    review_limit: int,
) -> Dict[str, Any]:
    limit = max(1, int(review_limit or 24))
    reviews_list = reviews or []
    captured = _to_int(listing.get("reviews_captured_count"))
    if captured is None:
        captured = len(reviews_list)
    captured = max(0, int(captured))
    total = _comparison_reviews_total(listing)
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
        snapshot = _comparison_coverage_snapshot(listing, reviews, review_limit)
        coverage = float(snapshot.get("coverage") or 0.0)
        if coverage + 1e-9 >= threshold:
            continue
        target = int(snapshot.get("target") or 1)
        required_captured = min(target, max(1, int(math.ceil(target * threshold))))
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


@api_bp.get("/health")
def health_check():
    return jsonify({"status": "ok", "service": "rental-dashboard"})


@api_bp.post("/api/v1/listings/ingest")
def ingest_listing():
    payload = _payload()
    listing_url = (payload.get("url") or "").strip()
    if not listing_url:
        return jsonify({"error": "url is required"}), 400
    if _is_airbnb_search_url(listing_url):
        return (
            jsonify(
                {
                    "error": "Ingest URLs expects Airbnb listing detail URLs. Use New search for Airbnb /s/... search URLs."
                }
            ),
            400,
        )
    if _is_airbnb_url(listing_url) and not _has_airbnb_room_id(listing_url):
        return jsonify({"error": "Airbnb listing ingest requires a /rooms/{id} URL."}), 400

    include_reviews = payload.get("include_reviews")
    review_mode = payload.get("review_mode")
    review_only = payload.get("review_only")
    review_limit = payload.get("review_limit")
    force = payload.get("force")
    job_payload = {"url": listing_url}
    if include_reviews is not None:
        job_payload["include_reviews"] = bool(include_reviews)
    if review_mode:
        job_payload["review_mode"] = str(review_mode)
    if review_only is not None:
        job_payload["review_only"] = bool(review_only)
    if review_limit is not None:
        job_payload["review_limit"] = int(review_limit)
    job_payload.update(_extract_capture_overrides(payload, include_review_controls=True))
    if force:
        job_payload["force"] = True
    job = storage.create_job("listing_ingest", job_payload)
    return jsonify({"job": job})


@api_bp.post("/api/v1/search")
def run_search():
    payload = _payload()
    if not payload.get("location"):
        return jsonify({"error": "location is required"}), 400

    job_payload = dict(payload)
    job_payload.pop("capture_timeout_ms", None)
    job_payload.pop("review_pagination_passes", None)
    job_payload.update(_extract_capture_overrides(payload, include_review_controls=False))
    job = storage.create_job("search", job_payload)
    return jsonify({"job": job})


@api_bp.get("/api/v1/geo/suggest")
def geo_suggest():
    query = (request.args.get("query") or request.args.get("q") or "").strip()
    if len(query) < 3:
        return jsonify({"query": query, "suggestions": []})
    suggestions = suggest_locations(query)
    if not suggestions:
        return jsonify({"query": query, "suggestions": []})
    return jsonify({"query": query, "suggestions": suggestions})


@api_bp.post("/api/v1/search/ingest")
def ingest_search_listings():
    payload = _payload()
    run_id = (payload.get("run_id") or "").strip()
    listing_ids = payload.get("listing_ids") or []
    review_mode = payload.get("review_mode")
    review_limit = payload.get("review_limit")
    review_only = payload.get("review_only")
    force = payload.get("force")
    capture_overrides = _extract_capture_overrides(payload, include_review_controls=True)
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    if not isinstance(listing_ids, list):
        return jsonify({"error": "listing_ids must be a list"}), 400
    listing_ids = [str(value).strip() for value in listing_ids if str(value).strip()]
    if not listing_ids:
        return jsonify({"error": "listing_ids is required"}), 400

    run_params = {}
    runs = storage.list_search_runs(limit=200)
    run = next((item for item in runs if item.get("run_id") == run_id), None)
    if run:
        run_params = run.get("params") or {}

    listings = storage.list_search_listings(run_id, limit=5000)
    by_id = {str(item.get("id")): item for item in listings if item.get("id")}
    jobs = []
    missing = []
    for listing_id in listing_ids:
        listing = by_id.get(listing_id)
        if not listing:
            missing.append(listing_id)
            continue
        url = listing.get("url")
        if not url:
            missing.append(listing_id)
            continue
        job_payload = {"url": url}
        for key in ("check_in", "check_out", "adults", "children", "infants", "pets", "currency"):
            value = _run_param_value(run_params, key)
            if value is not None and value != "":
                job_payload[key] = value
        if review_mode:
            job_payload["review_mode"] = str(review_mode)
        else:
            job_payload["include_reviews"] = True
        if review_only is not None:
            job_payload["review_only"] = bool(review_only)
        if review_limit is not None:
            job_payload["review_limit"] = int(review_limit)
        job_payload.update(capture_overrides)
        if force:
            job_payload["force"] = True
        job = storage.create_job("listing_ingest", job_payload)
        jobs.append(job)

    return jsonify({"jobs": jobs, "missing": missing})


@api_bp.get("/api/v1/search/runs")
def list_search_runs():
    limit = request.args.get("limit", "50")
    runs = storage.list_search_runs(limit=int(limit or 50))
    return jsonify({"runs": runs})


@api_bp.get("/api/v1/search/runs/<run_id>")
def get_search_run(run_id: str):
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    runs = storage.list_search_runs(limit=200)
    run = next((item for item in runs if item.get("run_id") == run_id), None)
    if not run:
        return jsonify({"error": "search run not found"}), 404
    return jsonify({"run": run})


@api_bp.get("/api/v1/search/listings")
def list_search_listings():
    run_id = (request.args.get("run_id") or "").strip()
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    limit = request.args.get("limit", "200")
    listings = storage.list_search_listings(run_id, limit=int(limit or 200))
    return jsonify({"listings": listings})


@api_bp.get("/api/v1/search/runs/<run_id>/summary")
def get_search_run_summary(run_id: str):
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    listings = storage.list_search_listings(run_id, limit=5000)
    if not listings:
        summary = {
            "run_id": run_id,
            "total_listings": 0,
            "with_errors": 0,
            "avg_quality_score": 0.0,
            "missing_fields": {
                "currency": 0,
                "price": 0,
                "location": 0,
                "rating": 0,
            },
        }
        return jsonify({"summary": summary})

    total = len(listings)
    with_errors = 0
    currency_missing = 0
    price_missing = 0
    location_missing = 0
    rating_missing = 0
    avg_quality = 0.0

    scores = []
    for listing in listings:
        validation = listing.get("validation") or {}
        if validation.get("errors"):
            with_errors += 1
        score = validation.get("quality_score")
        if isinstance(score, (int, float)):
            scores.append(float(score))
        if not listing.get("currency"):
            currency_missing += 1
        if not listing.get("price"):
            price_missing += 1
        if not listing.get("location"):
            location_missing += 1
        if listing.get("rating") is None:
            rating_missing += 1

    if scores:
        avg_quality = round(sum(scores) / len(scores), 2)

    summary = {
        "run_id": run_id,
        "total_listings": total,
        "with_errors": with_errors,
        "avg_quality_score": avg_quality,
        "missing_fields": {
            "currency": currency_missing,
            "price": price_missing,
            "location": location_missing,
            "rating": rating_missing,
        },
    }
    return jsonify({"summary": summary})


@api_bp.get("/api/v1/jobs")
def list_jobs():
    limit = request.args.get("limit", "50")
    jobs = storage.list_jobs(limit=int(limit or 50))
    return jsonify({"jobs": jobs})


@api_bp.get("/api/v1/jobs/<job_id>")
def get_job(job_id: str):
    job = storage.get_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"job": job})


@api_bp.post("/api/v1/agent/chat")
def agent_chat_message():
    payload = _payload()
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    session_id = payload.get("session_id")
    user_id = payload.get("user_id")
    try:
        response = agent_chat.chat(session_id=session_id, message=message, user_id=user_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(response)


def _sse_event_line(event: Dict[str, Any]) -> str:
    payload = event if isinstance(event, dict) else {"event": "message", "data": event}
    event_name = str(payload.get("event") or "message").strip() or "message"
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {data}\n\n"


@api_bp.post("/api/v1/agent/chat/stream")
def agent_chat_message_stream():
    payload = _payload()
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    session_id = payload.get("session_id")
    user_id = payload.get("user_id")

    def _generate() -> Any:
        try:
            for event in agent_chat.stream_chat(session_id=session_id, message=message, user_id=user_id):
                if not isinstance(event, dict):
                    continue
                yield _sse_event_line(event)
        except Exception as exc:
            yield _sse_event_line({"event": "error", "error": str(exc)})
            yield _sse_event_line({"event": "done", "response": None})

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(_generate()), mimetype="text/event-stream", headers=headers)


@api_bp.post("/api/v1/memory/upload")
def upload_memory_file():
    uploaded = request.files.get("file")
    if uploaded is None:
        return jsonify({"error": "file is required"}), 400
    filename = str(uploaded.filename or "").strip()
    if not filename:
        return jsonify({"error": "filename is required"}), 400
    try:
        raw_bytes = uploaded.read() or b""
        user_id = request.form.get("user_id")
        title = request.form.get("title")
        tags = _split_tags(request.form.get("tags"))
        metadata = {
            "source": request.form.get("source") or "upload",
            "trip_id": request.form.get("trip_id"),
        }
        result = personality_rag.ingest_upload(
            user_id=user_id,
            filename=filename,
            mime_type=uploaded.mimetype,
            raw_bytes=raw_bytes,
            title=title,
            tags=tags,
            metadata=metadata,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@api_bp.post("/api/v1/memory/upsert")
def upsert_memory_text():
    payload = _payload()
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    try:
        result = personality_rag.upsert_memory_text(
            user_id=payload.get("user_id"),
            title=str(payload.get("title") or "").strip() or "Manual memory",
            text=text,
            tags=_split_tags(payload.get("tags")),
            metadata={
                "source": payload.get("source") or "manual",
                "trip_id": payload.get("trip_id"),
            },
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@api_bp.post("/api/v1/memory/query")
def query_memory_context():
    payload = _payload()
    query = str(payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    limit = _coerce_int_range(payload.get("limit"), 1, 20) or 6
    tags = _split_tags(payload.get("tags"))
    try:
        result = personality_rag.query_context(
            user_id=payload.get("user_id"),
            query=query,
            limit=limit,
            tags=tags,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


@api_bp.get("/api/v1/memory/files")
def list_memory_files():
    user_id = request.args.get("user_id")
    limit = int(request.args.get("limit", "100") or 100)
    try:
        memories = personality_rag.list_memories(user_id=user_id, limit=max(1, min(limit, 500)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"memories": memories})


@api_bp.delete("/api/v1/memory/files/<memory_id>")
def delete_memory_file(memory_id: str):
    user_id = request.args.get("user_id")
    try:
        deleted = personality_rag.delete_memory(memory_id=memory_id, user_id=user_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if not deleted:
        return jsonify({"error": "memory not found"}), 404
    return jsonify({"deleted": True, "memory_id": memory_id})


@api_bp.get("/api/v1/metrics/jobs")
def list_job_metrics():
    limit = int(request.args.get("limit", "50") or 50)
    summary_limit = int(request.args.get("summary_limit", "200") or 200)
    job_type = (request.args.get("job_type") or "").strip() or None
    status = (request.args.get("status") or "").strip() or None

    metrics = storage.list_job_metrics(
        limit=max(1, limit),
        job_type=job_type,
        status=status,
    )
    summary_source = storage.list_job_metrics(
        limit=max(1, summary_limit),
        job_type=job_type,
        status=status,
    )
    summary = _summarize_job_metrics(summary_source)
    return jsonify({"metrics": metrics, "summary": summary})


@api_bp.get("/api/v1/listings")
def list_listings():
    limit = request.args.get("limit", "50")
    listings = storage.list_listings(limit=int(limit or 50))
    return jsonify({"listings": listings})


@api_bp.get("/api/v1/listings/<listing_id>")
def get_listing(listing_id: str):
    listing = storage.get_listing(listing_id)
    if not listing:
        return jsonify({"error": "listing not found"}), 404
    return jsonify({"listing": listing})


@api_bp.get("/api/v1/enrich/listings/<listing_id>/summary")
def get_listing_summary(listing_id: str):
    summary = storage.get_latest_enrichment(listing_id, "listing_summary")
    if not summary:
        return jsonify({"error": "summary not found"}), 404
    return jsonify({"summary": summary})


@api_bp.post("/api/v1/enrich/listings/<listing_id>/summary")
def create_listing_summary(listing_id: str):
    payload = _payload()
    force = bool(payload.get("force"))
    sync = bool(payload.get("sync"))
    model_override = payload.get("model")
    review_limit = payload.get("review_limit")
    listing = storage.get_listing(listing_id)
    if not listing:
        return jsonify({"error": "listing not found"}), 404
    if review_limit is not None:
        reviews = storage.list_reviews(listing_id, limit=int(review_limit))
    else:
        reviews = storage.list_reviews(listing_id, limit=200)
    model, input_hash = build_summary_request(listing, reviews, model=model_override)
    if not force:
        existing = storage.get_enrichment_by_hash(
            listing_id, "listing_summary", model, LLM_PROMPT_VERSION, input_hash
        )
        if existing:
            return jsonify({"status": "cached", "summary": existing})
    if sync:
        job = storage.create_job(
            "listing_enrich",
            {"listing_id": listing_id, "kind": "listing_summary", "model": model, "input_hash": input_hash},
        )
        storage.update_job(job["job_id"], status="running")
        try:
            output = generate_listing_summary(listing, reviews, model=model)
            enrichment_id = storage.add_enrichment(
                listing_id,
                "listing_summary",
                model,
                LLM_PROMPT_VERSION,
                input_hash,
                output,
            )
            storage.update_job(job["job_id"], status="complete", result_ref=enrichment_id)
            return jsonify({"status": "complete", "summary": output, "job": job})
        except Exception as exc:
            storage.update_job(job["job_id"], status="failed", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    job = storage.create_job(
        "listing_enrich",
        {"listing_id": listing_id, "kind": "listing_summary", "model": model, "input_hash": input_hash},
    )
    return jsonify({"status": "queued", "job": job})


@api_bp.post("/api/v1/enrich/compare")
def create_listing_comparison():
    payload = _payload()
    listing_ids = payload.get("listing_ids") or []
    force = bool(payload.get("force"))
    sync = bool(payload.get("sync"))
    model_override = payload.get("model")
    review_limit = _coerce_int_range(payload.get("review_limit"), 1, 50) or 24
    require_min_coverage = _to_bool(
        payload.get("require_min_coverage"),
        _to_bool(os.getenv("RENTAL_COMPARE_REQUIRE_MIN_COVERAGE_DEFAULT"), False),
    )
    min_review_coverage = _coerce_ratio(
        payload.get("min_review_coverage"),
        _coerce_ratio(os.getenv("RENTAL_COMPARE_MIN_COVERAGE_DEFAULT"), 0.5),
    )
    if not isinstance(listing_ids, list) or len(listing_ids) < 2:
        return jsonify({"error": "listing_ids must include at least 2 items"}), 400
    listing_ids = [str(value).strip() for value in listing_ids if str(value).strip()]
    if len(listing_ids) < 2:
        return jsonify({"error": "listing_ids must include at least 2 items"}), 400
    if len(listing_ids) > 6:
        return jsonify({"error": "listing_ids must include at most 6 items"}), 400

    listings = []
    missing = []
    reviews_by_listing: Dict[str, Any] = {}
    for listing_id in listing_ids:
        listing = storage.get_listing(listing_id)
        if not listing:
            missing.append(listing_id)
            continue
        listings.append(listing)
        reviews_by_listing[str(listing_id)] = storage.list_reviews(
            listing_id, limit=max(1, review_limit)
        )

    if missing:
        return jsonify({"error": "Listings not found", "missing": missing}), 404

    if require_min_coverage:
        violations = _comparison_coverage_violations(
            listings,
            reviews_by_listing,
            review_limit=review_limit,
            min_review_coverage=min_review_coverage,
        )
        if violations:
            return (
                jsonify(
                    {
                        "error": "Comparison blocked by minimum review coverage policy.",
                        "code": "comparison_coverage_blocked",
                        "policy": {
                            "require_min_coverage": True,
                            "min_review_coverage": round(min_review_coverage, 4),
                            "review_limit": int(review_limit),
                        },
                        "violations": violations,
                        "suggested_action": "Fetch full reviews for listed listings and retry.",
                    }
                ),
                409,
            )

    model, input_hash = build_comparison_request(listings, reviews_by_listing, model=model_override)
    compare_key = f"compare:{input_hash}"
    if not force:
        existing = storage.get_enrichment_by_hash(
            compare_key, "listing_comparison", model, COMPARISON_PROMPT_VERSION, input_hash
        )
        if existing:
            return jsonify({"status": "cached", "summary": existing})

    if sync:
        job = storage.create_job(
            "listing_compare",
            {
                "listing_ids": listing_ids,
                "kind": "listing_comparison",
                "model": model,
                "input_hash": input_hash,
                "review_limit": int(review_limit),
                "require_min_coverage": bool(require_min_coverage),
                "min_review_coverage": float(min_review_coverage),
            },
        )
        storage.update_job(job["job_id"], status="running")
        try:
            output = generate_listing_comparison(listings, reviews_by_listing, model=model)
            enrichment_id = storage.add_enrichment(
                compare_key,
                "listing_comparison",
                model,
                COMPARISON_PROMPT_VERSION,
                input_hash,
                output,
            )
            storage.update_job(job["job_id"], status="complete", result_ref=enrichment_id)
            return jsonify({"status": "complete", "summary": output, "job": job})
        except Exception as exc:
            storage.update_job(job["job_id"], status="failed", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    job = storage.create_job(
        "listing_compare",
        {
            "listing_ids": listing_ids,
            "kind": "listing_comparison",
            "model": model,
            "input_hash": input_hash,
            "review_limit": int(review_limit),
            "require_min_coverage": bool(require_min_coverage),
            "min_review_coverage": float(min_review_coverage),
        },
    )
    return jsonify({"status": "queued", "job": job})


@api_bp.get("/api/v1/reviews")
def list_reviews():
    listing_id = (request.args.get("listing_id") or "").strip()
    if not listing_id:
        return jsonify({"error": "listing_id is required"}), 400
    limit = request.args.get("limit", "200")
    reviews = storage.list_reviews(listing_id, limit=int(limit or 200))
    return jsonify({"reviews": reviews})


@api_bp.post("/api/v1/listings")
def upsert_listing():
    payload = _payload()
    listing_id = payload.get("id") or payload.get("listing_id")
    if not listing_id:
        return jsonify({"error": "listing_id is required"}), 400
    listing = storage.upsert_listing(payload)
    return jsonify({"listing": listing})


@api_bp.post("/api/v1/reviews")
def add_reviews():
    payload = _payload()
    listing_id = (payload.get("listing_id") or "").strip()
    reviews = payload.get("reviews") or []
    if not listing_id:
        return jsonify({"error": "listing_id is required"}), 400
    if not isinstance(reviews, list):
        return jsonify({"error": "reviews must be a list"}), 400
    inserted = storage.add_reviews(listing_id, reviews)
    return jsonify({"inserted": inserted})



