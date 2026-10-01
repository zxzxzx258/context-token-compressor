from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ctc.main import create_dashboard_app
from ctc.providers import ProviderStore
from ctc.storage import CtcStore, RequestStat


def _dashboard_client(app) -> TestClient:
    return TestClient(app, headers={"Authorization": "Bearer test-admin-token"})


def _record_request(store: CtcStore, idx: int) -> None:
    timestamp = (datetime(2026, 6, 14, tzinfo=UTC) + timedelta(seconds=idx)).isoformat()
    store.record_request(
        RequestStat(
            request_id=f"r{idx:03d}",
            timestamp=timestamp,
            model="gpt-5.5",
            path="/v1/responses",
            stream=False,
            original_chars=1000 + idx,
            compressed_chars=900 + idx,
            estimated_original_tokens=250 + idx,
            estimated_compressed_tokens=200 + idx,
            estimated_saved_tokens=50,
            saved_ratio=0.2,
            compressed_items_count=1,
            passthrough_items_count=0,
            latency_ms=idx,
            status_code=200,
            source="本机客户端",
            client_host="127.0.0.1",
            error=None,
        ),
        [],
    )


def _record_source_request(
    store: CtcStore,
    request_id: str,
    idx: int,
    client_host: str,
    source: str,
    status_code: int = 200,
    error: str | None = None,
) -> None:
    timestamp = (datetime(2026, 6, 14, tzinfo=UTC) + timedelta(seconds=idx)).isoformat()
    store.record_request(
        RequestStat(
            request_id=request_id,
            timestamp=timestamp,
            model="gpt-5.5",
            path="/v1/responses",
            stream=False,
            original_chars=1000 + idx,
            compressed_chars=900 + idx,
            estimated_original_tokens=250 + idx,
            estimated_compressed_tokens=200 + idx,
            estimated_saved_tokens=50,
            saved_ratio=0.2,
            compressed_items_count=1,
            passthrough_items_count=0,
            latency_ms=idx,
            status_code=status_code,
            source=source,
            client_host=client_host,
            error=error,
        ),
        [],
    )


def test_dashboard_recent_requests_are_server_paginated(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    store = CtcStore(db)
    for idx in range(25):
        _record_request(store, idx)

    client = _dashboard_client(create_dashboard_app(store))
    response = client.get(
        "/api/dashboard",
        params={
            "since": "2026-06-13T00:00:00+00:00",
            "until": "2026-06-15T00:00:00+00:00",
            "recent_limit": 10,
            "recent_offset": 20,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_requests"] == 25
    assert payload["recent"]["total"] == 25
    assert payload["recent"]["limit"] == 10
    assert payload["recent"]["offset"] == 20
    assert len(payload["recent"]["rows"]) == 5
    assert payload["recent"]["rows"][0]["latency_ms"] == 4
    assert payload["recent"]["rows"][-1]["latency_ms"] == 0
    assert "trend_recent" in payload
    assert "trend_hourly_24h" in payload


def test_dashboard_traffic_logs_filter_by_source(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    store = CtcStore(db)
    _record_source_request(store, "local-ok", 1, "127.0.0.1", "本机客户端")
    _record_source_request(store, "lan-ok", 2, "192.0.2.16", "LAN PC (192.0.2.16)")
    _record_source_request(
        store,
        "lan-error",
        3,
        "192.0.2.16",
        "LAN PC (192.0.2.16)",
        status_code=500,
        error="upstream failed",
    )

    client = _dashboard_client(create_dashboard_app(store))
    response = client.get(
        "/api/dashboard",
        params={
            "since": "2026-06-13T00:00:00+00:00",
            "until": "2026-06-15T00:00:00+00:00",
            "recent_source": "192.0.2.16",
            "error_source": "127.0.0.1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["recent"]["total"] == 2
    assert {row["client_host"] for row in payload["recent"]["rows"]} == {"192.0.2.16"}
    assert payload["errors"]["total"] == 0
    assert payload["summary"]["error_count"] == 1
    assert payload["traffic_sources"][0]["client_host"] == "192.0.2.16"
    assert payload["traffic_sources"][0]["request_count"] == 2
    assert payload["traffic_sources"][0]["error_count"] == 1


def test_dashboard_summary_exposes_counterfactual_metering(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    store = CtcStore(db)
    base_time = datetime(2026, 6, 14, tzinfo=UTC)
    store.record_request(
        RequestStat(
            request_id="metered",
            timestamp=(base_time + timedelta(seconds=1)).isoformat(),
            model="gpt-5.5",
            path="/v1/responses",
            stream=False,
            original_chars=2400,
            compressed_chars=1400,
            estimated_original_tokens=600,
            estimated_compressed_tokens=300,
            estimated_saved_tokens=300,
            saved_ratio=0.5,
            compressed_items_count=1,
            passthrough_items_count=0,
            latency_ms=12,
            status_code=200,
            source="本机客户端",
            client_host="127.0.0.1",
            error=None,
            actual_input_tokens=500,
            actual_output_tokens=100,
            actual_total_tokens=600,
            cached_input_tokens=200,
            actual_uncached_input_tokens=300,
            baseline_input_tokens=800,
            baseline_uncached_input_tokens=600,
            cache_aligned_saved_tokens=300,
            cache_aligned_saved_ratio=0.5,
            metering_source="responses_json",
        ),
        [],
    )
    store.record_request(
        RequestStat(
            request_id="unmetered",
            timestamp=(base_time + timedelta(seconds=2)).isoformat(),
            model="gpt-5.5",
            path="/v1/responses",
            stream=False,
            original_chars=1200,
            compressed_chars=1100,
            estimated_original_tokens=300,
            estimated_compressed_tokens=260,
            estimated_saved_tokens=40,
            saved_ratio=40 / 300,
            compressed_items_count=1,
            passthrough_items_count=0,
            latency_ms=8,
            status_code=200,
            source="本机客户端",
            client_host="127.0.0.1",
            error=None,
        ),
        [],
    )

    client = _dashboard_client(create_dashboard_app(store))
    response = client.get(
        "/api/dashboard",
        params={"since": "2026-06-13T00:00:00+00:00", "until": "2026-06-15T00:00:00+00:00"},
    )

    assert response.status_code == 200
    payload = response.json()
    summary = payload["summary"]
    assert summary["metered_requests"] == 1
    assert summary["metered_coverage_ratio"] == 0.5
    assert summary["actual_input_tokens"] == 500
    assert summary["baseline_input_tokens"] == 800
    assert summary["cached_input_tokens"] == 200
    assert summary["cache_aligned_saved_tokens"] == 300
    assert summary["cache_aligned_saved_ratio"] == 0.5
    assert summary["estimated_vs_metered_saved_delta"] == 40
    assert summary["baseline_minus_actual_input_tokens"] == 300

    recent_rows = payload["recent"]["rows"]
    assert recent_rows[0]["metering_source"] == ""
    assert recent_rows[0]["metering_reason"].startswith("上游未返回 usage")
    assert recent_rows[1]["metering_source"] == "responses_json"
    assert "同缓存状态基线 800 / 实际 500 / 缓存命中 200 / 对齐节省 300 token" == recent_rows[1]["metering_reason"]


def test_dashboard_recent_requests_explain_uncompressed_rows(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    store = CtcStore(db)
    base_time = datetime(2026, 6, 14, tzinfo=UTC)
    store.record_request(
        RequestStat(
            request_id="transparent",
            timestamp=(base_time + timedelta(seconds=1)).isoformat(),
            model="",
            path="/v1/models",
            stream=False,
            original_chars=0,
            compressed_chars=0,
            estimated_original_tokens=0,
            estimated_compressed_tokens=0,
            estimated_saved_tokens=0,
            saved_ratio=0,
            compressed_items_count=0,
            passthrough_items_count=0,
            latency_ms=3,
            status_code=200,
            source="本机客户端",
            client_host="127.0.0.1",
            profile="safe",
        ),
        [],
    )
    store.record_request(
        RequestStat(
            request_id="miss",
            timestamp=(base_time + timedelta(seconds=2)).isoformat(),
            model="gpt-5.5",
            path="/v1/responses",
            stream=False,
            original_chars=0,
            compressed_chars=0,
            estimated_original_tokens=0,
            estimated_compressed_tokens=0,
            estimated_saved_tokens=0,
            saved_ratio=0,
            compressed_items_count=0,
            passthrough_items_count=3,
            latency_ms=5,
            status_code=200,
            source="本机客户端",
            client_host="127.0.0.1",
            profile="safe",
        ),
        [],
    )
    store.record_request(
        RequestStat(
            request_id="chat-miss",
            timestamp=(base_time + timedelta(seconds=3)).isoformat(),
            model="deepseek-v4-flash",
            path="/v1/chat/completions",
            stream=False,
            original_chars=0,
            compressed_chars=0,
            estimated_original_tokens=0,
            estimated_compressed_tokens=0,
            estimated_saved_tokens=0,
            saved_ratio=0,
            compressed_items_count=0,
            passthrough_items_count=2,
            latency_ms=7,
            status_code=200,
            source="本机客户端",
            client_host="127.0.0.1",
            profile="safe",
        ),
        [],
    )

    client = _dashboard_client(create_dashboard_app(store))
    response = client.get(
        "/api/dashboard",
        params={
            "since": "2026-06-13T00:00:00+00:00",
            "until": "2026-06-15T00:00:00+00:00",
        },
    )

    assert response.status_code == 200
    rows = response.json()["recent"]["rows"]
    assert rows[0]["compression_status"] == "skipped"
    assert "Chat 请求经过 CTC" in rows[0]["compression_reason"]
    assert rows[1]["compression_status"] == "skipped"
    assert "未发现超过阈值" in rows[1]["compression_reason"]
    assert rows[2]["compression_status"] == "transparent"
    assert "透明转发" in rows[2]["compression_reason"]


def test_dashboard_profile_rules_api(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    store = CtcStore(db)
    store.touch_profile_source("192.0.2.13", "LAN 192.0.2.13", "off")

    client = _dashboard_client(create_dashboard_app(store))
    response = client.get("/api/profiles")
    assert response.status_code == 200
    payload = response.json()
    assert "dev" in payload["profiles"]
    assert payload["sources"][0]["effective_profile"] == "off"

    response = client.post("/api/profiles", json={"client_host": "192.0.2.13", "profile": "dev"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["rule"]["profile"] == "dev"
    assert store.resolve_profile("192.0.2.13") == "dev"


def test_dashboard_provider_management_api(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    monkeypatch.setenv("CTC_UPSTREAM_BASE_URL", "")
    store = CtcStore(db)

    client = _dashboard_client(create_dashboard_app(store))
    response = client.get("/api/providers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active_provider"]["base_url"] == "/v1"
    assert payload["provider_routing"] == {
        "enabled": False,
        "rules": {},
        "rule_count": 0,
        "source": "default",
    }

    response = client.post(
        "/api/providers",
        json={
            "name": "Fallback",
            "base_url": "https://fallback.example/v1",
            "api_key": "secret-key",
            "model": "gpt-5.5",
            "provider_type": "deepseek_chat_bridge",
            "forwarded_user_agent": "CTC-Dashboard/1.0",
            "activate": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    provider = payload["provider"]
    assert provider["active"] is True
    assert provider["provider_type"] == "deepseek_chat_bridge"
    assert provider["forwarded_user_agent"] == "CTC-Dashboard/1.0"
    assert provider["has_api_key"] is True
    assert "secret-key" not in str(payload)
    assert payload["active_provider"]["model"] == "gpt-5.5"

    response = client.patch(
        f"/api/providers/{provider['id']}",
        json={
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "forwarded_user_agent": "CTC-Edited/1.0",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    provider = payload["provider"]
    assert provider["name"] == "DeepSeek"
    assert provider["base_url"] == "https://api.deepseek.com/v1"
    assert provider["forwarded_user_agent"] == "CTC-Edited/1.0"
    assert "secret-key" not in str(payload)

    response = client.patch(f"/api/providers/{provider['id']}", json={"model": ""})
    assert response.status_code == 200
    provider = response.json()["provider"]
    assert provider["model"] == ""

    response = client.post(f"/api/providers/{provider['id']}/activate")
    assert response.status_code == 200
    assert response.json()["active_provider"]["id"] == provider["id"]

    saved = ProviderStore(provider_path, client.app.state.settings).get_provider(provider["id"])
    assert saved is not None
    assert saved.api_key == "secret-key"
    assert saved.model == ""


def test_dashboard_provider_routing_api_ignores_environment_rules(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    runtime_path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    monkeypatch.setenv("CTC_RUNTIME_CONFIG_PATH", str(runtime_path))
    monkeypatch.setenv("CTC_ENABLE_PROVIDER_RULES", "1")
    monkeypatch.setenv("CTC_PROVIDER_RULES", "*=missing-env-provider")
    store = CtcStore(db)

    client = _dashboard_client(create_dashboard_app(store))
    first = client.post(
        "/api/providers",
        json={
            "name": "First",
            "base_url": "https://first.example/v1",
            "api_key": "first-key",
            "activate": True,
        },
    ).json()["provider"]
    second = client.post(
        "/api/providers",
        json={
            "name": "Second",
            "base_url": "https://second.example/v1",
            "api_key": "second-key",
            "activate": False,
        },
    ).json()["provider"]

    response = client.get("/api/providers")
    assert response.status_code == 200
    routing = response.json()["provider_routing"]
    assert routing["enabled"] is False
    assert routing["rules"] == {}
    assert routing["source"] == "default"

    response = client.patch(
        "/api/runtime-config/provider-routing",
        json={"enabled": True, "rules": {"192.0.2.13": second["id"]}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_routing"]["source"] == "runtime_config"
    assert payload["provider_routing"]["rules"] == {"192.0.2.13": second["id"]}

    provider_store = client.app.state.provider_store
    assert provider_store.provider_for_host("192.0.2.13").id == second["id"]
    assert provider_store.provider_for_host("192.0.2.23").id == first["id"]

    response = client.patch(
        "/api/runtime-config/provider-routing",
        json={"enabled": False, "rules": {"192.0.2.13": second["id"]}},
    )
    assert response.status_code == 200
    assert provider_store.provider_for_host("192.0.2.13").id == first["id"]


def test_dashboard_review_page_is_served(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    client = _dashboard_client(create_dashboard_app(CtcStore(db)))

    response = client.get("/test.html")

    assert response.status_code == 200
    assert "Context Token Compressor 人工审核版" in response.text
    assert "/api/runtime-config/provider-routing" in response.text
    assert "总请求数" in response.text
    assert "进入 CTC 的模型请求" in response.text
    assert "压缩请求数" in response.text
    assert "估算原始 Token" in response.text
    assert "估算压缩后 Token" in response.text
    assert "累计节省 Token" in response.text
    assert "平均每请求节省" in response.text
    assert "4xx/5xx 或代理错误" in response.text
    assert "可压缩原 Token" in response.text
    assert "压缩后" in response.text
    assert "节省" in response.text
    assert '<section id="traffic" class="view">\n      <article class="card">' in response.text


def test_provider_routing_rejects_unknown_provider(tmp_path, monkeypatch):
    db = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    runtime_path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("CTC_DB_PATH", str(db))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    monkeypatch.setenv("CTC_RUNTIME_CONFIG_PATH", str(runtime_path))
    client = _dashboard_client(create_dashboard_app(CtcStore(db)))

    response = client.patch(
        "/api/runtime-config/provider-routing",
        json={"enabled": True, "rules": {"*": "missing-provider"}},
    )

    assert response.status_code == 400
    assert "unknown provider id" in response.json()["detail"]


def test_index_routing_add_keeps_wildcard_rule_as_draft():
    index_path = Path(__file__).resolve().parents[1] / "ctc" / "static" / "index.html"
    html = index_path.read_text(encoding="utf-8")
    add_function = html.split("function addProviderRoutingRow", 1)[1].split("function updateRoutingRule", 1)[0]

    assert 'host = host || "*"' not in add_function
    assert "providerRouting.rules[host] = pid" not in add_function
    assert "data-routing-draft" in add_function
    assert "data-routing-create" in add_function


def test_dashboard_rejects_invalid_range_params(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    app = create_dashboard_app()
    client = _dashboard_client(app)

    res = client.get("/api/dashboard", params={"since": "garbage"})
    assert res.status_code == 400

    res = client.get("/api/dashboard", params={"since": "2026-06-20T00:00:00+00:00", "until": "2026-06-01T00:00:00+00:00"})
    assert res.status_code == 400
