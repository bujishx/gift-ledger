"""Bind one original five-file export to an independent, empty recovery site."""

from pathlib import Path

import backup_store
import restore_file_check


class RecoveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise RecoveryError(code)


class RestoreInput:
    def __init__(self, folder, operation_id):
        self.folder = Path(folder)
        self.manifest = backup_store.inspect_bundle(self.folder)
        self.bundle_hash = backup_store.digest(backup_store.canonical(self.manifest))
        self.backup, self.verification = backup_store._pair(
            backup_store._json(self.folder / "backup.json"), backup_store._json(self.folder / "verify.json")
        )
        require(self.manifest["backup_operation_id"] == operation_id == self.backup["operation_id"],
                "selected_backup_operation_mismatch")
        details = self.backup["details"]
        require(details["schema_revision"] == "schema_v2_s011", "restore_source_schema_unsupported")
        self.ciphertext = self.folder / details["ciphertext"]
        restore_file_check.check(self.ciphertext, self.folder / "backup.json", self.folder / "verify.json")
        require(backup_store.inspect_bundle(self.folder) == self.manifest, "restore_input_changed")

    def options(self):
        backup = self.backup
        return {
            "source_project": backup["project"], "source_instance_id": backup["instance_id"],
            "source_server_uuid": backup["server_uuid"], "source_operation_id": backup["operation_id"],
            "ciphertext": backup["details"]["ciphertext"], "sha256": backup["details"]["ciphertext_sha256"],
        }

    def check_archive(self, archive, digest):
        details = self.backup["details"]
        require(digest == details["archive_sha256"] and archive["operation_id"] == self.backup["operation_id"]
                and archive["source"] == details["source"] and archive["fingerprint"] == details["fingerprint"]
                and archive["app_files"] == details["app_files"], "restore_decrypted_source_mismatch")

    def request(self, target, operation_id):
        require(target["instance_id"] != self.backup["instance_id"] and target["server_uuid"] != self.backup["server_uuid"],
                "restore_requires_independent_site")
        require(target["success"] is True and target["schema_revision"] == "schema_v2_s011"
                and target["readonly"] == {"read_only": False, "super_read_only": False}
                and all(target["fingerprint"]["tables"][t]["rows"] == 0 for t in
                        ("users", "contacts", "event_records", "gift_records", "legacy_import_batches"))
                and not target["app_files"], "restore_requires_empty_target")
        return {"format": "gift-ledger-gui-v2-request", "version": 1, "action": "restore",
                "operation_id": operation_id, "project": target["project"], "instance_id": target["instance_id"],
                "server_uuid": target["server_uuid"], "schema_revision": target["schema_revision"], "options": self.options()}
