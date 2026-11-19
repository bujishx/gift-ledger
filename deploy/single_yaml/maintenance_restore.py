"""Run the release restore engine for one operator-bound station, including when its API is offline."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pkgutil
import re
import stat
from uuid import UUID, uuid4

import backup_ops as ops
import dsm_backup
from dsm_events import adapter, io, observer, provider, target
import owner_login_code as owner
import restore_site
from restore_input import require

KEY_LABEL = "org.gift-ledger.maintenance-restore-operation"
HINT = {"none": "start_new", "rejected": "retry_preflight", "preflight": "continue_preflight",
        "eligible": "confirm_or_cancel", "stopping": "review_original_operation",
        "applying": "verify_original_commit_only", "verified": "resume_original_services",
        "completed": "read_completed_receipt"}


class Authority:
    def __init__(self, config, approved_image, *, manifest=None, key_operation=None):
        project = self.project = config["project"]
        self.manifest, self.key_operation = manifest, key_operation
        require(isinstance(approved_image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", approved_image),
                "approved_restore_image_required")
        binary = Path(owner.docker_args()[0])
        self.package = None
        if binary == Path("/var/packages/ContainerManager/target/usr/bin/docker"):
            self.package = target.Package.discover(backup_events=True)
            require(binary.resolve() == (self.package.target / "usr/bin/docker").resolve(), "restore_wrong_dsm_engine")
        else:
            require(binary == Path("/usr/bin/docker"), "restore_unknown_engine")
            for part in [binary.resolve(), *binary.resolve().parents]:
                info = part.stat()
                require(info.st_uid == 0 and not info.st_mode & 0o022, "restore_engine_not_root_owned")
        self.docker = ops.Docker(binary)
        self.host = dsm_backup.HostSite(self.docker, project, config["station"])
        self.host.service = "restore"
        self.station = self.host.station
        require(self.host.coordinator["image"] == approved_image, "approved_restore_image_mismatch")
        os.environ.update(self.host.settings)
        ip = dsm_backup.site.single_yaml_site.project_host(project)
        origin = dsm_backup.site.single_yaml_site.site_url(ip)
        self.site, _, port = owner.binding(project, config["instance"], origin)
        self.expected_web = (ip, port)
        with owner.directory(self.station) as fd:
            meta = os.fstat(fd)
            require([meta.st_dev, meta.st_ino] == config["station_inode"], "restore_station_changed")
        owner.verify_site_metadata(self.station, self.site)
        owner.store_binding(self.station, self.site, setup=False)
        control = self.station / "data/root_config/site/control"
        with owner.directory(control) as fd:
            identity, _ = owner.read_file(fd, "identity.json")
            log, _ = owner.read_file(fd, "initialize-completed.json")
        initialized = json.loads(log)
        self.server_uuid = initialized["server_uuid"]
        require(initialized["project"] == project and initialized["instance_id"] == self.site["instance_id"]
                and initialized["identity_sha256"] == hashlib.sha256(identity).hexdigest()
                and str(UUID(self.server_uuid)) == self.server_uuid and initialized["status"] == "completed",
                "restore_initialized_identity_changed")
        self.state, _ = self.host.directories()
        self.inputs = self.station / "restore-input"
        for folder, mode in ((self.state, 0o700), (self.inputs, 0o755)):
            with owner.directory(folder) as fd:
                info = os.fstat(fd)
                require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, mode)
                        and not any("acl" in x.lower() for x in os.listxattr(fd)), "restore_directory_permissions")

    def temporary_key(self):
        name = self.project + "_restore_identity"
        names = self.docker.call("volume", "ls", "--format", "{{.Name}}", "--filter", "name=" + name).splitlines()
        if name not in names:
            return None
        item, = json.loads(self.docker.call("volume", "inspect", name))
        labels = item.get("Labels") or {}
        require(item["Name"] == name and item["Driver"] == "local" and not item.get("Options")
                and labels.get("com.docker.compose.project") == labels.get("org.gift-ledger.gui-v2-project") == self.project
                and labels.get("com.docker.compose.volume") == labels.get("org.gift-ledger.gui-v2-role") == "restore_identity"
                and labels.get(KEY_LABEL) == self.key_operation, "restore_temporary_key_not_owned")
        return name

    def check_key(self):
        require(self.temporary_key() is not None, "restore_temporary_key_required")

    def release_key(self):
        if self.temporary_key() is None:
            return False
        # The coordinator lock is held. The release helper independently verifies the ordinary volume and no users.
        restore_site.remove_transfer_key(self.docker, self.project, expected_key_operation=self.key_operation)
        require(self.temporary_key() is None, "restore_temporary_key_cleanup_pending")
        return True

    def check_record(self, record, backup_id):
        if record is None:
            return
        require(record["project"] == self.project, "restore_web_job_changed")
        if record["stage"] == "rejected":
            return
        require(record["project"] == self.project and record["backup_id"] == backup_id
                and record["identity_role"] == "restore_identity" and record["bundle_sha256"] == self.manifest
                and record.get("web_key_operation") == self.key_operation, "restore_web_job_changed")
        if "target" in record:
            self.check_target(record["target"])

    def check_target(self, proof):
        require(proof["project"] == self.project and proof["instance_id"] == self.site["instance_id"]
                and proof["server_uuid"] == self.server_uuid and proof["url"] == self.site["origin"] + "/"
                and all(ops.SHA.fullmatch(proof[name]) for name in ("identity_sha256", "leaf_pem_sha256", "root_pem_sha256")),
                "restore_web_target_changed")

    def check_result(self, result, record, selected):
        self.check_target(result["target"])
        rows = selected.backup["details"]["fingerprint"]["tables"]["gift_records"]["rows"]
        require(result["target"] == record["target"] and result["source"] == selected.options()
                and result["operation_id"] == record["operation"] and result["backup_operation_id"] == record["backup_id"]
                and result["full_database_and_app_files_verified"] is True and type(result["gift_count"]) is int
                and result["gift_count"] == rows and isinstance(result["cash_amount_all_gift_records"], str)
                and re.fullmatch(r"[0-9]+\.[0-9]{2}", result["cash_amount_all_gift_records"]), "restore_web_receipt_mismatch")

    @contextlib.contextmanager
    def lifecycle(self, profile, record):
        if self.package is None:
            yield self.docker
            return
        engine = json.loads(self.docker.call("info", "--format", "{{json .ID}}"))
        require(isinstance(engine, str) and engine, "restore_engine_id_missing")
        profile = dict(profile, engine_id=engine, instance_id=self.site["instance_id"], server_uuid=self.server_uuid)
        base = dsm_backup.EventBinding(profile, self.package)
        reports = ops.mkdir(self.state / "dsm-events")
        io.checked_directory(reports)
        with ops.host_lock(reports):
            dsm_backup.reconcile_events(reports, self.package, self.docker)
            label = "gl-events-" + uuid4().hex
            folder = reports / label
            folder.mkdir(mode=0o700)
            io.checked_directory(folder)
            events = provider.EventProvider(self.docker, profile, folder, dsm_backup.EventEnvironment(self.package),
                observer, pkgutil.get_data("dsm_events", "observer.py"), base, io,
                lambda value: ops.save_json(folder / "events.json", value), run_label=label, start_allowed=lambda now: True)
            planned = adapter.BackupStopAdapter(self.docker, profile, events, lambda value: ops.save_json(folder / "actions.json", value))
            planned.record["format"] = "giftledger-restore-planned-services-v1"
            planned.bind_worker(profile, {"session_id": record["operation"], "project": self.project,
                                "instance_id": self.site["instance_id"], "original": record["original"]},
                                recovering=record["stage"] != "eligible")
            ops.save_json(reports / "active.json", {"run": label}, new=True)
            try:
                yield planned
            finally:
                require(events.retire().get("retired") is True, "restore_dsm_events_require_review")
                dsm_backup.reconcile_events(reports, self.package, self.docker)

    def finish_observing(self, driver):
        if self.package is not None and driver.prepared:
            # Exact Web/API die-event barriers were observed. Remove observers before long-running SQL.
            require(driver.provider.retire().get("retired") is True, "restore_dsm_events_require_review")

    def allow_resume(self, driver, record, reason):
        self.check_target(record["target"])
        require((reason == "verified" and record["stage"] == "verified"
                 and record["result"]["full_database_and_app_files_verified"] is True)
                or (reason == "empty" and record["stage"] == "applying" and record.get("empty_release") is True),
                "restore_safe_start_proof_missing")
        if self.package is not None:
            require(driver.binding["session_id"] == record["operation"] and driver.binding["original"] == record["original"],
                    "restore_lifecycle_operation_changed")
            driver.record["restore_release_proof"] = reason
            driver._save()
            driver.resume_authorized = True


def run(config, approved_key_image, action, backup_id, manifest_sha256=None, *, operation=None, key_operation=None):
    require(action in ("preflight", "apply", "cancel", "status") and isinstance(backup_id, str)
            and ops.ID.fullmatch(backup_id), "restore_host_action_invalid")
    if action != "status":
        require(isinstance(manifest_sha256, str) and ops.SHA.fullmatch(manifest_sha256)
                and isinstance(key_operation, str) and ops.ID.fullmatch(key_operation), "restore_web_proof_required")
    if action in ("apply", "cancel"):
        require(isinstance(operation, str) and ops.ID.fullmatch(operation), "restore_confirmed_operation_required")
    auth = Authority(config, approved_key_image, manifest=manifest_sha256, key_operation=key_operation)
    result = restore_site.run_bound(auth.host, auth.docker, auth.project, backup_id, "restore_identity", auth.state, auth.inputs,
                      check_only=action == "preflight", cancel=action == "cancel", status=action == "status", operation=operation,
                      expected_web=auth.expected_web, authority=auth)
    if action == "status":
        if "target" in result:
            auth.check_target(result["target"])
        return dict(result, next_action=HINT[result["stage"]])
    if action in ("apply", "cancel"):
        require(auth.temporary_key() is None, "restore_temporary_key_cleanup_pending")
        result = dict(result, temporary_key_removed=True)
    return result


def cleanup_temporary(config, approved_key_image, key_operation):
    require(isinstance(key_operation, str) and ops.ID.fullmatch(key_operation), "restore_web_proof_required")
    auth = Authority(config, approved_key_image, key_operation=key_operation)
    with ops.host_lock(auth.state):
        profile = auth.host.runtime_profile()
        dsm_backup.site.check_web_endpoint(auth.docker, profile["containers"]["web"]["id"], *auth.expected_web,
                                          "restore_site_port_mismatch")
        journal = auth.state / "restore.json"
        if os.path.lexists(journal):
            record = ops.read_json(journal)
            require(record["project"] == auth.project and record["stage"] in ("completed", "rejected"),
                    "restore_operation_retains_temporary_key")
        removed = auth.release_key()
        return {"removed": removed, "absent": True}
