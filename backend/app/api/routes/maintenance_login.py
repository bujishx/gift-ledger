"""Login for the site owner. Both methods delegate to the same site-bound session store."""

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.maintenance_access import (
    HEADERS,
    check_owner_request,
    owner_bearer,
    owner_sessions,
    session_errors,
)
from app.core.security import PasswordPolicyError

router = APIRouter(
    prefix="/api/v1/maintenance",
    include_in_schema=False,
    dependencies=[Depends(check_owner_request)],
)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["code", "password"]
    credential: SecretStr = Field(min_length=1, max_length=128)


class OwnerPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr | None = Field(..., max_length=128)


@router.get("/access-methods")
def access_methods():
    return JSONResponse(
        {"methods": ["code", "password"], "default": "code", "username_required": False},
        headers=HEADERS,
    )


def session_response(token: str):
    return JSONResponse(
        {"token": token, "role": "site_owner", "project": os.environ["GIFTLEDGER_PROJECT"]},
        headers=HEADERS,
    )


@router.post("/login")
def login(body: Login, request: Request):
    sessions = owner_sessions(request)
    credential = body.credential.get_secret_value()
    with session_errors():
        verify = sessions.login_with_code if body.method == "code" else sessions.login_with_password
        return session_response(verify(credential, request_origin=request.headers["origin"]))


@router.post("/owner-password")
def change_password(body: OwnerPassword, request: Request):
    sessions = owner_sessions(request)
    code = body.code.get_secret_value()
    origin = request.headers["origin"]
    with session_errors():
        if body.new_password is None:
            return session_response(sessions.remove_password_with_code(code, request_origin=origin))
        try:
            token = sessions.set_password_with_code(
                code, body.new_password.get_secret_value(), request_origin=origin
            )
        except PasswordPolicyError as exc:
            raise HTTPException(422, exc.message, headers=HEADERS) from exc
        return session_response(token)


@router.post("/logout")
def logout(request: Request):
    token = owner_bearer(request)
    with session_errors():
        owner_sessions(request).logout(token, request_origin=request.headers["origin"])
    return JSONResponse({"logged_out": True}, headers=HEADERS)
