"""Compare saved public restore receipts; never contacts a host or reads keys.

Callers validate public input formats and pin their selected source/target files.
This check proves consistency of saved receipts, not current service health or
the owner's original-password login and account-isolation acceptance.
"""

import re


class RestoreReceiptError(ValueError):
    pass


def require(value, code):
    if not value:
        raise RestoreReceiptError(code)


def validate(before, request, request_sha256, backup, verification, completed, after):
    require(all(type(v) is dict for v in
                (before, request, backup, verification, completed, after)),
            "public_records_required")
    require(before.get("success") is True and after.get("success") is True
            and before.get("format") == after.get("format") == "gift-ledger-gui-v2-status",
            "successful_status_required")
    require(request.get("action") == "restore" and completed.get("action") == "restore"
            and completed.get("status") == "completed" and completed.get("success") is True
            and completed.get("format") == "gift-ledger-gui-v2-operation",
            "successful_restore_completion_required")
    for name in ("project", "instance_id", "server_uuid"):
        require(bool(before.get(name)) and before[name] == request.get(name)
                == completed.get(name) == after.get(name), "target_identity_mismatch")
    for name in ("assets_sha256", "layout_sha256"):
        require(bool(before.get(name)) and before[name] == completed.get(name)
                == after.get(name), "target_layout_or_assets_changed")
    require(bool(before.get("canonical_identity_sha256"))
            and before["canonical_identity_sha256"] == after.get("canonical_identity_sha256"),
            "target_configuration_identity_changed")
    require(re.fullmatch(r"[0-9a-f]{32}", str(request.get("operation_id", "")))
            and completed.get("operation_id") == request["operation_id"]
            and re.fullmatch(r"[0-9a-f]{64}", str(request_sha256))
            and completed.get("request_sha256") == request_sha256,
            "restore_operation_or_request_mismatch")
    require(before.get("schema_revision") == request.get("schema_revision")
            == after.get("schema_revision") == "schema_v2_s011", "S011_required")
    options = request.get("options", {})
    details, source = completed.get("details", {}), backup.get("details", {})
    proof = verification.get("details", {})
    require(all(type(v) is dict for v in (options, details, source, proof)), "details_required")
    require(backup.get("action") == "backup" and backup.get("status") == "completed"
            and backup.get("success") is True and verification.get("action") == "verify"
            and verification.get("status") == "completed" and verification.get("success") is True
            and proof.get("reliable_backup_verified") is True
            and proof.get("encryption_roundtrip_verified") is True
            and proof.get("backup_operation_id") == backup.get("operation_id"),
            "verified_backup_required")
    for name in ("project", "instance_id", "server_uuid"):
        require(bool(backup.get(name)) and backup[name] == verification.get(name)
                == options.get("source_" + name), "backup_source_identity_mismatch")
    require(before["instance_id"] != backup["instance_id"]
            and before["server_uuid"] != backup["server_uuid"], "independent_target_required")
    require(options.get("source_operation_id") == details.get("source_operation_id")
            == backup.get("operation_id") and bool(backup.get("operation_id")),
            "backup_operation_mismatch")
    require(type(source.get("source")) is dict and details.get("source") == source["source"],
            "restored_source_metadata_mismatch")
    require(options.get("ciphertext") == source.get("ciphertext")
            == proof.get("ciphertext") and bool(source.get("ciphertext"))
            and options.get("sha256") == source.get("ciphertext_sha256")
            == proof.get("ciphertext_sha256") and bool(source.get("ciphertext_sha256")),
            "ciphertext_proof_mismatch")
    for name in ("fingerprint", "app_files", "archive_sha256"):
        require(name in source and source[name] == proof.get(name) == details.get(name),
                "restored_backup_content_mismatch")
    require(type(source.get("fingerprint")) is dict and type(source.get("app_files")) is dict
            and after.get("fingerprint") == source["fingerprint"]
            and after.get("app_files") == source["app_files"], "post_restore_status_mismatch")
    for name in ("restored", "new_empty_initialized_schema_rebuilt",
                 "target_configuration_preserved", "readonly_restored"):
        require(details.get(name) is True, "restore_safety_flags_invalid")
    for name in ("existing_business_data_overwritten", "source_configuration_installed",
                 "applications_started"):
        require(details.get(name) is False, "restore_safety_flags_invalid")
    require(after.get("readonly") == {"read_only": False, "super_read_only": False},
            "post_restore_readonly_not_restored")
    return {"format": "gift-ledger-saved-restore-receipts-check-v1", "success": True,
            "project": before["project"], "operation_id": request["operation_id"],
            "saved_receipts_consistent": True, "live_service_health_checked": False,
            "original_account_login_checked": False, "NAS_contacted": False}
