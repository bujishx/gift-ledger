import hmac
import secrets

from fastapi import Request, Response

from app.core.config import Settings
from app.core.security import JWT_LIFETIME

AUTH_COOKIE_NAME = "gift_ledger_session"
CSRF_COOKIE_NAME = "gift_ledger_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def entry_origin(request: Request) -> str:
    # The web container forwards the verified browser-facing Host, including its port.
    # The internal HTTP hop to Uvicorn does not change the browser's HTTPS origin.
    authority = request.headers.get("host", "").lower().removesuffix(":443")
    return f"https://{authority}"


def business_origin_is_trusted(request: Request, settings: Settings, origin: str | None) -> bool:
    if origin is None:
        return False
    local = settings.site_origin
    if local is None:
        return origin.rstrip("/") in settings.trusted_origin_list
    return origin == entry_origin(request) and origin in {local, settings.reverse_proxy_origin}


def csrf_token_is_valid(request: Request) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    return bool(cookie_token and header_token and hmac.compare_digest(cookie_token, header_token))


def set_auth_cookies(response: Response, access_token: str, settings: Settings) -> None:
    max_age = int(JWT_LIFETIME.total_seconds())
    cookie_options = {
        "secure": settings.auth_cookie_secure,
        "samesite": "strict",
        "path": "/",
        "max_age": max_age,
    }
    response.set_cookie(
        AUTH_COOKIE_NAME,
        access_token,
        httponly=True,
        **cookie_options,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        secrets.token_urlsafe(32),
        httponly=False,
        **cookie_options,
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    shared_options = {
        "secure": settings.auth_cookie_secure,
        "samesite": "strict",
        "path": "/",
    }
    response.delete_cookie(AUTH_COOKIE_NAME, httponly=True, **shared_options)
    response.delete_cookie(CSRF_COOKIE_NAME, httponly=False, **shared_options)
