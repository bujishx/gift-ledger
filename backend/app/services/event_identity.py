"""Stable event identity: host, kinship, event type and trimmed remark.

Dates remain business/statistics data and are deliberately not part of identity.
The S008 fingerprint format is frozen so historical migration replay stays stable.
"""

from hashlib import sha256
from struct import pack

IDENTITY_VERSION = b"gift-ledger/event-identity/v1"
IDENTITY_HASH_LENGTH = 32


def event_identity_components(
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    remark: str | None,
) -> tuple[int, int, int, str]:
    ids = (host_contact_id, kinship_type_id, event_type_id)
    if any(type(item) is not int or item <= 0 for item in ids):
        raise ValueError("事件身份编号必须为正整数")
    if remark is not None and not isinstance(remark, str):
        raise TypeError("事件备注必须为文字或空值")
    return (*ids, (remark or "").strip())


def event_identity_hash(
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    remark: str | None,
) -> bytes:
    digest = sha256(IDENTITY_VERSION)
    for component in event_identity_components(
        host_contact_id, kinship_type_id, event_type_id, remark
    ):
        encoded = str(component).encode("utf-8")
        digest.update(pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.digest()


def event_identity_hash_default(context) -> bytes:
    """Supply the hash for ORM/Core inserts which omit the internal column."""
    values = context.get_current_parameters()
    return event_identity_hash(
        values["contact_id"],
        values["relative_type_id"],
        values["event_type_id"],
        values.get("remark"),
    )
