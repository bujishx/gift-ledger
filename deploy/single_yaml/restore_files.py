"""Copy five encrypted files into a restoration target, or retire that exact copy."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

import backup_export
import backup_store


INPUT = Path("/work/restore-input")
INCOMING = Path("/work/incoming")


def file_identity(path):
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        raise ValueError("invalid_managed_restore_file")
    return [item.st_dev, item.st_ino, item.st_size, item.st_ctime_ns]


def publish_owner(folder, marker, backup_id, digest):
    statdir = folder.lstat()
    proof = {"format": 1, "backup_id": backup_id, "manifest_sha256": digest,
             "directory": [statdir.st_dev, statdir.st_ino],
             "files": {p.name: file_identity(p) for p in folder.iterdir()}}
    with tempfile.NamedTemporaryFile(dir=marker.parent, prefix=".restore-receipt-", delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            tmp.write(backup_store.canonical(proof))
            tmp.flush()
            os.fsync(tmp.fileno())
            backup_export._rename_noreplace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)


def read_owner(marker, backup_id, digest):
    if not os.path.lexists(marker):
        return None
    item = marker.lstat()
    if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o600 or item.st_size > 8192):
        return None
    try:
        proof = json.loads(marker.read_bytes())
    except (ValueError, UnicodeError):
        return None
    names = {"BUNDLE.json", "SHA256SUMS", "backup.json", "verify.json", f"backup-{backup_id}.glgui.age"}
    if (not isinstance(proof, dict) or proof.get("format") != 1 or proof.get("backup_id") != backup_id
            or proof.get("manifest_sha256") != digest
            or not isinstance(proof.get("directory"), list) or len(proof["directory"]) != 2
            or not isinstance(proof.get("files"), dict) or set(proof["files"]) != names):
        return None
    return proof


def managed_folder(folder, proof, *, partial=False):
    statdir = folder.lstat()
    if not stat.S_ISDIR(statdir.st_mode) or proof["directory"] != [statdir.st_dev, statdir.st_ino]:
        return False
    present = {p.name for p in folder.iterdir()}
    names = set(proof["files"])
    try:
        return (present <= names if partial else present == names) and all(
            file_identity(folder / name) == proof["files"][name] for name in present)
    except (OSError, ValueError):
        return False


def finish_retirement(folder, backup_id, digest):
    if not stat.S_ISDIR(folder.lstat().st_mode):
        raise ValueError("invalid_retiring_directory")
    files = {item.name for item in folder.iterdir()}
    if files:
        manifest = folder / "BUNDLE.json"
        if (not stat.S_ISREG(manifest.lstat().st_mode) or
                hashlib.sha256(manifest.read_bytes()).hexdigest() != digest):
            raise ValueError("retiring_manifest_mismatch")
        allowed = {"BUNDLE.json", "SHA256SUMS", "backup.json", "verify.json", f"backup-{backup_id}.glgui.age"}
        if files - allowed:
            raise ValueError("unexpected_retiring_files")
        # Keep the authenticated manifest until all its members have gone.
        for name in sorted(files - {"BUNDLE.json"}):
            (folder / name).unlink()
        manifest.unlink()
    folder.rmdir()


def stage_or_retire(action, backup_id, expected_manifest, *, root=INPUT, incoming=INCOMING):
    if not re.fullmatch(r"[0-9a-f]{32}", backup_id) or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest):
        raise ValueError("invalid_restore_copy_identity")
    if action not in {"stage", "retire", "retire-owned"}:
        raise ValueError("invalid_restore_copy_action")
    # Both operations select immutable public files. Serialize mutations at the
    # destination and use the existing store validator before copying or deleting.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        folder = root / backup_id
        marker = root / f".mac-restore-{backup_id}-{expected_manifest}.json"
        retiring = root / f".retiring-{backup_id}-{expected_manifest}"
        receipt = {"action": action, "backup_id": backup_id, "manifest_sha256": expected_manifest, "created": False}
        if action != "stage":
            partial = os.path.lexists(retiring)
            current = retiring if partial else folder
            exists = os.path.lexists(current)
            proof = read_owner(marker, backup_id, expected_manifest)
            if action == "retire-owned":
                receipt.update(retired=False, preserved=exists)
            if not exists:
                if proof:
                    marker.unlink()
                    os.fsync(fd)
                return receipt
            if not proof or not managed_folder(current, proof, partial=partial):
                return receipt
            if not partial:
                manifest = backup_store.inspect_bundle(current)
                if (manifest["backup_operation_id"] != backup_id or
                        hashlib.sha256((current / "BUNDLE.json").read_bytes()).hexdigest() != expected_manifest):
                    raise ValueError("restore_copy_manifest_mismatch")
                backup_export._rename_noreplace(current, retiring)
                os.fsync(fd)
            finish_retirement(retiring, backup_id, expected_manifest)
            marker.unlink()
            os.fsync(fd)
            if action == "retire-owned":
                receipt.update(retired=True, preserved=os.path.lexists(folder))
            return receipt
        manifest = backup_store.inspect_bundle(incoming)
        digest = hashlib.sha256((incoming / "BUNDLE.json").read_bytes()).hexdigest()
        if manifest["backup_operation_id"] != backup_id or digest != expected_manifest:
            raise ValueError("restore_copy_manifest_mismatch")
        if os.path.lexists(retiring):
            raise ValueError("previous_restore_copy_retirement_pending")
        if not os.path.lexists(folder) and os.path.lexists(marker):
            raise ValueError("previous_restore_copy_identity_unresolved")
        result = backup_export.export_bundle(incoming, root)
        if not result["reused_identical_destination"]:
            publish_owner(folder, marker, backup_id, digest)
            os.fsync(fd)
            receipt["created"] = True
        return receipt
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "retire", "retire-owned"))
    parser.add_argument("backup_id")
    parser.add_argument("manifest_sha256")
    args = parser.parse_args()
    try:
        print(json.dumps(stage_or_retire(args.action, args.backup_id, args.manifest_sha256)))
    except (backup_export.BackupExportError, backup_store.BackupStoreError, ValueError, OSError) as exc:
        sys.exit(getattr(exc, "code", None) or type(exc).__name__)
