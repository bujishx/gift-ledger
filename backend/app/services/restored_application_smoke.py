# ruff: noqa: RUF002
"""在隔离恢复库中执行不依赖既有账号密码的应用层冒烟验证。"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.db.models import EventType, KinshipType, RelationshipType
from app.schemas.ledger import LedgerDirection
from app.schemas.ledger_queries import LedgerQueryFilters
from app.services.auth import authenticate_user, register_user
from app.services.contacts import create_contact
from app.services.database_backup import MysqlConnection
from app.services.database_restore import RestoreError, validate_restore_target
from app.services.event_identity import event_identity_hash
from app.services.home import home_summary
from app.services.ledger import GiftAlreadyExistsError, create_ledger_entry
from app.services.ledger_queries import query_gifts
from app.services.profile import create_self_profile
from app.services.statistics import timeline_statistics


def _system_item_id(session: Session, model, *, exclude_id: int | None = None) -> int:
    statement = select(model.id).where(model.user_id.is_(None), model.is_active == 1)
    if exclude_id is not None:
        statement = statement.where(model.id != exclude_id)
    item_id = session.scalar(statement.order_by(model.id).limit(1))
    if item_id is None:
        raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库缺少应用冒烟所需的系统字典")
    return int(item_id)


def run_restored_application_smoke(
    *,
    connection: MysqlConnection,
    target: str,
    connect_timeout: int,
) -> None:
    """验证登录、首页、查账、统计、记账和用户隔离；只允许测试库前缀。"""

    validate_restore_target(target, allow_test=True)
    url = URL.create(
        drivername="mysql+pymysql",
        username=connection.user or None,
        password=connection.password,
        host=connection.host,
        port=connection.port,
        database=target,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": connect_timeout},
    )
    suffix = secrets.token_hex(6)
    owner_username = f"smoke_owner_{suffix}"
    other_username = f"smoke_other_{suffix}"
    password = f"Restore-Smoke-{secrets.token_urlsafe(18)}"
    smoke_date = date.today()

    try:
        with Session(engine) as session:
            relationship_id = _system_item_id(session, RelationshipType, exclude_id=1)
            kinship_id = _system_item_id(session, KinshipType)
            event_type_id = _system_item_id(session, EventType)
            session.rollback()

            owner = register_user(session, owner_username, password)
            create_self_profile(session, owner.id, "恢复演练本人甲", None)
            counterparty = create_contact(
                session,
                owner.id,
                name="恢复演练联系人",
                relationship_type_id=relationship_id,
                location="隔离测试库",
                phone=None,
                remark=suffix,
            )
            other = register_user(session, other_username, password)
            create_self_profile(session, other.id, "恢复演练本人乙", None)

            authenticated = authenticate_user(session, owner_username, password)
            if authenticated.id != owner.id:
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库登录冒烟失败")

            created = create_ledger_entry(
                session,
                owner.id,
                host_contact_id=counterparty.id,
                kinship_type_id=kinship_id,
                event_type_id=event_type_id,
                event_date=smoke_date,
                event_remark="隔离恢复演练",
                giver_contact_id=None,
                amount=Decimal("1.00"),
                gift_details=None,
                gift_remark="恢复演练自动生成",
            )
            if created.direction is not LedgerDirection.EXPENSE:
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库记账方向冒烟失败")

            # S008 keeps the event/giver unique even when the supplied date changes.
            original_event_id = created.event.id
            original_gift_id = created.gift.id
            if created.event.identity_hash != event_identity_hash(
                counterparty.id, kinship_id, event_type_id, "隔离恢复演练"
            ):
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库事件身份指纹冒烟失败")
            try:
                create_ledger_entry(
                    session,
                    owner.id,
                    host_contact_id=counterparty.id,
                    kinship_type_id=kinship_id,
                    event_type_id=event_type_id,
                    event_date=smoke_date + timedelta(days=1),
                    event_remark=" 隔离恢复演练 ",
                    giver_contact_id=None,
                    amount=Decimal("2.00"),
                    gift_details=None,
                    gift_remark=None,
                )
            except GiftAlreadyExistsError as error:
                if error.gift_id != original_gift_id:
                    raise RestoreError(
                        "RESTORE_SMOKE_FAILED", "恢复库重复记账定位冒烟失败"
                    ) from error
            else:
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库重复记账保护冒烟失败")
            distinct = create_ledger_entry(
                session,
                owner.id,
                host_contact_id=counterparty.id,
                kinship_type_id=kinship_id,
                event_type_id=event_type_id,
                event_date=smoke_date,
                event_remark="隔离恢复演练第二次",
                giver_contact_id=None,
                amount=Decimal("1.00"),
                gift_details=None,
                gift_remark=None,
            )
            if distinct.event.id == original_event_id:
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库不同备注事件区分冒烟失败")

            summary = home_summary(session, owner.id, smoke_date.year, today=smoke_date)
            if summary.annual.expense_amount < Decimal("1.00"):
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库首页冒烟失败")

            gifts = query_gifts(session, owner.id, LedgerQueryFilters())
            if gifts.page.total < 1:
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库查账冒烟失败")

            timeline = timeline_statistics(session, owner.id, smoke_date.year)
            if timeline.year_expense_amount < Decimal("1.00"):
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库统计冒烟失败")

            other_gifts = query_gifts(session, other.id, LedgerQueryFilters())
            if other_gifts.page.total != 0:
                raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库用户隔离冒烟失败")
    except RestoreError:
        raise
    except Exception as exc:
        raise RestoreError("RESTORE_SMOKE_FAILED", "恢复库应用冒烟失败") from exc
    finally:
        engine.dispose()


__all__ = ["run_restored_application_smoke"]
