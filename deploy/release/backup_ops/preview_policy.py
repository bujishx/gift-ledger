"""Conservative metadata policy for application-owned, expired preview caches.

``scan`` never reads uploaded workbooks. Quarantine is an explicit POSIX-only
maintenance action: the caller must already have stopped the applications and
verified its database guard. It copies private bytes, verifies every copy, then
removes only unchanged expired pending pairs. Unknown or ambiguous work is never
deleted. Nothing in this module publishes a backup or accesses the user's
original Excel files outside the configured application preview directory.
"""

import datetime
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

FORMAT = "gift-ledger-preview-policy-v1"
QUARANTINE_FORMAT = "gift-ledger-preview-quarantine-v1"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
IMPORT_KEYS = {"version", "user_id", "expires_at", "decisions", "plan_fingerprint", "issues"}
RESTORE_KEYS = {"version", "token", "user_id", "created_at", "expires_at", "byte_length", "sha256"}
ISSUE_KEYS = {"row_number", "field", "code", "message", "severity", "raw_value", "remedy",
              "confirmation_key", "options", "existing_event_id", "existing_gift_id"}


class PreviewPolicyError(RuntimeError):
    def __init__(self, code, receipt=None):
        self.code = code
        self.receipt = receipt
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise PreviewPolicyError(code)


def _path(value):
    path = Path(value)
    _require(".." not in path.parts and "\x00" not in str(path), "unsafe-path")
    return Path(os.path.abspath(str(path)))


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns if os.name != "nt" else None)


def _node(path, directory=False, private=False, owner=None):
    try:
        info = path.lstat()
    except OSError:
        raise PreviewPolicyError("path-unavailable") from None
    _require(not stat.S_ISLNK(info.st_mode)
             and not (getattr(info, "st_file_attributes", 0) & 0x400), "unsafe-link-or-reparse")
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
             "unexpected-member-type")
    if not directory:
        _require(info.st_nlink == 1, "unsafe-hard-link")
    if private and os.name != "nt":
        _require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
                 "private-mode-required")
        if owner is not None:
            _require((info.st_uid, info.st_gid) == owner, "private-owner-mismatch")
        _require(hasattr(os, "listxattr"), "private-acl-inspection-unavailable")
        try:
            attributes = os.listxattr(str(path), follow_symlinks=False)
        except OSError:
            raise PreviewPolicyError("private-acl-inspection-unavailable") from None
        _require(not any("acl" in name.lower() for name in attributes), "private-acl-forbidden")
    return info


def _chain(path):
    return tuple((str(item), _signature(_node(item, directory=True))[:2])
                 for item in list(reversed(path.parents)) + [path])


def _owner(info):
    return (info.st_uid, info.st_gid) if os.name != "nt" else None


def _time(value):
    _require(isinstance(value, str), "metadata-time-invalid")
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise PreviewPolicyError("metadata-time-invalid") from None
    _require(parsed.tzinfo is not None and parsed.utcoffset() == datetime.timedelta(0),
             "metadata-utc-required")
    return parsed


def _now(now, ttl_minutes):
    _require(isinstance(now, datetime.datetime) and now.tzinfo is not None
             and now.utcoffset() is not None, "current-time-with-timezone-required")
    _require(type(ttl_minutes) is int and 5 <= ttl_minutes <= 120, "preview-ttl-invalid")
    return now.astimezone(datetime.timezone.utc)


def _open_source(path, signature, owner):
    _require(_signature(_node(path, private=True, owner=owner)) == signature,
             "source-changed")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    if _signature(os.fstat(descriptor)) != signature:
        os.close(descriptor)
        raise PreviewPolicyError("source-changed")
    return descriptor


def _metadata(path, signature, owner):
    _require(signature[4] <= MAX_METADATA_BYTES, "metadata-too-large")
    descriptor = _open_source(path, signature, owner)
    with os.fdopen(descriptor, "rb") as incoming:
        raw = incoming.read(MAX_METADATA_BYTES + 1)
        _require(_signature(os.fstat(incoming.fileno())) == signature, "source-changed")
    _require(len(raw) <= MAX_METADATA_BYTES
             and _signature(_node(path, private=True, owner=owner)) == signature, "source-changed")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate-metadata-key")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=lambda value: _require(False, "invalid-metadata-json"))
    except (ValueError, UnicodeError, RecursionError):
        raise PreviewPolicyError("invalid-metadata-json") from None
    _require(type(payload) is dict, "metadata-object-required")
    return payload, hashlib.sha256(raw).hexdigest()


def _validate_issue(issue):
    _require(type(issue) is dict and set(issue) == ISSUE_KEYS, "metadata-issue-schema-invalid")
    for name in ("row_number", "existing_event_id", "existing_gift_id"):
        _require(issue[name] is None or (type(issue[name]) is int and issue[name] > 0),
                 "metadata-issue-schema-invalid")
    for name in ("field", "raw_value", "confirmation_key"):
        _require(issue[name] is None or type(issue[name]) is str, "metadata-issue-schema-invalid")
    for name in ("code", "message", "remedy"):
        _require(type(issue[name]) is str, "metadata-issue-schema-invalid")
    _require(type(issue["severity"]) is str and issue["severity"] in {"error", "confirmation", "info"},
             "metadata-issue-schema-invalid")
    _require(type(issue["options"]) is list and all(
        type(option) is dict and all(type(key) is str and type(value) is str
                                    for key, value in option.items())
        for option in issue["options"]), "metadata-issue-schema-invalid")


def _validate_metadata(payload, token, kind, content_size, ttl_minutes):
    _require(set(payload) == (IMPORT_KEYS if kind == "imports" else RESTORE_KEYS),
             "metadata-schema-invalid")
    _require(type(payload["version"]) is int and payload["version"] == 1
             and type(payload["user_id"]) is int and payload["user_id"] > 0,
             "metadata-schema-invalid")
    expires = _time(payload["expires_at"])
    if kind == "imports":
        decisions, fingerprint, issues = payload["decisions"], payload["plan_fingerprint"], payload["issues"]
        _require(type(decisions) is dict and len(decisions) <= 20000
                 and all(type(key) is str and type(value) is str
                         and len(key) <= 100 and len(value) <= 100 for key, value in decisions.items()),
                 "metadata-decisions-invalid")
        _require(fingerprint is None or (type(fingerprint) is str and SHA256.fullmatch(fingerprint)),
                 "metadata-plan-invalid")
        _require(type(issues) is list, "metadata-issues-invalid")
        for issue in issues:
            _validate_issue(issue)
    else:
        created = _time(payload["created_at"])
        _require(payload["token"] == token and created <= expires
                 and datetime.timedelta(minutes=5) <= expires - created <= datetime.timedelta(minutes=120),
                 "metadata-restore-identity-invalid")
        _require(type(payload["byte_length"]) is int and payload["byte_length"] == content_size
                 and type(payload["sha256"]) is str and SHA256.fullmatch(payload["sha256"]),
                 "metadata-restore-content-invalid")
    return expires


def _report(kind, now, ttl_minutes):
    return {"format": FORMAT, "kind": kind,
            "checked_at": now.isoformat() if isinstance(now, datetime.datetime) else None,
            "ttl_minutes": ttl_minutes, "state": "clear", "blocked": False,
            "counts": {"members": 0, "expired_pending_pairs": 0, "unexpired_pending_pairs": 0,
                       "processing_members": 0, "orphan_groups": 0, "unknown_members": 0,
                       "invalid_metadata": 0, "unsafe_members": 0},
            "reasons": [], "candidate_token_hashes": [],
            "payload_content_read": False, "cryptographic_provenance_verified": False,
            "expiry_source": "manifest.expires_at", "api_ttl_independently_verified": False}


def _scan(preview_dir, now, ttl_minutes, kind):
    current = _now(now, ttl_minutes)
    _require(kind in {"imports", "personal-restores"}, "unsupported-preview-kind")
    directory = _path(preview_dir)
    report, candidates, reasons = _report(kind, current, ttl_minutes), [], set()
    _chain(directory.parent)
    if not os.path.lexists(str(directory)):
        return report, candidates, None
    chain = _chain(directory)
    directory_info = _node(directory, directory=True, private=True)
    owner = _owner(directory_info)
    entries, groups = sorted(directory.iterdir(), key=lambda item: item.name), {}
    report["counts"]["members"] = len(entries)
    suffix = ".xlsx" if kind == "imports" else ".giftledger"
    processing_suffix = ".processing" if kind == "imports" else ".claimed"
    for entry in entries:
        try:
            info = _node(entry, private=True, owner=owner)
        except PreviewPolicyError as error:
            report["counts"]["unsafe_members"] += 1
            reasons.add(error.code)
            continue
        matched = False
        for ending in (".json", suffix, processing_suffix):
            if entry.name.endswith(ending) and TOKEN.fullmatch(entry.name[:-len(ending)]):
                token = entry.name[:-len(ending)]
                groups.setdefault(token, {})[ending] = (entry, _signature(info))
                if ending == processing_suffix:
                    report["counts"]["processing_members"] += 1
                    reasons.add("processing-preview-present")
                matched = True
                break
        if not matched:
            report["counts"]["unknown_members"] += 1
            reasons.add("unknown-preview-member")
    for token, members in sorted(groups.items()):
        if processing_suffix in members:
            continue
        if set(members) != {".json", suffix}:
            report["counts"]["orphan_groups"] += 1
            reasons.add("orphan-preview-member")
            continue
        manifest_path, manifest_signature = members[".json"]
        content_path, content_signature = members[suffix]
        try:
            _require(0 < content_signature[4] <= MAX_PAYLOAD_BYTES, "preview-payload-size-invalid")
            payload, metadata_hash = _metadata(manifest_path, manifest_signature, owner)
            expires = _validate_metadata(payload, token, kind, content_signature[4], ttl_minutes)
        except PreviewPolicyError as error:
            report["counts"]["invalid_metadata"] += 1
            reasons.add(error.code)
            continue
        if expires > current:
            report["counts"]["unexpired_pending_pairs"] += 1
            reasons.add("unexpired-pending-preview")
            continue
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        report["counts"]["expired_pending_pairs"] += 1
        report["candidate_token_hashes"].append(token_hash)
        candidates.append({"token_hash": token_hash, "owner": owner,
                           "metadata": (manifest_path, manifest_signature, metadata_hash),
                           "content": (content_path, content_signature,
                                       payload.get("sha256") if kind == "personal-restores" else None)})
    _require(_chain(directory) == chain and _signature(_node(directory, directory=True, private=True))
             == _signature(directory_info), "preview-directory-changed")
    report["reasons"] = sorted(reasons)
    report["blocked"] = bool(reasons)
    report["state"] = "blocked" if reasons else ("expired_pending" if candidates else "clear")
    return report, candidates, {"directory": directory, "chain": chain,
                                 "signature": _signature(directory_info), "owner": owner,
                                 "members": {entry.name for entry in entries}}


def scan(preview_dir, now, ttl_minutes, kind="imports"):
    """Return deterministic counts/reasons and token hashes, never file contents.

    TTL is validated and recorded, but import expiry is read from the actual
    manifest: imports do not record creation time. mtime never decides expiry.
    """
    try:
        return _scan(preview_dir, now, ttl_minutes, kind)[0]
    except (PreviewPolicyError, OSError, ValueError, TypeError) as error:
        result = _report(kind, now, ttl_minutes)
        result.update({"state": "blocked", "blocked": True,
                       "reasons": [getattr(error, "code", "preview-scan-unavailable")]})
        return result


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash_private(path, owner):
    signature = _signature(_node(path, private=True, owner=owner))
    descriptor, digest, count = _open_source(path, signature, owner), hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as incoming:
        while True:
            block = incoming.read(CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
            count += len(block)
        _require(_signature(os.fstat(incoming.fileno())) == signature, "source-changed")
    _require(_signature(_node(path, private=True, owner=owner)) == signature, "source-changed")
    return {"sha256": digest.hexdigest(), "bytes": count}


def _copy_private(source, destination, signature, owner, expected_hash):
    descriptor = _open_source(source, signature, owner)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        target = os.open(str(destination), flags, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    digest, count = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as incoming:  # noqa: SIM117 -- retain Python 3.8 context syntax
        with os.fdopen(target, "wb") as outgoing:
            os.fchmod(outgoing.fileno(), 0o600)
            while True:
                block = incoming.read(CHUNK_BYTES)
                if not block:
                    break
                outgoing.write(block)
                digest.update(block)
                count += len(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            _require(_signature(os.fstat(incoming.fileno())) == signature, "source-changed")
    _require(_signature(_node(source, private=True, owner=owner)) == signature, "source-changed")
    _require(count == signature[4] and (expected_hash is None or digest.hexdigest() == expected_hash),
             "preview-copy-source-mismatch")
    result = {"sha256": digest.hexdigest(), "bytes": count}
    target_owner = _owner(_node(destination, private=True))
    _require(_hash_private(destination, target_owner) == result, "preview-copy-readback-mismatch")
    return result


def _write_receipt(directory, value, replace=False):
    path = directory / ("QUARANTINE.json.part" if replace else "QUARANTINE.json")
    payload = (json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    with path.open("xb") as outgoing:
        os.fchmod(outgoing.fileno(), 0o600)
        outgoing.write(payload)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if replace:
        _node(directory / "QUARANTINE.json", private=True)
        os.replace(str(path), str(directory / "QUARANTINE.json"))
    _fsync_directory(directory)


def quarantine_expired(preview_dir, private_session_dir, now, ttl_minutes=30, kind="imports",
                       applications_stopped=False, db_guard_verified=False, allow_preserved_work=False):
    """Explicitly preserve expired pending pairs outside backup/sync products.

    A failed action retains any private copies plus remaining originals. No
    automatic retry, restoration, recursive deletion or ambiguous cleanup occurs.
    Permissions are enforced on POSIX; Windows cannot use chmod to prove a private
    DACL and must run this action inside its isolated Linux maintenance runtime.
    """
    _require(applications_stopped is True, "applications-stopped-required")
    _require(db_guard_verified is True, "database-guard-required")
    _require(os.name != "nt", "private-quarantine-requires-posix-runtime")
    _require(type(allow_preserved_work) is bool, "preserved-work-policy-invalid")
    report, candidates, source = _scan(preview_dir, now, ttl_minutes, kind)
    preserved_snapshot = stable_snapshot(preview_dir, now, ttl_minutes, kind) if allow_preserved_work else None
    _require(not report["blocked"] or allow_preserved_work, "preview-policy-blocked")
    result = {"format": QUARANTINE_FORMAT, "kind": kind, "status": "nothing-to-quarantine",
              "recorded_at": _now(now, ttl_minutes).isoformat(), "candidate_pairs": len(candidates),
              "copied_files": 0, "removed_source_files": 0, "files": {},
              "applications_stopped_declared": True, "database_guard_declared": True,
              "private_permissions_verified": True, "readback_verified": False,
              "source_removal_completed": False, "backup_product_eligible": False}
    if preserved_snapshot is not None:
        result["preserved_pairs"] = sum(group[1] != "expired_pending" for group in preserved_snapshot.groups)
        result["preserved_files"] = len(preserved_snapshot.files) - len(candidates) * 2
    if not candidates:
        result.update({"readback_verified": True, "source_removal_completed": True})
        return result
    session, preview = _path(private_session_dir), source["directory"]
    _chain(session)
    session_info = _node(session, directory=True, private=True)
    _require(session_info.st_uid == os.geteuid(), "session-owner-mismatch")
    try:
        common = os.path.commonpath((str(session), str(preview)))
    except ValueError:
        common = None
    _require(common not in {str(session), str(preview)}, "quarantine-source-overlap")
    quarantine = session / "private-quarantine"
    _require(not os.path.lexists(str(quarantine)), "quarantine-already-exists")
    os.mkdir(str(quarantine), 0o700)
    os.chmod(str(quarantine), 0o700)
    quarantine_info = _node(quarantine, directory=True, private=True, owner=_owner(session_info))
    quarantine_chain = _chain(quarantine)
    result["status"] = "copying"
    copied = []
    try:
        _write_receipt(quarantine, result)
        for candidate in candidates:
            _require(_chain(preview) == source["chain"] and _chain(quarantine) == quarantine_chain,
                     "preview-directory-changed")
            leaf = quarantine / candidate["token_hash"]
            os.mkdir(str(leaf), 0o700)
            os.chmod(str(leaf), 0o700)
            _node(leaf, directory=True, private=True, owner=_owner(quarantine_info))
            result["files"][candidate["token_hash"]] = {}
            for label, name in (("metadata", "preview.json"),
                                ("content", "source.xlsx" if kind == "imports" else "source.giftledger")):
                original, signature, expected_hash = candidate[label]
                target = leaf / name
                summary = _copy_private(original, target, signature, candidate["owner"], expected_hash)
                result["files"][candidate["token_hash"]][label] = summary
                result["copied_files"] += 1
                copied.append((original, signature, candidate["owner"], target, summary))
            _fsync_directory(leaf)
        # A second metadata-only scan must classify exactly the same source set.
        current_report, current_candidates, current_source = _scan(preview, now, ttl_minutes, kind)
        _require(current_report == report and current_candidates == candidates and current_source == source,
                 "source-changed")
        if preserved_snapshot is not None:
            validate_snapshot(preserved_snapshot)
        for original, signature, owner, target, wanted in copied:
            _require(_signature(_node(original, private=True, owner=owner)) == signature
                     and _hash_private(original, owner) == wanted
                     and _hash_private(target, _owner(quarantine_info)) == wanted,
                     "preview-copy-readback-mismatch")
        result.update({"status": "copies-verified", "readback_verified": True})
        _write_receipt(quarantine, result, replace=True)
        remaining = set(source["members"])
        # Preserve the manifest if removing its content fails.
        for candidate in candidates:
            for label in ("content", "metadata"):
                original, signature, _expected_hash = candidate[label]
                _require(_chain(preview) == source["chain"]
                         and {item.name for item in preview.iterdir()} == remaining,
                         "source-changed")
                _require(_signature(_node(original, private=True, owner=candidate["owner"])) == signature,
                         "source-changed")
                os.unlink(str(original))
                remaining.remove(original.name)
                result["removed_source_files"] += 1
                _fsync_directory(preview)
        if preserved_snapshot is not None:
            after = stable_snapshot(preview, now, ttl_minutes, kind)
            _require(after.files == tuple(item for item in preserved_snapshot.files if item[0] in remaining)
                     and after.groups == tuple(group for group in preserved_snapshot.groups
                                               if group[1] != "expired_pending")
                     and after.owner == preserved_snapshot.owner and after.chain == preserved_snapshot.chain,
                     "preserved-preview-changed")
        result.update({"status": "quarantined", "source_removal_completed": True})
        _write_receipt(quarantine, result, replace=True)
        _fsync_directory(session)
        return result
    except (PreviewPolicyError, OSError, ValueError, TypeError) as error:
        result.update({"status": "failed", "code": getattr(error, "code", "private-quarantine-io-failed")})
        try:
            _write_receipt(quarantine, result, replace=True)
        except (OSError, PreviewPolicyError, ValueError, TypeError):
            pass
        raise PreviewPolicyError(result["code"], result) from None


def _purge_proof(session_receipt, backup_receipt, verify_receipt):
    try:
        from . import backup_store
    except ImportError:
        import backup_store
    try:
        backup, verification = backup_store._pair(backup_receipt, verify_receipt)
    except backup_store.BackupStoreError:
        raise PreviewPolicyError("purge-backup-verification-proof-invalid") from None
    session = session_receipt
    _require(type(session) is dict and session.get("format") == "gift-ledger-gui-v2-operation"
             and type(session.get("version")) is int and session["version"] == 1
             and session.get("action") == "automation-session" and session.get("status") == "completed"
             and session.get("success") is True, "purge-finished-session-required")
    details = session.get("details")
    _require(type(details) is dict and details.get("outcome") == "verified"
             and details.get("readonly_restored") is True
             and details.get("host_reported_services_restored") is True,
             "purge-safe-service-restoration-required")
    _require(type(session.get("operation_id")) is str
             and re.fullmatch(r"[0-9a-f]{32}", session["operation_id"])
             and session["operation_id"] not in {backup["operation_id"], verification["operation_id"]}
             and details.get("backup_operation_id") == backup["operation_id"]
             and details.get("verify_operation_id") == verification["operation_id"]
             and all(session.get(key) == backup[key] for key in ("project", "instance_id", "server_uuid")),
             "purge-session-proof-binding-invalid")
    started, completed = _time(session.get("started_at")), _time(session.get("completed_at"))
    _require(started <= _time(backup["started_at"]) <= _time(verification["completed_at"]) <= completed,
             "purge-session-proof-time-invalid")
    return {"session_operation_id": session["operation_id"],
            "backup_operation_id": backup["operation_id"], "verify_operation_id": verification["operation_id"],
            "session_receipt_sha256": hashlib.sha256(backup_store.canonical(session)).hexdigest(),
            "backup_receipt_sha256": hashlib.sha256(backup_store.canonical(backup)).hexdigest(),
            "verify_receipt_sha256": hashlib.sha256(backup_store.canonical(verification)).hexdigest()}, started, completed


def purge_quarantine(private_session_dir, now, *, session_receipt, backup_receipt,
                     verify_receipt, retention_days=0):
    """Purge only proven private copies after their successful backup session.

    The original session must be finished with its own successful backup/verify
    proofs and restored services. Default retention is zero days, per owner
    policy. Record time, not mtime, controls optional deferred retention. Only
    receipt-enumerated, hash-matching private files and empty child directories
    are removed. The redacted QUARANTINE.json is retained, including on retries.
    """
    _require(os.name != "nt", "private-quarantine-requires-posix-runtime")
    current = _now(now, 30)
    _require(type(retention_days) is int and 0 <= retention_days <= 365,
             "quarantine-retention-invalid")
    proof, started_at, completed_at = _purge_proof(session_receipt, backup_receipt, verify_receipt)
    _require(current >= completed_at, "purge-session-not-finished-at-current-time")
    session = _path(private_session_dir)
    kind = session.name
    _require(kind in {"imports", "personal-restores"}
             and session.parent.name == "private-quarantine-sets"
             and session.parent.parent.name == proof["session_operation_id"], "purge-session-path-mismatch")
    _chain(session)
    session_info = _node(session, directory=True, private=True)
    owner = _owner(session_info)
    _require(session_info.st_uid == os.geteuid(), "session-owner-mismatch")
    quarantine = session / "private-quarantine"
    quarantine_info = _node(quarantine, directory=True, private=True, owner=owner)
    receipt_path = quarantine / "QUARANTINE.json"
    receipt, _receipt_hash = _metadata(receipt_path, _signature(_node(receipt_path, private=True, owner=owner)), owner)
    required_keys = {"format", "kind", "status", "recorded_at", "candidate_pairs", "copied_files",
                     "removed_source_files", "files", "applications_stopped_declared", "database_guard_declared",
                     "private_permissions_verified", "readback_verified", "source_removal_completed",
                     "backup_product_eligible"}
    extra_keys = {"purge_authorization", "retention_days", "purged_files", "purged_at", "purge_error_code",
                  "preserved_pairs", "preserved_files"}
    _require(required_keys <= set(receipt) <= required_keys | extra_keys, "purge-receipt-schema-invalid")
    _require(receipt.get("format") == QUARANTINE_FORMAT and receipt.get("kind") == kind
             and type(receipt.get("status")) is str
             and receipt["status"] in {"quarantined", "purging", "purge-failed", "purged"}
             and receipt.get("readback_verified") is True and receipt.get("source_removal_completed") is True
             and receipt.get("private_permissions_verified") is True
             and receipt.get("applications_stopped_declared") is True and receipt.get("database_guard_declared") is True
             and receipt.get("backup_product_eligible") is False, "purge-quarantine-not-completed")
    recorded_at = _time(receipt.get("recorded_at"))
    _require(started_at <= recorded_at <= completed_at, "purge-quarantine-session-time-mismatch")
    _require(current >= recorded_at + datetime.timedelta(days=retention_days), "quarantine-retention-not-due")
    files = receipt.get("files")
    _require(type(files) is dict and files
             and all(type(token_hash) is str and SHA256.fullmatch(token_hash) for token_hash in files),
             "purge-quarantine-file-set-invalid")
    _require(type(receipt.get("candidate_pairs")) is int and receipt["candidate_pairs"] == len(files)
             and receipt.get("copied_files") == receipt.get("removed_source_files") == len(files) * 2,
             "purge-quarantine-counts-invalid")
    resuming = receipt["status"] in {"purging", "purge-failed", "purged"}
    if resuming:
        _require(receipt.get("purge_authorization") == proof, "purge-authorization-changed")
    else:
        _require("purge_authorization" not in receipt, "purge-authorization-unexpected")
    expected_members = {"QUARANTINE.json"} | set(files)
    actual_members = {path.name for path in quarantine.iterdir()}
    _require(actual_members <= expected_members and "QUARANTINE.json" in actual_members,
             "purge-unknown-member")
    if not resuming:
        _require(actual_members == expected_members, "purge-missing-private-copy")
    remaining = []
    content_name = "source.xlsx" if kind == "imports" else "source.giftledger"
    for token_hash, entries in sorted(files.items()):
        _require(type(entries) is dict and set(entries) == {"metadata", "content"},
                 "purge-quarantine-file-set-invalid")
        leaf = quarantine / token_hash
        if not os.path.lexists(str(leaf)):
            _require(resuming, "purge-missing-private-copy")
            continue
        _node(leaf, directory=True, private=True, owner=owner)
        expected_names = {"preview.json", content_name}
        actual_names = {path.name for path in leaf.iterdir()}
        _require(actual_names <= expected_names and (resuming or actual_names == expected_names),
                 "purge-unknown-or-missing-member")
        for label, name in (("metadata", "preview.json"), ("content", content_name)):
            wanted = entries[label]
            _require(type(wanted) is dict and set(wanted) == {"sha256", "bytes"}
                     and type(wanted["sha256"]) is str and SHA256.fullmatch(wanted["sha256"])
                     and type(wanted["bytes"]) is int and wanted["bytes"] >= 0,
                     "purge-quarantine-hash-invalid")
            if name in actual_names:
                path = leaf / name
                _require(_hash_private(path, owner) == wanted, "purge-private-copy-changed")
                remaining.append((path, _signature(_node(path, private=True, owner=owner)), wanted))
    if receipt["status"] == "purged":
        _require(actual_members == {"QUARANTINE.json"} and not remaining, "purge-completed-tree-changed")
        _require(type(receipt.get("purged_files")) is int and receipt["purged_files"] == len(files) * 2
                 and _time(receipt.get("purged_at")) >= recorded_at, "purge-completed-receipt-invalid")
        return receipt
    receipt.update({"status": "purging", "purge_authorization": proof, "retention_days": retention_days})
    _write_receipt(quarantine, receipt, replace=True)
    try:
        for path, signature, wanted in remaining:
            _require(_signature(_node(path, private=True, owner=owner)) == signature
                     and _hash_private(path, owner) == wanted, "purge-private-copy-changed")
            os.unlink(str(path))
            _fsync_directory(path.parent)
        for token_hash in sorted(files):
            leaf = quarantine / token_hash
            if os.path.lexists(str(leaf)):
                _node(leaf, directory=True, private=True, owner=owner)
                _require(not list(leaf.iterdir()), "purge-unknown-member")
                os.rmdir(str(leaf))
                _fsync_directory(quarantine)
        _require(_signature(_node(quarantine, directory=True, private=True, owner=owner))[:2]
                 == _signature(quarantine_info)[:2]
                 and {path.name for path in quarantine.iterdir()} == {"QUARANTINE.json"},
                 "purge-quarantine-tree-changed")
        receipt.update({"status": "purged", "purged_files": len(files) * 2,
                        "purged_at": current.isoformat()})
        receipt.pop("purge_error_code", None)
        _write_receipt(quarantine, receipt, replace=True)
        _fsync_directory(session)
        return receipt
    except (PreviewPolicyError, OSError, ValueError, TypeError) as error:
        receipt.update({"status": "purge-failed", "purge_error_code": getattr(error, "code", "private-purge-io-failed")})
        try:
            _write_receipt(quarantine, receipt, replace=True)
        except (OSError, PreviewPolicyError, ValueError, TypeError):
            pass
        raise PreviewPolicyError(receipt["purge_error_code"], receipt) from None


@dataclass(frozen=True, repr=False)
class PreviewSnapshot:
    """Private in-memory proof; raw member names must never be logged/exported."""

    directory: Path
    kind: str
    checked_at: datetime.datetime
    ttl_minutes: int
    exists: bool
    owner: tuple
    chain: tuple
    directory_signature: tuple
    files: tuple
    groups: tuple

    def summary(self):
        counts = {"members": len(self.files), "expired_pending_pairs": 0,
                  "unexpired_pending_pairs": 0, "processing_pairs": 0,
                  "processing_members": 0, "orphan_groups": 0, "unknown_members": 0,
                  "invalid_metadata": 0, "unsafe_members": 0}
        for _token_hash, state, _expiry in self.groups:
            counts[state + "_pairs"] += 1
        counts["processing_members"] = counts["processing_pairs"]
        state = ("expired_pending" if counts["expired_pending_pairs"] else
                 ("preserved_work" if self.groups else "clear"))
        candidates = [group[0] for group in self.groups if group[1] == "expired_pending"]
        return {"format": "gift-ledger-preview-stable-summary-v1", "kind": self.kind,
                "state": state, "blocked": False, "reasons": [],
                "checked_at": self.checked_at.isoformat(), "counts": counts,
                "candidate_token_hashes": candidates, "expired_candidate_token_hashes": candidates,
                "payload_content_read": False, "cryptographic_provenance_verified": False,
                "expiry_source": "manifest.expires_at", "api_ttl_independently_verified": False}

    def validate(self):
        return validate_snapshot(self)


def stable_snapshot(preview_dir, now, ttl_minutes=30, kind="imports"):
    """Freeze only valid complete preview groups after the caller drains work.

    Pending (expired or unexpired) and processing/claimed groups are accepted.
    A valid import claim may leave identical json+processing copies when unlink
    was temporarily locked; both must be byte-identical. No payload is opened.
    This function alone does not prove that applications or transactions stopped.
    """
    current, directory = _now(now, ttl_minutes), _path(preview_dir)
    _require(kind in {"imports", "personal-restores"}, "unsupported-preview-kind")
    parent_chain = _chain(directory.parent)
    if not os.path.lexists(str(directory)):
        return PreviewSnapshot(directory, kind, current, ttl_minutes, False, (), parent_chain, (), (), ())
    chain, info = _chain(directory), _node(directory, directory=True, private=True)
    signature, owner = _signature(info), _owner(info)
    suffix = ".xlsx" if kind == "imports" else ".giftledger"
    processing = ".processing" if kind == "imports" else ".claimed"
    groups, frozen_files = {}, {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        metadata = _node(entry, private=True, owner=owner)
        matching = [ending for ending in (".json", suffix, processing)
                    if entry.name.endswith(ending) and TOKEN.fullmatch(entry.name[:-len(ending)])]
        _require(len(matching) == 1, "unknown-preview-member")
        ending = matching[0]
        token = entry.name[:-len(ending)]
        groups.setdefault(token, {})[ending] = (entry, _signature(metadata))
        frozen_files[entry.name] = (entry.name, _signature(metadata), None)
    frozen_groups = []
    for token, members in sorted(groups.items()):
        pending_set, processing_set = {".json", suffix}, {processing, suffix}
        allowed = set(members) in (pending_set, processing_set)
        if kind == "imports" and set(members) == {".json", processing, suffix}:
            allowed = True
        _require(allowed, "orphan-or-ambiguous-preview-group")
        _content, content_signature = members[suffix]
        _require(0 < content_signature[4] <= MAX_PAYLOAD_BYTES, "preview-payload-size-invalid")
        metadata_payload, metadata_hash = None, None
        for ending in (processing, ".json"):
            if ending not in members:
                continue
            metadata_path, metadata_signature = members[ending]
            payload, digest = _metadata(metadata_path, metadata_signature, owner)
            expires = _validate_metadata(payload, token, kind, content_signature[4], ttl_minutes)
            if metadata_payload is not None:
                _require(payload == metadata_payload and digest == metadata_hash,
                         "ambiguous-import-claim-metadata")
            metadata_payload, metadata_hash = payload, digest
            frozen_files[metadata_path.name] = (metadata_path.name, metadata_signature, digest)
        state = ("processing" if processing in members else
                 ("expired_pending" if expires <= current else "unexpired_pending"))
        frozen_groups.append((hashlib.sha256(token.encode("ascii")).hexdigest(), state, expires.isoformat()))
    _require(_chain(directory) == chain
             and _signature(_node(directory, directory=True, private=True)) == signature,
             "preview-directory-changed")
    _require({entry.name for entry in directory.iterdir()} == set(frozen_files), "preview-directory-changed")
    for name, expected, _digest in frozen_files.values():
        _require(_signature(_node(directory / name, private=True, owner=owner)) == expected,
                 "source-changed")
    return PreviewSnapshot(directory, kind, current, ttl_minutes, True, owner or (), chain, signature,
                           tuple(frozen_files[name] for name in sorted(frozen_files)), tuple(frozen_groups))


def validate_snapshot(snapshot):
    """Re-read private JSON/stat only; any exact-member or metadata change fails."""
    _require(isinstance(snapshot, PreviewSnapshot), "preview-snapshot-required")
    _require(stable_snapshot(snapshot.directory, snapshot.checked_at, snapshot.ttl_minutes,
                             snapshot.kind) == snapshot, "preview-snapshot-changed")
    return True
