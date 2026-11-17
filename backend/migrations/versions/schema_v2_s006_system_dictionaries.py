# ruff: noqa: RUF002, RUF003
"""定稿系统字典，并增加跨环境稳定标识。

Revision ID: schema_v2_s006
Revises: schema_v2_s005
Create Date: 2026-09-28

升级前必须完成整库备份、隔离恢复验证、维护停写和字典冲突预检。
两类同学合并不可无损自动降级；回退须恢复升级前备份。
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import bindparam
from sqlalchemy.dialects import mysql

revision: str = "schema_v2_s006"
down_revision: str | None = "schema_v2_s005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@dataclass(frozen=True)
class _Seed:
    system_code: str
    name: str
    sort_order: int
    legacy_id: int | None


# 已发布迁移必须自包含；后续字典版本不能通过修改应用目录来改变 S006 历史结果。
RELATIONSHIP_SEEDS = (
    _Seed("relationship.self", "本人", 1, 1),
    _Seed("relationship.relative", "亲戚", 2, 2),
    _Seed("relationship.friend", "朋友", 3, 9),
    _Seed("relationship.classmate", "同学", 4, 4),
    _Seed("relationship.colleague", "同事", 5, 3),
    _Seed("relationship.neighbor", "邻居", 6, 8),
    _Seed("relationship.military_comrade", "战友", 7, 6),
    _Seed("relationship.business_partner", "生意伙伴", 8, 7),
    _Seed("relationship.other", "其他", 9, None),
)

KINSHIP_SEEDS = (
    _Seed("kinship.self", "本人", 1, 1),
    _Seed("kinship.spouse", "配偶", 2, None),
    _Seed("kinship.father", "父亲", 3, 2),
    _Seed("kinship.mother", "母亲", 4, 3),
    _Seed("kinship.wife_father", "岳父", 5, 4),
    _Seed("kinship.wife_mother", "岳母", 6, 5),
    _Seed("kinship.husband_father", "公公", 7, 6),
    _Seed("kinship.husband_mother", "婆婆", 8, 7),
    _Seed("kinship.first_son", "长子", 9, 8),
    _Seed("kinship.second_son", "次子", 10, 9),
    _Seed("kinship.first_daughter", "长女", 11, 10),
    _Seed("kinship.second_daughter", "次女", 12, 11),
    _Seed("kinship.paternal_grandfather", "祖父（爷爷）", 13, 12),
    _Seed("kinship.paternal_grandmother", "祖母（奶奶）", 14, 13),
    _Seed("kinship.maternal_grandfather", "外祖父（外公）", 15, None),
    _Seed("kinship.maternal_grandmother", "外祖母（外婆）", 16, None),
)

EVENT_SEEDS = (
    _Seed("event.wedding", "婚礼", 1, 1),
    _Seed("event.newborn", "新生儿（满月／百日）", 2, 2),
    _Seed("event.visit_patient", "探病慰问", 3, 8),
    _Seed("event.funeral", "白事（丧葬）", 4, 4),
    _Seed("event.housewarming", "乔迁", 5, 5),
    _Seed("event.education", "升学", 6, 6),
    _Seed("event.business_opening", "开业", 7, 7),
    _Seed("event.new_year_visit", "拜年", 8, 9),
    _Seed("event.birthday", "生日／寿宴", 9, 3),
)

_SEEDS_BY_CATEGORY = {
    "relationship": RELATIONSHIP_SEEDS,
    "kinship": KINSHIP_SEEDS,
    "event": EVENT_SEEDS,
}


def _final_names(category: str) -> tuple[str, ...]:
    return tuple(seed.name for seed in _SEEDS_BY_CATEGORY[category])


_S005_ROWS = {
    "relationship_types": (
        (1, "自己", 1, 1),
        (2, "亲戚", 2, 1),
        (3, "同事", 3, 1),
        (4, "初高中同学", 4, 1),
        (5, "大学同学", 5, 1),
        (6, "战友", 6, 1),
        (7, "生意伙伴", 7, 1),
        (8, "邻居", 8, 1),
        (9, "朋友", 9, 1),
    ),
    "kinship_types": (
        (1, "本人", 1, 1),
        (2, "父亲", 2, 1),
        (3, "母亲", 3, 1),
        (4, "岳父", 4, 1),
        (5, "岳母", 5, 1),
        (6, "公公", 6, 1),
        (7, "婆婆", 7, 1),
        (8, "长子", 8, 1),
        (9, "次子", 9, 1),
        (10, "长女", 10, 1),
        (11, "次女", 11, 1),
        (12, "祖父 (爷爷)", 12, 1),
        (13, "祖母 (奶奶)", 13, 1),
    ),
    "event_types": (
        (1, "结婚", 1, 1),
        (2, "添子 (满月/百天)", 2, 1),
        (3, "生日 (寿宴)", 3, 1),
        (4, "驾归瑶池 (白事)", 4, 1),
        (5, "乔迁新居", 5, 1),
        (6, "榜上有名 (升学)", 6, 1),
        (7, "金日开业", 7, 1),
        (8, "微恙慰问 (生病就医)", 8, 1),
        (9, "日常拜年", 9, 1),
    ),
}

_CATEGORY_TABLES = {
    "relationship": "relationship_types",
    "kinship": "kinship_types",
    "event": "event_types",
}


def _assert_s005_system_rows(bind) -> None:
    for table, expected in _S005_ROWS.items():
        rows = tuple(
            (int(row.id), str(row.type_name), int(row.sort_order), int(row.is_active))
            for row in bind.execute(
                sa.text(
                    f"SELECT id, type_name, sort_order, is_active FROM {table} "
                    "WHERE user_id IS NULL ORDER BY id"
                )
            )
        )
        if rows != expected:
            raise RuntimeError(
                "S006_SYSTEM_DICTIONARY_PREFLIGHT_FAILED："
                f"{table} 的 S005 系统项编号、名称、顺序或启用状态发生漂移"
            )


def _custom_conflicts(bind) -> list[dict]:
    conflicts: list[dict] = []
    for category, table in _CATEGORY_TABLES.items():
        statement = sa.text(
            f"SELECT d.user_id, u.username, d.id, d.type_name FROM {table} AS d "
            "INNER JOIN users AS u ON u.id = d.user_id "
            "WHERE d.user_id IS NOT NULL AND d.name_key IN :names "
            "ORDER BY d.user_id, d.id"
        ).bindparams(bindparam("names", expanding=True))
        for row in bind.execute(statement, {"names": _final_names(category)}).mappings():
            conflicts.append(
                {
                    "category": category,
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"]),
                    "item_id": int(row["id"]),
                    "name": str(row["type_name"]),
                }
            )
    return conflicts


def _raise_for_conflicts(bind) -> None:
    conflicts = _custom_conflicts(bind)
    if not conflicts:
        return
    summary = "；".join(
        f"用户{item['user_id']}／{item['category']}／项目{item['item_id']}「{item['name']}」"
        for item in conflicts[:20]
    )
    if len(conflicts) > 20:
        summary += f"；另有{len(conflicts) - 20}项"
    raise RuntimeError(
        "S006_CUSTOM_DICTIONARY_CONFLICT："
        f"发现{len(conflicts)}个自定义项与新系统名称冲突，迁移未开始：{summary}"
    )


def _add_system_code_column(table: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "system_code",
            mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"),
            nullable=True,
            comment="系统内置项稳定标识；用户自定义项必须为空",
        ),
    )


def _update_existing_seed(bind, table: str, seed) -> None:
    result = bind.execute(
        sa.text(
            f"UPDATE {table} SET system_code = :system_code, type_name = :name, "
            "sort_order = :sort_order WHERE id = :legacy_id AND user_id IS NULL"
        ),
        {
            "system_code": seed.system_code,
            "name": seed.name,
            "sort_order": seed.sort_order,
            "legacy_id": seed.legacy_id,
        },
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "S006_SYSTEM_DICTIONARY_UPDATE_FAILED："
            f"{table} 的旧系统项 {seed.legacy_id} 更新数量异常"
        )


def _insert_new_seed(bind, table: str, seed) -> None:
    bind.execute(
        sa.text(
            f"INSERT INTO {table} "
            "(user_id, system_code, type_name, sort_order, is_active) "
            "VALUES (NULL, :system_code, :name, :sort_order, 1)"
        ),
        {
            "system_code": seed.system_code,
            "name": seed.name,
            "sort_order": seed.sort_order,
        },
    )


def _apply_seeds(bind, table: str, seeds) -> None:
    for seed in seeds:
        if seed.legacy_id is None:
            _insert_new_seed(bind, table, seed)
        else:
            _update_existing_seed(bind, table, seed)


def _add_system_code_constraints(table: str, prefix: str) -> None:
    op.create_unique_constraint(f"uq_{prefix}_system_code", table, ["system_code"])
    op.create_check_constraint(
        f"ck_{prefix}_system_owner",
        table,
        "(`user_id` IS NULL AND `system_code` IS NOT NULL) OR "
        "(`user_id` IS NOT NULL AND `system_code` IS NULL)",
    )
    op.create_check_constraint(
        f"ck_{prefix}_system_code",
        table,
        "`system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')",
    )


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S006 包含数据预检与迁移，只允许连接数据库在线执行")

    bind = op.get_bind()
    _assert_s005_system_rows(bind)
    _raise_for_conflicts(bind)

    for table in _S005_ROWS:
        _add_system_code_column(table)

    # D078：两类同学统一引用原“初高中同学”系统项（ID 4）。
    bind.execute(sa.text("UPDATE contacts SET relation_type_id = 4 WHERE relation_type_id = 5"))
    deleted = bind.execute(
        sa.text("DELETE FROM relationship_types WHERE id = 5 AND user_id IS NULL")
    )
    if deleted.rowcount != 1:
        raise RuntimeError("S006_CLASSMATE_MERGE_FAILED：旧大学同学系统项删除数量异常")

    op.alter_column(
        "contacts",
        "relation_type_id",
        existing_type=mysql.INTEGER(),
        existing_nullable=False,
        existing_comment="关系类型，ID=1表示自己",
        comment="关系类型，ID=1表示本人",
    )

    _apply_seeds(bind, "relationship_types", RELATIONSHIP_SEEDS)
    _apply_seeds(bind, "kinship_types", KINSHIP_SEEDS)
    _apply_seeds(bind, "event_types", EVENT_SEEDS)

    _add_system_code_constraints("relationship_types", "relationship")
    _add_system_code_constraints("kinship_types", "kinship")
    _add_system_code_constraints("event_types", "event_type")


def downgrade() -> None:
    raise RuntimeError(
        "S006 合并了两类同学，不能无损自动降级；"
        "请保持维护状态并恢复升级前已验证整库备份与对应代码版本"
    )
