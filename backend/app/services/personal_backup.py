# ruff: noqa: RUF002, RUF003
"""个人无损备份包的生成与严格解析。

本模块实现第五阶段步骤 5A 的两个职责：

1. :func:`build_personal_backup` —— 只读当前用户数据库数据，生成版本化
   ``.giftledger`` ZIP 字节（内部为 ``manifest.json`` + ``ledger.json``），
   不写数据库、不执行 MySQL；
2. :func:`parse_personal_backup` —— 在纯内存中对 ``.giftledger`` 字节做严格的
   结构与业务一致性校验，产出后续恢复服务可直接使用的不可变、强类型数据对象，
   不访问数据库。

当前生成包格式 v3（``gift-ledger-personal-backup`` / 版本 ``3`` / Schema 基线
``schema_v2_s011``），解析器继续兼容 S010 格式 v3、S005 格式 v1 与 S006—S009 格式 v2：

- ZIP 只允许 ``manifest.json``、``ledger.json`` 两个普通文件，使用标准库
  Deflate，不加密；SHA-256 仅用于发现文件损坏，不宣称防恶意篡改；
- 两个 JSON 均 UTF-8、无 BOM、禁止 NaN/Infinity、禁止重复键；生成时使用稳定
  键顺序和紧凑编码；
- 数据库编号在生成时按数据库 ID 排序、重新分配从 1 开始的连续备份内编号，
  所有引用只使用备份内编号；格式 v2 的系统字典引用使用类别与稳定 ``system_code``，
  自定义项使用 ``{"scope":"custom","backup_id":1}``。S005 格式 v1 的旧数字编号和旧名称
  会先严格核对，再转换为 S006 稳定编码。

安全边界：上传的备份包一律按不可信处理。解析器拒绝空文件、损坏 ZIP、加密
条目、目录、绝对路径、``..``、反斜杠路径、符号链接、重复条目、额外文件和
异常压缩比例，并对压缩后、解压后、单项和记录总数设限。
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Contact,
    EventRecord,
    EventType,
    GiftRecord,
    KinshipType,
    RelationshipType,
    User,
)
from app.schemas.ledger import LedgerDirection
from app.services.contact_identity import contact_identity_components
from app.services.event_dates import EventDateValue, date_value, event_date_fields
from app.services.ledger import LedgerValidationError, derive_direction, validate_gift_value
from app.services.system_dictionary_catalog import (
    SEEDS_BY_CATEGORY,
    SELF_RELATIONSHIP_SYSTEM_CODE,
    final_names,
)

# ---------------------------------------------------------------- 固定包格式常量

FORMAT_NAME = "gift-ledger-personal-backup"
FORMAT_VERSION = 3
SCHEMA_REVISION = "schema_v2_s011"
S010_SCHEMA_REVISION = "schema_v2_s010"
S009_SCHEMA_REVISION = "schema_v2_s009"
S006_FORMAT_VERSION = 2
S008_SCHEMA_REVISION = "schema_v2_s008"
S007_SCHEMA_REVISION = "schema_v2_s007"
S006_SCHEMA_REVISION = "schema_v2_s006"
S005_FORMAT_VERSION = 1
S005_SCHEMA_REVISION = "schema_v2_s005"
MANIFEST_FILENAME = "manifest.json"
LEDGER_FILENAME = "ledger.json"
PACKAGE_SUFFIX = ".giftledger"

# 系统预置字典可见名称（S003/S004 基线）。用于校验自定义字典名称不与系统项冲突。
S005_SYSTEM_RELATIONSHIP_NAMES: tuple[str, ...] = (
    "自己",
    "亲戚",
    "同事",
    "初高中同学",
    "大学同学",
    "战友",
    "生意伙伴",
    "邻居",
    "朋友",
)
S005_SYSTEM_KINSHIP_NAMES: tuple[str, ...] = (
    "本人",
    "父亲",
    "母亲",
    "岳父",
    "岳母",
    "公公",
    "婆婆",
    "长子",
    "次子",
    "长女",
    "次女",
    "祖父 (爷爷)",
    "祖母 (奶奶)",
)
S005_SYSTEM_EVENT_NAMES: tuple[str, ...] = (
    "结婚",
    "添子 (满月/百天)",
    "生日 (寿宴)",
    "驾归瑶池 (白事)",
    "乔迁新居",
    "榜上有名 (升学)",
    "金日开业",
    "微恙慰问 (生病就医)",
    "日常拜年",
)

SYSTEM_RELATIONSHIP_NAMES = final_names("relationship")
SYSTEM_KINSHIP_NAMES = final_names("kinship")
SYSTEM_EVENT_NAMES = final_names("event")

_S005_CODES_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "relationship": (
        "relationship.self",
        "relationship.relative",
        "relationship.colleague",
        "relationship.classmate",
        "relationship.classmate",
        "relationship.military_comrade",
        "relationship.business_partner",
        "relationship.neighbor",
        "relationship.friend",
    ),
    "kinship": (
        "kinship.self",
        "kinship.father",
        "kinship.mother",
        "kinship.wife_father",
        "kinship.wife_mother",
        "kinship.husband_father",
        "kinship.husband_mother",
        "kinship.first_son",
        "kinship.second_son",
        "kinship.first_daughter",
        "kinship.second_daughter",
        "kinship.paternal_grandfather",
        "kinship.paternal_grandmother",
    ),
    "event": (
        "event.wedding",
        "event.newborn",
        "event.birthday",
        "event.funeral",
        "event.housewarming",
        "event.education",
        "event.business_opening",
        "event.visit_patient",
        "event.new_year_visit",
    ),
}

_S005_NAMES_BY_CATEGORY = {
    "relationship": S005_SYSTEM_RELATIONSHIP_NAMES,
    "kinship": S005_SYSTEM_KINSHIP_NAMES,
    "event": S005_SYSTEM_EVENT_NAMES,
}

_FINAL_CODES_BY_CATEGORY = {
    category: frozenset(seed.system_code for seed in seeds)
    for category, seeds in SEEDS_BY_CATEGORY.items()
}

# 解析限制默认值。
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_RECORDS = 100_000
MAX_ZIP_ENTRIES = 10
MAX_COMPRESSION_RATIO = 200

# 字段长度（复用既有规则）。
MAX_CONTACT_NAME_LENGTH = 50
MAX_CONTACT_LOCATION_LENGTH = 50
MAX_CONTACT_PHONE_LENGTH = 20
MAX_DICTIONARY_NAME_LENGTH = 50
MIN_EVENT_DATE = date(1000, 1, 1)

# 严格日期：只认 yyyy-mm-dd。
_STRICT_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)

# ---------------------------------------------------------------- 错误码

ERROR_EMPTY_FILE = "empty_file"
ERROR_FILE_TOO_LARGE = "file_too_large"
ERROR_NOT_ZIP = "not_zip"
ERROR_ZIP_CORRUPTED = "zip_corrupted"
ERROR_ZIP_ENCRYPTED = "zip_encrypted"
ERROR_ZIP_ENTRY_TYPE = "zip_entry_type"
ERROR_ZIP_UNSAFE_PATH = "zip_unsafe_path"
ERROR_ZIP_DUPLICATE_ENTRY = "zip_duplicate_entry"
ERROR_ZIP_TOO_MANY_ENTRIES = "zip_too_many_entries"
ERROR_ZIP_ENTRY_TOO_LARGE = "zip_entry_too_large"
ERROR_ZIP_TOTAL_TOO_LARGE = "zip_total_too_large"
ERROR_ZIP_COMPRESSION_RATIO = "zip_compression_ratio"
ERROR_ZIP_COMPRESSION_UNSUPPORTED = "zip_compression_unsupported"
ERROR_ZIP_UNEXPECTED_FILES = "zip_unexpected_files"

ERROR_JSON_BOM = "json_bom"
ERROR_JSON_UTF8 = "json_utf8"
ERROR_JSON_INVALID = "json_invalid"
ERROR_JSON_DUPLICATE_KEY = "json_duplicate_key"
ERROR_JSON_NON_FINITE = "json_non_finite"

ERROR_FIELD_MISSING = "field_missing"
ERROR_FIELD_UNKNOWN = "field_unknown"
ERROR_FIELD_TYPE = "field_type"

ERROR_FORMAT_UNSUPPORTED = "format_unsupported"
ERROR_VERSION_UNSUPPORTED = "version_unsupported"
ERROR_SCHEMA_UNSUPPORTED = "schema_unsupported"
ERROR_LEDGER_SIZE_MISMATCH = "ledger_size_mismatch"
ERROR_LEDGER_SHA_MISMATCH = "ledger_sha_mismatch"
ERROR_COUNTS_MISMATCH = "counts_mismatch"

ERROR_BACKUP_ID_INVALID = "backup_id_invalid"
ERROR_REFERENCE_MISSING = "reference_missing"
ERROR_REFERENCE_CATEGORY = "reference_category"
ERROR_SELF_CONTACT_INVALID = "self_contact_invalid"
ERROR_CONTACT_IDENTITY_DUPLICATE = "contact_identity_duplicate"
ERROR_DICTIONARY_NAME_DUPLICATE = "dictionary_name_duplicate"
ERROR_DICTIONARY_NAME_CONFLICT = "dictionary_name_conflict"
ERROR_EVENT_DUPLICATE = "event_duplicate"
ERROR_GIFT_DUPLICATE = "gift_duplicate"

ERROR_NAME_REQUIRED = "name_required"
ERROR_FIELD_TOO_LONG = "field_too_long"
ERROR_DATE_INVALID = "date_invalid"
ERROR_DATETIME_INVALID = "datetime_invalid"
ERROR_RECORD_LIMIT = "record_limit"

# 生成阶段。
ERROR_USER_UNAVAILABLE = "user_unavailable"
ERROR_SELF_CONTACT_REQUIRED = "self_contact_required"
ERROR_REFERENCE_INTEGRITY = "reference_integrity"


class PersonalBackupError(Exception):
    """个人备份生成或解析的稳定业务错误，至少包含 ``code`` 与中文 ``message``。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------- 冻结数据结构


@dataclass(frozen=True, slots=True)
class BackupCounts:
    """备份包六类数据的数量。"""

    relationship_types: int
    kinship_types: int
    event_types: int
    contacts: int
    events: int
    gifts: int


@dataclass(frozen=True, slots=True)
class PersonalBackupDownload:
    """生成的个人备份包：文件名、ZIP 字节与六项数量。"""

    filename: str
    content: bytes
    counts: BackupCounts


@dataclass(frozen=True, slots=True)
class BackupDictionaryRef:
    """解析后的字典引用（严格联合结构）。"""

    scope: str
    system_id: int | None
    system_code: str | None
    system_name: str | None
    backup_id: int | None


@dataclass(frozen=True, slots=True)
class BackupDictionaryEntry:
    """自定义字典项。"""

    backup_id: int
    name: str
    sort_order: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class BackupDictionaryNameConflict:
    """旧备份自定义字典名称与 S006 系统项重名，需用户确认改名。"""

    category: str
    backup_id: int
    original_name: str
    suggested_name: str


@dataclass(frozen=True, slots=True)
class BackupContact:
    """联系人（含本人与停用项）。"""

    backup_id: int
    name: str
    relationship: BackupDictionaryRef
    location: str | None
    phone: str | None
    remark: str | None
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BackupEvent:
    """事件。"""

    backup_id: int
    host_contact: int
    kinship_type: BackupDictionaryRef
    event_type: BackupDictionaryRef
    event_date: date | None
    remark: str | None
    created_at: datetime
    date_precision: str = "day"
    event_year: int | None = None
    event_month: int | None = None
    date_source: str = "web"


@dataclass(frozen=True, slots=True)
class BackupGift:
    """随礼流水。"""

    backup_id: int
    event: int
    giver_contact: int
    amount: Decimal
    gift_details: str | None
    remark: str | None
    created_at: datetime
    direction: LedgerDirection


@dataclass(frozen=True, slots=True)
class ParsedPersonalBackup:
    """严格解析后的不可变备份数据，供恢复服务直接使用。"""

    format_name: str
    format_version: int
    application_version: str
    schema_revision: str
    created_at: datetime
    self_contact_backup_id: int
    relationship_types: tuple[BackupDictionaryEntry, ...]
    kinship_types: tuple[BackupDictionaryEntry, ...]
    event_types: tuple[BackupDictionaryEntry, ...]
    contacts: tuple[BackupContact, ...]
    events: tuple[BackupEvent, ...]
    gifts: tuple[BackupGift, ...]
    dictionary_name_conflicts: tuple[BackupDictionaryNameConflict, ...]


# ---------------------------------------------------------------- 通用工具


def _normalize_key(value: str) -> str:
    """去首尾空白并忽略 ASCII 字母大小写，用于名称去重与冲突判断（D022）。"""
    return value.strip().translate(_ASCII_LOWER_TRANSLATION)


def _format_datetime(value: datetime) -> str:
    return value.isoformat()


def _format_amount(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _utc_datetime(generated_at: datetime | None) -> datetime:
    if generated_at is None:
        return datetime.now(UTC)
    if generated_at.tzinfo is None:
        return generated_at.replace(tzinfo=UTC)
    return generated_at.astimezone(UTC)


def _zip_info(filename: str, generated_at: datetime) -> ZipInfo:
    info = ZipInfo(
        filename,
        date_time=(
            generated_at.year,
            generated_at.month,
            generated_at.day,
            generated_at.hour,
            generated_at.minute,
            generated_at.second,
        ),
    )
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


# ---------------------------------------------------------------- 生成


def build_personal_backup(
    session: Session,
    user_id: int,
    *,
    application_version: str,
    generated_at: datetime | None = None,
) -> PersonalBackupDownload:
    """从当前用户数据库数据生成版本化 ``.giftledger`` ZIP 字节（只读）。"""
    user = session.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise PersonalBackupError(ERROR_USER_UNAVAILABLE, "账号不可用，无法生成备份")

    contacts = list(
        session.scalars(
            select(Contact).where(Contact.user_id == user_id).order_by(Contact.id)
        ).all()
    )
    self_relationship = session.scalar(
        select(RelationshipType).where(
            RelationshipType.user_id.is_(None),
            RelationshipType.system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
        )
    )
    if self_relationship is None:
        raise PersonalBackupError(ERROR_SELF_CONTACT_REQUIRED, "本人关系类型不存在，无法生成备份")
    self_contacts = [
        contact for contact in contacts if contact.relation_type_id == self_relationship.id
    ]
    if len(self_contacts) != 1:
        raise PersonalBackupError(ERROR_SELF_CONTACT_REQUIRED, "本人联系人不唯一，无法生成备份")
    self_contact = self_contacts[0]

    events = list(
        session.scalars(
            select(EventRecord).where(EventRecord.user_id == user_id).order_by(EventRecord.id)
        ).all()
    )
    gifts = list(
        session.scalars(
            select(GiftRecord).where(GiftRecord.user_id == user_id).order_by(GiftRecord.id)
        ).all()
    )
    relationships = list(
        session.scalars(
            select(RelationshipType)
            .where(RelationshipType.user_id == user_id)
            .order_by(RelationshipType.id)
        ).all()
    )
    kinships = list(
        session.scalars(
            select(KinshipType).where(KinshipType.user_id == user_id).order_by(KinshipType.id)
        ).all()
    )
    event_types = list(
        session.scalars(
            select(EventType).where(EventType.user_id == user_id).order_by(EventType.id)
        ).all()
    )

    contact_backup_ids = {contact.id: index for index, contact in enumerate(contacts, start=1)}
    event_backup_ids = {event.id: index for index, event in enumerate(events, start=1)}
    relationship_backup_ids = {item.id: index for index, item in enumerate(relationships, start=1)}
    kinship_backup_ids = {item.id: index for index, item in enumerate(kinships, start=1)}
    event_type_backup_ids = {item.id: index for index, item in enumerate(event_types, start=1)}

    def resolve_system(model: type[Any], item_id: int, category: str) -> dict[str, Any]:
        item = session.scalar(select(model).where(model.id == item_id, model.user_id.is_(None)))
        if item is None or item.system_code not in _FINAL_CODES_BY_CATEGORY[category]:
            raise PersonalBackupError(ERROR_REFERENCE_INTEGRITY, "账本引用不完整，无法生成备份")
        return {
            "scope": "system",
            "category": category,
            "system_code": item.system_code,
            "name": item.type_name,
        }

    def resolve_relationship(item_id: int) -> dict[str, Any]:
        if item_id in relationship_backup_ids:
            return {"scope": "custom", "backup_id": relationship_backup_ids[item_id]}
        return resolve_system(RelationshipType, item_id, "relationship")

    def resolve_kinship(item_id: int) -> dict[str, Any]:
        if item_id in kinship_backup_ids:
            return {"scope": "custom", "backup_id": kinship_backup_ids[item_id]}
        return resolve_system(KinshipType, item_id, "kinship")

    def resolve_event_type(item_id: int) -> dict[str, Any]:
        if item_id in event_type_backup_ids:
            return {"scope": "custom", "backup_id": event_type_backup_ids[item_id]}
        return resolve_system(EventType, item_id, "event")

    relationship_entries = [
        {
            "backup_id": relationship_backup_ids[item.id],
            "name": item.type_name,
            "sort_order": item.sort_order,
            "is_active": bool(item.is_active),
        }
        for item in relationships
    ]
    kinship_entries = [
        {
            "backup_id": kinship_backup_ids[item.id],
            "name": item.type_name,
            "sort_order": item.sort_order,
            "is_active": bool(item.is_active),
        }
        for item in kinships
    ]
    event_type_entries = [
        {
            "backup_id": event_type_backup_ids[item.id],
            "name": item.type_name,
            "sort_order": item.sort_order,
            "is_active": bool(item.is_active),
        }
        for item in event_types
    ]

    contact_entries = []
    for contact in contacts:
        contact_entries.append(
            {
                "backup_id": contact_backup_ids[contact.id],
                "name": contact.name,
                "relationship": resolve_relationship(contact.relation_type_id),
                "location": contact.location,
                "phone": contact.phone,
                "remark": contact.remark,
                "is_active": bool(contact.is_active),
                "created_at": _format_datetime(contact.created_at),
            }
        )

    event_entries = []
    for event in events:
        host_backup_id = contact_backup_ids.get(event.contact_id)
        if host_backup_id is None:
            raise PersonalBackupError(ERROR_REFERENCE_INTEGRITY, "账本引用不完整，无法生成备份")
        event_entries.append(
            {
                "backup_id": event_backup_ids[event.id],
                "host_contact": host_backup_id,
                "kinship_type": resolve_kinship(event.relative_type_id),
                "event_type": resolve_event_type(event.event_type_id),
                "event_date": event.event_date.isoformat() if event.event_date else None,
                "date_precision": date_value(event).precision,
                "event_year": date_value(event).year,
                "event_month": date_value(event).month,
                "date_source": getattr(event, "date_source", None) or "web",
                "remark": event.remark,
                "created_at": _format_datetime(event.created_at),
            }
        )

    gift_entries = []
    for index, gift in enumerate(gifts, start=1):
        event_backup_id = event_backup_ids.get(gift.event_record_id)
        giver_backup_id = contact_backup_ids.get(gift.contact_id)
        if event_backup_id is None or giver_backup_id is None:
            raise PersonalBackupError(ERROR_REFERENCE_INTEGRITY, "账本引用不完整，无法生成备份")
        gift_entries.append(
            {
                "backup_id": index,
                "event": event_backup_id,
                "giver_contact": giver_backup_id,
                "amount": _format_amount(gift.amount),
                "gift_details": gift.gift_details,
                "remark": gift.remark,
                "created_at": _format_datetime(gift.created_at),
            }
        )

    ledger = {
        "self_contact_backup_id": contact_backup_ids[self_contact.id],
        "relationship_types": relationship_entries,
        "kinship_types": kinship_entries,
        "event_types": event_type_entries,
        "contacts": contact_entries,
        "events": event_entries,
        "gifts": gift_entries,
    }
    ledger_bytes = _dump_json(ledger)

    generated_utc = _utc_datetime(generated_at)
    counts = BackupCounts(
        relationship_types=len(relationships),
        kinship_types=len(kinships),
        event_types=len(event_types),
        contacts=len(contacts),
        events=len(events),
        gifts=len(gifts),
    )
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "application_version": application_version,
        "schema_revision": SCHEMA_REVISION,
        "created_at": _format_datetime(generated_utc),
        "ledger_sha256": sha256(ledger_bytes).hexdigest(),
        "ledger_size": len(ledger_bytes),
        "counts": {
            "relationship_types": counts.relationship_types,
            "kinship_types": counts.kinship_types,
            "event_types": counts.event_types,
            "contacts": counts.contacts,
            "events": counts.events,
            "gifts": counts.gifts,
        },
    }
    manifest_bytes = _dump_json(manifest)

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(_zip_info(MANIFEST_FILENAME, generated_utc), manifest_bytes)
        archive.writestr(_zip_info(LEDGER_FILENAME, generated_utc), ledger_bytes)
    content = buffer.getvalue()

    filename = f"gift-ledger-personal-backup-{generated_utc:%Y%m%d-%H%M%S}{PACKAGE_SUFFIX}"
    return PersonalBackupDownload(filename=filename, content=content, counts=counts)


def _dump_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# ---------------------------------------------------------------- 解析


def _parse_json(data: bytes, context: str) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise PersonalBackupError(ERROR_JSON_BOM, f"{context} 含有 BOM，已拒绝解析")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PersonalBackupError(ERROR_JSON_UTF8, f"{context} 不是合法的 UTF-8 文本") from error

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PersonalBackupError(ERROR_JSON_DUPLICATE_KEY, f"{context} 含有重复键")
            result[key] = value
        return result

    def _constant(value: str) -> Any:
        raise PersonalBackupError(ERROR_JSON_NON_FINITE, f"{context} 含非法数值")

    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except PersonalBackupError:
        raise
    except (json.JSONDecodeError, ValueError) as error:
        raise PersonalBackupError(ERROR_JSON_INVALID, f"{context} 不是合法 JSON") from error


def _expect_object(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise PersonalBackupError(ERROR_FIELD_TYPE, f"「{field}」字段类型不正确")
    return value


def _expect_list(value: Any, field: str) -> list[Any]:
    if type(value) is not list:
        raise PersonalBackupError(ERROR_FIELD_TYPE, f"「{field}」字段类型不正确")
    return value


def _expect_string(value: Any, field: str) -> str:
    if type(value) is not str:
        raise PersonalBackupError(ERROR_FIELD_TYPE, f"「{field}」字段类型不正确")
    return value


def _expect_nullable_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise PersonalBackupError(ERROR_FIELD_TYPE, f"「{field}」字段类型不正确")
    return value


def _expect_int(value: Any, field: str) -> int:
    if type(value) is not int:
        raise PersonalBackupError(ERROR_FIELD_TYPE, f"「{field}」字段类型不正确")
    return value


def _expect_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise PersonalBackupError(ERROR_FIELD_TYPE, f"「{field}」字段类型不正确")
    return value


def _require_keys(obj: dict[str, Any], required: set[str], context: str) -> None:
    missing = required - set(obj)
    if missing:
        raise PersonalBackupError(ERROR_FIELD_MISSING, f"{context} 缺少必要字段")


def _reject_unknown_keys(obj: dict[str, Any], allowed: set[str], context: str) -> None:
    for key in obj:
        if key not in allowed:
            raise PersonalBackupError(ERROR_FIELD_UNKNOWN, f"{context} 包含未知字段")


def _parse_date_string(value: str) -> date:
    if _STRICT_DATE_PATTERN.fullmatch(value) is None:
        raise PersonalBackupError(ERROR_DATE_INVALID, "办事日期格式不正确")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise PersonalBackupError(ERROR_DATE_INVALID, "办事日期格式不正确") from error
    if parsed < MIN_EVENT_DATE:
        raise PersonalBackupError(ERROR_DATE_INVALID, "办事日期格式不正确")
    return parsed


def _parse_datetime_string(value: str, *, require_utc: bool = False) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PersonalBackupError(ERROR_DATETIME_INVALID, "时间格式不正确") from error
    if parsed.isoformat() != value:
        raise PersonalBackupError(ERROR_DATETIME_INVALID, "时间格式不正确")
    if require_utc and (parsed.tzinfo is None or parsed.utcoffset() != timedelta(0)):
        raise PersonalBackupError(ERROR_DATETIME_INVALID, "备份生成时间必须使用 UTC")
    return parsed


def _validate_name(value: str, field: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise PersonalBackupError(ERROR_NAME_REQUIRED, f"「{field}」不能为空")
    if len(normalized) > max_length:
        raise PersonalBackupError(ERROR_FIELD_TOO_LONG, f"「{field}」超过长度限制")
    return normalized


def _normalize_optional_text(value: str | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise PersonalBackupError(ERROR_FIELD_TOO_LONG, f"「{field}」超过长度限制")
    return normalized


def _normalize_contact_phone(value: str | None) -> str | None:
    without_whitespace = "".join(value.split()) if value is not None else None
    return _normalize_optional_text(
        without_whitespace,
        "contacts.phone",
        MAX_CONTACT_PHONE_LENGTH,
    )


def _validate_backup_ids(ids: list[int], context: str) -> None:
    if set(ids) != set(range(1, len(ids) + 1)):
        raise PersonalBackupError(ERROR_BACKUP_ID_INVALID, f"{context} 备份内编号不连续或重复")


def _parse_dictionary_ref(
    value: Any,
    *,
    field: str,
    custom_ids: set[int],
    category: str,
    format_version: int,
) -> BackupDictionaryRef:
    obj = _expect_object(value, field)
    scope = _expect_string(obj.get("scope"), f"{field}.scope")
    if scope == "system":
        if format_version == S005_FORMAT_VERSION:
            _reject_unknown_keys(obj, {"scope", "id", "name"}, field)
            _require_keys(obj, {"scope", "id", "name"}, field)
            system_id = _expect_int(obj["id"], f"{field}.id")
            system_name = _validate_name(
                _expect_string(obj["name"], f"{field}.name"),
                f"{field}.name",
                MAX_DICTIONARY_NAME_LENGTH,
            )
            old_names = _S005_NAMES_BY_CATEGORY[category]
            expected_name = old_names[system_id - 1] if 1 <= system_id <= len(old_names) else None
            if expected_name is None or system_name != expected_name:
                raise PersonalBackupError(
                    ERROR_REFERENCE_CATEGORY,
                    f"「{field}」系统字典编号、名称或类别不正确",
                )
            return BackupDictionaryRef(
                scope="system",
                system_id=system_id,
                system_code=_S005_CODES_BY_CATEGORY[category][system_id - 1],
                system_name=system_name,
                backup_id=None,
            )

        _reject_unknown_keys(obj, {"scope", "category", "system_code", "name"}, field)
        _require_keys(obj, {"scope", "category", "system_code"}, field)
        ref_category = _expect_string(obj["category"], f"{field}.category")
        system_code = _expect_string(obj["system_code"], f"{field}.system_code")
        if ref_category != category or system_code not in _FINAL_CODES_BY_CATEGORY[category]:
            raise PersonalBackupError(
                ERROR_REFERENCE_CATEGORY,
                f"「{field}」系统字典稳定编码或类别不正确",
            )
        system_name = None
        if "name" in obj:
            system_name = _validate_name(
                _expect_string(obj["name"], f"{field}.name"),
                f"{field}.name",
                MAX_DICTIONARY_NAME_LENGTH,
            )
        return BackupDictionaryRef(
            scope="system",
            system_id=None,
            system_code=system_code,
            system_name=system_name,
            backup_id=None,
        )
    if scope == "custom":
        _reject_unknown_keys(obj, {"scope", "backup_id"}, field)
        _require_keys(obj, {"scope", "backup_id"}, field)
        backup_id = _expect_int(obj["backup_id"], f"{field}.backup_id")
        if backup_id <= 0 or backup_id not in custom_ids:
            raise PersonalBackupError(ERROR_REFERENCE_MISSING, f"「{field}」引用了不存在的字典项")
        return BackupDictionaryRef(
            scope="custom",
            system_id=None,
            system_code=None,
            system_name=None,
            backup_id=backup_id,
        )
    raise PersonalBackupError(ERROR_REFERENCE_CATEGORY, f"「{field}」字典引用类型不正确")


def _parse_dictionary_entries(value: Any, field: str) -> tuple[BackupDictionaryEntry, ...]:
    items = _expect_list(value, field)
    entries: list[BackupDictionaryEntry] = []
    ids: list[int] = []
    for item in items:
        obj = _expect_object(item, field)
        _reject_unknown_keys(obj, {"backup_id", "name", "sort_order", "is_active"}, field)
        _require_keys(obj, {"backup_id", "name", "sort_order", "is_active"}, field)
        backup_id = _expect_int(obj["backup_id"], f"{field}.backup_id")
        name = _validate_name(
            _expect_string(obj["name"], f"{field}.name"),
            f"{field}.name",
            MAX_DICTIONARY_NAME_LENGTH,
        )
        sort_order = _expect_int(obj["sort_order"], f"{field}.sort_order")
        is_active = _expect_bool(obj["is_active"], f"{field}.is_active")
        ids.append(backup_id)
        entries.append(
            BackupDictionaryEntry(
                backup_id=backup_id, name=name, sort_order=sort_order, is_active=is_active
            )
        )
    _validate_backup_ids(ids, field)
    return tuple(entries)


def _is_self_relationship(ref: BackupDictionaryRef) -> bool:
    return ref.scope == "system" and ref.system_code == SELF_RELATIONSHIP_SYSTEM_CODE


# ---------------------------------------------------------------- ZIP 预检


def _is_windows_absolute(name: str) -> bool:
    return len(name) >= 2 and name[0].isalpha() and name[1] == ":"


def _check_entry_name(entry: ZipInfo) -> None:
    name = entry.filename
    if "\\" in name:
        raise PersonalBackupError(ERROR_ZIP_UNSAFE_PATH, "备份包含反斜杠路径条目，已拒绝解析")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _is_windows_absolute(normalized):
        raise PersonalBackupError(ERROR_ZIP_UNSAFE_PATH, "备份包含绝对路径条目，已拒绝解析")
    if ".." in normalized.split("/"):
        raise PersonalBackupError(ERROR_ZIP_UNSAFE_PATH, "备份包含上级目录穿越条目，已拒绝解析")


def _check_entry_type(entry: ZipInfo) -> None:
    if entry.is_dir():
        raise PersonalBackupError(ERROR_ZIP_ENTRY_TYPE, "备份包含目录条目，已拒绝解析")
    mode = (entry.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise PersonalBackupError(ERROR_ZIP_ENTRY_TYPE, "备份包含符号链接条目，已拒绝解析")


def _check_entry_size(entry: ZipInfo, limit: int) -> None:
    if entry.file_size > limit:
        raise PersonalBackupError(
            ERROR_ZIP_ENTRY_TOO_LARGE, f"备份内单个文件解压后约 {entry.file_size} 字节，超过限制。"
        )


def _check_compression_ratio(entry: ZipInfo) -> None:
    if entry.compress_size <= 0 or entry.file_size <= entry.compress_size * MAX_COMPRESSION_RATIO:
        return
    raise PersonalBackupError(
        ERROR_ZIP_COMPRESSION_RATIO, "备份压缩比例异常，疑似压缩炸弹，已拒绝解析"
    )


def _read_backup_files(
    content: bytes, *, max_file_bytes: int, max_uncompressed_bytes: int
) -> tuple[bytes, bytes]:
    if not content:
        raise PersonalBackupError(ERROR_EMPTY_FILE, "备份文件是空的")
    if len(content) > max_file_bytes:
        raise PersonalBackupError(
            ERROR_FILE_TOO_LARGE,
            f"文件大小约 {len(content)} 字节，超过上限 {max_file_bytes} 字节。",
        )
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as error:
        raise PersonalBackupError(
            ERROR_NOT_ZIP, "文件不是有效的备份包（压缩包已损坏或格式不对）"
        ) from error

    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise PersonalBackupError(
                ERROR_ZIP_TOO_MANY_ENTRIES,
                f"压缩包内有 {len(entries)} 个条目，超过上限 {MAX_ZIP_ENTRIES} 个。",
            )
        seen: set[str] = set()
        total = 0
        for entry in entries:
            if entry.flag_bits & 0x1:
                raise PersonalBackupError(ERROR_ZIP_ENCRYPTED, "备份包含加密条目，已拒绝解析")
            _check_entry_type(entry)
            _check_entry_name(entry)
            if entry.compress_type != ZIP_DEFLATED:
                raise PersonalBackupError(
                    ERROR_ZIP_COMPRESSION_UNSUPPORTED,
                    "备份包含不受支持的压缩方式，已拒绝解析",
                )
            normalized = entry.filename.replace("\\", "/")
            if normalized in seen:
                raise PersonalBackupError(ERROR_ZIP_DUPLICATE_ENTRY, "备份包含重复条目，已拒绝解析")
            seen.add(normalized)
            _check_compression_ratio(entry)
            _check_entry_size(entry, max_uncompressed_bytes)
            total += entry.file_size
        if total > max_uncompressed_bytes:
            raise PersonalBackupError(
                ERROR_ZIP_TOTAL_TOO_LARGE, f"压缩包解压后合计约 {total} 字节，超过上限。"
            )
        names = {entry.filename for entry in entries}
        if names != {MANIFEST_FILENAME, LEDGER_FILENAME}:
            raise PersonalBackupError(
                ERROR_ZIP_UNEXPECTED_FILES,
                f"备份应只包含 {MANIFEST_FILENAME} 和 {LEDGER_FILENAME} 两个文件。",
            )
        try:
            manifest_bytes = archive.read(MANIFEST_FILENAME)
            ledger_bytes = archive.read(LEDGER_FILENAME)
        except (BadZipFile, zlib.error, RuntimeError, EOFError, KeyError) as error:
            raise PersonalBackupError(ERROR_ZIP_CORRUPTED, "备份压缩包已损坏，无法读取") from error
    return manifest_bytes, ledger_bytes


# ---------------------------------------------------------------- 解析主流程


def parse_personal_backup(
    content: bytes,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> ParsedPersonalBackup:
    """在纯内存中严格解析并校验 ``.giftledger`` 字节，不访问数据库。"""
    manifest_bytes, ledger_bytes = _read_backup_files(
        content,
        max_file_bytes=max_file_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )

    manifest = _expect_object(_parse_json(manifest_bytes, MANIFEST_FILENAME), "manifest")
    _reject_unknown_keys(
        manifest,
        {
            "format",
            "format_version",
            "application_version",
            "schema_revision",
            "created_at",
            "ledger_sha256",
            "ledger_size",
            "counts",
        },
        "manifest",
    )
    _require_keys(
        manifest,
        {
            "format",
            "format_version",
            "application_version",
            "schema_revision",
            "created_at",
            "ledger_sha256",
            "ledger_size",
            "counts",
        },
        "manifest",
    )

    format_name = _expect_string(manifest["format"], "format")
    if format_name != FORMAT_NAME:
        raise PersonalBackupError(ERROR_FORMAT_UNSUPPORTED, "备份格式不受支持")

    format_version = _expect_int(manifest["format_version"], "format_version")
    supported_versions = {S005_FORMAT_VERSION, S006_FORMAT_VERSION, FORMAT_VERSION}
    if format_version not in supported_versions:
        raise PersonalBackupError(
            ERROR_VERSION_UNSUPPORTED, f"备份格式版本 {format_version} 不受支持"
        )

    schema_revision = _expect_string(manifest["schema_revision"], "schema_revision")
    supported_revisions = (
        {S005_SCHEMA_REVISION}
        if format_version == S005_FORMAT_VERSION
        else {SCHEMA_REVISION, S010_SCHEMA_REVISION}
        if format_version == FORMAT_VERSION
        else {
            S006_SCHEMA_REVISION,
            S007_SCHEMA_REVISION,
            S008_SCHEMA_REVISION,
            S009_SCHEMA_REVISION,
        }
    )
    if schema_revision not in supported_revisions:
        raise PersonalBackupError(ERROR_SCHEMA_UNSUPPORTED, "备份 Schema 基线不受支持")

    application_version = _expect_string(manifest["application_version"], "application_version")
    created_at = _parse_datetime_string(
        _expect_string(manifest["created_at"], "created_at"), require_utc=True
    )

    ledger_sha256 = _expect_string(manifest["ledger_sha256"], "ledger_sha256")
    ledger_size = _expect_int(manifest["ledger_size"], "ledger_size")

    if ledger_size != len(ledger_bytes):
        raise PersonalBackupError(ERROR_LEDGER_SIZE_MISMATCH, "备份数据大小校验失败")
    if sha256(ledger_bytes).hexdigest() != ledger_sha256:
        raise PersonalBackupError(ERROR_LEDGER_SHA_MISMATCH, "备份数据校验失败")

    counts_obj = _expect_object(manifest["counts"], "counts")
    _reject_unknown_keys(
        counts_obj,
        {
            "relationship_types",
            "kinship_types",
            "event_types",
            "contacts",
            "events",
            "gifts",
        },
        "counts",
    )
    _require_keys(
        counts_obj,
        {
            "relationship_types",
            "kinship_types",
            "event_types",
            "contacts",
            "events",
            "gifts",
        },
        "counts",
    )

    ledger = _expect_object(_parse_json(ledger_bytes, LEDGER_FILENAME), "ledger")
    _reject_unknown_keys(
        ledger,
        {
            "self_contact_backup_id",
            "relationship_types",
            "kinship_types",
            "event_types",
            "contacts",
            "events",
            "gifts",
        },
        "ledger",
    )
    _require_keys(
        ledger,
        {
            "self_contact_backup_id",
            "relationship_types",
            "kinship_types",
            "event_types",
            "contacts",
            "events",
            "gifts",
        },
        "ledger",
    )

    relationship_entries = _parse_dictionary_entries(
        ledger["relationship_types"], "relationship_types"
    )
    kinship_entries = _parse_dictionary_entries(ledger["kinship_types"], "kinship_types")
    event_type_entries = _parse_dictionary_entries(ledger["event_types"], "event_types")

    total_records = (
        len(relationship_entries)
        + len(kinship_entries)
        + len(event_type_entries)
        + len(_expect_list(ledger["contacts"], "contacts"))
        + len(_expect_list(ledger["events"], "events"))
        + len(_expect_list(ledger["gifts"], "gifts"))
    )
    if total_records > max_records:
        raise PersonalBackupError(ERROR_RECORD_LIMIT, "备份记录数量超过上限")

    self_contact_backup_id = _expect_int(ledger["self_contact_backup_id"], "self_contact_backup_id")
    if self_contact_backup_id <= 0:
        raise PersonalBackupError(ERROR_SELF_CONTACT_INVALID, "本人联系人编号不正确")

    relationship_ids = {entry.backup_id for entry in relationship_entries}
    kinship_ids = {entry.backup_id for entry in kinship_entries}
    event_type_ids = {entry.backup_id for entry in event_type_entries}

    contacts = _parse_contacts(
        ledger["contacts"],
        relationship_ids=relationship_ids,
        format_version=format_version,
    )
    events = _parse_events(
        ledger["events"],
        kinship_ids=kinship_ids,
        event_type_ids=event_type_ids,
        format_version=format_version,
    )
    gifts, dictionary_name_conflicts = _validate_cross_references(
        contacts=contacts,
        events=events,
        gifts=_parse_gifts(ledger["gifts"]),
        self_contact_backup_id=self_contact_backup_id,
        relationship_entries=relationship_entries,
        kinship_entries=kinship_entries,
        event_type_entries=event_type_entries,
    )

    expected_counts = {
        "relationship_types": len(relationship_entries),
        "kinship_types": len(kinship_entries),
        "event_types": len(event_type_entries),
        "contacts": len(contacts),
        "events": len(events),
        "gifts": len(gifts),
    }
    for key, expected in expected_counts.items():
        actual = _expect_int(counts_obj[key], f"counts.{key}")
        if actual != expected:
            raise PersonalBackupError(ERROR_COUNTS_MISMATCH, "备份数据数量与声明不一致")

    return ParsedPersonalBackup(
        format_name=format_name,
        format_version=format_version,
        application_version=application_version,
        schema_revision=schema_revision,
        created_at=created_at,
        self_contact_backup_id=self_contact_backup_id,
        relationship_types=relationship_entries,
        kinship_types=kinship_entries,
        event_types=event_type_entries,
        contacts=contacts,
        events=events,
        gifts=gifts,
        dictionary_name_conflicts=dictionary_name_conflicts,
    )


def _parse_contacts(
    value: Any, *, relationship_ids: set[int], format_version: int
) -> tuple[BackupContact, ...]:
    items = _expect_list(value, "contacts")
    contacts: list[BackupContact] = []
    ids: list[int] = []
    for item in items:
        obj = _expect_object(item, "contacts")
        _reject_unknown_keys(
            obj,
            {
                "backup_id",
                "name",
                "relationship",
                "location",
                "phone",
                "remark",
                "is_active",
                "created_at",
            },
            "contacts",
        )
        _require_keys(
            obj,
            {
                "backup_id",
                "name",
                "relationship",
                "location",
                "phone",
                "remark",
                "is_active",
                "created_at",
            },
            "contacts",
        )
        backup_id = _expect_int(obj["backup_id"], "contacts.backup_id")
        name = _validate_name(
            _expect_string(obj["name"], "contacts.name"),
            "contacts.name",
            MAX_CONTACT_NAME_LENGTH,
        )
        relationship = _parse_dictionary_ref(
            obj["relationship"],
            field="contacts.relationship",
            custom_ids=relationship_ids,
            category="relationship",
            format_version=format_version,
        )
        location = _normalize_optional_text(
            _expect_nullable_string(obj["location"], "contacts.location"),
            "contacts.location",
            MAX_CONTACT_LOCATION_LENGTH,
        )
        phone = _normalize_contact_phone(_expect_nullable_string(obj["phone"], "contacts.phone"))
        remark = _expect_nullable_string(obj["remark"], "contacts.remark")
        is_active = _expect_bool(obj["is_active"], "contacts.is_active")
        created_at = _parse_datetime_string(
            _expect_string(obj["created_at"], "contacts.created_at")
        )
        ids.append(backup_id)
        contacts.append(
            BackupContact(
                backup_id=backup_id,
                name=name,
                relationship=relationship,
                location=location,
                phone=phone,
                remark=remark,
                is_active=is_active,
                created_at=created_at,
            )
        )
    _validate_backup_ids(ids, "contacts")
    return tuple(contacts)


def _parse_events(
    value: Any,
    *,
    kinship_ids: set[int],
    event_type_ids: set[int],
    format_version: int,
) -> tuple[BackupEvent, ...]:
    items = _expect_list(value, "events")
    events: list[BackupEvent] = []
    ids: list[int] = []
    for item in items:
        obj = _expect_object(item, "events")
        date_keys = (
            {"date_precision", "event_year", "event_month", "date_source"}
            if format_version == FORMAT_VERSION
            else set()
        )
        _reject_unknown_keys(
            obj,
            {
                "backup_id",
                "host_contact",
                "kinship_type",
                "event_type",
                "event_date",
                "remark",
                "created_at",
            }
            | date_keys,
            "events",
        )
        _require_keys(
            obj,
            {
                "backup_id",
                "host_contact",
                "kinship_type",
                "event_type",
                "event_date",
                "remark",
                "created_at",
            }
            | date_keys,
            "events",
        )
        backup_id = _expect_int(obj["backup_id"], "events.backup_id")
        host_contact = _expect_int(obj["host_contact"], "events.host_contact")
        kinship_type = _parse_dictionary_ref(
            obj["kinship_type"],
            field="events.kinship_type",
            custom_ids=kinship_ids,
            category="kinship",
            format_version=format_version,
        )
        event_type = _parse_dictionary_ref(
            obj["event_type"],
            field="events.event_type",
            custom_ids=event_type_ids,
            category="event",
            format_version=format_version,
        )
        event_date = (
            _parse_date_string(_expect_string(obj["event_date"], "events.event_date"))
            if obj["event_date"] is not None
            else None
        )
        if format_version == FORMAT_VERSION:
            try:
                value = EventDateValue(
                    event_date, obj["date_precision"], obj["event_year"], obj["event_month"]
                )
                if event_date is not None and (
                    obj["event_year"] != event_date.year or obj["event_month"] != event_date.month
                ):
                    raise ValueError("完整日期缺少一致的年、月")
                date_fields = event_date_fields(value, source=obj["date_source"])
            except (ValueError, TypeError, KeyError):
                raise PersonalBackupError(
                    ERROR_DATE_INVALID, "备份办事日期精度或组成不正确"
                ) from None
        else:
            if event_date is None:
                raise PersonalBackupError(ERROR_DATE_INVALID, "旧版备份必须有完整办事日期")
            date_fields = event_date_fields(EventDateValue(event_date, "day"), source="web")
        remark = _expect_nullable_string(obj["remark"], "events.remark")
        created_at = _parse_datetime_string(_expect_string(obj["created_at"], "events.created_at"))
        ids.append(backup_id)
        events.append(
            BackupEvent(
                backup_id=backup_id,
                host_contact=host_contact,
                kinship_type=kinship_type,
                event_type=event_type,
                **date_fields,
                remark=remark,
                created_at=created_at,
            )
        )
    _validate_backup_ids(ids, "events")
    return tuple(events)


def _parse_gifts(value: Any) -> tuple[BackupGift, ...]:
    items = _expect_list(value, "gifts")
    gifts: list[BackupGift] = []
    ids: list[int] = []
    for item in items:
        obj = _expect_object(item, "gifts")
        _reject_unknown_keys(
            obj,
            {
                "backup_id",
                "event",
                "giver_contact",
                "amount",
                "gift_details",
                "remark",
                "created_at",
            },
            "gifts",
        )
        _require_keys(
            obj,
            {
                "backup_id",
                "event",
                "giver_contact",
                "amount",
                "gift_details",
                "remark",
                "created_at",
            },
            "gifts",
        )
        backup_id = _expect_int(obj["backup_id"], "gifts.backup_id")
        event = _expect_int(obj["event"], "gifts.event")
        giver_contact = _expect_int(obj["giver_contact"], "gifts.giver_contact")
        amount_text = _expect_string(obj["amount"], "gifts.amount")
        amount = _parse_amount(amount_text)
        gift_details = _expect_nullable_string(obj["gift_details"], "gifts.gift_details")
        remark = _expect_nullable_string(obj["remark"], "gifts.remark")
        created_at = _parse_datetime_string(_expect_string(obj["created_at"], "gifts.created_at"))
        ids.append(backup_id)
        gifts.append(
            BackupGift(
                backup_id=backup_id,
                event=event,
                giver_contact=giver_contact,
                amount=amount,
                gift_details=gift_details,
                remark=remark,
                created_at=created_at,
                direction=LedgerDirection.INCOME,
            )
        )
    _validate_backup_ids(ids, "gifts")
    return tuple(gifts)


_AMOUNT_PATTERN = re.compile(r"(?:0|[1-9]\d{0,7})\.\d{2}\Z")


def _parse_amount(text: str) -> Decimal:
    if _AMOUNT_PATTERN.fullmatch(text) is None:
        raise PersonalBackupError(ERROR_FIELD_TYPE, "礼金金额格式不正确")
    return Decimal(text)


def _validate_cross_references(
    *,
    contacts: tuple[BackupContact, ...],
    events: tuple[BackupEvent, ...],
    gifts: tuple[BackupGift, ...],
    self_contact_backup_id: int,
    relationship_entries: tuple[BackupDictionaryEntry, ...],
    kinship_entries: tuple[BackupDictionaryEntry, ...],
    event_type_entries: tuple[BackupDictionaryEntry, ...],
) -> tuple[tuple[BackupGift, ...], tuple[BackupDictionaryNameConflict, ...]]:
    contact_ids = {contact.backup_id for contact in contacts}
    event_ids = {event.backup_id for event in events}

    if self_contact_backup_id not in contact_ids:
        raise PersonalBackupError(ERROR_SELF_CONTACT_INVALID, "本人联系人编号不正确")

    self_count = sum(1 for contact in contacts if _is_self_relationship(contact.relationship))
    if self_count != 1:
        raise PersonalBackupError(ERROR_SELF_CONTACT_INVALID, "本人联系人必须恰好一个")

    self_contact = next(
        contact for contact in contacts if _is_self_relationship(contact.relationship)
    )
    if self_contact.backup_id != self_contact_backup_id:
        raise PersonalBackupError(ERROR_SELF_CONTACT_INVALID, "本人联系人编号与声明不一致")

    # 联系人身份唯一（D108：姓名＋备注）。
    identities: set[tuple[str, str]] = set()
    for contact in contacts:
        identity = contact_identity_components(contact.name, contact.location, contact.remark)
        if identity in identities:
            raise PersonalBackupError(ERROR_CONTACT_IDENTITY_DUPLICATE, "存在身份重复的联系人")
        identities.add(identity)

    # 自定义字典名称在各类别内规范化后唯一；与 S006 系统名称的冲突留给预览改名。
    conflicts = (
        *_validate_dictionary_names(
            "relationship", relationship_entries, SYSTEM_RELATIONSHIP_NAMES
        ),
        *_validate_dictionary_names("kinship", kinship_entries, SYSTEM_KINSHIP_NAMES),
        *_validate_dictionary_names("event", event_type_entries, SYSTEM_EVENT_NAMES),
    )

    # 事件身份在文件内唯一，且引用存在。
    # 系统字典的显示名称可随版本调整，身份只按稳定编码；自定义项用备份内编号。
    def dictionary_identity(ref: BackupDictionaryRef) -> tuple[str, str | int | None]:
        return (ref.scope, ref.system_code if ref.scope == "system" else ref.backup_id)

    event_identities: dict[tuple[Any, ...], list[int]] = {}
    for event in events:
        if event.host_contact not in contact_ids:
            raise PersonalBackupError(ERROR_REFERENCE_MISSING, "事件引用了不存在的事主联系人")
        identity = (
            event.host_contact,
            dictionary_identity(event.kinship_type),
            dictionary_identity(event.event_type),
            (event.remark or "").strip(),
        )
        event_identities.setdefault(identity, []).append(event.backup_id)
    duplicate_ids = [ids for ids in event_identities.values() if len(ids) > 1]
    if duplicate_ids:
        groups = "；".join(
            "、".join(str(item) for item in ids[:20]) + ("等" if len(ids) > 20 else "")
            for ids in duplicate_ids[:10]
        )
        if len(duplicate_ids) > 10:
            groups += f"；共 {len(duplicate_ids)} 组冲突"
        raise PersonalBackupError(
            ERROR_EVENT_DUPLICATE,
            "备份中的事件按事主、亲属称谓、事件类型和备注判重后存在冲突"
            f"（备份事件编号：{groups}）；恢复不会自动合并，请先核对并区分事件备注。",
        )

    # 流水引用、事主≠随礼人、同一事件同一随礼人唯一、方向可推导。
    host_by_event = {event.backup_id: event.host_contact for event in events}
    gift_keys: set[tuple[int, int]] = set()
    resolved_gifts: list[BackupGift] = []
    for gift in gifts:
        if gift.event not in event_ids:
            raise PersonalBackupError(ERROR_REFERENCE_MISSING, "流水引用了不存在的事件")
        if gift.giver_contact not in contact_ids:
            raise PersonalBackupError(ERROR_REFERENCE_MISSING, "流水引用了不存在的随礼人联系人")
        host = host_by_event[gift.event]
        try:
            validated_amount, normalized_details = validate_gift_value(
                gift.amount, gift.gift_details
            )
            direction = derive_direction(
                self_contact_id=self_contact_backup_id,
                host_contact_id=host,
                giver_contact_id=gift.giver_contact,
            )
        except LedgerValidationError as error:
            raise PersonalBackupError(error.code, error.message) from error
        key = (gift.event, gift.giver_contact)
        if key in gift_keys:
            raise PersonalBackupError(ERROR_GIFT_DUPLICATE, "同一事件同一随礼人存在重复流水")
        gift_keys.add(key)
        resolved_gifts.append(
            replace(
                gift,
                amount=validated_amount,
                gift_details=normalized_details,
                direction=direction,
            )
        )
    return tuple(resolved_gifts), tuple(conflicts)


def _validate_dictionary_names(
    category: str,
    entries: tuple[BackupDictionaryEntry, ...],
    system_names: tuple[str, ...],
) -> tuple[BackupDictionaryNameConflict, ...]:
    seen: set[str] = set()
    system_keys = {_normalize_key(name) for name in system_names}
    conflicts: list[BackupDictionaryNameConflict] = []
    all_keys = system_keys | {_normalize_key(entry.name) for entry in entries}
    for entry in entries:
        key = _normalize_key(entry.name)
        if key in seen:
            raise PersonalBackupError(ERROR_DICTIONARY_NAME_DUPLICATE, "存在重复的自定义字典名称")
        if key in system_keys:
            base = f"{entry.name}（原自定义）"
            suggested = base[:MAX_DICTIONARY_NAME_LENGTH]
            index = 2
            while _normalize_key(suggested) in all_keys:
                suffix = f"（原自定义{index}）"
                suggested = f"{entry.name[: MAX_DICTIONARY_NAME_LENGTH - len(suffix)]}{suffix}"
                index += 1
            all_keys.add(_normalize_key(suggested))
            conflicts.append(
                BackupDictionaryNameConflict(
                    category=category,
                    backup_id=entry.backup_id,
                    original_name=entry.name,
                    suggested_name=suggested,
                )
            )
        seen.add(key)
    return tuple(conflicts)


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_MAX_UNCOMPRESSED_BYTES",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "LEDGER_FILENAME",
    "MANIFEST_FILENAME",
    "PACKAGE_SUFFIX",
    "S005_FORMAT_VERSION",
    "S005_SCHEMA_REVISION",
    "S006_SCHEMA_REVISION",
    "S007_SCHEMA_REVISION",
    "S008_SCHEMA_REVISION",
    "SCHEMA_REVISION",
    "BackupContact",
    "BackupCounts",
    "BackupDictionaryEntry",
    "BackupDictionaryNameConflict",
    "BackupDictionaryRef",
    "BackupEvent",
    "BackupGift",
    "ParsedPersonalBackup",
    "PersonalBackupDownload",
    "PersonalBackupError",
    "build_personal_backup",
    "parse_personal_backup",
]
