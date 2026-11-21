"""Read-only business aggregates and the site's narrow host-issued status snapshot."""

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, GiftRecord, RelationshipType, User
from app.services.maintenance_sessions import OwnerSessions, SessionStoreError
from app.services.system_dictionary_catalog import SELF_RELATIONSHIP_SYSTEM_CODE
from app.services.user_management import has_business_data

TABLES = {"users": User, "contacts": Contact, "events": EventRecord, "entries": GiftRecord}
PAGE_SIZE = 50
BACKUP_MIN_BYTES = 512 * 1024**2


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def timestamp(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def host_json(root: Path, filename: str) -> dict:
    fd = os.open(root / "status" / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        publisher = (root / "owner-site.json").lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 4096
            or metadata.st_nlink != 1
            or metadata.st_uid != publisher.st_uid
            or metadata.st_mode & 0o022
        ):
            raise ValueError("untrusted site observation")
        data = json.loads(stream.read(4097))
    if not isinstance(data, dict):
        raise ValueError("invalid site observation")
    return data


def read_host_snapshot(root: Path, owner: OwnerSessions, now: datetime) -> tuple[dict, str | None]:
    try:
        data = host_json(root, "overview.json")
        if (data.get("format"), data.get("project"), data.get("instance")) != (
            "giftledger-owner-overview-v1",
            *owner.binding[:2],
        ):
            return {}, None
        sample = timestamp(data.get("sampled_at"))
        if sample is None:
            return {}, None
        if not timedelta(seconds=-5) <= now - sample <= timedelta(minutes=5):
            return {}, iso(sample)
        return data, iso(sample)
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return {}, None


def single(db: Session, statement):
    try:
        return db.scalar(statement)
    except SQLAlchemyError:
        db.rollback()
        return None


def overview(db: Session, owner: OwnerSessions, root: Path):
    now = datetime.now(UTC)
    host, sampled = read_host_snapshot(root, owner, now)
    expires = timestamp(host.get("certificate_expires_at"))
    database_ok = single(db, text("SELECT 1")) == 1
    totals = {name: None for name in TABLES}
    version = revision = None
    if database_ok:
        version_query = (
            "SELECT sqlite_version()"
            if db.get_bind().dialect.name == "sqlite"
            else "SELECT VERSION()"
        )
        version = single(db, text(version_query))
        revision = single(db, text("SELECT version_num FROM alembic_version LIMIT 1"))
        totals = {
            name: single(db, select(func.count()).select_from(model))
            for name, model in TABLES.items()
        }
    state = host.get("backup_state")
    if state not in ("idle", "failed", "succeeded", "needs_review"):
        state = "unknown"
    backup_id = host.get("backup_id")
    verified = host.get("decryption_verified")
    if state == "succeeded" and not (
        isinstance(backup_id, str) and re.fullmatch(r"[a-f0-9]{32}", backup_id) and verified is True
    ):
        state = "unknown"
    backup_time = timestamp(host.get("backup_checked_at"))
    web = host.get("web_status")

    def space(name):
        value = host.get(name)
        return value if type(value) is int and value >= 0 else None

    health = host.get("backup_health")
    warnings = {"failed", "overdue", "never", "unknown"}
    if (not isinstance(health, dict) or type(health.get("days")) is not int or not 1 <= health["days"] <= 365
            or not isinstance(health.get("issues"), list)
            or not all(isinstance(item, str) and item in warnings for item in health["issues"])):
        health = {"days": None, "last_verified_at": None, "issues": ["unknown"]}
    else:
        last_verified = timestamp(health.get("last_verified_at"))
        health = {"days": health["days"], "last_verified_at": iso(last_verified) if last_verified else None,
                  "issues": list(dict.fromkeys(health["issues"]))}

    return {
        "project": owner.binding[0],
        "instance": owner.binding[1],
        "checked_at": iso(now),
        "sampled_at": sampled,
        "service": {
            "web": web if web in ("ok", "unavailable") else "unknown",
            "api": "ok",
            "database": "ok" if database_ok else "unavailable",
        },
        "database": {"version": version, "schema_revision": revision},
        "backup_health": health,
        "totals": totals,
        "backup": {
            "state": state,
            "backup_id": backup_id if state == "succeeded" else None,
            "checked_at": iso(backup_time) if backup_time else None,
            "decryption_verified": True
            if state == "succeeded"
            else (False if state == "failed" and verified is False else None),
        },
        "certificate": {
            "expires_at": iso(expires) if expires else None,
            "remaining_days": (expires - now).days if expires else None,
        },
        "storage": {
            "ledger_free_bytes": space("ledger_free_bytes"),
            "backup_free_bytes": space("backup_free_bytes"),
            "backup_required_bytes": BACKUP_MIN_BYTES,
        },
    }


def include_web_task(value: dict, root: Path, task: dict) -> None:
    """Include queued work and failures that occurred before the backup engine could start."""
    state = task["state"]
    if state in ("queued", "running", "needs_review"):
        if state == "needs_review" and "backup_health" in value and "failed" not in value["backup_health"]["issues"]:
            value["backup_health"]["issues"].insert(0, "failed")
        value["backup"] = {
            "state": state,
            "checked_at": None,
            "backup_id": None,
            "decryption_verified": None,
        }
        return
    if state not in ("succeeded", "failed"):
        return
    try:
        observed = host_json(root, "backup-observation.json")
        if any(observed[k] != task[k] for k in ("project", "operation_id", "state")):
            return
        seen = timestamp(observed["observed_at"])
        previous = timestamp(value["backup"]["checked_at"])
        if (
            seen is None
            or seen > datetime.now(UTC) + timedelta(seconds=5)
            or (previous and seen < previous)
        ):
            return
        health = value.get("backup_health")
        if health is not None and state == "failed":
            success = timestamp(health.get("last_verified_at"))
            if (not success or seen >= success) and "failed" not in health["issues"]:
                health["issues"].insert(0, "failed")
        value["backup"] = {
            "state": state,
            "checked_at": iso(seen),
            "backup_id": task.get("backup_id"),
            "decryption_verified": task.get("decryption_verified"),
        }
    except (OSError, TypeError, ValueError, KeyError):
        return


def usage(db: Session, owner: OwnerSessions, after: int, *, active: bool | None = None):
    query = select(User.id, User.username, User.is_active).where(User.id > after)
    if active is not None:
        query = query.where(User.is_active == int(active)).add_columns(
            (~has_business_data(User.id)).label("can_delete")
        )
    page = list(db.execute(query.order_by(User.id).limit(PAGE_SIZE + 1)))
    cursor = page[PAGE_SIZE - 1].id if len(page) > PAGE_SIZE else None
    page = page[:PAGE_SIZE]
    ids = [row.id for row in page]
    if not ids:
        return {"users": [], "next_after": None}
    names = dict(
        db.execute(
            select(Contact.user_id, Contact.name)
            .join(RelationshipType, RelationshipType.id == Contact.relation_type_id)
            .where(
                Contact.user_id.in_(ids),
                RelationshipType.user_id.is_(None),
                RelationshipType.system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
            )
        ).all()
    )
    counts = {
        key: dict(
            db.execute(
                select(model.user_id, func.count())
                .where(model.user_id.in_(ids))
                .group_by(model.user_id)
            ).all()
        )
        for key, model in TABLES.items()
        if key != "users"
    }
    try:
        logins = owner.business_logins(ids)
    except SessionStoreError:
        logins = {}
    users = []
    for row in page:
        login = logins.get((row.id, row.username))
        account = {"id": row.id, "can_delete": bool(row.can_delete)} if active is not None else {}
        users.append(
            {
                **account,
                "username": row.username,
                "display_name": names.get(row.id),
                "is_active": bool(row.is_active),
                **{key: values.get(row.id, 0) for key, values in counts.items()},
                "last_login_at": iso(datetime.fromtimestamp(login, UTC))
                if login is not None
                else None,
            }
        )
    return {"users": users, "next_after": cursor}
