"""联系人身份规范化与稳定指纹。

该模块供联系人服务使用。D108 将身份改为“姓名和备注”。旧指纹必须由独立迁移
预检冲突。取得处理决定后统一重算。不能仅升级应用代码而跳过数据库迁移。
"""

from hashlib import sha256
from struct import pack

IDENTITY_VERSION_V1 = b"gift-ledger/contact-identity/v1"
IDENTITY_VERSION_V2 = b"gift-ledger/contact-identity/v2"
IDENTITY_VERSION = IDENTITY_VERSION_V2
IDENTITY_HASH_LENGTH = 32

_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def normalize_identity_component(value: str | None, *, required: bool = False) -> str:
    if value is None:
        if required:
            raise ValueError("联系人姓名不能为空")
        return ""
    normalized = value.strip()
    if required and not normalized:
        raise ValueError("联系人姓名不能为空")
    return normalized.translate(_ASCII_LOWER_TRANSLATION)


def contact_identity_components(
    name: str,
    location: str | None,
    remark: str | None,
) -> tuple[str, str]:
    return contact_identity_components_v2(name, location, remark)


def contact_identity_components_v2(
    name: str,
    location: str | None,
    remark: str | None,
) -> tuple[str, str]:
    """Freeze the S007 two-field identity rule for historical migration replay."""
    return (
        normalize_identity_component(name, required=True),
        normalize_identity_component(remark),
    )


def contact_identity_hash(
    name: str,
    location: str | None,
    remark: str | None,
) -> bytes:
    return contact_identity_hash_v2(name, location, remark)


def contact_identity_hash_v2(
    name: str,
    location: str | None,
    remark: str | None,
) -> bytes:
    """Freeze the S007 fingerprint format for historical migration replay."""
    digest = sha256()
    digest.update(IDENTITY_VERSION_V2)
    for component in contact_identity_components_v2(name, location, remark):
        encoded = component.encode("utf-8")
        digest.update(pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.digest()


def contact_identity_components_v1(
    name: str,
    location: str | None,
    remark: str | None,
) -> tuple[str, str, str]:
    """供历史 S005 迁移重放的旧三项规则。不能用于新写入。"""
    return (
        normalize_identity_component(name, required=True),
        normalize_identity_component(location),
        normalize_identity_component(remark),
    )


def contact_identity_hash_v1(
    name: str,
    location: str | None,
    remark: str | None,
) -> bytes:
    """供历史 S005 迁移重放的旧指纹。不能用于新写入。"""
    digest = sha256()
    digest.update(IDENTITY_VERSION_V1)
    for component in contact_identity_components_v1(name, location, remark):
        encoded = component.encode("utf-8")
        digest.update(pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.digest()


def same_contact_identity(
    *,
    left_name: str,
    left_location: str | None,
    left_remark: str | None,
    right_name: str,
    right_location: str | None,
    right_remark: str | None,
) -> bool:
    return contact_identity_components(left_name, left_location, left_remark) == (
        contact_identity_components(right_name, right_location, right_remark)
    )
