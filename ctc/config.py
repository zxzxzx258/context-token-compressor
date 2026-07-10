from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .profiles import PROFILE_SAFE, ProfileRules, normalize_profile, parse_profile_rules

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_LOCAL_RUNTIME_DIR = WORKSPACE_ROOT / "release" / "runtime_state"
DEFAULT_REMOTE_RUNTIME_DIR = Path("/var/lib/ctc")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if value.endswith("/v1"):
        return value[:-3].rstrip("/")
    return value


def parse_host_value_rules(raw: str | None) -> dict[str, str]:
    rules: dict[str, str] = {}
    if not raw:
        return rules
    for part in raw.replace(",", ";").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        host, value = item.split("=", 1)
        host = host.strip()
        value = value.strip()
        if host and value:
            rules[host] = value
    return rules


def parse_host_set(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.replace(";", ",").split(",") if item.strip())


def _default_runtime_dir() -> Path:
    if DEFAULT_LOCAL_RUNTIME_DIR.exists():
        return DEFAULT_LOCAL_RUNTIME_DIR
    if str(PROJECT_ROOT).startswith("/"):
        return DEFAULT_REMOTE_RUNTIME_DIR
    return PROJECT_ROOT


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    proxy_host: str
    proxy_port: int
    lan_proxy_host: str
    lan_proxy_port: int
    dashboard_host: str
    dashboard_port: int
    db_path: Path
    compress_threshold_chars: int
    compress_target_chars: int
    request_timeout_seconds: float
    stream_timeout_seconds: float
    trust_env_proxy: bool
    profile_rules: ProfileRules
    provider_config_path: Path
    runtime_config_path: Path
    upstream_api_key: str
    forwarded_user_agent: str
    allow_self_restart: bool
    admin_token: str
    proxy_token: str
    trusted_proxy_hosts: frozenset[str]
    max_body_bytes: int

    @property
    def upstream_v1_url(self) -> str:
        return f"{self.upstream_base_url}/v1"

    def upstream_url_for_path(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        if normalized.startswith("/v1/") or normalized == "/v1":
            return f"{self.upstream_base_url}{normalized}"
        return f"{self.upstream_v1_url}{normalized}"


def load_settings() -> Settings:
    runtime_dir = Path(os.getenv("CTC_LOCAL_RUNTIME_DIR", str(_default_runtime_dir())))
    db_path = Path(os.getenv("CTC_DB_PATH", str(runtime_dir / "ctc.sqlite3")))
    config_dir = db_path.parent if os.getenv("CTC_DB_PATH") else runtime_dir
    provider_config_path = Path(
        os.getenv("CTC_PROVIDER_CONFIG_PATH", str(config_dir / "providers.json"))
    )
    runtime_config_path = Path(
        os.getenv("CTC_RUNTIME_CONFIG_PATH", str(config_dir / "runtime_config.json"))
    )
    return Settings(
        upstream_base_url=normalize_base_url(
            os.getenv("CTC_UPSTREAM_BASE_URL", "")
        ),
        proxy_host=os.getenv("CTC_PROXY_HOST", "127.0.0.1"),
        proxy_port=_env_int("CTC_PROXY_PORT", 8787),
        lan_proxy_host=os.getenv("CTC_LAN_PROXY_HOST", ""),
        lan_proxy_port=_env_int("CTC_LAN_PROXY_PORT", 0),
        dashboard_host=os.getenv("CTC_DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_env_int("CTC_DASHBOARD_PORT", 8788),
        db_path=db_path,
        compress_threshold_chars=_env_int("CTC_COMPRESS_THRESHOLD_CHARS", 2000),
        compress_target_chars=_env_int("CTC_COMPRESS_TARGET_CHARS", 10000),
        request_timeout_seconds=float(os.getenv("CTC_REQUEST_TIMEOUT_SECONDS", "240")),
        stream_timeout_seconds=float(os.getenv("CTC_STREAM_TIMEOUT_SECONDS", "600")),
        trust_env_proxy=_env_bool("CTC_TRUST_ENV_PROXY", False),
        profile_rules=ProfileRules(
            default=normalize_profile(os.getenv("CTC_DEFAULT_PROFILE"), PROFILE_SAFE),
            by_host=parse_profile_rules(os.getenv("CTC_PROFILE_RULES")),
            allow_header_override=_env_bool("CTC_ALLOW_PROFILE_HEADER", False),
        ),
        provider_config_path=provider_config_path,
        runtime_config_path=runtime_config_path,
        upstream_api_key=os.getenv("CTC_UPSTREAM_API_KEY", ""),
        forwarded_user_agent=os.getenv("CTC_FORWARDED_USER_AGENT", "").strip(),
        allow_self_restart=_env_bool("CTC_ALLOW_SELF_RESTART", False),
        admin_token=os.getenv("CTC_ADMIN_TOKEN", "").strip(),
        proxy_token=os.getenv("CTC_PROXY_TOKEN", "").strip(),
        trusted_proxy_hosts=parse_host_set(os.getenv("CTC_TRUSTED_PROXY_HOSTS")),
        max_body_bytes=max(1024, _env_int("CTC_MAX_BODY_BYTES", 16 * 1024 * 1024)),
    )


_normalize_base_url = normalize_base_url
