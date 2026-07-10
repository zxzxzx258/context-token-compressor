from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .factsheet import build_factsheet_sidecar
from .profiles import PROFILE_DEV, PROFILE_OFF, PROFILE_SAFE
from .tokens import estimate_tokens

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PATH_RE = re.compile(
    r"([A-Za-z]:\\|/home/|/data/|/var/|/etc/|/tmp/|\.py\b|\.ts\b|\.tsx\b|\.js\b|\.jsx\b|\.json\b|\.yaml\b|\.yml\b|\.md\b)"
)
ERROR_RE = re.compile(
    r"(error|failed|failure|exception|traceback|warning|warn|exit code|exit_code|assert|timeout|refused|denied|not found)",
    re.IGNORECASE,
)
DIFF_RE = re.compile(r"^(\+\+\+|---|@@|\+[^+]|-[^-])")
STAT_RE = re.compile(r"\b(total|passed|failed|errors?|warnings?|collected|duration|tokens?|chars?|bytes?|files?)\b", re.IGNORECASE)
COMMAND_RE = re.compile(
    r"\b(git|rg|grep|pytest|python|pip|npm|pnpm|yarn|tsc|eslint|mypy|ruff|cargo|go|docker|kubectl|make)\b",
    re.IGNORECASE,
)
FILE_HEADING_RE = re.compile(r"^(diff --git |@@ |[A-Za-z]:\\|/|\.{0,2}/|[A-Za-z0-9_.-]+\.(py|ts|tsx|js|jsx|json|yaml|yml|md|rs|go|java|cs|cpp|h)\b)")
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`]{1,160})`")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
WHITESPACE_RE = re.compile(r"\s+")

TEXT_FIELDS = ("output", "content", "stdout", "stderr", "text")
RECENT_CONTEXT_KEEP = 6
DEV_TEXT_THRESHOLD_CHARS = 3000
DEV_MAX_CACHE_ENTRIES = 4096


class CompressionKind(StrEnum):
    TOOL = "tool"
    MESSAGE = "message"


@dataclass
class CompressedItemStat:
    item_index: int
    field_path: str
    tool_name: str | None
    original_chars: int
    compressed_chars: int
    original_tokens: int
    compressed_tokens: int
    saved_tokens: int
    output_hash: str


@dataclass
class SummaryCache:
    max_entries: int = DEV_MAX_CACHE_ENTRIES
    values: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        if key in self.values:
            self.values[key] = value
            return
        self.values[key] = value
        self.order.append(key)
        while len(self.order) > self.max_entries:
            old = self.order.pop(0)
            self.values.pop(old, None)


@dataclass
class CompressionResult:
    body: dict[str, Any]
    original_chars: int = 0
    compressed_chars: int = 0
    estimated_original_tokens: int = 0
    estimated_compressed_tokens: int = 0
    compressed_items: list[CompressedItemStat] = field(default_factory=list)
    passthrough_items_count: int = 0

    @property
    def estimated_saved_tokens(self) -> int:
        return max(0, self.estimated_original_tokens - self.estimated_compressed_tokens)

    @property
    def saved_ratio(self) -> float:
        if self.estimated_original_tokens <= 0:
            return 0.0
        return self.estimated_saved_tokens / self.estimated_original_tokens


def _attach_factsheet(summary: str, *, source_text: str, max_chars: int, sidecar_chars: int) -> str:
    sidecar = build_factsheet_sidecar(source_text, max_chars=sidecar_chars)
    if not sidecar:
        return summary
    if len(summary) + 1 + len(sidecar) <= max_chars:
        return summary + "\n" + sidecar
    trim_budget = max(0, max_chars - len(sidecar) - 1)
    if trim_budget <= 0:
        return sidecar[:max_chars]
    return summary[:trim_budget].rstrip() + "\n" + sidecar


def sanitize_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _line_score(line: str) -> int:
    stripped = line.strip()
    if not stripped:
        return 0
    score = 0
    if ERROR_RE.search(stripped):
        score += 7
    if PATH_RE.search(stripped):
        score += 4
    if DIFF_RE.search(stripped):
        score += 4
    if STAT_RE.search(stripped):
        score += 3
    if stripped.startswith(("FAIL ", "FAILED ", "ERROR ", "WARNING ")):
        score += 4
    if len(stripped) > 200:
        score -= 1
    return score


def _dedupe_lines(lines: list[str]) -> tuple[list[str], int]:
    seen: dict[str, int] = {}
    result: list[str] = []
    dropped = 0
    for line in lines:
        key = line.strip()
        if key:
            count = seen.get(key, 0)
            seen[key] = count + 1
            if count >= 2:
                dropped += 1
                continue
        result.append(line)
    return result, dropped


def _line_keep_score(line: str) -> int:
    score = _line_score(line)
    stripped = line.strip()
    if COMMAND_RE.search(stripped):
        score += 4
    if FILE_HEADING_RE.search(stripped):
        score += 4
    if stripped.startswith((">", "$", "PS ", "PS>", ">>>")):
        score += 2
    if stripped.startswith(("diff --git ", "@@ ")):
        score += 8
    return score


def _head_tail_key_lines(
    lines: list[str],
    *,
    target_chars: int,
    header_budget: int = 700,
    head_min: int = 600,
    tail_min: int = 900,
) -> tuple[list[str], list[str], list[str], int]:
    head_budget = max(head_min, target_chars // 5)
    tail_budget = max(tail_min, target_chars // 4)
    middle_budget = max(500, target_chars - head_budget - tail_budget - header_budget)

    head: list[str] = []
    head_chars = 0
    for line in lines:
        if head_chars + len(line) + 1 > head_budget:
            break
        head.append(line)
        head_chars += len(line) + 1

    tail: list[str] = []
    tail_chars = 0
    for line in reversed(lines):
        if tail_chars + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        tail_chars += len(line) + 1
    tail.reverse()

    head_ids = set(range(len(head)))
    tail_start = max(0, len(lines) - len(tail))
    candidates: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        if idx in head_ids or idx >= tail_start:
            continue
        score = _line_keep_score(line)
        if score > 0:
            candidates.append((score, idx, line))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    selected: list[tuple[int, str]] = []
    selected_chars = 0
    for _score, idx, line in candidates:
        if selected_chars + len(line) + 1 > middle_budget:
            continue
        selected.append((idx, line))
        selected_chars += len(line) + 1
        if selected_chars >= middle_budget:
            break
    selected.sort(key=lambda item: item[0])
    key_lines = [line for _idx, line in selected]
    omitted_lines = max(0, len(lines) - len(head) - len(tail) - len(key_lines))
    return head, key_lines, tail, omitted_lines


def compress_text(text: str, *, model: str | None, target_chars: int) -> str:
    cleaned = sanitize_text(text)
    if len(cleaned) <= target_chars:
        return cleaned

    lines, dropped = _dedupe_lines(cleaned.split("\n"))
    head, key_lines, tail, omitted_lines = _head_tail_key_lines(lines, target_chars=target_chars, header_budget=900)
    header = (
        "CTC compressed tool output: "
        f"original_chars={len(text)}, cleaned_chars={len(cleaned)}, "
        f"estimated_original_tokens={estimate_tokens(cleaned, model)}, "
        f"deduplicated_repeated_lines={dropped}, omitted_lines={omitted_lines}, "
        "raw transcript remains with the calling client and is not stored by CTC.\n"
    )
    parts = [header, "[head]\n", "\n".join(head)]
    if key_lines:
        parts.extend(["\n\n[key lines]\n", "\n".join(key_lines)])
    parts.extend(["\n\n[tail]\n", "\n".join(tail)])
    compressed = "".join(parts)
    compressed = _attach_factsheet(
        compressed,
        source_text=cleaned,
        max_chars=target_chars,
        sidecar_chars=min(320, max(160, target_chars // 5)),
    )
    if len(compressed) > target_chars:
        compressed = compressed[: max(0, target_chars - 120)] + "\n[CTC truncated to target limit]\n"
    return compressed


def compress_dev_tool_output(
    text: str,
    *,
    model: str | None,
    target_chars: int,
    cache: SummaryCache | None = None,
) -> str:
    cleaned = sanitize_text(text)
    if len(cleaned) <= max(1200, target_chars // 2):
        return cleaned

    output_hash = stable_hash(cleaned)
    if cache is not None:
        cached = cache.get(output_hash)
        if cached is not None:
            return cached

    lines, dropped = _dedupe_lines(cleaned.split("\n"))
    dev_target = max(1600, min(target_chars, target_chars // 2))
    head, key_lines, tail, omitted_lines = _head_tail_key_lines(
        lines,
        target_chars=dev_target,
        header_budget=700,
        head_min=450,
        tail_min=650,
    )
    command_like = sum(1 for line in lines[:80] if COMMAND_RE.search(line) or FILE_HEADING_RE.search(line))
    header = (
        "CTC dev RTK-style tool summary: "
        f"hash={output_hash[:12]}, original_chars={len(text)}, cleaned_chars={len(cleaned)}, "
        f"estimated_original_tokens={estimate_tokens(cleaned, model)}, command_like_lines={command_like}, "
        f"deduplicated_repeated_lines={dropped}, omitted_lines={omitted_lines}. "
        "Preserved errors, paths, diffs, stats, head and tail; raw transcript is not stored by CTC.\n"
    )
    parts = [header]
    if head:
        parts.extend(["[head]\n", "\n".join(head)])
    if key_lines:
        parts.extend(["\n\n[rtk key lines]\n", "\n".join(key_lines)])
    if tail:
        parts.extend(["\n\n[tail]\n", "\n".join(tail)])
    compressed = "".join(parts)
    compressed = _attach_factsheet(
        compressed,
        source_text=cleaned,
        max_chars=dev_target,
        sidecar_chars=min(360, max(180, dev_target // 5)),
    )
    if len(compressed) > dev_target:
        compressed = compressed[: max(0, dev_target - 120)] + "\n[CTC dev summary truncated]\n"
    if cache is not None:
        cache.set(output_hash, compressed)
    return compressed


def _extract_content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else None
    return None


def _caveman_line(line: str) -> str:
    line = MARKDOWN_LINK_RE.sub(r"\1 \2", line)
    line = INLINE_CODE_RE.sub(r"\1", line)
    line = WHITESPACE_RE.sub(" ", line).strip()
    replacements = {
        "please": "",
        "could you": "",
        "would you": "",
        "I think": "",
        "I believe": "",
        "we need to": "need",
        "you should": "should",
        "make sure": "ensure",
        "because": "bc",
        "without": "w/o",
        "with": "w/",
        "approximately": "approx",
        "configuration": "config",
        "implementation": "impl",
        "information": "info",
    }
    lowered = line.lower()
    for old, new in replacements.items():
        if old in lowered:
            line = re.sub(re.escape(old), new, line, flags=re.IGNORECASE)
            lowered = line.lower()
    return WHITESPACE_RE.sub(" ", line).strip(" -")


def compress_dev_message_text(text: str, *, role: str, model: str | None, target_chars: int) -> str:
    cleaned = sanitize_text(text)
    if len(cleaned) <= DEV_TEXT_THRESHOLD_CHARS:
        return cleaned
    protected_blocks: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected_blocks.append(match.group(0))
        return f" CTC_CODE_BLOCK_{len(protected_blocks) - 1} "

    without_code = CODE_FENCE_RE.sub(protect, cleaned)
    lines = [line.strip() for line in without_code.splitlines() if line.strip()]
    selected: list[str] = []
    seen: set[str] = set()
    for line in lines:
        score = _line_keep_score(line)
        if score <= 0 and len(selected) >= 18:
            continue
        if len(line) > 220 and score <= 0:
            line = line[:220].rstrip() + "..."
        compact = _caveman_line(line)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        selected.append(compact)
        if len("\n".join(selected)) > target_chars - 700:
            break
    code_refs = []
    for idx, block in enumerate(protected_blocks[:3]):
        first = block.splitlines()[0] if block.splitlines() else "```"
        code_refs.append(f"code_block_{idx}: preserved marker {first[:80]}")
    omitted = max(0, len(lines) - len(selected))
    header = (
        f"CTC dev Caveman-style {role} summary: "
        f"original_chars={len(text)}, estimated_original_tokens={estimate_tokens(cleaned, model)}, "
        f"omitted_lines={omitted}. Preserved paths, errors, constraints, code block markers.\n"
    )
    body = "\n".join(f"- {line}" for line in selected)
    if code_refs:
        body += "\n" + "\n".join(f"- {line}" for line in code_refs)
    compressed = header + body
    compressed = _attach_factsheet(
        compressed,
        source_text=cleaned,
        max_chars=target_chars,
        sidecar_chars=min(320, max(180, target_chars // 5)),
    )
    if len(compressed) > target_chars:
        compressed = compressed[: max(0, target_chars - 120)] + "\n[CTC dev message summary truncated]\n"
    return compressed


def _maybe_json_loads(value: str) -> Any | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _compress_json_text_fields(
    value: Any,
    *,
    model: str | None,
    threshold_chars: int,
    target_chars: int,
    profile: str = PROFILE_SAFE,
    cache: SummaryCache | None = None,
    path: str = "$",
) -> tuple[Any, list[tuple[str, str, str]]]:
    replacements: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        updated: dict[str, Any] = {}
        for key, item in value.items():
            item_path = f"{path}.{key}"
            if key in TEXT_FIELDS and isinstance(item, str) and len(item) > threshold_chars:
                if profile == PROFILE_DEV:
                    compressed = compress_dev_tool_output(item, model=model, target_chars=target_chars, cache=cache)
                else:
                    compressed = compress_text(item, model=model, target_chars=target_chars)
                updated[key] = compressed
                replacements.append((item_path, item, compressed))
            else:
                updated_item, nested = _compress_json_text_fields(
                    item,
                    model=model,
                    threshold_chars=threshold_chars,
                    target_chars=target_chars,
                    profile=profile,
                    cache=cache,
                    path=item_path,
                )
                updated[key] = updated_item
                replacements.extend(nested)
        return updated, replacements
    if isinstance(value, list):
        updated_list = []
        for idx, item in enumerate(value):
            updated_item, nested = _compress_json_text_fields(
                item,
                model=model,
                threshold_chars=threshold_chars,
                target_chars=target_chars,
                profile=profile,
                cache=cache,
                path=f"{path}[{idx}]",
            )
            updated_list.append(updated_item)
            replacements.extend(nested)
        return updated_list, replacements
    return value, replacements


def _compress_output_value(
    value: Any,
    *,
    model: str | None,
    threshold_chars: int,
    target_chars: int,
    profile: str,
    cache: SummaryCache | None,
) -> tuple[Any, list[tuple[str, str, str]]]:
    if isinstance(value, str):
        parsed = _maybe_json_loads(value)
        if parsed is not None:
            updated, replacements = _compress_json_text_fields(
                parsed,
                model=model,
                threshold_chars=threshold_chars,
                target_chars=target_chars,
                profile=profile,
                cache=cache,
            )
            if replacements:
                return json.dumps(updated, ensure_ascii=False, separators=(",", ":")), replacements
        if len(value) > threshold_chars:
            if profile == PROFILE_DEV:
                compressed = compress_dev_tool_output(value, model=model, target_chars=target_chars, cache=cache)
            else:
                compressed = compress_text(value, model=model, target_chars=target_chars)
            return compressed, [("$.output", value, compressed)]
    elif isinstance(value, (dict, list)):
        updated, replacements = _compress_json_text_fields(
            value,
            model=model,
            threshold_chars=threshold_chars,
            target_chars=target_chars,
            profile=profile,
            cache=cache,
        )
        return updated, replacements
    return value, []


def _should_dev_compress_message(item: dict[str, Any], idx: int, total: int) -> bool:
    role = item.get("role")
    if role not in {"user", "assistant"}:
        return False
    if idx >= max(0, total - RECENT_CONTEXT_KEEP):
        return False
    content = _extract_content_text(item.get("content"))
    return bool(content and len(content) > DEV_TEXT_THRESHOLD_CHARS)


def _content_has_image(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url"}:
            return True
        return any(_content_has_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_content_has_image(item) for item in value)
    return False


def _is_vision_message(item: dict[str, Any]) -> bool:
    return _content_has_image(item.get("content"))


def _compress_content_text_parts(
    content: Any,
    *,
    role: str,
    model: str | None,
    target_chars: int,
) -> tuple[Any, list[tuple[str, str, str]]]:
    if isinstance(content, str):
        compressed = compress_dev_message_text(
            content,
            role=role,
            model=model,
            target_chars=target_chars,
        )
        if compressed == content:
            return content, []
        return compressed, [("$.content", content, compressed)]

    if not isinstance(content, list):
        return content, []

    updated: list[Any] = []
    replacements: list[tuple[str, str, str]] = []
    for idx, part in enumerate(content):
        if not isinstance(part, dict):
            updated.append(part)
            continue
        part_type = part.get("type")
        text_key = "text" if part_type in {"input_text", "text"} else None
        if text_key and isinstance(part.get(text_key), str):
            original = part[text_key]
            compressed = compress_dev_message_text(
                original,
                role=role,
                model=model,
                target_chars=target_chars,
            )
            if compressed != original:
                updated_part = dict(part)
                updated_part[text_key] = compressed
                updated.append(updated_part)
                replacements.append((f"$.content[{idx}].{text_key}", original, compressed))
                continue
        updated.append(part)
    return updated, replacements


def compress_responses_body(
    body: dict[str, Any],
    *,
    threshold_chars: int,
    target_chars: int,
    profile: str = PROFILE_SAFE,
    cache: SummaryCache | None = None,
) -> CompressionResult:
    if profile == PROFILE_OFF:
        return CompressionResult(body=body)
    model = body.get("model") if isinstance(body.get("model"), str) else None
    input_items = body.get("input")
    if not isinstance(input_items, list):
        return CompressionResult(body=body)

    updated_body = dict(body)
    updated_input: list[Any] = []
    result = CompressionResult(body=updated_body)
    has_vision_input = any(isinstance(item, dict) and _is_vision_message(item) for item in input_items)

    for idx, item in enumerate(input_items):
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            if profile == PROFILE_DEV and isinstance(item, dict):
                role = item.get("role") if isinstance(item.get("role"), str) else "message"
                should_compress = _should_dev_compress_message(item, idx, len(input_items))
                if not should_compress and has_vision_input and role in {"user", "assistant"} and not _is_vision_message(item):
                    original_text = _extract_content_text(item.get("content"))
                    should_compress = bool(original_text and len(original_text) > DEV_TEXT_THRESHOLD_CHARS)
                if should_compress:
                    updated_content, replacements = _compress_content_text_parts(
                        item.get("content"),
                        role=role,
                        model=model,
                        target_chars=max(1800, target_chars // 2),
                    )
                    if replacements:
                        updated_item = dict(item)
                        updated_item["content"] = updated_content
                        updated_input.append(updated_item)
                        for field_path, original_text, compressed_text in replacements:
                            original_tokens = estimate_tokens(original_text, model)
                            compressed_tokens = estimate_tokens(compressed_text, model)
                            stat = CompressedItemStat(
                                item_index=idx,
                                field_path=field_path,
                                tool_name=f"dev_{role}",
                                original_chars=len(original_text),
                                compressed_chars=len(compressed_text),
                                original_tokens=original_tokens,
                                compressed_tokens=compressed_tokens,
                                saved_tokens=max(0, original_tokens - compressed_tokens),
                                output_hash=stable_hash(original_text),
                            )
                            result.compressed_items.append(stat)
                            result.original_chars += stat.original_chars
                            result.compressed_chars += stat.compressed_chars
                            result.estimated_original_tokens += stat.original_tokens
                            result.estimated_compressed_tokens += stat.compressed_tokens
                        continue
            updated_input.append(item)
            result.passthrough_items_count += 1
            continue

        original_output = item.get("output")
        updated_output, replacements = _compress_output_value(
            original_output,
            model=model,
            threshold_chars=threshold_chars,
            target_chars=target_chars,
            profile=profile,
            cache=cache,
        )
        if not replacements:
            updated_input.append(item)
            result.passthrough_items_count += 1
            continue

        updated_item = dict(item)
        updated_item["output"] = updated_output
        updated_input.append(updated_item)

        tool_name = item.get("name") if isinstance(item.get("name"), str) else None
        for field_path, original_text, compressed_text in replacements:
            original_tokens = estimate_tokens(original_text, model)
            compressed_tokens = estimate_tokens(compressed_text, model)
            stat = CompressedItemStat(
                item_index=idx,
                field_path=field_path,
                tool_name=tool_name,
                original_chars=len(original_text),
                compressed_chars=len(compressed_text),
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                saved_tokens=max(0, original_tokens - compressed_tokens),
                output_hash=stable_hash(original_text),
            )
            result.compressed_items.append(stat)
            result.original_chars += stat.original_chars
            result.compressed_chars += stat.compressed_chars
            result.estimated_original_tokens += stat.original_tokens
            result.estimated_compressed_tokens += stat.compressed_tokens

    updated_body["input"] = updated_input
    return result

def compress_chat_body(
    body: dict[str, Any],
    *,
    threshold_chars: int,
    target_chars: int,
    profile: str = PROFILE_SAFE,
    cache: SummaryCache | None = None,
) -> CompressionResult:
    """Compress Chat Completions format body (``messages[]``).

    ``safe``: compress only ``role="tool"`` message content with ``compress_text()``.
    ``dev``:          compress tool messages with ``compress_dev_tool_output()``,
                     compress long user/assistant messages with ``compress_dev_message_text()``,
                     skipping the most recent ``RECENT_CONTEXT_KEEP`` messages.
    ``off``:          no compression.
    """
    if profile == PROFILE_OFF:
        return CompressionResult(body=body)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return CompressionResult(body=body)

    model = body.get("model") if isinstance(body.get("model"), str) else None
    updated_messages: list[dict[str, Any]] = []
    result = CompressionResult(body=dict(body))
    total = len(messages)

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            updated_messages.append(msg)
            result.passthrough_items_count += 1
            continue

        role = msg.get("role", "")
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= threshold_chars:
            updated_messages.append(dict(msg))
            result.passthrough_items_count += 1
            continue

        if role == "tool":
            # Tool output compression — two strategies
            if profile == PROFILE_DEV:
                compressed = compress_dev_tool_output(content, model=model, target_chars=target_chars, cache=cache)
            else:
                compressed = compress_text(content, model=model, target_chars=target_chars)
            if compressed != content:
                updated = dict(msg)
                updated["content"] = compressed
                updated_messages.append(updated)
                orig_tok = estimate_tokens(content, model)
                comp_tok = estimate_tokens(compressed, model)
                result.compressed_items.append(CompressedItemStat(
                    item_index=idx,
                    field_path=f"messages[{idx}].content",
                    tool_name="tool",
                    original_chars=len(content),
                    compressed_chars=len(compressed),
                    original_tokens=orig_tok,
                    compressed_tokens=comp_tok,
                    saved_tokens=max(0, orig_tok - comp_tok),
                    output_hash=stable_hash(content),
                ))
                result.original_chars += len(content)
                result.compressed_chars += len(compressed)
                result.estimated_original_tokens += orig_tok
                result.estimated_compressed_tokens += comp_tok
            else:
                updated_messages.append(dict(msg))
                result.passthrough_items_count += 1

        elif profile == PROFILE_DEV and role in ("user", "assistant"):
            # Dev mode also compresses long user/assistant text (Caveman style),
            # but protects the most recent RECENT_CONTEXT_KEEP messages.
            if idx >= max(0, total - RECENT_CONTEXT_KEEP):
                updated_messages.append(dict(msg))
                result.passthrough_items_count += 1
                continue
            compressed = compress_dev_message_text(
                content, role=role, model=model, target_chars=target_chars,
            )
            if compressed != content:
                updated = dict(msg)
                updated["content"] = compressed
                updated_messages.append(updated)
                orig_tok = estimate_tokens(content, model)
                comp_tok = estimate_tokens(compressed, model)
                result.compressed_items.append(CompressedItemStat(
                    item_index=idx,
                    field_path=f"messages[{idx}].content",
                    tool_name=role,
                    original_chars=len(content),
                    compressed_chars=len(compressed),
                    original_tokens=orig_tok,
                    compressed_tokens=comp_tok,
                    saved_tokens=max(0, orig_tok - comp_tok),
                    output_hash=stable_hash(content),
                ))
                result.original_chars += len(content)
                result.compressed_chars += len(compressed)
                result.estimated_original_tokens += orig_tok
                result.estimated_compressed_tokens += comp_tok
            else:
                updated_messages.append(dict(msg))
                result.passthrough_items_count += 1

        else:
            updated_messages.append(dict(msg))
            result.passthrough_items_count += 1

    result.body["messages"] = updated_messages
    return result
