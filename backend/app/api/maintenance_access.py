"""Site-bound authorization and response headers for the maintenance interface."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Request

from app.services.maintenance_sessions import (
    ATTEMPT_SECONDS,
    OwnerSessions,
    SessionDenied,
    SessionRateLimited,
    SessionStoreError,
)

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; "
        "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


def owner_sessions(request: Request) -> OwnerSessions:
    try:
        config = json.loads((request.app.state.maintenance_root / "owner-site.json").read_bytes())
        project = os.environ["GIFTLEDGER_PROJECT"]
        origin = config["origin"]
        instance = config["instance_id"]
        if (
            config["project"] != project
            or str(UUID(instance)) != instance
            or request.app.state.settings.trusted_origin_list != (origin,)
        ):
            raise ValueError("owner site mismatch")
        return OwnerSessions(
            Path(request.app.state.maintenance_owner_dir),
            project=project,
            instance_id=instance,
            origin=origin,
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError, SessionStoreError) as exc:
        raise HTTPException(503, "本站维护身份尚未就绪。", headers=HEADERS) from exc


def check_owner_request(request: Request, content_type: str = "application/json") -> None:
    if request.query_params:
        raise HTTPException(400, "请从本站维护页访问。", headers=HEADERS)
    if request.method not in {"GET", "HEAD"} and (
        request.headers.get("origin") not in request.app.state.settings.trusted_origin_list
        or request.headers.get("content-type", "").partition(";")[0].lower() != content_type
        or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
    ):
        raise HTTPException(403, "请从本站维护页提交。", headers=HEADERS)


@contextmanager
def session_errors():
    try:
        yield
    except SessionRateLimited as exc:
        raise HTTPException(
            429,
            "验证尝试过多，请稍后再试。",
            headers={**HEADERS, "Retry-After": str(ATTEMPT_SECONDS)},
        ) from exc
    except SessionDenied as exc:
        raise HTTPException(403, "验证未通过，请核对本站凭据后再试。", headers=HEADERS) from exc
    except SessionStoreError as exc:
        raise HTTPException(503, "暂时无法核对本站站主身份。", headers=HEADERS) from exc


def owner_bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "请先登录本站维护页。", headers=HEADERS)
    return token


def require_owner(request: Request) -> OwnerSessions:
    return require_owner_for_content(request, "application/json")


def require_owner_for_content(request: Request, content_type: str) -> OwnerSessions:
    check_owner_request(request, content_type)
    token = owner_bearer(request)
    sessions = owner_sessions(request)
    with session_errors():
        sessions.require(
            token, request_origin=request.headers.get("origin"), mutation=request.method != "GET"
        )
    return sessions
