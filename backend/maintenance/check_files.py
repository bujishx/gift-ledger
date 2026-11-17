"""Read-only application-file acceptance as the unchanged API's Linux UID.

Only the isolated candidate volume is allowed. Existing stores are reused with
directory preparation disabled, and expiry deletion is blocked in this process.
The report contains counts and aggregate hashes, never safety tokens or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from app.services.backup_index import BackupIndex
from app.services.database_backup import parse_admin_backup, sha256_file
from app.services.personal_backup import parse_personal_backup
from app.services.personal_safety_backup_store import PersonalSafetyBackupStore
from maintenance.database import guarded_engine, load_config, require_guard
from maintenance.rehearsal_data import EXPECTED_USERS, USERNAMES, work_file, write_new_result

API_UID = 10001
API_GID = 0
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
MAX_METADATA_BYTES = 64 * 1024


def _private_path(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    expected_mode = 0o700 if directory else 0o600
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected_type(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != API_UID or info.st_gid != API_GID
            or stat.S_IMODE(info.st_mode) != expected_mode):
        raise ValueError("Candidate files must have the unchanged application's private ownership")


class _ReadOnlySafetyStore(PersonalSafetyBackupStore):
    def _prepare_directory(self) -> None:
        _private_path(self.directory, directory=True)


class _ReadOnlyBackupIndex(BackupIndex):
    def _prepare_directory(self) -> None:
        _private_path(self.backup_dir, directory=True)


@contextmanager
def as_application_user():
    """Drop only this helper process to the exact existing API UID/GID."""
    if not hasattr(os, "seteuid") or os.geteuid() != 0 or os.getegid() != API_GID:
        raise ValueError("The isolated file check requires its Linux maintenance root process")
    original_groups = os.getgroups()
    try:
        os.setgroups([])
        os.seteuid(API_UID)
        yield
    finally:
        os.seteuid(0)
        os.setgroups(original_groups)


def _inventory(root: Path) -> dict:
    _private_path(root, directory=True)
    files = {}
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        for name in subdirectories:
            _private_path(Path(directory) / name, directory=True)
        for name in names:
            path = Path(directory) / name
            _private_path(path)
            info = path.stat()
            files[path.relative_to(root).as_posix()] = {
                "sha256": sha256_file(path)[0], "size": info.st_size,
                "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            }
    if not files:
        raise ValueError("The restored candidate application volume must contain files")
    return files


def _validate_index(root: Path) -> dict:
    directory = root / "admin-backups"
    index = _ReadOnlyBackupIndex(directory)
    records = index.load()
    if len(records) != 1:
        raise ValueError("The candidate index must register only the imported current backup")
    record = records[0]
    if (not record.success or record.verified or record.restore_target is not None
            or record.restore_structure_verified_at is not None
            or record.offsite_status != "not_configured" or record.offsite_sha256 is not None
            or record.failure_summary is not None):
        raise ValueError("Imported index must not inherit previous verification state")
    package = directory / f"{record.backup_id}.gladminbackup"
    if set(path.name for path in directory.iterdir()) != {"index.json", package.name}:
        raise ValueError("The restored index and database packages do not match")
    digest, size = sha256_file(package)
    if (digest, size) != (record.sha256, record.size):
        raise ValueError("The registered database package is missing or corrupt")
    parsed = parse_admin_backup(package)
    if (parsed.backup_id != record.backup_id or parsed.database != "gift_ledger_v2"
            or parsed.schema_revision != "schema_v2_s011" or parsed.application_version != "1.5.0"):
        raise ValueError("The readable database package and registration disagree")
    return {"registered_backups": 1, "fresh_import_verification_state": True,
            "database_package_readable": True}


def _delete_forbidden(_path) -> None:
    raise ValueError("The application-file acceptance helper cannot delete anything")


def _validate_safety_backups(root: Path, users: dict[int, str]) -> list[dict]:
    directory = root / "personal-safety-backups"
    _private_path(directory, directory=True)
    files = list(directory.iterdir())
    if not files or len(files) > 4:
        raise ValueError("The current synthetic fixture requires one or two safety backup pairs")
    tokens = {path.stem for path in files}
    if any(not TOKEN_PATTERN.fullmatch(token) for token in tokens):
        raise ValueError("The personal safety backup filenames are invalid")
    expected_names = {token + suffix for token in tokens for suffix in (".json", ".giftledger")}
    if {path.name for path in files} != expected_names:
        raise ValueError("Each personal backup must have a corresponding metadata file")
    store = _ReadOnlySafetyStore(directory)
    results = []
    checked_users = set()
    current = datetime.now(UTC)
    # load() normally removes expired files. Blocking that helper guarantees this
    # probe cannot remove files even if metadata changes between the two reads.
    with patch("app.services.personal_safety_backup_store._unlink_quietly", _delete_forbidden):
        for token in sorted(tokens):
            metadata_path = directory / f"{token}.json"
            if metadata_path.stat().st_size > MAX_METADATA_BYTES:
                raise ValueError("Personal backup metadata exceeds its size limit")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            user_id = metadata.get("user_id")
            if type(user_id) is not int or user_id not in users:
                raise ValueError("The safety backup is not owned by a synthetic rehearsal user")
            loaded = store.load(token, user_id=user_id, now=current)
            parsed = parse_personal_backup(loaded.content)
            username = users[user_id]
            expected = EXPECTED_USERS[username]
            if (parsed.schema_revision != "schema_v2_s011" or parsed.application_version != "1.5.0"
                    or len(parsed.contacts) != expected["contact_count"]
                    or len(parsed.events) != expected["event_count"]
                    or len(parsed.gifts) != expected["gift_count"]):
                raise ValueError("The restored personal safety backup has incorrect business data")
            income = sum((gift.amount for gift in parsed.gifts if gift.direction == "income"),
                         Decimal(0))
            expense = sum((gift.amount for gift in parsed.gifts if gift.direction == "expense"),
                          Decimal(0))
            if (income != Decimal(expected["income_amount"])
                    or expense != Decimal(expected["expense_amount"])):
                raise ValueError("The restored personal safety backup has incorrect money totals")
            checked_users.add(username)
            results.append({"username": username, "contacts": len(parsed.contacts),
                            "events": len(parsed.events), "gifts": len(parsed.gifts),
                            "content_sha256": loaded.sha256, "readable_as_api_uid": True})
    if USERNAMES[0] not in checked_users:
        raise ValueError("The required owner safety backup is missing")
    return results


def check_files(config, app_data: Path) -> dict:
    if (config.role, config.host) != ("restore", "restore-db"):
        raise ValueError("File validation is limited to the isolated restored candidate")
    if app_data != Path("/work/restore-app-data"):
        raise ValueError("Only the independent candidate application data mount is permitted")
    alias = Path("/candidate-app-data")
    with guarded_engine(config, "gift_ledger_v2") as engine:
        if app_data.is_symlink() or alias.is_symlink():
            raise ValueError("Candidate mounts cannot be symbolic links")
        mounted_stat, alias_stat = app_data.stat(), alias.stat()
        if (mounted_stat.st_dev, mounted_stat.st_ino) != (alias_stat.st_dev, alias_stat.st_ino):
            raise ValueError("Candidate volume aliases do not reference the same files")
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT id, username FROM users ORDER BY id")).all()
            users = {row.id: row.username for row in rows}
        if set(users.values()) != set(USERNAMES) or len(users) != len(USERNAMES):
            raise ValueError("Application file checks require the two synthetic users only")
        before = _inventory(alias)
        with as_application_user():
            _inventory(alias)
            index_report = _validate_index(alias)
            safety_reports = _validate_safety_backups(alias, users)
        after = _inventory(alias)
        if before != after:
            raise ValueError("Read-only validation unexpectedly changed candidate files")
        require_guard(engine, config)
    inventory_digest = sha256(json.dumps(
        before, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "format": "gift-ledger-synthetic-application-file-check", "version": 1,
        "run_id": config.run_id, "synthetic_only": True, "read_only": True,
        "effective_application_uid_checked": API_UID, "application_gid_checked": API_GID,
        "private_ownership_and_modes_verified": True, "files_unchanged": True,
        "application_file_count": len(before), "inventory_sha256": inventory_digest,
        "backup_index": index_report, "personal_safety_backups": safety_reports,
        "success": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only candidate application-file acceptance")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--app-data", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result_path = work_file(args.result, Path(config.work_root))
        if result_path.exists():
            raise ValueError("Existing evidence cannot be overwritten")
        result = check_files(config, args.app_data)
        write_new_result(result_path, Path(config.work_root), result)
    except Exception as exc:
        print(json.dumps({"success": False, "operation": "application-file-check",
                          "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"success": True, "operation": "application-file-check",
                      "run_id": config.run_id,
                      "application_file_count": result["application_file_count"],
                      "api_uid_read_verified": True, "files_unchanged": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
