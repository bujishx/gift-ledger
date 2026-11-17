"""Use host, kinship, event type and trimmed remark for event uniqueness.

Revision ID: schema_v2_s008
Revises: schema_v2_s007
Create Date: 2026-10-06

Back up the database and stop all business writers before migration. MySQL DDL
commits independently; the migration supports resuming after an interrupted DDL.
Historical conflicts are reported by IDs before any DDL or data write. No events
or gifts are merged, deleted, renumbered, or relinked by this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

from app.services.event_identity import event_identity_components, event_identity_hash

revision: str = "schema_v2_s008"
down_revision: str | None = "schema_v2_s007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_COMMENT = "服务端按事主、亲属称谓、事件类型和首尾去空白备注生成的内部身份指纹"
NEW_UNIQUE = "uq_events_user_identity"
OLD_UNIQUE = "uq_events_identity"
HOST_INDEX = "idx_events_user_host"
OLD_IDENTITY_COLUMNS = ["user_id", "contact_id", "relative_type_id", "event_type_id", "event_date"]


def _event_rows(bind) -> list[dict]:
    return [
        dict(row)
        for row in bind.execute(
            sa.text(
                "SELECT id, user_id, contact_id, relative_type_id, event_type_id, "
                "event_date, remark FROM event_records ORDER BY user_id, id"
            )
        ).mappings()
    ]


def _preflight(rows: list[dict]) -> list[dict]:
    prepared = []
    identities: dict[tuple[int, bytes], list[tuple[int, tuple]]] = {}
    for row in rows:
        event_id, user_id = int(row["id"]), int(row["user_id"])
        try:
            args = (
                int(row["contact_id"]),
                int(row["relative_type_id"]),
                int(row["event_type_id"]),
                row["remark"],
            )
            components = event_identity_components(*args)
            fingerprint = event_identity_hash(*args)
        except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "S008_EVENT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {user_id} 的事件 {event_id} 无法生成身份指纹"
            ) from exc
        identities.setdefault((user_id, fingerprint), []).append((event_id, components))
        prepared.append({"event_id": event_id, "user_id": user_id, "identity_hash": fingerprint})
    conflicts = []
    for (user_id, _fingerprint), items in identities.items():
        if len(items) > 1:
            kind = "身份重复" if len({components for _, components in items}) == 1 else "指纹碰撞"
            ids = "、".join(str(event_id) for event_id, _ in items)
            conflicts.append(f"用户 {user_id} 的事件 {ids} 出现{kind}")
    if conflicts:
        raise RuntimeError(
            "S008_EVENT_IDENTITY_PREFLIGHT_FAILED："
            + "；".join(conflicts)
            + "；迁移不会自动合并或删除，请处理后重试"
        )
    return prepared


def _downgrade_preflight(rows: list[dict]) -> None:
    identities: dict[tuple, list[int]] = {}
    for row in rows:
        key = tuple(row[column] for column in OLD_IDENTITY_COLUMNS)
        identities.setdefault(key, []).append(int(row["id"]))
    conflicts = [
        f"用户 {key[0]} 的事件 {'、'.join(str(item) for item in ids)}"
        for key, ids in identities.items()
        if len(ids) > 1
    ]
    if conflicts:
        raise RuntimeError(
            "S008_EVENT_IDENTITY_DOWNGRADE_PREFLIGHT_FAILED："
            + "；".join(conflicts)
            + " 不符合旧日期唯一规则；降级不会自动合并或删除，请处理后重试"
        )


def _schema_state(bind) -> tuple[dict, dict]:
    inspector = sa.inspect(bind)
    columns = {item["name"]: item for item in inspector.get_columns("event_records")}
    indexes = {item["name"]: item for item in inspector.get_indexes("event_records")}
    indexes.update(
        {item["name"]: item for item in inspector.get_unique_constraints("event_records")}
    )
    return columns, indexes


def _verify_hashes(bind, prepared: list[dict]) -> None:
    expected = {(item["user_id"], item["event_id"]): item["identity_hash"] for item in prepared}
    actual = {
        (int(row["user_id"]), int(row["id"])): bytes(row["identity_hash"])
        if row["identity_hash"] is not None
        else None
        for row in bind.execute(
            sa.text("SELECT id, user_id, identity_hash FROM event_records")
        ).mappings()
    }
    if actual != expected:
        raise RuntimeError("S008_EVENT_IDENTITY_BACKFILL_FAILED：指纹核对失败，请保持维护状态")


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S008 包含事件数据预检与回填，只允许连接数据库在线执行")
    bind = op.get_bind()
    rows = _event_rows(bind)
    prepared = _preflight(rows)  # Must precede every DDL/write, including interrupted retries.
    columns, indexes = _schema_state(bind)
    if "identity_hash" not in columns:
        op.add_column(
            "event_records",
            sa.Column("identity_hash", mysql.BINARY(32), nullable=True, comment=HASH_COMMENT),
        )
    else:
        expected = {(item["user_id"], item["event_id"]): item["identity_hash"] for item in prepared}
        for row in bind.execute(
            sa.text("SELECT id, user_id, identity_hash FROM event_records")
        ).mappings():
            if row["identity_hash"] is not None and bytes(row["identity_hash"]) != expected.get(
                (int(row["user_id"]), int(row["id"]))
            ):
                raise RuntimeError(
                    "S008_EVENT_IDENTITY_BACKFILL_FAILED：现存指纹不符，请保持维护状态"
                )
    if prepared:
        bind.execute(
            sa.text(
                "UPDATE event_records SET identity_hash = :identity_hash "
                "WHERE id = :event_id AND user_id = :user_id AND identity_hash IS NULL"
            ),
            prepared,
        )
    _verify_hashes(bind, prepared)
    if _event_rows(bind) != rows:
        raise RuntimeError("S008_EVENT_IDENTITY_BACKFILL_FAILED：迁移期间事件变化，请保持维护状态")
    op.alter_column(
        "event_records",
        "identity_hash",
        existing_type=mysql.BINARY(32),
        nullable=False,
        existing_comment=HASH_COMMENT,
    )
    if NEW_UNIQUE not in indexes:
        op.create_unique_constraint(NEW_UNIQUE, "event_records", ["user_id", "identity_hash"])
    # The old unique also supports fk_events_host; preserve its leftmost FK columns first.
    if HOST_INDEX not in indexes:
        op.create_index(HOST_INDEX, "event_records", ["user_id", "contact_id"])
    if OLD_UNIQUE in indexes:
        op.drop_constraint(OLD_UNIQUE, "event_records", type_="unique")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S008 降级只允许连接数据库在线执行")
    bind = op.get_bind()
    _downgrade_preflight(_event_rows(bind))  # A different remark can share the same old date key.
    columns, indexes = _schema_state(bind)
    if OLD_UNIQUE not in indexes:
        op.create_unique_constraint(OLD_UNIQUE, "event_records", OLD_IDENTITY_COLUMNS)
    if NEW_UNIQUE in indexes:
        op.drop_constraint(NEW_UNIQUE, "event_records", type_="unique")
    if "identity_hash" in columns:
        op.drop_column("event_records", "identity_hash")
    if HOST_INDEX in indexes:
        op.drop_index(HOST_INDEX, table_name="event_records")
