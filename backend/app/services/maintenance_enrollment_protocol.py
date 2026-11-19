"""Public wire fields shared with the site's single-YAML age enrollment helper."""

import base64
import binascii
import hashlib
import json
import re

OP = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")
AGE = re.compile(r"age1[a-z0-9]{58}")
MAX_PACKET = 32768
ACTIVE = {"queued", "awaiting_packet", "verifying"}
TERMINAL = {"succeeded", "failed", "expired", "cancelled", "needs_review"}


def check(test):
    if not test:
        raise ValueError("invalid_enrollment_wire_record")


def matches(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def encode(value):
    return (
        json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unpack(raw):
    def unique(pairs):
        value = dict(pairs)
        check(len(value) == len(pairs))
        return value

    check(len(raw) <= MAX_PACKET)
    try:
        data = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid_enrollment_json") from exc
    check(isinstance(data, dict))
    return data


def check_target(target, project, instance):
    public = {"format", "project", "instance_id", "operation_id", "started_at"}
    check(
        isinstance(target, dict)
        and target.keys() == public | {"recipient", "started_sha256", "success"}
        and target["format"] == "gift-ledger-gui-key-target-v1"
        and target["success"] is True
        and (target["project"], target["instance_id"]) == (project, instance)
        and matches(OP, target["operation_id"])
        and matches(AGE, target["recipient"])
        and isinstance(target["started_at"], str)
        and re.fullmatch(r"[0-9TZ.:+\-]{19,40}", target["started_at"])
        and target["started_sha256"] == digest(encode({k: target[k] for k in public}))
    )


def check_packet(packet, operation, target):
    check(isinstance(packet, dict) and packet.keys() == {"request", "ciphertext_b64"})
    request = packet["request"]
    check(
        isinstance(request, dict)
        and request.keys()
        == {
            "format",
            "project",
            "instance_id",
            "operation_id",
            "target_recipient",
            "backup_recipient",
            "ciphertext",
            "ciphertext_sha256",
        }
        and request["format"] == "gift-ledger-gui-key-import-request-v1"
        and request["operation_id"] == operation
        and matches(OP, operation)
        and all(request[k] == target[k] for k in ("project", "instance_id"))
        and request["target_recipient"] == target["recipient"]
        and matches(AGE, request["backup_recipient"])
        and request["ciphertext"] == "key-transfer-" + operation + ".age"
        and matches(SHA, request["ciphertext_sha256"])
        and isinstance(packet["ciphertext_b64"], str)
        and len(packet["ciphertext_b64"]) <= 21848
    )
    try:
        cipher = base64.b64decode(packet["ciphertext_b64"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid_enrollment_ciphertext") from exc
    check(
        0 < len(cipher) <= 16384
        and cipher.startswith(b"age-encryption.org/v1\n")
        and digest(cipher) == request["ciphertext_sha256"]
    )
    return request


def check_receipt(receipt, target, request=None):
    check(
        isinstance(receipt, dict)
        and receipt.keys()
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
        }
        and receipt["format"] == "gift-ledger-gui-key-import-completed-v1"
        and receipt["success"] is True
        and all(receipt[k] == target[k] for k in ("project", "instance_id"))
        and matches(OP, receipt["operation_id"])
        and matches(AGE, receipt["backup_recipient"])
        and matches(SHA, receipt["request_sha256"])
        and matches(SHA, receipt["identity_sha256"])
        and receipt["started_sha256"]
        == digest(encode({"operation_id": receipt["operation_id"], "success": False}))
    )
    if request is not None:
        check(
            receipt["operation_id"] == request["operation_id"]
            and receipt["backup_recipient"] == request["backup_recipient"]
            and receipt["request_sha256"] == digest(encode(request))
        )
