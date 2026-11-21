"""Site admission policy and one-person invitations."""

import hashlib
import secrets
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.db.registration import invitations, policy

INVITE_LIFETIME = 48 * 60 * 60


class InvalidInvitation(ValueError):
    pass


def read_policy(session: Session, *, lock: bool = False) -> dict:
    statement = select(policy.c.lan_enabled, policy.c.public_mode).where(policy.c.id == 1)
    if lock:
        statement = statement.with_for_update(read=True)
    return dict(session.execute(statement).mappings().one())


def save_policy(session: Session, lan_enabled: bool, public_mode: str) -> dict:
    with session.begin():
        session.execute(
            update(policy)
            .where(policy.c.id == 1)
            .values(lan_enabled=lan_enabled, public_mode=public_mode)
        )
        return read_policy(session)


def timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def issue_invitation(session: Session) -> dict:
    code = secrets.token_urlsafe(24)
    invitation_id = uuid4().hex
    now = int(time.time())
    expires = now + INVITE_LIFETIME
    with session.begin():
        session.execute(
            invitations.insert().values(
                id=invitation_id,
                code_hash=code_digest(code),
                created_at=now,
                expires_at=expires,
            )
        )
    return {"id": invitation_id, "code": code, "expires_at": timestamp(expires)}


def code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def available(now: int):
    return (
        invitations.c.used_at.is_(None),
        invitations.c.revoked_at.is_(None),
        invitations.c.expires_at > now,
    )


def consume_invitation(session: Session, code: str | None) -> None:
    now = int(time.time())
    if (
        not code
        or session.execute(
            update(invitations)
            .where(invitations.c.code_hash == code_digest(code.strip()), *available(now))
            .values(used_at=now)
        ).rowcount
        != 1
    ):
        raise InvalidInvitation


def list_invitations(session: Session) -> list[dict]:
    table = invitations.c
    rows = session.execute(
        select(
            table.id, table.created_at, table.expires_at, table.revoked_at, table.used_at
        ).order_by(table.created_at.desc(), table.id.desc())
    )
    now = int(time.time())
    result = []
    for row in rows:
        status = "available"
        if row.used_at is not None:
            status = "used"
        elif row.revoked_at is not None:
            status = "revoked"
        elif row.expires_at <= now:
            status = "expired"
        result.append(
            {
                "id": row.id,
                "created_at": timestamp(row.created_at),
                "expires_at": timestamp(row.expires_at),
                "status": status,
            }
        )
    return result


def revoke_invitation(session: Session, invitation_id: str) -> bool:
    now = int(time.time())
    with session.begin():
        changed = session.execute(
            update(invitations)
            .where(invitations.c.id == invitation_id, *available(now))
            .values(revoked_at=now)
        ).rowcount
        if changed:
            return True
        row = session.execute(
            select(invitations.c.revoked_at).where(invitations.c.id == invitation_id)
        ).one_or_none()
        if row is None:
            raise NoResultFound
        return row.revoked_at is not None
