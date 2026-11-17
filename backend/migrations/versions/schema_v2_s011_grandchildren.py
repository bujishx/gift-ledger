"""Add the generic grandchildren kinship without changing existing identities.

Revision ID: schema_v2_s011
Revises: schema_v2_s010
Create Date: 2026-10-08

Stop writers and verify a backup first. Conflicts abort before any write; custom
items are never merged or relinked. S009's frozen catalog is reused as history,
never the evolving application's catalog.
"""

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import context, op

revision: str = "schema_v2_s011"
down_revision: str | None = "schema_v2_s010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S010_ROWS = import_module("migrations.versions.schema_v2_s009_dictionary_labels").S009_ROWS
NEW_CODE = "kinship.grandchildren"
NEW_NAME = "之孙辈"
NEW_ORDER = 13
S011_ROWS = {
    table: tuple((code, name, order + 1 if table == "kinship_types" and order >= 13 else order)
                 for code, name, order in rows)
    + (((NEW_CODE, NEW_NAME, NEW_ORDER),) if table == "kinship_types" else ())
    for table, rows in S010_ROWS.items()
}


def _preflight_system_rows(bind, *, downgrade: bool = False) -> None:
    for table, rows in S010_ROWS.items():
        actual = {
            str(row.system_code): (int(row.id), str(row.type_name), int(row.sort_order),
                                   int(row.is_active))
            for row in bind.execute(sa.text(
                f"SELECT id, system_code, type_name, sort_order, is_active FROM {table} "
                "WHERE user_id IS NULL ORDER BY sort_order, id"
            ))
        }
        old = {code: (name, order) for code, name, order in rows}
        new = {code: (name, order) for code, name, order in S011_ROWS[table]}
        required = set(new) if downgrade else set(old)
        if not required <= set(actual) <= set(new):
            raise RuntimeError(f"S011_SYSTEM_DICTIONARY_PREFLIGHT_FAILED: {table} codes drifted")
        for code, (item_id, name, order, active) in actual.items():
            if active != 1 or (name, order) not in {old.get(code), new.get(code)}:
                raise RuntimeError(f"S011_SYSTEM_DICTIONARY_PREFLIGHT_FAILED: {table} {code}")
            if code == "relationship.self" and item_id != 1:
                raise RuntimeError("S011_SYSTEM_DICTIONARY_PREFLIGHT_FAILED: self id must be 1")


def custom_conflicts(bind) -> list[dict]:
    return [{"table": "kinship_types", "user_id": int(row.user_id), "item_id": int(row.id)}
            for row in bind.execute(sa.text(
                "SELECT user_id, id FROM kinship_types WHERE user_id IS NOT NULL "
                "AND name_key = :name ORDER BY user_id, id"
            ), {"name": NEW_NAME})]


def preflight(bind, *, downgrade: bool = False) -> None:
    _preflight_system_rows(bind, downgrade=downgrade)
    if not downgrade and (conflicts := custom_conflicts(bind)):
        raise RuntimeError(f"S011_CUSTOM_DICTIONARY_CONFLICT: {len(conflicts)} items; no writes")
    if downgrade and bind.scalar(sa.text(
        "SELECT COUNT(*) FROM event_records AS e INNER JOIN kinship_types AS d "
        "ON d.id = e.relative_type_id WHERE d.user_id IS NULL AND d.system_code = :code"
    ), {"code": NEW_CODE}):
        raise RuntimeError("S011_DOWNGRADE_ITEM_IN_USE: grandchildren has event references")


def _apply_orders(bind, *, downgrade: bool = False) -> None:
    for code, _name, order in S010_ROWS["kinship_types"]:
        if order >= 13:
            bind.execute(sa.text(
                "UPDATE kinship_types SET sort_order = :sort_order "
                "WHERE user_id IS NULL AND system_code = :code"
            ), {"sort_order": order if downgrade else order + 1, "code": code})


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S011 requires online preflight")
    bind = op.get_bind()
    preflight(bind)
    _apply_orders(bind)
    bind.execute(sa.text(
        "INSERT INTO kinship_types (user_id, system_code, type_name, sort_order, is_active) "
        "SELECT NULL, :code, :name, :sort_order, 1 WHERE NOT EXISTS "
        "(SELECT 1 FROM kinship_types WHERE system_code = :code)"
    ), {"code": NEW_CODE, "name": NEW_NAME, "sort_order": NEW_ORDER})
    _preflight_system_rows(bind, downgrade=True)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S011 requires online downgrade preflight")
    bind = op.get_bind()
    preflight(bind, downgrade=True)
    bind.execute(sa.text(
        "DELETE FROM kinship_types WHERE user_id IS NULL AND system_code = :code"
    ), {"code": NEW_CODE})
    _apply_orders(bind, downgrade=True)
