import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from .config import DATA_DIR, DB_PATH


class Storage:
    def __init__(self, db_path: Optional[str] = None) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db_path = db_path or DB_PATH
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT,
                    payload_json TEXT,
                    status TEXT,
                    result_ref TEXT,
                    error TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    listing_id TEXT PRIMARY KEY,
                    source TEXT,
                    url TEXT,
                    title TEXT,
                    payload_json TEXT,
                    raw_refs_json TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    listing_id TEXT,
                    payload_json TEXT,
                    raw_ref TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_runs (
                    run_id TEXT PRIMARY KEY,
                    params_json TEXT,
                    result_json TEXT,
                    raw_refs_json TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS search_listings (
                    run_id TEXT,
                    listing_id TEXT,
                    payload_json TEXT,
                    created_at INTEGER,
                    PRIMARY KEY (run_id, listing_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_payloads (
                    raw_id TEXT PRIMARY KEY,
                    kind TEXT,
                    path TEXT,
                    metadata_json TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichments (
                    enrichment_id TEXT PRIMARY KEY,
                    listing_id TEXT,
                    kind TEXT,
                    model TEXT,
                    prompt_version TEXT,
                    input_hash TEXT,
                    output_json TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_metrics (
                    metric_id TEXT PRIMARY KEY,
                    job_id TEXT,
                    job_type TEXT,
                    status TEXT,
                    metrics_json TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_metrics_created_at ON job_metrics(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_metrics_job_type ON job_metrics(job_type)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_memories (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    source_type TEXT,
                    title TEXT,
                    filename TEXT,
                    mime_type TEXT,
                    tags_json TEXT,
                    metadata_json TEXT,
                    content_text TEXT,
                    content_hash TEXT,
                    status TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rag_memories_user_updated
                ON rag_memories(user_id, updated_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    memory_id TEXT,
                    user_id TEXT,
                    chunk_index INTEGER,
                    chunk_text TEXT,
                    embedding_json TEXT,
                    metadata_json TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rag_chunks_user_memory
                ON rag_chunks(user_id, memory_id, chunk_index)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rag_chunks_memory
                ON rag_chunks(memory_id, chunk_index)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT,
                    updated_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated
                ON agent_sessions(updated_at)
                """
            )
            conn.commit()

    def _now(self) -> int:
        return int(time.time())

    def create_job(self, job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (job_id, job_type, payload_json, status, result_ref, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    json.dumps(payload or {}),
                    "queued",
                    None,
                    None,
                    now,
                    now,
                ),
            )
            conn.commit()
        return {
            "job_id": job_id,
            "job_type": job_type,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "payload": payload or {},
        }

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        result_ref: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, result_ref = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, result_ref, error, now, job_id),
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        if not job_id:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT job_id, job_type, payload_json, status, result_ref, error, created_at, updated_at FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": row[0],
            "job_type": row[1],
            "payload": json.loads(row[2] or "{}"),
            "status": row[3],
            "result_ref": row[4],
            "error": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, int(limit or 50))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT job_id, job_type, payload_json, status, result_ref, error, created_at, updated_at FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        jobs: List[Dict[str, Any]] = []
        for row in rows:
            jobs.append(
                {
                    "job_id": row[0],
                    "job_type": row[1],
                    "payload": json.loads(row[2] or "{}"),
                    "status": row[3],
                    "result_ref": row[4],
                    "error": row[5],
                    "created_at": row[6],
                    "updated_at": row[7],
                }
            )
        return jobs

    def claim_next_job(self, job_types: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        query = (
            "SELECT job_id, job_type, payload_json, status, result_ref, error, created_at, updated_at "
            "FROM jobs WHERE status = ?"
        )
        params: List[Any] = ["queued"]
        if job_types:
            placeholders = ",".join(["?"] * len(job_types))
            query += f" AND job_type IN ({placeholders})"
            params.extend(job_types)
        query += " ORDER BY created_at ASC LIMIT 1"
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(query, params).fetchone()
            if not row:
                conn.commit()
                return None
            job_id = row[0]
            now = self._now()
            result = conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ? AND status = ?",
                ("running", now, job_id, "queued"),
            )
            if int(result.rowcount or 0) != 1:
                conn.rollback()
                return None
            conn.commit()
        return {
            "job_id": row[0],
            "job_type": row[1],
            "payload": json.loads(row[2] or "{}"),
            "status": "running",
            "result_ref": row[4],
            "error": row[5],
            "created_at": row[6],
            "updated_at": now,
        }

    def get_job_status_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {
            "queued": 0,
            "running": 0,
            "complete": 0,
            "failed": 0,
            "total": 0,
        }
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(1) FROM jobs GROUP BY status"
            ).fetchall()
        total = 0
        for status, value in rows:
            key = str(status or "").strip().lower() or "unknown"
            count = int(value or 0)
            counts[key] = count
            total += count
        counts["total"] = total
        return counts

    def upsert_listing(self, listing: Dict[str, Any]) -> Dict[str, Any]:
        listing_id = str(listing.get("id") or listing.get("listing_id") or "").strip()
        if not listing_id:
            raise ValueError("listing_id is required")
        now = self._now()
        payload_json = json.dumps(listing)
        raw_refs = listing.get("raw_payload_refs") or []
        raw_refs_json = json.dumps(raw_refs)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO listings (listing_id, source, url, title, payload_json, raw_refs_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    source = excluded.source,
                    url = excluded.url,
                    title = excluded.title,
                    payload_json = excluded.payload_json,
                    raw_refs_json = excluded.raw_refs_json,
                    updated_at = excluded.updated_at
                """,
                (
                    listing_id,
                    listing.get("source"),
                    listing.get("url"),
                    listing.get("title"),
                    payload_json,
                    raw_refs_json,
                    now,
                    now,
                ),
            )
            conn.commit()
        listing["updated_at"] = now
        return listing

    def get_listing(self, listing_id: str) -> Optional[Dict[str, Any]]:
        if not listing_id:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload_json FROM listings WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0] or "{}")
        if isinstance(payload, dict):
            payload.setdefault("id", listing_id)
            payload.setdefault("listing_id", listing_id)
        return payload

    def list_listings(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, int(limit or 50))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT listing_id, payload_json FROM listings ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        listings = []
        for listing_id, payload_json in rows:
            payload = json.loads(payload_json or "{}")
            if isinstance(payload, dict):
                payload.setdefault("id", listing_id)
                payload.setdefault("listing_id", listing_id)
            listings.append(payload)
        return listings

    def add_reviews(self, listing_id: str, reviews: List[Dict[str, Any]]) -> int:
        if not listing_id or not reviews:
            return 0
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            count = 0
            for review in reviews:
                review_id = str(review.get("id") or review.get("review_id") or uuid.uuid4())
                payload_json = json.dumps(review)
                raw_ref = review.get("raw_payload_ref")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO reviews (review_id, listing_id, payload_json, raw_ref, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (review_id, listing_id, payload_json, raw_ref, now),
                )
                count += 1
            conn.commit()
        return count

    def list_reviews(self, listing_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        if not listing_id:
            return []
        limit = max(1, int(limit or 200))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM reviews WHERE listing_id = ? ORDER BY created_at DESC LIMIT ?",
                (listing_id, limit),
            ).fetchall()
        return [json.loads(row[0] or "{}") for row in rows]

    def add_raw_payload(self, kind: str, path: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        raw_id = str(uuid.uuid4())
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO raw_payloads (raw_id, kind, path, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (raw_id, kind, path, json.dumps(metadata or {}), now),
            )
            conn.commit()
        return raw_id

    def add_search_run(
        self,
        params: Dict[str, Any],
        result: Dict[str, Any],
        raw_refs: List[str],
    ) -> str:
        run_id = str(uuid.uuid4())
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO search_runs (run_id, params_json, result_json, raw_refs_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    json.dumps(params or {}),
                    json.dumps(result or {}),
                    json.dumps(raw_refs or []),
                    now,
                ),
            )
            conn.commit()
        return run_id

    def add_search_listings(self, run_id: str, listings: List[Dict[str, Any]]) -> int:
        if not run_id or not listings:
            return 0
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            count = 0
            for listing in listings:
                listing_id = str(listing.get("id") or listing.get("listing_id") or "").strip()
                if not listing_id:
                    continue
                payload_json = json.dumps(listing)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO search_listings (run_id, listing_id, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run_id, listing_id, payload_json, now),
                )
                count += 1
            conn.commit()
        return count

    def list_search_listings(self, run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        if not run_id:
            return []
        limit = max(1, int(limit or 200))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM search_listings WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [json.loads(row[0] or "{}") for row in rows]

    def list_search_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, int(limit or 50))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT run_id, params_json, result_json, raw_refs_json, created_at FROM search_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "run_id": row[0],
                    "params": json.loads(row[1] or "{}"),
                    "result": json.loads(row[2] or "{}"),
                    "raw_refs": json.loads(row[3] or "[]"),
                    "created_at": row[4],
                }
            )
        return output

    def upsert_agent_session_state(self, session_id: str, state: Dict[str, Any]) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        now = self._now()
        payload = json.dumps(state or {})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_sessions (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (sid, payload, now),
            )
            conn.commit()

    def get_agent_session_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT state_json FROM agent_sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return None
        return payload

    def list_raw_payloads(self, kind: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, int(limit or 100))
        query = "SELECT raw_id, kind, path, metadata_json, created_at FROM raw_payloads"
        params: List[Any] = []
        if kind:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "raw_id": row[0],
                    "kind": row[1],
                    "path": row[2],
                    "metadata": json.loads(row[3] or "{}"),
                    "created_at": row[4],
                }
            )
        return output

    def add_enrichment(
        self,
        listing_id: str,
        kind: str,
        model: str,
        prompt_version: str,
        input_hash: str,
        output: Dict[str, Any],
    ) -> str:
        enrichment_id = str(uuid.uuid4())
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO enrichments (enrichment_id, listing_id, kind, model, prompt_version, input_hash, output_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    enrichment_id,
                    listing_id,
                    kind,
                    model,
                    prompt_version,
                    input_hash,
                    json.dumps(output or {}),
                    now,
                ),
            )
            conn.commit()
        return enrichment_id

    def get_enrichment_by_hash(
        self,
        listing_id: str,
        kind: str,
        model: str,
        prompt_version: str,
        input_hash: str,
    ) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT output_json FROM enrichments
                WHERE listing_id = ? AND kind = ? AND model = ? AND prompt_version = ? AND input_hash = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (listing_id, kind, model, prompt_version, input_hash),
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0] or "{}")

    def get_latest_enrichment(
        self,
        listing_id: str,
        kind: str,
        model: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        query = "SELECT output_json FROM enrichments WHERE listing_id = ? AND kind = ?"
        params: List[Any] = [listing_id, kind]
        if model:
            query += " AND model = ?"
            params.append(model)
        if prompt_version:
            query += " AND prompt_version = ?"
            params.append(prompt_version)
        query += " ORDER BY created_at DESC LIMIT 1"
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(query, params).fetchone()
        if not row:
            return None
        return json.loads(row[0] or "{}")

    def add_job_metric(
        self,
        job_id: str,
        job_type: str,
        status: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> str:
        metric_id = str(uuid.uuid4())
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO job_metrics (metric_id, job_id, job_type, status, metrics_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    metric_id,
                    job_id,
                    job_type,
                    status,
                    json.dumps(metrics or {}),
                    now,
                ),
            )
            conn.commit()
        return metric_id

    def list_job_metrics(
        self,
        limit: int = 100,
        job_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit = max(1, int(limit or 100))
        query = (
            "SELECT metric_id, job_id, job_type, status, metrics_json, created_at "
            "FROM job_metrics"
        )
        where: List[str] = []
        params: List[Any] = []
        if job_type:
            where.append("job_type = ?")
            params.append(str(job_type))
        if status:
            where.append("status = ?")
            params.append(str(status))
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        output: List[Dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "metric_id": row[0],
                    "job_id": row[1],
                    "job_type": row[2],
                    "status": row[3],
                    "metrics": json.loads(row[4] or "{}"),
                    "created_at": row[5],
                }
            )
        return output

    def add_rag_memory(
        self,
        *,
        user_id: str,
        source_type: str,
        title: str,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        content_text: str,
        content_hash: str,
        status: str = "ready",
    ) -> Dict[str, Any]:
        memory_id = str(uuid.uuid4())
        now = self._now()
        payload = {
            "memory_id": memory_id,
            "user_id": str(user_id or "").strip(),
            "source_type": str(source_type or "").strip() or "upload",
            "title": str(title or "").strip() or "Untitled memory",
            "filename": str(filename or "").strip() or None,
            "mime_type": str(mime_type or "").strip() or None,
            "tags": [str(item).strip() for item in (tags or []) if str(item).strip()],
            "metadata": metadata or {},
            "content_text": str(content_text or ""),
            "content_hash": str(content_hash or "").strip(),
            "status": str(status or "ready").strip() or "ready",
            "created_at": now,
            "updated_at": now,
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO rag_memories (
                    memory_id, user_id, source_type, title, filename, mime_type,
                    tags_json, metadata_json, content_text, content_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["memory_id"],
                    payload["user_id"],
                    payload["source_type"],
                    payload["title"],
                    payload["filename"],
                    payload["mime_type"],
                    json.dumps(payload["tags"]),
                    json.dumps(payload["metadata"]),
                    payload["content_text"],
                    payload["content_hash"],
                    payload["status"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
            conn.commit()
        return payload

    def update_rag_memory(
        self,
        memory_id: str,
        *,
        status: Optional[str] = None,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not memory_id:
            return
        updates: List[str] = []
        params: List[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(str(status))
        if title is not None:
            updates.append("title = ?")
            params.append(str(title))
        if tags is not None:
            updates.append("tags_json = ?")
            params.append(json.dumps([str(item).strip() for item in tags if str(item).strip()]))
        if metadata is not None:
            updates.append("metadata_json = ?")
            params.append(json.dumps(metadata or {}))
        if not updates:
            return
        updates.append("updated_at = ?")
        params.append(self._now())
        params.append(memory_id)
        query = f"UPDATE rag_memories SET {', '.join(updates)} WHERE memory_id = ?"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(query, params)
            conn.commit()

    def get_rag_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        if not memory_id:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT memory_id, user_id, source_type, title, filename, mime_type,
                       tags_json, metadata_json, content_text, content_hash, status, created_at, updated_at
                FROM rag_memories
                WHERE memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "memory_id": row[0],
            "user_id": row[1],
            "source_type": row[2],
            "title": row[3],
            "filename": row[4],
            "mime_type": row[5],
            "tags": json.loads(row[6] or "[]"),
            "metadata": json.loads(row[7] or "{}"),
            "content_text": row[8] or "",
            "content_hash": row[9],
            "status": row[10],
            "created_at": row[11],
            "updated_at": row[12],
        }

    def get_rag_memory_by_hash(self, user_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
        if not user_id or not content_hash:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT memory_id
                FROM rag_memories
                WHERE user_id = ? AND content_hash = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, content_hash),
            ).fetchone()
        if not row:
            return None
        return self.get_rag_memory(str(row[0]))

    def list_rag_memories(self, *, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        limit = max(1, int(limit or 100))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT memory_id, user_id, source_type, title, filename, mime_type,
                       tags_json, metadata_json, content_text, content_hash, status, created_at, updated_at
                FROM rag_memories
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "memory_id": row[0],
                    "user_id": row[1],
                    "source_type": row[2],
                    "title": row[3],
                    "filename": row[4],
                    "mime_type": row[5],
                    "tags": json.loads(row[6] or "[]"),
                    "metadata": json.loads(row[7] or "{}"),
                    "content_text": row[8] or "",
                    "content_hash": row[9],
                    "status": row[10],
                    "created_at": row[11],
                    "updated_at": row[12],
                }
            )
        return output

    def delete_rag_memory(self, *, memory_id: str, user_id: Optional[str] = None) -> bool:
        if not memory_id:
            return False
        with sqlite3.connect(self.db_path) as conn:
            if user_id:
                row = conn.execute(
                    "SELECT memory_id FROM rag_memories WHERE memory_id = ? AND user_id = ?",
                    (memory_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT memory_id FROM rag_memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM rag_chunks WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM rag_memories WHERE memory_id = ?", (memory_id,))
            conn.commit()
        return True

    def replace_rag_chunks(
        self,
        *,
        memory_id: str,
        user_id: str,
        chunks: List[Dict[str, Any]],
    ) -> int:
        if not memory_id:
            return 0
        now = self._now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM rag_chunks WHERE memory_id = ?", (memory_id,))
            inserted = 0
            for index, chunk in enumerate(chunks):
                chunk_id = str(chunk.get("chunk_id") or str(uuid.uuid4()))
                chunk_text = str(chunk.get("chunk_text") or "").strip()
                if not chunk_text:
                    continue
                embedding = chunk.get("embedding")
                if not isinstance(embedding, list):
                    continue
                metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
                conn.execute(
                    """
                    INSERT INTO rag_chunks (
                        chunk_id, memory_id, user_id, chunk_index, chunk_text, embedding_json, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        memory_id,
                        user_id,
                        int(chunk.get("chunk_index") if chunk.get("chunk_index") is not None else index),
                        chunk_text,
                        json.dumps(embedding),
                        json.dumps(metadata),
                        now,
                    ),
                )
                inserted += 1
            conn.commit()
        return inserted

    def list_rag_chunks(
        self,
        *,
        user_id: str,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        limit = max(1, int(limit or 5000))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, memory_id, user_id, chunk_index, chunk_text, embedding_json, metadata_json, created_at
                FROM rag_chunks
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        output: List[Dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "chunk_id": row[0],
                    "memory_id": row[1],
                    "user_id": row[2],
                    "chunk_index": row[3],
                    "chunk_text": row[4] or "",
                    "embedding": json.loads(row[5] or "[]"),
                    "metadata": json.loads(row[6] or "{}"),
                    "created_at": row[7],
                }
            )
        return output
