from typing import Any, Callable, Dict, Optional

ParserFn = Callable[[Dict[str, Any]], Dict[str, Any]]


class ParserRegistry:
    """
    Registry for versioned parsers.

    Parsers should accept raw payloads and return normalized data.
    """

    def __init__(self) -> None:
        self._parsers: Dict[str, ParserFn] = {}

    def register(self, version: str, parser: ParserFn) -> None:
        if not version:
            raise ValueError("version is required")
        self._parsers[version] = parser

    def get(self, version: str) -> Optional[ParserFn]:
        return self._parsers.get(version)

    def latest(self) -> Optional[ParserFn]:
        if not self._parsers:
            return None
        # Simple heuristic: max lexicographic version.
        key = sorted(self._parsers.keys())[-1]
        return self._parsers[key]
