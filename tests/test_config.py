from __future__ import annotations

from ctc.config import load_settings


def test_config_paths_follow_db_directory_when_only_db_path_is_explicit(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    db_path = runtime_dir / "ctc.sqlite3"

    monkeypatch.setenv("CTC_DB_PATH", str(db_path))
    monkeypatch.delenv("CTC_LOCAL_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CTC_PROVIDER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("CTC_RUNTIME_CONFIG_PATH", raising=False)

    settings = load_settings()

    assert settings.db_path == db_path
    assert settings.provider_config_path == runtime_dir / "providers.json"
    assert settings.runtime_config_path == runtime_dir / "runtime_config.json"


def test_config_paths_use_local_runtime_dir_when_db_path_is_not_set(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    monkeypatch.setenv("CTC_LOCAL_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.delenv("CTC_DB_PATH", raising=False)
    monkeypatch.delenv("CTC_PROVIDER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("CTC_RUNTIME_CONFIG_PATH", raising=False)

    settings = load_settings()

    assert settings.db_path == runtime_dir / "ctc.sqlite3"
    assert settings.provider_config_path == runtime_dir / "providers.json"
    assert settings.runtime_config_path == runtime_dir / "runtime_config.json"


def test_security_defaults_disable_remote_and_header_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CTC_LOCAL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("CTC_LAN_PROXY_HOST", raising=False)
    monkeypatch.delenv("CTC_LAN_PROXY_PORT", raising=False)
    monkeypatch.delenv("CTC_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("CTC_ALLOW_PROFILE_HEADER", raising=False)
    monkeypatch.delenv("CTC_ALLOW_SELF_RESTART", raising=False)

    settings = load_settings()

    assert settings.lan_proxy_host == ""
    assert settings.lan_proxy_port == 0
    assert settings.dashboard_host == "127.0.0.1"
    assert settings.profile_rules.allow_header_override is False
    assert settings.allow_self_restart is False
