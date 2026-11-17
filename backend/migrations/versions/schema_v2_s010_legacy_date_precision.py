"""Preserve legacy date precision without inventing dates.

Revision ID: schema_v2_s010
Revises: schema_v2_s009
Create Date: 2026-10-07

Run only after a verified backup and stopping writers. DDL is resumable; old
events and gifts retain IDs, identity hashes and all original business columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

# Frozen S010 SQL; never import mutable application rules into historical migrations.
EVENT_DATE_CHECK = (
    "`date_source` IN ('web', 'legacy_import') AND "
    "(`date_source` = 'legacy_import' OR `date_precision` = 'day') AND ("
    "(`date_precision` = 'day' AND `event_date` IS NOT NULL "
    "AND `event_date` >= '1000-01-01' AND LAST_DAY(`event_date`) IS NOT NULL "
    "AND DAYOFMONTH(`event_date`) BETWEEN 1 AND DAYOFMONTH(LAST_DAY(`event_date`)) "
    "AND `event_year` IS NOT NULL AND `event_year` = YEAR(`event_date`) "
    "AND `event_month` IS NOT NULL AND `event_month` = MONTH(`event_date`)) OR "
    "(`date_precision` = 'month' AND `event_date` IS NULL "
    "AND `event_year` IS NOT NULL AND `event_year` BETWEEN 1000 AND 9999 "
    "AND `event_month` IS NOT NULL AND `event_month` BETWEEN 1 AND 12) OR "
    "(`date_precision` = 'year' AND `event_date` IS NULL "
    "AND `event_year` IS NOT NULL AND `event_year` BETWEEN 1000 AND 9999 "
    "AND `event_month` IS NULL) OR "
    "(`date_precision` = 'unknown' AND `event_date` IS NULL "
    "AND `event_year` IS NULL AND `event_month` IS NULL))"
)

revision: str = "schema_v2_s010"
down_revision: str | None = "schema_v2_s009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_DATE_CHECK = (
    "`event_date` >= '1000-01-01' AND LAST_DAY(`event_date`) IS NOT NULL "
    "AND DAYOFMONTH(`event_date`) BETWEEN 1 AND DAYOFMONTH(LAST_DAY(`event_date`))"
)


def _state(bind):
    inspector = sa.inspect(bind)
    return {column["name"]: column for column in inspector.get_columns("event_records")}


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S010 日期预检需要连接数据库在线执行")
    bind = op.get_bind()
    columns = _state(bind)
    invalid = bind.execute(
        sa.text(
            "SELECT id FROM event_records WHERE event_date IS NOT NULL AND NOT ("
            + OLD_DATE_CHECK
            + ") LIMIT 1"
        )
    ).scalar()
    if invalid is not None:
        raise RuntimeError(
            f"S010_DATE_PREFLIGHT_FAILED：事件{invalid}日期不正确，迁移不会修改业务日期"
        )
    definitions = (
        sa.Column("date_precision", sa.String(7), nullable=False, server_default=sa.text("'day'")),
        sa.Column("event_year", mysql.SMALLINT(unsigned=True), nullable=True),
        sa.Column("event_month", mysql.TINYINT(unsigned=True), nullable=True),
        sa.Column("date_source", sa.String(20), nullable=False, server_default=sa.text("'web'")),
    )
    for column in definitions:
        if column.name not in columns:
            op.add_column("event_records", column)
    # Only populate newly introduced derived components; no original column is rewritten.
    bind.execute(
        sa.text(
            "UPDATE event_records SET event_year=YEAR(event_date), event_month=MONTH(event_date) "
            "WHERE date_precision='day' AND event_date IS NOT NULL "
            "AND (event_year IS NULL OR event_month IS NULL)"
        )
    )
    op.alter_column("event_records", "event_date", existing_type=sa.Date(), nullable=True)
    checks = {item["name"] for item in sa.inspect(bind).get_check_constraints("event_records")}
    if "ck_events_date" in checks:
        op.drop_constraint("ck_events_date", "event_records", type_="check")
    op.create_check_constraint("ck_events_date", "event_records", EVENT_DATE_CHECK)
    if "legacy_import_batches" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "legacy_import_batches",
            sa.Column("id", mysql.BIGINT(), primary_key=True, autoincrement=True),
            sa.Column("user_id", mysql.INTEGER(), nullable=False),
            sa.Column("batch_id", sa.String(64), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                mysql.TIMESTAMP(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("user_id", "batch_id", name="uq_import_batches_user_batch"),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="fk_import_batches_user",
                ondelete="RESTRICT",
                onupdate="RESTRICT",
            ),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_0900_bin",
            comment="用户旧账导入完成结果，用于幂等重试",
        )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("S010 降级预检需要连接数据库在线执行")
    bind = op.get_bind()
    if (
        bind.execute(
            sa.text(
                "SELECT id FROM event_records WHERE date_precision<>'day' "
                "OR event_date IS NULL LIMIT 1"
            )
        ).scalar()
        is not None
    ):
        raise RuntimeError(
            "S010_DOWNGRADE_PARTIAL_DATES：存在不完整旧账日期，不能有损降级或编造日期"
        )
    if bind.execute(sa.text("SELECT id FROM legacy_import_batches LIMIT 1")).scalar() is not None:
        raise RuntimeError("S010_DOWNGRADE_BATCH_HISTORY：存在导入结果记录，不能丢失幂等历史")
    op.drop_table("legacy_import_batches")
    op.drop_constraint("ck_events_date", "event_records", type_="check")
    op.alter_column("event_records", "event_date", existing_type=sa.Date(), nullable=False)
    for name in ("date_source", "event_month", "event_year", "date_precision"):
        op.drop_column("event_records", name)
    op.create_check_constraint("ck_events_date", "event_records", OLD_DATE_CHECK)
