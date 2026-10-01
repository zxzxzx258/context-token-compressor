from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__
from .config import Settings
from .profiles import VALID_PROFILES
from .providers import ProviderStore, check_provider
from .storage import CtcStore

MAX_PAGE_SIZE = 200


class ProfileRuleUpdate(BaseModel):
    client_host: str
    profile: str
    label: str = ""


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    model: str = ""
    provider_type: str = "openai_responses"
    forwarded_user_agent: str = ""
    activate: bool = False


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    provider_type: str | None = None
    forwarded_user_agent: str | None = None
    activate: bool | None = None


class ProviderRoutingUpdate(BaseModel):
    enabled: bool
    rules: dict[str, str] = {}


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _parse_range(range_name: str, since: str | None, until: str | None) -> tuple[str, str]:
    now = datetime.now(UTC)
    try:
        end = datetime.fromisoformat(until) if until else now
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="until must be an ISO-8601 timestamp") from exc
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    if since:
        try:
            start = datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be an ISO-8601 timestamp") from exc
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
    else:
        delta = {
            "1h": timedelta(hours=1),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }.get(range_name, timedelta(hours=24))
        start = end - delta
    if start > end:
        raise HTTPException(status_code=400, detail="since must not be after until")
    return start.isoformat(), end.isoformat()


def _providers_payload(provider_store: ProviderStore) -> dict:
    providers = provider_store.list_providers()
    active = next((provider for provider in providers if provider.get("active")), providers[0] if providers else None)
    routing = provider_store.runtime_config_store.provider_routing()
    return {
        "providers": providers,
        "active_provider": active,
        "provider_routing": routing.public_dict(),
    }


def _restart_process_later() -> None:
    def exit_process() -> None:
        os._exit(75)

    timer = threading.Timer(0.35, exit_process)
    timer.daemon = True
    timer.start()


def create_dashboard_router(
    store: CtcStore,
    provider_store: ProviderStore,
    settings: Settings,
) -> APIRouter:
    router = APIRouter()
    static_dir = Path(__file__).resolve().parent / "static"

    @router.get("/")
    async def dashboard_index():
        return FileResponse(static_dir / "index.html")

    @router.get("/test.html")
    async def dashboard_review_page():
        return FileResponse(static_dir / "test.html")

    @router.get("/api/dashboard")
    async def dashboard_data(
        range: str = "24h",
        since: str | None = None,
        until: str | None = None,
        recent_limit: int = 10,
        recent_offset: int = 0,
        error_limit: int = 10,
        error_offset: int = 0,
        recent_source: str = "",
        error_source: str = "",
        trend_hourly: bool = False,
        trend_recent_limit: int = 50,
    ):
        start, end = _parse_range(range, since, until)
        # The frontend drives the trend mode: hourly mode follows the selected
        # range, request mode picks how many recent rows to include.
        hourly_start, hourly_end = (start, end) if trend_hourly else _parse_range("24h", None, None)
        recent_limit = _clamp_int(recent_limit, 1, MAX_PAGE_SIZE)
        error_limit = _clamp_int(error_limit, 1, MAX_PAGE_SIZE)
        recent_offset = max(0, recent_offset)
        error_offset = max(0, error_offset)
        recent_source = recent_source.strip()
        error_source = error_source.strip()
        # trend_recent_limit <= 0 means the frontend does not need the
        # request-mode trend (hourly mode is active).
        if trend_recent_limit <= 0:
            trend_recent = []
        else:
            trend_recent = store.latest_requests_for_trend(_clamp_int(trend_recent_limit, 1, MAX_PAGE_SIZE))
        return {
            "range": {"since": start, "until": end},
            "summary": store.dashboard_summary(start, end),
            "trend": store.dashboard_trend(start, end),
            "trend_recent": trend_recent,
            "trend_hourly_24h": store.dashboard_trend(hourly_start, hourly_end),
            "traffic_sources": store.traffic_sources(start, end),
            "recent": {
                "source": recent_source,
                "total": store.recent_requests_count(start, end, recent_source or None),
                "limit": recent_limit,
                "offset": recent_offset,
                "rows": store.recent_requests(start, end, recent_limit, recent_offset, recent_source or None),
            },
            "errors": {
                "source": error_source,
                "total": store.error_requests_count(start, end, error_source or None),
                "limit": error_limit,
                "offset": error_offset,
                "rows": store.error_requests(start, end, error_limit, error_offset, error_source or None),
            },
        }

    @router.get("/api/profiles")
    async def profile_sources():
        return {
            "profiles": sorted(VALID_PROFILES),
            "sources": store.profile_sources(),
        }

    @router.post("/api/profiles")
    async def update_profile_rule(update: ProfileRuleUpdate):
        row = store.set_profile_rule(update.client_host, update.profile, update.label)
        return {
            "ok": True,
            "rule": row,
            "sources": store.profile_sources(),
        }

    @router.get("/api/providers")
    async def providers():
        return _providers_payload(provider_store)

    @router.get("/api/runtime-config")
    async def runtime_config():
        return {
            "provider_routing": provider_store.runtime_config_store.provider_routing().public_dict(),
        }

    @router.get("/api/status")
    async def status():
        return {
            "version": __version__,
            "provider": provider_store.active_provider().public_dict(),
            "provider_routing": provider_store.runtime_config_store.provider_routing().public_dict(),
            "database": {"path": str(settings.db_path)},
            "limits": {"max_body_bytes": settings.max_body_bytes},
        }

    @router.patch("/api/runtime-config/provider-routing")
    async def update_provider_routing(update: ProviderRoutingUpdate):
        known_provider_ids = {provider["id"] for provider in provider_store.list_providers()}
        unknown = sorted(
            {provider_id for provider_id in update.rules.values() if provider_id not in known_provider_ids}
        )
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown provider id: {', '.join(unknown)}")
        provider_store.runtime_config_store.update_provider_routing(
            enabled=update.enabled,
            rules=update.rules,
        )
        return _providers_payload(provider_store)

    @router.post("/api/providers")
    async def create_provider(update: ProviderCreate):
        try:
            provider = provider_store.add_provider(
                name=update.name,
                base_url=update.base_url,
                api_key=update.api_key,
                model=update.model,
                provider_type=update.provider_type,
                forwarded_user_agent=update.forwarded_user_agent,
                activate=update.activate,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _providers_payload(provider_store)
        payload["provider"] = provider
        return payload

    @router.patch("/api/providers/{provider_id}")
    async def update_provider(provider_id: str, update: ProviderUpdate):
        try:
            provider = provider_store.update_provider(
                provider_id,
                name=update.name,
                base_url=update.base_url,
                api_key=update.api_key,
                model=update.model,
                provider_type=update.provider_type,
                forwarded_user_agent=update.forwarded_user_agent,
                activate=update.activate,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="provider not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _providers_payload(provider_store)
        payload["provider"] = provider
        return payload

    @router.post("/api/providers/{provider_id}/activate")
    async def activate_provider(provider_id: str):
        try:
            provider = provider_store.set_active(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="provider not found") from exc
        payload = _providers_payload(provider_store)
        payload["provider"] = provider
        return payload

    @router.delete("/api/providers/{provider_id}")
    async def delete_provider(provider_id: str):
        try:
            provider = provider_store.delete_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="provider not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = _providers_payload(provider_store)
        payload["deleted_provider"] = provider
        return payload

    @router.post("/api/providers/{provider_id}/check")
    async def run_provider_check(provider_id: str):
        provider = provider_store.get_provider(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="provider not found")
        status, message = await check_provider(
            provider,
            timeout_seconds=settings.request_timeout_seconds,
            trust_env_proxy=settings.trust_env_proxy,
        )
        checked = provider_store.mark_check_result(provider_id, status, message)
        payload = _providers_payload(provider_store)
        payload["provider"] = checked
        return payload

    @router.post("/api/admin/restart")
    async def restart_ctc():
        if not settings.allow_self_restart:
            raise HTTPException(status_code=403, detail="CTC self restart is disabled")
        _restart_process_later()
        return {
            "ok": True,
            "message": "CTC 正在重启；如果由 systemd 托管，会自动拉起新进程。",
        }

    return router
