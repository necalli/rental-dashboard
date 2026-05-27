import json
import os
import time
import uuid
from typing import Any, Dict

from .config import RAW_DIR


def _timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def write_raw_payload(kind: str, key: str, payload: Dict[str, Any]) -> str:
    safe_kind = (kind or "unknown").strip().lower() or "unknown"
    safe_key = (key or "unknown").strip() or "unknown"
    base_dir = os.path.join(RAW_DIR, safe_kind, safe_key)
    os.makedirs(base_dir, exist_ok=True)
    unique = uuid.uuid4().hex[:12]
    filename = f"{_timestamp()}_{unique}_{safe_kind}.json"
    path = os.path.join(base_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path
