from __future__ import annotations

from ctc.deepseek_bridge import (
    ResponseStateCache,
    chat_completions_to_response,
    chat_message_from_response,
    response_to_sse,
    responses_input_has_matching_function_calls,
    responses_to_chat_completions,
)


def test_responses_input_converts_to_chat_messages_and_tools():
    body = {
        "model": "gpt-5.5",
        "instructions": "system prompt",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "run tool"}]},
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{\"cmd\":\"date\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "done"},
        ],
        "tools": [{"type": "function", "name": "shell", "description": "run shell", "parameters": {"type": "object"}}],
        "max_output_tokens": 128,
    }

    converted = responses_to_chat_completions(body, model="deepseek-v4-flash")

    assert converted.body["model"] == "deepseek-v4-flash"
    assert converted.body["max_tokens"] == 128
    assert converted.body["messages"][0] == {"role": "system", "content": "system prompt"}
    assert converted.body["messages"][1] == {"role": "user", "content": "run tool"}
    assert converted.body["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert converted.body["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "done"}
    assert converted.body["tools"][0]["function"]["name"] == "shell"
    assert responses_input_has_matching_function_calls(body["input"]) is True


def test_multiple_tool_outputs_stay_adjacent_to_matching_tool_calls():
    body = {
        "model": "gpt-5.5",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "one"},
            {"type": "function_call", "call_id": "call_2", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_2", "output": "two"},
        ],
    }

    converted = responses_to_chat_completions(body, model="deepseek-v4-flash")
    messages = converted.body["messages"]

    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["id"] == "call_1"
    assert messages[0]["tool_calls"][1]["id"] == "call_2"
    assert messages[1] == {"role": "tool", "tool_call_id": "call_1", "content": "one"}
    assert messages[2] == {"role": "tool", "tool_call_id": "call_2", "content": "two"}


def test_chat_response_converts_to_responses_tool_call_and_sse():
    chat = {
        "id": "chatcmpl_1",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{\"cmd\":\"pwd\"}"},
                        }
                    ],
                }
            }
        ],
    }

    response = chat_completions_to_response(chat, model="deepseek-v4-flash", response_id="resp_1")
    sse = response_to_sse(response)

    assert response["id"] == "resp_1"
    assert response["output_text"] == "hello"
    assert response["output"][0]["content"][0]["text"] == "hello"
    assert response["output"][1]["type"] == "function_call"
    assert response["output"][1]["call_id"] == "call_2"
    assert b"response.in_progress" in sse
    assert b"response.output_item.added" in sse
    assert b"response.content_part.added" in sse
    assert b"response.completed" in sse
    assert b"item_id\":\"msg_" in sse
    assert b"item_id\":\"resp_1\"" not in sse
    assert b"data: [DONE]" in sse


def test_chat_message_cache_keeps_reasoning_content_for_followup():
    chat = {
        "id": "chatcmpl_reasoning",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private chain token",
                    "tool_calls": [
                        {
                            "id": "call_3",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                }
            }
        ],
    }

    message = chat_message_from_response(chat)
    converted = responses_to_chat_completions(
        {"model": "gpt-5.5", "input": [{"type": "function_call_output", "call_id": "call_3", "output": "done"}]},
        model="deepseek-v4-flash",
        previous_messages=[message],
    )

    assert converted.body["messages"][0]["reasoning_content"] == "private chain token"
    assert converted.body["messages"][0]["tool_calls"][0]["id"] == "call_3"
    assert converted.body["messages"][1] == {"role": "tool", "tool_call_id": "call_3", "content": "done"}


def test_responses_history_annotation_reasoning_is_replayed_with_tool_call():
    converted = responses_to_chat_completions(
        {
            "model": "gpt-5.5",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "run code"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "",
                            "annotations": [{"type": "reasoning_summary", "text": "must pass back"}],
                        }
                    ],
                },
                {"type": "function_call", "call_id": "call_calc", "name": "execute_code", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_calc", "output": "blocked"},
            ],
        },
        model="deepseek-v4-flash",
    )

    messages = converted.body["messages"]
    assert messages[1]["role"] == "assistant"
    assert messages[1]["reasoning_content"] == "must pass back"
    assert messages[1]["tool_calls"][0]["id"] == "call_calc"
    assert messages[2] == {"role": "tool", "tool_call_id": "call_calc", "content": "blocked"}


def test_chat_style_assistant_tool_call_gets_empty_reasoning_when_thinking_enabled():
    body = {
        "model": "gpt-5.5",
        "input": [
            {"role": "user", "content": "run tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ],
    }

    converted = responses_to_chat_completions(body, model="deepseek-v4-flash")

    assert converted.body["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert converted.body["messages"][1]["reasoning_content"] == ""


def test_responses_function_call_gets_empty_reasoning_when_thinking_enabled():
    converted = responses_to_chat_completions(
        {
            "model": "gpt-5.5",
            "input": [
                {"role": "user", "content": "run tool"},
                {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "done"},
            ],
        },
        model="deepseek-v4-flash",
    )

    assert converted.body["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert converted.body["messages"][1]["reasoning_content"] == ""


def test_thinking_mode_filters_sampling_params_and_maps_supported_effort():
    body = {
        "model": "gpt-5.5",
        "input": "hello",
        "thinking": {"type": "enabled"},
        "reasoning": {"effort": "xhigh"},
        "temperature": 0.7,
        "top_p": 0.8,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.2,
        "max_output_tokens": 64,
    }

    converted = responses_to_chat_completions(body, model="deepseek-v4-pro")

    assert converted.body["thinking"] == {"type": "enabled"}
    assert converted.body["reasoning_effort"] == "max"
    assert converted.body["max_tokens"] == 64
    assert "temperature" not in converted.body
    assert "top_p" not in converted.body
    assert "presence_penalty" not in converted.body
    assert "frequency_penalty" not in converted.body


def test_disabled_thinking_keeps_sampling_params_and_drops_unsupported_effort():
    body = {
        "model": "gpt-5.5",
        "input": "hello",
        "thinking": {"type": "disabled"},
        "reasoning_effort": "medium",
        "temperature": 0.7,
        "top_p": 0.8,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.2,
    }

    converted = responses_to_chat_completions(body, model="deepseek-v4-flash")

    assert converted.body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in converted.body
    assert converted.body["temperature"] == 0.7
    assert converted.body["top_p"] == 0.8
    assert converted.body["presence_penalty"] == 0.1
    assert converted.body["frequency_penalty"] == 0.2


def test_empty_reasoning_content_is_preserved_for_tool_followup():
    message = chat_message_from_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "",
                        "tool_calls": [
                            {
                                "id": "call_empty",
                                "type": "function",
                                "function": {"name": "noop", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert message is not None
    assert "reasoning_content" in message
    assert message["reasoning_content"] == ""


def test_response_state_cache_expires_and_copies_messages(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("ctc.deepseek_bridge.time.time", lambda: now[0])
    cache = ResponseStateCache(max_entries=2, ttl_seconds=10)

    messages = [{"role": "user", "content": "hello"}]
    cache.set("resp_1", messages)
    messages[0]["content"] = "changed"

    assert cache.get("resp_1") == [{"role": "user", "content": "hello"}]
    now[0] = 1011.0
    assert cache.get("resp_1") == []


def test_response_state_cache_finds_previous_messages_by_tool_call_id():
    cache = ResponseStateCache()
    messages = [
        {"role": "user", "content": "run tool"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "must replay",
            "tool_calls": [
                {
                    "id": "call_lookup",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        },
    ]

    cache.set("resp_lookup", messages)
    found = cache.get_for_tool_outputs([{"type": "function_call_output", "call_id": "call_lookup", "output": "done"}])

    assert found[1]["reasoning_content"] == "must replay"
