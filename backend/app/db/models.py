from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, mysql_table_options
from app.services.event_dates import EVENT_DATE_CHECK, date_value, event_date_fields
from app.services.event_identity import event_identity_hash, event_identity_hash_default

RESTRICT = "RESTRICT"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        CheckConstraint(
            "CHAR_LENGTH(`username`) BETWEEN 4 AND 30 "
            "AND ASCII(LEFT(`username`, 1)) BETWEEN 97 AND 122 "
            "AND NOT REGEXP_LIKE(`username`, '[^a-z0-9_]', 'c')",
            name="ck_users_username",
        ),
        CheckConstraint(
            "REGEXP_LIKE(`password_hash`, '[^[:space:]]')",
            name="ck_users_password_hash",
        ),
        CheckConstraint("`is_admin` IN (0, 1)", name="ck_users_admin"),
        CheckConstraint("`is_active` IN (0, 1)", name="ck_users_active"),
        mysql_table_options("登录账号"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="登录名，后端归一化为小写后写入"
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="密码哈希；不保存明文"
    )
    is_admin: Mapped[int] = mapped_column(mysql.TINYINT, nullable=False, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(mysql.TINYINT, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        mysql.TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class RelationshipType(Base):
    __tablename__ = "relationship_types"
    __table_args__ = (
        UniqueConstraint("scope_key", "name_key", name="uq_relationship_scope_name"),
        UniqueConstraint("system_code", name="uq_relationship_system_code"),
        Index(
            "idx_relationship_user_active_sort",
            "user_id",
            "is_active",
            "sort_order",
        ),
        CheckConstraint(
            "`type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50",
            name="ck_relationship_name",
        ),
        CheckConstraint("`is_active` IN (0, 1)", name="ck_relationship_active"),
        CheckConstraint(
            "(`user_id` IS NULL AND `system_code` IS NOT NULL) OR "
            "(`user_id` IS NOT NULL AND `system_code` IS NULL)",
            name="ck_relationship_system_owner",
        ),
        CheckConstraint(
            "`system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')",
            name="ck_relationship_system_code",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_relationship_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("关系类型：系统预置及用户自定义"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        mysql.INTEGER, nullable=True, comment="NULL=系统内置，非NULL=用户自定义"
    )
    system_code: Mapped[str | None] = mapped_column(
        mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"),
        nullable=True,
        comment="系统内置项稳定标识；用户自定义项必须为空",
    )
    type_name: Mapped[str] = mapped_column(String(50), nullable=False)
    sort_order: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(mysql.TINYINT, nullable=False, server_default=text("1"))
    scope_key: Mapped[int | None] = mapped_column(
        mysql.INTEGER(unsigned=True),
        Computed("COALESCE(`user_id`, 0)", persisted=False),
        nullable=True,
    )
    name_key: Mapped[str | None] = mapped_column(
        mysql.VARCHAR(50, charset="utf8mb4", collation="utf8mb4_0900_as_ci"),
        Computed("TRIM(`type_name`)", persisted=False),
        nullable=True,
    )


class KinshipType(Base):
    __tablename__ = "kinship_types"
    __table_args__ = (
        UniqueConstraint("scope_key", "name_key", name="uq_kinship_scope_name"),
        UniqueConstraint("system_code", name="uq_kinship_system_code"),
        Index("idx_kinship_user_active_sort", "user_id", "is_active", "sort_order"),
        CheckConstraint(
            "`type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50",
            name="ck_kinship_name",
        ),
        CheckConstraint("`is_active` IN (0, 1)", name="ck_kinship_active"),
        CheckConstraint(
            "(`user_id` IS NULL AND `system_code` IS NOT NULL) OR "
            "(`user_id` IS NOT NULL AND `system_code` IS NULL)",
            name="ck_kinship_system_owner",
        ),
        CheckConstraint(
            "`system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')",
            name="ck_kinship_system_code",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_kinship_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("亲属称谓：系统预置及用户自定义"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        mysql.INTEGER, nullable=True, comment="NULL=系统内置，非NULL=用户自定义"
    )
    system_code: Mapped[str | None] = mapped_column(
        mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"),
        nullable=True,
        comment="系统内置项稳定标识；用户自定义项必须为空",
    )
    type_name: Mapped[str] = mapped_column(String(50), nullable=False)
    sort_order: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(mysql.TINYINT, nullable=False, server_default=text("1"))
    scope_key: Mapped[int | None] = mapped_column(
        mysql.INTEGER(unsigned=True),
        Computed("COALESCE(`user_id`, 0)", persisted=False),
        nullable=True,
    )
    name_key: Mapped[str | None] = mapped_column(
        mysql.VARCHAR(50, charset="utf8mb4", collation="utf8mb4_0900_as_ci"),
        Computed("TRIM(`type_name`)", persisted=False),
        nullable=True,
    )


class EventType(Base):
    __tablename__ = "event_types"
    __table_args__ = (
        UniqueConstraint("scope_key", "name_key", name="uq_event_type_scope_name"),
        UniqueConstraint("system_code", name="uq_event_type_system_code"),
        Index("idx_event_type_user_active_sort", "user_id", "is_active", "sort_order"),
        CheckConstraint(
            "`type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50",
            name="ck_event_type_name",
        ),
        CheckConstraint("`is_active` IN (0, 1)", name="ck_event_type_active"),
        CheckConstraint(
            "(`user_id` IS NULL AND `system_code` IS NOT NULL) OR "
            "(`user_id` IS NOT NULL AND `system_code` IS NULL)",
            name="ck_event_type_system_owner",
        ),
        CheckConstraint(
            "`system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')",
            name="ck_event_type_system_code",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_event_type_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("事件类型：系统预置及用户自定义"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        mysql.INTEGER, nullable=True, comment="NULL=系统内置，非NULL=用户自定义"
    )
    system_code: Mapped[str | None] = mapped_column(
        mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"),
        nullable=True,
        comment="系统内置项稳定标识；用户自定义项必须为空",
    )
    type_name: Mapped[str] = mapped_column(String(50), nullable=False)
    sort_order: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(mysql.TINYINT, nullable=False, server_default=text("1"))
    scope_key: Mapped[int | None] = mapped_column(
        mysql.INTEGER(unsigned=True),
        Computed("COALESCE(`user_id`, 0)", persisted=False),
        nullable=True,
    )
    name_key: Mapped[str | None] = mapped_column(
        mysql.VARCHAR(50, charset="utf8mb4", collation="utf8mb4_0900_as_ci"),
        Computed("TRIM(`type_name`)", persisted=False),
        nullable=True,
    )


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_contacts_user_id"),
        UniqueConstraint("self_user_id", name="uq_contacts_one_self"),
        UniqueConstraint("user_id", "identity_hash", name="uq_contacts_user_identity"),
        Index("idx_contacts_user_relation", "user_id", "relation_type_id"),
        Index("idx_contacts_user_active_name", "user_id", "is_active", "name"),
        Index("idx_contacts_relation_fk", "relation_type_id"),
        CheckConstraint("REGEXP_LIKE(`name`, '[^[:space:]]')", name="ck_contacts_name"),
        CheckConstraint("`is_active` IN (0, 1)", name="ck_contacts_active"),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_contacts_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        ForeignKeyConstraint(
            ["relation_type_id"],
            ["relationship_types.id"],
            name="fk_contacts_relation",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("用户独立联系人"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="真实姓名，可重名")
    relation_type_id: Mapped[int] = mapped_column(
        mysql.INTEGER, nullable=False, comment="关系类型，ID=1表示本人"
    )
    location: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phone: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="电话，入库前移除全部空白字符"
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_hash: Mapped[bytes] = mapped_column(
        mysql.BINARY(32),
        nullable=False,
        comment="服务端按规范化姓名和联系人备注生成的内部身份指纹",
    )
    is_active: Mapped[int] = mapped_column(mysql.TINYINT, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        mysql.TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    self_user_id: Mapped[int | None] = mapped_column(
        mysql.INTEGER,
        Computed(
            "CASE WHEN `relation_type_id` = 1 THEN `user_id` ELSE NULL END",
            persisted=False,
        ),
        nullable=True,
        comment="内部本人唯一键，不可手工填写",
    )


class EventRecord(Base):
    __tablename__ = "event_records"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_events_user_id"),
        UniqueConstraint(
            "user_id",
            "identity_hash",
            name="uq_events_user_identity",
        ),
        Index("idx_events_user_host", "user_id", "contact_id"),
        Index("idx_events_user_date", "user_id", "event_date"),
        Index("idx_events_user_type", "user_id", "event_type_id"),
        Index("idx_events_relative_fk", "relative_type_id"),
        Index("idx_events_type_fk", "event_type_id"),
        CheckConstraint(EVENT_DATE_CHECK, name="ck_events_date"),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_events_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        ForeignKeyConstraint(
            ["user_id", "contact_id"],
            ["contacts.user_id", "contacts.id"],
            name="fk_events_host",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        ForeignKeyConstraint(
            ["relative_type_id"],
            ["kinship_types.id"],
            name="fk_events_relative",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        ForeignKeyConstraint(
            ["event_type_id"],
            ["event_types.id"],
            name="fk_events_type",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("用户独立事件"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    contact_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False, comment="事主、收礼方")
    relative_type_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    event_type_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'day'")
    )
    event_year: Mapped[int | None] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=True)
    event_month: Mapped[int | None] = mapped_column(mysql.TINYINT(unsigned=True), nullable=True)
    date_source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'web'")
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_hash: Mapped[bytes] = mapped_column(
        mysql.BINARY(32),
        nullable=False,
        default=event_identity_hash_default,
        comment="服务端按事主、亲属称谓、事件类型和首尾去空白备注生成的内部身份指纹",
    )
    created_at: Mapped[datetime] = mapped_column(
        mysql.TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


@event.listens_for(EventRecord, "before_insert")
@event.listens_for(EventRecord, "before_update")
def _set_event_identity_hash(_mapper, _connection, target: EventRecord) -> None:
    # Old complete-date callers need no new arguments; partial dates only come from trusted import.
    if not target.date_precision:
        target.date_precision = "day"
    if not target.date_source:
        target.date_source = "web"
    if target.date_precision == "day":
        target.event_year = target.event_date.year if target.event_date else None
        target.event_month = target.event_date.month if target.event_date else None
    values = event_date_fields(date_value(target), source=target.date_source)
    for key, value in values.items():
        setattr(target, key, value)
    target.identity_hash = event_identity_hash(
        target.contact_id, target.relative_type_id, target.event_type_id, target.remark
    )


class GiftRecord(Base):
    __tablename__ = "gift_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "event_record_id",
            "contact_id",
            name="uq_gifts_user_event_giver",
        ),
        Index("idx_gifts_user_giver", "user_id", "contact_id"),
        CheckConstraint("`amount` >= 0", name="ck_gifts_amount"),
        CheckConstraint(
            "`amount` > 0 OR (`gift_details` IS NOT NULL "
            "AND REGEXP_LIKE(`gift_details`, '[^[:space:]]'))",
            name="ck_gifts_cash_or_goods",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_gifts_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        ForeignKeyConstraint(
            ["user_id", "event_record_id"],
            ["event_records.user_id", "event_records.id"],
            name="fk_gifts_event",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        ForeignKeyConstraint(
            ["user_id", "contact_id"],
            ["contacts.user_id", "contacts.id"],
            name="fk_gifts_giver",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("用户独立随礼流水"),
    )

    id: Mapped[int] = mapped_column(mysql.INTEGER, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    event_record_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    contact_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False, comment="随礼人、送礼方")
    amount: Mapped[Decimal] = mapped_column(
        mysql.DECIMAL(10, 2), nullable=False, server_default=text("'0.00'")
    )
    gift_details: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        mysql.TIMESTAMP,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="系统录入时间，不作为收支统计日期",
    )


class LegacyImportBatch(Base):
    __tablename__ = "legacy_import_batches"
    __table_args__ = (
        UniqueConstraint("user_id", "batch_id", name="uq_import_batches_user_batch"),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_import_batches_user",
            ondelete=RESTRICT,
            onupdate=RESTRICT,
        ),
        mysql_table_options("用户旧账导入完成结果，用于幂等重试"),
    )
    id: Mapped[int] = mapped_column(mysql.BIGINT, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(mysql.INTEGER, nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        mysql.TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


__all__ = [
    "Contact",
    "EventRecord",
    "EventType",
    "GiftRecord",
    "KinshipType",
    "LegacyImportBatch",
    "RelationshipType",
    "User",
]
