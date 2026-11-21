"""Offline recovery worker. Only checked five-file inputs and one read-only age volume."""

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from backup_worker import load_maintenance
import restore_receipt_check
from restore_input import RestoreInput, require
import single_yaml_site


INPUT = Path("/work/source")
core = load_maintenance()


def target_identity(project, status):
    identity = core.json_file(core.SITE / "control/identity.json", private=True)
    tls = identity["tls"]
    require(identity["project"] == project and identity["instance_id"] == status["instance_id"]
            and tls["host"] == single_yaml_site.project_host(project), "restore_target_site_identity_mismatch")
    return {"project": project, "instance_id": status["instance_id"], "server_uuid": status["server_uuid"],
            "url": single_yaml_site.site_url(tls["host"]) + "/", "identity_sha256": status["canonical_identity_sha256"],
            "leaf_pem_sha256": core.file_sha(core.SITE / "certs/fullchain.pem"),
            "root_pem_sha256": core.file_sha(core.SITE / "certs/ca-chain.pem")}


def persist(name, raw):
    path = core.EXCHANGE / name
    if path.exists():
        require(core.read_file(path) == raw, "previous_restore_proof_changed")
    else:
        core.exclusive_bytes(path, raw)


def check_capacity(cipher_bytes, database_bytes=0, app_bytes=0):
    # The outer uncompressed ZIP and its extracted files coexist during verification.
    require(shutil.disk_usage("/tmp").free >= 2 * cipher_bytes + 8 * 1024**2, "restore_private_workspace_too_small")
    database_fs = Path("/work/mysql-storage-info")
    require(database_fs.is_file() and not database_fs.is_symlink(), "restore_database_storage_unavailable")
    margin = 32 * 1024**2
    db_needed = 2 * database_bytes
    if core.APP_DATA.stat().st_dev == database_fs.stat().st_dev:
        db_needed += app_bytes
    require(shutil.disk_usage(database_fs).free >= db_needed + margin and
            shutil.disk_usage(core.APP_DATA).free >= app_bytes + margin, "restore_destination_space_low")


def preflight(selected, project, operation, status):
    request = selected.request(status, operation)
    core.valid_request(request, project, "restore")
    context = core.local_context(project)
    core.require_tmpfs()
    cipher_bytes = core.ciphertext_path(request["options"]).stat().st_size
    check_capacity(cipher_bytes)
    # Authentication and an actual decrypt-and-read finish while the website is online.
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        try:
            from app.services.database_backup import parse_admin_backup

            manifest, extracted, _ = core.decrypted_archive(request, Path(tmp), context, extract=True)
            package = extracted / "database.gladminbackup"
            metadata = parse_admin_backup(package)
            core.stream_sql(package, metadata)
            app_size = sum(item["size"] for item in manifest["app_files"].values())
            check_capacity(0, metadata.sql_size, app_size)
        except core.GuiMaintenanceError as exc:
            if exc.code == "maintenance_subprocess_failed":
                raise core.GuiMaintenanceError("restore_original_key_cannot_decrypt_bundle") from None
            raise
    marker = target_identity(project, status)
    before = {"status": status, "target": marker, "bundle_sha256": selected.bundle_hash}
    persist("target-before.json", core.json_bytes(before) + b"\n")
    persist("request.json", core.json_bytes(request) + b"\n")
    persist("capacity.json", core.json_bytes({"cipher": cipher_bytes, "sql": metadata.sql_size, "app": app_size}) + b"\n")
    return {"target": marker, "source": selected.options(), "eligible": True}


def apply_or_verify(selected, project, operation, status, *, verify_only):
    before = core.json_file(core.EXCHANGE / "target-before.json", private=True)
    require(selected.bundle_hash == before["bundle_sha256"] and target_identity(project, status) == before["target"],
            "restore_target_or_input_changed")
    request = core.json_file(core.REQUEST, private=True)
    require(request == selected.request(before["status"], operation), "restore_request_changed")
    completed = core.CONTROL / operation / "completed.json"
    if not completed.exists():
        require(not verify_only, "restore_completion_not_proven")
        require(request == selected.request(status, operation), "restore_requires_empty_target")
    if not verify_only:
        if not completed.exists():
            capacity = core.json_file(core.EXCHANGE / "capacity.json", private=True)
            check_capacity(capacity["cipher"], capacity["sql"], capacity["app"])
        core.execute(project, "restore")
    context = core.local_context(project)
    receipt = core.public_receipt(core.completed_operation(operation, context, expected_action="restore"))
    after = core.inspect_site(project)
    marker = target_identity(project, after)
    require(before["target"] == marker, "restored_target_identity_or_tls_changed")
    restore_receipt_check.validate(before["status"], request, core.file_sha(core.REQUEST),
                                   selected.backup, selected.verification, receipt, after)
    engine = core.engine_for(context)
    try:
        with engine.connect() as connection:
            core.database_guard(connection, context)
            amount = connection.exec_driver_sql("SELECT COALESCE(SUM(amount), 0) FROM gift_records").scalar_one()
    finally:
        engine.dispose()
    return {"target": marker, "source": selected.options(), "operation_id": operation,
            "full_database_and_app_files_verified": True, "gift_count": after["fingerprint"]["tables"]["gift_records"]["rows"],
            "cash_amount_all_gift_records": format(amount, ".2f"), "backup_operation_id": selected.backup["operation_id"]}


def run(action, backup_id, operation):
    require(action in ("preflight", "check-target", "apply", "verify") and core.OPERATION.fullmatch(backup_id)
            and core.OPERATION.fullmatch(operation), "restore_worker_arguments_invalid")
    project = os.environ["GIFTLEDGER_PROJECT"]
    core.REQUEST = core.EXCHANGE / "request.json"
    selected = RestoreInput(INPUT, backup_id)
    with core.single_yaml_restore_input(selected):
        status = core.inspect_site(project)
        if action == "preflight":
            return preflight(selected, project, operation, status)
        if action == "check-target":
            if not (core.EXCHANGE / "capacity.json").exists():
                preflight(selected, project, operation, status)
            capacity = core.json_file(core.EXCHANGE / "capacity.json", private=True)
            check_capacity(capacity["cipher"], capacity["sql"], capacity["app"])
            before = core.json_file(core.EXCHANGE / "target-before.json", private=True)
            require(selected.bundle_hash == before["bundle_sha256"] and target_identity(project, status) == before["target"]
                    and selected.request(status, operation) == core.json_file(core.REQUEST, private=True),
                    "restore_target_or_input_changed")
            return {"eligible": True}
        return apply_or_verify(selected, project, operation, status, verify_only=action == "verify")


if __name__ == "__main__":
    try:
        result = {"ok": True, "result": run(*sys.argv[1:])}
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_-]{1,100}", code):
            code = "restore_worker_failed"
        before_writes = False
        if len(sys.argv) == 4 and sys.argv[1] == "apply" and core.OPERATION.fullmatch(sys.argv[3]):
            try:
                # A target already carrying a partial/failed import is never
                # released just because a new helper failed to parse its input.
                target = core.json_file(core.EXCHANGE / "target-before.json", private=True)["target"]
                context = core.local_context(os.environ["GIFTLEDGER_PROJECT"])
                core.directory(core.CONTROL, private=True)
                before_writes = (target["instance_id"] == context["identity"]["instance_id"]
                                 and target["server_uuid"] == context["server_uuid"]
                                 and not os.path.lexists(core.CONTROL / sys.argv[3])
                                 and not os.path.lexists(core.CONTROL / "active.json"))
            except Exception:
                pass
        result = {"ok": False, "code": code, "before_writes": before_writes}
    # The coordinator consumes only this bounded public result, including for rejection.
    print(json.dumps(result, ensure_ascii=False))
