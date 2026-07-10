from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    source: str = ""


@dataclass(frozen=True)
class CounterfactualMetering:
    actual_input_tokens: int
    actual_output_tokens: int
    actual_total_tokens: int
    cached_input_tokens: int
    actual_uncached_input_tokens: int
    baseline_input_tokens: int
    baseline_uncached_input_tokens: int
    cache_aligned_saved_tokens: int
    cache_aligned_saved_ratio: float
    source: str = ""


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _usage_dict_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str] | tuple[None, str]:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage, "usage"
    response = payload.get("response")
    if isinstance(response, dict):
        nested = response.get("usage")
        if isinstance(nested, dict):
            return nested, "response.usage"
    return None, ""


def usage_snapshot_from_body(payload: Any, *, source_hint: str = "") -> UsageSnapshot | None:
    if not isinstance(payload, dict):
        return None
    usage, source = _usage_dict_from_payload(payload)
    if usage is None:
        return None
    return usage_snapshot_from_usage_dict(usage, source_hint=source_hint or source)


def usage_snapshot_from_sse_payload(payload: Any, *, source_hint: str = "") -> UsageSnapshot | None:
    if not isinstance(payload, dict):
        return None
    usage, source = _usage_dict_from_payload(payload)
    if usage is None:
        return None
    event_type = payload.get("type") if isinstance(payload.get("type"), str) else ""
    return usage_snapshot_from_usage_dict(usage, source_hint=source_hint or event_type or source)


def usage_snapshot_from_usage_dict(usage: dict[str, Any], *, source_hint: str = "") -> UsageSnapshot | None:
    input_tokens = _coerce_int(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _coerce_int(usage.get("prompt_tokens"))
    output_tokens = _coerce_int(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _coerce_int(usage.get("completion_tokens"))
    total_tokens = _coerce_int(usage.get("total_tokens"))

    input_tokens = max(0, input_tokens or 0)
    output_tokens = max(0, output_tokens or 0)
    total_tokens = max(input_tokens + output_tokens, total_tokens or 0)

    cached_input_tokens = _coerce_int(usage.get("cached_tokens"))
    if cached_input_tokens is None:
        input_details = usage.get("input_tokens_details")
        if isinstance(input_details, dict):
            cached_input_tokens = _coerce_int(input_details.get("cached_tokens"))
    if cached_input_tokens is None:
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_input_tokens = _coerce_int(prompt_details.get("cached_tokens"))
    if cached_input_tokens is None:
        cached_input_tokens = _coerce_int(usage.get("prompt_cache_hit_tokens"))
    cached_input_tokens = max(0, min(input_tokens, cached_input_tokens or 0))

    if input_tokens <= 0 and output_tokens <= 0 and total_tokens <= 0:
        return None
    return UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        source=source_hint,
    )


def build_counterfactual_metering(
    usage: UsageSnapshot | None,
    *,
    estimated_saved_input_tokens: int,
) -> CounterfactualMetering | None:
    if usage is None:
        return None
    saved = max(0, int(estimated_saved_input_tokens))
    actual_input_tokens = max(0, usage.input_tokens)
    cached_input_tokens = max(0, min(actual_input_tokens, usage.cached_input_tokens))
    baseline_input_tokens = max(actual_input_tokens, actual_input_tokens + saved)
    actual_uncached = max(0, actual_input_tokens - cached_input_tokens)
    baseline_uncached = max(actual_uncached, baseline_input_tokens - cached_input_tokens)
    cache_aligned_saved = max(0, baseline_uncached - actual_uncached)
    return CounterfactualMetering(
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=max(0, usage.output_tokens),
        actual_total_tokens=max(usage.total_tokens, actual_input_tokens + max(0, usage.output_tokens)),
        cached_input_tokens=cached_input_tokens,
        actual_uncached_input_tokens=actual_uncached,
        baseline_input_tokens=baseline_input_tokens,
        baseline_uncached_input_tokens=baseline_uncached,
        cache_aligned_saved_tokens=cache_aligned_saved,
        cache_aligned_saved_ratio=(cache_aligned_saved / baseline_uncached) if baseline_uncached > 0 else 0.0,
        source=usage.source,
    )
