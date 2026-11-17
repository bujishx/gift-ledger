# ruff: noqa: RUF002, RUF003
"""旧账确认导入的原子事务写入服务。

本模块把确认时重新分析得到的 :class:`~app.services.legacy_import_analysis.LegacyImportAnalysis`
写进数据库。所有新字典、新联系人、新事件和新礼金流水都必须在**同一个事务**里完成：

1. 先 ``SELECT ... FOR UPDATE`` 锁定当前用户；用户不存在或已停用立即回滚并报
   ``ACCOUNT_UNAVAILABLE``。
2. 在同一个确认事务里**重新调用** ``analyze_old_ledger_import``，绝不信任预览阶段保存的
   数据库编号或汇总结果。
3. 任一必修问题、未完成确认、无有效行或已审阅计划变化均立即回滚且零写入。
4. 按「明确确认的日期补齐 → 用户字典 → 联系人 → 事件 → 流水 → 批次结果」写入；
   为取得自增 ID 分层 ``flush()``，全流程只在最后成功时 ``commit()`` 一次。
   已完成批次的重试直接返回原结果，不重复新增。
5. 任意异常都执行 ``rollback()``；``IntegrityError`` 转换为 ``IMPORT_STATE_CHANGED``，
   不向用户暴露 SQL、路径或内部堆栈。

本模块不实现 HTTP 路由、不读取上传文件、不管理预览 token、不执行真实 MySQL、不修改 Schema
或 Alembic。它只接收解析好的 :class:`~app.services.old_ledger_excel.ParsedOldLedgerWorkbook`
并完成确认写入。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Contact,
    EventRecord,
    EventType,
    GiftRecord,
    KinshipType,
    LegacyImportBatch,
    RelationshipType,
    User,
)
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.legacy_import import LegacyImportConfirmResponse
from app.services.contact_identity import contact_identity_hash
from app.services.event_dates import date_value, event_date_fields
from app.services.event_identity import event_identity_hash
from app.services.legacy_import_analysis import (
    ContactReference,
    DictionaryReference,
    EventReference,
    LegacyImportAnalysis,
    analyze_old_ledger_import,
    legacy_import_plan_fingerprint,
    legacy_import_preview_response,
)
from app.services.old_ledger_excel import ParsedOldLedgerWorkbook

# ---------------------------------------------------------------- 稳定错误码

ERROR_ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
ERROR_IMPORT_VALIDATION_FAILED = "IMPORT_VALIDATION_FAILED"
ERROR_IMPORT_STATE_CHANGED = "IMPORT_STATE_CHANGED"
ERROR_IMPORT_COMMIT_FAILED = "IMPORT_COMMIT_FAILED"

# 三类字典模型映射，按 DictionaryCategory 取得对应 ORM 类。
_DICTIONARY_MODELS: dict[DictionaryCategory, type[Any]] = {
    DictionaryCategory.RELATIONSHIP: RelationshipType,
    DictionaryCategory.KINSHIP: KinshipType,
    DictionaryCategory.EVENT: EventType,
}

# 内部键类型别名，仅用于可读性。
_ContactKey = tuple[str, str]
_DictionaryKey = tuple[DictionaryCategory, str]
_EventKey = tuple[Any, ...]


class LegacyImportCommitError(ValueError):
    """确认导入失败。``analysis`` 仅在重新分析阶段失败时保留完整预检结果。"""

    def __init__(
        self,
        code: str,
        message: str,
        analysis: LegacyImportAnalysis | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.analysis = analysis


# ---------------------------------------------------------------- 引用解析


def _resolve_dictionary_id(
    reference: DictionaryReference,
    new_dictionary_ids: dict[_DictionaryKey, int],
) -> int:
    if reference.existing_id is not None:
        return reference.existing_id
    return new_dictionary_ids[(reference.category, reference.name)]


def _resolve_contact_id(
    reference: ContactReference,
    new_contact_ids: dict[_ContactKey, int],
) -> int:
    if reference.existing_id is not None:
        return reference.existing_id
    return new_contact_ids[reference.identity]


def _event_key(reference: EventReference) -> _EventKey:
    return (
        reference.host.identity,
        reference.kinship.name,
        reference.event_type.name,
        (reference.remark or "").strip(),
    )


def _resolve_event_id(
    reference: EventReference,
    new_event_ids: dict[_EventKey, int],
) -> int:
    if reference.existing_id is not None:
        return reference.existing_id
    return new_event_ids[_event_key(reference)]


# ---------------------------------------------------------------- 分层写入


def _create_dictionary_items(
    session: Session,
    user_id: int,
    analysis: LegacyImportAnalysis,
) -> dict[_DictionaryKey, int]:
    keyed: dict[_DictionaryKey, Any] = {}
    for item in analysis.new_dictionary_items:
        model = _DICTIONARY_MODELS[item.category]
        record = model(
            user_id=user_id,
            system_code=None,
            type_name=item.name,
            sort_order=0,
            is_active=1,
        )
        session.add(record)
        keyed[(item.category, item.name)] = record
    if keyed:
        session.flush()
    return {key: record.id for key, record in keyed.items()}


def _create_contacts(
    session: Session,
    user_id: int,
    analysis: LegacyImportAnalysis,
    new_dictionary_ids: dict[_DictionaryKey, int],
) -> dict[_ContactKey, int]:
    keyed: dict[_ContactKey, Any] = {}
    for reference in analysis.new_contacts:
        relation_type_id = _resolve_dictionary_id(reference.relationship, new_dictionary_ids)
        record = Contact(
            user_id=user_id,
            name=reference.name,
            relation_type_id=relation_type_id,
            location=reference.location,
            phone=None,
            remark=reference.remark,
            identity_hash=contact_identity_hash(
                reference.name, reference.location, reference.remark
            ),
            is_active=1,
        )
        session.add(record)
        keyed[reference.identity] = record
    if keyed:
        session.flush()
    return {key: record.id for key, record in keyed.items()}


def _create_events(
    session: Session,
    user_id: int,
    analysis: LegacyImportAnalysis,
    new_dictionary_ids: dict[_DictionaryKey, int],
    new_contact_ids: dict[_ContactKey, int],
) -> dict[_EventKey, int]:
    keyed: dict[_EventKey, Any] = {}
    for reference in analysis.new_events:
        host_id = _resolve_contact_id(reference.host, new_contact_ids)
        kinship_id = _resolve_dictionary_id(reference.kinship, new_dictionary_ids)
        event_type_id = _resolve_dictionary_id(reference.event_type, new_dictionary_ids)
        record = EventRecord(
            user_id=user_id,
            contact_id=host_id,
            relative_type_id=kinship_id,
            event_type_id=event_type_id,
            **event_date_fields(date_value(reference)),
            remark=reference.remark,
            identity_hash=event_identity_hash(host_id, kinship_id, event_type_id, reference.remark),
        )
        session.add(record)
        keyed[_event_key(reference)] = record
    if keyed:
        session.flush()
    return {key: record.id for key, record in keyed.items()}


def _create_gifts(
    session: Session,
    user_id: int,
    analysis: LegacyImportAnalysis,
    new_contact_ids: dict[_ContactKey, int],
    new_event_ids: dict[_EventKey, int],
) -> None:
    for plan in analysis.rows:
        event_id = _resolve_event_id(plan.event, new_event_ids)
        giver_id = _resolve_contact_id(plan.giver, new_contact_ids)
        record = GiftRecord(
            user_id=user_id,
            event_record_id=event_id,
            contact_id=giver_id,
            amount=plan.amount,
            gift_details=plan.gift_details,
            remark=plan.gift_remark,
        )
        session.add(record)
    if analysis.rows:
        session.flush()


# ---------------------------------------------------------------- 对外入口


def commit_old_ledger_import(
    session: Session,
    user_id: int,
    parsed: ParsedOldLedgerWorkbook,
    *,
    decisions: dict[str, str] | None = None,
    expected_fingerprint: str | None = None,
    batch_id: str | None = None,
) -> LegacyImportConfirmResponse:
    """确认旧账导入，把所有新数据放在一个事务里原子写入。

    任一步失败都会整批回滚，不留下半批数据；失败通过
    :class:`LegacyImportCommitError` 以稳定错误码对外暴露。
    """

    analysis: LegacyImportAnalysis | None = None
    try:
        # Authentication and the route's completed-batch lookup may already
        # have opened a REPEATABLE READ snapshot. The confirmation owns a new
        # transaction; discard that read-only snapshot before taking its lock.
        if session.in_transaction():
            session.rollback()
        session.expire_all()
        user = session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user is None or user.is_active != 1:
            raise LegacyImportCommitError(ERROR_ACCOUNT_UNAVAILABLE, "账号不可用，请重新登录")

        if batch_id:
            completed = get_completed_import(session, user_id, batch_id)
            if completed:
                session.rollback()
                return completed
        analysis = (
            analyze_old_ledger_import(session, user_id, parsed, decisions)
            if decisions
            else analyze_old_ledger_import(session, user_id, parsed)
        )
        if analysis.has_errors or analysis.valid_rows == 0:
            raise LegacyImportCommitError(
                ERROR_IMPORT_VALIDATION_FAILED,
                "导入预检未通过，请根据提示修正文件后重新上传",
                analysis,
            )
        if (
            expected_fingerprint is not None
            and legacy_import_plan_fingerprint(analysis) != expected_fingerprint
        ):
            raise LegacyImportCommitError(
                ERROR_IMPORT_STATE_CHANGED,
                "账本或最终导入计划已改变，请重新预览并核对后再确认",
                analysis,
            )

        for reference in analysis.date_completions:
            record = session.scalar(
                select(EventRecord)
                .where(EventRecord.user_id == user_id, EventRecord.id == reference.existing_id)
                .with_for_update()
            )
            if record is None:
                raise LegacyImportCommitError(
                    ERROR_IMPORT_STATE_CHANGED, "待补齐事件已改变，请重新预览", analysis
                )
            fields = event_date_fields(
                date_value(reference), source=record.date_source or "legacy_import"
            )
            for name, value in fields.items():
                setattr(record, name, value)
        new_dictionary_ids = _create_dictionary_items(session, user_id, analysis)
        new_contact_ids = _create_contacts(session, user_id, analysis, new_dictionary_ids)
        new_event_ids = _create_events(
            session, user_id, analysis, new_dictionary_ids, new_contact_ids
        )
        _create_gifts(session, user_id, analysis, new_contact_ids, new_event_ids)

        completed_at = datetime.now(UTC)
        result = LegacyImportConfirmResponse(
            imported_contacts=len(analysis.new_contacts),
            imported_dictionary_items=len(analysis.new_dictionary_items),
            imported_events=len(analysis.new_events),
            imported_gifts=analysis.new_gift_count,
            reused_contacts=len(analysis.reused_contact_ids),
            reused_events=len(analysis.reused_event_ids),
            completed_date_events=len(analysis.date_completions),
            batch_id=batch_id,
            completed_at=completed_at if batch_id else None,
            summary=legacy_import_preview_response(analysis).summary,
        )
        if batch_id:
            session.add(
                LegacyImportBatch(
                    user_id=user_id,
                    batch_id=batch_id,
                    result_json=result.model_dump_json(),
                    created_at=completed_at,
                )
            )
            session.flush()
        session.commit()
        return result
    except LegacyImportCommitError:
        session.rollback()
        raise
    except IntegrityError:
        session.rollback()
        raise LegacyImportCommitError(
            ERROR_IMPORT_STATE_CHANGED,
            "导入时账本数据已发生变化，请重新预览后再确认",
            analysis,
        ) from None
    except Exception:
        session.rollback()
        raise LegacyImportCommitError(
            ERROR_IMPORT_COMMIT_FAILED,
            "导入失败，请稍后重试",
            analysis,
        ) from None


def get_completed_import(session, user_id, batch_id):
    record = session.scalar(
        select(LegacyImportBatch).where(
            LegacyImportBatch.user_id == user_id, LegacyImportBatch.batch_id == batch_id
        )
    )
    if not isinstance(record, LegacyImportBatch):
        return None
    return LegacyImportConfirmResponse.model_validate_json(record.result_json)


def list_completed_imports(session, user_id):
    records = session.scalars(
        select(LegacyImportBatch)
        .where(LegacyImportBatch.user_id == user_id)
        .order_by(LegacyImportBatch.created_at.desc(), LegacyImportBatch.id.desc())
        .limit(20)
    ).all()
    return [
        LegacyImportConfirmResponse.model_validate(json.loads(record.result_json))
        for record in records
    ]


__all__ = [
    "ERROR_ACCOUNT_UNAVAILABLE",
    "ERROR_IMPORT_COMMIT_FAILED",
    "ERROR_IMPORT_STATE_CHANGED",
    "ERROR_IMPORT_VALIDATION_FAILED",
    "LegacyImportCommitError",
    "commit_old_ledger_import",
]
