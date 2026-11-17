"""Add fellow townsman and shorten seven system dictionary labels in place.

Revision ID: schema_v2_s009
Revises: schema_v2_s008
Create Date: 2026-10-07

Data-only migration. Back up the database and stop every writer first. Existing
IDs/codes and all business references are unchanged. Custom-name conflicts stop
before the first write; no custom item is merged, renamed, deleted or relinked.
Frozen snapshots below must not depend on the evolving application catalog.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import bindparam

revision: str = "schema_v2_s009"
down_revision: str | None = "schema_v2_s008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S008_ROWS = {
    "relationship_types": (
        ("relationship.self", "我", 1),
        ("relationship.relative", "亲戚", 2),
        ("relationship.friend", "朋友", 3),
        ("relationship.classmate", "同学", 4),
        ("relationship.colleague", "同事", 5),
        ("relationship.neighbor", "邻居", 6),
        ("relationship.military_comrade", "战友", 7),
        ("relationship.business_partner", "生意伙伴", 8),
        ("relationship.other", "其他", 9),
    ),
    "kinship_types": (
        ("kinship.self", "本人", 1),
        ("kinship.spouse", "之配偶", 2),
        ("kinship.father", "之父亲", 3),
        ("kinship.mother", "之母亲", 4),
        ("kinship.wife_father", "之岳父", 5),
        ("kinship.wife_mother", "之岳母", 6),
        ("kinship.husband_father", "之公公", 7),
        ("kinship.husband_mother", "之婆婆", 8),
        ("kinship.first_son", "之长子", 9),
        ("kinship.second_son", "之次子", 10),
        ("kinship.first_daughter", "之长女", 11),
        ("kinship.second_daughter", "之次女", 12),
        ("kinship.paternal_grandfather", "之祖父（爷爷）", 13),
        ("kinship.paternal_grandmother", "之祖母（奶奶）", 14),
        ("kinship.maternal_grandfather", "之外祖父（外公）", 15),
        ("kinship.maternal_grandmother", "之外祖母（外婆）", 16),
    ),
    "event_types": (
        ("event.wedding", "婚礼", 1),
        ("event.newborn", "新生儿（满月／百日）", 2),
        ("event.visit_patient", "探病慰问", 3),
        ("event.funeral", "白事（丧葬）", 4),
        ("event.housewarming", "乔迁", 5),
        ("event.education", "升学", 6),
        ("event.business_opening", "开业", 7),
        ("event.new_year_visit", "拜年", 8),
        ("event.birthday", "生日／寿宴", 9),
    ),
}
RENAMES = {
    "kinship_types": (
        ("kinship.paternal_grandfather", "之祖父（爷爷）", "之祖父"),
        ("kinship.paternal_grandmother", "之祖母（奶奶）", "之祖母"),
        ("kinship.maternal_grandfather", "之外祖父（外公）", "之外祖父"),
        ("kinship.maternal_grandmother", "之外祖母（外婆）", "之外祖母"),
    ),
    "event_types": (
        ("event.newborn", "新生儿（满月／百日）", "新生儿"),
        ("event.funeral", "白事（丧葬）", "白事"),
        ("event.birthday", "生日／寿宴", "生日"),
    ),
}
NEW_CODE = "relationship.fellow_townsman"
NEW_NAME = "同乡"
NEW_ORDER = 9
S009_ROWS = {
    table: tuple(
        (
            code,
            next((new for changed, _old, new in RENAMES.get(table, ()) if code == changed), name),
            10 if code == "relationship.other" else order,
        )
        for code, name, order in rows
    )
    + (((NEW_CODE, NEW_NAME, NEW_ORDER),) if table == "relationship_types" else ())
    for table, rows in S008_ROWS.items()
}


def _preflight_system_rows(bind, *, downgrade: bool = False) -> None:
    for table, rows in S008_ROWS.items():
        actual = {
            str(row.system_code): (
                int(row.id),
                str(row.type_name),
                int(row.sort_order),
                int(row.is_active),
            )
            for row in bind.execute(
                sa.text(
                    f"SELECT id, system_code, type_name, sort_order, is_active FROM {table} "
                    "WHERE user_id IS NULL ORDER BY sort_order, id"
                )
            )
        }
        old = {code: (name, order) for code, name, order in rows}
        new = {code: (name, order) for code, name, order in S009_ROWS[table]}
        # Accept complete or individually applied changes to permit safe retry.
        allowed_codes = set(new)
        required_codes = set(new) if downgrade else set(old)
        if not required_codes <= set(actual) <= allowed_codes:
            raise RuntimeError(f"S009_SYSTEM_DICTIONARY_PREFLIGHT_FAILED：{table} 系统编码发生漂移")
        for code, (item_id, name, order, active) in actual.items():
            if active != 1 or (name, order) not in {old.get(code), new.get(code)}:
                raise RuntimeError(
                    f"S009_SYSTEM_DICTIONARY_PREFLIGHT_FAILED：{table} 的 {code} "
                    "名称、顺序或状态异常"
                )
            if code == "relationship.self" and item_id != 1:
                raise RuntimeError(
                    "S009_SYSTEM_DICTIONARY_PREFLIGHT_FAILED：本人系统关系编号必须为1"
                )


def custom_conflicts(bind, *, downgrade: bool = False) -> list[dict]:
    """Read-only conflict IDs; business contents and usernames are not returned."""
    names_by_table = {
        table: tuple(old if downgrade else new for _code, old, new in rows)
        for table, rows in RENAMES.items()
    }
    if not downgrade:
        names_by_table["relationship_types"] = (NEW_NAME,)
    conflicts = []
    for table, names in names_by_table.items():
        statement = sa.text(
            f"SELECT user_id, id FROM {table} WHERE user_id IS NOT NULL "
            "AND name_key IN :names ORDER BY user_id, id"
        ).bindparams(bindparam("names", expanding=True))
        conflicts.extend(
            {"table": table, "user_id": int(row.user_id), "item_id": int(row.id)}
            for row in bind.execute(statement, {"names": names})
        )
    return conflicts


def preflight(bind, *, downgrade: bool = False) -> None:
    _preflight_system_rows(bind, downgrade=downgrade)
    conflicts = custom_conflicts(bind, downgrade=downgrade)
    if conflicts:
        summary = "；".join(
            f"{item['table']} 用户{item['user_id']} 项目{item['item_id']}"
            for item in conflicts[:20]
        )
        raise RuntimeError(
            f"S009_CUSTOM_DICTIONARY_CONFLICT：共{len(conflicts)}项；{summary}；不会自动合并或改名"
        )
    if downgrade:
        used = bind.scalar(
            sa.text(
                "SELECT COUNT(*) FROM contacts AS c INNER JOIN relationship_types AS d "
                "ON d.id = c.relation_type_id WHERE d.user_id IS NULL AND d.system_code = :code"
            ),
            {"code": NEW_CODE},
        )
        if used:
            raise RuntimeError(
                "S009_DOWNGRADE_ITEM_IN_USE：同乡已被联系人使用，不能删除或改指历史引用"
            )


def _apply_names(bind, *, downgrade: bool = False) -> None:
    for table, rows in RENAMES.items():
        for code, old, new in rows:
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET type_name = :name "
                    "WHERE user_id IS NULL AND system_code = :code"
                ),
                {"name": old if downgrade else new, "code": code},
            )
    bind.execute(
        sa.text(
            "UPDATE relationship_types SET sort_order = :sort_order "
            "WHERE user_id IS NULL AND system_code = 'relationship.other'"
        ),
        {"sort_order": 9 if downgrade else 10},
    )


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S009 包含字典预检，只允许连接数据库在线执行")
    bind = op.get_bind()
    preflight(bind)
    _apply_names(bind)
    bind.execute(
        sa.text(
            "INSERT INTO relationship_types "
            "(user_id, system_code, type_name, sort_order, is_active) "
            "SELECT NULL, :code, :name, :sort_order, 1 WHERE NOT EXISTS "
            "(SELECT 1 FROM relationship_types WHERE system_code = :code)"
        ),
        {"code": NEW_CODE, "name": NEW_NAME, "sort_order": NEW_ORDER},
    )
    _preflight_system_rows(bind, downgrade=True)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S009 包含降级预检，只允许连接数据库在线执行")
    bind = op.get_bind()
    preflight(bind, downgrade=True)
    _apply_names(bind, downgrade=True)
    bind.execute(
        sa.text("DELETE FROM relationship_types WHERE user_id IS NULL AND system_code = :code"),
        {"code": NEW_CODE},
    )
