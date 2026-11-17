"""Copy and read back finished encrypted backup bundles; never decrypt them.

The destination is an existing, owner-selected directory. Each successful export
publishes exactly one immutable ``<backup_operation_id>`` child. Windows, Linux
and macOS use their native no-replace rename. Audit is read-only and does not
depend on that filesystem capability. No cloud API, device
discovery, private-key access, or operating-system eject operation is performed.
"""

import argparse
import ctypes
import datetime
import errno
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

try:
    from . import backup_store
except ImportError:
    import backup_store


FORMAT = "gift-ledger-backup-copy-receipt-v1"
CHUNK_BYTES = 1024 * 1024
_BACKUP_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_MARKERS = (
    b"AGE-SECRET-KEY-",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
_MARKER_TAIL = max(len(item) for item in _PRIVATE_MARKERS) - 1


class BackupExportError(RuntimeError):
    """A safe error code; exception text never includes input file contents."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise BackupExportError(code)


def _absolute(path):
    path = Path(path)
    _require(".." not in path.parts, "path-traversal-rejected")
    _require("\x00" not in str(path), "path-invalid")
    return Path(os.path.abspath(str(path)))


def _reparse(metadata):
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino)


def _signature(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        # Windows path-stat and descriptor-stat can expose different creation/
        # change times for a just-written file. Identity, size, mtime and a full
        # streamed digest remain mandatory; POSIX additionally checks ctime.
        metadata.st_ctime_ns if os.name != "nt" else None,
    )


def _node(path, *, directory=False):
    try:
        metadata = path.lstat()
    except OSError:
        raise BackupExportError("path-unavailable") from None
    _require(not stat.S_ISLNK(metadata.st_mode) and not _reparse(metadata),
             "link-or-reparse-rejected")
    if directory:
        _require(stat.S_ISDIR(metadata.st_mode), "directory-required")
    else:
        _require(stat.S_ISREG(metadata.st_mode), "regular-file-required")
        _require(metadata.st_nlink == 1, "hard-link-rejected")
    return metadata


def _directory_chain(path):
    """Reject junctions/symlinks in every existing ancestor, not only the leaf."""
    chain = list(reversed(path.parents)) + [path]
    return tuple((str(item), _identity(_node(item, directory=True))) for item in chain)


def _check_chain(path, expected):
    _require(_directory_chain(path) == expected, "directory-changed-during-copy")


def _open_read(path, expected):
    _require(_signature(_node(path)) == expected, "file-changed-during-read")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise BackupExportError("file-unavailable") from None
    try:
        _require(_signature(os.fstat(descriptor)) == expected,
                 "file-changed-during-read")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _scan_private(block, tail):
    joined = tail + block
    _require(not any(marker in joined for marker in _PRIVATE_MARKERS),
             "plaintext-private-key-rejected")
    return joined[-_MARKER_TAIL:]


def _hash_file(path, expected=None):
    expected = expected or _signature(_node(path))
    descriptor = _open_read(path, expected)
    digest, count, tail, header = hashlib.sha256(), 0, b"", b""
    with os.fdopen(descriptor, "rb") as incoming:
        while True:
            block = incoming.read(CHUNK_BYTES)
            if not block:
                break
            tail = _scan_private(block, tail)
            if len(header) < 22:
                header += block[:22 - len(header)]
            digest.update(block)
            count += len(block)
        _require(_signature(os.fstat(incoming.fileno())) == expected,
                 "file-changed-during-read")
    _require(_signature(_node(path)) == expected, "file-changed-during-read")
    _require(count == expected[4], "file-size-changed")
    if path.name.endswith(".glgui.age"):
        _require(header == b"age-encryption.org/v1\n", "encrypted-ciphertext-header-invalid")
    return {"sha256": digest.hexdigest(), "bytes": count}


def _inspect(path):
    try:
        manifest = backup_store.inspect_bundle(path)
    except backup_store.BackupStoreError as error:
        raise BackupExportError("bundle-invalid:" + str(error.code)) from None
    except OSError:
        raise BackupExportError("bundle-unavailable") from None
    _require(isinstance(manifest, dict), "bundle-manifest-invalid")
    backup_id = manifest.get("backup_operation_id")
    _require(isinstance(backup_id, str) and _BACKUP_ID.fullmatch(backup_id),
             "backup-id-invalid")
    expected_names = {"backup-" + backup_id + ".glgui.age", "backup.json", "verify.json"}
    files = manifest.get("files")
    _require(isinstance(files, dict) and set(files) == expected_names,
             "bundle-file-set-invalid")
    _require(all(isinstance(value, str) and _HASH.fullmatch(value)
                 for value in files.values()), "bundle-file-hash-invalid")
    return manifest


def _snapshot(path):
    path = _absolute(path)
    chain = _directory_chain(path)
    directory_signature = _signature(_node(path, directory=True))
    manifest = _inspect(path)
    names = set(manifest["files"]) | {"BUNDLE.json", "SHA256SUMS"}
    _require({item.name for item in path.iterdir()} == names, "bundle-file-set-invalid")
    signatures = {name: _signature(_node(path / name)) for name in sorted(names)}
    hashes = {name: _hash_file(path / name, signatures[name]) for name in sorted(names)}
    _require(all(hashes[name]["sha256"] == value
                 for name, value in manifest["files"].items()), "bundle-digest-mismatch")
    _check_chain(path, chain)
    _require(_signature(_node(path, directory=True)) == directory_signature,
             "bundle-changed-during-read")
    _require({item.name for item in path.iterdir()} == names, "bundle-file-set-invalid")
    _require(_inspect(path) == manifest, "bundle-changed-during-read")
    return {"path": path, "manifest": manifest, "files": hashes,
            "signatures": signatures, "chain": chain}


def _receipt(snapshot, action, expected_source_verified):
    manifest = snapshot["manifest"]
    return {
        "format": FORMAT,
        "action": action,
        "project": manifest["project"],
        "instance_id": manifest["instance_id"],
        "server_uuid": manifest["server_uuid"],
        "backup_operation_id": manifest["backup_operation_id"],
        "verify_operation_id": manifest["verify_operation_id"],
        "bundle_completed_at": manifest["completed_at"],
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "files": snapshot["files"],
        "total_bytes": sum(item["bytes"] for item in snapshot["files"].values()),
        "embedded_identity_consistent": True,
        "expected_source_verified": bool(expected_source_verified),
        "readback_verified": True,
        "decryption_verified": False,
        "restoration_verified": False,
    }


def audit_copy(bundle_dir, expected_manifest=None):
    """Read every member, verify hashes and (if supplied) original public identity.

    Without an independently retained expected manifest, this establishes bundle
    consistency only. An attacker can replace a bundle and its public hashes;
    this function never claims authenticated provenance, decryption or recovery.
    """
    snapshot = _snapshot(bundle_dir)
    if expected_manifest is not None:
        _require(isinstance(expected_manifest, dict), "expected-manifest-invalid")
        _require(snapshot["manifest"] == expected_manifest, "expected-source-mismatch")
    return _receipt(snapshot, "audit", expected_manifest is not None)


def _overlap(left, right):
    left, right = os.path.normcase(str(left)), os.path.normcase(str(right))
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return False  # Different Windows drives cannot be ancestors.
    return common in {left, right}


def _fsync_directory(path):
    if os.name == "nt":
        return  # Windows os.fsync flushes each data file; directory fsync is unavailable.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source, target):
    if os.name == "nt":
        os.rename(str(source), str(target))  # Windows refuses even an empty existing directory.
        return
    library = ctypes.CDLL(None, use_errno=True)
    src, dst = os.fsencode(source), os.fsencode(target)
    if sys.platform == "darwin":
        # Darwin's renamex_np(..., RENAME_EXCL) also refuses an empty destination.
        unsupported = "destination-does-not-support-exclusive-rename"
        function = getattr(library, "renamex_np", None)
        _require(function is not None, unsupported)
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (src, dst, 0x00000004)
    elif sys.platform.startswith("linux"):
        unsupported = "atomic-publish-unsupported-use-windows-export"
        function = getattr(library, "renameat2", None)
        _require(function is not None, unsupported)
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, src, -100, dst, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise BackupExportError("atomic-publish-unsupported-use-windows-export")
    function.restype = ctypes.c_int
    if function(*args) == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, "destination-exists")
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", 95)}:
        raise BackupExportError(unsupported)
    raise BackupExportError("atomic-publish-failed")


def _copy_one(source, target, expected_signature, expected_hash, created):
    source_descriptor = _open_read(source, expected_signature)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        destination_descriptor = os.open(str(target), flags, 0o600)
    except BaseException:
        os.close(source_descriptor)
        raise
    created[target.name] = _identity(os.fstat(destination_descriptor))
    digest, count, tail = hashlib.sha256(), 0, b""
    with os.fdopen(source_descriptor, "rb") as incoming:  # noqa: SIM117 -- retain Python 3.8 context syntax
        with os.fdopen(destination_descriptor, "wb") as outgoing:
            while True:
                block = incoming.read(CHUNK_BYTES)
                if not block:
                    break
                tail = _scan_private(block, tail)
                outgoing.write(block)
                digest.update(block)
                count += len(block)
            _require(_signature(os.fstat(incoming.fileno())) == expected_signature,
                     "source-changed-during-copy")
            outgoing.flush()
            os.fsync(outgoing.fileno())
    _require(_signature(_node(source)) == expected_signature, "source-changed-during-copy")
    _require({"sha256": digest.hexdigest(), "bytes": count} == expected_hash,
             "source-changed-during-copy")


def _same_finished_copy(source, target):
    target_snapshot = _snapshot(target)
    _require(target_snapshot["manifest"] == source["manifest"]
             and target_snapshot["files"] == source["files"], "destination-conflict")
    receipt = _receipt(target_snapshot, "export", True)
    receipt.update({"copy_complete": True, "reused_identical_destination": True,
                    "requires_operating_system_safe_eject": True})
    return receipt


def _cleanup_stage(path, stage_identity, created):
    """Only remove our exact newly created inodes, never a substituted tree."""
    try:
        if _identity(_node(path, directory=True)) != stage_identity:
            return
        for name, identity in created.items():
            item = path / name
            if _identity(_node(item)) != identity:
                return
        for name in created:
            os.unlink(str(path / name))
        os.rmdir(str(path))  # Refuses if an unexpected member was added.
    except (OSError, BackupExportError):
        pass  # Incomplete staging is never accepted as a finished ID directory.


def export_bundle(bundle_dir, destination_root):
    """Publish an exact encrypted copy and return a freshly read-back receipt.

    Existing, byte-identical exports are audited without modification. Different
    or incomplete existing exports are never repaired or overwritten. Failure
    before publication leaves the source and all existing targets untouched.
    """
    source_path, destination_root = _absolute(bundle_dir), _absolute(destination_root)
    _require(not _overlap(source_path, destination_root), "source-destination-overlap")
    destination_chain = _directory_chain(destination_root)
    source = _snapshot(source_path)
    final = destination_root / source["manifest"]["backup_operation_id"]
    _require(not _overlap(source_path, final), "source-destination-overlap")
    if os.path.lexists(str(final)):
        return _same_finished_copy(source, final)
    stage = destination_root / (".export-" + final.name + "-" + uuid.uuid4().hex)
    _check_chain(destination_root, destination_chain)
    os.mkdir(str(stage), 0o700)
    stage_identity, created = _identity(_node(stage, directory=True)), {}
    published = False
    try:
        for name in sorted(source["files"]):
            _check_chain(source_path, source["chain"])
            _check_chain(destination_root, destination_chain)
            _require(_identity(_node(stage, directory=True)) == stage_identity,
                     "staging-directory-changed")
            _copy_one(source_path / name, stage / name, source["signatures"][name],
                      source["files"][name], created)
        current_source = _snapshot(source_path)
        _require(current_source["manifest"] == source["manifest"]
                 and current_source["files"] == source["files"]
                 and current_source["signatures"] == source["signatures"],
                 "source-changed-during-copy")
        staged = _snapshot(stage)
        _require(staged["manifest"] == source["manifest"]
                 and staged["files"] == source["files"], "copy-readback-mismatch")
        _fsync_directory(stage)
        _check_chain(destination_root, destination_chain)
        _require(_identity(_node(stage, directory=True)) == stage_identity,
                 "staging-directory-changed")
        try:
            _rename_noreplace(stage, final)
        except FileExistsError:
            return _same_finished_copy(source, final)
        published = True
        _fsync_directory(destination_root)
        result = _same_finished_copy(source, final)
        result["reused_identical_destination"] = False
        return result
    except BackupExportError:
        raise
    except OSError:
        raise BackupExportError("copy-io-failed") from None
    finally:
        if not published:
            _cleanup_stage(stage, stage_identity, created)


def _public_manifest(path):
    path = _absolute(path)
    _directory_chain(path.parent)
    metadata = _node(path)
    _require(metadata.st_size <= 1024 * 1024, "expected-manifest-too-large")
    descriptor = _open_read(path, _signature(metadata))
    with os.fdopen(descriptor, "rb") as incoming:
        raw = incoming.read(1024 * 1024 + 1)
    _require(len(raw) <= 1024 * 1024, "expected-manifest-too-large")
    _scan_private(raw, b"")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise BackupExportError("expected-manifest-invalid") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="成品备份离机复制和回读校验；不解密、不恢复。")
    parser.add_argument("--json", action="store_true", help="输出公开JSON结果")
    subparsers = parser.add_subparsers(dest="action", required=True)
    audit = subparsers.add_parser("audit", help="校验从云端或移动盘取回的完整文件夹")
    audit.add_argument("bundle_dir")
    audit.add_argument("--expected-manifest", help="独立保留的原BUNDLE.json；用于核对来源")
    export = subparsers.add_parser("export", help="导出到已存在的本机或移动盘目录")
    export.add_argument("bundle_dir")
    export.add_argument("destination_root")
    for command in (audit, export):
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        if arguments.action == "audit":
            expected = (_public_manifest(arguments.expected_manifest)
                        if arguments.expected_manifest else None)
            result = audit_copy(arguments.bundle_dir, expected)
        else:
            result = export_bundle(arguments.bundle_dir, arguments.destination_root)
    except (BackupExportError, OSError, ValueError, TypeError) as error:
        result = {"format": "gift-ledger-backup-copy-error-v1", "success": False,
                  "code": getattr(error, "code", "copy-input-or-io-invalid"),
                  "readback_verified": False, "decryption_verified": False,
                  "restoration_verified": False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
