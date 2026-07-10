from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, parse_host_value_rules
from .permissions import enforce_private_file


@dataclass(frozen=True)
class ProviderRoutingConfig:
    enabled: bool
    rules: dict[str, str]
    source: str

    @property
    def rule_count(self) -> int:
        return len(self.rules)

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rules": self.rules,
            "rule_count": self.rule_count,
            "source": self.source,
        }


class RuntimeConfigStore:
    def __init__(self, path: Path, settings: Settings) -> None:
        self.path = Path(path)
        self.settings = settings
        self._lock = threading.RLock()
        self._routing: ProviderRoutingConfig | None = None
        self._loaded_mtime_ns: int | None = None

    def _env_routing(self) -> ProviderRoutingConfig:
        return ProviderRoutingConfig(
            enabled=False,
            rules={},
            source="default",
        )

    def _load(self) -> ProviderRoutingConfig:
        current_mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
        if self._routing is not None and current_mtime == self._loaded_mtime_ns:
            return self._routing
        with self._lock:
            current_mtime = self.path.stat().st_mtime_ns if self.path.exists() else None
            if self._routing is not None and current_mtime == self._loaded_mtime_ns:
                return self._routing
            if current_mtime is None:
                self._routing = self._env_routing()
                self._loaded_mtime_ns = None
                return self._routing
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._routing = self._env_routing()
                self._loaded_mtime_ns = current_mtime
                return self._routing
            routing = raw.get("provider_routing", {}) if isinstance(raw, dict) else {}
            raw_rules = routing.get("rules", {}) if isinstance(routing, dict) else {}
            if isinstance(raw_rules, dict):
                rules = raw_rules
            else:
                rules = parse_host_value_rules(str(raw_rules))
            normalized_rules = {
                str(host).strip(): str(provider_id).strip()
                for host, provider_id in rules.items()
                if str(host).strip() and str(provider_id).strip()
            }
            self._routing = ProviderRoutingConfig(
                enabled=bool(routing.get("enabled", False)) if isinstance(routing, dict) else False,
                rules=normalized_rules,
                source="runtime_config",
            )
            self._loaded_mtime_ns = current_mtime
            return self._routing

    def provider_routing(self) -> ProviderRoutingConfig:
        return self._load()

    def update_provider_routing(self, *, enabled: bool, rules: dict[str, str]) -> ProviderRoutingConfig:
        normalized_rules = {
            str(host).strip(): str(provider_id).strip()
            for host, provider_id in rules.items()
            if str(host).strip() and str(provider_id).strip()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        data = {"version": 1, "provider_routing": {"enabled": bool(enabled), "rules": normalized_rules}}
        with self._lock:
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            enforce_private_file(tmp_path)
            tmp_path.replace(self.path)
            enforce_private_file(self.path)
            self._loaded_mtime_ns = self.path.stat().st_mtime_ns
            self._routing = ProviderRoutingConfig(
                enabled=bool(enabled),
                rules=normalized_rules,
                source="runtime_config",
            )
            return self._routing
