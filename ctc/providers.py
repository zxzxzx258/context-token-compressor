from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings, normalize_base_url
from .permissions import enforce_private_file
from .runtime_config import RuntimeConfigStore

PROVIDER_TYPE_OPENAI_RESPONSES = "openai_responses"
PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE = "deepseek_chat_bridge"
PROVIDER_TYPE_DEEPSEEK_RESPONSES = "deepseek_responses"
VALID_PROVIDER_TYPES = {
    PROVIDER_TYPE_OPENAI_RESPONSES,
    PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE,
    PROVIDER_TYPE_DEEPSEEK_RESPONSES,
}
PROVIDER_TYPE_LABELS = {
    PROVIDER_TYPE_OPENAI_RESPONSES: "OpenAI Responses 兼容",
    PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE: "DeepSeek Chat 桥接",
    PROVIDER_TYPE_DEEPSEEK_RESPONSES: "DeepSeek Responses 直通",
}
DEFAULT_PROVIDER_ID = "env-default"


def _now_label() -> str:
    from .storage import utc_now_iso

    return utc_now_iso()


def _normalize_model_name(raw: str) -> str:
    return raw.strip()


def _provider_id_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        slug = "provider"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def _merge_headers(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    for key, value in extra.items():
        if value:
            merged[key] = value
    return merged


def normalize_provider_type(raw: str | None) -> str:
    value = (raw or PROVIDER_TYPE_OPENAI_RESPONSES).strip()
    if value in VALID_PROVIDER_TYPES:
        return value
    return PROVIDER_TYPE_OPENAI_RESPONSES


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("base_url must not contain a URL fragment")


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    provider_type: str
    base_url: str
    api_key: str = ""
    model: str = ""
    forwarded_user_agent: str = ""
    active: bool = False
    created_at: str = ""
    updated_at: str = ""
    last_checked_at: str = ""
    last_status: str = ""
    last_message: str = ""

    @property
    def upstream_v1_url(self) -> str:
        return f"{self.base_url}/v1"

    def upstream_url_for_path(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        if self.provider_type in {PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE, PROVIDER_TYPE_DEEPSEEK_RESPONSES}:
            # DeepSeek API uses /chat/completions, /responses, /models etc --
            # no /v1 prefix
            stripped = normalized[3:] if normalized.startswith("/v1/") else normalized
            return f"{self.base_url}{stripped}"
        if normalized.startswith("/v1/") or normalized == "/v1":
            return f"{self.base_url}{normalized}"
        return f"{self.upstream_v1_url}{normalized}"

    def authorization_header(self, fallback: str | None = None) -> str:
        if self.api_key:
            return f"Bearer {self.api_key}"
        return fallback or ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider_type": self.provider_type,
            "provider_type_label": PROVIDER_TYPE_LABELS.get(self.provider_type, self.provider_type),
            "base_url": self.upstream_v1_url,
            "model": self.model,
            "forwarded_user_agent": self.forwarded_user_agent,
            "active": self.active,
            "has_api_key": bool(self.api_key),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
            "last_status": self.last_status,
            "last_message": self.last_message,
        }

    def private_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider_type": self.provider_type,
            "base_url": self.upstream_v1_url,
            "api_key": self.api_key,
            "model": self.model,
            "forwarded_user_agent": self.forwarded_user_agent,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
            "last_status": self.last_status,
            "last_message": self.last_message,
        }


def _provider_from_dict(raw: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        id=str(raw.get("id") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        provider_type=normalize_provider_type(str(raw.get("provider_type") or PROVIDER_TYPE_OPENAI_RESPONSES)),
        base_url=normalize_base_url(str(raw.get("base_url") or "")),
        api_key=str(raw.get("api_key") or ""),
        model=_normalize_model_name(str(raw.get("model") or "")),
        forwarded_user_agent=str(raw.get("forwarded_user_agent") or "").strip(),
        active=bool(raw.get("active")),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        last_checked_at=str(raw.get("last_checked_at") or ""),
        last_status=str(raw.get("last_status") or ""),
        last_message=str(raw.get("last_message") or ""),
    )


class ProviderStore:
    def __init__(
        self,
        path: Path,
        settings: Settings,
        runtime_config_store: RuntimeConfigStore | None = None,
    ):
        self.path = Path(path)
        self.settings = settings
        self.runtime_config_store = runtime_config_store or RuntimeConfigStore(
            settings.runtime_config_path,
            settings,
        )
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._providers: list[ProviderConfig] | None = None
        self._loaded_mtime_ns: int | None = None

    def _default_provider(self) -> ProviderConfig:
        now = _now_label()
        return ProviderConfig(
            id=DEFAULT_PROVIDER_ID,
            name="默认环境上游",
            provider_type=PROVIDER_TYPE_OPENAI_RESPONSES,
            base_url=normalize_base_url(self.settings.upstream_base_url),
            api_key=self.settings.upstream_api_key,
            model="",
            forwarded_user_agent=self.settings.forwarded_user_agent,
            active=True,
            created_at=now,
            updated_at=now,
            last_status="unknown",
            last_message="来自环境变量 CTC_UPSTREAM_BASE_URL",
        )

    def _load(self) -> list[ProviderConfig]:
        with self._lock:
            current_mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
            if self._providers is not None and current_mtime == self._loaded_mtime_ns:
                return self._providers
            if not self.path.exists():
                self._providers = [self._default_provider()]
                self._loaded_mtime_ns = None
                return self._providers
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._providers = [self._default_provider()]
                self._loaded_mtime_ns = current_mtime
                return self._providers
            providers = [_provider_from_dict(item) for item in raw.get("providers", []) if isinstance(item, dict)]
            providers = [
                item
                for item in providers
                if item.id and item.name and item.base_url and item.provider_type in VALID_PROVIDER_TYPES
            ]
            if not providers:
                providers = [self._default_provider()]
            if not any(item.active for item in providers):
                providers = [self._replace(providers[0], active=True), *providers[1:]]
            if sum(1 for item in providers if item.active) > 1:
                active_seen = False
                normalized: list[ProviderConfig] = []
                for item in providers:
                    if item.active and not active_seen:
                        active_seen = True
                        normalized.append(item)
                    else:
                        normalized.append(self._replace(item, active=False))
                providers = normalized
            self._providers = providers
            self._loaded_mtime_ns = current_mtime
            return providers

    def _save(self, providers: list[ProviderConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        data = {
            "version": 1,
            "providers": [provider.private_dict() for provider in providers],
        }
        with self._lock:
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            enforce_private_file(tmp_path)
            tmp_path.replace(self.path)
            enforce_private_file(self.path)
            self._providers = providers
            self._loaded_mtime_ns = self.path.stat().st_mtime_ns

    @staticmethod
    def _replace(provider: ProviderConfig, **updates: Any) -> ProviderConfig:
        values = provider.private_dict()
        values.update(updates)
        return _provider_from_dict(values)

    def list_providers(self) -> list[dict[str, Any]]:
        return [provider.public_dict() for provider in self._load()]

    def active_provider(self) -> ProviderConfig:
        providers = self._load()
        return next((item for item in providers if item.active), providers[0])

    def first_openai_responses_provider(self) -> ProviderConfig | None:
        return next((item for item in self._load() if item.provider_type == PROVIDER_TYPE_OPENAI_RESPONSES), None)

    def provider_for_host(self, client_host: str) -> ProviderConfig:
        providers = self._load()
        by_id = {item.id: item for item in providers}
        active = next((item for item in providers if item.active), providers[0])
        routing = self.runtime_config_store.provider_routing()
        if not routing.enabled:
            return active
        provider_id = routing.rules.get(client_host) or routing.rules.get("*")
        if not provider_id:
            return active
        return by_id.get(provider_id) or active

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return next((item for item in self._load() if item.id == provider_id), None)

    def add_provider(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str = "",
        provider_type: str = PROVIDER_TYPE_OPENAI_RESPONSES,
        forwarded_user_agent: str = "",
        activate: bool = False,
    ) -> dict[str, Any]:
        normalized_name = name.strip()
        normalized_base = normalize_base_url(base_url)
        normalized_type = normalize_provider_type(provider_type)
        if not normalized_name:
            raise ValueError("name is required")
        _validate_base_url(normalized_base)
        now = _now_label()
        provider = ProviderConfig(
            id=_provider_id_from_name(normalized_name),
            name=normalized_name,
            provider_type=normalized_type,
            base_url=normalized_base,
            api_key=api_key.strip(),
            model=_normalize_model_name(model),
            forwarded_user_agent=forwarded_user_agent.strip(),
            active=False,
            created_at=now,
            updated_at=now,
            last_status="unknown",
            last_message="尚未检测",
        )
        # Hold the store lock across load-modify-save; RLock is reentrant
        # so the nested _load/_save acquisitions keep working.
        with self._lock:
            providers = self._load()
            if activate:
                providers = [self._replace(item, active=False) for item in providers]
                provider = self._replace(provider, active=True)
            providers.append(provider)
            self._save(providers)
            return provider.public_dict()

    def update_provider(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider_type: str | None = None,
        forwarded_user_agent: str | None = None,
        activate: bool | None = None,
    ) -> dict[str, Any]:
        # Hold the store lock across load-modify-save; RLock is reentrant
        # so the nested _load/_save acquisitions keep working.
        with self._lock:
            providers = self._load()
            now = _now_label()
            updated: list[ProviderConfig] = []
            found: ProviderConfig | None = None
            for item in providers:
                if item.id != provider_id:
                    updated.append(item)
                    continue
                values: dict[str, Any] = {"updated_at": now}
                if name is not None:
                    if not name.strip():
                        raise ValueError("name is required")
                    values["name"] = name.strip()
                if base_url is not None:
                    normalized_base = normalize_base_url(base_url)
                    _validate_base_url(normalized_base)
                    values["base_url"] = normalized_base
                if api_key is not None:
                    values["api_key"] = api_key.strip()
                if model is not None:
                    values["model"] = _normalize_model_name(model)
                if provider_type is not None:
                    values["provider_type"] = normalize_provider_type(provider_type)
                if forwarded_user_agent is not None:
                    values["forwarded_user_agent"] = forwarded_user_agent.strip()
                found = self._replace(item, **values)
                updated.append(found)
            if found is None:
                raise KeyError(provider_id)
            if activate is True:
                updated = [self._replace(item, active=item.id == provider_id) for item in updated]
                found = next(item for item in updated if item.id == provider_id)
            elif activate is False:
                if found.active and len(updated) > 1:
                    for index, item in enumerate(updated):
                        if item.id != provider_id:
                            updated[index] = self._replace(item, active=True)
                            break
                updated = [self._replace(item, active=False) if item.id == provider_id else item for item in updated]
                found = next(item for item in updated if item.id == provider_id)
            if not any(item.active for item in updated):
                updated[0] = self._replace(updated[0], active=True)
            self._save(updated)
            return found.public_dict()

    def delete_provider(self, provider_id: str) -> dict[str, Any]:
        # Hold the store lock across load-modify-save; RLock is reentrant
        # so the nested _load/_save acquisitions keep working.
        with self._lock:
            providers = self._load()
            if len(providers) <= 1:
                raise ValueError("cannot delete the last provider")
            target = next((item for item in providers if item.id == provider_id), None)
            if target is None:
                raise KeyError(provider_id)
            remaining = [item for item in providers if item.id != provider_id]
            if target.active and remaining:
                remaining[0] = self._replace(remaining[0], active=True)
            self._save(remaining)
            return target.public_dict()

    def set_active(self, provider_id: str) -> dict[str, Any]:
        # Hold the store lock across load-modify-save; RLock is reentrant
        # so the nested _load/_save acquisitions keep working.
        with self._lock:
            providers = self._load()
            if not any(item.id == provider_id for item in providers):
                raise KeyError(provider_id)
            updated = [self._replace(item, active=item.id == provider_id) for item in providers]
            self._save(updated)
            return next(item.public_dict() for item in updated if item.id == provider_id)

    def mark_check_result(self, provider_id: str, status: str, message: str) -> dict[str, Any]:
        # Hold the store lock across load-modify-save; RLock is reentrant
        # so the nested _load/_save acquisitions keep working.
        with self._lock:
            providers = self._load()
            updated: list[ProviderConfig] = []
            found: ProviderConfig | None = None
            for item in providers:
                if item.id == provider_id:
                    found = self._replace(
                        item,
                        last_checked_at=_now_label(),
                        last_status=status,
                        last_message=message[:300],
                        updated_at=_now_label(),
                    )
                    updated.append(found)
                else:
                    updated.append(item)
            if found is None:
                raise KeyError(provider_id)
            self._save(updated)
            return found.public_dict()


async def check_provider(provider: ProviderConfig, *, timeout_seconds: float, trust_env_proxy: bool) -> tuple[str, str]:
    headers: dict[str, str] = {"accept": "application/json"}
    auth = provider.authorization_header()
    if auth:
        headers["authorization"] = auth
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(min(max(timeout_seconds, 5), 30)),
            follow_redirects=False,
            trust_env=trust_env_proxy,
        ) as client:
            models_url = provider.upstream_url_for_path("/v1/models")
            response = await client.get(models_url, headers=headers)
            if response.status_code < 400:
                if provider.provider_type == PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE and provider.model:
                    test_response = await client.post(
                        provider.upstream_url_for_path("/v1/chat/completions"),
                        headers=_merge_headers(headers, {"content-type": "application/json"}),
                        json={
                            "model": provider.model,
                            "messages": [{"role": "user", "content": "CTC provider check"}],
                            "stream": False,
                            "max_tokens": 8,
                        },
                    )
                    if test_response.status_code < 400:
                        return "ok", f"/v1/models HTTP {response.status_code}; /v1/chat/completions HTTP {test_response.status_code}"
                    if test_response.status_code in {401, 403}:
                        return "auth_error", f"/v1/chat/completions HTTP {test_response.status_code}，请检查 API Key"
                    return "error", f"/v1/chat/completions HTTP {test_response.status_code}"
                return "ok", f"/v1/models HTTP {response.status_code}"
            if response.status_code in {401, 403}:
                return "auth_error", f"/v1/models HTTP {response.status_code}，请检查 API Key"
            if response.status_code == 404 and provider.model and provider.provider_type == PROVIDER_TYPE_OPENAI_RESPONSES:
                test_response = await client.post(
                    provider.upstream_url_for_path("/v1/responses"),
                    headers=_merge_headers(headers, {"content-type": "application/json"}),
                    json={"model": provider.model, "input": "CTC provider check", "max_output_tokens": 16},
                )
                if test_response.status_code < 400:
                    return "ok", f"/v1/responses HTTP {test_response.status_code}"
                return "error", f"/v1/responses HTTP {test_response.status_code}"
            if response.status_code == 404 and provider.model and provider.provider_type == PROVIDER_TYPE_DEEPSEEK_CHAT_BRIDGE:
                test_response = await client.post(
                    provider.upstream_url_for_path("/v1/chat/completions"),
                    headers=_merge_headers(headers, {"content-type": "application/json"}),
                    json={
                        "model": provider.model,
                        "messages": [{"role": "user", "content": "CTC provider check"}],
                        "stream": False,
                        "max_tokens": 8,
                    },
                )
                if test_response.status_code < 400:
                    return "ok", f"/v1/chat/completions HTTP {test_response.status_code}"
                if test_response.status_code in {401, 403}:
                    return "auth_error", f"/v1/chat/completions HTTP {test_response.status_code}，请检查 API Key"
                return "error", f"/v1/chat/completions HTTP {test_response.status_code}"
            return "error", f"/v1/models HTTP {response.status_code}"
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"
