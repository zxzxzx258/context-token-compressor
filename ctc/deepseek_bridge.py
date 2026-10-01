from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

Message = dict[str, Any]


@dataclass(frozen=True)
class ChatBridgeRequest:
    body: dict[str, Any]
    source_messages: list[Message]


class ResponseStateCache:
    def __init__(self, max_entries: int = 64, ttl_seconds: int = 3600):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, list[Message]]] = OrderedDict()
        self._tool_items: OrderedDict[str, tuple[float, list[Message]]] = OrderedDict()

    def get(self, response_id: str | None) -> list[Message]:
        if not response_id:
            return []
        now = time.time()
        item = self._items.get(response_id)
        if item is None:
            return []
        created_at, messages = item
        if now - created_at > self.ttl_seconds:
            self._items.pop(response_id, None)
            return []
        self._items.move_to_end(response_id)
        return _copy_messages(messages)

    def get_for_tool_outputs(self, value: Any) -> list[Message]:
        call_ids = _tool_output_call_ids(value)
        if not call_ids:
            return []
        now = time.time()
        for call_id in call_ids:
            item = self._tool_items.get(call_id)
            if item is None:
                continue
            created_at, messages = item
            if now - created_at > self.ttl_seconds:
                self._tool_items.pop(call_id, None)
                continue
            self._tool_items.move_to_end(call_id)
            return _copy_messages(messages)
        return []

    def set(self, response_id: str, messages: list[Message]) -> None:
        if not response_id:
            return
        created_at = time.time()
        stored_messages = _copy_messages(messages)
        self._items[response_id] = (created_at, stored_messages)
        self._items.move_to_end(response_id)
        for call_id in _tool_call_ids_from_messages(stored_messages):
            self._tool_items[call_id] = (created_at, stored_messages)
            self._tool_items.move_to_end(call_id)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)
        while len(self._tool_items) > self.max_entries * 4:
            self._tool_items.popitem(last=False)


def responses_to_chat_completions(body: dict[str, Any], *, model: str, previous_messages: list[Message] | None = None) -> ChatBridgeRequest:
    messages: list[Message] = _copy_messages(previous_messages or [])
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        # Continuation turns already carry the system message from the cached
        # history. Replace it in place instead of appending, otherwise the
        # system prompt lands after assistant tool_calls and DeepSeek rejects
        # the tool message that follows it.
        system_index = next(
            (index for index, message in enumerate(messages) if message.get("role") == "system"),
            None,
        )
        system_message: Message = {"role": "system", "content": instructions}
        if system_index is None:
            messages.insert(0, system_message)
        else:
            messages[system_index] = system_message
    thinking_enabled = _thinking_enabled(body)
    known_tool_call_ids = _tool_call_ids_from_messages(messages)
    known_tool_output_ids = {
        str(message.get("tool_call_id"))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id")
    }
    messages.extend(
        _input_to_messages(
            body.get("input"),
            skip_function_call_ids=known_tool_call_ids,
            skip_tool_output_ids=known_tool_output_ids,
            ensure_reasoning_for_tool_calls=thinking_enabled,
        )
    )

    chat_body: dict[str, Any] = {
        "model": model or str(body.get("model") or ""),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    _copy_optional(body, chat_body, "thinking")
    reasoning_effort = _reasoning_effort(body) if thinking_enabled else ""
    if reasoning_effort:
        chat_body["reasoning_effort"] = reasoning_effort
    if not thinking_enabled:
        _copy_optional(body, chat_body, "temperature")
        _copy_optional(body, chat_body, "top_p")
        _copy_optional(body, chat_body, "presence_penalty")
        _copy_optional(body, chat_body, "frequency_penalty")
    _copy_optional(body, chat_body, "max_output_tokens", target_key="max_tokens")
    _copy_optional(body, chat_body, "max_tokens")
    _copy_optional(body, chat_body, "stop")

    tools = body.get("tools")
    chat_tools: list[dict[str, Any]] = []
    if isinstance(tools, list) and tools:
        chat_tools = [tool for tool in (_response_tool_to_chat_tool(tool) for tool in tools if isinstance(tool, dict)) if tool]
    if thinking_enabled and chat_tools:
        # DeepSeek requires the reasoning_content of every prior assistant
        # turn (tool turns and plain turns alike) once tools are present;
        # missing entries make the API return 400 mid-session.
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "assistant" and "reasoning_content" not in message:
                message["reasoning_content"] = ""
    if chat_tools:
        chat_body["tools"] = chat_tools
        tool_choice = body.get("tool_choice")
        if tool_choice is not None:
            converted_choice = _tool_choice_to_chat(tool_choice)
            if converted_choice is not None:
                chat_body["tool_choice"] = converted_choice

    return ChatBridgeRequest(body=chat_body, source_messages=messages)


def chat_completions_to_response(body: dict[str, Any], *, model: str, response_id: str | None = None) -> dict[str, Any]:
    choice = _first_choice(body)
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}

    # A response cut off by max_tokens must not be reported as fully
    # completed, otherwise clients execute truncated tool-call arguments.
    finish_reason = str(choice.get("finish_reason") or "").strip().lower() if isinstance(choice, dict) else ""
    status = "completed"
    incomplete_details: dict[str, Any] | None = None
    if finish_reason == "length":
        status = "incomplete"
        incomplete_details = {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        status = "incomplete"
        incomplete_details = {"reason": "content_filter"}

    output: list[dict[str, Any]] = []
    content = _message_content_text(message.get("content"))
    reasoning = _message_content_text(message.get("reasoning_content"))
    if content or reasoning:
        output_text: dict[str, Any] = {"type": "output_text", "text": content}
        if reasoning:
            output_text["annotations"] = [{"type": "reasoning_summary", "text": reasoning}]
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": status,
                "role": "assistant",
                "content": [output_text],
            }
        )

    for tool_call in _tool_calls_from_message(message):
        call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex}"
        output.append(
            {
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": tool_call.get("name") or "",
                "arguments": tool_call.get("arguments") or "{}",
                "status": status,
            }
        )

    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    response: dict[str, Any] = {
        "id": response_id or str(body.get("id") or f"resp_ctc_{uuid.uuid4().hex}"),
        "object": "response",
        "created_at": int(time.time()),
        "model": str(body.get("model") or model),
        "output": output,
        "output_text": content,
        "parallel_tool_calls": True,
        "status": status,
    }
    if incomplete_details is not None:
        response["incomplete_details"] = incomplete_details
    if usage is not None:
        response["usage"] = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    return response


def chat_message_from_response(response: dict[str, Any]) -> Message | None:
    choice = _first_choice(response)
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        return _sanitize_chat_message(message)
    return None


def stream_created_event(*, model: str, response_id: str) -> bytes:
    return _sse_event(
        "response.created",
        {
            "type": "response.created",
            "response": _stream_completed_response(model, response_id, status="in_progress"),
            # OpenAI numbers SSE events from 0 on response.created.
            "sequence_number": 0,
        },
    )


def stream_done_event(*, model: str, response_id: str) -> bytes:
    return _sse_event(
        "response.completed",
        {"type": "response.completed", "response": _stream_completed_response(model, response_id)},
    ) + b"data: [DONE]\n\n"


def response_to_sse(response: dict[str, Any]) -> bytes:
    response_id = str(response.get("id") or f"resp_ctc_{uuid.uuid4().hex}")
    model = str(response.get("model") or "")
    sequence_number = 0

    def next_sequence() -> int:
        nonlocal sequence_number
        sequence_number += 1
        return sequence_number

    out = [
        stream_created_event(model=model, response_id=response_id),
        _sse_event(
            "response.in_progress",
            {
                "type": "response.in_progress",
                "response": _stream_completed_response(model, response_id, status="in_progress"),
                "sequence_number": next_sequence(),
            },
        ),
    ]
    for item_index, item in enumerate(response.get("output") if isinstance(response.get("output"), list) else []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("call_id") or f"item_{uuid.uuid4().hex}")
        item = {**item, "id": item_id}
        added_item = item
        if item.get("type") == "message" and isinstance(item.get("content"), list):
            # Per the Responses stream contract, output_item.added for a
            # message carries empty text; content arrives via deltas.
            added_item = {
                **item,
                "content": [{**part, "text": ""} if isinstance(part, dict) and part.get("type") == "output_text" else part for part in item["content"]],
            }
        out.append(
            _sse_event(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": item_index,
                    "item": added_item,
                    "sequence_number": next_sequence(),
                },
            )
        )
        if item.get("type") != "message":
            out.append(
                _sse_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": item_index,
                        "item": item,
                        "sequence_number": next_sequence(),
                    },
                )
            )
            continue
        content = item.get("content")
        if not isinstance(content, list):
            out.append(
                _sse_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": item_index,
                        "item": item,
                        "sequence_number": next_sequence(),
                    },
                )
            )
            continue
        for content_index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = str(part.get("text") or "")
            out.append(
                _sse_event(
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": item_id,
                        "output_index": item_index,
                        "content_index": content_index,
                        "part": {**part, "text": ""},
                        "sequence_number": next_sequence(),
                    },
                )
            )
            if text:
                out.append(
                    _sse_event(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": item_id,
                            "output_index": item_index,
                            "content_index": content_index,
                            "delta": text,
                            "sequence_number": next_sequence(),
                        },
                    )
                )
            out.append(
                _sse_event(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": item_id,
                        "output_index": item_index,
                        "content_index": content_index,
                        "text": text,
                        "sequence_number": next_sequence(),
                    },
                )
            )
            out.append(
                _sse_event(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": item_index,
                        "content_index": content_index,
                        "part": part,
                        "sequence_number": next_sequence(),
                    },
                )
            )
        out.append(
            _sse_event(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": item_index,
                    "item": item,
                    "sequence_number": next_sequence(),
                },
            )
        )
    out.append(_sse_event("response.completed", {"type": "response.completed", "response": response}))
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


def responses_input_has_tool_outputs(value: Any) -> bool:
    return bool(_tool_output_call_ids(value))


def responses_input_has_matching_function_calls(value: Any) -> bool:
    tool_output_ids = _tool_output_call_ids(value)
    if not tool_output_ids:
        return False
    return bool(tool_output_ids & _function_call_ids_from_input(value))


def _input_to_messages(
    value: Any,
    *,
    skip_function_call_ids: set[str] | None = None,
    skip_tool_output_ids: set[str] | None = None,
    ensure_reasoning_for_tool_calls: bool = False,
) -> list[Message]:
    skip_function_call_ids = skip_function_call_ids or set()
    skip_tool_output_ids = skip_tool_output_ids or set()
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, dict):
        message = _input_item_to_message(value, ensure_reasoning_for_tool_calls=ensure_reasoning_for_tool_calls)
        return [message] if message else []
    if not isinstance(value, list):
        return []
    messages: list[Message] = []
    pending_tool_calls: list[dict[str, Any]] = []
    pending_tool_call_ids: set[str] = set()
    pending_tool_outputs: list[Message] = []
    pending_assistant_message: Message | None = None

    def flush_pending_assistant() -> None:
        nonlocal pending_assistant_message
        if pending_assistant_message is not None:
            messages.append(pending_assistant_message)
            pending_assistant_message = None

    def flush_pending_tools() -> None:
        nonlocal pending_tool_calls, pending_tool_call_ids, pending_tool_outputs, pending_assistant_message
        if not pending_tool_calls:
            return
        assistant_message: Message = {"role": "assistant", "content": "", "tool_calls": pending_tool_calls}
        if pending_assistant_message is not None and pending_assistant_message.get("role") == "assistant":
            assistant_message["content"] = str(pending_assistant_message.get("content") or "")
            if "reasoning_content" in pending_assistant_message:
                assistant_message["reasoning_content"] = pending_assistant_message["reasoning_content"]
            pending_assistant_message = None
        if ensure_reasoning_for_tool_calls and "reasoning_content" not in assistant_message:
            assistant_message["reasoning_content"] = ""
        messages.append(assistant_message)
        messages.extend(pending_tool_outputs)
        pending_tool_calls = []
        pending_tool_call_ids = set()
        pending_tool_outputs = []

    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id and call_id in skip_function_call_ids:
                continue
            pending_tool_calls.append(_function_call_to_tool_call(item))
            if call_id:
                pending_tool_call_ids.add(call_id)
            continue
        if item.get("type") == "function_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id and call_id in skip_tool_output_ids:
                # The cached history already carries this tool output; the
                # client is replaying it. Appending would duplicate the tool
                # message and break the continuation turn.
                continue
            message = _input_item_to_message(item, ensure_reasoning_for_tool_calls=ensure_reasoning_for_tool_calls)
            if call_id and call_id in pending_tool_call_ids and message:
                pending_tool_outputs.append(message)
                continue
            flush_pending_tools()
            flush_pending_assistant()
            if message:
                messages.append(message)
            continue
        flush_pending_tools()
        flush_pending_assistant()
        message = _input_item_to_message(item, ensure_reasoning_for_tool_calls=ensure_reasoning_for_tool_calls)
        if message:
            if message.get("role") == "assistant":
                pending_assistant_message = message
            else:
                messages.append(message)
    flush_pending_tools()
    flush_pending_assistant()
    return messages


def _input_item_to_message(item: dict[str, Any], *, ensure_reasoning_for_tool_calls: bool = False) -> Message | None:
    item_type = item.get("type")
    if item_type == "function_call_output":
        return {
            "role": "tool",
            "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
            "content": _content_to_text(item.get("output")),
        }
    role = item.get("role")
    if role in {"system", "user", "assistant", "tool"}:
        content = _content_parts_for_role(role, item.get("content"))
        message: Message = {"role": role, "content": content}
        if role == "assistant":
            reasoning_content = _reasoning_content_from_content(item.get("content"))
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                message["tool_calls"] = json.loads(json.dumps(tool_calls, ensure_ascii=False))
                if ensure_reasoning_for_tool_calls and "reasoning_content" not in message:
                    message["reasoning_content"] = _message_content_text(item.get("reasoning_content"))
        if role == "tool" and item.get("tool_call_id"):
            message["tool_call_id"] = str(item.get("tool_call_id"))
        return message
    if item_type in {"input_image", "image_url"}:
        return {"role": "user", "content": [_image_part_to_chat(item)]}
    if item_type in {"input_file", "file"}:
        return {"role": "user", "content": [_file_part_to_chat(item)]}
    if item_type == "message":
        role = item.get("role") if item.get("role") in {"system", "user", "assistant"} else "user"
        message: Message = {"role": role, "content": _content_parts_for_role(role, item.get("content"))}
        if role == "assistant":
            reasoning_content = _reasoning_content_from_content(item.get("content"))
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                message["tool_calls"] = json.loads(json.dumps(tool_calls, ensure_ascii=False))
                if ensure_reasoning_for_tool_calls and "reasoning_content" not in message:
                    message["reasoning_content"] = _message_content_text(item.get("reasoning_content"))
        return message
    return None


def _image_part_text(part: dict[str, Any]) -> str:
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    url = str(url or "")
    if url.startswith("data:"):
        return f"[image: inline data, {len(url)} chars]"
    return f"[image: {url}]" if url else "[image]"


def _file_part_text(part: dict[str, Any]) -> str:
    filename = part.get("filename")
    if not isinstance(filename, str) or not filename:
        file_id = part.get("file_id")
        filename = str(file_id) if file_id else "unknown"
    return f"[file: {filename}]"


def _image_part_to_chat(item: dict[str, Any]) -> dict[str, Any]:
    # Responses carries images as {"type": "input_image", "image_url": "<url>"}
    # (plain string); Chat Completions wraps the url in an object.
    url = item.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and url:
        image_url: dict[str, Any] = {"url": url}
        detail = item.get("detail")
        if isinstance(detail, str) and detail:
            image_url["detail"] = detail
        return {"type": "image_url", "image_url": image_url}
    file_id = item.get("file_id")
    if isinstance(file_id, str) and file_id:
        return {"type": "file", "file_id": file_id}
    return {"type": "text", "text": _image_part_text(item)}


def _file_part_to_chat(item: dict[str, Any]) -> dict[str, Any]:
    file_part: dict[str, Any] = {"type": "file"}
    if isinstance(item.get("file_id"), str) and item["file_id"]:
        file_part["file_id"] = item["file_id"]
    if isinstance(item.get("file_data"), str) and item["file_data"]:
        file_part["file_data"] = item["file_data"]
    if isinstance(item.get("filename"), str) and item["filename"]:
        file_part["filename"] = item["filename"]
    if len(file_part) > 1:
        return file_part
    return {"type": "text", "text": _file_part_text(item)}


def _content_parts_for_role(role: str, value: Any) -> Any:
    """Convert Responses content into Chat content for the given role.

    User messages keep image/file parts in Chat Completions form — DeepSeek
    vision accepts them in user messages only. Every other role degrades
    non-text parts to text placeholders because DeepSeek rejects images in
    system/assistant/tool messages.
    """
    if role != "user":
        return _content_to_text(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _content_to_text(value)
    if not isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    parts: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            parts.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        part_type = item.get("type")
        if part_type in {"input_text", "text"} and isinstance(item.get("text"), str):
            parts.append({"type": "text", "text": item["text"]})
        elif part_type in {"input_image", "image_url"}:
            parts.append(_image_part_to_chat(item))
        elif part_type in {"input_file", "file"}:
            parts.append(_file_part_to_chat(item))
        else:
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
    if parts and all(part.get("type") == "text" for part in parts):
        return "\n".join(part["text"] for part in parts if part.get("text"))
    return parts


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                part_type = item.get("type")
                if part_type in {"input_image", "image_url"}:
                    parts.append(_image_part_text(item))
                elif part_type in {"input_file", "file"}:
                    parts.append(_file_part_text(item))
                else:
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        # Tolerant clients send structured payloads (e.g. output objects); a
        # Python repr would not be parseable as JSON by the model.
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _reasoning_content_from_content(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        annotations = item.get("annotations")
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            if not isinstance(annotation, dict) or annotation.get("type") != "reasoning_summary":
                continue
            text = _message_content_text(annotation.get("text"))
            if text:
                parts.append(text)
    return "\n".join(parts)


def _message_content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _content_to_text(value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _copy_messages(messages: list[Message]) -> list[Message]:
    return json.loads(json.dumps(messages, ensure_ascii=False))


def _tool_call_ids_from_messages(messages: list[Message]) -> set[str]:
    call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or "")
            if call_id:
                call_ids.add(call_id)
    return call_ids


def _tool_output_call_ids(value: Any) -> set[str]:
    items = value if isinstance(value, list) else [value]
    call_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id:
                call_ids.add(call_id)
        elif item.get("role") == "tool":
            call_id = str(item.get("tool_call_id") or item.get("call_id") or "")
            if call_id:
                call_ids.add(call_id)
    return call_ids


def _function_call_ids_from_input(value: Any) -> set[str]:
    items = value if isinstance(value, list) else [value]
    call_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id:
                call_ids.add(call_id)
    return call_ids


def _sanitize_chat_message(message: dict[str, Any]) -> Message:
    role = message.get("role") if message.get("role") in {"system", "user", "assistant", "tool"} else "assistant"
    clean: Message = {"role": role, "content": _message_content_text(message.get("content"))}
    if "reasoning_content" in message:
        clean["reasoning_content"] = _message_content_text(message.get("reasoning_content"))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        clean["tool_calls"] = json.loads(json.dumps(tool_calls, ensure_ascii=False))
    if role == "tool" and message.get("tool_call_id"):
        clean["tool_call_id"] = str(message.get("tool_call_id"))
    return clean


def _function_call_to_tool_call(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
        "type": "function",
        "function": {
            "name": str(item.get("name") or ""),
            "arguments": str(item.get("arguments") or "{}"),
        },
    }


def _response_tool_to_chat_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    if tool.get("type") != "function":
        return None
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": parameters,
        },
    }


def _tool_choice_to_chat(value: Any) -> Any | None:
    # Responses object form is {"type": "function", "name": ...}; Chat
    # Completions nests the name under "function".
    if isinstance(value, dict) and value.get("type") == "function":
        name = value.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
        return None
    if isinstance(value, str) and value.strip().lower() in {"auto", "none", "required"}:
        return value.strip().lower()
    return value


def _copy_optional(source: dict[str, Any], target: dict[str, Any], key: str, *, target_key: str | None = None) -> None:
    if key in source:
        target[target_key or key] = source[key]


def _thinking_enabled(body: dict[str, Any]) -> bool:
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").lower() == "disabled":
        return False
    return True


# DeepSeek documents these effort mappings for thinking mode:
# minimal→low, medium→high, xhigh→high, ultra→max.
DEEPSEEK_REASONING_EFFORTS = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}


def _reasoning_effort(body: dict[str, Any]) -> str:
    effort = body.get("reasoning_effort")
    reasoning = body.get("reasoning")
    if not isinstance(effort, str) and isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not isinstance(effort, str):
        return ""
    return DEEPSEEK_REASONING_EFFORTS.get(effort.strip().lower(), "")


def _first_choice(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def _tool_calls_from_message(message: dict[str, Any]) -> list[dict[str, str]]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[dict[str, str]] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        calls.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or "{}"),
            }
        )
    return calls


def _stream_completed_response(model: str, response_id: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": [],
        "status": status,
    }


def _sse_event(event: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode()
