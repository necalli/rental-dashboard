import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_SKILLS_DIR = os.getenv("RENTAL_AGENT_SKILLS_DIR", "backend/agent_skills")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_FILES = ("runtime.json", "skill.runtime.json")


def _resolve_path(raw: Optional[str]) -> Path:
    value = str(raw or DEFAULT_SKILLS_DIR).strip()
    if not value:
        return PROJECT_ROOT
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _parse_scalar(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    if lowered.isdigit():
        try:
            return int(lowered)
        except Exception:
            return text
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1].strip()
        if not body:
            return []
        parts = [part.strip().strip("'\"") for part in body.split(",")]
        return [part for part in parts if part]
    return text.strip("'\"")


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    source = str(text or "")
    if not source.startswith("---"):
        return {}, source.strip()
    lines = source.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, source.strip()
    end_index = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_index = idx
            break
    if end_index is None:
        return {}, source.strip()
    frontmatter_lines = lines[1:end_index]
    body = "\n".join(lines[end_index + 1 :]).strip()

    metadata: Dict[str, Any] = {}
    i = 0
    while i < len(frontmatter_lines):
        raw_line = frontmatter_lines[i]
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            i += 1
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            metadata[key] = _parse_scalar(value)
            i += 1
            continue

        # Parse block list:
        items: List[str] = []
        j = i + 1
        while j < len(frontmatter_lines):
            block = frontmatter_lines[j]
            block_stripped = block.strip()
            if not block_stripped:
                j += 1
                continue
            if block_stripped.startswith("- "):
                items.append(block_stripped[2:].strip().strip("'\""))
                j += 1
                continue
            if ":" in block_stripped and not block.startswith((" ", "\t")):
                break
            break
        metadata[key] = items
        i = j
    return metadata, body


def _normalize_tools(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]
    return []


def _normalize_keywords(value: Any) -> List[str]:
    if isinstance(value, list):
        parts = [str(item).strip().lower() for item in value if str(item).strip()]
    elif isinstance(value, str):
        parts = [part.strip().lower() for part in value.split(",")]
    else:
        parts = []
    output: List[str] = []
    seen = set()
    for part in parts:
        if not part:
            continue
        normalized = re.sub(r"\s+", " ", part).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _load_runtime_config(skill_dir: Path) -> Dict[str, Any]:
    for filename in RUNTIME_CONFIG_FILES:
        candidate = skill_dir / filename
        if not candidate.exists():
            continue
        payload = _read_json(candidate)
        if payload is not None:
            return payload
    return {}


def _skill_from_markdown(skill_dir: Path, skill_file: Path) -> Optional[Dict[str, Any]]:
    text = _read_text(skill_file)
    if text is None:
        return None
    metadata, body = _split_frontmatter(text)
    runtime = _load_runtime_config(skill_dir)
    skill_id = skill_dir.name
    tools = _normalize_tools(
        runtime.get("tools")
        if "tools" in runtime
        else (metadata.get("tools") or metadata.get("allowed-tools"))
    )
    trigger_keywords = _normalize_keywords(
        runtime.get("trigger_keywords")
        if "trigger_keywords" in runtime
        else (metadata.get("trigger-keywords") or metadata.get("trigger_keywords"))
    )
    always_on = bool(runtime.get("always_on", False))

    if "enabled" in runtime:
        enabled = bool(runtime.get("enabled"))
    elif "enabled" in metadata:
        enabled = bool(metadata.get("enabled"))
    else:
        disabled_by_model = bool(metadata.get("disable-model-invocation"))
        user_invocable = bool(metadata.get("user-invocable", True))
        enabled = (not disabled_by_model) and user_invocable

    skill: Dict[str, Any] = {
        "skill_id": skill_id,
        "name": str(metadata.get("name") or skill_id),
        "description": str(metadata.get("description") or "").strip(),
        "instruction": body.strip(),
        "tools": tools,
        "enabled": enabled,
        "phase": runtime.get("phase", metadata.get("phase")),
        "placeholder": bool(runtime.get("placeholder", metadata.get("placeholder", False))),
        "always_on": always_on,
        "trigger_keywords": trigger_keywords,
        "raw_frontmatter": metadata,
        "runtime": runtime,
        "path": str(skill_file),
    }
    return skill


def load_skill_packages(skills_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    root = _resolve_path(skills_dir)
    if not root.exists() or not root.is_dir():
        return []
    output: List[Dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _skill_from_markdown(child, skill_file)
        if skill:
            output.append(skill)
    return output


def enabled_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [skill for skill in skills if bool(skill.get("enabled"))]


def enabled_tool_names(skills: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    seen = set()
    for skill in enabled_skills(skills):
        tool_names = skill.get("tools")
        if not isinstance(tool_names, list):
            continue
        for tool_name in tool_names:
            value = str(tool_name or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            names.append(value)
    return names


def select_skills_for_message(skills: List[Dict[str, Any]], message: str) -> List[Dict[str, Any]]:
    active = enabled_skills(skills)
    if not active:
        return []

    lowered = re.sub(r"\s+", " ", str(message or "").lower()).strip()
    if not lowered:
        return active

    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    unscoped: List[Dict[str, Any]] = []
    for skill in active:
        skill_id = str(skill.get("skill_id") or "").strip()
        if bool(skill.get("always_on")):
            if skill_id not in selected_ids:
                selected_ids.add(skill_id)
                selected.append(skill)
            continue
        keywords = skill.get("trigger_keywords") if isinstance(skill.get("trigger_keywords"), list) else []
        if not keywords:
            unscoped.append(skill)
            continue
        if any(keyword in lowered for keyword in keywords):
            if skill_id not in selected_ids:
                selected_ids.add(skill_id)
                selected.append(skill)

    if selected:
        return selected
    if unscoped:
        return unscoped
    return active


def skill_system_prompt(
    skills: List[Dict[str, Any]],
    *,
    selected_skill_ids: Optional[List[str]] = None,
) -> str:
    active = enabled_skills(skills)
    if selected_skill_ids:
        wanted = {str(value or "").strip() for value in selected_skill_ids if str(value or "").strip()}
        if wanted:
            scoped = [skill for skill in active if str(skill.get("skill_id") or "").strip() in wanted]
            if scoped:
                active = scoped
    if not active:
        return (
            "You are the rental assistant. Use available tools conservatively and return concise, factual answers."
        )
    lines: List[str] = [
        "You are the rental assistant running with explicit skill packages.",
        "The listed skills are the active scope for this user request.",
        "Use only the provided tools when needed and avoid guessing unknown facts.",
        "If required identifiers are missing, ask for them directly.",
        "Response style requirements:",
        "- Keep responses clean and professional.",
        "- Do not use emojis or decorative symbols.",
        "- Use concise markdown structure (short headings, bullets, and tables only when useful).",
    ]
    for skill in active:
        lines.append(f"Skill: {skill.get('name')}")
        description = str(skill.get("description") or "").strip()
        if description:
            lines.append(f"Description: {description}")
        instruction = str(skill.get("instruction") or "").strip()
        if instruction:
            lines.append(f"Instruction: {instruction}")
        tools = skill.get("tools") if isinstance(skill.get("tools"), list) else []
        if tools:
            lines.append(f"Tools: {', '.join(str(item) for item in tools)}")
    return "\n".join(lines)
