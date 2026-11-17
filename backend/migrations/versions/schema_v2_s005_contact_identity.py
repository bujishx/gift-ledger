"""增加同一用户范围内的联系人身份唯一约束。

Revision ID: schema_v2_s005
Revises: schema_v2_s004
Create Date: 2026-09-27

正式升级前必须完成整库备份、停止联系人写入。并先在隔离副本演练。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

from app.services.contact_identity import (
    contact_identity_components_v1,
    contact_identity_hash_v1,
)

revision: str = "schema_v2_s005"
down_revision: str | None = "schema_v2_s004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _contact_rows(bind) -> list[dict]:
    rows = bind.execute(
        sa.text("SELECT id, user_id, name, location, remark FROM contacts ORDER BY user_id, id")
    ).mappings()
    return [dict(row) for row in rows]


def _preflight(rows: list[dict]) -> list[dict]:
    prepared: list[dict] = []
    identities: dict[tuple[int, bytes], tuple[int, tuple[str, str, str]]] = {}
    for row in rows:
        contact_id = int(row["id"])
        user_id = int(row["user_id"])
        try:
            components = contact_identity_components_v1(
                row["name"],
                row["location"],
                row["remark"],
            )
            identity_hash = contact_identity_hash_v1(
                row["name"],
                row["location"],
                row["remark"],
            )
        except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "S005_CONTACT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {user_id} 的联系人 {contact_id} 无法生成身份指纹"
            ) from exc

        key = (user_id, identity_hash)
        previous = identities.get(key)
        if previous is not None:
            previous_id, previous_components = previous
            error_type = "身份重复" if previous_components == components else "指纹碰撞"
            raise RuntimeError(
                "S005_CONTACT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {user_id} 的联系人 {previous_id} 与 {contact_id} 出现{error_type}；"
                "迁移不会自动合并或删除，请人工处理后重试"
            )
        identities[key] = (contact_id, components)
        prepared.append(
            {
                "contact_id": contact_id,
                "user_id": user_id,
                "identity_hash": identity_hash,
            }
        )
    return prepared


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S005 包含联系人数据预检与回填，只允许连接数据库在线执行")

    bind = op.get_bind()
    prepared = _preflight(_contact_rows(bind))

    op.add_column(
        "contacts",
        sa.Column(
            "identity_hash",
            mysql.BINARY(32),
            nullable=True,
            comment="服务端按规范化姓名、常住地、联系人备注生成的内部身份指纹",
        ),
    )
    if prepared:
        bind.execute(
            sa.text(
                "UPDATE contacts SET identity_hash = :identity_hash "
                "WHERE id = :contact_id AND user_id = :user_id"
            ),
            prepared,
        )

    null_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM contacts WHERE identity_hash IS NULL")
    ).scalar_one()
    duplicate_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT user_id, identity_hash FROM contacts "
            "GROUP BY user_id, identity_hash HAVING COUNT(*) > 1"
            ") AS duplicate_identities"
        )
    ).scalar_one()
    if int(null_count) or int(duplicate_count):
        raise RuntimeError(
            "S005_CONTACT_IDENTITY_BACKFILL_FAILED："
            "存在空指纹或用户范围内重复；请保持维护状态并检查"
        )

    op.alter_column(
        "contacts",
        "identity_hash",
        existing_type=mysql.BINARY(32),
        nullable=False,
        existing_comment="服务端按规范化姓名、常住地、联系人备注生成的内部身份指纹",
    )
    op.create_unique_constraint(
        "uq_contacts_user_identity",
        "contacts",
        ["user_id", "identity_hash"],
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S005 降级只允许连接数据库在线执行")
    op.drop_constraint("uq_contacts_user_identity", "contacts", type_="unique")
    op.drop_column("contacts", "identity_hash")
