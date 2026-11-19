"""Public, site-bound restore messages. No client-supplied target paths or engine commands."""

import os
import re
import stat
from uuid import UUID

from app.services import maintenance_enrollment_protocol as wire

CHUNK = 4 * 1024**2
META_LIMIT = 2 * 1024**2
META = {"BUNDLE.json", "backup.json", "verify.json", "SHA256SUMS"}
CIPHER = re.compile(r"backup-[0-9a-f]{32}\.glgui\.age")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
ACTIVE = {"queued", "awaiting_packet", "preflighting", "eligible", "applying"}
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "needs_review"}
BLOCKED = ACTIVE | {"needs_review"}


def read(path, limit=wire.MAX_PACKET):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        wire.check(stat.S_ISREG(st.st_mode) and 0 < st.st_size <= limit)
        raw = f.read(limit + 1)
        wire.check(len(raw) == st.st_size)
        return raw


def files(value):
    wire.check(isinstance(value, list) and len(value) in (1, 5))
    for item in value:
        wire.check(
            isinstance(item, dict)
            and item.keys() == {"name", "size"}
            and isinstance(item["name"], str)
            and type(item["size"]) is int
            and 0 < item["size"] <= 2**53 - 1
        )
        if item["name"] in META:
            wire.check(item["size"] <= META_LIMIT)
    names = {item["name"] for item in value}
    if len(value) == 1:
        wire.check(names == {"input.zip"})
    else:
        wire.check(len(names) == 5 and META < names and CIPHER.fullmatch(next(iter(names - META))))
    return value


def preview(data, binding):
    project, instance, origin = binding
    wire.check(
        isinstance(data, dict)
        and data.keys()
        == {
            "backup_id",
            "manifest_sha256",
            "source_project",
            "source_instance_id",
            "gift_count",
            "target_url",
            "target_project",
            "target_instance_id",
            "engine_operation_id",
        }
        and wire.matches(wire.OP, data["backup_id"])
        and wire.matches(wire.SHA, data["manifest_sha256"])
        and wire.matches(wire.OP, data["engine_operation_id"])
        and wire.matches(PROJECT, data["source_project"])
        and isinstance(data["source_instance_id"], str)
        and str(UUID(data["source_instance_id"])) == data["source_instance_id"] != instance
        and type(data["gift_count"]) is int
        and data["gift_count"] >= 0
        and (data["target_project"], data["target_instance_id"]) == (project, instance)
        and data["target_url"] in {origin, origin + "/"}
    )


def public(raw, binding):
    data = wire.unpack(raw)
    op, state = data["operation_id"], data["state"]
    wire.check(
        data.keys()
        <= {
            "operation_id",
            "state",
            "project",
            "instance_id",
            "code",
            "target",
            "preview",
            "result",
        }
        and (data["project"], data["instance_id"]) == binding[:2]
        and (
            (state == "idle" and op is None)
            or (state in ACTIVE | TERMINAL and wire.matches(wire.OP, op))
        )
    )
    if "code" in data:
        wire.check(wire.matches(re.compile(r"[a-zA-Z0-9_-]{1,100}"), data["code"]))
    if "target" in data or state == "awaiting_packet":
        wire.check_target(data["target"], *binding[:2])
    if "preview" in data or state in {"eligible", "applying", "succeeded"}:
        preview(data["preview"], binding)
    if "result" in data or state == "succeeded":
        r = data["result"]
        wire.check(
            state == "succeeded"
            and isinstance(r, dict)
            and r.keys()
            == {
                "gift_count",
                "cash_amount_all_gift_records",
                "temporary_key_removed",
                "full_database_and_app_files_verified",
            }
            and r["temporary_key_removed"] is True
            and r["full_database_and_app_files_verified"] is True
            and type(r["gift_count"]) is int
            and r["gift_count"] == data["preview"]["gift_count"]
            and wire.matches(re.compile(r"-?[0-9]+\.[0-9]{2}"), r["cash_amount_all_gift_records"])
        )
    return data


def busy(root):
    """Called inside the shared submission lock by competing backup and key operations."""
    try:
        if read(root / "requests/restore.upload") or read(root / "requests/restore.request", 33):
            return True
        raw = read(root / "status/restore.json")
        if raw is None:
            return False
        state = wire.unpack(raw)["state"]
        return state not in {"idle", "succeeded", "failed", "cancelled", "expired"}
    except (KeyError, OSError, TypeError, ValueError):
        # Keep other maintenance operations out while this site's restore state is unknown.
        return True
