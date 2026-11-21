"""Owner-only admission settings and single-use registration invitations."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import NoResultFound

from app.api.dependencies import DatabaseSessionDependency
from app.api.maintenance_access import HEADERS, require_owner
from app.services import registration

router = APIRouter(
    prefix="/api/v1/maintenance", include_in_schema=False, dependencies=[Depends(require_owner)]
)


class RegistrationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lan_enabled: bool
    public_mode: Literal["closed", "invite", "open"]


class IssueInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvitationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern="^[0-9a-f]{32}$")


@router.get("/registration")
def read_policy(db: DatabaseSessionDependency):
    return JSONResponse(registration.read_policy(db), headers=HEADERS)


@router.post("/registration")
def change_policy(payload: RegistrationPolicy, db: DatabaseSessionDependency):
    saved = registration.save_policy(db, payload.lan_enabled, payload.public_mode)
    return JSONResponse(saved, headers=HEADERS)


@router.get("/invitations")
def invitations(db: DatabaseSessionDependency):
    return JSONResponse({"invitations": registration.list_invitations(db)}, headers=HEADERS)


@router.post("/invitations")
def issue_invitation(_payload: IssueInvitation, db: DatabaseSessionDependency):
    return JSONResponse(registration.issue_invitation(db), headers=HEADERS)


@router.post("/invitations/revoke")
def revoke_invitation(payload: InvitationTarget, db: DatabaseSessionDependency):
    try:
        revoked = registration.revoke_invitation(db, payload.id)
    except NoResultFound as exc:
        raise HTTPException(404, "邀请码不存在。", headers=HEADERS) from exc
    if not revoked:
        raise HTTPException(409, "该邀请码已失效。", headers=HEADERS)
    return JSONResponse({"revoked": True}, headers=HEADERS)
