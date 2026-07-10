from __future__ import annotations

from ctc.compressor import compress_dev_message_text, compress_text
from ctc.factsheet import build_factsheet_sidecar, extract_factsheet_tokens


def test_extract_factsheet_tokens_keeps_precise_paths_ids_versions_and_hashes():
    text = "\n".join(
        [
            r"C:\workspace\CTC\ctc\proxy.py failed with HTTP 502",
            "request_id=req_abc12345 trace_id=9d121ac version v1.2.3 ERR_SSL_PROTOCOL_ERROR",
            "see runtime_config.json and ctc.sqlite3",
        ]
    )
    tokens = extract_factsheet_tokens(text)

    assert r"C:\workspace\CTC\ctc\proxy.py" in tokens
    assert "HTTP 502" in tokens
    assert "ERR_SSL_PROTOCOL_ERROR" in tokens
    assert "trace_id=9d121ac" in tokens
    assert "v1.2.3" in tokens
    assert "proxy.py" not in tokens


def test_build_factsheet_sidecar_is_compact_and_deterministic():
    text = "path /srv/context-token-compressor/providers.json request_id=req_1234567 sha 6d80bd6"
    first = build_factsheet_sidecar(text, max_chars=200)
    second = build_factsheet_sidecar(text, max_chars=200)

    assert first == second
    assert first.startswith("[CTC factsheet] exact tokens: ")
    assert "/srv/context-token-compressor/providers.json" in first
    assert "req_1234567" in first
    assert "6d80bd6" in first


def test_compressed_outputs_append_factsheet_sidecar():
    tool_text = "\n".join(
        ["noise line"] * 800
        + [
            r"C:\workspace\CTC\ctc\proxy.py",
            "request_id=req_abcdef12",
            "version 1.4.7",
            "ERR_SSL_PROTOCOL_ERROR",
            "hash 6d80bd6",
        ]
    )
    compressed_tool = compress_text(tool_text, model="gpt-5.5", target_chars=2400)
    assert "[CTC factsheet] exact tokens:" in compressed_tool
    assert "req_abcdef12" in compressed_tool
    assert "6d80bd6" in compressed_tool

    message_text = (
        "请保留这次修改的真实路径和错误码。"
        + (r" C:\workspace\CTC\ctc\storage.py HTTP 500 version v2.0.1" * 120)
    )
    compressed_message = compress_dev_message_text(message_text, role="user", model="gpt-5.5", target_chars=2600)
    assert "[CTC factsheet] exact tokens:" in compressed_message
    assert r"C:\workspace\CTC\ctc\storage.py" in compressed_message
    assert "HTTP 500" in compressed_message
    assert "v2.0.1" in compressed_message
