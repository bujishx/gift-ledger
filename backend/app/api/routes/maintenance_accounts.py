"""Account assistance scoped to a session issued for this site's owner."""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DatabaseSessionDependency
from app.api.maintenance_access import HEADERS, require_owner
from app.api.routes.site_maintenance import queue_status
from app.core.security import PasswordPolicyError, UsernamePolicyError
from app.services import maintenance_overview
from app.services.maintenance_sessions import OwnerSessions
from app.services.user_management import (
    AccountHasDataError,
    UserNotFoundError,
    delete_empty_account,
    list_business_accounts,
    reset_user_password,
    set_user_active,
)

router = APIRouter(
    prefix="/api/v1/maintenance", include_in_schema=False, dependencies=[Depends(require_owner)]
)
Owner = Annotated[OwnerSessions, Depends(require_owner)]


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=4, max_length=30)
    new_password: SecretStr


class AccountTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(ge=1, le=2147483647)
    username: str = Field(min_length=4, max_length=30)


class AccountActivation(AccountTarget):
    is_active: bool


@router.get("/session")
def current_owner(session: Owner):
    return JSONResponse(
        {
            "authenticated": True,
            "role": "site_owner",
            "project": session.binding[0],
            "instance": session.binding[1],
        },
        headers=HEADERS,
    )


@router.get("/accounts")
def accounts(db: DatabaseSessionDependency):
    return JSONResponse(
        {"accounts": [asdict(account) for account in list_business_accounts(db)]}, headers=HEADERS
    )


@router.get("/overview")
def overview(request: Request, db: DatabaseSessionDependency, owner: Owner):
    root = request.app.state.maintenance_root
    value = maintenance_overview.overview(db, owner, root)
    try:
        maintenance_overview.include_web_task(value, root, queue_status(root))
    except HTTPException:
        pass
    return JSONResponse(value, headers=HEADERS)


@router.get("/usage/{after}")
@router.get("/usage/{state}/{after}")
def usage(
    db: DatabaseSessionDependency,
    after: Annotated[int, Path(ge=0, le=2147483647)],
    owner: Owner,
    state: Literal["active", "inactive"] | None = None,
):
    try:
        active = None if state is None else state == "active"
        value = maintenance_overview.usage(db, owner, after, active=active)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "暂时无法读取本站用户使用量。", headers=HEADERS) from exc
    return JSONResponse(value, headers=HEADERS)


@router.post("/accounts/set-active")
def set_account_active(body: AccountActivation, db: DatabaseSessionDependency):
    try:
        user = set_user_active(db, body.username, is_active=body.is_active, expected_id=body.id)
    except (UsernamePolicyError, UserNotFoundError) as exc:
        raise HTTPException(404, "找不到本站的这个账号，请刷新列表。", headers=HEADERS) from exc
    return JSONResponse({"id": user.id, "is_active": bool(user.is_active)}, headers=HEADERS)


@router.post("/accounts/delete-empty")
def delete_account(body: AccountTarget, db: DatabaseSessionDependency):
    try:
        delete_empty_account(db, body.username, body.id)
    except (UsernamePolicyError, UserNotFoundError) as exc:
        raise HTTPException(404, "找不到本站的这个账号，请刷新列表。", headers=HEADERS) from exc
    except AccountHasDataError as exc:
        raise HTTPException(409, "已有本人档案或业务资料，只能停用账号。", headers=HEADERS) from exc
    return JSONResponse({"id": body.id, "deleted": True}, headers=HEADERS)


@router.post("/accounts/reset-password")
def reset_password(body: PasswordReset, db: DatabaseSessionDependency):
    try:
        user = reset_user_password(db, body.username, body.new_password.get_secret_value())
    except (PasswordPolicyError, UsernamePolicyError) as exc:
        raise HTTPException(422, str(exc), headers=HEADERS) from exc
    except UserNotFoundError as exc:
        raise HTTPException(404, "找不到本站的这个账号。", headers=HEADERS) from exc
    return JSONResponse({"username": user.username, "reset": True}, headers=HEADERS)
