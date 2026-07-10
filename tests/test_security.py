from __future__ import annotations

import os
import stat
from dataclasses import replace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from ctc.config import load_settings
from ctc.main import create_dashboard_app, create_proxy_app
from ctc.providers import ProviderConfig, ProviderStore
from ctc.proxy import _client_host, _forward_headers
from ctc.runtime_config import RuntimeConfigStore
from ctc.storage import CtcStore


def test_dashboard_api_requires_token_and_health_is_minimal(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    app = create_dashboard_app()
    client = TestClient(app)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert set(health.json()) == {"status", "component", "version"}
    assert health.headers["x-content-type-options"] == "nosniff"

    denied = client.get("/api/status")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"

    allowed = client.get("/api/status", headers={"Authorization": "Bearer test-admin-token"})
    assert allowed.status_code == 200
    assert "api_key_preview" not in allowed.text


def test_non_loopback_proxy_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    monkeypatch.setenv("CTC_PROXY_HOST", "0.0.0.0")

    with pytest.raises(ValueError, match="CTC_PROXY_TOKEN"):
        create_proxy_app()


def test_proxy_authentication_and_body_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    monkeypatch.setenv("CTC_PROXY_TOKEN", "test-proxy-token")
    monkeypatch.setenv("CTC_MAX_BODY_BYTES", "64")
    app = create_proxy_app(require_proxy_token=True)
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/models").status_code == 401

    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer test-proxy-token"},
        json={"model": "test", "input": "x" * 2000},
    )
    assert response.status_code == 413


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits reliably")
def test_runtime_secret_files_are_private(tmp_path, monkeypatch):
    db_path = tmp_path / "ctc.sqlite3"
    provider_path = tmp_path / "providers.json"
    runtime_path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.setenv("CTC_PROVIDER_CONFIG_PATH", str(provider_path))
    monkeypatch.setenv("CTC_RUNTIME_CONFIG_PATH", str(runtime_path))
    settings = load_settings()
    store = CtcStore(db_path)
    runtime_store = RuntimeConfigStore(runtime_path, settings)
    providers = ProviderStore(provider_path, settings, runtime_store)

    providers.add_provider(
        name="example",
        base_url="https://example.invalid/v1",
        api_key="test-secret",
    )
    runtime_store.update_provider_routing(enabled=False, rules={})

    for path in (store.path, provider_path, runtime_path):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_provider_url_rejects_embedded_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_DB_PATH", str(tmp_path / "ctc.sqlite3"))
    settings = load_settings()
    providers = ProviderStore(tmp_path / "providers.json", settings)

    with pytest.raises(ValueError, match="embedded credentials"):
        providers.add_provider(
            name="unsafe",
            base_url="https://user:password@example.invalid/v1",
            api_key="",
        )


def test_proxy_token_is_consumed_before_upstream_forwarding(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_LOCAL_RUNTIME_DIR", str(tmp_path))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [(b"authorization", b"Bearer proxy-access-token")],
            "client": ("192.0.2.20", 50000),
            "server": ("127.0.0.1", 8787),
            "scheme": "http",
            "query_string": b"",
        }
    )
    request.state.ctc_proxy_auth_consumed = True
    provider = ProviderConfig(
        id="example",
        name="Example",
        provider_type="openai_responses",
        base_url="https://api.example.invalid",
        api_key="upstream-provider-token",
    )

    headers = _forward_headers(request, provider=provider)

    assert headers["authorization"] == "Bearer upstream-provider-token"
    assert "proxy-access-token" not in str(headers)


def test_forwarded_for_is_only_used_from_trusted_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("CTC_LOCAL_RUNTIME_DIR", str(tmp_path))
    settings = load_settings()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/healthz",
            "headers": [(b"x-forwarded-for", b"127.0.0.1")],
            "client": ("192.0.2.20", 50000),
            "server": ("127.0.0.1", 8787),
            "scheme": "http",
            "query_string": b"",
        }
    )

    assert _client_host(request, settings) == "192.0.2.20"
    trusted = replace(settings, trusted_proxy_hosts=frozenset({"192.0.2.20"}))
    assert _client_host(request, trusted) == "127.0.0.1"
