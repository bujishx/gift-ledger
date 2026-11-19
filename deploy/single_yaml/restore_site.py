"""Restore an original five-file bundle into one independently initialized Compose project."""

import json
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import backup_ops as ops
import backup_site as site
from restore_input import RecoveryError, RestoreInput, require


@contextmanager
def pin_restore_inputs(root):
    # restore-files serializes publication and retirement on this directory.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


class Job:
    def __init__(self, docker, profile, record):
        self.docker, self.profile, self.record = docker, profile, record
        self.op = record["operation"]
        self.name = profile["project"] + "-restore-" + self.op
        self.folder = ops.mkdir(site.STATE / "restores" / self.op)
        self.mounts = site.worker_mounts(profile, self.folder, record["identity_role"])
        self.mounts["/work/source"] = site.mount("bind", Path(profile["station"]) / "restore-input" / record["backup_id"], False)

    def owned(self, worker_id):
        item = self.docker.inspect(worker_id)
        require(item["id"] == worker_id and item["name"] == "/" + self.name and item["project"] == self.profile["project"]
                and item["session"] == self.op and item["image"] == self.profile["maintenance_image"],
                "restore_worker_identity_mismatch")
        site.check_mounts(item["mounts"], self.mounts, {"/tmp", "/var/lib/mysql"})
        return item

    def run(self, action):
        # On restart, a previous worker may have finished without its caller.
        # Its private operation journal decides whether SQL can ever be replayed.
        d, p = self.docker, self.profile
        old = d.call("ps", "-a", "--no-trunc", "--filter", "label=org.gift-ledger.backup-session=" + self.op,
                     "--filter", "label=com.docker.compose.project=" + p["project"], "--format", "{{.ID}}").splitlines()
        require(len(old) <= 1, "ambiguous_restore_worker")
        if old:
            require(not self.owned(old[0])["running"], "restore_worker_still_active")
            d.call("rm", old[0])
        args = ["create", "--name", self.name, "--label", "com.docker.compose.project=" + p["project"],
                "--label", "org.gift-ledger.backup-session=" + self.op, "--pull", "never", "--network", "none",
                "--read-only", "--memory", "768m", "--restart", "no", "--user", "0:0", "--cap-drop", "ALL",
                "--cap-add", "CHOWN", "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
                "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m",
                "--tmpfs", "/var/lib/mysql:rw,noexec,nosuid,nodev,size=16m", "--log-driver", "none"]
        for name in ("GIFTLEDGER_PROJECT", "GIFTLEDGER_SITE_IP", "GIFTLEDGER_HTTPS_PORT"):
            args.extend(["--env", name + "=" + os.environ[name]])
        for target, binding in self.mounts.items():
            args.extend(["--mount", site.mount_argument(target, binding)])
        args.extend(["--entrypoint", "/opt/venv/bin/python3.12", p["maintenance_image"],
                     "/opt/gift-ledger-install/restore_worker.py", action, self.record["backup_id"], self.op])
        identifier = d.call(*args)
        require(ops.SHA.fullmatch(identifier) and self.owned(identifier)["status"] == "created", "restore_worker_unbound")
        try:
            proof = json.loads(d.call("start", "--attach", identifier, timeout=2400))
            require(self.owned(identifier)["exit_code"] == 0, "restore_worker_aborted")
            if proof.get("ok") is not True:
                code = proof.get("code")
                error = RecoveryError(code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_-]{1,100}", code)
                                      else "restore_worker_failed")
                error.before_writes = proof.get("before_writes") is True
                raise error
            return proof["result"]
        finally:
            if not self.owned(identifier)["running"]:
                d.call("rm", identifier)


def remove_transfer_key(docker, project):
    name = project + "_restore_identity"
    existing = docker.call("volume", "ls", "--format", "{{.Name}}", "--filter", "name=" + name).splitlines()
    if name not in existing:
        return
    value = json.loads(docker.call("volume", "inspect", "--format", "{{json .}}", name))
    labels = value.get("Labels") or {}
    require(value["Name"] == name and value["Driver"] == "local" and not value.get("Options")
            and labels.get("com.docker.compose.project") == labels.get("org.gift-ledger.gui-v2-project") == project
            and labels.get("com.docker.compose.volume") == labels.get("org.gift-ledger.gui-v2-role") == "restore_identity",
            "restore_key_volume_identity_mismatch")
    require(not docker.call("ps", "-aq", "--filter", "volume=" + name), "restore_key_volume_still_in_use")
    docker.call("volume", "rm", name)


def stop_original(profile, docker, original):
    for role in ("web", "api"):
        expected = profile["containers"][role]
        current = docker.inspect(expected["id"])
        require(all(current[field] == original[role][field] for field in ("id", "image", "mounts", "project", "service")),
                "original_service_identity_changed")
        if current["running"]:
            require(original[role]["running"], "previously_stopped_service_changed")
            docker.call("stop", "--time", "30", current["id"])
        require(not docker.inspect(current["id"])["running"], "restore_application_stop_failed")


def restore_status(profile, journal, backup_id):
    # Writes use fsync + atomic replace. This read also works while the
    # original coordinator is active and never creates a lock or a key volume.
    if not journal.exists():
        return {"stage": "none"}
    record = ops.read_json(journal)
    require(record["project"] == profile["project"], "restore_status_project_changed")
    if record["backup_id"] != backup_id:
        require(record["stage"] == "rejected", "target_has_another_restore")
        return {"stage": "none"}
    for role, service in profile["containers"].items():
        require(all(service[key] == record["original"][role][key] for key in ("id", "image", "mounts")),
                "original_service_identity_changed")
    result = {key: record[key] for key in ("stage", "identity_role", "bundle_sha256", "source")}
    result["operation_id"] = record["operation"]
    if "target" in record:
        result["target"] = record["target"]
    if record["stage"] == "completed":
        result["result"] = record["result"]
        result["temporary_key_cleanup_pending"] = (record["identity_role"] == "restore_identity"
                                                   and not record.get("transfer_key_removed"))
    return result


def restore(backup_id, identity_role, *, check_only=False, cancel=False, status=False, operation=None):
    require(ops.ID.fullmatch(backup_id) and identity_role in ("backup_identity", "restore_identity"),
            "restore_arguments_invalid")
    project = os.environ["GIFTLEDGER_PROJECT"]
    site.single_yaml_site.project_host(project)
    docker = ops.Docker("/usr/bin/docker", "unix:///var/run/docker.sock")
    bindings = site.SiteBindings(docker, project, service="restore")
    profile = bindings.runtime_profile()
    journal = site.STATE / "restore.json"
    if status:
        return restore_status(profile, journal, backup_id)
    with ops.host_lock(site.STATE), pin_restore_inputs(site.RESTORE_INPUT):
        site.check_certificate_idle()
        pending = [r for _, r in ops.run_records(site.STATE)
                   if r["stage"] not in ("finished", "preflight-failed") or r.get("private_cleanup_pending")]
        require(not pending, "unfinished_backup_blocks_restore")
        record = ops.read_json(journal) if journal.exists() else None
        if operation is not None:
            require(ops.ID.fullmatch(operation) and record and record["operation"] == operation
                    and record["stage"] != "rejected", "restore_confirmation_operation_changed")
        if cancel:
            require(record is not None and record["stage"] in ("preflight", "eligible", "rejected")
                    and record["project"] == project and record["backup_id"] == backup_id
                    and record["identity_role"] == identity_role, "restore_cancellation_not_applicable")
            if identity_role == "restore_identity":
                remove_transfer_key(docker, project)
            record["stage"] = "rejected"
            ops.save_json(journal, record)
            return {"cancelled": True, "operation_id": record["operation"]}
        if record and record["stage"] == "completed":
            require(record["project"] == project and record["backup_id"] == backup_id, "target_already_restored")
            if not check_only and (identity_role == "restore_identity" or record["identity_role"] == "restore_identity"):
                remove_transfer_key(docker, project)
                if not record.get("transfer_key_removed"):
                    record["transfer_key_removed"] = True
                    ops.save_json(journal, record)
            return {**record["result"], "already_restored": True}

        try:
            selected = RestoreInput(site.RESTORE_INPUT / backup_id, backup_id)
        except Exception:
            if identity_role == "restore_identity" and (record is None or record["stage"] == "rejected"):
                remove_transfer_key(docker, project)
            raise
        if record is None or record["stage"] == "rejected":
            original = bindings.services(profile, docker)
            bindings.check_run(profile, docker, original, identity_role=identity_role)
            record = {"project": project, "operation": uuid4().hex, "backup_id": backup_id,
                      "bundle_sha256": selected.bundle_hash, "source": selected.options(),
                      "original": original, "identity_role": identity_role,
                      "stage": "preflight"}
            ops.save_json(journal, record)
        else:
            require(record["project"] == project and record["backup_id"] == backup_id
                    and record["identity_role"] == identity_role and record["bundle_sha256"] == selected.bundle_hash,
                    "unfinished_restore_has_different_inputs")

        job = Job(docker, profile, record)
        bindings.check_run(profile, docker, record["original"], identity_role=identity_role)
        if record["stage"] == "preflight":
            try:
                approval = job.run("preflight")
            except RecoveryError as error:
                if error.code != "restore_worker_still_active":
                    record["stage"] = "rejected"
                    ops.save_json(journal, record)
                    if identity_role == "restore_identity":
                        remove_transfer_key(docker, project)
                raise
            record.update(target=approval["target"], stage="eligible")
            ops.save_json(journal, record)
        elif record["stage"] == "eligible":
            job.run("preflight" if check_only else "check-target")
        if check_only:
            require(record["stage"] == "eligible", "restore_started_use_original_operation")
            return {"eligible": True, "target": record["target"], "source": selected.options(),
                    "full_decryption_verified": True, "operation_id": record["operation"]}
        if record["stage"] != "verified":
            record["stage"] = "stopping"
            ops.save_json(journal, record)
            stop_original(profile, docker, record["original"])
            record["stage"] = "applying"
            ops.save_json(journal, record)
            try:
                result = job.run("apply")
            except RecoveryError as error:
                # The worker observed this exact target's operation never started.
                # A partial/uncertain SQL import keeps its services stopped.
                if getattr(error, "before_writes", False):
                    ops.restore_services(profile, docker, record["original"])
                    record["stage"] = "rejected"
                    ops.save_json(journal, record)
                    if identity_role == "restore_identity":
                        remove_transfer_key(docker, project)
                raise
            require(result["target"] == record["target"] and result["full_database_and_app_files_verified"] is True,
                    "restored_target_unverified")
            record.update(stage="verified", result=result)
            ops.save_json(journal, record)
        else:
            require(job.run("verify") == record["result"], "previous_restore_result_changed")
        ops.restore_services(profile, docker, record["original"])
        record["stage"] = "completed"
        ops.save_json(journal, record)
        if identity_role == "restore_identity":
            remove_transfer_key(docker, project)
            record["transfer_key_removed"] = True
            ops.save_json(journal, record)
        return record["result"]


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="将选定的五件备份恢复到已初始化的独立空站")
        parser.add_argument("backup_id")
        parser.add_argument("--use-site-key", action="store_true")
        parser.add_argument("--operation", help="仅继续已核对的这一次操作")
        intention = parser.add_mutually_exclusive_group()
        intention.add_argument("--check-only", action="store_true")
        intention.add_argument("--cancel", action="store_true")
        intention.add_argument("--status", action="store_true")
        args = parser.parse_args()
        result = restore(args.backup_id, "backup_identity" if args.use_site_key else "restore_identity",
                         check_only=args.check_only, cancel=args.cancel, status=args.status, operation=args.operation)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        code = getattr(exc, "code", None)
        print("恢复任务未完成：" + (code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_-]{1,100}", code)
                                else "restore_environment_invalid"), file=sys.stderr)
        sys.exit(1)
