from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import Settings

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


def is_loopback_host(host: str) -> bool:
    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_startup_settings(settings: Settings) -> None:
    if not settings.admin_token:
        raise ValueError("CTC_ADMIN_TOKEN is required for the dashboard")
    if not is_loopback_host(settings.proxy_host) and not settings.proxy_token:
        raise ValueError("CTC_PROXY_TOKEN is required for a non-loopback proxy listener")
    if settings.lan_proxy_host and settings.lan_proxy_port and not settings.proxy_token:
        raise ValueError("CTC_PROXY_TOKEN is required when the LAN listener is enabled")


def bearer_token(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def token_matches(candidate: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def unauthorized(component: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": f"{component} authentication required", "type": "authentication_error"}},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def apply_security_headers(response: Response, *, dashboard: bool = False) -> Response:
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if dashboard:
        response.headers.setdefault(
            "content-security-policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
    return response


def authentication_middleware(
    expected_token: str,
    *,
    component: str,
    protected: Callable[[Request], bool],
    consume_authorization: bool = False,
    dashboard_headers: bool = False,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if protected(request):
            if not token_matches(bearer_token(request), expected_token):
                return apply_security_headers(unauthorized(component), dashboard=dashboard_headers)
            if consume_authorization:
                request.state.ctc_proxy_auth_consumed = True
        response = await call_next(request)
        return apply_security_headers(response, dashboard=dashboard_headers)

    return middleware
