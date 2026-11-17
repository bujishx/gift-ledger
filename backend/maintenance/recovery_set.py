"""Portable, strictly validated recovery sets, independent of application settings.

The outer ZIP is stored without compression. It contains exactly the files listed
by its manifest, including one existing admin backup package. Hashes detect
damage; they are not signatures. Confidential sets must be wrapped with age.
No key or identity is stored in a recovery set, and subprocess output is never
included in exceptions. Publication uses an exclusive hard link, never replace.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from app.services.database_backup import (
    DatabaseBackupError,
    ParsedAdminBackup,
    parse_admin_backup,
)
from app.services.personal_backup import PersonalBackupError, parse_personal_backup

FORMAT_NAME = "gift-ledger-recovery-set"
FORMAT_VERSION = 1
PACKAGE_SUFFIX = ".glrecoveryset"
MANIFEST_NAME = "manifest.json"
RECORD_NAME = "metadata/backup-record.json"
PROVENANCE_NAME = "metadata/provenance.json"
FINGERPRINT_NAME = "metadata/database-fingerprint.json"
MAX_TOTAL_BYTES = 3 * 1024**3
MAX_METADATA_BYTES = 1024**2
MAX_CONFIG_BYTES = 1024**2
MAX_APP_FILE_BYTES = 64 * 1024**2
MAX_ENTRIES = 256
AGE_TIMEOUT_SECONDS = 3600
CONFIG_WHITELIST = frozenset(
    {
        "deploy/.env",
        "deploy/secrets/mysql-root-password.txt",
        "deploy/secrets/mysql-app-password.txt",
        "deploy/secrets/jwt-secret.txt",
        "deploy/certs/fullchain.pem",
        "deploy/certs/privkey.pem",
    }
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFETY_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SAFETY_KEYS = {
    "version", "token", "user_id", "created_at", "expires_at", "byte_length", "sha256",
}
_RECORD_KEYS = {
    "backup_id", "backup_type", "created_at", "completed_at", "size", "sha256",
    "database", "schema_revision", "success", "verified", "offsite_status",
    "offsite_sha256", "failure_summary", "restore_target", "restore_structure_verified_at",
}
_MANIFEST_KEYS = {
    "format", "format_version", "backup_id", "database_package", "config_files", "app_files",
    "files",
}


class RecoverySetError(RuntimeError):
    """Stable public error; no source paths, contents or subprocess output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str = "recovery_set_invalid") -> None:
    raise RecoverySetError(code, "恢复资料集不符合安全格式或完整性要求")


def _valid_name(name: Any) -> str:
    if type(name) is not str or len(name) > 240 or "\\" in name or ":" in name:
        _fail("recovery_set_unsafe_path")
    parts = name.split("/")
    if not parts or any(
        part in {"", ".", ".."} or not _SAFE_SEGMENT.fullmatch(part)
        or part.endswith((".", " "))
        or part.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        for part in parts
    ):
        _fail("recovery_set_unsafe_path")
    return name


def _no_links(path: Path) -> None:
    """Reject links/junctions in every existing component, including parents."""
    for part in (path.absolute(), *path.absolute().parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("recovery_set_symlink")


def _regular_file(path: Path, limit: int = MAX_TOTAL_BYTES) -> None:
    _no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        _fail("recovery_set_file_invalid")


def _private_directory(path: Path) -> None:
    _no_links(path)
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        _fail("recovery_set_directory_invalid")
    # POSIX permissions are enforced in the maintenance container. On Windows,
    # the owner must additionally use a private NTFS directory with a suitable ACL.
    os.chmod(path, 0o700)


def _hash_file(path: Path, *, reject_age_identity: bool = False) -> tuple[int, str]:
    digest = sha256()
    size = 0
    previous = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            size += len(chunk)
            if size > MAX_TOTAL_BYTES:
                _fail("recovery_set_too_large")
            if reject_age_identity and b"AGE-SECRET-KEY-" in previous + chunk:
                _fail("recovery_set_identity_not_allowed")
            previous = chunk[-32:]
            digest.update(chunk)
    return size, digest.hexdigest()


def _copy_bounded(incoming, outgoing, limit: int) -> None:
    size = 0
    while chunk := incoming.read(1024**2):
        size += len(chunk)
        if size > limit:
            _fail("recovery_set_too_large")
        outgoing.write(chunk)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            _fail("recovery_set_duplicate_json_key")
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    if len(data) > MAX_METADATA_BYTES or data.startswith(b"\xef\xbb\xbf"):
        _fail()
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: _fail(),
        )
    except (ValueError, UnicodeDecodeError):
        _fail()
    if type(value) is not dict:
        _fail()
    return value


def _utc(value: Any) -> datetime:
    if type(value) is not str:
        _fail()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail()
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.now(UTC).utcoffset():
        _fail()
    return parsed


def _record(value: dict, *, normalize: bool = False) -> dict:
    if type(value) is not dict or set(value) != _RECORD_KEYS:
        _fail("recovery_set_record_invalid")
    value = dict(value)
    if not isinstance(value["backup_id"], str) or not _BACKUP_ID.fullmatch(value["backup_id"]):
        _fail()
    if value["backup_type"] not in {"automatic", "manual", "pre-maintenance", "rehearsal"}:
        _fail()
    if type(value["size"]) is not int or not 0 < value["size"] <= MAX_TOTAL_BYTES:
        _fail()
    if type(value["sha256"]) is not str or not _SHA.fullmatch(value["sha256"]):
        _fail()
    if value["success"] is not True or type(value["verified"]) is not bool:
        _fail()
    created = _utc(value["created_at"])
    completed = _utc(value["completed_at"])
    if completed < created:
        _fail()
    for name in ("database", "schema_revision"):
        if type(value[name]) is not str or not _TABLE.fullmatch(value[name]):
            _fail()
    if value["offsite_status"] not in {"not_configured", "copied", "failed"}:
        _fail()
    for name in ("offsite_sha256",):
        if value[name] is not None and (
            type(value[name]) is not str or not _SHA.fullmatch(value[name])
        ):
            _fail()
    for name in ("failure_summary", "restore_target"):
        if value[name] is not None and type(value[name]) is not str:
            _fail()
    if value["failure_summary"] is not None:
        _fail()
    if value["restore_structure_verified_at"] is not None:
        _utc(value["restore_structure_verified_at"])
    if normalize:
        value.update(
            verified=False, restore_target=None, restore_structure_verified_at=None,
            offsite_status="not_configured", offsite_sha256=None,
        )
    elif (
        value["verified"] or value["restore_target"] is not None
        or value["restore_structure_verified_at"] is not None
        or value["offsite_status"] != "not_configured" or value["offsite_sha256"] is not None
    ):
        _fail("recovery_set_import_state_invalid")
    return value


def normalize_backup_record(record: dict) -> dict:
    """Export just one record without inheriting any restore or offsite approval."""
    return _record(record, normalize=True)


def _provenance(value: dict) -> dict:
    if type(value) is not dict or set(value) != {
        "application_version", "schema_revision", "source_ref", "images",
    }:
        _fail("recovery_set_provenance_invalid")
    if value["application_version"] != "1.5.0" or value["schema_revision"] != "schema_v2_s011":
        _fail("recovery_set_version_incompatible")
    if (
        type(value["source_ref"]) is not str
        or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value["source_ref"])
    ):
        _fail()
    images = value["images"]
    if type(images) is not dict or set(images) != {"api", "web", "db"}:
        _fail()
    if any(type(item) is not str or not _IMAGE.fullmatch(item) for item in images.values()):
        _fail()
    return value


def _fingerprint(value: dict) -> dict:
    if type(value) is not dict or set(value) != {"algorithm", "schema_revision", "tables"}:
        _fail("recovery_set_fingerprint_invalid")
    if value["algorithm"] != "sha256-canonical-v1" or value["schema_revision"] != "schema_v2_s011":
        _fail()
    tables = value["tables"]
    if type(tables) is not dict or not tables or len(tables) > 128:
        _fail()
    for name, descriptor in tables.items():
        if type(name) is not str or not _TABLE.fullmatch(name):
            _fail()
        if type(descriptor) is not dict or set(descriptor) != {"rows", "sha256"}:
            _fail()
        if type(descriptor["rows"]) is not int or descriptor["rows"] < 0:
            _fail()
        if type(descriptor["sha256"]) is not str or not _SHA.fullmatch(descriptor["sha256"]):
            _fail()
    return value


def _mapping(value: dict, category: str) -> dict[str, str]:
    if type(value) is not dict:
        _fail()
    result = {}
    for name, member in value.items():
        _valid_name(name)
        _valid_name(member)
        if category == "config":
            if name not in CONFIG_WHITELIST or member != f"config/{name}":
                _fail("recovery_set_config_not_allowed")
        elif (
            not (name == "admin-backups/index.json" or name.startswith("personal-safety-backups/"))
            or member != f"app-data/{name}"
            or any(token in name.lower() for token in ("identity", "recovery-key", ".agekey"))
            or (
                name.startswith("personal-safety-backups/")
                and (len(name.split("/")) != 2 or not name.endswith((".giftledger", ".json")))
            )
        ):
            _fail("recovery_set_app_file_not_allowed")
        result[name] = member
    if category == "config" and set(result) != CONFIG_WHITELIST:
        _fail("recovery_set_config_incomplete")
    if category == "app" and "admin-backups/index.json" not in result:
        _fail("recovery_set_app_index_missing")
    return result


def _validate_app_files(mapping: dict[str, str], read_member, record: dict) -> None:
    """Require a fresh single-record index and intact personal safety pairs.

    Personal backups intentionally contain no source user ID. The metadata's
    positive user_id is preserved; the recovery engine must check its existence
    against the restored users table before restoring this application directory.
    """
    index = _json(read_member(mapping["admin-backups/index.json"]))
    if (
        set(index) != {"version", "entries"} or type(index["version"]) is not int
        or index["version"] != 2 or index["entries"] != [record]
    ):
        _fail("recovery_set_app_index_invalid")
    metadata_tokens = set()
    content_tokens = set()
    for name in mapping:
        if not name.startswith("personal-safety-backups/"):
            continue
        filename = name.split("/")[1]
        token, suffix = filename.rsplit(".", 1)
        if not _SAFETY_TOKEN.fullmatch(token):
            _fail("recovery_set_safety_invalid")
        (metadata_tokens if suffix == "json" else content_tokens).add(token)
    if metadata_tokens != content_tokens:
        _fail("recovery_set_safety_pair_missing")
    for token in metadata_tokens:
        metadata_name = f"personal-safety-backups/{token}.json"
        content_name = f"personal-safety-backups/{token}.giftledger"
        metadata = _json(read_member(mapping[metadata_name]))
        if (
            set(metadata) != _SAFETY_KEYS or type(metadata["version"]) is not int
            or metadata["version"] != 1 or metadata["token"] != token
            or type(metadata["user_id"]) is not int or metadata["user_id"] <= 0
            or type(metadata["byte_length"]) is not int
            or not 0 < metadata["byte_length"] <= MAX_APP_FILE_BYTES
            or type(metadata["sha256"]) is not str or not _SHA.fullmatch(metadata["sha256"])
        ):
            _fail("recovery_set_safety_invalid")
        if _utc(metadata["created_at"]) > _utc(metadata["expires_at"]):
            _fail("recovery_set_safety_invalid")
        content = read_member(mapping[content_name])
        if (
            len(content) != metadata["byte_length"]
            or sha256(content).hexdigest() != metadata["sha256"]
        ):
            _fail("recovery_set_safety_hash_mismatch")
        try:
            parse_personal_backup(content)
        except PersonalBackupError:
            _fail("recovery_set_safety_content_invalid")


def _match_database(record: dict, provenance: dict, fingerprint: dict, parsed: ParsedAdminBackup):
    for name in ("backup_id", "backup_type", "database", "schema_revision"):
        if record[name] != getattr(parsed, name):
            _fail("recovery_set_database_mismatch")
    if _utc(record["created_at"]) != parsed.created_at:
        _fail("recovery_set_database_mismatch")
    if (
        parsed.application_version != provenance["application_version"]
        or parsed.schema_revision != provenance["schema_revision"]
        or parsed.alembic_version != provenance["schema_revision"]
        or fingerprint["schema_revision"] != parsed.schema_revision
        or {name: item["rows"] for name, item in fingerprint["tables"].items()} != parsed.tables
    ):
        _fail("recovery_set_database_mismatch")


def _zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _publish(part: Path, target: Path) -> Path:
    _no_links(target)
    # link() is exclusive on both POSIX and NTFS: an existing target is never overwritten.
    try:
        os.link(part, target)
    except FileExistsError:
        raise RecoverySetError("recovery_set_target_exists", "目标已存在，拒绝覆盖") from None
    finally:
        part.unlink(missing_ok=True)
    return target


def _part(parent: Path) -> Path:
    fd, raw = tempfile.mkstemp(prefix=".recovery-", suffix=".part", dir=parent)
    os.close(fd)
    path = Path(raw)
    os.chmod(path, 0o600)
    return path


def build_recovery_set(
    output_dir: Path,
    package_path: Path,
    record: dict,
    provenance: dict,
    database_fingerprint: dict,
    config_files: dict[str, Path],
    app_files: dict[str, Path],
) -> Path:
    """Build a new complete ZIP; the successful source record is not modified."""
    part: Path | None = None
    try:
        normalized_record = _record(record, normalize=True)
        _provenance(provenance)
        _fingerprint(database_fingerprint)
        _regular_file(package_path)
        parsed = parse_admin_backup(package_path)
        _match_database(normalized_record, provenance, database_fingerprint, parsed)
        size, digest = _hash_file(package_path)
        if (size, digest) != (record["size"], record["sha256"]):
            _fail("recovery_set_database_mismatch")
        database_name = f"database/{record['backup_id']}.gladminbackup"
        config_mapping = _mapping({key: f"config/{key}" for key in config_files}, "config")
        app_mapping = _mapping({key: f"app-data/{key}" for key in app_files}, "app")
        sources = {database_name: package_path}
        sources.update({config_mapping[name]: path for name, path in config_files.items()})
        sources.update({app_mapping[name]: path for name, path in app_files.items()})
        json_files = {
            RECORD_NAME: _json_bytes(normalized_record),
            PROVENANCE_NAME: _json_bytes(provenance),
            FINGERPRINT_NAME: _json_bytes(database_fingerprint),
        }
        if len(sources) + len(json_files) + 1 > MAX_ENTRIES:
            _fail("recovery_set_too_many_files")
        files = {}
        for name, path in sources.items():
            limit = (
                MAX_CONFIG_BYTES if name.startswith("config/")
                else MAX_APP_FILE_BYTES if name.startswith("app-data/") else MAX_TOTAL_BYTES
            )
            _regular_file(path, limit)
            item_size, item_digest = _hash_file(
                path, reject_age_identity=name.startswith(("config/", "app-data/")),
            )
            files[name] = {"size": item_size, "sha256": item_digest}
        for name, data in json_files.items():
            if len(data) > MAX_METADATA_BYTES:
                _fail("recovery_set_too_large")
            files[name] = {"size": len(data), "sha256": sha256(data).hexdigest()}
        _validate_app_files(
            app_mapping, lambda member: sources[member].read_bytes(), normalized_record,
        )
        if sum(item["size"] for item in files.values()) > MAX_TOTAL_BYTES:
            _fail("recovery_set_too_large")
        manifest = {
            "format": FORMAT_NAME, "format_version": FORMAT_VERSION,
            "backup_id": record["backup_id"], "database_package": database_name,
            "config_files": config_mapping, "app_files": app_mapping, "files": files,
        }
        _private_directory(output_dir)
        target = output_dir / f"{record['backup_id']}{PACKAGE_SUFFIX}"
        if target.exists() or target.is_symlink():
            raise RecoverySetError("recovery_set_target_exists", "目标已存在，拒绝覆盖")
        part = _part(output_dir)
        with ZipFile(part, "w", compression=ZIP_STORED, allowZip64=True) as archive:
            archive.writestr(_zip_info(MANIFEST_NAME), _json_bytes(manifest))
            for name, data in json_files.items():
                archive.writestr(_zip_info(name), data)
            for name, source in sources.items():
                _no_links(source)
                with source.open("rb") as incoming, archive.open(_zip_info(name), "w") as outgoing:
                    _copy_bounded(incoming, outgoing, files[name]["size"])
        with part.open("r+b") as stream:
            os.fsync(stream.fileno())
        verify_recovery_set(part)
        return _publish(part, target)
    except RecoverySetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, BadZipFile, DatabaseBackupError):
        raise RecoverySetError("recovery_set_build_failed", "恢复资料集生成失败") from None
    finally:
        if part is not None:
            part.unlink(missing_ok=True)


def _entries(archive: ZipFile) -> dict[str, ZipInfo]:
    entries = archive.infolist()
    if not 1 <= len(entries) <= MAX_ENTRIES:
        _fail("recovery_set_too_many_files")
    result = {}
    folded = set()
    total = 0
    for item in entries:
        if item.orig_filename != item.filename:
            _fail("recovery_set_unsafe_path")
        name = _valid_name(item.filename)
        mode = (item.external_attr >> 16) & 0o170000
        if (
            item.is_dir() or mode not in {0, stat.S_IFREG} or item.flag_bits & 1
            or item.compress_type != ZIP_STORED or item.file_size < 0
        ):
            _fail("recovery_set_member_invalid")
        if name.casefold() in folded:
            _fail("recovery_set_duplicate_member")
        folded.add(name.casefold())
        total += item.file_size
        if total > MAX_TOTAL_BYTES + MAX_METADATA_BYTES:
            _fail("recovery_set_too_large")
        if name == MANIFEST_NAME and item.file_size > MAX_METADATA_BYTES:
            _fail("recovery_set_too_large")
        result[name] = item
    return result


def _read_json(archive: ZipFile, entries: dict, name: str) -> dict:
    if name not in entries or entries[name].file_size > MAX_METADATA_BYTES:
        _fail()
    return _json(archive.read(name))


def verify_recovery_set(path: Path) -> dict:
    """Verify every byte and nested SQL offline; returns no configuration contents."""
    try:
        _regular_file(path, MAX_TOTAL_BYTES + MAX_METADATA_BYTES)
        with ZipFile(path) as archive:
            entries = _entries(archive)
            manifest = _read_json(archive, entries, MANIFEST_NAME)
            if set(manifest) != _MANIFEST_KEYS:
                _fail("recovery_set_unknown_fields")
            if manifest["format"] != FORMAT_NAME or type(manifest["format_version"]) is not int:
                _fail()
            if manifest["format_version"] != FORMAT_VERSION:
                _fail("recovery_set_format_unsupported")
            backup_id = manifest["backup_id"]
            if type(backup_id) is not str or not _BACKUP_ID.fullmatch(backup_id):
                _fail()
            database_name = f"database/{backup_id}.gladminbackup"
            if manifest["database_package"] != database_name:
                _fail()
            config_mapping = _mapping(manifest["config_files"], "config")
            app_mapping = _mapping(manifest["app_files"], "app")
            expected = {
                RECORD_NAME, PROVENANCE_NAME, FINGERPRINT_NAME, database_name,
                *config_mapping.values(), *app_mapping.values(),
            }
            files = manifest["files"]
            if (
                type(files) is not dict or set(files) != expected
                or set(entries) != expected | {MANIFEST_NAME}
            ):
                _fail("recovery_set_unexpected_files")
            for name, descriptor in files.items():
                if type(descriptor) is not dict or set(descriptor) != {"size", "sha256"}:
                    _fail()
                size = descriptor["size"]
                digest = descriptor["sha256"]
                limit = (
                    MAX_CONFIG_BYTES if name.startswith("config/")
                    else MAX_APP_FILE_BYTES if name.startswith("app-data/")
                    else MAX_METADATA_BYTES if name.startswith("metadata/") else MAX_TOTAL_BYTES
                )
                if (
                    type(size) is not int or not 0 < size <= limit
                    or type(digest) is not str or not _SHA.fullmatch(digest)
                    or entries[name].file_size != size
                ):
                    _fail()
                actual_size = 0
                actual_digest = sha256()
                previous = b""
                with archive.open(name) as stream:
                    while chunk := stream.read(1024**2):
                        actual_size += len(chunk)
                        if actual_size > size:
                            _fail()
                        if name.startswith(("config/", "app-data/")) and (
                            b"AGE-SECRET-KEY-" in previous + chunk
                        ):
                            _fail("recovery_set_identity_not_allowed")
                        previous = chunk[-32:]
                        actual_digest.update(chunk)
                if actual_size != size or actual_digest.hexdigest() != digest:
                    _fail("recovery_set_hash_mismatch")
            record = _record(_read_json(archive, entries, RECORD_NAME))
            provenance = _provenance(_read_json(archive, entries, PROVENANCE_NAME))
            fingerprint = _fingerprint(_read_json(archive, entries, FINGERPRINT_NAME))
            _validate_app_files(app_mapping, archive.read, record)
            if record["backup_id"] != backup_id or (
                record["size"], record["sha256"]
            ) != (files[database_name]["size"], files[database_name]["sha256"]):
                _fail("recovery_set_database_mismatch")
            with tempfile.TemporaryDirectory(prefix="gl-recovery-verify-") as raw_temp:
                temporary = Path(raw_temp)
                os.chmod(temporary, 0o700)
                database_path = temporary / "database.gladminbackup"
                with archive.open(database_name) as incoming, database_path.open("xb") as outgoing:
                    os.chmod(database_path, 0o600)
                    _copy_bounded(incoming, outgoing, files[database_name]["size"])
                parsed = parse_admin_backup(database_path)
                _match_database(record, provenance, fingerprint, parsed)
            return {
                **manifest, "record": record, "provenance": provenance,
                "database_fingerprint": fingerprint,
            }
    except RecoverySetError:
        raise
    except (
        OSError, ValueError, TypeError, KeyError, BadZipFile, RuntimeError, DatabaseBackupError,
    ):
        raise RecoverySetError("recovery_set_verify_failed", "恢复资料集校验失败") from None


def extract_recovery_set(path: Path, destination: Path) -> dict:
    """Extract verified files into an exclusively created directory, never merge.

    A private staging directory is fully verified first. Destination ownership is
    acquired with mkdir(exist_ok=False), and manifest.json is published last.
    Therefore interrupted extraction has no success manifest and must be discarded.
    """
    created = False
    try:
        verified = verify_recovery_set(path)
        _no_links(destination)
        if destination.exists():
            raise RecoverySetError("recovery_set_target_exists", "目标已存在，拒绝覆盖")
        _private_directory(destination.parent)
        with tempfile.TemporaryDirectory(
            prefix=".recovery-extract-", dir=destination.parent,
        ) as raw:
            staged = Path(raw)
            os.chmod(staged, 0o700)
            with ZipFile(path) as archive:
                for name in verified["files"]:
                    output = staged.joinpath(*PurePosixPath(name).parts)
                    _private_directory(output.parent)
                    with archive.open(name) as incoming, output.open("xb") as outgoing:
                        os.chmod(output, 0o600)
                        _copy_bounded(incoming, outgoing, verified["files"][name]["size"])
                    if _hash_file(output) != (
                        verified["files"][name]["size"], verified["files"][name]["sha256"],
                    ):
                        _fail("recovery_set_hash_mismatch")
            destination.mkdir(mode=0o700, exist_ok=False)
            created = True
            for name in verified["files"]:
                source = staged.joinpath(*PurePosixPath(name).parts)
                output = destination.joinpath(*PurePosixPath(name).parts)
                _private_directory(output.parent)
                os.link(source, output)
            # The successful completion marker is published only after every file.
            manifest_path = destination / MANIFEST_NAME
            with manifest_path.open("xb") as stream:
                os.chmod(manifest_path, 0o600)
                stream.write(_json_bytes({key: verified[key] for key in _MANIFEST_KEYS}))
                stream.flush()
                os.fsync(stream.fileno())
            return {
                **verified, "root": destination,
                "package_path": destination / verified["database_package"],
            }
    except RecoverySetError:
        raise
    except FileExistsError:
        raise RecoverySetError("recovery_set_target_exists", "目标已存在，拒绝覆盖") from None
    except (OSError, ValueError, KeyError, BadZipFile):
        raise RecoverySetError("recovery_set_extract_failed", "恢复资料集提取失败") from None
    finally:
        if created and not (destination / MANIFEST_NAME).exists():
            # Only the newly acquired, private destination is eligible for cleanup.
            _no_links(destination)
            shutil.rmtree(destination)


def _age(command: list[str]) -> None:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, timeout=AGE_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RecoverySetError("recovery_set_crypto_failed", "资料集加密或解密失败") from None
    if result.returncode != 0:
        raise RecoverySetError("recovery_set_crypto_failed", "资料集加密或解密失败")


def encrypt_recovery_set(
    source_path: Path, recipient_file: Path, output_path: Path, age_path: str = "age",
) -> Path:
    """Wrap the entire set using an age recipient file; never package identities."""
    part: Path | None = None
    try:
        verify_recovery_set(source_path)
        _regular_file(recipient_file, MAX_CONFIG_BYTES)
        _no_links(output_path)
        if output_path.exists():
            raise RecoverySetError("recovery_set_target_exists", "目标已存在，拒绝覆盖")
        _private_directory(output_path.parent)
        part = _part(output_path.parent)
        _age([
            age_path, "--encrypt", "--recipients-file", str(recipient_file.absolute()),
            "--output", str(part.absolute()), str(source_path.absolute()),
        ])
        _regular_file(part, MAX_TOTAL_BYTES + 16 * MAX_METADATA_BYTES)
        with part.open("r+b") as stream:
            if stream.read(22) != b"age-encryption.org/v1\n":
                _fail("recovery_set_crypto_failed")
            os.fsync(stream.fileno())
        return _publish(part, output_path)
    except RecoverySetError:
        raise
    except OSError:
        raise RecoverySetError("recovery_set_crypto_failed", "资料集加密或解密失败") from None
    finally:
        if part is not None:
            part.unlink(missing_ok=True)


def decrypt_recovery_set(
    encrypted_path: Path, identity_file: Path, output_dir: Path, age_path: str = "age",
) -> Path:
    """Decrypt, verify all plaintext, then publish a new recovery ZIP exclusively."""
    part: Path | None = None
    try:
        _regular_file(encrypted_path, MAX_TOTAL_BYTES + 16 * MAX_METADATA_BYTES)
        _regular_file(identity_file, MAX_CONFIG_BYTES)
        _private_directory(output_dir)
        part = _part(output_dir)
        _age([
            age_path, "--decrypt", "--identity", str(identity_file.absolute()),
            "--output", str(part.absolute()), str(encrypted_path.absolute()),
        ])
        verified = verify_recovery_set(part)
        with part.open("r+b") as stream:
            os.fsync(stream.fileno())
        return _publish(part, output_dir / f"{verified['backup_id']}{PACKAGE_SUFFIX}")
    except RecoverySetError:
        raise
    except OSError:
        raise RecoverySetError("recovery_set_crypto_failed", "资料集加密或解密失败") from None
    finally:
        if part is not None:
            part.unlink(missing_ok=True)
