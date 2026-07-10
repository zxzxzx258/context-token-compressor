from __future__ import annotations

import json
import sqlite3
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ctc.config import load_settings
from ctc.main import create_proxy_app
from ctc.providers import ProviderStore
from ctc.storage import CtcStore


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_app(app: FastAPI, port: int) -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def test_proxy_compresses_and_forwards(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = await request.json()
        return JSONResponse({"id": "ok", "output": []})

    @upstream.get("/v1/models")
    async def models():
        return {"data": [{"id": "mock"}]}

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        long_output = "\n".join(f"line {i}" for i in range(1400))
        payload = {
            "model": "gpt-5.5",
            "input": [{"type": "function_call_output", "call_id": "c1", "output": long_output}],
        }
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            headers={"authorization": "Bearer test-token"},
            json=payload,
            timeout=20,
        )
        assert res.status_code == 200
        assert captured["authorization"] == "Bearer test-token"
        assert "CTC compressed tool output" in captured["body"]["input"][0]["output"]
        db_raw = (tmp_path / "ctc.sqlite3").read_bytes()
        assert b"test-token" not in db_raw
        assert b"Bearer" not in db_raw
        with sqlite3.connect(tmp_path / "ctc.sqlite3") as con:
            source, client_host, profile = con.execute(
                "select source, client_host, profile from request_stats where path = '/v1/responses'",
            ).fetchone()
        assert source == "本机客户端"
        assert client_host == "127.0.0.1"
        assert profile == "safe"
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_proxy_uses_x_forwarded_for_from_trusted_proxy_for_source_and_profile(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        captured["body"] = await request.json()
        return JSONResponse({"id": "ok", "output": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROFILE_RULES", "192.0.2.13=dev")
    monkeypatch.setenv("CTC_TRUSTED_PROXY_HOSTS", "127.0.0.1")
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        long_output = "\n".join(["pytest tests", "FAILED tests/test_ctc.py::test_dev", "Traceback error"] * 500)
        payload = {
            "model": "gpt-5.5",
            "input": [{"type": "function_call_output", "call_id": "c1", "output": long_output}],
        }
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            headers={"x-forwarded-for": "192.0.2.13, 127.0.0.1"},
            json=payload,
            timeout=20,
        )
        assert res.status_code == 200
        assert "CTC dev RTK-style tool summary" in captured["body"]["input"][0]["output"]
        with sqlite3.connect(db_path) as con:
            source, client_host, profile = con.execute(
                "select source, client_host, profile from request_stats where path = '/v1/responses'",
            ).fetchone()
        assert source == "LAN 192.0.2.13"
        assert client_host == "192.0.2.13"
        assert profile == "dev"
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_active_provider_overrides_upstream_key_and_model(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = await request.json()
        return JSONResponse({"id": "ok", "output": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", "")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="Fallback",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="fallback-key",
        model="gpt-5.5",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {"model": "test-model", "input": [{"role": "user", "content": "hi"}]}
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            headers={"authorization": "Bearer old-client-key"},
            json=payload,
            timeout=20,
        )
        assert res.status_code == 200
        assert captured["authorization"] == "Bearer fallback-key"
        assert captured["body"]["model"] == "gpt-5.5"
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_proxy_adds_output_text_when_upstream_omits_it(tmp_path, monkeypatch):
    upstream_port = _free_port()
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        await request.json()
        return JSONResponse(
            {
                "id": "resp_1",
                "object": "response",
                "output_text": "",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "visible text"}],
                    }
                ],
                "status": "completed",
            }
        )

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]}
        res = httpx.post(f"http://127.0.0.1:{proxy_port}/v1/responses", json=payload, timeout=20)

        assert res.status_code == 200
        assert res.json()["output_text"] == "visible text"
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_deepseek_bridge_provider_converts_responses_to_chat(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/chat/completions")
    @upstream.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        captured["user_agent"] = request.headers.get("user-agent")
        captured["body"] = await request.json()
        return JSONResponse(
            {
                "id": "chatcmpl_1",
                "model": captured["body"]["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        )

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", "")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="DeepSeek",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        provider_type="deepseek_chat_bridge",
        forwarded_user_agent="CTC-Test/1.0",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {
            "model": "gpt-5.5",
            "input": [{"role": "user", "content": "please continue"}],
        }
        res = httpx.post(f"http://127.0.0.1:{proxy_port}/v1/responses", json=payload, timeout=20)

        assert res.status_code == 200
        assert res.json()["object"] == "response"
        assert res.json()["model"] == "deepseek-v4-flash"
        assert captured["authorization"] == "Bearer deepseek-key"
        assert captured["user_agent"] == "CTC-Test/1.0"
        assert captured["body"]["model"] == "deepseek-v4-flash"
        assert captured["body"]["messages"][0] == {"role": "user", "content": "please continue"}
        with sqlite3.connect(db_path) as con:
            status_code, saved_tokens = con.execute(
                "select status_code, estimated_saved_tokens from request_stats where path = '/v1/responses'",
            ).fetchone()
        assert status_code == 200
        assert saved_tokens == 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_deepseek_bridge_replays_reasoning_content_for_tool_followup(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured: list[dict] = []
    upstream = FastAPI()

    @upstream.post("/chat/completions")
    @upstream.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        captured.append(body)
        if len(captured) == 1:
            return JSONResponse(
                {
                    "id": "chatcmpl_tool",
                    "model": body["model"],
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "need this later",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "shell", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ],
                }
            )
        return JSONResponse(
            {
                "id": "chatcmpl_done",
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
            }
        )

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="DeepSeek",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        provider_type="deepseek_chat_bridge",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        first = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={
                "model": "gpt-5.5",
                "input": [{"role": "user", "content": "run tool"}],
            },
            timeout=20,
        )
        assert first.status_code == 200
        response_id = first.json()["id"]

        long_output = "\n".join(f"line {i}" for i in range(1400))
        second = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={
                "model": "gpt-5.5",
                "previous_response_id": response_id,
                "input": [{"type": "function_call_output", "call_id": "call_1", "output": long_output}],
            },
            timeout=20,
        )

        assert second.status_code == 200
        assert len(captured) == 2
        second_messages = captured[1]["messages"]
        assert second_messages[1]["role"] == "assistant"
        assert second_messages[1]["reasoning_content"] == "need this later"
        assert second_messages[1]["tool_calls"][0]["id"] == "call_1"
        assert second_messages[2]["role"] == "tool"
        assert second_messages[2]["tool_call_id"] == "call_1"
        assert "CTC compressed tool output" in second_messages[2]["content"]
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_deepseek_bridge_stream_returns_responses_sse(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/chat/completions")
    @upstream.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        captured["body"] = await request.json()
        return JSONResponse(
            {
                "id": "chatcmpl_stream",
                "model": captured["body"]["model"],
                "choices": [{"message": {"role": "assistant", "content": "stream ok"}}],
            }
        )

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", "")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="DeepSeek",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        provider_type="deepseek_chat_bridge",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {"model": "gpt-5.5", "stream": True, "input": [{"role": "user", "content": "hi"}]}
        with httpx.stream("POST", f"http://127.0.0.1:{proxy_port}/v1/responses", json=payload, timeout=20) as res:
            body = b"".join(res.iter_bytes())

        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        assert captured["body"]["stream"] is False
        assert b"response.output_text.delta" in body
        assert b"stream ok" in body
        assert b"response.completed" in body
        assert b"data: [DONE]" in body
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_provider_rules_are_disabled_by_default_so_active_provider_wins(tmp_path, monkeypatch):
    default_port = _free_port()
    codex_port = _free_port()
    captured: dict[str, dict[str, str]] = {}
    default_upstream = FastAPI()
    codex_upstream = FastAPI()

    @default_upstream.post("/v1/responses")
    async def default_responses(request: Request):
        captured["default"] = {
            "authorization": request.headers.get("authorization", ""),
            "user_agent": request.headers.get("user-agent", ""),
        }
        return JSONResponse({"id": "default"})

    @codex_upstream.post("/v1/responses")
    async def codex_responses(request: Request):
        captured["codex"] = {
            "authorization": request.headers.get("authorization", ""),
            "user_agent": request.headers.get("user-agent", ""),
        }
        return JSONResponse({"id": "codex"})

    default_server = _run_app(default_upstream, default_port)
    codex_server = _run_app(codex_upstream, codex_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    codex_user_agent = "Codex/1.0 (Windows; x86_64)"
    provider_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "default-provider",
                        "name": "Default",
                        "provider_type": "openai_responses",
                        "base_url": f"http://127.0.0.1:{default_port}/v1",
                        "api_key": "default-key",
                        "active": True,
                    },
                    {
                        "id": "codex-provider",
                        "name": "Codex",
                        "provider_type": "openai_responses",
                        "base_url": f"http://127.0.0.1:{codex_port}/v1",
                        "api_key": "codex-key",
                        "active": False,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{default_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    monkeypatch.setenv("CTC_PROVIDER_RULES", "127.0.0.1=codex-provider")
    monkeypatch.setenv("CTC_FORWARDED_USER_AGENT", codex_user_agent)
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={"model": "gpt-5", "input": "hello"},
            headers={"authorization": "Bearer inbound", "user-agent": "Original/1.0"},
            timeout=20,
        )
        assert res.status_code == 200
        assert res.json()["id"] == "default"
        assert "codex" not in captured
        assert captured["default"]["authorization"] == "Bearer default-key"
        assert captured["default"]["user_agent"] == codex_user_agent
    finally:
        proxy_server.should_exit = True
        default_server.should_exit = True
        codex_server.should_exit = True


def test_runtime_provider_routing_and_forwarded_user_agent(tmp_path, monkeypatch):
    default_port = _free_port()
    codex_port = _free_port()
    captured: dict[str, dict[str, str]] = {}
    default_upstream = FastAPI()
    codex_upstream = FastAPI()

    @default_upstream.post("/v1/responses")
    async def default_responses(request: Request):
        captured["default"] = {
            "authorization": request.headers.get("authorization", ""),
            "user_agent": request.headers.get("user-agent", ""),
        }
        return JSONResponse({"id": "default"})

    @codex_upstream.post("/v1/responses")
    async def codex_responses(request: Request):
        captured["codex"] = {
            "authorization": request.headers.get("authorization", ""),
            "user_agent": request.headers.get("user-agent", ""),
        }
        return JSONResponse({"id": "codex"})

    default_server = _run_app(default_upstream, default_port)
    codex_server = _run_app(codex_upstream, codex_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    runtime_path = tmp_path / "runtime_config.json"
    codex_user_agent = "Codex/1.0 (Windows; x86_64)"
    provider_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "default-provider",
                        "name": "Default",
                        "provider_type": "openai_responses",
                        "base_url": f"http://127.0.0.1:{default_port}/v1",
                        "api_key": "default-key",
                        "active": True,
                    },
                    {
                        "id": "codex-provider",
                        "name": "Codex",
                        "provider_type": "openai_responses",
                        "base_url": f"http://127.0.0.1:{codex_port}/v1",
                        "api_key": "codex-key",
                        "active": False,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{default_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    monkeypatch.setenv("CTC_RUNTIME_CONFIG_PATH", str(runtime_path))
    monkeypatch.setenv("CTC_FORWARDED_USER_AGENT", codex_user_agent)
    runtime_path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider_routing": {
                    "enabled": True,
                    "rules": {"127.0.0.1": "codex-provider"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={"model": "gpt-5", "input": "hello"},
            headers={"authorization": "Bearer inbound", "user-agent": "Original/1.0"},
            timeout=20,
        )
        assert res.status_code == 200
        assert res.json()["id"] == "codex"
        assert "default" not in captured
        assert captured["codex"]["authorization"] == "Bearer codex-key"
        assert captured["codex"]["user_agent"] == codex_user_agent
    finally:
        proxy_server.should_exit = True
        default_server.should_exit = True
        codex_server.should_exit = True


def test_deepseek_bridge_models_are_served_locally(tmp_path, monkeypatch):
    upstream_port = _free_port()
    upstream_hits = {"count": 0}
    upstream = FastAPI()

    @upstream.get("/v1/models")
    async def models():
        upstream_hits["count"] += 1
        return JSONResponse({"object": "list", "data": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="DeepSeek",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        provider_type="deepseek_chat_bridge",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        list_response = httpx.get(f"http://127.0.0.1:{proxy_port}/v1/models", timeout=20)
        detail_response = httpx.get(f"http://127.0.0.1:{proxy_port}/v1/models/gpt-5.5", timeout=20)

        assert list_response.status_code == 200
        assert detail_response.status_code == 200
        assert list_response.json()["data"][0]["id"] == "deepseek-v4-flash"
        assert detail_response.json()["id"] == "gpt-5.5"
        assert detail_response.json()["ctc_bridge_model"] == "deepseek-v4-flash"
        assert upstream_hits["count"] == 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_deepseek_bridge_falls_back_for_orphan_tool_output(tmp_path, monkeypatch):
    fallback_port = _free_port()
    deepseek_port = _free_port()
    captured: dict[str, dict] = {}
    fallback_upstream = FastAPI()
    deepseek_upstream = FastAPI()

    @fallback_upstream.post("/v1/responses")
    async def fallback_responses(request: Request):
        captured["fallback"] = await request.json()
        return JSONResponse({"id": "fallback_resp", "object": "response", "output": []})

    @deepseek_upstream.post("/chat/completions")
    @deepseek_upstream.post("/v1/chat/completions")
    async def deepseek_chat(request: Request):
        captured["deepseek"] = await request.json()
        return JSONResponse({"id": "should_not_hit"})

    fallback_server = _run_app(fallback_upstream, fallback_port)
    deepseek_server = _run_app(deepseek_upstream, deepseek_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    provider_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "fallback-provider",
                        "name": "Fallback",
                        "provider_type": "openai_responses",
                        "base_url": f"http://127.0.0.1:{fallback_port}/v1",
                        "api_key": "fallback-key",
                        "active": False,
                    },
                    {
                        "id": "deepseek-provider",
                        "name": "DeepSeek",
                        "provider_type": "deepseek_chat_bridge",
                        "base_url": f"http://127.0.0.1:{deepseek_port}/v1",
                        "api_key": "deepseek-key",
                        "model": "deepseek-v4-flash",
                        "active": True,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={
                "model": "gpt-5.5",
                "input": [{"type": "function_call_output", "call_id": "unknown_call", "output": "done"}],
            },
            timeout=20,
        )

        assert res.status_code == 200
        assert res.json()["id"] == "fallback_resp"
        assert "deepseek" not in captured
        assert captured["fallback"]["input"][0]["call_id"] == "unknown_call"
    finally:
        proxy_server.should_exit = True
        fallback_server.should_exit = True
        deepseek_server.should_exit = True


def test_deepseek_bridge_keeps_paired_tool_output_on_deepseek(tmp_path, monkeypatch):
    fallback_port = _free_port()
    deepseek_port = _free_port()
    captured: dict[str, dict] = {}
    fallback_upstream = FastAPI()
    deepseek_upstream = FastAPI()

    @fallback_upstream.post("/v1/responses")
    async def fallback_responses(request: Request):
        captured["fallback"] = await request.json()
        return JSONResponse({"id": "fallback_resp", "object": "response", "output": []})

    @deepseek_upstream.post("/chat/completions")
    @deepseek_upstream.post("/v1/chat/completions")
    async def deepseek_chat(request: Request):
        captured["deepseek"] = await request.json()
        return JSONResponse(
            {
                "id": "chatcmpl_paired_tool",
                "model": captured["deepseek"]["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }
        )

    fallback_server = _run_app(fallback_upstream, fallback_port)
    deepseek_server = _run_app(deepseek_upstream, deepseek_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    provider_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "fallback-provider",
                        "name": "Fallback",
                        "provider_type": "openai_responses",
                        "base_url": f"http://127.0.0.1:{fallback_port}/v1",
                        "api_key": "fallback-key",
                        "active": False,
                    },
                    {
                        "id": "deepseek-provider",
                        "name": "DeepSeek",
                        "provider_type": "deepseek_chat_bridge",
                        "base_url": f"http://127.0.0.1:{deepseek_port}/v1",
                        "api_key": "deepseek-key",
                        "model": "deepseek-v4-flash",
                        "active": True,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={
                "model": "gpt-5.5",
                "input": [
                    {"role": "user", "content": "run tool"},
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "shell_command",
                        "arguments": "{\"command\":\"date\"}",
                    },
                    {"type": "function_call_output", "call_id": "call_1", "output": "done"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "shell_command",
                        "description": "run command",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            timeout=20,
        )

        assert res.status_code == 200
        assert res.json()["model"] == "deepseek-v4-flash"
        assert "fallback" not in captured
        messages = captured["deepseek"]["messages"]
        assert messages[1]["role"] == "assistant"
        assert messages[1]["tool_calls"][0]["id"] == "call_1"
        assert messages[2] == {"role": "tool", "tool_call_id": "call_1", "content": "done"}
    finally:
        proxy_server.should_exit = True
        fallback_server.should_exit = True
        deepseek_server.should_exit = True


def test_deepseek_bridge_current_tool_call_overrides_cached_call_id(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured: list[dict] = []
    upstream = FastAPI()

    @upstream.post("/chat/completions")
    @upstream.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        captured.append(body)
        if len(captured) == 1:
            return JSONResponse(
                {
                    "id": "chatcmpl_cached",
                    "model": body["model"],
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "old_tool", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ],
                }
            )
        return JSONResponse(
            {
                "id": "chatcmpl_current",
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }
        )

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="DeepSeek",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="deepseek-key",
        model="deepseek-v4-flash",
        provider_type="deepseek_chat_bridge",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        first = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={"model": "gpt-5.5", "input": [{"role": "user", "content": "cache call"}]},
            timeout=20,
        )
        assert first.status_code == 200

        second = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            json={
                "model": "gpt-5.5",
                "input": [
                    {"role": "user", "content": "new call"},
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "new_tool",
                        "arguments": "{\"fresh\":true}",
                    },
                    {"type": "function_call_output", "call_id": "call_1", "output": "fresh output"},
                ],
            },
            timeout=20,
        )

        assert second.status_code == 200
        second_messages = captured[1]["messages"]
        assert second_messages[1]["tool_calls"][0]["function"]["name"] == "new_tool"
        assert second_messages[2] == {"role": "tool", "tool_call_id": "call_1", "content": "fresh output"}
        assert all(message.get("tool_calls", [{}])[0].get("function", {}).get("name") != "old_tool" for message in second_messages if message.get("tool_calls"))
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_proxy_profile_header_can_disable_compression(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        captured["body"] = await request.json()
        return JSONResponse({"id": "ok", "output": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_ALLOW_PROFILE_HEADER", "1")
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        long_output = "\n".join(f"line {i}" for i in range(1400))
        payload = {
            "model": "gpt-5.5",
            "input": [{"type": "function_call_output", "call_id": "c1", "output": long_output}],
        }
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            headers={"x-ctc-profile": "off"},
            json=payload,
            timeout=20,
        )
        assert res.status_code == 200
        assert captured["body"]["input"][0]["output"] == long_output
        with sqlite3.connect(db_path) as con:
            profile, saved_tokens = con.execute(
                "select profile, estimated_saved_tokens from request_stats where path = '/v1/responses'",
            ).fetchone()
        assert profile == "off"
        assert saved_tokens == 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_profile_database_rule_overrides_env_fallback(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        captured["body"] = await request.json()
        return JSONResponse({"id": "ok", "output": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROFILE_RULES", "127.0.0.1=dev")
    CtcStore(db_path).set_profile_rule("127.0.0.1", "off")
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        long_output = "\n".join(f"line {i}" for i in range(1400))
        payload = {
            "model": "gpt-5.5",
            "input": [{"type": "function_call_output", "call_id": "c1", "output": long_output}],
        }
        res = httpx.post(f"http://127.0.0.1:{proxy_port}/v1/responses", json=payload, timeout=20)
        assert res.status_code == 200
        assert captured["body"]["input"][0]["output"] == long_output
        with sqlite3.connect(db_path) as con:
            profile, saved_tokens = con.execute(
                "select profile, estimated_saved_tokens from request_stats where path = '/v1/responses'",
            ).fetchone()
        assert profile == "off"
        assert saved_tokens == 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_proxy_dev_profile_header_uses_dev_summary(tmp_path, monkeypatch):
    upstream_port = _free_port()
    captured = {}
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        captured["body"] = await request.json()
        return JSONResponse({"id": "ok", "output": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_ALLOW_PROFILE_HEADER", "1")
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        long_output = "\n".join(["pytest tests", "FAILED tests/test_ctc.py::test_dev", "Traceback error"] * 500)
        payload = {
            "model": "gpt-5.5",
            "input": [{"type": "function_call_output", "call_id": "c1", "output": long_output}],
        }
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses",
            headers={"x-ctc-profile": "dev"},
            json=payload,
            timeout=20,
        )
        assert res.status_code == 200
        assert "CTC dev RTK-style tool summary" in captured["body"]["input"][0]["output"]
        with sqlite3.connect(db_path) as con:
            profile, saved_tokens = con.execute(
                "select profile, estimated_saved_tokens from request_stats where path = '/v1/responses'",
            ).fetchone()
        assert profile == "dev"
        assert saved_tokens > 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_proxy_stream_passthrough(tmp_path, monkeypatch):
    upstream_port = _free_port()
    upstream = FastAPI()

    @upstream.post("/v1/responses")
    async def responses(request: Request):
        async def events():
            yield b"data: one\n\n"
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {"model": "gpt-5.5", "stream": True, "input": [{"role": "user", "content": "hi"}]}
        with httpx.stream("POST", f"http://127.0.0.1:{proxy_port}/v1/responses", json=payload, timeout=20) as res:
            body = b"".join(res.iter_bytes())
        assert res.status_code == 200
        assert b"data: one" in body
        assert b"data: [DONE]" in body
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_props_probe_is_forwarded_without_stats(tmp_path, monkeypatch):
    upstream_port = _free_port()
    upstream = FastAPI()
    upstream_hits = {"count": 0}

    @upstream.get("/v1/props")
    async def props():
        upstream_hits["count"] += 1
        return JSONResponse({"unexpected": True}, status_code=404)

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        res = httpx.get(f"http://127.0.0.1:{proxy_port}/v1/props", timeout=20)
        assert res.status_code == 404
        assert res.json() == {"unexpected": True}
        assert upstream_hits["count"] == 1
        with sqlite3.connect(db_path) as con:
            count = con.execute("select count(*) from request_stats").fetchone()[0]
        assert count == 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_proxy_retries_retryable_upstream_502_once(tmp_path, monkeypatch):
    import ctc.proxy as proxy_module

    upstream_port = _free_port()
    upstream = FastAPI()
    upstream_hits = {"count": 0}
    sleep_calls: list[str] = []

    async def fake_sleep_before_retry() -> None:
        sleep_calls.append("sleep")

    monkeypatch.setattr(proxy_module, "_sleep_before_upstream_retry", fake_sleep_before_retry)

    @upstream.post("/v1/responses")
    async def responses():
        upstream_hits["count"] += 1
        if upstream_hits["count"] == 1:
            return JSONResponse({"error": {"message": "temporary bad gateway"}}, status_code=502)
        return JSONResponse({"id": "ok", "output": []})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}/v1")
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}]}
        res = httpx.post(f"http://127.0.0.1:{proxy_port}/v1/responses", json=payload, timeout=20)

        assert res.status_code == 200
        assert upstream_hits["count"] == 2
        assert sleep_calls == ["sleep"]
        with sqlite3.connect(db_path) as con:
            status_codes = con.execute(
                "select status_code from request_stats where path = '/v1/responses'",
            ).fetchall()
        assert status_codes == [(200,)]
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True


def test_retryable_upstream_exception_includes_httpx_network_errors():
    import ctc.proxy as proxy_module

    assert proxy_module._is_retryable_upstream_exception(httpx.WriteTimeout("write timed out"))
    assert not proxy_module._is_retryable_upstream_exception(ValueError("not a network error"))


def test_chat_completions_compresses_and_forwards(tmp_path, monkeypatch):
    """Chat Completions endpoint compresses tool messages and forwards."""
    upstream_port = _free_port()
    upstream = FastAPI()
    captured = {}

    @upstream.post("/chat/completions")
    @upstream.post("/v1/chat/completions")
    async def chat(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = await request.json()
        return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    upstream_server = _run_app(upstream, upstream_port)
    proxy_port = _free_port()
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_PROXY_PORT", str(proxy_port))
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    provider_store = ProviderStore(provider_path, load_settings())
    provider_store.add_provider(
        name="Fallback",
        base_url=f"http://127.0.0.1:{upstream_port}/v1",
        api_key="fallback-key",
        model="gpt-5.5",
        activate=True,
    )
    proxy_server = _run_app(create_proxy_app(), proxy_port)
    try:
        payload = {
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "tool",
                    "content": "\n".join(
                        f"ERROR /tmp/ctc_case_{idx}.py failed with tokens={idx}"
                        for idx in range(1000)
                    ),
                },
            ],
        }
        res = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            headers={"authorization": "Bearer old-client-key"},
            json=payload,
            timeout=20,
        )
        assert res.status_code == 200
        assert captured["authorization"] == "Bearer fallback-key"
        assert captured["body"]["model"] == "gpt-5.5"
        assert "CTC compressed tool output" in captured["body"]["messages"][1]["content"]
        with sqlite3.connect(db_path) as con:
            row = con.execute(
                """
                select model, profile, compressed_items_count, passthrough_items_count, estimated_saved_tokens
                from request_stats
                where path = '/v1/chat/completions'
                """,
            ).fetchone()
        model, profile, compressed_items, passthrough_items, saved_tokens = row
        assert model == "test-model"
        assert profile == "safe"
        assert compressed_items == 1
        assert passthrough_items == 1
        assert saved_tokens > 0
    finally:
        proxy_server.should_exit = True
        upstream_server.should_exit = True
