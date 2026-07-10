from __future__ import annotations

import json

from ctc.compressor import SummaryCache, compress_responses_body, compress_text


def test_compress_text_keeps_errors_paths_and_header():
    text = "\n".join(
        ["start"]
        + [f"noise {i % 3}" for i in range(900)]
        + [
            "/srv/example/project/file.py:10",
            "Traceback (most recent call last):",
            "Exception: failed hard",
            "summary total=900 failed=1 passed=899",
            "tail",
        ]
    )
    compressed = compress_text(text, model="gpt-5.5", target_chars=2500)
    assert "CTC compressed tool output" in compressed
    assert "Traceback" in compressed
    assert "/srv/example/project/file.py" in compressed
    assert len(compressed) < len(text)


def test_only_function_call_output_is_compressed():
    long_output = "\n".join(f"line {i}" for i in range(1500))
    body = {
        "model": "gpt-5.5",
        "input": [
            {"role": "system", "content": "do not touch"},
            {"role": "user", "content": long_output},
            {"type": "function_call", "call_id": "c1", "name": "tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": long_output},
        ],
    }
    result = compress_responses_body(body, threshold_chars=2000, target_chars=3000)
    assert result.body["input"][0]["content"] == "do not touch"
    assert result.body["input"][1]["content"] == long_output
    assert result.body["input"][2]["arguments"] == "{}"
    assert result.body["input"][3]["call_id"] == "c1"
    assert "CTC compressed tool output" in result.body["input"][3]["output"]
    assert len(result.compressed_items) == 1


def test_json_wrapped_output_compresses_text_field_and_keeps_json():
    long_stdout = "\n".join(f"stdout {i}" for i in range(1200))
    wrapped = json.dumps({"stdout": long_stdout, "exit_code": 1, "meta": {"keep": "yes"}}, ensure_ascii=False)
    body = {
        "model": "gpt-5.5",
        "input": [{"type": "function_call_output", "call_id": "c1", "output": wrapped}],
    }
    result = compress_responses_body(body, threshold_chars=2000, target_chars=2500)
    output = json.loads(result.body["input"][0]["output"])
    assert output["exit_code"] == 1
    assert output["meta"]["keep"] == "yes"
    assert "CTC compressed tool output" in output["stdout"]


def test_off_profile_does_not_compress():
    long_output = "\n".join(f"line {i}" for i in range(1500))
    body = {
        "model": "gpt-5.5",
        "input": [{"type": "function_call_output", "call_id": "c1", "output": long_output}],
    }
    result = compress_responses_body(body, threshold_chars=2000, target_chars=2500, profile="off")
    assert result.body is body
    assert result.compressed_items == []
    assert result.body["input"][0]["output"] == long_output


def test_dev_profile_uses_rtk_style_tool_summary_and_cache():
    repeated = "\n".join(["pytest tests", "FAILED tests/test_app.py::test_a", "Traceback error"] * 500)
    body = {
        "model": "gpt-5.5",
        "input": [{"type": "function_call_output", "call_id": "c1", "output": repeated}],
    }
    cache = SummaryCache()
    first = compress_responses_body(body, threshold_chars=1000, target_chars=3000, profile="dev", cache=cache)
    second = compress_responses_body(body, threshold_chars=1000, target_chars=3000, profile="dev", cache=cache)

    first_output = first.body["input"][0]["output"]
    second_output = second.body["input"][0]["output"]
    assert "CTC dev RTK-style tool summary" in first_output
    assert "FAILED tests/test_app.py::test_a" in first_output
    assert first_output == second_output
    assert len(cache.values) == 1


def test_dev_profile_compresses_old_user_and_assistant_but_keeps_recent_user():
    old_user = "Please keep this project goal and path C:/repo/app.py. " * 250
    old_assistant = "I changed tests and ran pytest successfully. " * 250
    current_user = "Current exact request must stay unchanged. " * 250
    body = {
        "model": "gpt-5.5",
        "input": [
            {"role": "user", "content": old_user},
            {"role": "assistant", "content": old_assistant},
            {"role": "user", "content": "short 1"},
            {"role": "assistant", "content": "short 2"},
            {"role": "user", "content": "short 3"},
            {"role": "assistant", "content": "short 4"},
            {"role": "user", "content": current_user},
            {"role": "assistant", "content": "current response"},
        ],
    }
    result = compress_responses_body(body, threshold_chars=1000, target_chars=5000, profile="dev")
    assert "CTC dev Caveman-style user summary" in result.body["input"][0]["content"]
    assert "CTC dev Caveman-style assistant summary" in result.body["input"][1]["content"]
    assert result.body["input"][-2]["content"] == current_user


def test_dev_profile_compresses_old_multimodal_context_but_keeps_recent_image():
    old_text = "Old admission-plan context C:/reports/gansu.png with repeated analysis. " * 260
    old_assistant = "Prior assistant OCR notes and partial table extraction should be compacted. " * 240
    recent_text = "Read the visible table in this image and extract the key enrollment numbers."
    image_url = "data:image/png;base64,AAAABBBB"
    body = {
        "model": "gpt-5.4-mini",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": old_text},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": old_assistant}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": recent_text},
                    {"type": "input_image", "image_url": image_url},
                ],
            },
        ],
    }
    result = compress_responses_body(body, threshold_chars=1000, target_chars=5000, profile="dev")

    old_part = result.body["input"][0]["content"][0]
    old_assistant_part = result.body["input"][1]["content"][0]
    recent_parts = result.body["input"][2]["content"]
    assert "CTC dev Caveman-style user summary" in old_part["text"]
    assert "CTC dev Caveman-style assistant summary" in old_assistant_part["text"]
    assert len(old_part["text"]) < len(old_text)
    assert len(old_assistant_part["text"]) < len(old_assistant)
    assert recent_parts[0]["text"] == recent_text
    assert recent_parts[1]["image_url"] == image_url
    assert len(result.compressed_items) == 2
    assert result.compressed_items[0].field_path == "$.content[0].text"
