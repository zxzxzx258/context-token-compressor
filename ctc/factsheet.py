from __future__ import annotations

import re

_TOKEN_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (0, re.compile(r"\bhttps?://[^\s)\"'<>]+")),
    (0, re.compile(r"\b[A-Za-z]:\\(?:[^\s\\/:*?\"<>|]+\\)*[^\s\\/:*?\"<>|]+")),
    (0, re.compile(r"(?<!\w)/(?:[\w.@:+-]+/)+[\w.@:+-]+")),
    (
        1,
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
    ),
    (
        1,
        re.compile(
            r"\b(?:request|response|session|trace|call|tool_call|provider|task|job|run|message|model|error|exit)_id[:=][A-Za-z0-9._:/-]{3,}\b",
            re.IGNORECASE,
        ),
    ),
    (1, re.compile(r"\b(?:req|resp|chatcmpl|cmpl|msg|call|tool|run|sess|trace|evt|event|job|task)_[A-Za-z0-9-]{3,}\b")),
    (2, re.compile(r"\bHTTP[ /-]?\d{3}\b", re.IGNORECASE)),
    (2, re.compile(r"\b[A-Za-z_][\w.]*?(?:Error|Exception)\b")),
    (2, re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+){1,}\b")),
    (2, re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+){1,}\b")),
    (3, re.compile(r"\b[0-9a-fA-F]{7,16}\b")),
    (4, re.compile(r"\bv?\d+\.\d+(?:\.\d+){0,3}(?:[-+][A-Za-z0-9.]+)?\b")),
    (
        5,
        re.compile(
            r"\b[\w.@+-]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml|md|txt|log|sql|sqlite3|ps1|sh|toml|ini|cfg|conf|service|html|css)\b"
        ),
    ),
)

_TRIM_CHARS = " \t\r\n,;:()[]{}<>\"'"
_FACTSHEET_PREFIX = "[CTC factsheet] exact tokens: "


def _normalize_token(token: str) -> str:
    normalized = token.strip(_TRIM_CHARS)
    return normalized.rstrip(".")


def extract_factsheet_tokens(text: str, *, max_items: int = 24) -> list[str]:
    if not text or max_items <= 0:
        return []

    candidates: list[tuple[int, int, int, str]] = []
    seen_spans: set[tuple[str, int]] = set()
    for priority, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            token = _normalize_token(match.group(0))
            if not token:
                continue
            if priority == 3 and not re.fullmatch(r"[0-9a-fA-F]{7,16}", token):
                continue
            span_key = (token, match.start())
            if span_key in seen_spans:
                continue
            seen_spans.add(span_key)
            candidates.append((priority, match.start(), -len(token), token))

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3].lower()))
    kept: list[str] = []
    seen_tokens: set[str] = set()
    for _, _, _, token in candidates:
        lowered = token.lower()
        if lowered in seen_tokens:
            continue
        if any(lowered != existing.lower() and lowered in existing.lower() for existing in kept):
            continue
        kept.append(token)
        seen_tokens.add(lowered)
        if len(kept) >= max_items:
            break
    return kept


def build_factsheet_sidecar(text: str, *, max_chars: int = 320, max_items: int = 24) -> str:
    tokens = extract_factsheet_tokens(text, max_items=max_items)
    if not tokens:
        return ""

    parts: list[str] = []
    length = len(_FACTSHEET_PREFIX)
    for token in tokens:
        piece = token if not parts else f" · {token}"
        if length + len(piece) > max_chars:
            break
        parts.append(token)
        length += len(piece)
    return _FACTSHEET_PREFIX + " · ".join(parts) if parts else ""
