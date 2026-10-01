from __future__ import annotations

import asyncio
import codecs
import json
import logging
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .compressor import CompressionResult, SummaryCache, compress_chat_body, compress_responses_body
from .config import Settings
from .deepseek_bridge import (
    ResponseStateCache,
    chat_completions_to_response,
    chat_message_from_response,
    response_to_sse,
    responses_input_has_matching_function_calls,
    responses_input_has_tool_outputs,
    responses_to_chat_completions,
)
from .metering import build_counterfactual_metering, usage_snapshot_from_body, usage_snapshot_from_sse_payload
from .profiles import normalize_profile, resolve_rule_fallback
from .providers import (
    PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE,
    ProviderConfig,
    ProviderStore,
)
from .security import token_matches
from .storage import CtcStore, RequestStat, utc_now_iso

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# Headers that describe the CTC<->client relationship and must never leak to
# the upstream provider: cookies scoped to CTC, client topology via proxy
# forwarding chains, and CTC's own profile selection header.
STRIPPED_CLIENT_HEADERS = HOP_BY_HOP_HEADERS | {
    "authorization",
    "cookie",
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "x-ctc-profile",
}

DEEPSEEK_BRIDGE_MODELS = ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro")
UPSTREAM_RETRY_DELAY_SECONDS = 10
UPSTREAM_RETRY_STATUS_CODES = {502, 503}
LOGGER = logging.getLogger("uvicorn.error")


def _forward_headers(
    request: Request,
    body: bytes | None = None,
    provider: ProviderConfig | None = None,
    forwarded_user_agent: str = "",
    proxy_token: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    consumed = getattr(request.state, "ctc_proxy_auth_consumed", False)
    inbound_authorization = ""
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in STRIPPED_CLIENT_HEADERS:
            if lower == "authorization" and not consumed:
                inbound_authorization = value
            continue
        headers[key] = value
    if inbound_authorization and proxy_token:
        # A client Authorization that carries the CTC proxy token is an access
        # credential for CTC itself, never for the upstream. Consume it even
        # on the loopback listener where no auth middleware is installed.
        scheme, _, value = inbound_authorization.partition(" ")
        candidate = value.strip() if scheme.lower() == "bearer" else inbound_authorization.strip()
        if token_matches(candidate, proxy_token):
            inbound_authorization = ""
    # CTC is a local proxy. Ask the upstream for identity encoding so the client
    # never receives compressed bytes with proxy-adjusted headers.
    headers["accept-encoding"] = "identity"
    if provider is not None:
        authorization = provider.authorization_header(inbound_authorization or "")
        if authorization:
            headers["authorization"] = authorization
    elif inbound_authorization:
        headers["authorization"] = inbound_authorization
    if forwarded_user_agent:
        headers["user-agent"] = forwarded_user_agent
    if provider is not None and provider.forwarded_user_agent:
        headers["user-agent"] = provider.forwarded_user_agent
    if body is not None:
        headers["content-length"] = str(len(body))
        headers["content-type"] = "application/json"
    return headers


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


async def _sleep_before_upstream_retry() -> None:
    await asyncio.sleep(UPSTREAM_RETRY_DELAY_SECONDS)


def _is_retryable_upstream_exception(exc: Exception) -> bool:
    # Only retry failures that happen before the request reaches the upstream:
    # connect-phase errors are guaranteed not to have been processed, while
    # read/protocol errors after delivery can mean the completion already ran
    # and a retry would bill it twice.
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def _safe_usage_snapshot(upstream_response: httpx.Response, *, source_hint: str) -> Any | None:
    """Extract a usage snapshot without exploding on non-JSON bodies.

    Upstream successes can legitimately be empty (204), binary (file
    downloads) or plain text; none of those may turn into fake 502s.
    """
    content_type = upstream_response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None
    try:
        body = upstream_response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return usage_snapshot_from_body(body, source_hint=source_hint)


def _safe_record(**kwargs: Any) -> None:
    """Record request stats without letting storage failures break the response."""
    store = kwargs.pop("store", None)
    try:
        _record(store, **kwargs)
    except Exception:
        LOGGER.exception("ctc_stats_failure request_id=%s path=%s", kwargs.get("request_id"), kwargs.get("path"))


def _normalize_proxy_path(path: str) -> str | None:
    """Reject dot segments so `/v1/../admin` cannot escape the /v1 scoping."""
    segments = path.split("/")
    if any(segment in {"..", "."} for segment in segments):
        return None
    return f"/v1/{path}"


def _is_retryable_upstream_response(response: httpx.Response) -> bool:
    return response.status_code in UPSTREAM_RETRY_STATUS_CODES


async def _request_with_upstream_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    content: bytes,
    request_id: str,
    path: str,
    provider: ProviderConfig,
) -> httpx.Response:
    try:
        response = await client.request(method, url, headers=headers, content=content)
    except Exception as exc:
        if not _is_retryable_upstream_exception(exc):
            raise
        LOGGER.warning(
            "ctc_upstream_retry request_id=%s path=%s provider_id=%s reason=%s delay_seconds=%s",
            request_id,
            path,
            provider.id,
            f"{type(exc).__name__}: {exc}",
            UPSTREAM_RETRY_DELAY_SECONDS,
        )
        await _sleep_before_upstream_retry()
        return await client.request(method, url, headers=headers, content=content)

    if not _is_retryable_upstream_response(response):
        return response

    LOGGER.warning(
        "ctc_upstream_retry request_id=%s path=%s provider_id=%s status_code=%s delay_seconds=%s",
        request_id,
        path,
        provider.id,
        response.status_code,
        UPSTREAM_RETRY_DELAY_SECONDS,
    )
    await response.aclose()
    await _sleep_before_upstream_retry()
    return await client.request(method, url, headers=headers, content=content)


def _deepseek_model_list(provider: ProviderConfig) -> dict[str, Any]:
    model_ids = []
    if provider.model:
        model_ids.append(provider.model)
    for model_id in DEEPSEEK_BRIDGE_MODELS:
        if model_id not in model_ids:
            model_ids.append(model_id)
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "deepseek",
                "ctc_bridge_model": provider.model,
            }
            for model_id in model_ids
        ],
    }


def _deepseek_model_detail(provider: ProviderConfig, requested_model: str) -> dict[str, Any]:
    return {
        "id": requested_model or provider.model or "deepseek-v4-flash",
        "object": "model",
        "created": 0,
        "owned_by": "deepseek",
        "ctc_bridge_model": provider.model,
    }


def _is_streaming_request(body: dict[str, Any]) -> bool:
    return bool(body.get("stream"))


def _collect_output_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    output = body.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def _ensure_output_text(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("object") != "response" and "output" not in body:
        return body
    if isinstance(body.get("output_text"), str) and body.get("output_text"):
        return body
    output_text = _collect_output_text(body)
    if not output_text:
        return body
    return {**body, "output_text": output_text}


def _normalized_json_response(upstream_response: httpx.Response, *, status_code: int) -> JSONResponse | None:
    content_type = upstream_response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None
    try:
        body = upstream_response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    normalized = _ensure_output_text(body)
    if normalized is body:
        return None
    return JSONResponse(
        normalized,
        status_code=status_code,
        headers=_response_headers(upstream_response.headers),
    )


def _model_name(body: dict[str, Any]) -> str:
    model = body.get("model")
    return model if isinstance(model, str) else ""


async def _read_body_limited(request: Request, max_body_bytes: int) -> bytes:
    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            if int(content_length) > max_body_bytes:
                raise HTTPException(status_code=413, detail="request body exceeds CTC_MAX_BODY_BYTES")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length header") from None

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise HTTPException(status_code=413, detail="request body exceeds CTC_MAX_BODY_BYTES")
    return bytes(body)


def _decode_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail=f"{label} body must be valid UTF-8 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=f"{label} body must be a JSON object")
    return body


def _client_host(request: Request, settings: Settings) -> str:
    peer_host = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for and peer_host in settings.trusted_proxy_hosts:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    return peer_host


def _source_label(client_host: str) -> str:
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return "本机客户端"
    return f"LAN {client_host}" if client_host else "未知"


def _record(
    store: CtcStore,
    *,
    request_id: str,
    started: float,
    model: str,
    path: str,
    stream: bool,
    result: CompressionResult | None,
    status_code: int,
    source: str,
    client_host: str,
    profile: str,
    provider: ProviderConfig | None = None,
    error: str | None = None,
    metering: Any | None = None,
) -> None:
    latency_ms = round((time.perf_counter() - started) * 1000)
    result = result or CompressionResult(body={})
    store.record_request(
        RequestStat(
            request_id=request_id,
            timestamp=utc_now_iso(),
            model=model,
            path=path,
            stream=stream,
            original_chars=result.original_chars,
            compressed_chars=result.compressed_chars,
            estimated_original_tokens=result.estimated_original_tokens,
            estimated_compressed_tokens=result.estimated_compressed_tokens,
            estimated_saved_tokens=result.estimated_saved_tokens,
            saved_ratio=result.saved_ratio,
            compressed_items_count=len(result.compressed_items),
            passthrough_items_count=result.passthrough_items_count,
            latency_ms=latency_ms,
            status_code=status_code,
            source=source,
            client_host=client_host,
            profile=profile,
            provider_id=provider.id if provider else "",
            provider_name=provider.name if provider else "",
            provider_type=provider.provider_type if provider else "",
            error=error[:500] if error else None,
            actual_input_tokens=metering.actual_input_tokens if metering else None,
            actual_output_tokens=metering.actual_output_tokens if metering else None,
            actual_total_tokens=metering.actual_total_tokens if metering else None,
            cached_input_tokens=metering.cached_input_tokens if metering else None,
            actual_uncached_input_tokens=metering.actual_uncached_input_tokens if metering else None,
            baseline_input_tokens=metering.baseline_input_tokens if metering else None,
            baseline_uncached_input_tokens=metering.baseline_uncached_input_tokens if metering else None,
            cache_aligned_saved_tokens=metering.cache_aligned_saved_tokens if metering else None,
            cache_aligned_saved_ratio=metering.cache_aligned_saved_ratio if metering else None,
            metering_source=metering.source if metering else "",
        ),
        result.compressed_items,
    )


class _StreamAudit:
    def __init__(
        self,
        *,
        request_id: str,
        path: str,
        model: str,
        status_code: int,
        source: str,
        client_host: str,
        profile: str,
        provider: ProviderConfig | None = None,
    ) -> None:
        self.request_id = request_id
        self.path = path
        self.model = model
        self.status_code = status_code
        self.source = source
        self.client_host = client_host
        self.profile = profile
        self.provider_id = provider.id if provider else ""
        self.provider_type = provider.provider_type if provider else ""
        self.byte_count = 0
        self.event_lines = 0
        self.data_lines = 0
        self.text_delta_chars = 0
        self.completed_events = 0
        self.done_markers = 0
        self.error_events = 0
        self.usage_snapshot = None
        self._current_event = ""
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: bytes) -> None:
        self.byte_count += len(chunk)
        # Incremental decoding keeps multi-byte UTF-8 sequences that straddle
        # chunk boundaries intact instead of dropping them.
        self._buffer += self._decoder.decode(chunk)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._process_line(line.rstrip("\r"))

    def finish(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        if self._buffer:
            self._process_line(self._buffer.rstrip("\r"))
            self._buffer = ""
        LOGGER.info(
            "ctc_stream_summary request_id=%s path=%s model=%s status_code=%s source=%s client_host=%s profile=%s provider_id=%s provider_type=%s bytes=%s event_lines=%s data_lines=%s text_delta_chars=%s completed_events=%s done_markers=%s error_events=%s",
            self.request_id,
            self.path,
            self.model,
            self.status_code,
            self.source,
            self.client_host,
            self.profile,
            self.provider_id,
            self.provider_type,
            self.byte_count,
            self.event_lines,
            self.data_lines,
            self.text_delta_chars,
            self.completed_events,
            self.done_markers,
            self.error_events,
        )

    def _process_line(self, line: str) -> None:
        if line.startswith("event:"):
            self.event_lines += 1
            self._current_event = line.split(":", 1)[1].strip()
            if self._current_event == "response.completed":
                self.completed_events += 1
            if "error" in self._current_event:
                self.error_events += 1
            return
        if not line.startswith("data:"):
            return
        self.data_lines += 1
        data = line.split(":", 1)[1].strip()
        if data == "[DONE]":
            self.done_markers += 1
            return
        try:
            payload = json.loads(data)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type") if isinstance(payload.get("type"), str) else self._current_event
        if event_type == "response.completed":
            self.completed_events += 1
        if event_type and "error" in event_type:
            self.error_events += 1
        if payload.get("error") is not None:
            self.error_events += 1
        usage_snapshot = usage_snapshot_from_sse_payload(payload, source_hint=event_type or self._current_event or "sse")
        if usage_snapshot is not None:
            self.usage_snapshot = usage_snapshot
        delta = payload.get("delta")
        if event_type == "response.output_text.delta" and isinstance(delta, str):
            self.text_delta_chars += len(delta)


def create_proxy_router(settings: Settings, store: CtcStore, provider_store: ProviderStore) -> APIRouter:
    router = APIRouter()
    summary_cache = SummaryCache()
    response_state_cache = ResponseStateCache()

    def choose_profile(request: Request, client_host: str) -> str:
        header_profile = request.headers.get("x-ctc-profile")
        host_default = store.default_profile_for_host(client_host)
        if settings.profile_rules.allow_header_override and header_profile:
            return normalize_profile(header_profile, host_default)
        fallback = resolve_rule_fallback(client_host, settings.profile_rules, host_default)
        return store.resolve_profile(client_host, fallback)

    timeout = httpx.Timeout(
        connect=30,
        read=settings.stream_timeout_seconds,
        write=settings.request_timeout_seconds,
        pool=30,
    )
    client = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=settings.trust_env_proxy,
    )

    @router.get("/v1/props")
    async def props_probe_proxy(request: Request) -> Response:
        # Some clients probe this endpoint to detect llama.cpp. It is metadata
        # discovery, not a model request, so CTC preserves upstream semantics
        # but keeps the probe out of compression statistics.
        provider = provider_store.provider_for_host(_client_host(request, settings))
        upstream_url = provider.upstream_url_for_path("/v1/props")
        if request.url.query:
            upstream_url = f"{upstream_url}?{request.url.query}"
        try:
            upstream_response = await client.get(
                upstream_url,
                headers=_forward_headers(
                    request,
                    provider=provider,
                    forwarded_user_agent=settings.forwarded_user_agent,
                    proxy_token=settings.proxy_token,
                ),
            )
        except httpx.HTTPError as exc:
            LOGGER.warning("ctc_props_probe_failed provider_id=%s error=%s", provider.id, f"{type(exc).__name__}: {exc}")
            return JSONResponse(
                {"error": {"message": "CTC upstream request failed", "type": "upstream_error"}},
                status_code=502,
            )
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response.headers),
            media_type=upstream_response.headers.get("content-type"),
        )

    @router.post("/v1/responses")
    async def responses_proxy(request: Request) -> Response:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        path = "/v1/responses"
        result: CompressionResult | None = None
        model = ""
        stream = False
        status_code = 502
        client_host = _client_host(request, settings)
        source = _source_label(client_host)
        profile = choose_profile(request, client_host)
        try:
            raw = await _read_body_limited(request, settings.max_body_bytes)
            body = _decode_json_object(raw, "Responses")
            model = _model_name(body)
            stream = _is_streaming_request(body)
            result = compress_responses_body(
                body,
                threshold_chars=settings.compress_threshold_chars,
                target_chars=settings.compress_target_chars,
                profile=profile,
                cache=summary_cache,
            )
            provider = provider_store.provider_for_host(client_host)
            if provider.model:
                result.body["model"] = provider.model
            if provider.provider_type == PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE:
                response_id = f"resp_ctc_{uuid.uuid4().hex}"
                previous_messages = response_state_cache.get(str(result.body.get("previous_response_id") or ""))
                input_has_matching_function_calls = responses_input_has_matching_function_calls(result.body.get("input"))
                if not previous_messages and not input_has_matching_function_calls:
                    previous_messages = response_state_cache.get_for_tool_outputs(result.body.get("input"))
                if (
                    not previous_messages
                    and responses_input_has_tool_outputs(result.body.get("input"))
                    and not input_has_matching_function_calls
                ):
                    fallback_provider = provider_store.first_openai_responses_provider()
                    if fallback_provider is not None:
                        provider = fallback_provider
                    else:
                        return JSONResponse(
                            {
                                "error": {
                                    "message": "CTC DeepSeek bridge is missing prior tool-call context; retry with an OpenAI Responses provider.",
                                    "type": "ctc_deepseek_context_missing",
                                }
                            },
                            status_code=409,
                        )
                if provider.provider_type != PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE:
                    outbound_body = json.dumps(result.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    upstream_url = provider.upstream_url_for_path(path)
                    headers = _forward_headers(
                        request,
                        outbound_body,
                        provider,
                        forwarded_user_agent=settings.forwarded_user_agent,
                        proxy_token=settings.proxy_token,
                    )
                    upstream_response = await _request_with_upstream_retry(
                        client,
                        "POST",
                        upstream_url,
                        headers=headers,
                        content=outbound_body,
                        request_id=request_id,
                        path=path,
                        provider=provider,
                    )
                    status_code = upstream_response.status_code
                    _safe_record(
                        store=store,
                        request_id=request_id,
                        started=started,
                        model=model,
                        path=path,
                        stream=stream,
                        result=result,
                        status_code=status_code,
                        source=source,
                        client_host=client_host,
                        profile=profile,
                        provider=provider,
                        metering=build_counterfactual_metering(
                            _safe_usage_snapshot(upstream_response, source_hint="responses_json"),
                            estimated_saved_input_tokens=result.estimated_saved_tokens,
                        ),
                    )
                    return Response(
                        content=upstream_response.content,
                        status_code=status_code,
                        headers=_response_headers(upstream_response.headers),
                        media_type=upstream_response.headers.get("content-type"),
                    )
                chat_request = responses_to_chat_completions(
                    result.body,
                    model=provider.model or model,
                    previous_messages=previous_messages,
                )
                if stream:
                    # Buffer bridged responses so Codex receives complete tool-call
                    # structures in the final Responses event.
                    chat_request.body["stream"] = False
                outbound_body = json.dumps(chat_request.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                upstream_url = provider.upstream_url_for_path("/v1/chat/completions")
                headers = _forward_headers(
                    request,
                    outbound_body,
                    provider,
                    forwarded_user_agent=settings.forwarded_user_agent,
                    proxy_token=settings.proxy_token,
                )
                upstream_response = await _request_with_upstream_retry(
                    client,
                    "POST",
                    upstream_url,
                    headers=headers,
                    content=outbound_body,
                    request_id=request_id,
                    path="/v1/chat/completions",
                    provider=provider,
                )
                status_code = upstream_response.status_code
                chat_body: dict[str, Any] | None = None
                if upstream_response.status_code < 400:
                    # The bridge can only translate JSON chat payloads; a
                    # non-JSON success is a genuine upstream malfunction and
                    # must surface as an explicit error, not a fake success.
                    content_type = upstream_response.headers.get("content-type", "")
                    if "json" in content_type.lower():
                        try:
                            parsed = upstream_response.json()
                        except ValueError:
                            parsed = None
                        if isinstance(parsed, dict):
                            chat_body = parsed
                    if chat_body is None:
                        _safe_record(
                            store=store,
                            request_id=request_id,
                            started=started,
                            model=model,
                            path=path,
                            stream=stream,
                            result=result,
                            status_code=502,
                            source=source,
                            client_host=client_host,
                            profile=profile,
                            provider=provider,
                            error=f"BridgeError: upstream returned non-JSON response (HTTP {status_code})",
                        )
                        return JSONResponse(
                            {
                                "error": {
                                    "message": "CTC bridge upstream returned a non-JSON response",
                                    "type": "upstream_error",
                                }
                            },
                            status_code=502,
                        )
                    response_body = chat_completions_to_response(
                        chat_body,
                        model=chat_request.body["model"],
                        response_id=response_id,
                    )
                    assistant_message = chat_message_from_response(chat_body)
                    if assistant_message is not None:
                        response_state_cache.set(response_body["id"], [*chat_request.source_messages, assistant_message])
                    _safe_record(
                        store=store,
                        request_id=request_id,
                        started=started,
                        model=model,
                        path=path,
                        stream=stream,
                        result=result,
                        status_code=status_code,
                        source=source,
                        client_host=client_host,
                        profile=profile,
                        provider=provider,
                        metering=build_counterfactual_metering(
                            usage_snapshot_from_body(response_body, source_hint="deepseek_bridge_response"),
                            estimated_saved_input_tokens=result.estimated_saved_tokens,
                        ),
                    )
                    if stream:
                        stream_body = response_to_sse(response_body)
                        audit = _StreamAudit(
                            request_id=request_id,
                            path=path,
                            model=model,
                            status_code=status_code,
                            source=source,
                            client_host=client_host,
                            profile=profile,
                            provider=provider,
                        )
                        audit.feed(stream_body)
                        audit.finish()
                        return Response(
                            content=stream_body,
                            status_code=status_code,
                            media_type="text/event-stream",
                        )
                    return JSONResponse(response_body, status_code=status_code)
                _safe_record(
                    store=store,
                    request_id=request_id,
                    started=started,
                    model=model,
                    path=path,
                    stream=stream,
                    result=result,
                    status_code=status_code,
                    source=source,
                    client_host=client_host,
                    profile=profile,
                    provider=provider,
                )
                normalized_response = _normalized_json_response(upstream_response, status_code=status_code)
                if normalized_response is not None:
                    return normalized_response
                return Response(
                    content=upstream_response.content,
                    status_code=status_code,
                    headers=_response_headers(upstream_response.headers),
                    media_type=upstream_response.headers.get("content-type"),
                )

            outbound_body = json.dumps(result.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            upstream_url = provider.upstream_url_for_path(path)
            headers = _forward_headers(
                request,
                outbound_body,
                provider,
                forwarded_user_agent=settings.forwarded_user_agent,
                proxy_token=settings.proxy_token,
            )

            if stream:
                upstream_cm = client.stream("POST", upstream_url, headers=headers, content=outbound_body)
                upstream_response = await upstream_cm.__aenter__()
                status_code = upstream_response.status_code
                audit = _StreamAudit(
                    request_id=request_id,
                    path=path,
                    model=model,
                    status_code=status_code,
                    source=source,
                    client_host=client_host,
                    profile=profile,
                    provider=provider,
                )

                async def iter_stream():
                    nonlocal upstream_cm
                    try:
                        async for chunk in upstream_response.aiter_bytes():
                            audit.feed(chunk)
                            yield chunk
                    finally:
                        await upstream_cm.__aexit__(None, None, None)
                        audit.finish()
                        _safe_record(
                            store=store,
                            request_id=request_id,
                            started=started,
                            model=model,
                            path=path,
                            stream=stream,
                            result=result,
                            status_code=status_code,
                            source=source,
                            client_host=client_host,
                            profile=profile,
                            provider=provider,
                            metering=build_counterfactual_metering(
                                audit.usage_snapshot,
                                estimated_saved_input_tokens=result.estimated_saved_tokens,
                            ),
                        )

                return StreamingResponse(
                    iter_stream(),
                    status_code=status_code,
                    headers=_response_headers(upstream_response.headers),
                    media_type=upstream_response.headers.get("content-type"),
                )

            upstream_response = await _request_with_upstream_retry(
                client,
                "POST",
                upstream_url,
                headers=headers,
                content=outbound_body,
                request_id=request_id,
                path=path,
                provider=provider,
            )
            status_code = upstream_response.status_code
            _safe_record(
                store=store,
                request_id=request_id,
                started=started,
                model=model,
                path=path,
                stream=stream,
                result=result,
                status_code=status_code,
                source=source,
                client_host=client_host,
                profile=profile,
                provider=provider,
                metering=build_counterfactual_metering(
                    _safe_usage_snapshot(upstream_response, source_hint="responses_json"),
                    estimated_saved_input_tokens=result.estimated_saved_tokens,
                ),
            )
            normalized_response = _normalized_json_response(upstream_response, status_code=status_code)
            if normalized_response is not None:
                return normalized_response
            return Response(
                content=upstream_response.content,
                status_code=status_code,
                headers=_response_headers(upstream_response.headers),
                media_type=upstream_response.headers.get("content-type"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            _safe_record(
                store=store,
                request_id=request_id,
                started=started,
                model=model,
                path=path,
                stream=stream,
                result=result,
                status_code=status_code,
                source=source,
                client_host=client_host,
                profile=profile,
                provider=provider if "provider" in locals() else None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return JSONResponse(
                {"error": {"message": "CTC upstream request failed", "type": "upstream_error"}},
                status_code=502,
            )

    @router.post("/v1/chat/completions")
    async def chat_completions_proxy(request: Request) -> Response:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        path = "/v1/chat/completions"
        result: CompressionResult | None = None
        model = ""
        stream = False
        status_code = 502
        client_host = _client_host(request, settings)
        source = _source_label(client_host)
        profile = choose_profile(request, client_host)
        try:
            raw = await _read_body_limited(request, settings.max_body_bytes)
            body = _decode_json_object(raw, "Chat")
            model = _model_name(body)
            stream = _is_streaming_request(body)
            result = compress_chat_body(
                body,
                threshold_chars=settings.compress_threshold_chars,
                target_chars=settings.compress_target_chars,
                profile=profile,
                cache=summary_cache,
            )
            provider = provider_store.provider_for_host(client_host)
            if provider.model:
                result.body["model"] = provider.model
            outbound_body = json.dumps(result.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            upstream_url = provider.upstream_url_for_path(path)
            headers = _forward_headers(
                request,
                outbound_body,
                provider,
                forwarded_user_agent=settings.forwarded_user_agent,
                proxy_token=settings.proxy_token,
            )
            if stream:
                # Streaming chat completions must be relayed as a live SSE
                # stream; buffering them here used to surface as an HTTP 200
                # carrying an error body after the upstream had succeeded.
                upstream_cm = client.stream("POST", upstream_url, headers=headers, content=outbound_body)
                upstream_response = await upstream_cm.__aenter__()
                status_code = upstream_response.status_code
                audit = _StreamAudit(
                    request_id=request_id,
                    path=path,
                    model=model,
                    status_code=status_code,
                    source=source,
                    client_host=client_host,
                    profile=profile,
                    provider=provider,
                )

                async def iter_chat_stream():
                    try:
                        async for chunk in upstream_response.aiter_bytes():
                            audit.feed(chunk)
                            yield chunk
                    finally:
                        await upstream_cm.__aexit__(None, None, None)
                        audit.finish()
                        _safe_record(
                            store=store,
                            request_id=request_id,
                            started=started,
                            model=model,
                            path=path,
                            stream=True,
                            result=result,
                            status_code=status_code,
                            source=source,
                            client_host=client_host,
                            profile=profile,
                            provider=provider,
                            metering=build_counterfactual_metering(
                                audit.usage_snapshot,
                                estimated_saved_input_tokens=result.estimated_saved_tokens if result else 0,
                            ),
                        )

                return StreamingResponse(
                    iter_chat_stream(),
                    status_code=status_code,
                    headers=_response_headers(upstream_response.headers),
                    media_type=upstream_response.headers.get("content-type"),
                )

            upstream_response = await _request_with_upstream_retry(
                client,
                "POST",
                upstream_url,
                headers=headers,
                content=outbound_body,
                request_id=request_id,
                path=path,
                provider=provider,
            )
            status_code = upstream_response.status_code
            _safe_record(
                store=store,
                request_id=request_id,
                started=started,
                model=model,
                path=path,
                stream=False,
                result=result,
                status_code=status_code,
                source=source,
                client_host=client_host,
                profile=profile,
                provider=provider,
                metering=build_counterfactual_metering(
                    _safe_usage_snapshot(upstream_response, source_hint="chat_json"),
                    estimated_saved_input_tokens=result.estimated_saved_tokens if result else 0,
                ),
            )
            normalized_resp = _normalized_json_response(upstream_response, status_code=status_code)
            if normalized_resp is not None:
                return normalized_resp
            return Response(
                content=upstream_response.content,
                status_code=status_code,
                headers=_response_headers(upstream_response.headers),
                media_type=upstream_response.headers.get("content-type"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            _safe_record(
                store=store,
                request_id=request_id,
                started=started,
                model=model,
                path=path,
                stream=stream,
                result=result,
                status_code=status_code,
                source=source,
                client_host=client_host,
                profile=profile,
                provider=provider if "provider" in locals() else None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return JSONResponse(
                {"error": {"message": "CTC upstream request failed", "type": "upstream_error"}},
                status_code=502,
            )

    @router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def transparent_v1_proxy(path: str, request: Request) -> Response:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        full_path = _normalize_proxy_path(path)
        status_code = 502
        stream = False
        client_host = _client_host(request, settings)
        source = _source_label(client_host)
        profile = choose_profile(request, client_host)
        if full_path is None:
            # Dot segments could escape the /v1 scoping and reach arbitrary
            # paths on the upstream origin.
            return JSONResponse(
                {"error": {"message": "invalid request path", "type": "invalid_request_error"}},
                status_code=404,
            )
        try:
            raw = await _read_body_limited(request, settings.max_body_bytes)
            provider = provider_store.provider_for_host(client_host)
            if (
                request.method == "GET"
                and provider.provider_type == PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE
                and (full_path == "/v1/models" or full_path.startswith("/v1/models/"))
            ):
                requested_model = full_path.removeprefix("/v1/models/").strip("/") if full_path != "/v1/models" else ""
                status_code = 200
                _safe_record(
                    store=store,
                    request_id=request_id,
                    started=started,
                    model="",
                    path=full_path,
                    stream=stream,
                    result=CompressionResult(body={}),
                    status_code=status_code,
                    source=source,
                    client_host=client_host,
                    profile=profile,
                )
                payload = (
                    _deepseek_model_list(provider)
                    if full_path == "/v1/models"
                    else _deepseek_model_detail(provider, requested_model)
                )
                return JSONResponse(payload, status_code=status_code)
            upstream_url = provider.upstream_url_for_path(full_path)
            if request.url.query:
                upstream_url = f"{upstream_url}?{request.url.query}"
            upstream_response = await _request_with_upstream_retry(
                client,
                request.method,
                upstream_url,
                headers=_forward_headers(
                    request,
                    provider=provider,
                    forwarded_user_agent=settings.forwarded_user_agent,
                    proxy_token=settings.proxy_token,
                ),
                content=raw,
                request_id=request_id,
                path=full_path,
                provider=provider,
            )
            status_code = upstream_response.status_code
            _safe_record(
                store=store,
                request_id=request_id,
                started=started,
                model="",
                path=full_path,
                stream=stream,
                result=CompressionResult(body={}),
                status_code=status_code,
                source=source,
                client_host=client_host,
                profile=profile,
                provider=provider,
                metering=build_counterfactual_metering(
                    _safe_usage_snapshot(upstream_response, source_hint="transparent_json"),
                    estimated_saved_input_tokens=0,
                ),
            )
            return Response(
                content=upstream_response.content,
                status_code=status_code,
                headers=_response_headers(upstream_response.headers),
                media_type=upstream_response.headers.get("content-type"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            _safe_record(
                store=store,
                request_id=request_id,
                started=started,
                model="",
                path=full_path,
                stream=stream,
                result=CompressionResult(body={}),
                status_code=status_code,
                source=source,
                client_host=client_host,
                profile=profile,
                provider=provider if "provider" in locals() else None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return JSONResponse(
                {"error": {"message": "CTC upstream request failed", "type": "upstream_error"}},
                status_code=502,
            )


    return router
