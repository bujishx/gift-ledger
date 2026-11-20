"""Isolated, networkless helper. Its public output never contains a plaintext identity."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

import backup_key
import key_transfer as keys
from restore_input import RestoreInput

DAILY = Path("/work/identity")
CANDIDATE = Path("/work/candidate")
SOURCE = Path("/work/source")
CHECK_BLOCK = 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encrypt(public_key, data):
    keys.require(keys.RECIPIENT.fullmatch(public_key), "invalid-receive-code")
    process = subprocess.run([keys.AGE, "-r", public_key], input=data, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=15)
    keys.require(process.returncode == 0 and 0 < len(process.stdout) < 8192, "cannot-seal-response")
    return base64.b64encode(process.stdout).decode("ascii")


def receipt(folder, project, instance):
    keys.private_metadata(folder, 0o700, directory=True)
    keys.require({p.name for p in folder.iterdir()} == set(backup_key.MEMBERS), "incomplete-key-material")
    return backup_key.verify_material({n:folder / n for n in backup_key.MEMBERS}, project, instance)


def decrypt_proof(folder, operation, project, instance, server):
    selected = RestoreInput(folder, operation)
    keys.require((selected.manifest["project"], selected.manifest["instance_id"], selected.manifest["server_uuid"])
                 == (project, instance, server), "backup-belongs-to-another-site")
    digest = hashlib.sha256()
    chain = bytes(32)
    with subprocess.Popen([keys.AGE, "-d", "-i", str(DAILY / "identity.txt"), str(selected.ciphertext)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        while True:
            chunk = process.stdout.read(CHECK_BLOCK)
            if not chunk:
                break
            digest.update(chunk)
            chain = hashlib.sha256(chain + hashlib.sha256(chunk).digest()).digest()
        process.stdout.close()
        keys.require(process.wait() == 0, "selected-backup-does-not-match-daily-key")
    keys.require(digest.hexdigest() == selected.backup["details"]["archive_sha256"], "archive-digest-mismatch")
    return {"backup": operation, "filename": selected.ciphertext.name, "bytes": selected.ciphertext.stat().st_size,
            "chain": chain.hex()}


def generate(folder, project, instance, operation):
    keys.private_metadata(folder, 0o700, directory=True)
    if not list(folder.iterdir()):
        with tempfile.TemporaryDirectory(dir="/tmp") as scratch:
            key = Path(scratch) / "new-key"
            keys.command([keys.KEYGEN, "-o", str(key)])
            recipient = keys.derive_recipient(key)
            lines = [line for line in keys.read_private(key).splitlines() if line and not line.startswith(b"#")]
            keys.require(len(lines) == 1 and lines[0].startswith(b"AGE-SECRET-KEY-1"), "invalid-generated-identity")
            secret = lines[0] + b"\n"
            started = keys.json_bytes({"operation_id": operation, "success": False})
            authorization = keys.json_bytes({"action": "root-key-change", "project": project,
                                            "instance_id": instance, "operation_id": operation,
                                            "backup_recipient": recipient})
            proof = {"format":"gift-ledger-gui-key-import-completed-v1", "success":True,
                     "project":project, "instance_id":instance, "operation_id":operation, "backup_recipient":recipient,
                     "request_sha256":sha(authorization), "started_sha256":sha(started), "identity_sha256":sha(secret)}
            for name, data in zip(backup_key.MEMBERS, (secret, started, keys.json_bytes(proof))):
                keys.new_file(folder / name, data, 0o600)
            backup_key.sync_directory(folder)
    proof = receipt(folder, project, instance)
    keys.require(proof["operation_id"] == operation, "candidate-belongs-to-another-operation")
    return proof


def promote(folder, project, instance, operation, old_hashes):
    new = receipt(folder, project, instance)
    keys.require(new["operation_id"] == operation and set(old_hashes) == set(backup_key.MEMBERS),
                 "invalid-key-commit-journal")
    staging = DAILY / (".key-change-" + operation)
    keys.require({p.name for p in DAILY.iterdir()} <= set(backup_key.MEMBERS) | {staging.name},
                 "unexpected-key-volume-files")
    data = {n:keys.read_private(folder / n) for n in backup_key.MEMBERS}
    for name in backup_key.MEMBERS:
        keys.require(sha(keys.read_private(DAILY / name)) in (old_hashes[name], sha(data[name])),
                     "registered-key-changed")
    if staging.exists():
        keys.read_private(staging)
        staging.unlink()
    for name in backup_key.MEMBERS:
        if keys.read_private(DAILY / name) != data[name]:
            keys.new_file(staging, data[name], 0o600)
            os.replace(staging, DAILY / name)
            backup_key.sync_directory(DAILY)
    return {"recipient":receipt(DAILY, project, instance)["backup_recipient"]}


def perform(args):
    action, project, instance, operation = args[:4]
    keys.require_tmpfs()
    keys.require(keys.OPERATION.fullmatch(operation), "invalid-key-operation")
    os.umask(0o077)
    lock = os.open(DAILY, os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "commit":
            return promote(CANDIDATE, project, instance, operation, keys.parse_json(args[4].encode()))
        owner = receipt(DAILY, project, instance)
        old_hashes = {n:sha(keys.read_private(DAILY / n)) for n in backup_key.MEMBERS}
        mode, public = action, args[4]
        result = {"version":1, "action":mode, "project":project, "instance":instance, "operation":operation,
                  "origin":args[5], "receiver":public}
        extra = {}
        if action == "recover":
            folder = DAILY
            result["verified"] = decrypt_proof(SOURCE, args[6], project, instance, args[7])
        else:
            keys.require(action == "prepare", "unknown-key-action")
            folder = CANDIDATE
            owner = generate(folder, project, instance, operation)
            challenge = secrets.token_hex(24)
            result["challenge"] = encrypt(owner["backup_recipient"], challenge.encode("ascii"))
            extra = {"challenge_hash":sha(challenge.encode("ascii")), "old_hashes":old_hashes}
        result.update(recipient=owner["backup_recipient"], identity=keys.read_private(folder / "identity.txt").decode("ascii"))
        return {"sealed":encrypt(public, keys.json_bytes(result)), "recipient":owner["backup_recipient"], **extra}
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        print(json.dumps(perform(sys.argv[1:]), separators=(",", ":")))
    except Exception as exc:
        code = str(exc) if isinstance(exc, keys.KeyErrorSafe) else "key-helper-verification-failed"
        print(code, file=sys.stderr)
        sys.exit(1)
