"""Only registered, unexpired JWT session IDs are live. Rows stay on the issuing NAS."""

import hashlib
import time

from sqlalchemy import delete, exists, insert

from app.db.registration import business_sessions


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def record_session(db, user_id: int, session_id: str, expires_at: int) -> None:
    db.execute(delete(business_sessions).where(business_sessions.c.expires_at <= int(time.time())))
    db.execute(
        insert(business_sessions).values(
            user_id=user_id, token_hash=_digest(session_id), expires_at=expires_at
        )
    )


def active_session_exists(user_id: int, session_id: str):
    return exists().where(
        business_sessions.c.token_hash == _digest(session_id),
        business_sessions.c.user_id == user_id,
        business_sessions.c.expires_at > int(time.time()),
    )


def revoke_session(db, user_id: int, session_id: str) -> None:
    db.execute(
        delete(business_sessions).where(
            business_sessions.c.token_hash == _digest(session_id),
            business_sessions.c.user_id == user_id,
        )
    )


def revoke_user_sessions(db, user_id: int) -> None:
    db.execute(delete(business_sessions).where(business_sessions.c.user_id == user_id))
