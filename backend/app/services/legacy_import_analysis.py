# ruff: noqa: RUF002, RUF003
"""旧账导入的只读业务预检分析。

本模块把解析器产出的 :class:`~app.services.old_ledger_excel.ParsedOldLedgerWorkbook`
转换成一份足以在同一次事务里完成写入的 :class:`LegacyImportAnalysis`：

- 逐行必修问题、待确认项和提示带 Excel 行号、原值、处理建议及可用确认选项；
- 每个有效行解析后的事主、随礼人、三类字典、事件身份、金额、备注和方向；
- 去重后的待新建联系人、待新建字典、待新建事件，以及复用联系人和事件的编号集合；
- 总行数、有效行数、错误行数、新增收支金额、纯实物子集及事件日期精度汇总；
- ``has_errors`` 属性：存在任一必修问题或未完成的待确认项，后续就不得写入。

本模块是**只读**的：只对 ``session`` 做 ``SELECT``，绝不调用
``add / flush / commit / rollback / delete``，也绝不修改任何 ORM 对象。所有查询都带当前
用户隔离条件；字典只看系统项与当前用户自定义项，绝不读取其他用户的数据。

待创建对象用稳定的规范化业务键关联，不依赖伪造的数据库编号：

- 联系人以规范化后的 ``(姓名, 备注)`` 为键；同名未精确匹配须明确选择已有联系人，
  或以不同非空备注新建。关系差异、已有常住地差异分别确认，不静默改写已有资料；
- 字典项以 ``(类别, 规范化名称)`` 为键；
- 事件以 ``(事主身份, 亲属称谓, 事件类型, 首尾去空白的备注)`` 为键；日期独立按
  完整日期、年月、年份、不详四种精度匹配，已知部分冲突阻断，补齐已有事件须明确确认。
- 同一事件与同一随礼人的重复流水逐行阻断，不静默去重、累计或覆盖。

单行分析是原子的：一行解析期间产生的待新建对象、复用编号与礼金键都先
写进该行的 :class:`_RowDraft`，只有整行成功产出计划时才合并回全局状态；失败行只保留
问题，不留下写入副作用。文件内关联的日期与重复检查完成后，汇总只来源于最终可写入行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, EventType, GiftRecord, KinshipType, RelationshipType
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.ledger import LedgerDirection
from app.schemas.legacy_import import (
    LegacyImportContactSummary,
    LegacyImportDictionarySummary,
    LegacyImportIssueResponse,
    LegacyImportPreviewResponse,
    LegacyImportRowSummary,
    LegacyImportTotals,
)
from app.services.contact_identity import contact_identity_components, normalize_identity_component
from app.services.event_dates import (
    EventDateValue,
    date_value,
    display_event_date,
    merge_event_dates,
)
from app.services.event_identity import event_identity_components
from app.services.ledger import LedgerValidationError, derive_direction, validate_gift_value
from app.services.old_ledger_excel import OldLedgerRow, ParsedOldLedgerWorkbook
from app.services.old_ledger_template import COLUMN_HEADERS
from app.services.system_dictionary_catalog import (
    S005_EXCEL_ALIASES_BY_CATEGORY,
    SELF_RELATIONSHIP_SYSTEM_CODE,
)

# ---------------------------------------------------------------- 常量与错误码

MAX_CONTACT_NAME_LENGTH = 50
MAX_CONTACT_LOCATION_LENGTH = 50
MAX_DICTIONARY_NAME_LENGTH = 50

# 全局 / 文件级。
ERROR_EMPTY_WORKBOOK = "empty_workbook"
ERROR_SELF_CONTACT_REQUIRED = "self_contact_required"

# 字段必填与长度。
ERROR_CONTACT_NAME_REQUIRED = "contact_name_required"
ERROR_CONTACT_NAME_TOO_LONG = "contact_name_too_long"
ERROR_CONTACT_LOCATION_TOO_LONG = "contact_location_too_long"
ERROR_DICTIONARY_NAME_REQUIRED = "dictionary_name_required"
ERROR_DICTIONARY_NAME_TOO_LONG = "dictionary_name_too_long"
ERROR_EVENT_DATE_REQUIRED = "date_required"
ERROR_AMOUNT_REQUIRED = "amount_required"
ERROR_EVENT_DATE_INVALID = "event_date_invalid"

# 本人识别。
ERROR_SELF_NAME_MISMATCH = "self_name_mismatch"
ERROR_SELF_PROFILE_MISMATCH = "self_profile_mismatch"

# 联系人。
ERROR_CONTACT_INACTIVE = "contact_inactive"
ERROR_CONTACT_RELATIONSHIP_MISMATCH = "contact_relationship_mismatch"
ERROR_CONTACT_DISTINGUISHING_INFO_REQUIRED = "contact_distinguishing_info_required"
ERROR_CONTACT_FILE_RELATIONSHIP_MISMATCH = "contact_file_relationship_mismatch"

# 字典。
ERROR_DICTIONARY_INACTIVE = "dictionary_inactive"
ERROR_SYSTEM_DICTIONARY_UNAVAILABLE = "system_dictionary_unavailable"

# 流水。
ERROR_GIFT_DUPLICATE = "gift_duplicate"
ERROR_GIFT_DUPLICATE_IN_FILE = "gift_duplicate_in_file"

_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)

_DICTIONARY_MODELS: dict[DictionaryCategory, type[Any]] = {
    DictionaryCategory.RELATIONSHIP: RelationshipType,
    DictionaryCategory.KINSHIP: KinshipType,
    DictionaryCategory.EVENT: EventType,
}

# 金额 / 方向错误到模板字段的映射。金额与方向规则复用现有服务，其错误码原样透传。
_AMOUNT_ERROR_FIELDS = {
    "GIFT_AMOUNT_INVALID": "礼金金额",
    "GIFT_AMOUNT_NEGATIVE": "礼金金额",
    "GIFT_AMOUNT_TOO_LARGE": "礼金金额",
    "GIFT_AMOUNT_DECIMAL_PLACES": "礼金金额",
    "GIFT_DETAILS_TOO_LONG": "实物明细",
    "GIFT_VALUE_REQUIRED": "礼金金额",
}
_DIRECTION_ERROR_FIELDS = {
    "HOST_GIVER_SAME": "随礼人姓名",
    "THIRD_PARTY_LEDGER_FORBIDDEN": "事主姓名",
}

_HOST_FIELDS = ("事主姓名", "事主常住地", "事主联系人备注", "事主关系类型")
_GIVER_FIELDS = ("随礼人姓名", "随礼人常住地", "随礼人联系人备注", "随礼人关系类型")


# ---------------------------------------------------------------- 冻结数据结构


@dataclass(frozen=True, slots=True)
class LegacyImportIssue:
    """预检发现的一个问题。``row_number`` 与 ``field`` 对全局问题可为 ``None``。"""

    row_number: int | None
    field: str | None
    code: str
    message: str
    severity: str = "error"
    raw_value: str | None = None
    remedy: str = "请根据提示修正模板后重新预检。"
    confirmation_key: str | None = None
    options: tuple[tuple[str, str], ...] = ()
    existing_event_id: int | None = None
    existing_gift_id: int | None = None


@dataclass(frozen=True, slots=True)
class DictionaryReference:
    """一个解析后的字典引用：已有项用 ``existing_id``，待新建项 ``existing_id`` 为 ``None``。"""

    category: DictionaryCategory
    name: str
    existing_id: int | None
    system_code: str | None = None


@dataclass(frozen=True, slots=True)
class ContactReference:
    """一个解析后的事主或随礼人引用。``identity`` 是 S005 规范化后的稳定业务键。"""

    name: str
    location: str | None
    remark: str | None
    identity: tuple[str, str]
    relationship: DictionaryReference
    existing_id: int | None
    is_self: bool


@dataclass(frozen=True, slots=True)
class EventReference:
    """一个解析后的事件引用。``existing_id`` 为 ``None`` 表示待新建。"""

    host: ContactReference
    kinship: DictionaryReference
    event_type: DictionaryReference
    event_date: date | None
    remark: str | None
    existing_id: int | None
    date_precision: str = "day"
    event_year: int | None = None
    event_month: int | None = None


@dataclass(frozen=True, slots=True)
class LegacyImportRowPlan:
    """一个有效行解析后的完整写入计划。"""

    row_number: int
    host: ContactReference
    giver: ContactReference
    kinship: DictionaryReference
    event_type: DictionaryReference
    event: EventReference
    amount: Decimal
    gift_details: str | None
    gift_remark: str | None
    direction: LedgerDirection


@dataclass(frozen=True, slots=True)
class LegacyImportAnalysis:
    """整个文件的只读预检结果。"""

    total_rows: int
    valid_rows: int
    error_rows: int
    errors: tuple[LegacyImportIssue, ...]
    rows: tuple[LegacyImportRowPlan, ...]
    new_contacts: tuple[ContactReference, ...]
    new_dictionary_items: tuple[DictionaryReference, ...]
    new_events: tuple[EventReference, ...]
    reused_contact_ids: frozenset[int]
    reused_event_ids: frozenset[int]
    new_gift_count: int
    date_completions: tuple[EventReference, ...] = ()
    issues: tuple[LegacyImportIssue, ...] = ()
    date_completion_context: tuple[tuple[Any, ...], ...] = ()

    @property
    def has_errors(self) -> bool:
        return bool(self.errors) or any(issue.severity == "confirmation" for issue in self.issues)


# ---------------------------------------------------------------- 内部处理状态


@dataclass
class _EventHolder:
    """待新建事件占位；收尾阶段合并同一身份各行兼容的日期已知部分。"""

    host: ContactReference
    kinship: DictionaryReference
    event_type: DictionaryReference
    event_date: date
    remark: str | None


@dataclass
class _RowDraft:
    """单行分析期间的暂存增量。只有整行成功产出计划后才合并回全局状态。"""

    dictionaries: dict[tuple[DictionaryCategory, str], DictionaryReference] = field(
        default_factory=dict
    )
    contacts: dict[tuple[str, str], ContactReference] = field(default_factory=dict)
    events: dict[tuple[Any, ...], _EventHolder] = field(default_factory=dict)
    reused_contact_ids: set[int] = field(default_factory=set)
    reused_event_ids: set[int] = field(default_factory=set)
    gift_file_keys: set[tuple[Any, ...]] = field(default_factory=set)
    placeholders: dict[tuple[str, str], int] = field(default_factory=dict)
    placeholder_counter: int = 0


@dataclass(frozen=True, slots=True)
class _EventResolution:
    """事件解析结果：要么复用已有事件，要么引用一个待新建事件的内部占位。"""

    existing_event: EventReference | None
    event_holder: _EventHolder | None


@dataclass(frozen=True, slots=True)
class _RowResult:
    """单行成功解析的中间结果；最终 ``LegacyImportRowPlan`` 在收尾阶段按规范事件备注生成。"""

    row_number: int
    host: ContactReference
    giver: ContactReference
    kinship: DictionaryReference
    event_type: DictionaryReference
    existing_event: EventReference | None
    event_holder: _EventHolder | None
    amount: Decimal
    gift_details: str | None
    gift_remark: str | None
    direction: LedgerDirection
    incoming_date: EventDateValue


class _ImportState:
    def __init__(self, session: Session, user_id: int, decisions: dict[str, str]) -> None:
        self.session = session
        self.user_id = user_id
        self.issues: list[LegacyImportIssue] = []
        self.decisions = decisions
        self.contact_inputs: dict[tuple[str, str], list[tuple[int, str | None]]] = {}
        self.contacts_by_name: dict[str, list[Contact]] = {}
        self.file_identities_by_name: dict[str, set[tuple[str, str]]] = {}
        self.gift_file_rows: dict[tuple[Any, ...], int] = {}
        self.raw_by_row: dict[int, tuple[Any, ...]] = {}
        self.self_contact: Contact | None = None
        self.contacts_by_identity: dict[tuple[str, str], Contact] = {}
        self.events_by_identity: dict[tuple[int, int, int, str], EventRecord] = {}
        self.gifts_by_event_giver: dict[tuple[int, int], GiftRecord] = {}
        self.dictionary_index: dict[DictionaryCategory, dict[str, Any]] = {}
        self.system_dictionary_by_code: dict[DictionaryCategory, dict[str, Any]] = {}
        self.planned_contacts: dict[tuple[str, str], ContactReference] = {}
        self.planned_dictionaries: dict[tuple[DictionaryCategory, str], DictionaryReference] = {}
        self.planned_events: dict[tuple[Any, ...], _EventHolder] = {}
        self.planned_gift_file_keys: set[tuple[Any, ...]] = set()
        self.reused_contact_ids: set[int] = set()
        self.reused_event_ids: set[int] = set()


def _commit_row(state: _ImportState, draft: _RowDraft) -> None:
    """把一行成功分析产生的增量合并回全局状态。"""

    for key, reference in draft.dictionaries.items():
        state.planned_dictionaries[key] = reference
    for key, reference in draft.contacts.items():
        state.planned_contacts[key] = reference
    for key, holder in draft.events.items():
        state.planned_events[key] = holder
    state.reused_contact_ids.update(draft.reused_contact_ids)
    state.reused_event_ids.update(draft.reused_event_ids)
    state.planned_gift_file_keys.update(draft.gift_file_keys)


def _direction_id(state: _ImportState, draft: _RowDraft, ref: ContactReference) -> int:
    if ref.is_self:
        return state.self_contact.id  # type: ignore[union-attr]
    if ref.existing_id is not None:
        return ref.existing_id
    placeholder = draft.placeholders.get(ref.identity)
    if placeholder is None:
        draft.placeholder_counter -= 1
        placeholder = draft.placeholder_counter
        draft.placeholders[ref.identity] = placeholder
    return placeholder


# ---------------------------------------------------------------- 数据加载（只读）


def _load_self_contact(session: Session, user_id: int) -> Contact | None:
    return session.scalar(
        select(Contact)
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                RelationshipType.user_id.is_(None),
                RelationshipType.system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
            ),
        )
        .where(Contact.user_id == user_id)
    )


def _load_contacts(session: Session, user_id: int) -> list[Contact]:
    return list(session.scalars(select(Contact).where(Contact.user_id == user_id)).all())


def _load_events(session: Session, user_id: int) -> list[EventRecord]:
    return list(session.scalars(select(EventRecord).where(EventRecord.user_id == user_id)).all())


def _load_gifts(session: Session, user_id: int) -> list[GiftRecord]:
    return list(session.scalars(select(GiftRecord).where(GiftRecord.user_id == user_id)).all())


def _load_dictionary_index(
    session: Session, user_id: int
) -> tuple[
    dict[DictionaryCategory, dict[str, Any]],
    dict[DictionaryCategory, dict[str, Any]],
]:
    name_index: dict[DictionaryCategory, dict[str, Any]] = {}
    code_index: dict[DictionaryCategory, dict[str, Any]] = {}
    for category, model in _DICTIONARY_MODELS.items():
        statement = select(model).where(or_(model.user_id.is_(None), model.user_id == user_id))
        items = list(session.scalars(statement).all())
        name_index[category] = {_dictionary_name_key(item.type_name): item for item in items}
        code_index[category] = {
            item.system_code: item
            for item in items
            if item.user_id is None and item.system_code is not None
        }
    return name_index, code_index


def _load_lookup_data(state: _ImportState) -> None:
    state.contacts_by_identity = {
        contact_identity_components(contact.name, contact.location, contact.remark): contact
        for contact in _load_contacts(state.session, state.user_id)
    }
    for contact in state.contacts_by_identity.values():
        state.contacts_by_name.setdefault(normalize_identity_component(contact.name), []).append(
            contact
        )
    state.events_by_identity = {
        event_identity_components(
            event.contact_id, event.relative_type_id, event.event_type_id, event.remark
        ): event
        for event in _load_events(state.session, state.user_id)
    }
    state.gifts_by_event_giver = {
        (gift.event_record_id, gift.contact_id): gift
        for gift in _load_gifts(state.session, state.user_id)
    }
    state.dictionary_index, state.system_dictionary_by_code = _load_dictionary_index(
        state.session, state.user_id
    )


# ---------------------------------------------------------------- 小工具


def _dictionary_name_key(name: str) -> str:
    """字典名称的规范化键：去除首尾空白并按 utf8mb4_0900_as_ci 忽略 ASCII 大小写。"""

    return name.strip().translate(_ASCII_LOWER_TRANSLATION)


def _issue(row_number: int | None, field: str | None, code: str, message: str) -> LegacyImportIssue:
    return LegacyImportIssue(row_number, field, code, message)


def _confirmation(state, *, key, row_number, field, code, message, raw_value, options, remedy=None):
    allowed = {value for value, _label in options}
    chosen = state.decisions.get(key)
    if chosen in allowed:
        return chosen
    prior = next(
        (index for index, issue in enumerate(state.issues) if issue.confirmation_key == key), None
    )
    if prior is None:
        state.issues.append(
            LegacyImportIssue(
                row_number,
                field,
                code,
                message,
                "confirmation",
                raw_value,
                remedy or "选择处理方式后重新预检；确认决定适用于文件内同一对象的全部行。",
                key,
                tuple(options),
            )
        )
    elif row_number != state.issues[prior].row_number:
        note = f"第 {row_number} 行「{field}」：{raw_value or '空白'}"
        issue = state.issues[prior]
        if note not in issue.message:
            state.issues[prior] = replace(issue, message=issue.message + f"；同一决定也涉及 {note}")
    return None


def _stable_key(kind, *components):
    raw = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
    return kind + ":" + sha256(raw.encode()).hexdigest()[:24]


def _info(state, row_number, field, code, message, raw_value=None):
    state.issues.append(
        LegacyImportIssue(
            row_number, field, code, message, "info", raw_value, "信息已列入最终导入计划，请核对。"
        )
    )


# ---------------------------------------------------------------- 字段校验


def _contact_name(
    state: _ImportState, value: str | None, field: str, row_number: int
) -> str | None:
    if value is None:
        state.issues.append(
            _issue(
                row_number, field, ERROR_CONTACT_NAME_REQUIRED, f"「{field}」是必填项，请填写姓名。"
            )
        )
        return None
    if len(value) > MAX_CONTACT_NAME_LENGTH:
        state.issues.append(
            _issue(
                row_number,
                field,
                ERROR_CONTACT_NAME_TOO_LONG,
                f"「{field}」最多允许 {MAX_CONTACT_NAME_LENGTH} 个字符。",
            )
        )
        return None
    return value


def _contact_location(
    state: _ImportState, value: str | None, field: str, row_number: int
) -> str | None:
    if value is None:
        return None
    if len(value) > MAX_CONTACT_LOCATION_LENGTH:
        state.issues.append(
            _issue(
                row_number,
                field,
                ERROR_CONTACT_LOCATION_TOO_LONG,
                f"「{field}」最多允许 {MAX_CONTACT_LOCATION_LENGTH} 个字符。",
            )
        )
        return None
    return value


def _dictionary_name(
    state: _ImportState, value: str | None, field: str, row_number: int
) -> str | None:
    if value is None:
        state.issues.append(
            _issue(
                row_number, field, ERROR_DICTIONARY_NAME_REQUIRED, f"「{field}」是必填项，请填写。"
            )
        )
        return None
    if len(value) > MAX_DICTIONARY_NAME_LENGTH:
        state.issues.append(
            _issue(
                row_number,
                field,
                ERROR_DICTIONARY_NAME_TOO_LONG,
                f"「{field}」最多允许 {MAX_DICTIONARY_NAME_LENGTH} 个字符。",
            )
        )
        return None
    return value


def _validate_amount(
    state: _ImportState,
    amount: Decimal,
    gift_details: str | None,
    row_number: int,
) -> tuple[Decimal, str | None] | None:
    try:
        return validate_gift_value(amount, gift_details)
    except LedgerValidationError as exc:
        field = _AMOUNT_ERROR_FIELDS.get(exc.code, "礼金金额")
        state.issues.append(_issue(row_number, field, exc.code, exc.message))
        return None


# ---------------------------------------------------------------- 字典解析


def _resolve_dictionary(
    state: _ImportState,
    draft: _RowDraft,
    category: DictionaryCategory,
    name: str,
    field: str,
    row_number: int,
) -> DictionaryReference | None:
    alias_code = S005_EXCEL_ALIASES_BY_CATEGORY[category.value].get(name)
    if alias_code is not None:
        item = state.system_dictionary_by_code[category].get(alias_code)
        if item is None:
            state.issues.append(
                _issue(
                    row_number,
                    field,
                    ERROR_SYSTEM_DICTIONARY_UNAVAILABLE,
                    f"「{field}」对应的系统内置项不可用，请联系管理员检查字典配置。",
                )
            )
            return None
        if not item.is_active:
            state.issues.append(
                _issue(
                    row_number,
                    field,
                    ERROR_DICTIONARY_INACTIVE,
                    f"「{field}」填写的「{item.type_name}」已停用，请先重新启用或改用其他项。",
                )
            )
            return None
        if name != item.type_name:
            _info(
                state,
                row_number,
                field,
                "dictionary_alias",
                f"旧名称「{name}」对应当前「{item.type_name}」。",
                name,
            )
        return DictionaryReference(category, item.type_name, item.id, item.system_code)

    name_key = _dictionary_name_key(name)
    item = state.dictionary_index[category].get(name_key)
    if item is not None:
        if not item.is_active:
            state.issues.append(
                _issue(
                    row_number,
                    field,
                    ERROR_DICTIONARY_INACTIVE,
                    f"「{field}」填写的「{name}」已停用，请先重新启用或改用其他项。",
                )
            )
            return None
        return DictionaryReference(category, item.type_name, item.id, item.system_code)

    key = (category, name_key)
    options = [("create", f"新建个人字典项：{name}")]
    options.extend(
        (f"map:{item.id}", f"采用已有项：{item.type_name}")
        for item in state.dictionary_index[category].values()
        if item.is_active
    )
    decision = _confirmation(
        state,
        key=_stable_key("dictionary", category.value, name_key),
        row_number=row_number,
        field=field,
        code="dictionary_confirmation",
        raw_value=name,
        options=options,
        message=f"「{name}」不在当前可用字典中，请确认新建个人项或映射已有项。",
    )
    if decision and decision.startswith("map:"):
        item_id = int(decision[4:])
        item = next(
            item
            for item in state.dictionary_index[category].values()
            if item.id == item_id and item.is_active
        )
        _info(
            state,
            row_number,
            field,
            "dictionary_mapped",
            f"「{name}」采用「{item.type_name}」。",
            name,
        )
        return DictionaryReference(category, item.type_name, item.id, item.system_code)
    reference = draft.dictionaries.get(key)
    if reference is None:
        reference = state.planned_dictionaries.get(key)
    if reference is None:
        reference = DictionaryReference(category, name, None)
        draft.dictionaries[key] = reference
    return reference


# ---------------------------------------------------------------- 联系人解析


def _resolve_contact(
    state: _ImportState,
    draft: _RowDraft,
    *,
    name: str,
    location: str | None,
    remark: str | None,
    relationship: DictionaryReference,
    fields: tuple[str, str, str, str],
    row_number: int,
) -> ContactReference | None:
    name_field, location_field, remark_field, relationship_field = fields
    if relationship.system_code == SELF_RELATIONSHIP_SYSTEM_CODE:
        return _resolve_self_contact(
            state,
            name=name,
            location=location,
            remark=remark,
            relationship=relationship,
            name_field=name_field,
            location_field=location_field,
            remark_field=remark_field,
            row_number=row_number,
        )
    return _resolve_ordinary_contact(
        state,
        draft,
        name=name,
        location=location,
        remark=remark,
        relationship=relationship,
        name_field=name_field,
        relationship_field=relationship_field,
        row_number=row_number,
    )


def _resolve_self_contact(
    state: _ImportState,
    *,
    name: str,
    location: str | None,
    remark: str | None,
    relationship: DictionaryReference,
    name_field: str,
    location_field: str,
    remark_field: str,
    row_number: int,
) -> ContactReference | None:
    self_contact = state.self_contact
    assert self_contact is not None
    if normalize_identity_component(name, required=True) != normalize_identity_component(
        self_contact.name, required=True
    ):
        state.issues.append(
            _issue(
                row_number,
                name_field,
                ERROR_SELF_NAME_MISMATCH,
                f"「{name_field}」填写的姓名与当前本人姓名不一致，本人请填写真实姓名。",
            )
        )
        return None
    if location is not None and normalize_identity_component(
        location
    ) != normalize_identity_component(self_contact.location):
        state.issues.append(
            _issue(
                row_number,
                location_field,
                ERROR_SELF_PROFILE_MISMATCH,
                f"「{location_field}」与当前本人资料不一致。",
            )
        )
        return None
    if remark is not None and normalize_identity_component(remark) != normalize_identity_component(
        self_contact.remark
    ):
        state.issues.append(
            _issue(
                row_number,
                remark_field,
                ERROR_SELF_PROFILE_MISMATCH,
                f"「{remark_field}」与当前本人资料不一致。",
            )
        )
        return None
    return ContactReference(
        name=self_contact.name,
        location=self_contact.location,
        remark=self_contact.remark,
        identity=contact_identity_components(
            self_contact.name, self_contact.location, self_contact.remark
        ),
        relationship=relationship,
        existing_id=self_contact.id,
        is_self=True,
    )


def _resolve_ordinary_contact(
    state: _ImportState,
    draft: _RowDraft,
    *,
    name: str,
    location: str | None,
    remark: str | None,
    relationship: DictionaryReference,
    name_field: str,
    relationship_field: str,
    row_number: int,
) -> ContactReference | None:
    identity = contact_identity_components(name, location, remark)
    existing = state.contacts_by_identity.get(identity)
    candidates = [
        c for c in state.contacts_by_name.get(identity[0], []) if c.id != state.self_contact.id
    ]
    if existing is None and (
        candidates or len(state.file_identities_by_name.get(identity[0], ())) > 1
    ):
        options = [
            (
                f"reuse:{c.id}",
                f"复用：{c.name} · {c.remark or '无备注'} · {'启用' if c.is_active else '停用'}",
            )
            for c in candidates
        ]
        if identity[1] and all(
            normalize_identity_component(c.remark) != identity[1] for c in candidates
        ):
            options.append(("create", f"新建同名联系人：{name} · {remark}"))
        decision = _confirmation(
            state,
            key=_stable_key("contact", *identity),
            row_number=row_number,
            field=name_field,
            code="contact_match_confirmation",
            raw_value=f"{name} · {remark or '无备注'}",
            options=options,
            message="发现同名联系人，请核对联系人备注后选择复用或新建。",
            remedy="同名新建须在模板填写不同的联系人备注后重新上传；常住地不能用于区分身份。",
        )
        if decision is None:
            if not options:
                state.issues.append(
                    _issue(
                        row_number,
                        name_field,
                        ERROR_CONTACT_DISTINGUISHING_INFO_REQUIRED,
                        "同名联系人需要不同的联系人备注，请回模板补充后上传。",
                    )
                )
            return None
        if decision.startswith("reuse:"):
            existing = next(c for c in candidates if c.id == int(decision[6:]))
    if existing is not None:
        if not existing.is_active:
            state.issues.append(
                _issue(
                    row_number,
                    name_field,
                    ERROR_CONTACT_INACTIVE,
                    f"「{name_field}」对应的联系人已停用，请先重新启用后再导入。",
                )
            )
            return None
        existing_relationship = next(
            (
                item
                for item in state.dictionary_index[DictionaryCategory.RELATIONSHIP].values()
                if item.id == existing.relation_type_id and item.is_active
            ),
            None,
        )
        if existing_relationship is None:
            state.issues.append(
                _issue(
                    row_number,
                    relationship_field,
                    ERROR_DICTIONARY_INACTIVE,
                    "已有联系人的关系类型不可用，请先在字典管理中处理。",
                )
            )
            return None
        if existing.relation_type_id != relationship.existing_id:
            decision = _confirmation(
                state,
                key=f"relationship:{existing.id}",
                row_number=row_number,
                field=relationship_field,
                code=ERROR_CONTACT_RELATIONSHIP_MISMATCH,
                raw_value=relationship.name,
                options=[
                    ("use_existing", f"全文件采用已有关系：{existing_relationship.type_name}")
                ],
                message=f"模板关系「{relationship.name}」与已有关系「{existing_relationship.type_name}」不同，导入保持已有资料。",
            )
            if decision is None:
                return None
            relationship = DictionaryReference(
                DictionaryCategory.RELATIONSHIP,
                existing_relationship.type_name,
                existing_relationship.id,
                existing_relationship.system_code,
            )
            _info(
                state,
                row_number,
                relationship_field,
                "contact_relationship_preserved",
                f"采用已有关系「{relationship.name}」，不修改联系人资料。",
            )
        if location is not None and normalize_identity_component(
            location
        ) != normalize_identity_component(existing.location):
            decision = _confirmation(
                state,
                key=f"residence:{existing.id}",
                row_number=row_number,
                field="事主常住地" if name_field == "事主姓名" else "随礼人常住地",
                code="contact_residence_confirmation",
                raw_value=location,
                options=[
                    (
                        "keep_existing",
                        f"保留已有常住地：{existing.location or '空白'}；模板住址不保存",
                    )
                ],
                message=(
                    f"模板常住地「{location}」与已有「{existing.location or '空白'}」不同，"
                    "请确认不保存模板住址。"
                ),
            )
            if decision is None:
                return None
            _info(
                state,
                row_number,
                name_field,
                "contact_residence_preserved",
                "保留已有常住地，模板住址不写入；如需保留历史地址可先放入流水备注。",
                location,
            )
        draft.reused_contact_ids.add(existing.id)
        return ContactReference(
            name=existing.name,
            location=existing.location,
            remark=existing.remark,
            identity=contact_identity_components(existing.name, existing.location, existing.remark),
            relationship=relationship,
            existing_id=existing.id,
            is_self=False,
        )

    locations = {
        normalize_identity_component(value): value
        for _number, value in state.contact_inputs.get(identity, [])
        if value is not None
    }
    if len(locations) > 1:
        rows = "、".join(str(number) for number, _value in state.contact_inputs[identity])
        state.issues.append(
            _issue(
                row_number,
                name_field,
                "contact_file_residence_conflict",
                f"文件第 {rows} 行同一联系人的常住地不一致，请统一填写后上传。",
            )
        )
        return None
    if locations:
        location = next(iter(locations.values()))
    reference = draft.contacts.get(identity)
    if reference is None:
        reference = state.planned_contacts.get(identity)
    if reference is not None:
        if reference.relationship != relationship:
            state.issues.append(
                _issue(
                    row_number,
                    relationship_field,
                    ERROR_CONTACT_FILE_RELATIONSHIP_MISMATCH,
                    f"「{relationship_field}」与文件内同一联系人的关系类型不一致。",
                )
            )
            return None
        return reference

    reference = ContactReference(
        name=name,
        location=location,
        remark=remark,
        identity=identity,
        relationship=relationship,
        existing_id=None,
        is_self=False,
    )
    draft.contacts[identity] = reference
    return reference


# ---------------------------------------------------------------- 方向与事件


def _derive_direction(
    state: _ImportState,
    draft: _RowDraft,
    host: ContactReference,
    giver: ContactReference,
    row_number: int,
) -> LedgerDirection | None:
    try:
        direction = derive_direction(
            self_contact_id=state.self_contact.id,  # type: ignore[union-attr]
            host_contact_id=_direction_id(state, draft, host),
            giver_contact_id=_direction_id(state, draft, giver),
        )
        if host.is_self == giver.is_self:
            state.issues.append(
                _issue(
                    row_number,
                    "事主关系类型",
                    "self_relationship_required",
                    "事主和随礼人中有且只有一方是本人，该方关系类型须填写“我”。",
                )
            )
            return None
        return direction
    except LedgerValidationError as exc:
        field = _DIRECTION_ERROR_FIELDS.get(exc.code, "事主姓名")
        state.issues.append(_issue(row_number, field, exc.code, exc.message))
        return None


def _resolve_event(
    state: _ImportState,
    draft: _RowDraft,
    host: ContactReference,
    kinship: DictionaryReference,
    event_type: DictionaryReference,
    event_date: date | None,
    event_note: str | None,
    row_number: int,
) -> _EventResolution | None:
    if (
        host.existing_id is not None
        and kinship.existing_id is not None
        and event_type.existing_id is not None
    ):
        existing = state.events_by_identity.get(
            event_identity_components(
                host.existing_id, kinship.existing_id, event_type.existing_id, event_note
            )
        )
        if existing is not None:
            draft.reused_event_ids.add(existing.id)
            return _EventResolution(
                existing_event=EventReference(
                    host,
                    kinship,
                    event_type,
                    existing.event_date,
                    existing.remark,
                    existing.id,
                    date_value(existing).precision,
                    date_value(existing).year,
                    date_value(existing).month,
                ),
                event_holder=None,
            )

    file_key = (
        host.identity,
        _dictionary_name_key(kinship.name),
        _dictionary_name_key(event_type.name),
        (event_note or "").strip(),
    )
    holder = draft.events.get(file_key)
    if holder is None:
        holder = state.planned_events.get(file_key)
    if holder is not None:
        return _EventResolution(existing_event=None, event_holder=holder)

    holder = _EventHolder(host, kinship, event_type, event_date, remark=event_note)
    draft.events[file_key] = holder
    return _EventResolution(existing_event=None, event_holder=holder)


def _check_gift_duplicate(
    state: _ImportState,
    draft: _RowDraft,
    *,
    host: ContactReference,
    kinship: DictionaryReference,
    event_type: DictionaryReference,
    event_remark: str | None,
    event_existing_id: int | None,
    giver: ContactReference,
    row_number: int,
) -> bool:
    event_key = (
        host.identity,
        _dictionary_name_key(kinship.name),
        _dictionary_name_key(event_type.name),
        (event_remark or "").strip(),
    )
    gift_file_key = (event_key, giver.identity)
    if gift_file_key in state.planned_gift_file_keys:
        first_number = state.gift_file_rows[gift_file_key]
        if not any(
            issue.row_number == first_number and issue.code == ERROR_GIFT_DUPLICATE_IN_FILE
            for issue in state.issues
        ):
            state.issues.append(
                _issue(
                    first_number,
                    "随礼人姓名",
                    ERROR_GIFT_DUPLICATE_IN_FILE,
                    f"该随礼人在同一事件中与第 {row_number} 行重复，请修正，系统不会合并金额。",
                )
            )
        state.issues.append(
            _issue(
                row_number,
                "随礼人姓名",
                ERROR_GIFT_DUPLICATE_IN_FILE,
                f"该随礼人在这个事件中与第 {first_number} 行重复，系统不会合并金额。",
            )
        )
        return False
    if event_existing_id is not None and giver.existing_id is not None:
        if (event_existing_id, giver.existing_id) in state.gifts_by_event_giver:
            gift = state.gifts_by_event_giver[(event_existing_id, giver.existing_id)]
            original_event = next(
                event
                for event in state.events_by_identity.values()
                if event.id == event_existing_id
            )
            state.issues.append(
                LegacyImportIssue(
                    row_number,
                    "随礼人姓名",
                    ERROR_GIFT_DUPLICATE,
                    (
                        f"该随礼人在这个事件中已有流水，礼金 {gift.amount} 元，"
                        f"实物 {gift.gift_details or '无'}，备注 {gift.remark or '无'}；"
                        f"办事日期 {display_event_date(original_event)}，"
                        f"登记 {gift.created_at or '无登记时间'}；"
                        "请查看或修改原记录。"
                    ),
                    existing_event_id=event_existing_id,
                    existing_gift_id=gift.id,
                )
            )
            return False
    draft.gift_file_keys.add(gift_file_key)
    state.gift_file_rows[gift_file_key] = row_number
    return True


# ---------------------------------------------------------------- 单行分析


def _analyze_row(state: _ImportState, row: OldLedgerRow) -> _RowResult | None:
    draft = _RowDraft()
    row_number = row.row_number
    for value, label in (
        (row.event_contact_note, "事主联系人备注"),
        (row.gift_contact_note, "随礼人联系人备注"),
        (row.event_note, "事件备注"),
        (row.gift_note, "流水备注"),
    ):
        if value is not None and len(value) > 255:
            state.issues.append(
                _issue(row_number, label, "text_too_long", f"「{label}」最多255个字符。")
            )
            return None

    if row.event_date is None and row.date_precision == "day":
        state.issues.append(
            _issue(
                row_number,
                "办事日期",
                ERROR_EVENT_DATE_REQUIRED,
                "「办事日期」是必填项，请按 yyyy-mm-dd 填写办事日期。",
            )
        )
    if row.amount is None:
        state.issues.append(
            _issue(
                row_number,
                "礼金金额",
                ERROR_AMOUNT_REQUIRED,
                "「礼金金额」是必填项，纯实物礼金请填 0。",
            )
        )

    host_name = _contact_name(state, row.event_contact_name, _HOST_FIELDS[0], row_number)
    host_location = _contact_location(
        state, row.event_contact_residence, _HOST_FIELDS[1], row_number
    )
    host_relationship_name = _dictionary_name(
        state, row.event_contact_relation_type, _HOST_FIELDS[3], row_number
    )
    kinship_name = _dictionary_name(state, row.kinship_name, "亲属称谓", row_number)
    event_type_name = _dictionary_name(state, row.event_type_name, "事件类型", row_number)
    giver_name = _contact_name(state, row.gift_contact_name, _GIVER_FIELDS[0], row_number)
    giver_location = _contact_location(
        state, row.gift_contact_residence, _GIVER_FIELDS[1], row_number
    )
    giver_relationship_name = _dictionary_name(
        state, row.gift_contact_relation_type, _GIVER_FIELDS[3], row_number
    )

    amount_result: tuple[Decimal, str | None] | None = None
    if row.amount is not None:
        amount_result = _validate_amount(state, row.amount, row.gift_detail, row_number)

    if (
        (row.event_date is None and row.date_precision == "day")
        or row.amount is None
        or host_name is None
        or host_relationship_name is None
        or kinship_name is None
        or event_type_name is None
        or giver_name is None
        or giver_relationship_name is None
        or amount_result is None
    ):
        return None
    amount, gift_details = amount_result

    if row.event_date is not None and row.event_date < date(1000, 1, 1):
        state.issues.append(
            _issue(
                row_number,
                "办事日期",
                ERROR_EVENT_DATE_INVALID,
                "「办事日期」不能早于 1000-01-01。",
            )
        )
        return None

    host_relationship = _resolve_dictionary(
        state,
        draft,
        DictionaryCategory.RELATIONSHIP,
        host_relationship_name,
        _HOST_FIELDS[3],
        row_number,
    )
    giver_relationship = _resolve_dictionary(
        state,
        draft,
        DictionaryCategory.RELATIONSHIP,
        giver_relationship_name,
        _GIVER_FIELDS[3],
        row_number,
    )
    kinship = _resolve_dictionary(
        state, draft, DictionaryCategory.KINSHIP, kinship_name, "亲属称谓", row_number
    )
    event_type = _resolve_dictionary(
        state, draft, DictionaryCategory.EVENT, event_type_name, "事件类型", row_number
    )
    if (
        host_relationship is None
        or giver_relationship is None
        or kinship is None
        or event_type is None
    ):
        return None

    host = _resolve_contact(
        state,
        draft,
        name=host_name,
        location=host_location,
        remark=row.event_contact_note,
        relationship=host_relationship,
        fields=_HOST_FIELDS,
        row_number=row_number,
    )
    giver = _resolve_contact(
        state,
        draft,
        name=giver_name,
        location=giver_location,
        remark=row.gift_contact_note,
        relationship=giver_relationship,
        fields=_GIVER_FIELDS,
        row_number=row_number,
    )
    if host is None or giver is None:
        return None

    direction = _derive_direction(state, draft, host, giver, row_number)
    if direction is None:
        return None

    event_resolution = _resolve_event(
        state, draft, host, kinship, event_type, row.event_date, row.event_note, row_number
    )
    if event_resolution is None:
        return None

    event_existing_id = (
        event_resolution.existing_event.existing_id
        if event_resolution.existing_event is not None
        else None
    )
    if not _check_gift_duplicate(
        state,
        draft,
        host=host,
        kinship=kinship,
        event_type=event_type,
        event_remark=row.event_note,
        event_existing_id=event_existing_id,
        giver=giver,
        row_number=row_number,
    ):
        return None

    _commit_row(state, draft)

    return _RowResult(
        row_number=row_number,
        host=host,
        giver=giver,
        kinship=kinship,
        event_type=event_type,
        existing_event=event_resolution.existing_event,
        event_holder=event_resolution.event_holder,
        amount=amount,
        gift_details=gift_details,
        gift_remark=row.gift_note,
        direction=direction,
        incoming_date=date_value(row),
    )


# ---------------------------------------------------------------- 对外入口


def analyze_old_ledger_import(
    session: Session,
    user_id: int,
    parsed: ParsedOldLedgerWorkbook,
    decisions: dict[str, str] | None = None,
) -> LegacyImportAnalysis:
    """对解析后的旧账工作簿做只读业务预检，返回可供同一事务写入的分析结果。"""

    state = _ImportState(session, user_id, decisions or {})
    state.raw_by_row = {row.row_number: row.raw_values for row in parsed.rows}
    for error in parsed.errors:
        state.issues.append(
            LegacyImportIssue(error.row_number, error.field, error.code, error.message)
        )

    total_rows = len(parsed.rows)
    if total_rows == 0:
        state.issues.append(
            _issue(
                None,
                None,
                ERROR_EMPTY_WORKBOOK,
                "工作簿没有可导入的数据行，请先填写旧账模板。",
            )
        )
        return _finalize(state, total_rows, ())

    state.self_contact = _load_self_contact(session, user_id)
    if state.self_contact is None or not state.self_contact.is_active:
        state.issues.append(
            _issue(None, None, ERROR_SELF_CONTACT_REQUIRED, "请先完成本人信息建档后再导入旧账。")
        )
        return _finalize(state, total_rows, ())

    _load_lookup_data(state)
    for row in parsed.valid_rows:
        for name, location, note in (
            (row.event_contact_name, row.event_contact_residence, row.event_contact_note),
            (row.gift_contact_name, row.gift_contact_residence, row.gift_contact_note),
        ):
            if name:
                identity = contact_identity_components(name, location, note)
                state.contact_inputs.setdefault(identity, []).append((row.row_number, location))
                state.file_identities_by_name.setdefault(identity[0], set()).add(identity)

    results: list[_RowResult] = []
    for row in parsed.valid_rows:
        result = _analyze_row(state, row)
        if result is not None:
            results.append(result)

    return _finalize(state, total_rows, results)


def _canonical_event(holder: _EventHolder) -> EventReference:
    return EventReference(
        host=holder.host,
        kinship=holder.kinship,
        event_type=holder.event_type,
        event_date=holder.event_date,
        remark=holder.remark,
        existing_id=None,
        date_precision="day" if holder.event_date else "unknown",
        event_year=holder.event_date.year if holder.event_date else None,
        event_month=holder.event_date.month if holder.event_date else None,
    )


def _finalize(
    state: _ImportState,
    total_rows: int,
    results: list[_RowResult] | tuple[_RowResult, ...],
) -> LegacyImportAnalysis:
    grouped: dict[tuple[Any, ...], list[_RowResult]] = {}
    for result in results:
        reference = result.existing_event or _canonical_event(result.event_holder)
        key = (
            reference.host.identity,
            reference.kinship.name,
            reference.event_type.name,
            (reference.remark or "").strip(),
        )
        grouped.setdefault(key, []).append(result)
    canonical_events = {}
    date_completions = []
    date_completion_context = []
    for key, group in grouped.items():
        first = group[0]
        reference = first.existing_event or _canonical_event(first.event_holder)
        merged = date_value(reference) if reference.existing_id else first.incoming_date
        try:
            for result in group:
                merged = merge_event_dates(merged, result.incoming_date)
        except ValueError as error:
            numbers = "、".join(str(result.row_number) for result in group)
            for result in group:
                state.issues.append(
                    LegacyImportIssue(
                        result.row_number,
                        "办事日期",
                        "event_date_conflict",
                        (
                            f"同一事件的日期存在矛盾（第 {numbers} 行）：{error}。"
                            "请核对日期，或用事件备注区分不同事件。"
                        ),
                        existing_event_id=reference.existing_id,
                    )
                )
            continue
        updated = replace(
            reference,
            event_date=merged.event_date,
            date_precision=merged.precision,
            event_year=merged.year,
            event_month=merged.month,
        )
        if reference.existing_id and merged != date_value(reference):
            related = sum(
                1
                for event_id, _giver in state.gifts_by_event_giver
                if event_id == reference.existing_id
            )
            message = (
                f"原事件日期「{display_event_date(reference)}」"
                f"拟补齐为「{display_event_date(merged)}」；保留同一事件编号，"
                f"影响关联 {related} 笔现有流水的日期展示和统计。"
            )
            choice = _confirmation(
                state,
                key=f"event_date:{reference.existing_id}",
                row_number=first.row_number,
                field="办事日期",
                code="event_date_completion_confirmation",
                raw_value=display_event_date(merged),
                options=[("complete", "补齐原事件日期")],
                message=message,
            )
            if choice == "complete":
                _info(state, first.row_number, "办事日期", "event_date_completion_plan", message)
            date_completions.append(updated)
            current_date = date_value(reference)
            current_record = next(
                event
                for event in state.events_by_identity.values()
                if event.id == reference.existing_id
            )
            date_completion_context.append(
                (
                    reference.existing_id,
                    current_date.precision,
                    current_date.event_date.isoformat() if current_date.event_date else None,
                    current_date.year,
                    current_date.month,
                    current_record.date_source or "web",
                    related,
                )
            )
        for result in group:
            if result.incoming_date != merged:
                _info(
                    state,
                    result.row_number,
                    "办事日期",
                    "event_date_merged",
                    f"同一事件统一采用更完整日期「{display_event_date(merged)}」，不修改已知日期部分。",
                    display_event_date(result.incoming_date),
                )
        canonical_events[key] = updated
    plans: list[LegacyImportRowPlan] = []
    for result in results:
        if result.existing_event is not None:
            event = result.existing_event
        else:
            assert result.event_holder is not None
            event = _canonical_event(result.event_holder)
        key = (
            event.host.identity,
            event.kinship.name,
            event.event_type.name,
            (event.remark or "").strip(),
        )
        if key not in canonical_events:
            continue
        event = canonical_events[key]
        if any(
            issue.row_number == result.row_number and issue.severity == "error"
            for issue in state.issues
        ):
            continue
        plans.append(
            LegacyImportRowPlan(
                row_number=result.row_number,
                host=result.host,
                giver=result.giver,
                kinship=result.kinship,
                event_type=result.event_type,
                event=event,
                amount=result.amount,
                gift_details=result.gift_details,
                gift_remark=result.gift_remark,
                direction=result.direction,
            )
        )

    valid_rows = len(plans)
    all_issues = tuple(
        sorted(
            state.issues, key=lambda issue: (issue.row_number is not None, issue.row_number or 0)
        )
    )
    all_issues = tuple(
        replace(issue, raw_value=_issue_raw_value(state, issue))
        if issue.raw_value is None
        else issue
        for issue in all_issues
    )
    used_dictionary_keys = {
        (ref.category, _dictionary_name_key(ref.name))
        for plan in plans
        for ref in (plan.host.relationship, plan.giver.relationship, plan.kinship, plan.event_type)
        if ref.existing_id is None
    }
    new_dictionaries = tuple(
        reference
        for key, reference in state.planned_dictionaries.items()
        if key in used_dictionary_keys
    )
    dictionary_confirmation_keys = {
        _stable_key("dictionary", category.value, name) for category, name in used_dictionary_keys
    }
    all_issues = tuple(
        issue
        for issue in all_issues
        if issue.code != "dictionary_confirmation"
        or issue.confirmation_key in dictionary_confirmation_keys
    )
    errors = tuple(issue for issue in all_issues if issue.severity == "error")
    used_contacts = {
        reference.identity: reference
        for plan in plans
        for reference in (plan.host, plan.giver)
        if reference.existing_id is None
    }
    new_events = tuple(
        reference
        for reference in canonical_events.values()
        if reference.existing_id is None and any(plan.event == reference for plan in plans)
    )
    return LegacyImportAnalysis(
        total_rows=total_rows,
        valid_rows=valid_rows,
        error_rows=(
            total_rows
            if any(issue.row_number is None for issue in errors)
            else len({issue.row_number for issue in errors if issue.row_number is not None})
        ),
        errors=errors,
        rows=tuple(plans),
        new_contacts=tuple(used_contacts.values()),
        new_dictionary_items=new_dictionaries,
        new_events=new_events,
        reused_contact_ids=frozenset(
            ref.existing_id
            for plan in plans
            for ref in (plan.host, plan.giver)
            if ref.existing_id is not None and not ref.is_self
        ),
        reused_event_ids=frozenset(
            plan.event.existing_id for plan in plans if plan.event.existing_id is not None
        ),
        new_gift_count=valid_rows,
        date_completions=tuple(
            ref for ref in date_completions if any(plan.event == ref for plan in plans)
        ),
        issues=all_issues,
        date_completion_context=tuple(
            context
            for context in date_completion_context
            if any(plan.event.existing_id == context[0] for plan in plans)
        ),
    )


def legacy_import_preview_response(
    analysis: LegacyImportAnalysis,
    *,
    preview_token: str | None = None,
    expires_at: datetime | None = None,
) -> LegacyImportPreviewResponse:
    """把分析结果转换为预览响应。``preview_token`` 与 ``expires_at`` 由主对话后续传入。"""

    issues = analysis.issues or analysis.errors
    income = [plan for plan in analysis.rows if plan.direction == LedgerDirection.INCOME]
    expense = [plan for plan in analysis.rows if plan.direction == LedgerDirection.EXPENSE]
    unique_events = {(_event_plan_key(plan.event)): plan.event for plan in analysis.rows}
    date_counts = {
        precision: sum(event.date_precision == precision for event in unique_events.values())
        for precision in ("day", "month", "year", "unknown")
    }

    return LegacyImportPreviewResponse(
        preview_token=preview_token,
        expires_at=expires_at,
        total_rows=analysis.total_rows,
        valid_rows=analysis.valid_rows,
        error_rows=analysis.error_rows,
        errors=[
            LegacyImportIssueResponse(
                row_number=issue.row_number,
                field=issue.field,
                code=issue.code,
                message=issue.message,
            )
            for issue in analysis.errors
        ],
        errors_truncated=False,
        new_contacts=[
            LegacyImportContactSummary(
                name=contact.name,
                location=contact.location,
                remark=contact.remark,
                relationship_type=contact.relationship.name,
            )
            for contact in analysis.new_contacts
        ],
        reused_contact_count=len(analysis.reused_contact_ids),
        new_dictionary_items=[
            LegacyImportDictionarySummary(category=item.category, name=item.name)
            for item in analysis.new_dictionary_items
        ],
        new_event_count=len(analysis.new_events),
        reused_event_count=len(analysis.reused_event_ids),
        new_gift_count=analysis.new_gift_count,
        can_import=not analysis.has_errors and analysis.valid_rows > 0,
        issues=[
            LegacyImportIssueResponse(
                row_number=issue.row_number,
                field=issue.field,
                code=issue.code,
                message=issue.message,
                severity=issue.severity,
                raw_value=issue.raw_value,
                remedy=issue.remedy,
                confirmation_key=issue.confirmation_key,
                options=[{"value": value, "label": label} for value, label in issue.options],
                existing_event_id=issue.existing_event_id,
                existing_gift_id=issue.existing_gift_id,
            )
            for issue in issues
        ],
        rows=[
            LegacyImportRowSummary(
                row_number=plan.row_number,
                event_name=(
                    f"{plan.host.name}"
                    f"{'' if plan.kinship.name == '本人' else plan.kinship.name}"
                    f" · {plan.event_type.name}"
                )
                + (f" · {plan.event.remark}" if plan.event.remark else ""),
                event_date=display_event_date(plan.event),
                date_precision=plan.event.date_precision,
                host_name=plan.host.name,
                host_relationship=plan.host.relationship.name,
                giver_name=plan.giver.name,
                giver_relationship=plan.giver.relationship.name,
                amount=f"{plan.amount:.2f}",
                gift_details=plan.gift_details,
                gift_remark=plan.gift_remark,
                direction=plan.direction.value,
                event_action=(
                    "complete_date"
                    if plan.event in analysis.date_completions
                    else "reuse"
                    if plan.event.existing_id
                    else "create"
                ),
                host_action="reuse" if plan.host.existing_id else "create",
                giver_action="reuse" if plan.giver.existing_id else "create",
            )
            for plan in analysis.rows
        ],
        summary=LegacyImportTotals(
            income_count=len(income),
            income_amount=f"{sum((p.amount for p in income), Decimal(0)):.2f}",
            expense_count=len(expense),
            expense_amount=f"{sum((p.amount for p in expense), Decimal(0)):.2f}",
            pure_goods_count=sum(plan.amount == 0 for plan in analysis.rows),
            date_precision_counts=date_counts,
        ),
        completed_date_event_count=len(analysis.date_completions),
    )


def _event_plan_key(reference):
    return (
        reference.host.identity,
        reference.kinship.name,
        reference.event_type.name,
        (reference.remark or "").strip(),
        reference.existing_id,
    )


def _issue_raw_value(state, issue):
    values = state.raw_by_row.get(issue.row_number, ())
    if issue.field not in COLUMN_HEADERS or not values:
        return None
    value = values[COLUMN_HEADERS.index(issue.field)]
    return None if value is None else str(value)


def legacy_import_plan_fingerprint(analysis):
    """Hash reviewed business plans, not token or volatile timestamps."""
    response = legacy_import_preview_response(analysis).model_dump(mode="json")
    for key in ("preview_token", "expires_at", "issues", "errors", "errors_truncated"):
        response.pop(key, None)
    response["contact_ids"] = sorted(analysis.reused_contact_ids)
    response["event_ids"] = sorted(analysis.reused_event_ids)
    response["dictionary_ids"] = [
        (r.category.value, r.name, r.existing_id)
        for plan in analysis.rows
        for r in (plan.host.relationship, plan.giver.relationship, plan.kinship, plan.event_type)
    ]
    response["contact_profiles"] = [
        (ref.existing_id, ref.identity, ref.location, ref.relationship.existing_id)
        for plan in analysis.rows
        for ref in (plan.host, plan.giver)
    ]
    response["date_completion_context"] = sorted(analysis.date_completion_context)
    return sha256(
        json.dumps(response, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "ERROR_AMOUNT_REQUIRED",
    "ERROR_CONTACT_DISTINGUISHING_INFO_REQUIRED",
    "ERROR_CONTACT_FILE_RELATIONSHIP_MISMATCH",
    "ERROR_CONTACT_INACTIVE",
    "ERROR_CONTACT_LOCATION_TOO_LONG",
    "ERROR_CONTACT_NAME_REQUIRED",
    "ERROR_CONTACT_NAME_TOO_LONG",
    "ERROR_CONTACT_RELATIONSHIP_MISMATCH",
    "ERROR_DICTIONARY_INACTIVE",
    "ERROR_DICTIONARY_NAME_REQUIRED",
    "ERROR_DICTIONARY_NAME_TOO_LONG",
    "ERROR_EMPTY_WORKBOOK",
    "ERROR_EVENT_DATE_INVALID",
    "ERROR_EVENT_DATE_REQUIRED",
    "ERROR_GIFT_DUPLICATE",
    "ERROR_GIFT_DUPLICATE_IN_FILE",
    "ERROR_SELF_CONTACT_REQUIRED",
    "ERROR_SELF_NAME_MISMATCH",
    "ERROR_SELF_PROFILE_MISMATCH",
    "ERROR_SYSTEM_DICTIONARY_UNAVAILABLE",
    "ContactReference",
    "DictionaryReference",
    "EventReference",
    "LegacyImportAnalysis",
    "LegacyImportIssue",
    "LegacyImportRowPlan",
    "analyze_old_ledger_import",
    "legacy_import_preview_response",
]
