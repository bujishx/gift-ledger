# ruff: noqa: RUF002
"""D102 系统字典改名，以及 D108 联系人姓名和备注两项唯一。

Revision ID: schema_v2_s007
Revises: schema_v2_s006
Create Date: 2026-09-30

升级前必须备份数据库并停止账本写入。先在隔离副本演练。
如历史联系人只靠常住地区分。本迁移在任何写入前停止。不会自动合并或删除资料。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import bindparam
from sqlalchemy.dialects import mysql

from app.services.contact_identity import (
    contact_identity_components_v2,
    contact_identity_hash_v1,
    contact_identity_hash_v2,
)

revision: str = "schema_v2_s007"
down_revision: str | None = "schema_v2_s006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 已发布 S006 系统字典快照。S007 不能依赖会继续演进的应用目录常量。
_RELATIONSHIP_S006 = (
    ("relationship.self", "本人", 1),
    ("relationship.relative", "亲戚", 2),
    ("relationship.friend", "朋友", 3),
    ("relationship.classmate", "同学", 4),
    ("relationship.colleague", "同事", 5),
    ("relationship.neighbor", "邻居", 6),
    ("relationship.military_comrade", "战友", 7),
    ("relationship.business_partner", "生意伙伴", 8),
    ("relationship.other", "其他", 9),
)
_KINSHIP_S006 = (
    ("kinship.self", "本人", 1),
    ("kinship.spouse", "配偶", 2),
    ("kinship.father", "父亲", 3),
    ("kinship.mother", "母亲", 4),
    ("kinship.wife_father", "岳父", 5),
    ("kinship.wife_mother", "岳母", 6),
    ("kinship.husband_father", "公公", 7),
    ("kinship.husband_mother", "婆婆", 8),
    ("kinship.first_son", "长子", 9),
    ("kinship.second_son", "次子", 10),
    ("kinship.first_daughter", "长女", 11),
    ("kinship.second_daughter", "次女", 12),
    ("kinship.paternal_grandfather", "祖父（爷爷）", 13),
    ("kinship.paternal_grandmother", "祖母（奶奶）", 14),
    ("kinship.maternal_grandfather", "外祖父（外公）", 15),
    ("kinship.maternal_grandmother", "外祖母（外婆）", 16),
)
_DICTIONARY_RENAMES = {
    "relationship_types": (("relationship.self", "本人", "我"),),
    "kinship_types": tuple(
        (code, old_name, "之" + old_name)
        for code, old_name, _order in _KINSHIP_S006
        if code != "kinship.self"
    ),
}


def _preflight_dictionary_rows(bind) -> None:
    for table, expected in (
        ("relationship_types", _RELATIONSHIP_S006),
        ("kinship_types", _KINSHIP_S006),
    ):
        actual = tuple(
            (str(row.system_code), str(row.type_name), int(row.sort_order), int(row.is_active))
            for row in bind.execute(
                sa.text(
                    f"SELECT system_code, type_name, sort_order, is_active FROM {table} "
                    "WHERE user_id IS NULL ORDER BY sort_order"
                )
            )
        )
        if actual != tuple((*row, 1) for row in expected):
            raise RuntimeError(
                "S007_SYSTEM_DICTIONARY_PREFLIGHT_FAILED："
                f"{table} 的 S006 系统项名称、顺序或启用状态发生漂移，迁移未修改数据"
            )


def _preflight_dictionary_conflicts(bind) -> None:
    conflicts: list[str] = []
    for table, renames in _DICTIONARY_RENAMES.items():
        new_names = tuple(new_name for _code, _old_name, new_name in renames)
        statement = sa.text(
            f"SELECT user_id, id, type_name FROM {table} "
            "WHERE user_id IS NOT NULL AND name_key IN :names ORDER BY user_id, id"
        ).bindparams(bindparam("names", expanding=True))
        conflicts.extend(
            f"{table} 用户{int(row.user_id)} 项目{int(row.id)}「{row.type_name}」"
            for row in bind.execute(statement, {"names": new_names})
        )
    if conflicts:
        raise RuntimeError(
            "S007_CUSTOM_DICTIONARY_CONFLICT："
            f"发现{len(conflicts)}个自定义项与新系统名称冲突，迁移未修改数据："
            + "；".join(conflicts[:20])
        )


def _upgrade_dictionary_names(bind) -> None:
    for table, renames in _DICTIONARY_RENAMES.items():
        for code, old_name, new_name in renames:
            result = bind.execute(
                sa.text(
                    f"UPDATE {table} SET type_name = :new_name "
                    "WHERE user_id IS NULL AND system_code = :code AND type_name = :old_name"
                ),
                {"new_name": new_name, "code": code, "old_name": old_name},
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    "S007_SYSTEM_DICTIONARY_UPDATE_FAILED："
                    f"{table} 的 {code} 更新数量异常，请保持维护状态并检查"
                )


def _contact_rows(bind) -> list[dict]:
    rows = bind.execute(
        sa.text(
            "SELECT id, user_id, name, location, remark, identity_hash "
            "FROM contacts ORDER BY user_id, id FOR UPDATE"
        )
    ).mappings()
    return [dict(row) for row in rows]


def _preflight_contact_identity(rows: list[dict]) -> list[dict]:
    prepared: list[dict] = []
    identities: dict[tuple[int, bytes], tuple[int, tuple[str, str]]] = {}
    current_hashes: dict[tuple[int, bytes], int] = {}

    for row in rows:
        contact_id = int(row["id"])
        user_id = int(row["user_id"])
        try:
            components = contact_identity_components_v2(row["name"], row["location"], row["remark"])
            target_hash = contact_identity_hash_v2(row["name"], row["location"], row["remark"])
            old_hash = contact_identity_hash_v1(row["name"], row["location"], row["remark"])
            current_hash = bytes(row["identity_hash"])
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "S007_CONTACT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {user_id} 的联系人 {contact_id} 无法校验身份指纹"
            ) from exc

        if current_hash not in (old_hash, target_hash):
            raise RuntimeError(
                "S007_CONTACT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {user_id} 的联系人 {contact_id} 现有指纹与资料不符；"
                "迁移未修改数据，请核查后重试"
            )

        key = (user_id, target_hash)
        previous = identities.get(key)
        if previous is not None:
            previous_id, previous_components = previous
            error_type = "身份重复" if previous_components == components else "指纹碰撞"
            raise RuntimeError(
                "S007_CONTACT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {user_id} 的联系人 {previous_id} 与 {contact_id} 出现{error_type}；"
                "常住地资料已保留，迁移不会自动合并或删除，请人工处理后重试"
            )
        identities[key] = (contact_id, components)
        current_hashes[(user_id, current_hash)] = contact_id
        prepared.append(
            {
                "contact_id": contact_id,
                "user_id": user_id,
                "previous_hash": current_hash,
                "identity_hash": target_hash,
            }
        )

    # MySQL 的唯一键按每行更新检查。极罕见的新旧指纹交叉占位必须先停止。
    for item in prepared:
        if item["previous_hash"] == item["identity_hash"]:
            continue
        occupied_by = current_hashes.get((item["user_id"], item["identity_hash"]))
        if occupied_by is not None and occupied_by != item["contact_id"]:
            raise RuntimeError(
                "S007_CONTACT_IDENTITY_PREFLIGHT_FAILED："
                f"用户 {item['user_id']} 的联系人 {item['contact_id']} 与 {occupied_by} "
                "出现新旧指纹交叉占位；迁移未修改数据，请人工检查"
            )

    return prepared


def _upgrade_contact_identity(bind, prepared: list[dict]) -> None:
    changes = [item for item in prepared if item["previous_hash"] != item["identity_hash"]]
    if changes:
        bind.execute(
            sa.text(
                "UPDATE contacts SET identity_hash = :identity_hash "
                "WHERE id = :contact_id AND user_id = :user_id "
                "AND identity_hash = :previous_hash"
            ),
            changes,
        )

    expected = {(item["user_id"], item["contact_id"]): item["identity_hash"] for item in prepared}
    verified = _contact_rows(bind)
    if len(verified) != len(prepared) or any(
        expected.get((int(row["user_id"]), int(row["id"]))) != bytes(row["identity_hash"])
        for row in verified
    ):
        raise RuntimeError(
            "S007_CONTACT_IDENTITY_BACKFILL_FAILED：身份指纹回填验证失败；请保持维护状态并检查"
        )

    op.alter_column(
        "contacts",
        "identity_hash",
        existing_type=mysql.BINARY(32),
        existing_nullable=False,
        existing_comment="服务端按规范化姓名、常住地、联系人备注生成的内部身份指纹",
        comment="服务端按规范化姓名和联系人备注生成的内部身份指纹",
    )


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S007 包含联系人数据预检与回填，只允许连接数据库在线执行")

    bind = op.get_bind()
    prepared = _preflight_contact_identity(_contact_rows(bind))
    _preflight_dictionary_rows(bind)
    _preflight_dictionary_conflicts(bind)
    _upgrade_dictionary_names(bind)
    _upgrade_contact_identity(bind, prepared)


def downgrade() -> None:
    raise RuntimeError("S007 改变联系人身份口径，不能无损自动降级")
