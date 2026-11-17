"""Import an independently held backup key through encrypted GUI uploads only.

Shared DSM directories contain public target records and age ciphertext. The
transfer key and imported backup identity stay in separate private Docker volumes.
No terminal password, secret environment value, network or Docker socket is used.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/work/site")
TRANSFER = Path("/work/key-transfer")
IDENTITY = Path("/work/identity")
EXCHANGE = Path("/work/exchange")
REQUEST = Path("/work/request/request.json")
AGE = "/usr/local/bin/age"
KEYGEN = "/usr/local/bin/age-keygen"
TARGET_FORMAT = "gift-ledger-gui-key-target-v1"
REQUEST_FORMAT = "gift-ledger-gui-key-import-request-v1"
PAYLOAD_FORMAT = "gift-ledger-gui-key-transfer-payload-v1"
PROJECT = re.compile(
    r"^(gift-ledger-nas-prod|gift-ledger-nas-restore-check|gift-ledger-dsm9-demo-20261026|gift-ledger-dsm9-restore-20261026|gift-ledger-gui-test-[0-9a-f]{16})$"
)
RECIPIENT = re.compile(r"^age1[0-9a-z]{58}$")
OPERATION = re.compile(r"^[0-9a-f]{32}$")


class KeyErrorSafe(RuntimeError):
    """Only fixed nonsecret codes are ever printed."""


def require(condition, code):
    if not condition:
        raise KeyErrorSafe(code)


def json_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def unique_object(pairs):
    output = {}
    for name, value in pairs:
        require(name not in output, "duplicate-json-key")
        output[name] = value
    return output


def parse_json(raw):
    require(len(raw) <= 32768, "json-too-large")
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise KeyErrorSafe("invalid-json") from exc


def no_links(path):
    for current in (path, *path.parents):
        require(not current.is_symlink(), "symlink-refused")


def no_acl(path):
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise KeyErrorSafe("acl-inspection-failed") from exc
    require(
        not any("acl" in name.lower() for name in names), "private-volume-acl-refused"
    )


def private_metadata(path, mode, *, directory=False):
    no_links(path)
    entry = path.lstat()
    require(
        stat.S_ISDIR(entry.st_mode) if directory else stat.S_ISREG(entry.st_mode),
        "private-type-invalid",
    )
    require(directory or entry.st_nlink == 1, "private-hardlink-refused")
    require((entry.st_uid, entry.st_gid) == (0, 0), "private-owner-invalid")
    require(stat.S_IMODE(entry.st_mode) == mode, "private-mode-invalid")
    no_acl(path)


def read_private(path, mode=0o600):
    private_metadata(path, mode)
    with path.open("rb") as source:
        raw = source.read(32769)
    require(len(raw) <= 32768, "private-input-too-large")
    return raw


def read_public(path, maximum=4 * 1024 * 1024):
    # ACL is intentionally allowed for PUBLIC metadata and ciphertext only.
    no_links(path)
    entry = path.lstat()
    require(stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1, "public-type-invalid")
    require(entry.st_size <= maximum, "public-input-too-large")
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    require(len(raw) <= maximum, "public-input-too-large")
    return raw


def new_file(path, raw, mode):
    no_links(path.parent)
    with path.open("xb") as destination:
        os.fchmod(destination.fileno(), mode)
        destination.write(raw)
        destination.flush()
        os.fsync(destination.fileno())


def prepare_empty_private(path):
    no_links(path)
    require(path.is_dir() and not any(path.iterdir()), "private-volume-not-empty")
    no_acl(path)
    os.chown(path, 0, 0)
    os.chmod(path, 0o700)
    private_metadata(path, 0o700, directory=True)


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, timeout=60, check=False)
    require(result.returncode == 0, "age-command-failed")
    return result.stdout


def derive_recipient(identity):
    read_private(identity)
    raw = command([KEYGEN, "-y", str(identity)]).decode("ascii").strip()
    require(RECIPIENT.fullmatch(raw), "age-recipient-invalid")
    return raw


def project_identity(project, root=ROOT):
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        from single_yaml_site import project_host

        project_host(project)
    elif ((Path(__file__).absolute().parent / "PORTABLE-DSM.json").exists()
            or isinstance(project, str) and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", project)):
        path = Path(__file__).absolute().parent / "portable_dsm_profile.py"
        spec = importlib.util.spec_from_file_location("key_transfer_portable_profile", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.load(project)
    elif isinstance(project, str) and re.fullmatch(r"gift-ledger-site-[0-9a-f]{16}", project):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "operations_v3"))
        sys.path.insert(0, "/opt/gift-ledger-ops")
        from contract_bridge import authorize_target

        try:
            authorize_target(project)
        except (OSError, ValueError):
            require(False, "generated-target-contract-invalid")
    else:
        require(PROJECT.fullmatch(project), "project-not-allowed")
    require(
        os.geteuid() == 0 and sys.version_info[:2] == (3, 12),
        "fixed-root-runtime-required",
    )
    sys.path.insert(0, "/opt/gift-ledger-gui")
    from prepare import verify_prepared

    verified = verify_prepared(root / "site", project)
    return verified["instance_id"]


def target_record(project, instance, transfer=TRANSFER):
    private_metadata(transfer, 0o700, directory=True)
    require(
        {p.name for p in transfer.iterdir()}
        == {"identity.txt", "started.json", "completed.json"},
        "transfer-incomplete-or-unknown",
    )
    started = parse_json(read_private(transfer / "started.json"))
    completed = parse_json(read_private(transfer / "completed.json"))
    require(
        set(completed)
        == {
            "format",
            "project",
            "instance_id",
            "operation_id",
            "started_at",
            "recipient",
            "started_sha256",
            "success",
        },
        "transfer-record-shape-invalid",
    )
    require(
        completed.get("format") == TARGET_FORMAT
        and completed.get("project") == project
        and completed.get("instance_id") == instance
        and completed.get("success") is True,
        "transfer-identity-mismatch",
    )
    require(
        OPERATION.fullmatch(completed.get("operation_id", "")),
        "transfer-operation-invalid",
    )
    require(
        completed.get("started_sha256")
        == hashlib.sha256(json_bytes(started)).hexdigest(),
        "transfer-started-mismatch",
    )
    require(
        started
        == {
            "format": TARGET_FORMAT,
            "project": project,
            "instance_id": instance,
            "operation_id": completed["operation_id"],
            "started_at": completed["started_at"],
        },
        "transfer-started-invalid",
    )
    require(
        completed.get("recipient") == derive_recipient(transfer / "identity.txt"),
        "transfer-key-mismatch",
    )
    return completed


def prepare_target(
    project, *, root=ROOT, transfer=TRANSFER, identity=IDENTITY, exchange=EXCHANGE
):
    instance = project_identity(project, root)
    if any(transfer.iterdir()):
        target = target_record(project, instance, transfer)
        private_metadata(identity, 0o700, directory=True)
        members = {p.name for p in identity.iterdir()}
        require(
            not members
            or members
            == {"identity.txt", "identity-imported.json", "import-started.json"},
            "backup-identity-incomplete-or-unknown",
        )
        if members:
            receipt = parse_json(read_private(identity / "identity-imported.json"))
            validate_receipt(receipt, project, instance)
            require(
                receipt["started_sha256"]
                == hashlib.sha256(
                    read_private(identity / "import-started.json")
                ).hexdigest()
                and receipt["identity_sha256"]
                == hashlib.sha256(read_private(identity / "identity.txt")).hexdigest()
                and receipt["backup_recipient"]
                == derive_recipient(identity / "identity.txt"),
                "backup-identity-record-mismatch",
            )
        return {"status": "key-target-readonly-verified", **target}
    prepare_empty_private(transfer)
    prepare_empty_private(identity)
    operation = uuid.uuid4().hex
    started = {
        "format": TARGET_FORMAT,
        "project": project,
        "instance_id": instance,
        "operation_id": operation,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    new_file(transfer / "started.json", json_bytes(started), 0o600)
    try:
        command([KEYGEN, "-o", str(transfer / "identity.txt")])
        os.chmod(transfer / "identity.txt", 0o600)
        read_private(transfer / "identity.txt")
        recipient = derive_recipient(transfer / "identity.txt")
        target = {
            **started,
            "success": True,
            "recipient": recipient,
            "started_sha256": hashlib.sha256(json_bytes(started)).hexdigest(),
        }
        new_file(transfer / "completed.json", json_bytes(target), 0o600)
        new_file(
            exchange / ("key-target-" + instance + ".json"), json_bytes(target), 0o644
        )
        return {"status": "key-target-prepared", **target}
    except BaseException:
        # A durable started marker makes every incomplete preparation refuse.
        if not (transfer / "failed.json").exists():
            new_file(
                transfer / "failed.json",
                json_bytes(
                    {
                        "success": False,
                        "error_code": "key-target-failed",
                        "operation_id": operation,
                    }
                ),
                0o600,
            )
        raise


def validate_import_request(request, target):
    require(
        set(request)
        == {
            "format",
            "operation_id",
            "project",
            "instance_id",
            "target_recipient",
            "ciphertext",
            "ciphertext_sha256",
            "backup_recipient",
        },
        "key-request-shape-invalid",
    )
    require(
        request["format"] == REQUEST_FORMAT
        and OPERATION.fullmatch(request["operation_id"]),
        "key-request-format-invalid",
    )
    require(
        all(
            request[name] == target[other]
            for name, other in [
                ("project", "project"),
                ("instance_id", "instance_id"),
                ("target_recipient", "recipient"),
            ]
        ),
        "key-request-target-mismatch",
    )
    require(
        RECIPIENT.fullmatch(request["backup_recipient"]), "backup-recipient-invalid"
    )
    filename = request["ciphertext"]
    require(
        isinstance(filename, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,119}\.age", filename)
        and ".." not in filename,
        "ciphertext-name-invalid",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", request["ciphertext_sha256"]),
        "ciphertext-sha-invalid",
    )


def validate_payload(payload, request):
    require(
        set(payload)
        == {
            "format",
            "operation_id",
            "project",
            "instance_id",
            "target_recipient",
            "backup_recipient",
            "identity",
        },
        "key-payload-shape-invalid",
    )
    require(payload["format"] == PAYLOAD_FORMAT, "key-payload-format-invalid")
    require(
        all(
            payload[name] == request[name]
            for name in (
                "operation_id",
                "project",
                "instance_id",
                "target_recipient",
                "backup_recipient",
            )
        ),
        "key-payload-target-mismatch",
    )
    value = payload["identity"]
    require(
        isinstance(value, str)
        and re.fullmatch(r"AGE-SECRET-KEY-1[A-Z0-9]{40,100}\n", value),
        "backup-identity-format-invalid",
    )
    return value.encode("ascii")


def validate_receipt(receipt, project, instance):
    require(
        set(receipt)
        == {
            "format",
            "success",
            "project",
            "instance_id",
            "operation_id",
            "backup_recipient",
            "request_sha256",
            "started_sha256",
            "identity_sha256",
        },
        "key-receipt-shape-invalid",
    )
    require(
        receipt["format"] == "gift-ledger-gui-key-import-completed-v1"
        and receipt["success"] is True
        and receipt["project"] == project
        and receipt["instance_id"] == instance
        and OPERATION.fullmatch(receipt["operation_id"])
        and RECIPIENT.fullmatch(receipt["backup_recipient"])
        and all(
            re.fullmatch(r"[0-9a-f]{64}", receipt[k])
            for k in ("request_sha256", "started_sha256", "identity_sha256")
        ),
        "key-receipt-identity-invalid",
    )


def require_tmpfs():
    lines = Path("/proc/self/mountinfo").read_text().splitlines()
    require(
        any(
            line.split()[4] == "/tmp" and line.split(" - ", 1)[1].split()[0] == "tmpfs"
            for line in lines
            if " - " in line
        ),
        "key-plaintext-tmpfs-required",
    )


def import_key(
    project,
    *,
    root=ROOT,
    transfer=TRANSFER,
    identity=IDENTITY,
    exchange=EXCHANGE,
    request_path=REQUEST,
):
    instance = project_identity(project, root)
    target = target_record(project, instance, transfer)
    request = parse_json(read_public(request_path, 32768))
    validate_import_request(request, target)
    private_metadata(identity, 0o700, directory=True)
    receipt_path = identity / "identity-imported.json"
    if any(identity.iterdir()):
        require(
            {p.name for p in identity.iterdir()}
            == {"identity.txt", "identity-imported.json", "import-started.json"},
            "backup-identity-incomplete-or-unknown",
        )
        receipt = parse_json(read_private(receipt_path))
        validate_receipt(receipt, project, instance)
        require(
            receipt.get("request_sha256")
            == hashlib.sha256(json_bytes(request)).hexdigest()
            and receipt.get("success") is True,
            "backup-identity-already-bound",
        )
        require(
            receipt.get("started_sha256")
            == hashlib.sha256(
                read_private(identity / "import-started.json")
            ).hexdigest(),
            "import-started-mismatch",
        )
        require(
            receipt.get("identity_sha256")
            == hashlib.sha256(read_private(identity / "identity.txt")).hexdigest(),
            "import-identity-sha-mismatch",
        )
        require(
            derive_recipient(identity / "identity.txt") == request["backup_recipient"],
            "imported-key-mismatch",
        )
        require(
            parse_json(
                read_public(
                    exchange / ("key-import-" + request["operation_id"] + ".json")
                )
            )
            == receipt,
            "import-public-completion-missing",
        )
        return {"status": "key-import-readonly-verified", **receipt}
    ciphertext = read_public(exchange / request["ciphertext"])
    require(
        hashlib.sha256(ciphertext).hexdigest() == request["ciphertext_sha256"],
        "ciphertext-integrity-mismatch",
    )
    require_tmpfs()
    # Claim before decryption. Any failure leaves a permanent incomplete identity
    # marker, preventing replacement or silent retries against the same volume.
    started_path = identity / "import-started.json"
    new_file(
        started_path,
        json_bytes({"operation_id": request["operation_id"], "success": False}),
        0o600,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="gui-key-", dir="/tmp") as temporary:
            folder = Path(temporary)
            encrypted = folder / "transfer.age"
            plaintext = folder / "transfer.json"
            candidate = folder / "identity.txt"
            new_file(encrypted, ciphertext, 0o600)
            command(
                [
                    AGE,
                    "--decrypt",
                    "--identity",
                    str(transfer / "identity.txt"),
                    "--output",
                    str(plaintext),
                    str(encrypted),
                ]
            )
            # age creates its output using the utility's restrictive umask.
            os.chmod(plaintext, 0o600)
            payload = parse_json(read_private(plaintext))
            secret = validate_payload(payload, request)
            new_file(candidate, secret, 0o600)
            require(
                derive_recipient(candidate) == request["backup_recipient"],
                "backup-key-recipient-mismatch",
            )
            new_file(identity / "identity.txt", secret, 0o600)
            receipt = {
                "format": "gift-ledger-gui-key-import-completed-v1",
                "success": True,
                "project": project,
                "instance_id": instance,
                "operation_id": request["operation_id"],
                "backup_recipient": request["backup_recipient"],
                "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest(),
                "started_sha256": hashlib.sha256(
                    read_private(started_path)
                ).hexdigest(),
                "identity_sha256": hashlib.sha256(secret).hexdigest(),
            }
            new_file(receipt_path, json_bytes(receipt), 0o600)
        # Publish completion last. Failed publication leaves a permanent failure
        # alongside the started marker, so private partial results cannot re-enter.
        new_file(
            exchange / ("key-import-" + request["operation_id"] + ".json"),
            json_bytes(receipt),
            0o644,
        )
    except BaseException:
        if not (identity / "import-failed.json").exists():
            new_file(
                identity / "import-failed.json",
                json_bytes(
                    {
                        "success": False,
                        "operation_id": request["operation_id"],
                        "error_code": "key-import-failed",
                    }
                ),
                0o600,
            )
        raise
    return {"status": "key-import-completed", **receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--action", choices=("prepare", "import"), required=True)
    args = parser.parse_args()
    try:
        result = (
            prepare_target(args.project)
            if args.action == "prepare"
            else import_key(args.project)
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except BaseException as exc:  # noqa: BLE001 -- never print secret-bearing driver errors
        code = (
            str(exc)
            if isinstance(exc, KeyErrorSafe)
            else "key-operation-failed-preserve-evidence"
        )
        print(
            json.dumps({"success": False, "error_code": code}),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
