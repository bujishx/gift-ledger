"""S006 升级前的自定义字典冲突扫描与安全改名。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.services.dictionaries import DictionaryValidationError, normalize_dictionary_name

# 历史 S006 升级工具只能使用当时已经发布的名称。当前目录已进入 D102/S007,
# 若读取当前名称会漏报旧库中的「配偶」等冲突。
S006_SYSTEM_NAMES_BY_CATEGORY = {
    "relationship": (
        "本人",
        "亲戚",
        "朋友",
        "同学",
        "同事",
        "邻居",
        "战友",
        "生意伙伴",
        "其他",
    ),
    "kinship": (
        "本人",
        "配偶",
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
        "祖父（爷爷）",
        "祖母（奶奶）",
        "外祖父（外公）",
        "外祖母（外婆）",
    ),
    "event": (
        "婚礼",
        "新生儿（满月／百日）",
        "探病慰问",
        "白事（丧葬）",
        "乔迁",
        "升学",
        "开业",
        "拜年",
        "生日／寿宴",
    ),
}


class DictionaryUpgradeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CategoryDefinition:
    table: str
    reference_table: str
    reference_column: str


CATEGORY_DEFINITIONS = {
    "relationship": CategoryDefinition("relationship_types", "contacts", "relation_type_id"),
    "kinship": CategoryDefinition("kinship_types", "event_records", "relative_type_id"),
    "event": CategoryDefinition("event_types", "event_records", "event_type_id"),
}


@dataclass(frozen=True)
class DictionaryUpgradeConflict:
    category: str
    user_id: int
    username: str
    item_id: int
    name: str
    is_active: bool
    reference_count: int
    suggested_name: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _definition(category: str) -> CategoryDefinition:
    try:
        return CATEGORY_DEFINITIONS[category]
    except KeyError as exc:
        raise DictionaryUpgradeError("CATEGORY_INVALID", "字典类别无效") from exc


def _suggested_name(name: str) -> str:
    suffix = "（原自定义）"
    return f"{name[: 50 - len(suffix)]}{suffix}"


def scan_dictionary_upgrade_conflicts(session: Session) -> list[DictionaryUpgradeConflict]:
    conflicts: list[DictionaryUpgradeConflict] = []
    for category, definition in CATEGORY_DEFINITIONS.items():
        statement = text(
            f"SELECT d.id, d.user_id, u.username, d.type_name, d.is_active, "
            f"(SELECT COUNT(*) FROM {definition.reference_table} AS r "
            f" WHERE r.user_id = d.user_id AND r.{definition.reference_column} = d.id) "
            f"AS reference_count FROM {definition.table} AS d "
            "INNER JOIN users AS u ON u.id = d.user_id "
            "WHERE d.user_id IS NOT NULL AND d.name_key IN :names "
            "ORDER BY d.user_id, d.id"
        ).bindparams(bindparam("names", expanding=True))
        rows = session.execute(
            statement, {"names": S006_SYSTEM_NAMES_BY_CATEGORY[category]}
        ).mappings()
        conflicts.extend(
            DictionaryUpgradeConflict(
                category=category,
                user_id=int(row["user_id"]),
                username=str(row["username"]),
                item_id=int(row["id"]),
                name=str(row["type_name"]),
                is_active=bool(row["is_active"]),
                reference_count=int(row["reference_count"]),
                suggested_name=_suggested_name(str(row["type_name"])),
            )
            for row in rows
        )
    return conflicts


def rename_dictionary_upgrade_conflict(
    session: Session,
    *,
    category: str,
    user_id: int,
    item_id: int,
    expected_name: str,
    new_name: str,
) -> str:
    definition = _definition(category)
    try:
        normalized = normalize_dictionary_name(new_name)
    except DictionaryValidationError as exc:
        raise DictionaryUpgradeError(exc.code, exc.message) from exc
    if normalized.casefold() in {
        name.casefold() for name in S006_SYSTEM_NAMES_BY_CATEGORY[category]
    }:
        raise DictionaryUpgradeError(
            "NAME_RESERVED_BY_S006",
            "新名称仍与 S006 系统内置项重名",
        )

    try:
        session.execute(
            text("SELECT id FROM users WHERE id = :user_id FOR UPDATE"), {"user_id": user_id}
        )
        row = (
            session.execute(
                text(
                    f"SELECT id, user_id, type_name FROM {definition.table} "
                    "WHERE id = :item_id AND user_id = :user_id FOR UPDATE"
                ),
                {"item_id": item_id, "user_id": user_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DictionaryUpgradeError("ITEM_NOT_FOUND", "找不到该用户的自定义字典项")
        if str(row["type_name"]) != expected_name:
            raise DictionaryUpgradeError(
                "ITEM_CHANGED",
                "自定义项名称已变化，请重新扫描冲突后再操作",
            )

        conflict_id = session.execute(
            text(
                f"SELECT id FROM {definition.table} WHERE id != :item_id "
                "AND (user_id IS NULL OR user_id = :user_id) AND name_key = :new_name LIMIT 1"
            ),
            {"item_id": item_id, "user_id": user_id, "new_name": normalized},
        ).scalar_one_or_none()
        if conflict_id is not None:
            raise DictionaryUpgradeError("NAME_CONFLICT", "新名称与当前可见字典项重名")

        session.execute(
            text(
                f"UPDATE {definition.table} SET type_name = :new_name "
                "WHERE id = :item_id AND user_id = :user_id"
            ),
            {"new_name": normalized, "item_id": item_id, "user_id": user_id},
        )
        session.commit()
        return normalized
    except Exception:
        session.rollback()
        raise


__all__ = [
    "CATEGORY_DEFINITIONS",
    "DictionaryUpgradeConflict",
    "DictionaryUpgradeError",
    "rename_dictionary_upgrade_conflict",
    "scan_dictionary_upgrade_conflicts",
]
