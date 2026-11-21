# ruff: noqa: RUF002, RUF003
"""个人备份的只读恢复分析与单事务替换恢复核心。

本模块实现第五阶段步骤 5B 的两个职责：

1. :func:`analyze_personal_restore` —— 只读恢复分析。严格解析备份字节、查询账号、只统计当前
   ``user_id``、核对备份中用到的全部系统字典引用与当前数据库一致，返回不可变、强类型的预览结果，
   不写数据库、不创建安全备份文件；
2. :func:`replace_personal_ledger` —— 单事务替换恢复核心。锁定账号、校验当前密码、重新解析备份、
   再次核对系统字典、删除前生成并保存恢复前安全备份、按序删除当前账本、分层 ``flush()`` 重映射
   备份内编号、重算联系人和事件身份指纹、重哈希密码，最后 ``commit()`` 一次，任何失败回滚。

本模块不实现 HTTP 路由、不管理上传预览 token、不改 Schema／ORM／Alembic、不访问旧
``contact_db``。数据库旧 ID 绝不注入为新主键；系统字典统一按稳定 ``system_code`` 解析目标库
实际主键；方向仍由本人／事主／随礼人推导，不新增方向字段。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
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
from app.db.registration import personal_restore_receipts
from app.services.contact_identity import contact_identity_hash
from app.services.event_dates import date_value, event_date_fields
from app.services.event_identity import event_identity_hash
from app.services.personal_backup import (
    DEFAULT_PERSONAL_LIMITS,
    ERROR_EVENT_DUPLICATE,
    ERROR_FILE_TOO_LARGE,
    ERROR_RECORD_LIMIT,
    ERROR_ZIP_COMPRESSION_RATIO,
    ERROR_ZIP_ENTRY_TOO_LARGE,
    ERROR_ZIP_TOTAL_TOO_LARGE,
    BackupCounts,
    BackupDictionaryEntry,
    BackupDictionaryNameConflict,
    BackupDictionaryRef,
    ParsedPersonalBackup,
    PersonalBackupError,
    PersonalBackupLimits,
    build_personal_backup,
    parse_personal_backup,
)
from app.services.personal_safety_backup_store import (
    PersonalSafetyBackupStore,
    PersonalSafetyBackupStoreError,
)
from app.services.system_dictionary_catalog import final_names

# ---------------------------------------------------------------- 稳定错误码

ERROR_ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
ERROR_BACKUP_INVALID = "BACKUP_INVALID"
ERROR_SYSTEM_DICTIONARY_MISMATCH = "SYSTEM_DICTIONARY_MISMATCH"
ERROR_PASSWORD_INCORRECT = "PASSWORD_INCORRECT"
ERROR_SAFETY_BACKUP_FAILED = "SAFETY_BACKUP_FAILED"
ERROR_RESTORE_STATE_CHANGED = "RESTORE_STATE_CHANGED"
ERROR_RESTORE_FAILED = "RESTORE_FAILED"
ERROR_DICTIONARY_RENAMES_INVALID = "DICTIONARY_RENAMES_INVALID"

RESTORE_MODE_REPLACE = "replace"
_MAX_DICTIONARY_NAME_LENGTH = 50
_ASCII_LOWER_TRANSLATION = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class PersonalRestoreError(ValueError):
    """恢复分析或替换恢复的稳定业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _backup_validation_message(error: PersonalBackupError) -> str:
    # 唯一性冲突只包含受控提示和数字编号，可明确告知用户；其他解析错误仍统一脱敏。
    if error.code in {
        ERROR_FILE_TOO_LARGE,
        ERROR_RECORD_LIMIT,
        ERROR_ZIP_ENTRY_TOO_LARGE,
        ERROR_ZIP_TOTAL_TOO_LARGE,
        ERROR_ZIP_COMPRESSION_RATIO,
    }:
        return error.message + " 请保留原件，参阅《从备份恢复》的历史大账本迁移说明。"
    if error.code == ERROR_EVENT_DUPLICATE:
        return error.message
    return "备份文件无法解析或校验失败，请使用系统生成的原版备份文件"


@dataclass(frozen=True, slots=True)
class PersonalRestoreAnalysis:
    """只读恢复预览：当前账本与被替换账本的六项数量及备份包信息。"""

    current: BackupCounts
    incoming: BackupCounts
    ledger_revision: str
    format_version: int
    application_version: str
    created_at: datetime
    mode: str
    dictionary_name_conflicts: tuple[BackupDictionaryNameConflict, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonalRestoreDictionaryRename:
    """确认恢复时提交的一项自定义字典冲突改名。"""

    category: str
    backup_id: int
    original_name: str
    new_name: str


@dataclass(frozen=True, slots=True)
class PersonalRestoreResult:
    """替换恢复成功结果：六项恢复数量、安全备份 token 与过期时间、需重新登录。"""

    counts: BackupCounts
    safety_backup_token: str
    safety_backup_expires_at: datetime
    reauth_required: bool


def _dictionary_name_key(value: str) -> str:
    return value.strip().translate(_ASCII_LOWER_TRANSLATION)


def _apply_dictionary_renames(
    parsed: ParsedPersonalBackup,
    renames: tuple[PersonalRestoreDictionaryRename, ...],
) -> ParsedPersonalBackup:
    """严格核对预览冲突改名并返回改名后的内存对象，不修改上传原件。"""
    expected = {
        (conflict.category, conflict.backup_id): conflict
        for conflict in parsed.dictionary_name_conflicts
    }
    supplied: dict[tuple[str, int], PersonalRestoreDictionaryRename] = {}
    for rename in renames:
        key = (rename.category, rename.backup_id)
        if key in supplied:
            raise PersonalRestoreError(
                ERROR_DICTIONARY_RENAMES_INVALID, "冲突改名存在重复项目，请重新确认"
            )
        supplied[key] = rename
    if set(supplied) != set(expected):
        raise PersonalRestoreError(
            ERROR_DICTIONARY_RENAMES_INVALID, "冲突改名项目不完整或包含多余项目，请重新确认"
        )

    entries_by_category = {
        "relationship": parsed.relationship_types,
        "kinship": parsed.kinship_types,
        "event": parsed.event_types,
    }
    renamed_by_category: dict[str, tuple[BackupDictionaryEntry, ...]] = {}
    for category, entries in entries_by_category.items():
        system_keys = {_dictionary_name_key(name) for name in final_names(category)}
        final_keys: set[str] = set()
        renamed_entries: list[BackupDictionaryEntry] = []
        for entry in entries:
            conflict = expected.get((category, entry.backup_id))
            if conflict is None:
                new_name = entry.name
            else:
                submitted = supplied[(category, entry.backup_id)]
                if submitted.original_name != conflict.original_name:
                    raise PersonalRestoreError(
                        ERROR_DICTIONARY_RENAMES_INVALID,
                        "冲突改名的原名称与预览不一致，请重新预览",
                    )
                new_name = submitted.new_name.strip()
                if not new_name or len(new_name) > _MAX_DICTIONARY_NAME_LENGTH:
                    raise PersonalRestoreError(
                        ERROR_DICTIONARY_RENAMES_INVALID,
                        "冲突改名不能为空且最多允许 50 个字符",
                    )
            name_key = _dictionary_name_key(new_name)
            if name_key in system_keys or name_key in final_keys:
                raise PersonalRestoreError(
                    ERROR_DICTIONARY_RENAMES_INVALID,
                    "冲突改名后仍与系统项或其他自定义项重名",
                )
            final_keys.add(name_key)
            renamed_entries.append(replace(entry, name=new_name))
        renamed_by_category[category] = tuple(renamed_entries)

    return replace(
        parsed,
        relationship_types=renamed_by_category["relationship"],
        kinship_types=renamed_by_category["kinship"],
        event_types=renamed_by_category["event"],
        dictionary_name_conflicts=(),
    )


# ---------------------------------------------------------------- 系统字典核对


def _iter_system_references(
    parsed: ParsedPersonalBackup,
) -> list[tuple[type[object], str]]:
    """收集备份中用到的全部系统字典引用（类别、稳定编码）。"""
    refs: list[tuple[type[object], str]] = []
    for contact in parsed.contacts:
        if contact.relationship.scope == "system":
            refs.append((RelationshipType, contact.relationship.system_code))
    for event in parsed.events:
        if event.kinship_type.scope == "system":
            refs.append((KinshipType, event.kinship_type.system_code))
        if event.event_type.scope == "system":
            refs.append((EventType, event.event_type.system_code))
    return refs


def _verify_system_dictionaries(
    session: Session, parsed: ParsedPersonalBackup
) -> dict[tuple[type[object], str], int]:
    """按稳定编码核对系统字典，并返回当前数据库的实际主键。"""
    seen: set[tuple[type[object], str]] = set()
    resolved: dict[tuple[type[object], str], int] = {}
    for model, system_code in _iter_system_references(parsed):
        key = (model, system_code)
        if key in seen:
            continue
        seen.add(key)
        item = session.scalar(
            select(model).where(
                model.system_code == system_code,
                model.user_id.is_(None),
            )
        )
        if item is None:
            raise PersonalRestoreError(
                ERROR_SYSTEM_DICTIONARY_MISMATCH,
                "备份引用的系统字典与当前系统不一致，无法恢复",
            )
        resolved[key] = item.id
    return resolved


# ---------------------------------------------------------------- 只读恢复分析


def _ledger_snapshot(session: Session, user_id: int) -> tuple[str, BackupCounts]:
    """Hash each owned row in primary-key order, including rows removed by restore."""
    digest = sha256()
    counts = {}
    for key, model in (
        ("relationship_types", RelationshipType),
        ("kinship_types", KinshipType),
        ("event_types", EventType),
        ("contacts", Contact),
        ("events", EventRecord),
        ("gifts", GiftRecord),
        ("import_batches", LegacyImportBatch),
    ):
        digest.update(key.encode() + b"\n")
        statement = select(model.__table__).where(model.user_id == user_id).order_by(model.id)
        count = 0
        for row in session.execute(statement.execution_options(yield_per=256)):
            digest.update(
                json.dumps(
                    tuple(row), default=str, ensure_ascii=False, separators=(",", ":")
                ).encode()
                + b"\n"
            )
            count += 1
        if key != "import_batches":
            counts[key] = count
    return digest.hexdigest(), BackupCounts(**counts)


def _account_key(user: User) -> str:
    identity = [user.id, user.username, user.created_at.isoformat()]
    return sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def recent_restore_receipts(session: Session, user: User, before: int | None = None) -> list[dict]:
    t = personal_restore_receipts.c
    statement = select(t.id, t.safety_token, t.restored_at, t.expires_at, t.counts).where(
        t.user_id == user.id, t.account_key == _account_key(user)
    )
    if before is not None:
        statement = statement.where(t.id < before)
    rows = session.execute(statement.order_by(t.id.desc()).limit(20))
    now = datetime.now(UTC)
    return [
        {
            "receipt_id": row.id,
            "restored_at": datetime.fromtimestamp(row.restored_at, UTC),
            "safety_backup_expires_at": datetime.fromtimestamp(row.expires_at, UTC),
            "safety_backup_token": row.safety_token if row.expires_at > now.timestamp() else None,
            "counts": json.loads(row.counts),
        }
        for row in rows
    ]


def analyze_personal_restore(
    session: Session,
    user_id: int,
    content: bytes,
    *,
    limits: PersonalBackupLimits = DEFAULT_PERSONAL_LIMITS,
) -> PersonalRestoreAnalysis:
    """只读分析个人备份，返回不可变预览结果，不写数据库、不创建安全备份。"""
    try:
        parsed = parse_personal_backup(content, **asdict(limits))
    except PersonalBackupError as error:
        raise PersonalRestoreError(
            ERROR_BACKUP_INVALID, _backup_validation_message(error)
        ) from None

    user = session.scalar(select(User).where(User.id == user_id))
    if user is None or user.is_active != 1:
        raise PersonalRestoreError(ERROR_ACCOUNT_UNAVAILABLE, "账号不可用，请重新登录")

    _verify_system_dictionaries(session, parsed)

    ledger_revision, current = _ledger_snapshot(session, user_id)
    return PersonalRestoreAnalysis(
        current=current,
        ledger_revision=ledger_revision,
        incoming=BackupCounts(
            relationship_types=len(parsed.relationship_types),
            kinship_types=len(parsed.kinship_types),
            event_types=len(parsed.event_types),
            contacts=len(parsed.contacts),
            events=len(parsed.events),
            gifts=len(parsed.gifts),
        ),
        format_version=parsed.format_version,
        application_version=parsed.application_version,
        created_at=parsed.created_at,
        mode=RESTORE_MODE_REPLACE,
        dictionary_name_conflicts=parsed.dictionary_name_conflicts,
    )


# ---------------------------------------------------------------- 单事务替换恢复


def _delete_current_ledger(session: Session, user_id: int) -> None:
    """仅按当前 ``user_id`` 删除流水、事件、联系人、三类自定义字典；不动系统字典与账号。"""
    session.execute(delete(LegacyImportBatch).where(LegacyImportBatch.user_id == user_id))
    session.execute(delete(GiftRecord).where(GiftRecord.user_id == user_id))
    session.execute(delete(EventRecord).where(EventRecord.user_id == user_id))
    session.execute(delete(Contact).where(Contact.user_id == user_id))
    session.execute(delete(RelationshipType).where(RelationshipType.user_id == user_id))
    session.execute(delete(KinshipType).where(KinshipType.user_id == user_id))
    session.execute(delete(EventType).where(EventType.user_id == user_id))


def _resolve_dictionary_id(
    ref: BackupDictionaryRef,
    model: type[object],
    new_ids: dict[int, object],
    system_ids: dict[tuple[type[object], str], int],
) -> int:
    if ref.scope == "system":
        return system_ids[(model, ref.system_code)]
    return new_ids[ref.backup_id].id


def _restore_ledger(
    session: Session,
    user_id: int,
    parsed: ParsedPersonalBackup,
    system_ids: dict[tuple[type[object], str], int],
) -> None:
    """按「自定义字典 → 联系人 → 事件 → 流水」顺序恢复，分层 ``flush()`` 重映射编号。"""
    new_relationships: dict[int, RelationshipType] = {}
    for entry in parsed.relationship_types:
        record = RelationshipType(
            user_id=user_id,
            type_name=entry.name,
            sort_order=entry.sort_order,
            is_active=1 if entry.is_active else 0,
            system_code=None,
        )
        session.add(record)
        new_relationships[entry.backup_id] = record
    new_kinships: dict[int, KinshipType] = {}
    for entry in parsed.kinship_types:
        record = KinshipType(
            user_id=user_id,
            type_name=entry.name,
            sort_order=entry.sort_order,
            is_active=1 if entry.is_active else 0,
            system_code=None,
        )
        session.add(record)
        new_kinships[entry.backup_id] = record
    new_event_types: dict[int, EventType] = {}
    for entry in parsed.event_types:
        record = EventType(
            user_id=user_id,
            type_name=entry.name,
            sort_order=entry.sort_order,
            is_active=1 if entry.is_active else 0,
            system_code=None,
        )
        session.add(record)
        new_event_types[entry.backup_id] = record
    if new_relationships or new_kinships or new_event_types:
        session.flush()

    def relationship_id(ref: BackupDictionaryRef) -> int:
        return _resolve_dictionary_id(ref, RelationshipType, new_relationships, system_ids)

    def kinship_id(ref: BackupDictionaryRef) -> int:
        return _resolve_dictionary_id(ref, KinshipType, new_kinships, system_ids)

    def event_type_id(ref: BackupDictionaryRef) -> int:
        return _resolve_dictionary_id(ref, EventType, new_event_types, system_ids)

    new_contacts: dict[int, Contact] = {}
    for contact in parsed.contacts:
        record = Contact(
            user_id=user_id,
            name=contact.name,
            relation_type_id=relationship_id(contact.relationship),
            location=contact.location,
            phone=contact.phone,
            remark=contact.remark,
            identity_hash=contact_identity_hash(contact.name, contact.location, contact.remark),
            is_active=1 if contact.is_active else 0,
            created_at=contact.created_at,
        )
        session.add(record)
        new_contacts[contact.backup_id] = record
    session.flush()

    new_events: dict[int, EventRecord] = {}
    for event in parsed.events:
        host_id = new_contacts[event.host_contact].id
        relative_id = kinship_id(event.kinship_type)
        type_id = event_type_id(event.event_type)
        record = EventRecord(
            user_id=user_id,
            contact_id=host_id,
            relative_type_id=relative_id,
            event_type_id=type_id,
            **event_date_fields(date_value(event), source=event.date_source),
            remark=event.remark,
            identity_hash=event_identity_hash(host_id, relative_id, type_id, event.remark),
            created_at=event.created_at,
        )
        session.add(record)
        new_events[event.backup_id] = record
    if new_events:
        session.flush()

    for gift in parsed.gifts:
        session.add(
            GiftRecord(
                user_id=user_id,
                event_record_id=new_events[gift.event].id,
                contact_id=new_contacts[gift.giver_contact].id,
                amount=gift.amount,
                gift_details=gift.gift_details,
                remark=gift.remark,
                created_at=gift.created_at,
            )
        )
    if parsed.gifts:
        session.flush()


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedRestorePassword:
    user_id: int
    previous_hash: str
    replacement_hash: str


def verify_restore_password(user: User, password: str) -> VerifiedRestorePassword:
    if not verify_password(password, user.password_hash):
        raise PersonalRestoreError(ERROR_PASSWORD_INCORRECT, "密码不正确，无法恢复")
    return VerifiedRestorePassword(user.id, user.password_hash, hash_password(password))


def replace_personal_ledger(
    session: Session,
    user_id: int,
    content: bytes,
    *,
    credential: VerifiedRestorePassword,
    expected_revision: str,
    safety_backup_store: PersonalSafetyBackupStore,
    application_version: str,
    limits: PersonalBackupLimits = DEFAULT_PERSONAL_LIMITS,
    dictionary_renames: tuple[PersonalRestoreDictionaryRename, ...] = (),
) -> PersonalRestoreResult:
    """替换恢复当前用户账本，全部步骤在一个事务内完成，只在最后提交一次。"""
    try:
        # Release any older consistent-read snapshot before acquiring the ledger write lock.
        session.rollback()
        # 1. 锁定当前账号。
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None or user.is_active != 1:
            raise PersonalRestoreError(ERROR_ACCOUNT_UNAVAILABLE, "账号不可用，请重新登录")

        # Authentication ran under the shared password budget; recheck its binding under lock.
        if credential.user_id != user_id or credential.previous_hash != user.password_hash:
            raise PersonalRestoreError(ERROR_ACCOUNT_UNAVAILABLE, "账号登录状态已变化，请重新登录")

        if _ledger_snapshot(session, user_id)[0] != expected_revision:
            raise PersonalRestoreError(
                ERROR_RESTORE_STATE_CHANGED, "预览后当前账本已变化，请重新预览并核对替换范围"
            )

        # 3. 重新严格解析原始字节，不信任预览阶段对象或编号。
        try:
            parsed = parse_personal_backup(content, **asdict(limits))
        except PersonalBackupError as error:
            raise PersonalRestoreError(
                ERROR_BACKUP_INVALID, _backup_validation_message(error)
            ) from None

        # 4. 重新核对预览冲突改名，再按稳定编码核对当前系统字典。
        parsed = _apply_dictionary_renames(parsed, dictionary_renames)
        system_ids = _verify_system_dictionaries(session, parsed)

        # 5. 删除任何数据前生成并保存恢复前安全备份。
        try:
            safety = build_personal_backup(
                session, user_id, application_version=application_version, limits=limits
            )
            safety_token, safety_expires_at = safety_backup_store.save(
                user_id=user_id, content=safety.content
            )
        except PersonalBackupError as error:
            raise PersonalRestoreError(
                ERROR_SAFETY_BACKUP_FAILED,
                "当前账本的安全备份未通过恢复校验，已取消替换。" + error.message,
            ) from None
        except PersonalSafetyBackupStoreError:
            raise PersonalRestoreError(
                ERROR_SAFETY_BACKUP_FAILED, "无法生成或保存恢复前安全备份，恢复已取消"
            ) from None

        # 6. 按序删除当前用户账本。
        _delete_current_ledger(session, user_id)

        # 7—9. 按「字典 → 联系人 → 事件 → 流水」恢复，分层 flush 重映射编号。
        _restore_ledger(session, user_id, parsed, system_ids)

        # 10. 用同一明文密码重新哈希，使既有 JWT 失效。
        user.password_hash = credential.replacement_hash

        counts = BackupCounts(
            relationship_types=len(parsed.relationship_types),
            kinship_types=len(parsed.kinship_types),
            event_types=len(parsed.event_types),
            contacts=len(parsed.contacts),
            events=len(parsed.events),
            gifts=len(parsed.gifts),
        )
        session.execute(
            personal_restore_receipts.insert().values(
                user_id=user_id,
                account_key=_account_key(user),
                safety_token=safety_token,
                restored_at=int(datetime.now(UTC).timestamp()),
                expires_at=int(safety_expires_at.timestamp()),
                counts=json.dumps(asdict(counts), separators=(",", ":")),
            )
        )
        # 11. 回执、密码轮换与账本替换同一事务提交。
        session.commit()
    except PersonalRestoreError:
        session.rollback()
        raise
    except IntegrityError:
        session.rollback()
        raise PersonalRestoreError(
            ERROR_RESTORE_STATE_CHANGED, "恢复时账本数据已发生变化，请重新预览后再确认"
        ) from None
    except Exception:
        session.rollback()
        raise PersonalRestoreError(ERROR_RESTORE_FAILED, "恢复失败，请稍后重试") from None

    return PersonalRestoreResult(
        counts=counts,
        safety_backup_token=safety_token,
        safety_backup_expires_at=safety_expires_at,
        reauth_required=True,
    )


__all__ = [
    "ERROR_ACCOUNT_UNAVAILABLE",
    "ERROR_BACKUP_INVALID",
    "ERROR_DICTIONARY_RENAMES_INVALID",
    "ERROR_PASSWORD_INCORRECT",
    "ERROR_RESTORE_FAILED",
    "ERROR_RESTORE_STATE_CHANGED",
    "ERROR_SAFETY_BACKUP_FAILED",
    "ERROR_SYSTEM_DICTIONARY_MISMATCH",
    "RESTORE_MODE_REPLACE",
    "PersonalRestoreAnalysis",
    "PersonalRestoreDictionaryRename",
    "PersonalRestoreError",
    "PersonalRestoreResult",
    "analyze_personal_restore",
    "replace_personal_ledger",
]
