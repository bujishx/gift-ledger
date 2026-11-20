"""Read-only identity of the current deployment, available to its signed-in users and owner."""
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.api.dependencies import SettingsDependency, get_current_user
from app.api.maintenance_access import require_owner

business = APIRouter(prefix="/site", tags=["关于本站"], dependencies=[Depends(get_current_user)])
owner = APIRouter(prefix="/api/v1/maintenance", tags=["站点维护"], dependencies=[Depends(require_owner)])
SITE_FILE = Path("/run/giftledger-site-info/site.json")


class About(BaseModel):
    version: str
    updated_at: date | None = None
    git_commit: str | None = None
    source_snapshot: str | None = None
    project: str | None = None
    instance_id: str | None = None
    origin: str | None = None
    restore_state: Literal["unknown", "none", "verified"] = "unknown"
    restored_at: datetime | None = None


@business.get("/about", response_model=About)
@owner.get("/about", response_model=About)
def about(settings: SettingsDependency, response: Response) -> About:
    response.headers["Cache-Control"] = "no-store"
    commit = os.getenv("GIFTLEDGER_BUILD_COMMIT", "")
    snapshot = os.getenv("GIFTLEDGER_BUILD_SNAPSHOT", "")
    try:
        day = date.fromisoformat(os.getenv("GIFTLEDGER_BUILD_DATE", ""))
    except ValueError:
        day = None
    info = About(version=os.getenv("GIFTLEDGER_BUILD_VERSION") or "开发版", updated_at=day,
                 git_commit=commit if re.fullmatch("[0-9a-f]{40}", commit) else None,
                 source_snapshot=snapshot if re.fullmatch("[0-9a-f]{64}", snapshot) else None)
    try:
        with SITE_FILE.open("rb") as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            return info
        site = json.loads(raw)
        if (not isinstance(site, dict) or not isinstance(site.get("project"), str)
                or site["project"] != os.getenv("GIFTLEDGER_PROJECT")
                or not re.fullmatch("[a-z0-9][a-z0-9_-]{0,62}", site["project"])
                or settings.trusted_origin_list != (site["origin"],)
                or str(UUID(site["instance_id"])) != site["instance_id"]):
            return info
        state = site.get("restore_state")
        when = site.get("restored_at")
        if state not in ("verified", "none", "unknown"):
            state, when = "unknown", None
        if state != "verified":
            when = None
        elif when is not None:
            try:
                stamp = datetime.fromisoformat(when.replace("Z", "+00:00"))
                when = stamp if stamp.utcoffset() is not None else None
            except (AttributeError, TypeError, ValueError):
                when = None
        return About(**info.model_dump(exclude={"origin","instance_id","project","restore_state","restored_at"}),
                     origin=site["origin"], instance_id=site["instance_id"], project=site["project"],
                     restore_state=state, restored_at=when)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return info
