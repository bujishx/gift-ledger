"""Enroll one independently held age identity into this Compose project."""

import base64
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

import key_transfer as keys

STATE = Path("/work/backup-state")
MEMBERS = ("identity.txt", "import-started.json", "identity-imported.json")
TRANSPORT = ("identity.txt", "started.json", "completed.json")


def sync_directory(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_incomplete(path, expected):
    keys.private_metadata(path, 0o700, directory=True)
    for child in path.iterdir():
        keys.require(child.name in expected, "unexpected-partial-file")
        keys.read_private(child)
    for child in path.iterdir():
        child.unlink()
    path.rmdir()


def volume(path):
    if not any(path.iterdir()):
        keys.prepare_empty_private(path)
    keys.private_metadata(path, 0o700, directory=True)


def transport_target(project, instance, tmp, restore):
    empty_identity = tmp / "empty-identity"
    empty_identity.mkdir(mode=0o700)
    if restore:
        # The transfer recipient belongs to this invocation. Its private half
        # disappears with tmpfs even while the validated recovery copy is in use.
        path = tmp / "restore-transfer"
        path.mkdir(mode=0o700)
        keys.prepare_target(project, transfer=path, identity=empty_identity, exchange=tmp)
        return path, keys.target_record(project, instance, path)

    volume(keys.TRANSFER)
    path = keys.TRANSFER / "target"
    staging = keys.TRANSFER / ".preparing"
    keys.require({p.name for p in keys.TRANSFER.iterdir()} <= {"target", ".preparing"},
                 "unexpected-transfer-volume-files")
    if staging.exists():
        remove_incomplete(staging, (*TRANSPORT, "failed.json"))
    if not path.exists():
        staging.mkdir(mode=0o700)
        keys.prepare_target(project, transfer=staging, identity=empty_identity, exchange=tmp)
        sync_directory(staging)
        staging.rename(path)
        sync_directory(keys.TRANSFER)
    return path, keys.target_record(project, instance, path)


def verify_material(paths, project, instance):
    receipt = keys.parse_json(keys.read_private(paths[MEMBERS[2]]))
    keys.validate_receipt(receipt, project, instance)
    started = keys.read_private(paths[MEMBERS[1]])
    secret = keys.read_private(paths[MEMBERS[0]])
    keys.require(keys.parse_json(started) == {"operation_id": receipt["operation_id"], "success": False}
                 and hashlib.sha256(started).hexdigest() == receipt["started_sha256"]
                 and hashlib.sha256(secret).hexdigest() == receipt["identity_sha256"]
                 and keys.derive_recipient(paths[MEMBERS[0]]) == receipt["backup_recipient"],
                 "private-copy-receipt-mismatch")
    return receipt


def committed(folder, project, instance):
    """Finish only a fully verified, previously committed set after interruption."""
    volume(folder)
    staging, pending = folder / ".writing", folder / ".pending"
    names = {p.name for p in folder.iterdir()}
    keys.require(names <= {*MEMBERS, ".writing", ".pending"}, "foreign-key-volume-content")
    if ".writing" in names:
        keys.require(names == {".writing"}, "inconsistent-private-key-staging")
        remove_incomplete(staging, MEMBERS)
    if pending.exists():
        keys.private_metadata(pending, 0o700, directory=True)
        source = {p.name for p in pending.iterdir()}
        done = {p.name for p in folder.iterdir()} - {".pending"}
        keys.require(source.isdisjoint(done) and source | done == set(MEMBERS),
                     "private-copy-journal-mismatch")
        paths = {name: (pending if name in source else folder) / name for name in MEMBERS}
        verify_material(paths, project, instance)
        for name in MEMBERS:
            if name in source:
                (pending / name).rename(folder / name)
                sync_directory(folder)
                sync_directory(pending)
        pending.rmdir()
        sync_directory(folder)
    if not any(folder.iterdir()):
        return None
    keys.require({p.name for p in folder.iterdir()} == set(MEMBERS), "incomplete-private-copy")
    return verify_material({name: folder / name for name in MEMBERS}, project, instance)


def publish(name, value):
    # The client only reads the final name after the atomic rename.
    raw = keys.json_bytes(value)
    path = keys.EXCHANGE / name
    keys.require(not path.exists() and not path.is_symlink(), "exchange-not-empty")
    temporary = path.with_suffix(".writing")
    keys.new_file(temporary, raw, 0o644)
    temporary.rename(path)


def site_state(project, instance, server_uuid, recipient):
    entry = {"project": project, "instance_id": instance, "server_uuid": server_uuid,
             "backup_recipient": recipient}
    keys.private_metadata(STATE, 0o700, directory=True)
    path = STATE / "site.json"
    if path.exists() or path.is_symlink():
        keys.require(keys.parse_json(keys.read_private(path)) == entry, "enrollment-conflicts-with-site")
        return
    # All writers for this project hold the identity-volume lock.
    tmp = STATE / ".enrolling"
    if tmp.exists() or tmp.is_symlink():
        keys.read_private(tmp)
        tmp.unlink()
    keys.new_file(tmp, keys.json_bytes(entry), 0o600)
    tmp.rename(path)
    sync_directory(STATE)


def record_desktop_filename(receipt, filename):
    """A display hint for the verified daily key, never a destination path."""
    keys.private_metadata(STATE, 0o700, directory=True)
    label = {field: receipt[field] for field in ("project", "instance_id")}
    label.update(recipient=receipt["backup_recipient"], filename=filename)
    with tempfile.NamedTemporaryFile(dir=STATE, prefix=".key-label-", delete=False) as temp:
        path = Path(temp.name)
        try:
            temp.write(keys.json_bytes(label))
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(path, STATE / "key-label.json")
            sync_directory(STATE)
        finally:
            path.unlink(missing_ok=True)


def receive(folder, project, instance, transport, target, tmp):
    limit = time.monotonic() + 180
    packet_path = keys.EXCHANGE / "packet.json"
    while not packet_path.exists():
        keys.require(time.monotonic() < limit and not (keys.EXCHANGE / "cancel").exists(),
                     "enrollment-cancelled-or-timed-out")
        time.sleep(0.25)
    packet = keys.parse_json(keys.read_public(packet_path, 32768))
    keys.require(isinstance(packet, dict) and set(packet) in ({"request", "ciphertext_b64"}, {"request", "ciphertext_b64", "filename"}),
                 "invalid-enrollment-packet")
    if "filename" in packet:
        from maintenance_enrollment_protocol import valid_filename
        keys.require(folder == keys.IDENTITY and valid_filename(packet["filename"]), "invalid-enrollment-filename")
    request = packet["request"]
    keys.validate_import_request(request, target)
    cipher = base64.b64decode(packet["ciphertext_b64"], validate=True)
    keys.require(0 < len(cipher) <= 16384 and
                 hashlib.sha256(cipher).hexdigest() == request["ciphertext_sha256"],
                 "ciphertext-integrity-mismatch")
    keys.new_file(tmp / request["ciphertext"], cipher, 0o600)
    request_file = tmp / "request.json"
    keys.new_file(request_file, keys.json_bytes(request), 0o600)
    candidate = tmp / "candidate"
    candidate.mkdir(mode=0o700)
    keys.import_key(project, transfer=transport, identity=candidate, exchange=tmp,
                    request_path=request_file)
    paths = {name: candidate / name for name in MEMBERS}
    verify_material(paths, project, instance)
    # Plaintext only enters durable storage after age and both receipts verify.
    staging = folder / ".writing"
    staging.mkdir(mode=0o700)
    for name in MEMBERS:
        keys.new_file(staging / name, keys.read_private(paths[name]), 0o600)
    sync_directory(staging)
    staging.rename(folder / ".pending")
    sync_directory(folder)
    receipt = committed(folder, project, instance)
    if "filename" in packet:
        record_desktop_filename(receipt, packet["filename"])
    return receipt


def enroll(restore):
    keys.require_tmpfs()
    os.umask(0o077)
    project = os.environ["GIFTLEDGER_PROJECT"]
    instance = keys.project_identity(project)
    expected = os.environ.get("GIFTLEDGER_EXPECTED_INSTANCE")
    keys.require(expected is None or instance == expected, "unexpected-enrollment-instance")
    from initialize import existing_state, json_file
    control = keys.ROOT / "site/control"
    _, identity_hash = json_file(control / "identity.json")
    initialized = existing_state(control, project, instance, identity_hash)
    keys.require(initialized, "site-must-be-initialized")
    keys.no_links(keys.EXCHANGE)
    keys.require(stat.S_ISDIR(keys.EXCHANGE.lstat().st_mode)
                 and stat.S_IMODE(keys.EXCHANGE.lstat().st_mode) == 0o700
                 and not any(keys.EXCHANGE.iterdir()), "private-new-exchange-required")
    folder = Path("/work/restore-identity") if restore else keys.IDENTITY
    mounts = {line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    expected = {str(folder)} if restore else {str(folder), str(keys.TRANSFER)}
    forbidden = {str(keys.IDENTITY), str(keys.TRANSFER), str(STATE)} if restore else {"/work/restore-identity"}
    keys.require(expected <= mounts and not forbidden.intersection(mounts), "unexpected-key-volume-mounts")
    lock = os.open(folder, os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(dir="/tmp") as scratch:
            tmp = Path(scratch)
            receipt = committed(folder, project, instance)
            transport, target = transport_target(project, instance, tmp, restore)
            offer = {"target": target, "registered": bool(receipt)}
            if not restore:
                offer["filename_supported"] = True
            publish("target.json", offer)
            if not receipt:
                if not restore:
                    keys.require(not (STATE / "site.json").exists(), "missing-bound-private-copy")
                receipt = receive(folder, project, instance, transport, target, tmp)
            if not restore:
                site_state(project, instance, initialized["server_uuid"], receipt["backup_recipient"])
            publish("receipt.json", receipt)
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        keys.require(sys.argv[1:] in (["enroll"], ["restore-key"]), "unknown-backup-key-operation")
        enroll(sys.argv[1] == "restore-key")
    except Exception as exc:
        code = str(exc) if isinstance(exc, keys.KeyErrorSafe) else type(exc).__name__
        sys.exit(f"备份钥匙登记未完成（{code}）。")
