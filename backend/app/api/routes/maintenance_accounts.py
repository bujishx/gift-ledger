"""Account assistance scoped to a session issued for this site's owner."""

import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.dependencies import DatabaseSessionDependency
from app.api.maintenance_access import HEADERS, require_owner
from app.core.security import PasswordPolicyError, UsernamePolicyError
from app.services.user_management import (
    UserNotFoundError,
    list_business_accounts,
    reset_user_password,
)

router = APIRouter(
    prefix="/api/v1/maintenance", include_in_schema=False, dependencies=[Depends(require_owner)]
)


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=4, max_length=30)
    new_password: SecretStr


@router.get("/session")
def current_owner():
    return JSONResponse(
        {"authenticated": True, "role": "site_owner", "project": os.environ["GIFTLEDGER_PROJECT"]},
        headers=HEADERS,
    )


@router.get("/accounts")
def accounts(db: DatabaseSessionDependency):
    return JSONResponse(
        {"accounts": [asdict(account) for account in list_business_accounts(db)]}, headers=HEADERS
    )


@router.post("/accounts/reset-password")
def reset_password(body: PasswordReset, db: DatabaseSessionDependency):
    try:
        user = reset_user_password(db, body.username, body.new_password.get_secret_value())
    except (PasswordPolicyError, UsernamePolicyError) as exc:
        raise HTTPException(422, str(exc), headers=HEADERS) from exc
    except UserNotFoundError as exc:
        raise HTTPException(404, "找不到本站的这个账号。", headers=HEADERS) from exc
    return JSONResponse({"username": user.username, "reset": True}, headers=HEADERS)
