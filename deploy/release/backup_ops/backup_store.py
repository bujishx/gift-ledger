"""Validated ciphertext bundles and explicit local retention; Python 3.8 stdlib.

This module never decrypts, accesses private volumes, contacts cloud storage, or
decides whether a service may start.  Give it a dedicated store directory. Only
``artifacts`` contains published bundles; interrupted ``pending`` data is never
automatically removed. Keep pending outside the Cloud Sync source selection.
"""

import copy
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

FORMAT = "gift-ledger-backup-bundle-v1"
PLAN_FORMAT = "gift-ledger-backup-retention-plan-v1"
RECEIPT_FORMAT = "gift-ledger-gui-v2-operation"
UTC = timezone.utc
ID = re.compile(r"^[0-9a-f]{32}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
MAX_JSON = 2 * 1024 * 1024
ROOT_MEMBERS = {"artifacts", "pending", ".backup-store.lock"}
PRIVATE_MARKERS = (b"AGE-SECRET-KEY-", b"-----BEGIN PRIVATE KEY-----",
                   b"-----BEGIN ENCRYPTED PRIVATE KEY-----", b"-----BEGIN RSA PRIVATE KEY-----",
                   b"-----BEGIN EC PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----")
RECEIPT_KEYS = {
    "format",
    "version",
    "project",
    "action",
    "operation_id",
    "instance_id",
    "server_uuid",
    "request_sha256",
    "assets_sha256",
    "layout_sha256",
    "started_at",
    "status",
    "success",
    "completed_at",
    "started_sha256",
    "details",
}
BACKUP_KEYS = {
    "fingerprint",
    "app_files",
    "ciphertext",
    "ciphertext_sha256",
    "archive_sha256",
    "schema_revision",
    "backup_type",
    "source",
    "plaintext_package_validated",
    "encryption_roundtrip_verified",
    "reliable_backup_verified",
    "restore_verified",
    "applications_stopped_verified",
    "sql_freeze_verified",
    "readonly_before",
    "readonly_restored",
    "automatic_backup_configured",
    "offsite_configured",
}
VERIFY_KEYS = {
    "backup_operation_id",
    "ciphertext",
    "ciphertext_sha256",
    "archive_sha256",
    "fingerprint",
    "app_files",
    "source",
    "package_verified",
    "encryption_roundtrip_verified",
    "reliable_backup_verified",
    "restore_verified",
    "readonly_before",
    "readonly_restored",
    "applications_stopped_verified",
    "automatic_backup_configured",
    "offsite_configured",
}
MANIFEST_KEYS = {
    "format",
    "project",
    "instance_id",
    "server_uuid",
    "backup_operation_id",
    "verify_operation_id",
    "completed_at",
    "protected",
    "files",
}


class BackupStoreError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise BackupStoreError(code)


def canonical(value):
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise BackupStoreError("invalid_json_value") from None


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _metadata(path, directory=False):
    try:
        info = Path(path).lstat()
    except OSError:
        raise BackupStoreError("path_unavailable") from None
    require(
        not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & 0x400),
        "link_forbidden",
    )
    if directory:
        require(stat.S_ISDIR(info.st_mode), "directory_required")
    else:
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            "regular_single_link_file_required",
        )
    return info


def _path(path):
    result = Path(os.path.abspath(os.fspath(path)))
    # Check before resolving: resolve() would hide symlinks and junctions.
    for parent in reversed((result,) + tuple(result.parents)):
        if parent.exists() or parent.is_symlink():
            _metadata(parent, directory=True)
    return result


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _read_file(path, collect=False, destination=None, ciphertext=False):
    path = Path(path)
    _path(path.parent)
    before = _metadata(path)
    if collect:
        require(before.st_size <= MAX_JSON, "json_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise BackupStoreError("file_open_failed") from None
    data = bytearray()
    hasher = hashlib.sha256()
    first, tail = True, b""
    with os.fdopen(descriptor, "rb") as source:
        require(
            _signature(os.fstat(source.fileno())) == _signature(before),
            "file_changed_during_read",
        )
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            if ciphertext:
                if first:
                    require(block.startswith(b"age-encryption.org/v1\n") and before.st_size > 64,
                            "ciphertext_format_invalid")
                    first = False
                combined = tail + block
                require(not any(marker in combined for marker in PRIVATE_MARKERS),
                        "private_material_forbidden")
                tail = combined[-max(map(len, PRIVATE_MARKERS)):]
            hasher.update(block)
            if collect:
                data.extend(block)
                require(len(data) <= MAX_JSON, "json_too_large")
            if destination is not None:
                destination.write(block)
        require(
            _signature(os.fstat(source.fileno())) == _signature(before)
            and _signature(_metadata(path)) == _signature(before),
            "file_changed_during_read",
        )
    require(not ciphertext or not first, "ciphertext_format_invalid")
    return bytes(data) if collect else hasher.hexdigest()


def _json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(
            _read_file(path, collect=True).decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: require(False, "invalid_json_value"),
        )
    except (ValueError, UnicodeError):
        raise BackupStoreError("invalid_json") from None
    require(isinstance(value, dict), "json_object_required")
    return value


def _public(value):
    if isinstance(value, dict):
        for key, child in value.items():
            require(isinstance(key, str), "invalid_public_key")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            require(
                normalized
                not in {
                    "password",
                    "mysqlpassword",
                    "mysqlrootpassword",
                    "mysqlapppassword",
                    "jwtsecret",
                    "privatekey",
                    "privkey",
                    "identitytxt",
                    "secret",
                    "env",
                    "databaseurl",
                    "accesskey",
                    "refreshtoken",
                    "accesstoken",
                },
                "secret_content_forbidden",
            )
            _public(child)
    elif isinstance(value, list):
        for child in value:
            _public(child)
    elif isinstance(value, str):
        require(
            not any(
                marker in value.upper()
                for marker in (
                    "-----BEGIN",
                    "AGE-SECRET-KEY-",
                    "MYSQL_PWD=",
                    "DATABASE_URL=",
                )
            ),
            "secret_content_forbidden",
        )
    else:
        require(
            value is None or type(value) in (bool, int, float), "invalid_public_value"
        )


def _time(value):
    require(isinstance(value, str), "timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        raise BackupStoreError("timestamp_invalid") from None
    require(
        parsed.tzinfo is not None and parsed.utcoffset() is not None,
        "timestamp_timezone_required",
    )
    return parsed.astimezone(UTC)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _receipt(value, action):
    result = copy.deepcopy(value) if isinstance(value, dict) else _json(value)
    require(len(canonical(result)) <= MAX_JSON, "json_too_large")
    _public(result)
    require(set(result) == RECEIPT_KEYS, "receipt_fields_invalid")
    require(
        result["format"] == RECEIPT_FORMAT
        and type(result["version"]) is int
        and result["version"] == 1
        and result["success"] is True
        and result["status"] == "completed"
        and result["action"] == action,
        "receipt_not_completed",
    )
    require(
        isinstance(result["project"], str)
        and PROJECT.fullmatch(result["project"])
        and _uuid(result["instance_id"])
        and _uuid(result["server_uuid"]),
        "receipt_identity_invalid",
    )
    require(
        isinstance(result["operation_id"], str)
        and ID.fullmatch(result["operation_id"]),
        "operation_id_invalid",
    )
    for key in ("request_sha256", "assets_sha256", "layout_sha256", "started_sha256"):
        require(
            isinstance(result[key], str) and HASH.fullmatch(result[key]),
            "receipt_hash_invalid",
        )
    require(
        _time(result["started_at"]) <= _time(result["completed_at"]),
        "receipt_time_order_invalid",
    )
    details = result["details"]
    require(
        isinstance(details, dict)
        and set(details) == (BACKUP_KEYS if action == "backup" else VERIFY_KEYS),
        "receipt_details_invalid",
    )
    for key in ("ciphertext_sha256", "archive_sha256"):
        require(
            isinstance(details[key], str) and HASH.fullmatch(details[key]),
            "receipt_hash_invalid",
        )
    require(details["readonly_restored"] is True, "readonly_not_restored")
    source = details["source"]
    require(
        isinstance(source, dict)
        and set(source)
        == {
            "project",
            "instance_id",
            "server_uuid",
            "canonical_identity_sha256",
            "layout_sha256",
            "engine_identity_verified",
            "applications_stopped_verified",
        },
        "source_metadata_invalid",
    )
    for key in ("project", "instance_id", "server_uuid", "layout_sha256"):
        require(source[key] == result[key], "source_identity_mismatch")
    require(
        isinstance(source["canonical_identity_sha256"], str)
        and HASH.fullmatch(source["canonical_identity_sha256"]),
        "source_metadata_invalid",
    )
    return result


def _pair(backup, verification):
    backup = _receipt(backup, "backup")
    verification = _receipt(verification, "verify")
    for key in (
        "project",
        "instance_id",
        "server_uuid",
        "assets_sha256",
        "layout_sha256",
    ):
        require(backup[key] == verification[key], "receipt_binding_mismatch")
    before, after = backup["details"], verification["details"]
    require(
        backup["operation_id"] != verification["operation_id"]
        and after["backup_operation_id"] == backup["operation_id"],
        "backup_operation_mismatch",
    )
    name = "backup-" + backup["operation_id"] + ".glgui.age"
    require(
        before["ciphertext"] == after["ciphertext"] == name, "ciphertext_name_mismatch"
    )
    for key in (
        "ciphertext_sha256",
        "archive_sha256",
        "fingerprint",
        "app_files",
        "source",
    ):
        require(before[key] == after[key], "verification_content_mismatch")
    require(
        after["encryption_roundtrip_verified"] is True
        and after["reliable_backup_verified"] is True
        and after["package_verified"] is True
        and before["plaintext_package_validated"] is True
        and before["sql_freeze_verified"] is True,
        "backup_not_reliably_verified",
    )
    require(
        _time(backup["completed_at"]) <= _time(verification["started_at"]),
        "verification_time_order_invalid",
    )
    return backup, verification


def _fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(path, payload):
    with Path(path).open("xb") as stream:
        os.chmod(str(path), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _store(root, create=False):
    root = _path(root)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    _metadata(root, directory=True)
    require(
        {entry.name for entry in root.iterdir()} <= ROOT_MEMBERS, "unknown_store_member"
    )
    if create:
        # This namespace contains only ciphertext and public proofs. Keep the
        # private scheduler state outside it; allow File Station to read finals.
        os.chmod(str(root), 0o755)
    for name in ("artifacts", "pending"):
        child = root / name
        if create:
            child.mkdir(exist_ok=True)
            os.chmod(str(child), 0o755 if name == "artifacts" else 0o700)
        if child.exists() or child.is_symlink():
            _metadata(child, directory=True)
    lock = root / ".backup-store.lock"
    if lock.exists() or lock.is_symlink():
        _metadata(lock)
    return root


@contextmanager
def _lock(root):
    path = root / ".backup-store.lock"
    if not path.exists():
        try:
            _write(path, b"0")
        except FileExistsError:
            pass
    before = _metadata(path)
    descriptor = os.open(str(path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r+b") as stream:
        require(
            _signature(before) == _signature(os.fstat(stream.fileno())),
            "store_lock_changed",
        )
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BackupStoreError("store_busy") from None
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupStoreError("store_busy") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _manifest(backup, verification, protected):
    return {
        "format": FORMAT,
        "project": backup["project"],
        "instance_id": backup["instance_id"],
        "server_uuid": backup["server_uuid"],
        "backup_operation_id": backup["operation_id"],
        "verify_operation_id": verification["operation_id"],
        "completed_at": verification["completed_at"],
        "protected": protected,
        "files": {
            backup["details"]["ciphertext"]: backup["details"]["ciphertext_sha256"],
            "backup.json": digest(canonical(backup)),
            "verify.json": digest(canonical(verification)),
        },
    }


def _sums(manifest):
    hashes = dict(manifest["files"])
    hashes["BUNDLE.json"] = digest(canonical(manifest))
    return "".join(
        value + "  " + key + "\n" for key, value in sorted(hashes.items())
    ).encode("ascii")


def inspect_bundle(bundle_dir):
    """Read and stream-validate exactly five immutable public bundle members."""
    bundle = _path(bundle_dir)
    _metadata(bundle, directory=True)
    manifest = _json(bundle / "BUNDLE.json")
    require(
        set(manifest) == MANIFEST_KEYS and manifest.get("format") == FORMAT,
        "manifest_fields_invalid",
    )
    require(type(manifest["protected"]) is bool, "protected_flag_invalid")
    backup, verification = _pair(
        _json(bundle / "backup.json"), _json(bundle / "verify.json")
    )
    expected = _manifest(backup, verification, manifest["protected"])
    require(manifest == expected, "manifest_receipt_mismatch")
    names = set(expected["files"]) | {"BUNDLE.json", "SHA256SUMS"}
    require(
        {entry.name for entry in bundle.iterdir()} == names, "bundle_members_invalid"
    )
    for name, wanted in expected["files"].items():
        require(_read_file(bundle / name, ciphertext=name.endswith(".glgui.age")) == wanted,
                "bundle_file_hash_mismatch")
    require(
        _read_file(bundle / "BUNDLE.json", collect=True) == canonical(manifest)
        and _read_file(bundle / "SHA256SUMS", collect=True) == _sums(manifest),
        "bundle_manifest_or_sums_changed",
    )
    require(
        {entry.name for entry in bundle.iterdir()} == names,
        "bundle_changed_during_inspection",
    )
    return manifest


def publish(store_root, ciphertext, backup_receipt, verify_receipt, *, protected=False):
    """Stage outside artifacts, then atomically publish a new, non-overwritten set.

    All cooperative writers use the dedicated store lock. The store must be
    writable only by its administrator; it is not a hostile shared directory.
    A failure deliberately retains its pending directory for investigation.
    """
    require(type(protected) is bool, "protected_flag_invalid")
    backup, verification = _pair(backup_receipt, verify_receipt)
    source = Path(ciphertext)
    require(source.name == backup["details"]["ciphertext"], "ciphertext_name_mismatch")
    manifest = _manifest(backup, verification, protected)
    require(
        _read_file(source, ciphertext=True) == backup["details"]["ciphertext_sha256"],
        "ciphertext_hash_mismatch",
    )
    root = _store(store_root, create=True)
    target = root / "artifacts" / backup["operation_id"]
    with _lock(root):
        _store(root)
        if target.exists() or target.is_symlink():
            require(inspect_bundle(target) == manifest, "existing_bundle_mismatch")
            return manifest
        pending = (
            root / "pending" / (backup["operation_id"] + "-" + secrets.token_hex(8))
        )
        pending.mkdir(mode=0o700)
        destination = pending / source.name
        with destination.open("xb") as stream:
            os.chmod(str(destination), 0o600)
            actual = _read_file(source, destination=stream, ciphertext=True)
            stream.flush()
            os.fsync(stream.fileno())
        require(
            actual == backup["details"]["ciphertext_sha256"],
            "ciphertext_changed_during_publish",
        )
        _write(pending / "backup.json", canonical(backup))
        _write(pending / "verify.json", canonical(verification))
        _write(pending / "BUNDLE.json", canonical(manifest))
        _write(pending / "SHA256SUMS", _sums(manifest))
        require(inspect_bundle(pending) == manifest, "pending_bundle_invalid")
        for item in pending.iterdir():
            _metadata(item)
            os.chmod(str(item), 0o644)
        os.chmod(str(pending), 0o755)
        _fsync_directory(pending)
        require(
            not target.exists() and not target.is_symlink(), "publication_target_exists"
        )
        os.rename(str(pending), str(target))
        _fsync_directory(root / "artifacts")
        _fsync_directory(root / "pending")
    return manifest


def list_bundles(store_root):
    root = _store(store_root)
    artifacts = root / "artifacts"
    if not artifacts.exists():
        return []
    output = []
    names = sorted(entry.name for entry in artifacts.iterdir())
    for name in names:
        require(ID.fullmatch(name), "unknown_artifact_member")
        manifest = inspect_bundle(artifacts / name)
        require(
            manifest["backup_operation_id"] == name,
            "artifact_directory_identity_mismatch",
        )
        output.append((artifacts / name, manifest))
    require(
        sorted(entry.name for entry in artifacts.iterdir()) == names,
        "store_changed_during_inspection",
    )
    return output


def _inventory(bundles):
    return [
        {"name": path.name, "manifest_sha256": digest(canonical(manifest))}
        for path, manifest in bundles
    ]


def retention_plan(
    store_root, now, timezone_offset_minutes=480, daily=14, weekly=8, monthly=12,
    hold_ids=(),
):
    """Select current/recent calendar windows; weekly/monthly reuse daily files.

    Each window picks the latest successful bundle (ties by operation id). Empty
    windows stay empty. Always preserve protected bundles and the last success.
    Plans bind the complete verified inventory and never include pending data.
    """
    require(
        isinstance(now, datetime)
        and now.tzinfo is not None
        and now.utcoffset() is not None,
        "aware_now_required",
    )
    require(
        type(timezone_offset_minutes) is int
        and -1439 <= timezone_offset_minutes <= 1439,
        "timezone_offset_invalid",
    )
    require(
        all(
            type(number) is int and 0 <= number <= 10000
            for number in (daily, weekly, monthly)
        ),
        "retention_count_invalid",
    )
    root = _store(store_root)
    bundles = list_bundles(root)
    if bundles:
        identities = {
            (item["project"], item["instance_id"], item["server_uuid"])
            for _, item in bundles
        }
        require(len(identities) == 1, "mixed_store_identities")
    zone = timezone(timedelta(minutes=timezone_offset_minutes))
    today = now.astimezone(zone).date()
    monday = today - timedelta(days=today.weekday())
    month_number = today.year * 12 + today.month - 1
    records = []
    for path, manifest in bundles:
        instant = _time(manifest["completed_at"])
        require(instant <= now.astimezone(UTC), "future_bundle_timestamp")
        date = instant.astimezone(zone).date()
        records.append((instant, path.name, manifest, date))
    records.sort(key=lambda item: (item[0], item[1]), reverse=True)
    reasons = {name: [] for _, name, _, _ in records}
    require(isinstance(hold_ids, (list, tuple, set))
            and all(isinstance(name, str) and name in reasons for name in hold_ids),
            "retention_hold_ids_invalid")
    holds = sorted(set(hold_ids))
    chosen = set()
    for instant, name, manifest, date in records:
        candidates = (
            ("daily", (today - date).days, daily),
            (
                "weekly",
                (monday - (date - timedelta(days=date.weekday()))).days // 7,
                weekly,
            ),
            ("monthly", month_number - (date.year * 12 + date.month - 1), monthly),
        )
        for category, age, count in candidates:
            window = (category, age)
            if 0 <= age < count and window not in chosen:
                reasons[name].append(category + ":" + str(age))
                chosen.add(window)
        if manifest["protected"]:
            reasons[name].append("protected")
        if name in holds:
            reasons[name].append("external-copy-unconfirmed")
    if records:
        reasons[records[0][1]].append("last-success")
    plan = {
        "format": PLAN_FORMAT,
        "store_root": str(root),
        "now": now.astimezone(UTC).isoformat(),
        "timezone_offset_minutes": timezone_offset_minutes,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "hold_ids": holds,
        "inventory": _inventory(bundles),
        "keep": sorted(name for name, tags in reasons.items() if tags),
        "remove": sorted(name for name, tags in reasons.items() if not tags),
        "reasons": reasons,
    }
    plan["plan_sha256"] = digest(canonical(plan))
    return plan


def apply_retention(store_root, plan):
    """Apply exactly an unchanged reviewed plan; local immutable artifacts only.

    Call under the scheduler's global maintenance lock as well. Interrupted
    deletion leaves an invalid artifact and blocks subsequent automatic cleanup;
    it never widens deletion to repair the directory or touches cloud history.
    """
    require(isinstance(plan, dict), "retention_plan_invalid")
    supplied = copy.deepcopy(plan)
    expected_hash = supplied.pop("plan_sha256", None)
    require(
        expected_hash == digest(canonical(supplied)), "retention_plan_hash_mismatch"
    )
    root = _store(store_root)
    require(supplied.get("store_root") == str(root), "retention_plan_root_mismatch")
    with _lock(root):
        try:
            current = retention_plan(
                root,
                _time(supplied["now"]),
                supplied["timezone_offset_minutes"],
                supplied["daily"],
                supplied["weekly"],
                supplied["monthly"],
                supplied["hold_ids"],
            )
        except KeyError:
            raise BackupStoreError("retention_plan_invalid") from None
        require(plan == current, "retention_plan_stale_or_changed")
        removed = []
        for name in plan["remove"]:
            target = root / "artifacts" / name
            manifest = inspect_bundle(target)
            wanted = next(item for item in plan["inventory"] if item["name"] == name)
            require(
                digest(canonical(manifest)) == wanted["manifest_sha256"],
                "retention_bundle_changed",
            )
            names = set(manifest["files"]) | {"BUNDLE.json", "SHA256SUMS"}
            require(
                {entry.name for entry in target.iterdir()} == names,
                "bundle_members_invalid",
            )
            for child in sorted(names - {"BUNDLE.json"}) + ["BUNDLE.json"]:
                _metadata(target / child)
                (target / child).unlink()
            target.rmdir()
            removed.append(target)
        if removed:
            _fsync_directory(root / "artifacts")
        return removed
