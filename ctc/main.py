from __future__ import annotations

import argparse
import threading

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import load_settings
from .dashboard import create_dashboard_router
from .providers import ProviderStore
from .proxy import create_proxy_router
from .runtime_config import RuntimeConfigStore
from .security import authentication_middleware, is_loopback_host, validate_startup_settings
from .storage import CtcStore


def create_proxy_app(*, require_proxy_token: bool | None = None) -> FastAPI:
    settings = load_settings()
    token_required = not is_loopback_host(settings.proxy_host) if require_proxy_token is None else require_proxy_token
    if token_required and not settings.proxy_token:
        raise ValueError("CTC_PROXY_TOKEN is required for a non-loopback proxy listener")
    store = CtcStore(settings.db_path)
    runtime_config_store = RuntimeConfigStore(settings.runtime_config_path, settings)
    provider_store = ProviderStore(settings.provider_config_path, settings, runtime_config_store)
    app = FastAPI(title="Context Token Compressor Proxy", version=__version__)
    app.state.settings = settings
    app.state.store = store
    app.state.provider_store = provider_store

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "component": "ctc-proxy",
            "version": __version__,
        }

    if token_required:
        app.middleware("http")(
            authentication_middleware(
                settings.proxy_token,
                component="CTC proxy",
                protected=lambda request: request.url.path.startswith("/v1"),
                consume_authorization=True,
            )
        )
    app.include_router(create_proxy_router(settings, store, provider_store))
    return app


def create_dashboard_app(
    store: CtcStore | None = None,
    provider_store: ProviderStore | None = None,
) -> FastAPI:
    settings = load_settings()
    if not settings.admin_token:
        raise ValueError("CTC_ADMIN_TOKEN is required for the dashboard")
    active_store = store or CtcStore(settings.db_path)
    runtime_config_store = RuntimeConfigStore(settings.runtime_config_path, settings)
    active_provider_store = provider_store or ProviderStore(
        settings.provider_config_path,
        settings,
        runtime_config_store,
    )
    app = FastAPI(title="Context Token Compressor Dashboard", version=__version__)
    app.state.settings = settings
    app.state.store = active_store
    app.state.provider_store = active_provider_store

    @app.get("/healthz")
    async def healthz():
        return JSONResponse(
            {
                "status": "ok",
                "component": "ctc-dashboard",
                "version": __version__,
            }
        )

    app.middleware("http")(
        authentication_middleware(
            settings.admin_token,
            component="CTC dashboard",
            protected=lambda request: request.url.path.startswith("/api/"),
            dashboard_headers=True,
        )
    )
    app.include_router(create_dashboard_router(active_store, active_provider_store, settings))
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Context Token Compressor proxy and dashboard")
    parser.add_argument("--version", action="version", version=f"Context Token Compressor {__version__}")
    parser.add_argument("--check-config", action="store_true", help="validate configuration without starting listeners")
    args = parser.parse_args(argv)
    settings = load_settings()
    validate_startup_settings(settings)
    if args.check_config:
        print("Context Token Compressor configuration is valid")
        return
    store = CtcStore(settings.db_path)
    runtime_config_store = RuntimeConfigStore(settings.runtime_config_path, settings)
    provider_store = ProviderStore(settings.provider_config_path, settings, runtime_config_store)
    proxy_app = create_proxy_app()
    lan_proxy_app = (
        create_proxy_app(require_proxy_token=True)
        if settings.lan_proxy_host and settings.lan_proxy_port
        else None
    )
    dashboard_app = create_dashboard_app(store, provider_store)

    dashboard_config = uvicorn.Config(
        dashboard_app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
        proxy_headers=False,
    )
    dashboard_server = uvicorn.Server(dashboard_config)
    dashboard_thread = threading.Thread(target=dashboard_server.run, name="ctc-dashboard", daemon=True)
    dashboard_thread.start()

    if lan_proxy_app is not None:
        lan_proxy_config = uvicorn.Config(
            lan_proxy_app,
            host=settings.lan_proxy_host,
            port=settings.lan_proxy_port,
            log_level="info",
            proxy_headers=False,
        )
        lan_proxy_server = uvicorn.Server(lan_proxy_config)
        lan_proxy_thread = threading.Thread(target=lan_proxy_server.run, name="ctc-lan-proxy", daemon=True)
        lan_proxy_thread.start()

    proxy_config = uvicorn.Config(
        proxy_app,
        host=settings.proxy_host,
        port=settings.proxy_port,
        log_level="info",
        proxy_headers=False,
    )
    proxy_server = uvicorn.Server(proxy_config)
    proxy_server.run()


if __name__ == "__main__":
    main()
