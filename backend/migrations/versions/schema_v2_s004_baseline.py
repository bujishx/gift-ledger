"""登记经确认的 S004 Schema 基线。

Revision ID: schema_v2_s004
Revises: None
Create Date: 2026-09-26

本文件不创建业务表。新库先人工执行 S004 并核对结构, 然后使用
`alembic stamp schema_v2_s004` 登记版本。
"""

from collections.abc import Sequence

revision: str = "schema_v2_s004"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASELINE_MESSAGE = (
    "S004 基线不能通过 alembic upgrade 自动建库。请先人工执行并核对 "
    "历史 S004 SQL 后再运行 alembic stamp schema_v2_s004。当前新库 "
    "schema_v2_multiuser.sql 是 S008，请核对后登记 schema_v2_s008，勿登记为 S004。"
)


def upgrade() -> None:
    raise RuntimeError(_BASELINE_MESSAGE)


def downgrade() -> None:
    raise RuntimeError("S004 基线不支持自动降级；请使用已确认的备份和人工回退方案。")
