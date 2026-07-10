from __future__ import annotations

from dataclasses import dataclass

PROFILE_SAFE = "safe"
PROFILE_DEV = "dev"
PROFILE_OFF = "off"
VALID_PROFILES = {PROFILE_SAFE, PROFILE_DEV, PROFILE_OFF}


@dataclass(frozen=True)
class ProfileRules:
    default: str
    by_host: dict[str, str]
    allow_header_override: bool


def normalize_profile(value: str | None, default: str = PROFILE_SAFE) -> str:
    if not value:
        return default
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "conservative": PROFILE_SAFE,
        "development": PROFILE_DEV,
        "aggressive": PROFILE_DEV,
        "dev_aggressive": PROFILE_DEV,
        "dev_ultra": PROFILE_DEV,
        "none": PROFILE_OFF,
        "passthrough": PROFILE_OFF,
        "raw": PROFILE_OFF,
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in VALID_PROFILES else default


def parse_profile_rules(raw: str | None) -> dict[str, str]:
    rules: dict[str, str] = {}
    if not raw:
        return rules
    for part in raw.replace(",", ";").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        host, profile = item.split("=", 1)
        host = host.strip()
        if not host:
            continue
        rules[host] = normalize_profile(profile)
    return rules


def resolve_profile(client_host: str, header_profile: str | None, rules: ProfileRules) -> str:
    if rules.allow_header_override and header_profile:
        return normalize_profile(header_profile, rules.default)
    if client_host in rules.by_host:
        return rules.by_host[client_host]
    if "*" in rules.by_host:
        return rules.by_host["*"]
    return rules.default


def resolve_rule_fallback(client_host: str, rules: ProfileRules, default: str) -> str:
    if client_host in rules.by_host:
        return rules.by_host[client_host]
    if "*" in rules.by_host:
        return rules.by_host["*"]
    return normalize_profile(rules.default, default)
