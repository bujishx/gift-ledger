"""One-off host coordinator for a single-YAML site. No private key mounts."""

import csv
import io
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path
from uuid import UUID

import backup_ops as ops
import single_yaml_site


STATE = Path("/work/backup-state")
STORE = Path("/work/backups")
RESTORE_INPUT = Path("/work/restore-input")
STATE_NAME = ".giftledger-backup-state"
STORE_NAME = "backups"
def release_images(path, *, raw=None):
    # Different engines report the config, manifest or index of the same image.
    values = json.loads(path.read_bytes() if raw is None else raw)
    ops.require(isinstance(values, dict) and set(values) == {"api", "db", "web"},
                "release_image_roles_invalid")
    result = {}
    used = set()
    for role, ids in values.items():
        ops.require(isinstance(ids, list) and ids and
                    all(isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in ids),
                    "release_image_digest_invalid")
        result[role] = frozenset(ids)
        ops.require(used.isdisjoint(result[role]), "release_image_role_overlap")
        used.update(result[role])
    return result


_images = Path(__file__).with_name("service_images.json")
SERVICE_IMAGES = release_images(_images, raw=__loader__.get_data(str(_images)))
TEMPORARY = {
    "web": {"/tmp", "/var/cache/nginx", "/var/run"},
    "api": {"/tmp", "/var/lib/mysql"},
    "db": set(),
}


def site_directories():
    """Only the prepare job gets the owner-selected station directory."""
    station = single_yaml_site.DATA.parent
    for name, mode in ((STATE_NAME, 0o700), (STORE_NAME, 0o755), ("restore-input", 0o755)):
        path = station / name
        if not os.path.lexists(path):
            single_yaml_site.create_private_directory(path)
            os.chown(path, 0, 0)
            path.chmod(mode)
        info = path.lstat()
        ops.require(stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_gid) == (0, 0)
                    and stat.S_IMODE(info.st_mode) == mode and not any(
                        "acl" in key.lower() for key in os.listxattr(path, follow_symlinks=False)),
                    "backup_directory_permission_mismatch")


def check_certificate_idle(state_dir=None):
    journal = (STATE if state_dir is None else Path(state_dir)) / "certificate.json"
    if os.path.lexists(journal):
        ops.no_links(journal)
        ops.require(ops.read_json(journal).get("stage") in {"completed", "rolled-back"},
                    "unfinished_certificate_renewal")


def mount(kind, source, writable):
    return kind, str(source), writable


def check_mounts(found, expected, temporary=()):
    actual = {}
    for item in found:
        target, kind = item["Destination"], item["Type"]
        ops.require(target not in actual, "duplicate_mount_target")
        if kind == "tmpfs":
            ops.require(target in temporary, "unexpected_tmpfs_mount")
            continue
        source = item.get("Source") if kind == "bind" else item.get("Name")
        actual[target] = mount(kind, source, item.get("RW"))
    ops.require(actual == expected, "single_yaml_mount_identity_mismatch")


def inspect_service(docker, project, role):
    ids = docker.call("ps", "-a", "--no-trunc",
                      "--filter", "label=com.docker.compose.project=" + project,
                      "--filter", "label=com.docker.compose.service=" + role,
                      "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}").splitlines()
    ops.require(len(ids) == 1 and ops.SHA.fullmatch(ids[0]), "compose_service_missing_or_ambiguous")
    found = docker.inspect(ids[0])
    ops.require(found["id"] == ids[0] and found["project"] == project and found["service"] == role,
                "compose_service_identity_mismatch")
    ops.require(found["image"] in SERVICE_IMAGES[role], "service_release_image_mismatch")
    return found


def mount_argument(target, binding):
    kind, source, writable = binding
    items = ["type=" + kind, "src=" + source, "dst=" + target]
    if kind == "volume":
        items.append("volume-nocopy")
    if not writable:
        items.append("readonly")
    stream = io.StringIO()
    csv.writer(stream, lineterminator="").writerow(items)
    return stream.getvalue()


def worker_mounts(profile, run_dir, identity_role):
    ops.require(identity_role in ("backup_identity", "restore_identity"), "unexpected_recovery_key_role")
    station = Path(profile["station"])
    project = profile["project"]
    exchange = ops.mkdir(run_dir / "exchange").relative_to(Path(profile.get("state_dir", STATE)))
    mounts = {"/work/exchange": mount("bind", station / STATE_NAME / exchange, True)}
    for role, target in ops.ROLES.items():
        if role in {"mysql_socket", "backup_identity"}:
            name = role if role == "mysql_socket" else identity_role
            mounts[target] = mount("volume", project + "_" + name, role == "mysql_socket")
        else:
            mounts[target] = mount("bind", station / "data" / role, role in {"root_config", "app_data"})
    return mounts


def previous_worker(profile, docker, journal):
    # Creation can precede the atomic journal update recording the returned ID.
    session = journal["session_id"]
    ids = docker.call("ps", "-a", "--no-trunc",
                      "--filter", "label=org.gift-ledger.backup-session=" + session,
                      "--filter", "label=com.docker.compose.project=" + profile["project"],
                      "--format", "{{.ID}}").splitlines()
    ops.require(len(ids) <= 1, "ambiguous_previous_worker")
    if ids:
        found = docker.inspect(ids[0])
        ops.require(ops.SHA.fullmatch(ids[0]) and (not journal.get("worker_id") or journal["worker_id"] == ids[0])
                    and found["id"] == ids[0] and found["image"] == profile["maintenance_image"]
                    and found["session"] == session and found["project"] == profile["project"]
                    and found["name"] == "/" + profile["project"] + "-backup-ops-" + session,
                    "previous_worker_identity_mismatch")
        return ids[0]


class Worker(ops.Worker):
    """Reuse the tested session/attach/recovery protocol with actual bind paths."""

    def __init__(self, profile, docker, journal, run_dir, *, recover=False):
        if recover:
            bound = previous_worker(profile, docker, journal)
            if bound:
                journal["worker_id"] = bound
        super().__init__(profile, docker, journal, run_dir, recover=recover)

    def _mount_arguments(self, run_dir):
        self.expected = worker_mounts(self.profile, run_dir, "backup_identity")
        args = []
        for target, binding in self.expected.items():
            args.extend(["--mount", mount_argument(target, binding)])
        for name in ("GIFTLEDGER_PROJECT", "GIFTLEDGER_SITE_IP", "GIFTLEDGER_HTTPS_PORT"):
            args.extend(["--env", name + "=" + os.environ[name]])
        return args

    def _check_created(self):
        found = self.docker.inspect(self.container_id)
        ops.require(found["id"] == self.container_id and found["status"] == "created"
                    and found["project"] == self.profile["project"] and found["session"] == self.journal["session_id"]
                    and found["image"] == self.profile["maintenance_image"], "worker_identity_mismatch")
        try:
            check_mounts(found["mounts"], self.expected, {"/tmp", "/var/lib/mysql"})
        except ops.BackupOpsError:
            # The ID above is exactly the newly created, never-started worker.
            self.docker.call("rm", self.container_id)
            raise


class SiteBindings:
    def __init__(self, docker, project, *, service="backup"):
        self.docker, self.project, self.service = docker, project, service
        self.coordinator = docker.inspect(socket.gethostname())
        ops.require(service in ("backup", "restore", "certificate") and self.coordinator["service"] == service and self.coordinator["project"] == project
                    and self.coordinator["running"], "backup_coordinator_identity_mismatch")
        binds = {item["Destination"]: item["Source"] for item in self.coordinator["mounts"] if item["Type"] == "bind"}
        self.station = Path(binds[str(STATE)]).parent
        ops.require(self.station.is_absolute() and self.station != Path("/") and ".." not in self.station.parts,
                    "station_directory_invalid")
        socket_source = "/var/run/docker.sock"
        # Docker Desktop can present its exact broker source even when Compose
        # requested /var/run/docker.sock. Ordinary Linux daemons have no alias.
        if binds.get(socket_source) == "/run/host-services/docker.proxy.sock":
            ops.require(docker.call("info", "--format", "{{.OperatingSystem}}") == "Docker Desktop",
                        "unexpected_docker_socket_proxy")
            socket_source = "/run/host-services/docker.proxy.sock"
        expected = {
            "/var/run/docker.sock": mount("bind", socket_source, True),
            str(STATE): mount("bind", self.station / STATE_NAME, True),
        }
        if service == "restore":
            expected[str(RESTORE_INPUT)] = mount("bind", self.station / "restore-input", False)
        elif service == "backup":
            expected[str(STORE)] = mount("bind", self.station / STORE_NAME, True)
        check_mounts(self.coordinator["mounts"], expected, {"/tmp", "/var/lib/mysql"})

    def validate(self, profile):
        ops.require(profile["project"] == self.project and profile["station"] == str(self.station)
                    and profile["maintenance_image"] == self.coordinator["image"], "site_profile_mismatch")
        return profile

    def service_mounts(self, project, role, mounts, contract):
        expected = {}
        for target, (source, writable) in ops.SERVICE_MOUNTS[role].items():
            expected[target] = (mount("volume", project + "_mysql_socket", writable) if source == "mysql_socket"
                                else mount("bind", self.station / "data" / source, writable))
        check_mounts(mounts, expected, TEMPORARY[role])

    def services(self, profile, docker):
        return ops.bound_services(profile, docker, mount_validator=self.service_mounts)

    def check_run(self, profile, docker, original, *, identity_role="backup_identity"):
        state = Path(profile.get("state_dir", STATE))
        check_certificate_idle(state)
        restore = state / "restore.json"
        if self.service == "backup" and restore.exists():
            ops.require(ops.read_json(restore)["stage"] in ("preflight", "eligible", "completed", "rejected"),
                        "unfinished_restore_blocks_backup")
        current = self.services(profile, docker)
        for role in ops.SERVICES:
            ops.require(all(original[role][key] == current[role][key] for key in
                            ("id", "image", "project", "service", "mounts")), "original_service_identity_changed")
        ops.require(identity_role in ("backup_identity", "restore_identity"), "unexpected_recovery_key_role")
        for role in ("mysql_socket", identity_role):
            name = self.project + "_" + role
            found = json.loads(docker.call("volume", "inspect", "--format", "{{json .}}", name))
            labels = found.get("Labels") or {}
            ops.require(found["Name"] == name and found["Driver"] == "local" and not found.get("Options")
                        and labels.get("com.docker.compose.project") == self.project
                        and labels.get("com.docker.compose.volume") == role, "project_volume_identity_mismatch")
            if role == identity_role:
                ops.require(labels.get("org.gift-ledger.gui-v2-project") == self.project
                            and labels.get("org.gift-ledger.gui-v2-role") == role, "backup_key_volume_mismatch")

    def profile(self):
        state, store = self.directories()
        ops.backup_store._store(store)  # reject a foreign output directory before stopping applications
        path = state / "site.json"
        ops.require(path.exists(), "backup_key_registration_required")
        ops.no_links(path)
        for item, mode in ((state, 0o700), (path, 0o600)):
            info = item.lstat()
            ops.require(stat.S_IMODE(info.st_mode) == mode and (info.st_uid, info.st_gid) == (0, 0),
                        "backup_registration_permissions_invalid")
        record = ops.read_json(path)
        ops.require(set(record) == {"project", "instance_id", "server_uuid", "backup_recipient"}
                    and record["project"] == self.project and ops.RECIPIENT.fullmatch(record["backup_recipient"]),
                    "backup_registration_invalid")
        for key in ("instance_id", "server_uuid"):
            ops.require(str(UUID(record[key])) == record[key], "backup_registration_invalid")
        return {**self.runtime_profile(), "instance_id": record["instance_id"], "server_uuid": record["server_uuid"],
                "recipient": record["backup_recipient"], "preview_policy": "fixed-window"}

    def directories(self):
        return STATE, STORE

    def runtime_profile(self):
        state, store = self.directories()
        containers = {}
        for role in ops.SERVICES:
            found = inspect_service(self.docker, self.project, role)
            self.service_mounts(self.project, role, found["mounts"], None)
            containers[role] = {key: found[key] for key in ("id", "image", "mounts")}
            containers[role]["name"] = found["name"].lstrip("/")
        return {"project": self.project, "schema_revision": "schema_v2_s011",
                "station": str(self.station), "state_dir": str(state), "store_dir": str(store),
                "docker_bin": str(self.docker.binary), "maintenance_image": self.coordinator["image"],
                "containers": containers}


def backup():
    project = os.environ["GIFTLEDGER_PROJECT"]
    single_yaml_site.project_host(project)
    docker = ops.Docker("/usr/bin/docker", "unix:///var/run/docker.sock")
    site = SiteBindings(docker, project)
    report_backup(run_backup(site, site.profile(), docker, Worker))


def run_backup(site, profile, docker, worker_factory):
    state, store = site.directories()
    pending = [row for _, row in ops.run_records(state)
               if row["stage"] not in ("finished", "preflight-failed") or row.get("private_cleanup_pending")]
    ops.require(len(pending) <= 1, "ambiguous_backup_session")
    mode, session = ("recover", pending[0]["session_id"]) if pending else ("manual", None)
    result = ops.run(profile, docker=docker, mode=mode, session_id=session,
                     worker_factory=worker_factory, site_bindings=site)
    ops.require(not result.get("private_cleanup_pending"), "private_cleanup_pending")
    ops.require(result.get("bundle_id"), "backup_not_published")
    bundle = store / "artifacts" / result["bundle_id"]
    manifest = ops.backup_store.inspect_bundle(bundle)
    ops.require(ops.bundle_matches_run(manifest, result, profile), "finished_backup_bundle_mismatch")
    return bundle


def report_backup(bundle):
    print(f"本站备份已实际解密核验，服务已回到备份前的状态。请复制整个目录：backups/artifacts/{bundle.name}/")


def main():
    if sys.argv[1:] == ["prepare"]:
        single_yaml_site.main()
        site_directories()
    elif sys.argv[1:] == ["refresh-guide"]:
        single_yaml_site.main()
    elif sys.argv[1:] == ["backup"]:
        backup()
    else:
        raise ops.BackupOpsError("unknown_backup_action")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_-]{1,100}", code) is None:
            code = "backup_environment_invalid"
        if code == "backup_key_registration_required":
            print("请先完成本站备份钥匙登记，再运行备份。", file=sys.stderr)
        else:
            print("备份任务未完成，保留了已有回执。原因：" + code, file=sys.stderr)
        sys.exit(1)
