import json
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


DEFAULT_BASE_URL = "https://api.frankfurter.dev/v1"
DEFAULT_FALLBACK_BASE_URL = "https://api.frankfurter.app"
DEFAULT_TARGET = "USD"
DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT = 10
DEFAULT_FAIL_COOLDOWN_SECONDS = 60
DEFAULT_PER_ATTEMPT_TIMEOUT = 3
DEFAULT_RETRIES = 1
DEFAULT_BACKOFF_BASE_MS = 200
DEFAULT_BACKOFF_MAX_MS = 1200
DEFAULT_MAX_STALE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_DB_RELOAD_MIN_SECONDS = 15

_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_CACHE_LOADED = False
_CACHE_PERSIST_MTIME: Optional[float] = None
_LOCKS: Dict[Tuple[str, str], threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()
_REFRESH_INFLIGHT: set[Tuple[str, str]] = set()
_REFRESH_LAST_TS: Dict[Tuple[str, str], float] = {}
_REFRESH_LOCK = threading.Lock()
_FAIL_UNTIL: Dict[Tuple[str, str], float] = {}
_CCY_RE = re.compile(r"\b([A-Z]{3})\b")
_LOGGER = logging.getLogger("rental.fx")
_METRICS_LOCK = threading.Lock()
_FX_METRICS: Dict[str, Dict[str, int]] = {
    "success": {},
    "failure": {},
    "http_status": {},
    "error_kind": {},
}
_ERROR_LOG_COUNTS: Dict[str, int] = {}
_DB_CACHE_LOADED = False
_DB_LAST_UPDATED_AT: float = 0.0
_DB_LAST_RELOAD_TS: float = 0.0


def _normalize_currency_code(raw: Any) -> str:
    """Return a 3-letter currency code (e.g. 'USD') or '' if unknown.

    Inputs are not always clean ISO codes. We see:
    - 'USD'
    - 'SGD\\u00a0' (NBSP)
    - '$335 SGD' (price display strings)

    Normalizing here prevents accidental cache misses and invalid FX queries that
    would otherwise show up as "USD n/a" in the UI.
    """
    if raw is None:
        return ""
    s = str(raw).replace("\u00a0", " ").strip().upper()
    if not s:
        return ""
    m = _CCY_RE.search(s)
    return m.group(1) if m else ""


def _pair_lock(key: Tuple[str, str]) -> threading.Lock:
    # Avoid bursty concurrent calls hammering the FX endpoint (and triggering 429/blocks).
    # We intentionally keep this lightweight and entirely in-process; disk cache covers restarts.
    with _LOCKS_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _now() -> float:
    return time.time()


def _get_env(name: str, fallback: Any) -> Any:
    value = os.getenv(name)
    return fallback if value in (None, "") else value


def _truthy_env(name: str, fallback: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return fallback
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _int_env(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(fallback)
    try:
        return int(str(raw).strip())
    except Exception:
        return int(fallback)


def _float_env(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(fallback)
    try:
        return float(str(raw).strip())
    except Exception:
        return float(fallback)


def _db_enabled() -> bool:
    return _truthy_env("RENTAL_FX_DB_PERSIST", True)


def _db_path() -> Path:
    raw = os.getenv("RENTAL_DB_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    # backend/services/fx_rates.py -> backend/data/rental_dashboard.db
    backend_dir = Path(__file__).resolve().parents[1]
    return backend_dir / "data" / "rental_dashboard.db"


def _ensure_db_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fx_rates_cache (
            base TEXT NOT NULL,
            target TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (base, target)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fx_rates_cache_updated_at ON fx_rates_cache(updated_at)"
    )


def _metric_inc(bucket: str, key: str) -> None:
    if not key:
        return
    with _METRICS_LOCK:
        values = _FX_METRICS.setdefault(bucket, {})
        values[key] = int(values.get(key, 0) or 0) + 1


def _record_success(provider: str) -> None:
    _metric_inc("success", provider or "unknown")


def _record_failure(provider: str, status_code: Optional[int] = None, error_kind: Optional[str] = None) -> None:
    _metric_inc("failure", provider or "unknown")
    if status_code is not None:
        _metric_inc("http_status", str(int(status_code)))
    if error_kind:
        _metric_inc("error_kind", error_kind)


def get_fx_metrics_snapshot() -> Dict[str, Dict[str, int]]:
    with _METRICS_LOCK:
        out: Dict[str, Dict[str, int]] = {}
        for bucket, values in _FX_METRICS.items():
            out[bucket] = dict(values)
        return out


def _should_log_error(provider: str) -> bool:
    key = provider or "unknown"
    with _METRICS_LOCK:
        count = int(_ERROR_LOG_COUNTS.get(key, 0) or 0) + 1
        _ERROR_LOG_COUNTS[key] = count
    return count <= 3 or (count % 25) == 0


def _log_provider_error(provider: str, message: str, details: Optional[str] = None) -> None:
    if not _should_log_error(provider):
        return
    if details:
        _LOGGER.warning("FX provider error (%s): %s | %s", provider, message, details)
    else:
        _LOGGER.warning("FX provider error (%s): %s", provider, message)


def _db_load_all_into_cache() -> None:
    global _DB_LAST_UPDATED_AT
    if not _db_enabled():
        return
    path = _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=1.5) as conn:
            _ensure_db_table(conn)
            rows = conn.execute(
                "SELECT base, target, fetched_at, updated_at, payload_json FROM fx_rates_cache"
            ).fetchall()
    except Exception:
        return

    max_updated = _DB_LAST_UPDATED_AT
    for base_raw, target_raw, fetched_at, updated_at, payload_json in rows:
        base = _normalize_currency_code(base_raw)
        target = _normalize_currency_code(target_raw)
        if not base or not target:
            continue
        if not isinstance(fetched_at, (int, float)):
            continue
        payload: Optional[Dict[str, Any]] = None
        try:
            candidate = json.loads(payload_json or "{}")
            if isinstance(candidate, dict):
                payload = candidate
        except Exception:
            payload = None
        if not payload:
            continue
        rate = _coerce_positive_rate(payload.get("rate"))
        if rate is None:
            continue
        payload["rate"] = rate
        _CACHE[(base, target)] = {"fetched_at": float(fetched_at), "payload": payload}
        if isinstance(updated_at, (int, float)):
            max_updated = max(float(updated_at), float(max_updated))

    _DB_LAST_UPDATED_AT = max_updated


def _db_reload_incremental() -> None:
    global _DB_LAST_RELOAD_TS
    global _DB_LAST_UPDATED_AT
    if not _db_enabled():
        return
    reload_min_seconds = max(0.0, _float_env("RENTAL_FX_DB_RELOAD_MIN_SECONDS", DEFAULT_DB_RELOAD_MIN_SECONDS))
    now = _now()
    if (now - float(_DB_LAST_RELOAD_TS or 0.0)) < reload_min_seconds:
        return
    _DB_LAST_RELOAD_TS = now

    path = _db_path()
    try:
        if not path.exists():
            return
        with sqlite3.connect(path, timeout=1.5) as conn:
            _ensure_db_table(conn)
            rows = conn.execute(
                """
                SELECT base, target, fetched_at, updated_at, payload_json
                FROM fx_rates_cache
                WHERE updated_at > ?
                ORDER BY updated_at ASC
                """,
                (float(_DB_LAST_UPDATED_AT),),
            ).fetchall()
    except Exception:
        return

    if not rows:
        return

    max_updated = _DB_LAST_UPDATED_AT
    for base_raw, target_raw, fetched_at, updated_at, payload_json in rows:
        base = _normalize_currency_code(base_raw)
        target = _normalize_currency_code(target_raw)
        if not base or not target:
            continue
        if not isinstance(fetched_at, (int, float)):
            continue
        try:
            candidate = json.loads(payload_json or "{}")
        except Exception:
            continue
        if not isinstance(candidate, dict):
            continue
        rate = _coerce_positive_rate(candidate.get("rate"))
        if rate is None:
            continue
        candidate["rate"] = rate
        _CACHE[(base, target)] = {"fetched_at": float(fetched_at), "payload": candidate}
        if isinstance(updated_at, (int, float)):
            max_updated = max(float(updated_at), float(max_updated))

    _DB_LAST_UPDATED_AT = max_updated


def _db_persist_pair(base: str, target: str, fetched_at: float, payload: Dict[str, Any]) -> None:
    global _DB_LAST_UPDATED_AT
    if not _db_enabled():
        return
    path = _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = float(_now())
        blob = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with sqlite3.connect(path, timeout=1.5) as conn:
            _ensure_db_table(conn)
            conn.execute(
                """
                INSERT INTO fx_rates_cache (base, target, fetched_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(base, target) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (base, target, float(fetched_at), ts, blob),
            )
            conn.commit()
        _DB_LAST_UPDATED_AT = max(float(_DB_LAST_UPDATED_AT), float(ts))
    except Exception:
        return


def _default_cache_path() -> Path:
    # backend/services/fx_rates.py -> backend/data/fx_cache.json
    backend_dir = Path(__file__).resolve().parents[1]
    return backend_dir / "data" / "fx_cache.json"


def _cache_path() -> Path:
    raw = os.getenv("RENTAL_FX_CACHE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return _default_cache_path()


def _quarantine_corrupt_cache(path: Path) -> None:
    """Move an unreadable cache file aside so future reads can recover cleanly."""
    try:
        if not path.exists():
            return
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        candidate = Path(f"{path}.corrupt.{ts}")
        i = 1
        while candidate.exists():
            candidate = Path(f"{path}.corrupt.{ts}.{i}")
            i += 1
        os.replace(str(path), str(candidate))
    except Exception:
        return


def _load_cache_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read cache JSON safely; quarantine if the file is corrupted."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        # Valid JSON but wrong shape is still unusable for this cache contract.
        _quarantine_corrupt_cache(path)
        return None
    except json.JSONDecodeError:
        _quarantine_corrupt_cache(path)
        return None
    except UnicodeDecodeError:
        _quarantine_corrupt_cache(path)
        return None
    except Exception:
        return None


def _max_stale_seconds() -> int:
    # Max age for stale values served during revalidation.
    return _int_env("RENTAL_FX_MAX_STALE_SECONDS", int(DEFAULT_MAX_STALE_SECONDS))


def _inverse_from_cached(
    base: str,
    target: str,
    ttl_seconds: int,
    allow_stale: bool,
) -> Optional[Dict[str, Any]]:
    """Build base->target from a cached target->base pair using reciprocal rate."""
    inv_key = (target, base)
    cached = _CACHE.get(inv_key)
    if not isinstance(cached, dict):
        return None
    fetched_at = float(cached.get("fetched_at", 0) or 0.0)
    payload = cached.get("payload")
    if not isinstance(payload, dict):
        return None
    inv_rate = _coerce_positive_rate(payload.get("rate"))
    if inv_rate is None:
        return None

    age = _now() - fetched_at
    fresh = age < float(ttl_seconds)
    stale = age <= float(_max_stale_seconds()) and allow_stale
    if not fresh and not stale:
        return None

    reciprocal = 1.0 / inv_rate
    source = payload.get("source")
    out = {
        "rate": reciprocal,
        "base": base,
        "target": target,
        "as_of": payload.get("as_of"),
        "source": f"{source}|inverse-cache" if source else "inverse-cache",
        "stale": not fresh,
    }
    _CACHE[(base, target)] = {"fetched_at": fetched_at, "payload": dict(out, stale=False)}
    return out


def _schedule_async_refresh(base: str, target: str) -> None:
    """Trigger a background refresh for stale values, with per-pair throttling."""
    if not _truthy_env("RENTAL_FX_STALE_WHILE_REVALIDATE", True):
        return
    key = (base, target)
    now = _now()
    throttle_s = max(0, _int_env("RENTAL_FX_REFRESH_THROTTLE_SECONDS", 120))
    with _REFRESH_LOCK:
        last = float(_REFRESH_LAST_TS.get(key, 0.0) or 0.0)
        if key in _REFRESH_INFLIGHT:
            return
        if throttle_s > 0 and (now - last) < float(throttle_s):
            return
        _REFRESH_INFLIGHT.add(key)
        _REFRESH_LAST_TS[key] = now

    def _worker() -> None:
        try:
            # Force bypass of stale fast-path to perform a real refresh.
            get_fx_rate(base, target, _force_refresh=True)
        finally:
            with _REFRESH_LOCK:
                _REFRESH_INFLIGHT.discard(key)

    t = threading.Thread(target=_worker, daemon=True, name=f"fx-refresh-{base}-{target}")
    t.start()


def _load_persistent_cache() -> None:
    """Best-effort: hydrate in-memory cache from disk for restart robustness.

    This avoids USD n/a if the FX endpoint is temporarily blocked/unreachable at startup.
    """
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    _CACHE_LOADED = True

    global _DB_CACHE_LOADED
    if not _DB_CACHE_LOADED:
        _DB_CACHE_LOADED = True
        _db_load_all_into_cache()

    persist_file = _truthy_env("RENTAL_FX_PERSIST", True)
    if not persist_file:
        return

    path = _cache_path()
    try:
        if not path.exists():
            return
        try:
            # Track mtime so other processes can detect updates later.
            global _CACHE_PERSIST_MTIME
            _CACHE_PERSIST_MTIME = path.stat().st_mtime
        except Exception:
            pass
        data = _load_cache_json(path)
        if not isinstance(data, dict):
            return
        cache = data.get("cache") if isinstance(data, dict) else None
        if not isinstance(cache, dict):
            return
        for key, entry in cache.items():
            if not isinstance(key, str) or "->" not in key:
                continue
            if not isinstance(entry, dict):
                continue
            fetched_at = entry.get("fetched_at")
            payload = entry.get("payload")
            if not isinstance(fetched_at, (int, float)):
                continue
            if not isinstance(payload, dict):
                continue
            base, target = key.split("->", 1)
            base = _normalize_currency_code(base)
            target = _normalize_currency_code(target)
            if not base or not target:
                continue
            _CACHE[(base, target)] = {"fetched_at": float(fetched_at), "payload": payload}
    except Exception:
        # Never break normal operation due to cache read errors.
        return


def _maybe_reload_persistent_cache() -> None:
    """Best-effort: if another process updated the persistent cache, merge it in.

    This improves robustness when multiple workers are running in separate processes:
    one process can fetch+persist a rate, and others can pick it up without hitting
    the FX endpoint themselves.

    Called only on cache miss/stale paths; cache-hit remains lock-free and fast.
    """
    _db_reload_incremental()
    if not _truthy_env("RENTAL_FX_PERSIST", True):
        return
    path = _cache_path()
    try:
        if not path.exists():
            return
        st = path.stat()
        mtime = float(getattr(st, "st_mtime", 0.0) or 0.0)
        global _CACHE_PERSIST_MTIME
        if _CACHE_PERSIST_MTIME is not None and mtime <= float(_CACHE_PERSIST_MTIME):
            return

        data = _load_cache_json(path)
        if not isinstance(data, dict):
            _CACHE_PERSIST_MTIME = mtime
            return
        cache = data.get("cache") if isinstance(data, dict) else None
        if not isinstance(cache, dict):
            _CACHE_PERSIST_MTIME = mtime
            return
        for key, entry in cache.items():
            if not isinstance(key, str) or "->" not in key:
                continue
            if not isinstance(entry, dict):
                continue
            fetched_at = entry.get("fetched_at")
            payload = entry.get("payload")
            if not isinstance(fetched_at, (int, float)):
                continue
            if not isinstance(payload, dict):
                continue
            base, target = key.split("->", 1)
            base = _normalize_currency_code(base)
            target = _normalize_currency_code(target)
            if not base or not target:
                continue
            _CACHE[(base, target)] = {"fetched_at": float(fetched_at), "payload": payload}

        _CACHE_PERSIST_MTIME = mtime
    except Exception:
        return


def _persist_cache() -> None:
    """Best-effort: persist cache to disk. Called only after a successful fetch."""
    persist_file = _truthy_env("RENTAL_FX_PERSIST", True)
    persist_db = _db_enabled()
    if not persist_file and not persist_db:
        return
    path = _cache_path()
    try:
        payload: Dict[str, Any] = {"version": 1, "cache": {}}
        cache_out: Dict[str, Any] = payload["cache"]
        for (base, target), entry in _CACHE.items():
            if not isinstance(entry, dict):
                continue
            fetched_at = entry.get("fetched_at")
            pl = entry.get("payload")
            if not isinstance(fetched_at, (int, float)):
                continue
            if not isinstance(pl, dict):
                continue
            if persist_db:
                _db_persist_pair(base, target, float(fetched_at), pl)
            if persist_file:
                cache_out[f"{base}->{target}"] = {"fetched_at": float(fetched_at), "payload": pl}

        if persist_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(str(tmp), str(path))
            try:
                global _CACHE_PERSIST_MTIME
                _CACHE_PERSIST_MTIME = path.stat().st_mtime
            except Exception:
                pass
    except Exception:
        # Never break conversions due to cache write errors.
        return


def _fx_headers() -> Dict[str, str]:
    # Some free FX endpoints will 403 or apply stricter rate limits without a UA.
    ua = str(
        _get_env(
            "RENTAL_FX_USER_AGENT",
            "rental-dashboard/0.1 (+https://localhost) python-urllib",
        )
    )
    return {
        "Accept": "application/json",
        "User-Agent": ua,
    }


def _fx_base_urls() -> list[str]:
    # Prefer an explicit list if provided; otherwise use primary + fallback.
    raw = os.getenv("RENTAL_FX_BASE_URLS")
    if raw and str(raw).strip():
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    else:
        # Default: configurable primary + secondary, then known Frankfurter variants.
        primary = str(
            _get_env("RENTAL_FX_PRIMARY_BASE_URL", _get_env("RENTAL_FX_BASE_URL", DEFAULT_FALLBACK_BASE_URL))
        ).strip()
        secondary = str(_get_env("RENTAL_FX_SECONDARY_BASE_URL", "")).strip()
        parts = [primary]
        if secondary:
            parts.append(secondary)
        # Ensure we always try the "other" Frankfurter variant as a fallback.
        if primary.rstrip("/") != DEFAULT_FALLBACK_BASE_URL:
            parts.append(DEFAULT_FALLBACK_BASE_URL)
        if primary.rstrip("/") != DEFAULT_BASE_URL:
            parts.append(DEFAULT_BASE_URL)

    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        url = str(p).strip().rstrip("/")
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _yyyy_mm_dd_from_unix(ts: Any) -> Optional[str]:
    try:
        t = float(ts)
        if t <= 0:
            return None
        return time.strftime("%Y-%m-%d", time.gmtime(t))
    except Exception:
        return None


def _coerce_positive_rate(value: Any) -> Optional[float]:
    """Return a strictly positive, finite float rate or None."""
    try:
        rate = float(value)
    except Exception:
        return None
    if rate <= 0 or not math.isfinite(rate):
        return None
    return rate


def _is_transient_http(status: Optional[int]) -> bool:
    if status is None:
        return False
    if status == 429:
        return True
    if 500 <= int(status) < 600:
        return True
    return False


def _fetch_json_with_retry(
    url: str,
    timeout_s: float,
    *,
    provider: str,
    deadline: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str]]:
    """Fetch JSON with transient retry/backoff and bounded timeout.

    NOTE: This is only used on FX cache-miss paths, and timeouts are bounded by the
    caller's remaining deadline budget.
    """
    max_retries = max(0, _int_env("RENTAL_FX_RETRIES", int(DEFAULT_RETRIES)))
    backoff_base = max(0.0, _float_env("RENTAL_FX_BACKOFF_BASE_MS", float(DEFAULT_BACKOFF_BASE_MS))) / 1000.0
    backoff_cap = max(0.0, _float_env("RENTAL_FX_BACKOFF_MAX_MS", float(DEFAULT_BACKOFF_MAX_MS))) / 1000.0
    req = urllib.request.Request(url, headers=_fx_headers())

    last_status: Optional[int] = None
    last_error_kind: Optional[str] = None
    for attempt in range(max_retries + 1):
        if deadline is not None:
            remaining = float(deadline) - _now()
            if remaining <= 0.20:
                break
            attempt_timeout = min(float(timeout_s), max(0.25, remaining))
        else:
            attempt_timeout = max(0.25, float(timeout_s))

        try:
            with urllib.request.urlopen(req, timeout=float(attempt_timeout)) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                body = resp.read().decode("utf-8")
            if status >= 400:
                last_status = status
                last_error_kind = "http_error"
                _record_failure(provider, status_code=status, error_kind=last_error_kind)
                _log_provider_error(provider, f"HTTP {status}", url)
                transient = _is_transient_http(status)
                if transient and attempt < max_retries:
                    jitter = 1.0 + random.uniform(-0.20, 0.20)
                    sleep_s = min(backoff_cap, backoff_base * (2**attempt) * jitter)
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    continue
                return None, last_status, last_error_kind
            try:
                data = json.loads(body)
            except Exception:
                last_status = status
                last_error_kind = "malformed_json"
                _record_failure(provider, status_code=status, error_kind=last_error_kind)
                sample = body[:240].replace("\n", "\\n")
                _log_provider_error(provider, "Malformed JSON", sample)
                if attempt < max_retries:
                    jitter = 1.0 + random.uniform(-0.20, 0.20)
                    sleep_s = min(backoff_cap, backoff_base * (2**attempt) * jitter)
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    continue
                return None, last_status, last_error_kind
            if not isinstance(data, dict):
                last_status = status
                last_error_kind = "invalid_payload"
                _record_failure(provider, status_code=status, error_kind=last_error_kind)
                _log_provider_error(provider, "JSON payload is not an object", url)
                return None, last_status, last_error_kind
            return data, status, None
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            last_status = status
            last_error_kind = "http_error"
            _record_failure(provider, status_code=status, error_kind=last_error_kind)
            try:
                body_raw = exc.read()
                body = body_raw.decode("utf-8", errors="replace") if isinstance(body_raw, (bytes, bytearray)) else str(body_raw)
            except Exception:
                body = ""
            details = body[:240].replace("\n", "\\n") if body else url
            _log_provider_error(provider, f"HTTP {status}", details)
            transient = _is_transient_http(status)
            if transient and attempt < max_retries:
                jitter = 1.0 + random.uniform(-0.20, 0.20)
                sleep_s = min(backoff_cap, backoff_base * (2**attempt) * jitter)
                if sleep_s > 0:
                    time.sleep(sleep_s)
                continue
            return None, last_status, last_error_kind
        except (urllib.error.URLError, TimeoutError) as exc:
            last_status = None
            last_error_kind = "timeout_or_network"
            _record_failure(provider, status_code=None, error_kind=last_error_kind)
            _log_provider_error(provider, "Network/timeout", str(exc))
            if attempt < max_retries:
                jitter = 1.0 + random.uniform(-0.20, 0.20)
                sleep_s = min(backoff_cap, backoff_base * (2**attempt) * jitter)
                if sleep_s > 0:
                    time.sleep(sleep_s)
                continue
            return None, last_status, last_error_kind
        except Exception as exc:
            last_status = None
            last_error_kind = "unexpected_error"
            _record_failure(provider, status_code=None, error_kind=last_error_kind)
            _log_provider_error(provider, "Unexpected fetch error", str(exc))
            return None, last_status, last_error_kind

    return None, last_status, last_error_kind


def _fetch_json(url: str, timeout_s: float, *, provider: str, deadline: Optional[float] = None) -> Optional[Dict[str, Any]]:
    data, _status, _error = _fetch_json_with_retry(
        url,
        timeout_s,
        provider=provider,
        deadline=deadline,
    )
    return data


def _fx_fallback_er_api(base: str, target: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    # https://open.er-api.com/v6/latest/USD
    url = f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base)}"
    data = _fetch_json(
        url,
        timeout_s,
        provider="https://open.er-api.com/v6",
        deadline=_now() + max(0.0, float(timeout_s)),
    )
    if not isinstance(data, dict):
        return None
    rates = data.get("conversion_rates")
    rate = _coerce_positive_rate(rates.get(target)) if isinstance(rates, dict) else None
    if rate is None:
        return None
    _record_success("https://open.er-api.com/v6")
    return {
        "rate": rate,
        "base": base,
        "target": target,
        "as_of": _yyyy_mm_dd_from_unix(data.get("time_last_update_unix")),
        "source": "https://open.er-api.com/v6",
        "stale": False,
    }


def _fx_fallback_exchangerate_api_v4(
    base: str, target: str, timeout_s: float
) -> Optional[Dict[str, Any]]:
    # https://api.exchangerate-api.com/v4/latest/USD
    url = f"https://api.exchangerate-api.com/v4/latest/{urllib.parse.quote(base)}"
    data = _fetch_json(
        url,
        timeout_s,
        provider="https://api.exchangerate-api.com/v4",
        deadline=_now() + max(0.0, float(timeout_s)),
    )
    if not isinstance(data, dict):
        return None
    rates = data.get("rates")
    rate = _coerce_positive_rate(rates.get(target)) if isinstance(rates, dict) else None
    if rate is None:
        return None
    _record_success("https://api.exchangerate-api.com/v4")
    return {
        "rate": rate,
        "base": base,
        "target": target,
        "as_of": data.get("date") if isinstance(data.get("date"), str) else None,
        "source": "https://api.exchangerate-api.com/v4",
        "stale": False,
    }


def _try_fallback_fx(
    base: str,
    target: str,
    deadline: float,
    per_attempt_timeout: float,
) -> Optional[Dict[str, Any]]:
    """Try free/no-key fallback FX providers within the remaining deadline.

    This is a robustness-only path. It is only invoked after primary providers fail,
    and it is bounded by the same overall timeout budget so it does not increase
    normal-path latency.
    """
    if not _truthy_env("RENTAL_FX_ENABLE_FALLBACK_PROVIDERS", True):
        return None

    providers = [
        _fx_fallback_er_api,
        _fx_fallback_exchangerate_api_v4,
    ]
    for fn in providers:
        remaining = float(deadline) - _now()
        if remaining <= 0.25:
            break
        attempt_timeout = min(float(remaining), max(0.25, float(per_attempt_timeout)))
        out = fn(base, target, attempt_timeout)
        if isinstance(out, dict) and out.get("rate") is not None:
            return out
    return None


def get_fx_rate(
    base_currency: str,
    target_currency: str = DEFAULT_TARGET,
    _force_refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    _load_persistent_cache()

    base = _normalize_currency_code(base_currency)
    env_target = _get_env("RENTAL_FX_TARGET", "")
    target = _normalize_currency_code(target_currency or env_target or DEFAULT_TARGET)
    if not base or not target:
        return None
    if base == target:
        return {"rate": 1.0, "base": base, "target": target, "as_of": None, "source": "identity", "stale": False}

    cache_key = (base, target)
    ttl_seconds = _int_env("RENTAL_FX_TTL_SECONDS", int(DEFAULT_TTL_SECONDS))
    allow_stale = _truthy_env("RENTAL_FX_ALLOW_STALE", True)
    cached = _CACHE.get(cache_key)
    if cached and (_now() - cached.get("fetched_at", 0)) < ttl_seconds:
        payload = cached.get("payload")
        if isinstance(payload, dict):
            out = dict(payload)
            out["stale"] = False
            return out
        return None

    if cached and not _force_refresh and allow_stale:
        age = _now() - float(cached.get("fetched_at", 0) or 0.0)
        if age <= float(_max_stale_seconds()):
            payload = cached.get("payload")
            if isinstance(payload, dict):
                _schedule_async_refresh(base, target)
                out = dict(payload)
                out["stale"] = True
                return out

    # Fast local fallback if inverse pair is cached (e.g. have USD->SGD but need SGD->USD).
    inverse = _inverse_from_cached(base, target, ttl_seconds, allow_stale)
    if inverse is not None:
        return inverse

    # In-flight de-dupe per currency pair to reduce rate-limit failures under concurrency.
    #
    # Keep the lock held across fetch+write; otherwise concurrent callers can stampede the
    # endpoint, get blocked/429, and intermittently return None (showing USD n/a).
    lock = _pair_lock(cache_key)
    with lock:
        # Another thread may have refreshed while we were waiting.
        cached = _CACHE.get(cache_key)
        if cached and (_now() - cached.get("fetched_at", 0)) < ttl_seconds:
            payload = cached.get("payload")
            if isinstance(payload, dict):
                out = dict(payload)
                out["stale"] = False
                return out
            return None

        # If this pair recently failed, do not keep hammering the endpoint. This keeps worst-case
        # latency bounded (no extra retries) and avoids intermittent "USD n/a" under transient issues.
        fail_until = _FAIL_UNTIL.get(cache_key, 0.0)
        if _now() < float(fail_until or 0.0):
            cached = _CACHE.get(cache_key)
            if allow_stale and cached and isinstance(cached.get("payload"), dict):
                out = dict(cached["payload"])
                out["stale"] = True
                return out

            # Cooldown window after a recent failure. If another process fetched+persisted
            # the rate, pick it up here without touching the network.
            _maybe_reload_persistent_cache()
            cached = _CACHE.get(cache_key)
            if cached and (_now() - cached.get("fetched_at", 0)) < ttl_seconds:
                payload = cached.get("payload")
                if isinstance(payload, dict):
                    out = dict(payload)
                    out["stale"] = False
                    return out

            if allow_stale and cached and isinstance(cached.get("payload"), dict):
                out = dict(cached["payload"])
                out["stale"] = True
                return out

            # Robustness: if the primary provider is in cooldown and we have no usable cached rate
            # (fresh or stale), try free/no-key fallback providers with a small time budget.
            #
            # This avoids "USD n/a" on transient primary failures without increasing normal-path
            # latency, since this code only runs in the cooldown+cache-miss edge case.
            probe_budget_ms = float(
                _int_env("RENTAL_FX_COOLDOWN_FALLBACK_BUDGET_MS", 750)
            )
            if probe_budget_ms > 0:
                probe_deadline = _now() + max(0.0, probe_budget_ms / 1000.0)
                per_attempt_timeout = float(
                    _int_env("RENTAL_FX_PER_ATTEMPT_TIMEOUT", int(DEFAULT_PER_ATTEMPT_TIMEOUT))
                )
                fallback = _try_fallback_fx(base, target, probe_deadline, per_attempt_timeout)
                if isinstance(fallback, dict) and fallback.get("rate") is not None:
                    _CACHE[cache_key] = {"fetched_at": _now(), "payload": fallback}
                    _FAIL_UNTIL.pop(cache_key, None)
                    _persist_cache()
                    return dict(fallback)

            inverse = _inverse_from_cached(base, target, ttl_seconds, allow_stale)
            if inverse is not None:
                return inverse

            return None

        # If another worker process persisted a fresh rate, pull it in before we fetch.
        _maybe_reload_persistent_cache()
        cached = _CACHE.get(cache_key)
        if cached and (_now() - cached.get("fetched_at", 0)) < ttl_seconds:
            payload = cached.get("payload")
            if isinstance(payload, dict):
                out = dict(payload)
                out["stale"] = False
                return out
            return None

        inverse = _inverse_from_cached(base, target, ttl_seconds, allow_stale)
        if inverse is not None:
            return inverse

        timeout_budget = float(_int_env("RENTAL_FX_TIMEOUT", int(DEFAULT_TIMEOUT)))
        deadline = _now() + max(0.0, timeout_budget)
        per_attempt_timeout = float(
            _int_env("RENTAL_FX_PER_ATTEMPT_TIMEOUT", int(DEFAULT_PER_ATTEMPT_TIMEOUT))
        )
        query = urllib.parse.urlencode({"from": base, "to": target})

        payload: Optional[Dict[str, Any]] = None
        used_base_url: Optional[str] = None
        chosen_rate: Optional[float] = None
        last_error: Optional[Exception] = None
        for base_url in _fx_base_urls():
            remaining = deadline - _now()
            if remaining <= 0.25:
                break
            url = f"{base_url}/latest?{query}"
            # Use a per-attempt timeout slice so a slow/blocked primary doesn't consume the
            # entire overall budget and prevent fallback endpoints from being tried.
            attempt_timeout = min(float(remaining), max(0.25, float(per_attempt_timeout)))
            candidate, status_code, error_kind = _fetch_json_with_retry(
                url,
                attempt_timeout,
                provider=base_url,
                deadline=deadline,
            )
            if not isinstance(candidate, dict):
                if error_kind:
                    last_error = RuntimeError(f"{base_url}:{error_kind}:{status_code}")
                continue

            # Only accept a provider response if it actually contains the target rate.
            # Some endpoints can return partial/error JSON without the desired rate.
            rates = candidate.get("rates") if isinstance(candidate, dict) else None
            rate = _coerce_positive_rate(rates.get(target)) if isinstance(rates, dict) else None
            if rate is None:
                _record_failure(base_url, status_code=status_code, error_kind="missing_rate")
                _log_provider_error(base_url, f"No {target} rate in provider payload", f"pair={base}->{target}")
                last_error = ValueError(f"FX provider returned no rate for {base}->{target}: {base_url}")
                continue

            _record_success(base_url)
            payload = candidate
            chosen_rate = rate
            used_base_url = base_url
            break

        if payload is None or used_base_url is None or chosen_rate is None:
            # Robustness fallback: if Frankfurter endpoints fail (or return partial JSON),
            # try additional free/no-key providers within the remaining deadline budget.
            fallback = _try_fallback_fx(base, target, deadline, per_attempt_timeout)
            if isinstance(fallback, dict) and fallback.get("rate") is not None:
                _CACHE[cache_key] = {"fetched_at": _now(), "payload": fallback}
                _FAIL_UNTIL.pop(cache_key, None)
                _persist_cache()
                return dict(fallback)

            cooldown = float(_int_env("RENTAL_FX_FAIL_COOLDOWN_SECONDS", int(DEFAULT_FAIL_COOLDOWN_SECONDS)))
            _FAIL_UNTIL[cache_key] = _now() + max(0.0, cooldown)

            # Don't add latency with retries here. If we have a cached value, return it even if TTL expired.
            allow_stale = _truthy_env("RENTAL_FX_ALLOW_STALE", True)

            # Another worker process may have persisted a usable rate while we were attempting fetches.
            _maybe_reload_persistent_cache()
            cached = _CACHE.get(cache_key)
            if cached and (_now() - cached.get("fetched_at", 0)) < ttl_seconds:
                payload2 = cached.get("payload")
                if isinstance(payload2, dict):
                    out = dict(payload2)
                    out["stale"] = False
                    return out

            cached = _CACHE.get(cache_key)
            if allow_stale and cached and isinstance(cached.get("payload"), dict):
                out = dict(cached["payload"])
                out["stale"] = True
                return out
            _ = last_error  # reserved for future debug logging if needed
            return None

        result = {
            "rate": float(chosen_rate),
            "base": base,
            "target": target,
            "as_of": payload.get("date"),
            "source": used_base_url,
            "stale": False,
        }
        _CACHE[cache_key] = {"fetched_at": _now(), "payload": result}
        _FAIL_UNTIL.pop(cache_key, None)
        _persist_cache()
        return result


def convert_to_usd(amount: Optional[float], currency: Optional[str]) -> Dict[str, Any]:
    if amount is None:
        return {"amount_usd": None, "rate": None, "source": None, "as_of": None, "stale": None}
    code = _normalize_currency_code(currency)
    if not code:
        return {"amount_usd": None, "rate": None, "source": None, "as_of": None, "stale": None}
    target = _normalize_currency_code(_get_env("RENTAL_FX_TARGET", DEFAULT_TARGET)) or DEFAULT_TARGET
    fx = get_fx_rate(code, target)
    if not fx:
        return {"amount_usd": None, "rate": None, "source": None, "as_of": None, "stale": None}
    rate = _coerce_positive_rate(fx.get("rate"))
    if rate is None:
        return {"amount_usd": None, "rate": None, "source": None, "as_of": None, "stale": None}
    amount_usd = round(float(amount) * rate, 2)
    return {
        "amount_usd": amount_usd,
        "rate": rate,
        "source": fx.get("source"),
        "as_of": fx.get("as_of"),
        "stale": bool(fx.get("stale")),
    }
