import hashlib
import io
import math
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

from .storage import Storage


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)


def _normalize_text(value: str) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    compact = "\n".join([line for line in lines if line])
    return compact.strip()


def _tokenize(value: str) -> List[str]:
    return re.findall(r"[a-z0-9][a-z0-9_\-']*", str(value or "").lower())


def _split_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[,\n]+", str(raw))
    cleaned: List[str] = []
    seen = set()
    for item in parts:
        value = str(item or "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


class PersonalityRagService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.default_user_id = str(os.getenv("RENTAL_RAG_DEFAULT_USER_ID", "default-user")).strip()
        self.max_upload_bytes = max(
            1_000_000,
            _to_int(os.getenv("RENTAL_RAG_MAX_UPLOAD_BYTES"), 8_000_000),
        )
        self.chunk_chars = max(200, _to_int(os.getenv("RENTAL_RAG_CHUNK_CHARS"), 1200))
        self.chunk_overlap = max(0, _to_int(os.getenv("RENTAL_RAG_CHUNK_OVERLAP"), 200))
        self.max_scan_chunks = max(100, _to_int(os.getenv("RENTAL_RAG_MAX_SCAN_CHUNKS"), 5000))
        self.default_limit = max(1, _to_int(os.getenv("RENTAL_RAG_DEFAULT_LIMIT"), 6))
        self.max_limit = max(1, _to_int(os.getenv("RENTAL_RAG_MAX_LIMIT"), 12))
        self.embed_dim = max(64, _to_int(os.getenv("RENTAL_RAG_EMBED_DIM"), 384))
        self.embed_provider = str(os.getenv("RENTAL_RAG_EMBED_PROVIDER", "auto")).strip().lower()
        self.embed_model_name = str(
            os.getenv("RENTAL_RAG_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        ).strip()
        self._embed_model: Any = None

    def resolve_user_id(self, user_id: Optional[str]) -> str:
        value = str(user_id or "").strip()
        return value or self.default_user_id

    def parse_file_text(self, *, filename: str, mime_type: Optional[str], raw_bytes: bytes) -> str:
        if not raw_bytes:
            raise ValueError("file is empty")
        if len(raw_bytes) > self.max_upload_bytes:
            raise ValueError(f"file exceeds max size ({self.max_upload_bytes} bytes)")

        name = str(filename or "").strip()
        ext = os.path.splitext(name.lower())[1]
        mime = str(mime_type or "").lower()
        if ext == ".txt" or "text/plain" in mime:
            text = raw_bytes.decode("utf-8", errors="ignore")
        elif ext == ".docx" or "officedocument.wordprocessingml.document" in mime:
            text = self._extract_docx_text(raw_bytes)
        elif ext == ".pdf" or "application/pdf" in mime:
            text = self._extract_pdf_text(raw_bytes)
        else:
            raise ValueError("unsupported file type; allowed: .txt, .docx, .pdf")

        normalized = _normalize_text(text)
        if not normalized:
            raise ValueError("no extractable text found in file")
        return normalized

    def ingest_upload(
        self,
        *,
        user_id: Optional[str],
        filename: str,
        mime_type: Optional[str],
        raw_bytes: bytes,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_user = self.resolve_user_id(user_id)
        text = self.parse_file_text(filename=filename, mime_type=mime_type, raw_bytes=raw_bytes)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self.storage.get_rag_memory_by_hash(resolved_user, content_hash)
        if existing:
            chunks = self.storage.list_rag_chunks(user_id=resolved_user, limit=self.max_scan_chunks)
            chunk_count = len([item for item in chunks if str(item.get("memory_id") or "") == existing["memory_id"]])
            existing_meta = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
            existing_meta.setdefault("chunk_count", chunk_count)
            return {
                "memory": existing,
                "chunk_count": chunk_count,
                "duplicate": True,
                "provider": existing_meta.get("embed_provider") or self.active_embed_provider(),
            }

        memory = self.storage.add_rag_memory(
            user_id=resolved_user,
            source_type="upload",
            title=str(title or "").strip() or (str(filename or "").strip() or "Uploaded memory"),
            filename=filename,
            mime_type=mime_type,
            tags=tags or [],
            metadata=metadata or {},
            content_text=text,
            content_hash=content_hash,
            status="processing",
        )

        chunks = self._chunk_text(text)
        vectors = self._embed_texts(chunks)
        chunk_rows: List[Dict[str, Any]] = []
        for index, chunk_text in enumerate(chunks):
            chunk_rows.append(
                {
                    "chunk_index": index,
                    "chunk_text": chunk_text,
                    "embedding": vectors[index],
                    "metadata": {"source_type": "upload", "filename": filename},
                }
            )
        chunk_count = self.storage.replace_rag_chunks(
            memory_id=memory["memory_id"],
            user_id=resolved_user,
            chunks=chunk_rows,
        )
        merged_meta = dict(memory.get("metadata") or {})
        merged_meta.update(
            {
                "chunk_count": chunk_count,
                "embed_provider": self.active_embed_provider(),
                "embed_dim": self.embed_dim,
            }
        )
        self.storage.update_rag_memory(
            memory["memory_id"],
            status="ready",
            metadata=merged_meta,
        )
        saved = self.storage.get_rag_memory(memory["memory_id"]) or memory
        return {
            "memory": saved,
            "chunk_count": chunk_count,
            "duplicate": False,
            "provider": self.active_embed_provider(),
        }

    def upsert_memory_text(
        self,
        *,
        user_id: Optional[str],
        title: str,
        text: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_user = self.resolve_user_id(user_id)
        normalized = _normalize_text(text)
        if not normalized:
            raise ValueError("text is required")
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        existing = self.storage.get_rag_memory_by_hash(resolved_user, content_hash)
        if existing:
            return {"memory": existing, "chunk_count": existing.get("metadata", {}).get("chunk_count", 0), "duplicate": True}

        memory = self.storage.add_rag_memory(
            user_id=resolved_user,
            source_type="manual",
            title=str(title or "").strip() or "Manual memory",
            filename=None,
            mime_type="text/plain",
            tags=tags or [],
            metadata=metadata or {},
            content_text=normalized,
            content_hash=content_hash,
            status="processing",
        )
        chunks = self._chunk_text(normalized)
        vectors = self._embed_texts(chunks)
        chunk_rows: List[Dict[str, Any]] = []
        for index, chunk_text in enumerate(chunks):
            chunk_rows.append(
                {
                    "chunk_index": index,
                    "chunk_text": chunk_text,
                    "embedding": vectors[index],
                    "metadata": {"source_type": "manual"},
                }
            )
        chunk_count = self.storage.replace_rag_chunks(
            memory_id=memory["memory_id"],
            user_id=resolved_user,
            chunks=chunk_rows,
        )
        merged_meta = dict(memory.get("metadata") or {})
        merged_meta.update(
            {
                "chunk_count": chunk_count,
                "embed_provider": self.active_embed_provider(),
                "embed_dim": self.embed_dim,
            }
        )
        self.storage.update_rag_memory(memory["memory_id"], status="ready", metadata=merged_meta)
        saved = self.storage.get_rag_memory(memory["memory_id"]) or memory
        return {"memory": saved, "chunk_count": chunk_count, "duplicate": False}

    def query_context(
        self,
        *,
        user_id: Optional[str],
        query: str,
        limit: Optional[int] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        resolved_user = self.resolve_user_id(user_id)
        query_text = _normalize_text(query)
        if not query_text:
            return {"user_id": resolved_user, "query": "", "hits": [], "profile": {"top_tags": []}}

        size = max(1, min(int(limit or self.default_limit), self.max_limit))
        chunks = self.storage.list_rag_chunks(user_id=resolved_user, limit=self.max_scan_chunks)
        memories = self.storage.list_rag_memories(user_id=resolved_user, limit=self.max_scan_chunks)
        memory_by_id = {str(item.get("memory_id") or ""): item for item in memories}
        filtered_tags = _split_tags(tags)
        query_vec = self._embed_texts([query_text])[0]

        def _rank(required_tags: Optional[List[str]]) -> List[Dict[str, Any]]:
            scored_local: List[Dict[str, Any]] = []
            for chunk in chunks:
                memory_id = str(chunk.get("memory_id") or "")
                memory = memory_by_id.get(memory_id)
                if not memory:
                    continue
                if str(memory.get("status") or "") != "ready":
                    continue
                memory_tags = [str(item).lower() for item in (memory.get("tags") or [])]
                if required_tags and not set(required_tags).issubset(set(memory_tags)):
                    continue
                embedding = chunk.get("embedding")
                if not isinstance(embedding, list) or not embedding:
                    continue
                score = self._dot(query_vec, embedding)
                scored_local.append(
                    {
                        "memory": memory,
                        "chunk": chunk,
                        "score": float(score),
                    }
                )
            scored_local.sort(key=lambda item: item["score"], reverse=True)
            return scored_local

        scored = _rank(filtered_tags)
        tag_filter_relaxed = False
        if not scored and filtered_tags:
            scored = _rank(None)
            tag_filter_relaxed = True

        hits: List[Dict[str, Any]] = []
        for item in scored[:size]:
            memory = item["memory"]
            chunk = item["chunk"]
            citation = {
                "memory_id": memory.get("memory_id"),
                "title": memory.get("title"),
                "filename": memory.get("filename"),
                "source_type": memory.get("source_type"),
                "tags": memory.get("tags") or [],
                "created_at": memory.get("created_at"),
            }
            hits.append(
                {
                    "memory_id": memory.get("memory_id"),
                    "chunk_id": chunk.get("chunk_id"),
                    "chunk_index": chunk.get("chunk_index"),
                    "score": round(float(item["score"]), 4),
                    "text": str(chunk.get("chunk_text") or ""),
                    "citation": citation,
                }
            )
        return {
            "user_id": resolved_user,
            "query": query_text,
            "tags_applied": filtered_tags,
            "tag_filter_relaxed": bool(tag_filter_relaxed),
            "hits": hits,
            "profile": self._build_profile(hits),
        }

    def list_memories(self, *, user_id: Optional[str], limit: int = 100) -> List[Dict[str, Any]]:
        resolved_user = self.resolve_user_id(user_id)
        rows = self.storage.list_rag_memories(user_id=resolved_user, limit=limit)
        out: List[Dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry.pop("content_text", None)
            out.append(entry)
        return out

    def delete_memory(self, *, memory_id: str, user_id: Optional[str]) -> bool:
        resolved_user = self.resolve_user_id(user_id)
        return self.storage.delete_rag_memory(memory_id=memory_id, user_id=resolved_user)

    def active_embed_provider(self) -> str:
        if self.embed_provider == "hash":
            return "hash"
        if self.embed_provider == "sentence_transformers":
            return "sentence_transformers" if self._try_load_sentence_transformers() else "hash"
        if self._try_load_sentence_transformers():
            return "sentence_transformers"
        return "hash"

    def _try_load_sentence_transformers(self) -> bool:
        if self._embed_model is not None:
            return True
        try:
            from sentence_transformers import SentenceTransformer

            self._embed_model = SentenceTransformer(self.embed_model_name)
            return True
        except Exception:
            self._embed_model = None
            return False

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        provider = self.active_embed_provider()
        if provider == "sentence_transformers" and self._embed_model is not None:
            try:
                vectors = self._embed_model.encode(texts, normalize_embeddings=True)
                out: List[List[float]] = []
                for vec in vectors:
                    out.append([float(item) for item in list(vec)])
                return out
            except Exception:
                pass
        return [self._hash_embedding(text) for text in texts]

    def _hash_embedding(self, text: str) -> List[float]:
        vector = [0.0] * self.embed_dim
        tokens = _tokenize(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.embed_dim
            sign = 1.0 if (digest[4] % 2 == 0) else -1.0
            weight = 1.0 + (digest[5] / 255.0)
            vector[bucket] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    def _dot(self, a: List[float], b: List[float]) -> float:
        size = min(len(a), len(b))
        if size <= 0:
            return 0.0
        return float(sum(float(a[i]) * float(b[i]) for i in range(size)))

    def _chunk_text(self, text: str) -> List[str]:
        source = _normalize_text(text)
        if not source:
            return []
        if len(source) <= self.chunk_chars:
            return [source]
        chunks: List[str] = []
        start = 0
        while start < len(source):
            end = min(len(source), start + self.chunk_chars)
            chunk = source[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(source):
                break
            start = max(0, end - self.chunk_overlap)
        return chunks

    def _extract_docx_text(self, raw_bytes: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
                xml_bytes = archive.read("word/document.xml")
        except Exception as exc:
            raise ValueError(f"unable to parse docx: {exc}") from exc

        try:
            root = ElementTree.fromstring(xml_bytes)
            text_nodes = []
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    text_nodes.append(node.text)
            return "\n".join(text_nodes)
        except Exception as exc:
            raise ValueError(f"unable to parse docx xml: {exc}") from exc

    def _extract_pdf_text(self, raw_bytes: bytes) -> str:
        try:
            from pypdf import PdfReader
        except Exception as exc:
            raise ValueError("PDF support requires `pypdf` to be installed") from exc

        try:
            reader = PdfReader(io.BytesIO(raw_bytes))
            pages: List[str] = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(page_text)
            return "\n".join(pages)
        except Exception as exc:
            raise ValueError(f"unable to parse pdf: {exc}") from exc

    def _build_profile(self, hits: List[Dict[str, Any]]) -> Dict[str, Any]:
        tag_counts: Dict[str, int] = {}
        for item in hits:
            citation = item.get("citation") if isinstance(item.get("citation"), dict) else {}
            tags = citation.get("tags") if isinstance(citation.get("tags"), list) else []
            for tag in tags:
                value = str(tag or "").strip().lower()
                if not value:
                    continue
                tag_counts[value] = int(tag_counts.get(value, 0) or 0) + 1
        top_tags = sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        return {
            "top_tags": [{"tag": key, "count": value} for key, value in top_tags],
        }
