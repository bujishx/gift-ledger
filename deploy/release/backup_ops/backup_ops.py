"""Portable host coordinator. Docker is invoked with argv, never a shell.

Secrets and plaintext stay in the existing restricted Linux maintenance image.
The host sees public identities, encrypted artifacts and redacted receipts only.
"""

import argparse
import contextlib
import hashlib
import html
import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_export
import backup_store
import windows_path_budget

FORMAT = "gift-ledger-backup-ops-profile-v1"
IMAGE = "gift-ledger-portable-maintenance:s011-8c49a16f230be7d2"
SERVICES = ("web", "api", "db")
ROLES = {
    "root_config": "/work/site",
    "app_secrets": "/work/app-secrets",
    "tls_config": "/work/tls",
    "db_secrets": "/work/db-secrets",
    "mysql_socket": "/work/mysql",
    "app_data": "/work/app-data",
    "backup_identity": "/work/identity",
}
TOOLS = (
    "backup_ops/backup_ops.py",
    "backup_ops/backup_worker.py",
    "backup_ops/backup_store.py",
    "backup_ops/windows_path_budget.py",
    "backup_ops/backup_export.py",
    "backup_ops/preview_policy.py",
    "backup_ops/backup_audit.py",
    "backup_ops/resource_meter.py",
    "gui_v2/maintenance.py",
    "gui_v2/prepare_volumes.py",
    "gui_v2/certificate_renewal.py",
    "gui_v2/key_transfer.py",
    "gui/prepare.py",
    "gui/initialize.py",
)
V3_TOOLS = TOOLS + (
    "backup_ops/restore_file_check.py",
    "gui_v2/request_builder.py",
    "operations_v3/target.py",
    "operations_v3/production_approval.py",
    "operations_v3/generator.py",
    "operations_v3/platforms.py",
    "operations_v3/scheduler.py",
    "operations_v3/binding.py",
    "operations_v3/operations.py",
    "operations_v3/wizard.py",
    "operations_v3/runtime.py",
    "operations_v3/contract_bridge.py",
    "operations_v3/notify.py",
    "operations_v3/custody.py",
    "operations_v3/workflows.py",
    "operations_v3/schema_upgrade.py",
    "operations_v3/certificates.py",
    "operations_v3/offline_audit.py",
    "operations_v3/handover.py",
    "operations_v3/trusted_open.py",
    "operations_v3/migration_peer.py",
    "operations_v3/website_release.py",
    "operations_v3/direct_ingress_probe.py",
    "operations_v3/legacy_adapter.py",
    "operations_v3/isolated_probe.py",
    "operations_v3/isolated_probe_worker.py",
    "operations_v3/isolated_probe_executor.py",
    "operations_v3/desktop_paths.py",
)
ID = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")
RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")
MAX_EVENT_BYTES = 8 * 1024**2
SERVICE_MOUNTS = {
    "web": {"/etc/nginx/certs": ("tls_config", False)},
    "api": {
        "/run/secrets": ("app_secrets", False),
        "/var/lib/gift-ledger": ("app_data", True),
    },
    "db": {
        "/run/secrets": ("db_secrets", False),
        "/var/lib/mysql": ("mysql_data", True),
        "/var/run/mysqld": ("mysql_socket", True),
    },
}


class BackupOpsError(RuntimeError):
    def __init__(self, code, session_started=None):
        self.code = code
        self.session_started = session_started
        super().__init__(code)


def require(value, code):
    if not value:
        raise BackupOpsError(code)


def utcnow():
    return datetime.now(timezone.utc)


def raw_json(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def unique_pairs(items):
    out = {}
    for key, value in items:
        require(key not in out, "duplicate_json_key")
        out[key] = value
    return out


def no_links(path):
    path = Path(path)
    require(".." not in path.parts, "path_traversal_forbidden")
    path = path.absolute()
    require(not str(path).startswith("\\\\"), "network_path_not_supported")
    for item in (path, *path.parents):
        if not os.path.lexists(str(item)):
            continue
        info = item.lstat()
        require(
            not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400,
            "linked_path_forbidden",
        )
        if stat.S_ISREG(info.st_mode):
            require(info.st_nlink == 1, "hardlinked_file_forbidden")
    return path


def read_json(path):
    path = no_links(path)
    info = path.stat()
    require(path.is_file() and info.st_size <= 2 * 1024**2, "json_file_invalid")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)


def digest(path):
    path = no_links(path)
    before = path.stat()
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            result.update(block)
    after = path.stat()
    require(
        (before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_ino, after.st_size, after.st_mtime_ns),
        "file_changed",
    )
    return result.hexdigest()


def mkdir(path):
    path = no_links(path)
    missing = [item for item in (path, *path.parents) if not item.exists()]
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(path.is_dir(), "directory_required")
    for created in reversed(missing):
        fsync_directory(created.parent)
    return path


def fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_json(path, value, *, new=False):
    path = no_links(path)
    mkdir(path.parent)
    if new:
        with path.open("xb") as stream:
            os.chmod(str(path), 0o600)
            stream.write(raw_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(path.parent)
        return
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            os.chmod(str(temporary), 0o600)
            stream.write(raw_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        no_links(path)
        os.replace(str(temporary), str(path))
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def host_lock(state):
    path = mkdir(state) / "coordinator.lock"
    no_links(path)
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BackupOpsError("another_run_is_active") from None
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupOpsError("another_run_is_active") from None
        yield
    finally:
        stream.close()


def allowed_project(project):
    if (Path(__file__).absolute().parent.parent / "gui_v2/PORTABLE-DSM.json").exists():
        return bool(isinstance(project,str) and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}",project) and dsm9_project(project))
    return project == "gift-ledger-nas-prod" or dsm9_project(project) or bool(
        re.fullmatch(r"gift-ledger-(?:gui-test|site)-[0-9a-f]{16}", str(project))
    )


def dsm9_project(project):
    """Load the separate synthetic profile only for its exact project family."""
    if isinstance(project, str) and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", project):
        path = Path(__file__).absolute().parent.parent / "gui_v2/portable_dsm_profile.py"
        spec = importlib.util.spec_from_file_location("backup_ops_portable_profile", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.load(project)
        return True
    if not isinstance(project, str) or not project.startswith("gift-ledger-dsm9-"):
        return False
    path = Path(__file__).resolve().parent.parent / "gui_v2/dsm9_profile.py"
    spec = importlib.util.spec_from_file_location("backup_ops_dsm9_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return project in module.PROJECTS


def profile_tools(profile):
    extra = ()
    if "probe_history_toolset" in profile:
        require(type(profile["probe_history_toolset"]) is int
                and profile["probe_history_toolset"] == 1
                and bool(profile.get("target_contract")),
                "probe_history_toolset_unsupported")
        extra += ("gui_v2/failed_probe_history.py",)
    if "access_origins_toolset" in profile:
        require(type(profile["access_origins_toolset"]) is int
                and profile["access_origins_toolset"] == 1,
                "access_origins_toolset_unsupported")
        if profile.get("target_contract"):
            extra += ("operations_v3/access_origins.py", "operations_v3/access_entries.py",
                     "operations_v3/access_entries_host.py", "operations_v3/access_entries_consumers.py",
                     "operations_v3/package_release.py")
        else:
            require(isinstance(profile.get("project"), str)
                    and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", profile["project"]),
                    "access_origins_toolset_platform_invalid")
            extra += ("gui_v2/access_origins.py",)
    if profile.get("target_contract"):
        return V3_TOOLS + extra
    if isinstance(profile.get("project"), str) and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", profile["project"]):
        dsm9_project(profile["project"])
        return TOOLS + ("gui_v2/portable_dsm_profile.py", "gui_v2/portable_dsm_target.py", "gui_v2/PORTABLE-DSM.json") + extra
    if dsm9_project(profile.get("project")):
        return TOOLS + ("gui_v2/dsm9_profile.py",)
    return TOOLS


def _load_profile_target(contract, toolkit):
    require(isinstance(contract, dict) and set(contract) == {"path", "sha256"}
            and SHA.fullmatch(str(contract.get("sha256"))), "target_contract_invalid")
    path = no_links(contract["path"])
    require(path.is_absolute() and digest(path) == contract["sha256"],
            "target_contract_changed")
    module_path = no_links(Path(toolkit) / "operations_v3/target.py")
    spec = importlib.util.spec_from_file_location("backup_ops_target_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_target_spec(path, expected_sha256=contract["sha256"])


def target_contract(profile):
    """Validate generated sites against their pinned complete resource intent."""
    contract = profile.get("target_contract")
    generated = str(profile.get("project", "")).startswith("gift-ledger-site-")
    require(not generated or contract is not None, "target_contract_required")
    if contract is None:
        return None
    target = _load_profile_target(contract, profile["toolkit_root"])
    require(target["project"] == profile["project"]
            and target["engine_id"] == profile["engine_id"], "target_profile_identity_mismatch")
    for key, field in (("installation_dir", "installation"), ("state_dir", "state"),
                       ("store_dir", "store")):
        require(no_links(profile[key]) == no_links(target["paths"][field]),
                "target_profile_paths_mismatch")
    source = target.get("source")
    if source:
        require(profile["instance_id"] != source["instance_id"]
                and profile["server_uuid"] != source["server_uuid"],
                "source_and_target_identity_reused")
    for role in SERVICES:
        require(profile["containers"][role]["image"] in target["images"][role]["allowed_ids"],
                "target_service_image_mismatch")
    require(profile["maintenance_image"] in target["images"]["maintenance"]["allowed_ids"],
            "target_maintenance_image_mismatch")
    return target


def validate_profile(value, *, check_tools=True):
    require(
        isinstance(value, dict)
        and value.get("format") == FORMAT
        and value.get("version") == 1,
        "profile_format_invalid",
    )
    require(allowed_project(value.get("project")), "unsupported_project")
    if "docker_endpoint" in value:
        require(value.get("target_contract") is not None
                and isinstance(value["docker_endpoint"], str)
                and value["docker_endpoint"] in Docker.LOCAL_ENDPOINTS,
                "profile_local_endpoint_binding_invalid")
    require(isinstance(value.get("engine_id"), str)
            and re.fullmatch(r"[A-Za-z0-9:._-]{10,200}", value["engine_id"]), "engine_binding_invalid")
    for key in ("instance_id", "server_uuid"):
        try:
            require(str(UUID(value[key])) == value[key], "profile_identity_invalid")
        except (KeyError, ValueError, TypeError):
            raise BackupOpsError("profile_identity_invalid") from None
    require(value.get("schema_revision") == "schema_v2_s011", "unsupported_release")
    require(RECIPIENT.fullmatch(str(value.get("recipient"))), "recipient_invalid")
    paths = []
    for key in (
        "installation_dir",
        "toolkit_root",
        "state_dir",
        "store_dir",
        "docker_bin",
    ):
        item = no_links(value.get(key, ""))
        require(
            Path(value.get(key, "")).is_absolute()
            and not any(c in str(item) for c in (",", "\n", "\r")),
            "profile_path_invalid",
        )
        paths.append(item)
    install, toolkit, state, store, docker = paths
    if re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", value["project"]):
        path = Path(__file__).absolute().parent.parent / "gui_v2/portable_dsm_profile.py"
        spec = importlib.util.spec_from_file_location("backup_ops_portable_paths", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        site = module.load(value["project"]).site(value["project"])
        require(str(install) == site["installation"]
                and str(toolkit) == site["installation"] + "-tools"
                and str(state) == site["installation"] + "-workspace/state"
                and str(store) == site["installation"] + "-workspace/store",
                "portable_profile_paths_mismatch")
    require(
        install.is_dir() and toolkit.is_dir() and docker.is_file(),
        "profile_program_missing",
    )
    for a, b in ((state, store), (state, install), (store, install)):
        require(
            a != b and a not in b.parents and b not in a.parents,
            "profile_directories_overlap",
        )
    require(
        set(value.get("containers", {})) == set(SERVICES), "service_bindings_invalid"
    )
    for role, item in value["containers"].items():
        require(
            item.get("name") == value["project"] + "-" + role + "-1"
            and SHA.fullmatch(str(item.get("id")))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("image"))),
            "service_bindings_invalid",
        )
        validate_mounts(value["project"], role, item.get("mounts"), value.get("target_contract"))
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("maintenance_image"))),
        "maintenance_image_invalid",
    )
    require(
        value.get("schedule") == {"timezone": "Asia/Singapore", "time": "03:30"},
        "schedule_invalid",
    )
    require(
        value.get("retention") == {"daily": 14, "weekly": 8, "monthly": 12},
        "retention_invalid",
    )
    require(
        value.get("preview_policy", "block")
        in ("block", "quarantine-expired", "fixed-window"),
        "preview_policy_invalid",
    )
    hashes = value.get("tool_hashes", {})
    require(set(hashes) == set(profile_tools(value)),
            "tool_manifest_invalid")
    if check_tools:
        for name, expected in hashes.items():
            require(
                digest(toolkit / name) == expected, "tool_changed_reenroll_required"
            )
    if value.get("target_contract") or value["project"].startswith("gift-ledger-site-"):
        target_contract(value)
    return value


class Docker:
    LOCAL_ENDPOINTS = {
        "unix:///var/run/docker.sock", "unix:///run/docker.sock",
        "npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine",
    }

    def __init__(self, binary, endpoint=None):
        self.binary = str(binary)
        self.endpoint = endpoint or ("npipe:////./pipe/docker_engine" if os.name == "nt"
                                     else "unix:///var/run/docker.sock")
        require(self.endpoint in self.LOCAL_ENDPOINTS, "local_docker_endpoint_required")

    def argv(self, *args):
        return [self.binary, "--host", self.endpoint, *map(str, args)]

    @staticmethod
    def environment():
        # Explicit --host pins the endpoint; no inherited context, TLS, API
        # version or alternative configuration may change the second process.
        return {key: value for key, value in os.environ.items()
                if not key.upper().startswith("DOCKER_")}

    def call(self, *args, timeout=120):
        try:
            result = subprocess.run(
                self.argv(*args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=self.environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise BackupOpsError("docker_unavailable_or_timed_out") from None
        if result.returncode != 0 and args and args[0] == "start" and "--attach" in args:
            # Only this pinned utility's explicitly public failure schema may
            # cross the redaction boundary. Never forward driver stderr.
            try:
                failure = json.loads(result.stdout)
                code = failure.get("code")
                if (failure.get("format") == "gift-ledger-offsite-decryption-audit-v1"
                        and failure.get("success") is False
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(code))):
                    raise BackupOpsError("external_audit_" + code)
            except (ValueError, AttributeError):
                pass
        require(
            result.returncode == 0 and len(result.stdout) < 1024**2,
            "docker_command_failed",
        )
        return result.stdout.decode("utf-8").strip()

    def inspect(self, name):
        template = (
            '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
            '"running":{{json .State.Running}},"status":{{json .State.Status}},'
            '"oom":{{json .State.OOMKilled}},"exit_code":{{json .State.ExitCode}},'
            '"health":{{if (index .State "Health")}}{{json (index (index .State "Health") "Status")}}{{else}}null{{end}},'
            '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
            '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
            '"session":{{json (index .Config.Labels "org.gift-ledger.backup-session")}}}'
        )
        template = template[:-1] + ',"mounts":{{json .Mounts}}}'
        result = json.loads(
            self.call("inspect", "--type", "container", "--format", template, name)
        )
        result["mounts"] = sorted(
            [
                dict({key: mount.get(key) for key in ("Type", "Name", "Destination", "RW")},
                     **({"Source": mount.get("Source")} if mount.get("Type") == "bind" else {}))
                for mount in result["mounts"]
            ],
            key=lambda item: item["Destination"],
        )
        return result


def validate_mounts(project, role, mounts, contract=None):
    require(isinstance(mounts, list), "service_mount_bindings_invalid")
    temporary = {
        "web": {"/tmp", "/var/cache/nginx", "/var/run"},
        "api": {"/tmp", "/var/lib/mysql"},
        "db": set(),
    }[role]
    require(
        all(
            item.get("Type") == "volume"
            or (item.get("Type") == "tmpfs" and item.get("Destination") in temporary)
            or (contract is not None and item.get("Type") == "bind"
                and item.get("Destination") == "/work/target/TARGET.json"
                and item.get("RW") is False
                and target_bind_source(item.get("Source"), contract.get("path")))
            for item in mounts
        ),
        "service_unexpected_mount",
    )
    require(len({item.get("Destination") for item in mounts}) == len(mounts),
            "service_duplicate_mount_destination")
    volumes = {
        item["Destination"]: item for item in mounts if item.get("Type") == "volume"
    }
    for destination, (suffix, writable) in SERVICE_MOUNTS[role].items():
        item = volumes.get(destination, {})
        require(
            item.get("Name") == project + "_" + suffix and item.get("RW") is writable,
            "service_mount_bindings_invalid",
        )
    require(len(volumes) == len(SERVICE_MOUNTS[role]), "service_unexpected_volume")
    if contract is not None:
        require(sum(item.get("Type") == "bind" for item in mounts) == 1,
                "target_contract_mount_required")


def target_bind_source(actual, expected):
    """Only Docker Desktop's deterministic drive mapping is equivalent on Windows."""
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    if actual == expected:
        return True
    if os.name == "nt":
        normalized = expected.replace("\\", "/")
        if re.fullmatch(r"[A-Za-z]:/[^\r\n,]+", normalized):
            native = actual.replace("\\", "/")
            if native.casefold() == normalized.casefold():
                return True
            drive = normalized[0].lower()
            tail = normalized[2:]
            return actual in ("/run/desktop/mnt/host/" + drive + tail,
                              "/host_mnt/" + drive + tail,
                              "/" + drive + tail)
    return False


def bound_services(profile, docker, *, mount_validator=validate_mounts):
    check_driver_endpoint(profile, docker)
    result = {}
    for role in SERVICES:
        expected = profile["containers"][role]
        found = docker.inspect(expected["name"])
        require(
            all(found[key] == expected[key] for key in ("id", "image"))
            and found["project"] == profile["project"]
            and found["service"] == role,
            "service_identity_changed_reenroll_required",
        )
        mount_validator(profile["project"], role, found.get("mounts"), profile.get("target_contract"))
        require(found["mounts"] == expected["mounts"], "service_mount_bindings_changed")
        require(
            found["status"] in ("running", "exited") and not found["oom"],
            "service_not_stable",
        )
        if found["running"]:
            require(found["health"] == "healthy", "service_not_healthy")
        result[role] = found
    require(result["db"]["running"], "database_not_running")
    return result


def bound_volumes(profile, docker, roles=ROLES):
    check_driver_endpoint(profile, docker)
    # Docker would silently create a missing named volume. Refuse before create.
    require(json.loads(docker.call("info", "--format", "{{json .ID}}")) == profile["engine_id"],
            "docker_engine_identity_changed")
    for role in roles:
        name = profile["project"] + "_" + role
        template = (
            '{"name":{{json .Name}},"project":{{json (index .Labels "org.gift-ledger.gui-v2-project")}},'
            '"role":{{json (index .Labels "org.gift-ledger.gui-v2-role")}}}'
        )
        value = json.loads(docker.call("volume", "inspect", "--format", template, name))
        require(
            value == {"name": name, "project": profile["project"], "role": role},
            "private_volume_identity_mismatch",
        )


def check_driver_endpoint(profile, docker):
    if "docker_endpoint" in profile:
        require(getattr(docker, "endpoint", None) == profile["docker_endpoint"],
                "docker_endpoint_binding_changed")


def create_profile(
    installation_dir,
    status_path,
    key_record_path,
    workspace,
    docker_bin,
    *,
    toolkit_root=None,
    profile_path=None,
    preview_policy="fixed-window",
    target_manifest=None,
    target_sha256=None,
    docker_endpoint=None,
):
    status, key = read_json(status_path), read_json(key_record_path)
    require(
        status.get("format") == "gift-ledger-gui-v2-status"
        and status.get("success") is True,
        "successful_site_status_required",
    )
    require(
        key.get("format") == "gift-ledger-gui-key-import-completed-v1"
        and key.get("project") == status.get("project")
        and key.get("instance_id") == status.get("instance_id"),
        "key_record_mismatch",
    )
    require(target_manifest is not None or docker_endpoint is None,
            "legacy_profile_endpoint_override_forbidden")
    require(target_manifest is not None or target_sha256 is None, "target_contract_required")
    docker = Docker(docker_bin, docker_endpoint)
    toolkit = Path(toolkit_root or Path(__file__).resolve().parent.parent).absolute()
    workspace = Path(workspace).absolute()
    contract = None
    image_reference = IMAGE
    if target_manifest is not None:
        target_path = no_links(target_manifest)
        contract = {"path": str(target_path),
                    "sha256": digest(target_path) if target_sha256 is None else target_sha256}
        if target_sha256 is not None:
            target = _load_profile_target(contract, toolkit)
            require(target["project"] == status.get("project"), "target_profile_identity_mismatch")
            image_reference = target["images"]["maintenance"]["reference"]
    value = {
        "format": FORMAT,
        "version": 1,
        "engine_id": json.loads(docker.call("info", "--format", "{{json .ID}}")),
        "project": status["project"],
        "instance_id": status["instance_id"],
        "server_uuid": status["server_uuid"],
        "schema_revision": status["schema_revision"],
        "recipient": key["backup_recipient"],
        "docker_bin": str(Path(docker_bin).resolve(strict=True)),
        "installation_dir": str(Path(installation_dir).absolute()),
        "toolkit_root": str(toolkit),
        "state_dir": str(workspace / "state"),
        "store_dir": str(workspace / "store"),
        "preview_policy": preview_policy,
        "schedule": {"timezone": "Asia/Singapore", "time": "03:30"},
        "retention": {"daily": 14, "weekly": 8, "monthly": 12},
        "maintenance_image": docker.call(
            "image", "inspect", "--format", "{{.Id}}", image_reference
        ),
        "containers": {},
        "tool_hashes": {},
    }
    if contract is not None:
        value["target_contract"] = contract
        value["docker_endpoint"] = docker.endpoint
        # New profiles pin the retained-failure validator. Existing profiles
        # keep their exact original tool list until an explicit tool migration.
        value["probe_history_toolset"] = 1
    # Explicitly version the new helper's integrity coverage. Legacy profiles
    # without this marker keep their original exact manifest; no silent upgrade.
    helper = (toolkit / "operations_v3/access_origins.py" if target_manifest is not None
              else toolkit / "gui_v2/access_origins.py")
    if (target_manifest is not None or re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", value["project"])) and helper.is_file():
        value["access_origins_toolset"] = 1
    value["tool_hashes"] = {name: digest(toolkit / name) for name in profile_tools(value)}
    for role in SERVICES:
        found = docker.inspect(value["project"] + "-" + role + "-1")
        value["containers"][role] = {
            "name": value["project"] + "-" + role + "-1",
            "id": found["id"],
            "image": found["image"],
            "mounts": found["mounts"],
        }
    validate_profile(value)
    bound_services(value, docker)
    bound_volumes(value, docker)
    for name in ("state_dir", "store_dir"):
        mkdir(value[name])
    target = Path(profile_path or workspace / "profile.json")
    save_json(target, value, new=True)
    installation_notes(target, value)
    return target


def installation_notes(profile_path, profile):
    root = Path(profile_path).absolute().parent
    program = Path(profile["toolkit_root"]) / "backup_ops" / "backup_ops.py"
    if os.name == "nt":

        def command(action, *args):
            return subprocess.list2cmdline(
                [
                    sys.executable,
                    "-B",
                    str(program),
                    action,
                    "--profile",
                    str(profile_path),
                    *args,
                ]
            )
    else:

        def command(action, *args):
            return " ".join(
                shlex.quote(str(x))
                for x in (
                    sys.executable,
                    "-B",
                    program,
                    action,
                    "--profile",
                    profile_path,
                    *args,
                )
            )

    commands = {
        "立即重要备份": command("run", "--human"),
        "查看状态": command("status", "--human"),
        "每天03:30任务": command("run", "--scheduled"),
        "每天04:15检查任务": command("watch"),
        "允许计划入口": command("enable"),
        "暂停计划入口": command("disable"),
        "查看保留清单": command("retention-plan"),
    }
    portable_dsm = bool(re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", profile["project"]))
    ubuntu_notes = (not portable_dsm and bool(profile.get("target_contract"))
                    and str(profile["installation_dir"]).startswith("/"))
    if portable_dsm:
        commands = {
            "手动备份（DSM网页）": "任务计划中运行本站已生成的02-手动备份.txt对应任务一次；通过已适配的计划停止入口执行。不要直接运行backup_ops.py run。",
            "查看状态（DSM网页）": "任务计划中运行本站03-查看状态.txt对应任务一次；查看本轮新status-目录中的结果.txt。此步骤不执行新备份。",
            "入口升级与域名变更后的衔接": "按本次材料的04-核对并升级入口.txt生成或更新本站事件绑定；只读核对通过后才使用02手动备份。不要重复首次登记或手改profile.json。",
            "计划备份（尚未启用）": "仅在另行决定启用计划时使用本站配套的计划入口及开启许可说明。材料生成、登记、入口升级均不自动启用DSM任务或计划许可。",
            "只读保留清单": command("retention-plan"),
        }
        cards = [
            ("手动备份", "DSM → 控制面板 → 任务计划",
             "本次备份材料的操作说明.html和02-手动备份.txt。",
             "选择与本站项目一致的专用手动任务；用户为root，保持未启用，命令须与本次材料完整一致。",
             "手动运行一次，等待本次执行结束。",
             "任务退出0，本次event-manual-报告的结果.txt显示“备份通过：成品已验证，三个服务正常，临时事件资源已清理。”；执行回执.json的backup_passed和artifact_readback_verified均为true。",
             "从任务输出的“报告：”打开本次准确目录；按其中成品位置保管同一套五文件。",
             "备份期间本站API和Web会按计划短暂停止，完成后由工具核对恢复。不要直接运行backup_ops.py run。",
             "失败、没有新报告或服务未恢复时先停止后续操作，查看任务运行结果、执行用户和完整命令；保留输出，不重复备份或手动补启动。"),
            ("查看当前状态", "DSM → 控制面板 → 任务计划",
             "本次备份材料的操作说明.html和03-查看状态.txt。",
             "选择本站专用状态任务；用户为root，保持未启用，核对完整命令。",
             "手动运行状态任务一次。",
             "任务退出0，本轮结果.txt显示“状态检查通过”及可靠备份套数；实时状态.json属于本站。若显示尚无成功备份或需要处理，按该提示继续，不能当作已经备份成功。",
             "按任务输出的“本次报告：”打开准确status-目录；其父目录以本次备份材料的操作说明.html为准。",
             "此步骤不生成新备份。计划入口未启用不代表DSM任务状态；状态通过也不代表邮件已经收到。",
             "没有新status-目录时，先查任务是否完成、执行用户是否root、命令是否完整；不要先重复运行或猜测报告位置。"),
            ("更新备份入口绑定", "DSM → 控制面板 → 任务计划",
             "本次备份材料的操作说明.html和04-核对并升级入口.txt。",
             "只在本站已有备份登记、需要升级入口或入口调整成功后使用；任务用户root，保持未启用。",
             "完整使用04命令，运行一次以核对当前身份并更新备份入口绑定。",
             "任务退出0，本次结果.txt显示“入口升级通过：已核对并绑定当前三个服务，可复用手动备份与计划入口。”；执行回执.json的event_upgrade_passed为true。",
             "按任务输出的“报告：”保存本次event-upgrade-events-报告，之后才可复用02手动备份入口。",
             "这一步会更新本站私有绑定并保留旧记录；不备份、不启停服务、不自动开启定时，也不重复首次登记。将来升级工具程序时还须使用匹配版本的新材料。",
             "失败时保留报告并停止后续备份；不要改profile.json、重登记或复用旧绑定。"),
            ("按需另行设置计划", "本机工具包与DSM → 控制面板 → 任务计划",
             "本次备份材料的操作说明.html、05-计划备份入口-未启用.txt及配套计划入口说明。",
             "先核对本站项目、计划时间与时区；本工具默认每天03:30，Asia/Singapore。",
             "仅在另行决定启用计划时按配套说明设置；未决定启用就保持原状。",
             "材料生成、首次登记、04升级都不会开启计划。只有配套许可、DSM任务设置及后续本轮报告分别核对后，才可按各自证据确认。",
             "启用后按配套说明核对首次实际计划结果；不需要定时则继续使用02手动入口。",
             "本页不是启用按钮，也不证明DSM定时触发或邮件收件。不得用直接backup_ops.py run --scheduled替代正式计划入口。",
             "未完成计划说明要求的核对前，不启用每日任务；不能用一次手动成功冒充定时验收。"),
            ("只读查看保留清单", "DSM → 控制面板 → 任务计划",
             "命令速查.txt中“只读保留清单”的准确命令。",
             "确认命令对应本站profile.json；若建立专用查看任务，用户为root并保持未启用。",
             "确需查看时仅运行只读保留清单命令一次；在任务运行结果中查看输出。",
             "任务退出0并给出本站保留清单；本步骤未执行备份、停站或删除。",
             "按清单理解保留规则；原成套备份、配套钥匙和正式任务材料继续保管。",
             "下面的精确命令只用于查看，不是删除命令：\n" + commands["只读保留清单"],
             "执行失败时保留非秘密输出；不要据此删除备份、钥匙或工作目录。"),
        ]
        labels = ("在哪里", "打开什么", "选择什么", "执行这一步", "看到什么才算成功", "接下来", "提醒", "继续前确认")
        text = "安装档案与常用操作（不含密码和私钥）\n项目：" + profile["project"] + "\n\n"
        for number, card in enumerate(cards, 1):
            text += str(number) + ". " + card[0] + "\n" + "\n".join(
                label + "：" + value for label, value in zip(labels, card[1:])) + "\n\n"
    elif ubuntu_notes:
        labels = ("在哪里", "打开什么", "选择什么", "执行这一步", "看到什么才算成功", "接下来", "提醒", "继续前确认")
        ubuntu_cards = [
            ("立即备份一次", "本站Ubuntu服务器终端", "本页与同目录命令速查.txt。",
             "核对上方项目、安装目录和工作目录；确认是要备份的本站。",
             commands["立即重要备份"],
             "命令退出0并显示“本地加密备份及实际解密核验通过；本站已按原运行状态恢复”；本次JSON的stage为finished、bundle_id非空，private_cleanup_pending为false。",
             "保存本次结果，按成品位置保管同一套五文件；原配套钥匙另行保管。",
             "备份期间API和Web会短暂停止；本命令不启用定时任务。",
             "失败或服务未恢复时保留会话编号和输出，停止后续操作；不要删锁、重复备份或手动补启动。"),
            ("查看已有状态记录", "本站Ubuntu服务器终端", "本页“查看已有状态记录”卡片。",
             "核对准确的profile.json路径；选择只读查看。", commands["查看状态"],
             "命令退出0并输出本站状态；需要处理项须按结果继续核对，不能仅凭退出0判定已备份成功。",
             "核对最近会话与可靠备份套数；如有未完成会话，先按对应故障分支处理。",
             "此命令没有--live，只读取已有记录；不代表重新探测三个服务，也不生成新备份。",
             "记录缺失、项目不符或结果不明时停止，不重新登记或删除工作目录。"),
            ("使用正式每日备份材料", "本站Ubuntu服务器及配套计划说明", "本版README.html的“先核对并复用已有正式绑定”“准备正式计划绑定候选”“本人确认具体接管对象后发布绑定”三卡，以及后续schedule-materials输出。",
             "仅在决定启用计划时选择每天03:30，Asia/Singapore；计划许可与服务器定时任务分别核对。",
             "先按正式绑定三卡核对或建立本站绑定，再生成schedule-materials。按README的禁用安装、通知验证及明确启用卡，只使用本次生成的daily.timer和daily.service。",
             "材料生成不代表计划已启用；只有实际定时触发的新会话通过，才证明本次计划执行成功。",
             "启用后核对首次计划会话；暂不需要计划就保持现状。",
             "计划入口检查03:30—04:00时段、当天重复执行与许可，不要为测试绕过这些检查。",
             "没有正式活动绑定、配套计划材料或许可时不要启用；一次手动备份不能代替计划触发验收。"),
            ("使用正式每日检查材料", "本站Ubuntu服务器及配套计划说明", "本版README.html的正式绑定三卡与计划材料卡，以及本站schedule-materials输出。",
             "若另行配置每日检查任务，核对本站profile.json、时间与时区。",
             "只用本次schedule-materials生成的watch.timer和watch.service；与每日备份共用同一准确活动绑定，按README相同步骤核对、安装和启用。",
             "实际04:15检查有本次结果，本站身份与最近备份状态明确；待处理事项按报告处理，不能只凭定时器已启用判定检查成功。",
             "保存当次结果；有问题先读会话与状态记录。",
             "检查不替代备份，也不证明通知邮件已收到；本页不创建定时任务。",
             "检查失败时不直接重复备份、删锁或启动服务。"),
            ("核对计划许可入口", "本站Ubuntu服务器及统一运维工具", "本版README.html中从“先核对并复用已有正式绑定”开始的三张卡；已有同站绑定先核对复用，没有才按候选与本人确认流程建立。",
             "先确认本站是否由TARGET.json生成并登记。",
             "依次按README的正式绑定三卡处理：已有同站绑定先核对复用；没有时保存候选，由本人核对具体接管对象后确认。",
             "当前绑定准确指向已验收的本站，首备和解密记录通过；绑定成功仍不表示服务器timer已经启用。",
             "按配套说明核对计划任务与许可；未决定启用时保持现状。",
             "安装、登记与生成本页都不会自动打开计划。",
             "不得为绕过拒绝而修改profile.json、伪造绑定或写入许可文件。"),
            ("按正式登记暂停计划", "本站Ubuntu服务器终端", "原活动绑定、计划登记及其配套停用说明。",
             "确认要暂停的是本站后续计划；先核对有无正在运行的操作。",
             "按README“只暂停未来计划，不终止进行中的工作”卡，停用两个准确timer并核对结果。",
             "两个timer均为disabled/inactive；原绑定、备份历史和已开始的操作保持，不将暂停触发写成已经完成备份。",
             "保存原状态与本次结果；进行中的操作按原记录等待并核对，需要再启用时重新满足启用条件。",
             "暂停未来触发不会中止正在运行的操作，不需要删除systemd任务。",
             "有未完成操作时保留现场并按状态处理；不要强行终止或清除记录。"),
            ("只读查看备份保留清单", "本站Ubuntu服务器终端", "本页只读保留清单命令。",
             "核对本站profile.json，选择retention-plan。", commands["查看保留清单"],
             "命令退出0并给出本站保留清单；本步骤未删除备份。",
             "按清单理解保留规则，继续保管成套备份和独立钥匙。",
             "这是只读预览，不是清理或删除入口。",
             "输出不明时不要据此删除备份、钥匙、目录或Docker资源。"),
        ]
        text = "安装档案与常用操作（不含密码和私钥）\n项目：" + profile["project"] + "\n\n"
        for number, card in enumerate(ubuntu_cards, 1):
            text += str(number) + ". " + card[0] + "\n" + "\n".join(
                label + "：" + value for label, value in zip(labels, card[1:])) + "\n\n"
    else:
        text = (
            "安装档案与常用命令（不含密码和私钥）\n\n"
            + "\n\n".join(name + "\n" + value for name, value in commands.items())
            + "\n"
        )
    target = no_links(root / "命令速查.txt")
    target.write_text(text, encoding="utf-8")
    body = "<h1>本站备份安装档案</h1><p>" + html.escape(profile["project"]) + "</p>"
    for key in (
        "instance_id",
        "server_uuid",
        "installation_dir",
        "toolkit_root",
        "state_dir",
        "store_dir",
        "preview_policy",
    ):
        body += (
            "<p><strong>"
            + key
            + "</strong><br>"
            + html.escape(str(profile[key]))
            + "</p>"
        )
    if portable_dsm:
        tokens = ("操作说明.html", "02-手动备份.txt", "03-查看状态.txt", "04-核对并升级入口.txt",
                  "05-计划备份入口-未启用.txt", "命令速查.txt", "结果.txt", "执行回执.json", "实时状态.json",
                  "profile.json", "backup_passed", "artifact_readback_verified", "event_upgrade_passed",
                  "event-upgrade-events-", "event-manual-", "status-", "root")
        def phrase(value):
            value = html.escape(value)
            for token in sorted(tokens, key=len, reverse=True):
                value = value.replace(html.escape(token), "<code>" + html.escape(token) + "</code>")
            return value.replace("\n", "<br>")
        body += '<p class="note">准确任务名、短命令、NAS上传位置和报告父目录，以本站本次备份材料的操作说明.html为准；不要从旧站或旧材料猜路径。</p>'
        for number, card in enumerate(cards, 1):
            body += '<section class="operation"><h2><span class="number">' + str(number) + "</span>" + html.escape(card[0]) + "</h2><dl>"
            for label, value in zip(labels[:6], card[1:7]):
                style = ' class="action"' if label == "执行这一步" else ' class="success"' if label == "看到什么才算成功" else ""
                body += "<dt>" + label + "</dt><dd" + style + ">" + phrase(value) + "</dd>"
            body += '</dl><p class="note">提醒：' + phrase(card[7]) + '</p><p class="gate">继续前确认：' + phrase(card[8]) + "</p></section>"
    elif ubuntu_notes:
        body += '<p class="note">本页命令已绑定本站资料；从Ubuntu终端按对应卡片操作。任务登记、计划许可和实际触发是不同步骤，不会由生成本页自动完成。</p>'
        def ubuntu_phrase(value):
            lines = []
            for line in value.split("\n"):
                escaped = html.escape(line)
                if line in commands.values():
                    escaped = "<code>" + escaped + "</code>"
                else:
                    for token in ("命令速查.txt", "README.html", "profile.json", "TARGET.json"):
                        escaped = escaped.replace(token, "<code>" + token + "</code>")
                lines.append(escaped)
            return "<br>".join(lines)
        for number, card in enumerate(ubuntu_cards, 1):
            body += '<section class="operation"><h2><span class="number">' + str(number) + "</span>" + html.escape(card[0]) + "</h2><dl>"
            for label, value in zip(labels[:6], card[1:7]):
                style = ' class="action"' if label == "执行这一步" else ' class="success"' if label == "看到什么才算成功" else ""
                body += "<dt>" + label + "</dt><dd" + style + ">" + ubuntu_phrase(value) + "</dd>"
            body += '</dl><p class="note">提醒：' + ubuntu_phrase(card[7]) + '</p><p class="gate">继续前确认：' + ubuntu_phrase(card[8]) + "</p></section>"
    else:
        body += "<p>每天03:30；时区Asia/Singapore。登记不会自动创建或启用DSM任务。</p>"
        for number, (name, value) in enumerate(commands.items(), 1):
            body += (
                '<section class="operation"><h2>' + str(number) + ". " + html.escape(name)
                + "</h2><pre>" + html.escape(value) + "</pre></section>"
            )
    target = no_links(root / "安装档案.html")
    target.write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<title>本站备份安装档案</title><style>body{max-width:960px;margin:30px auto;padding:20px;font:17px/1.8 system-ui;color:#20352d;background:#f7f9f7}"
        "h1,h2{color:#17624d}h2{font-size:21px}.operation{padding:18px;background:white;border:1px solid #dce5e0;border-radius:10px;margin:16px 0}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f1ff;color:#353e95;padding:15px;font:inherit}.note{font-size:14px;color:#64716d}"
        ".number{display:inline-block;background:#21664c;color:white;border-radius:8px;min-width:34px;text-align:center;margin-right:12px}dt{color:#62736b;font-size:14px;margin-top:12px}dd{margin:3px 0 0;overflow-wrap:anywhere}dd.action{color:#205d46;font-weight:650;font-size:19px}"
        "code{font:inherit;background:#f1ebf8;color:#5c367b;border-radius:4px;padding:2px 5px;overflow-wrap:anywhere}.success{background:#eef7f0;border-left:4px solid #2b815e;padding:10px}.gate{background:#fff3dc;border-left:4px solid #a4660c;padding:12px;font-weight:650}"
        "@media(max-width:640px){body{margin:16px auto;padding:12px}.operation{padding:14px}h2{font-size:20px}}"
        "</style>"
        + body,
        encoding="utf-8",
    )


def private_worker_resources():
    """Bound a single job's private memory while keeping headroom for the host and MySQL."""
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            line = next(line for line in meminfo if line.startswith("MemAvailable:"))
        available = int(line.split()[1]) // 1024
    except (OSError, StopIteration, ValueError, IndexError):
        raise BackupOpsError("maintenance_memory_unavailable") from None
    reserve = max(256, available // 3)
    scratch = min(1536, available - reserve - 192)
    require(scratch >= 64, "maintenance_memory_headroom_low")
    limit = str(scratch + 192) + "m"
    return ["--memory", limit, "--memory-swap", limit, "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,mode=1777,size=" + str(scratch) + "m"]


class Worker:
    def _volume_name(self, role):
        """Ordinary backups always use the site's original named volumes.

        The separate D03 coordinator subclasses this only for its independently
        verified temporary key volume; ordinary profile/CLI inputs cannot select
        a substitute volume or opt into that workflow.
        """
        require(role in ROLES, "worker_volume_role_invalid")
        return self.profile["project"] + "_" + role

    def _check_created(self):
        """Extension point for D03's additional exact-mount pre-start proof."""

    def _mount_arguments(self, run_dir):
        """Host paths and named volumes for an ordinary legacy installation."""
        profile = self.profile
        args = []
        for role, target in ROLES.items():
            writable = role in {"root_config", "mysql_socket"} or (
                role == "app_data"
                and profile.get("preview_policy")
                in ("quarantine-expired", "fixed-window")
            )
            args.extend(
                [
                    "--mount",
                    "type=volume,src="
                    + self._volume_name(role)
                    + ",dst="
                    + target
                    + ",volume-nocopy"
                    + ("" if writable else ",readonly"),
                ]
            )
        toolkit = Path(profile["toolkit_root"])
        if profile.get("target_contract"):
            for source, target in (
                (Path(profile["target_contract"]["path"]), "/work/target/TARGET.json"),
                (toolkit / "operations_v3", "/opt/gift-ledger-ops"),
            ):
                args.extend(["--mount", "type=bind,src=" + str(no_links(source))
                             + ",dst=" + target + ",readonly"])
            args.extend(["--env", "GIFT_LEDGER_TARGET_SHA256="
                         + profile["target_contract"]["sha256"]])
        for source, target in (
            (toolkit / "gui", "/opt/gift-ledger-gui"),
            (toolkit / "gui_v2", "/opt/gift-ledger-gui-v2"),
            (toolkit / "assets", "/opt/gift-ledger-release"),
            (toolkit / "backup_ops", "/opt/gift-ledger-backup-ops"),
        ):
            args.extend(
                [
                    "--mount",
                    "type=bind,src="
                    + str(no_links(source))
                    + ",dst="
                    + target
                    + ",readonly",
                ]
            )
        args.extend(
            [
                "--mount",
                "type=bind,src="
                + str(mkdir(run_dir / "exchange"))
                + ",dst=/work/exchange",
            ]
        )
        return args

    def __init__(self, profile, docker, journal, run_dir, *, recover=False):
        self.profile, self.docker, self.journal = profile, docker, journal
        self.name = profile["project"] + "-backup-ops-" + journal["session_id"]
        if recover and journal.get("worker_id"):
            # Never kill a potentially active maintenance operation. A stopped,
            # bound leftover container can be retired before reopening its journal.
            try:
                old = docker.inspect(journal["worker_id"])
            except BackupOpsError:
                old = None
            if old is not None:
                require(
                    old["id"] == journal["worker_id"]
                    and old["session"] == journal["session_id"]
                    and old["name"] == "/" + self.name,
                    "recovery_worker_identity_mismatch",
                )
                require(not old["running"], "previous_worker_still_active")
                docker.call("rm", old["id"])
        args = [
            "create",
            "--name",
            self.name,
            "--label",
            "org.gift-ledger.backup-session=" + journal["session_id"],
            "--label", "com.docker.compose.project=" + profile["project"],
            "--interactive",
            "--pull",
            "never",
            "--restart",
            "no",
            "--network",
            "none",
            "--read-only",
            *private_worker_resources(),
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/var/lib/mysql:rw,noexec,nosuid,nodev,size=16m",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=2m",
            "--log-opt",
            "max-file=2",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "GIFTLEDGER_BACKUP_DESTINATION_FREE_BYTES=" + str(shutil.disk_usage(profile["store_dir"]).free),
        ]
        args.extend(self._mount_arguments(run_dir))
        args.extend(
            [
                "--entrypoint",
                "/opt/venv/bin/python3.12",
                profile["maintenance_image"],
                "/opt/gift-ledger-backup-ops/backup_worker.py",
                "--project",
                profile["project"],
                "--session",
                journal["session_id"],
                "--instance-id",
                profile["instance_id"],
                "--server-uuid",
                profile["server_uuid"],
                "--recipient",
                profile["recipient"],
                "--backup-id",
                journal["backup_id"],
                "--verify-id",
                journal["verify_id"],
                "--preview-policy",
                profile.get("preview_policy", "block"),
            ]
        )
        if recover:
            args.append("--recover")
        self.container_id = docker.call(*args)
        require(SHA.fullmatch(self.container_id), "worker_container_id_invalid")
        journal["worker_id"] = self.container_id
        save_json(run_dir / "run.json", journal)
        self._check_created()
        try:
            attach_driver = Docker(docker.binary, getattr(docker, "endpoint", None))
            self.process = subprocess.Popen(
                attach_driver.argv("start", "--attach", "--interactive", self.container_id),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=attach_driver.environment(),
            )
        except OSError:
            found = docker.inspect(self.container_id)
            require(
                found["id"] == self.container_id
                and found["session"] == journal["session_id"]
                and not found["running"],
                "worker_attach_failed_preserve_container",
            )
            docker.call("rm", self.container_id)
            raise BackupOpsError(
                "worker_attach_failed", session_started=False
            ) from None
        self.events = queue.Queue(maxsize=8)

        def read_events():
            try:
                while True:
                    line = self.process.stdout.readline(MAX_EVENT_BYTES + 1)
                    if not line:
                        break
                    if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
                        self.events.put(
                            {"event": "error", "code": "worker_output_too_large"}
                        )
                        break
                    try:
                        self.events.put(json.loads(line))
                    except ValueError:
                        self.events.put(
                            {"event": "error", "code": "worker_output_invalid"}
                        )
                        break
            finally:
                self.events.put(None)

        threading.Thread(target=read_events, daemon=True).start()

    def receive(self, expected, timeout=720):
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            raise BackupOpsError("worker_timed_out_preserve_session") from None
        require(isinstance(value, dict), "worker_exited_preserve_session")
        if value.get("event") == "error":
            code = value.get(
                "error_code", value.get("code", "worker_failed_preserve_session")
            )
            raise BackupOpsError(
                code
                if re.fullmatch(r"[a-z0-9_-]{1,100}", str(code))
                else "worker_failed",
                session_started=value.get("session_started"),
            )
        require(value.get("event") == expected, "worker_protocol_invalid")
        return value

    def send(self, command):
        require(self.process.poll() is None, "worker_exited_preserve_session")
        self.process.stdin.write(json.dumps({"command": command}) + "\n")
        self.process.stdin.flush()

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            return False
        found = self.docker.inspect(self.container_id)
        if found["running"]:
            return False
        require(
            found["id"] == self.container_id
            and found["session"] == self.journal["session_id"],
            "worker_identity_changed",
        )
        self.docker.call("rm", self.container_id)
        return True


def save_run(path, record, stage):
    record["stage"] = stage
    record["updated_at"] = utcnow().isoformat()
    save_json(path, record)


def run_records(state):
    records = []
    for path in sorted(mkdir(Path(state) / "runs").iterdir()):
        no_links(path)
        require(path.is_dir() and ID.fullmatch(path.name), "run_history_unrecognized")
        record = read_json(path / "run.json")
        require(
            record.get("format") == "gift-ledger-backup-ops-run-v1"
            and record.get("session_id") == path.name,
            "run_history_invalid",
        )
        records.append((path, record))
    return records


def retire_exchange(run_dir, manifest):
    """Remove this run's duplicate ciphertext only after a verified publication.

    Public receipts and the small host journal remain for recovery diagnostics.
    The shared private maintenance history, quarantines and keys are untouched.
    """
    target = no_links(
        run_dir
        / "exchange"
        / ("backup-" + manifest["backup_operation_id"] + ".glgui.age")
    )
    if target.exists():
        require(
            digest(target) == manifest["files"][target.name],
            "duplicate_ciphertext_changed",
        )
        target.unlink()


def check_worker_status(profile, status):
    require(
        status.get("success") is True
        and all(
            status.get(k) == profile[k]
            for k in ("project", "instance_id", "server_uuid", "schema_revision")
        ),
        "worker_site_identity_mismatch",
    )
    require(
        status.get("readonly") == {"read_only": False, "super_read_only": False},
        "database_not_in_normal_mode",
    )


def restore_services(profile, docker, original, *, wait_seconds=120):
    for role in ("api", "web"):
        expected = profile["containers"][role]
        current = docker.inspect(expected["name"])
        require(
            current["id"] == expected["id"] and current["image"] == expected["image"],
            "service_identity_changed_during_run",
        )
        if not original[role]["running"]:
            require(not current["running"], "previously_stopped_service_changed")
            continue
        if not current["running"]:
            docker.call("start", expected["id"])
        until = time.monotonic() + wait_seconds
        while True:
            current = docker.inspect(expected["id"])
            require(
                current["id"] == expected["id"] and not current["oom"],
                "service_resume_failed",
            )
            if current["running"] and current["health"] == "healthy":
                break
            require(
                time.monotonic() < until and current["running"],
                "service_resume_not_healthy",
            )
            time.sleep(1)


def assert_no_schema_update_hold(profile):
    """The standalone/scheduled coordinator shares the migration hold too."""
    contract = profile.get("target_contract")
    if contract is None:
        return
    guard = no_links(Path(contract["path"]).parent / "SCHEMA_UPDATE.json")
    if not guard.exists():
        return
    record = read_json(guard)
    require(record.get("format") == "gift-ledger-schema-update-v1"
            and record.get("project") == profile["project"]
            and record.get("engine_id") == profile["engine_id"]
            and record.get("target_sha256") == contract["sha256"]
            and record.get("stage") == "completed"
            and record.get("restart_policies_restored") is True
            and record.get("after", {}).get("schema_revision") == "schema_v2_s011",
            "schema_update_hold_blocks_backup")


def bundle_matches_run(manifest, record, profile):
    """Match an inspected, five-file bundle to its original run and site."""
    return (manifest is not None
            and manifest["backup_operation_id"] == record.get("backup_id") == record.get("bundle_id")
            and manifest["verify_operation_id"] == record.get("verify_id")
            and all(manifest[key] == profile[key] for key in ("project", "instance_id", "server_uuid")))


def run(
    profile,
    *,
    mode="manual",
    now=None,
    docker=None,
    worker_factory=Worker,
    session_id=None,
    new_session_id=None,
    schedule_authorized=False,
    site_bindings=None,
):
    # A single-YAML coordinator supplies its own prevalidated host bindings.
    # This is an in-process adapter, never a path or option in a legacy profile.
    profile = validate_profile(profile) if site_bindings is None else site_bindings.validate(profile)
    assert_no_schema_update_hold(profile)
    require(mode in ("manual", "scheduled", "recover"), "run_mode_invalid")
    require(new_session_id is None or (mode == "manual" and site_bindings is not None
            and isinstance(new_session_id, str) and ID.fullmatch(new_session_id)), "invalid_new_backup_session")
    # Fail before directories, workers, leases or service stops. This release
    # supports traditional Windows path limits without changing host policy.
    if os.name == "nt":
        windows_path_budget.check_paths(state_dir=profile["state_dir"], store_dir=profile["store_dir"])
    docker = docker or Docker(profile["docker_bin"], profile.get("docker_endpoint"))
    state, store = mkdir(profile["state_dir"]), mkdir(profile["store_dir"])
    moment = now or utcnow()
    require(moment.tzinfo is not None, "aware_time_required")
    with host_lock(state):
        if site_bindings is not None and hasattr(site_bindings, "check_key_binding"):
            site_bindings.check_key_binding(profile, session_id if mode == "recover" else new_session_id)
        assert_no_schema_update_hold(profile)
        runs = mkdir(state / "runs")
        history = run_records(state)
        pending = [
            p
            for p, record in history
            if record.get("stage") not in ("finished", "preflight-failed")
        ]
        if mode == "recover":
            require(
                session_id and ID.fullmatch(session_id), "recovery_session_required"
            )
            run_dir = no_links(runs / session_id)
            record = read_json(run_dir / "run.json")
            require(
                record["project"] == profile["project"]
                and record["instance_id"] == profile["instance_id"],
                "recovery_profile_mismatch",
            )
            require(
                record["stage"] != "finished"
                or record.get("private_cleanup_pending") is True,
                "session_already_finished",
            )
            original = record["original"]
        else:
            require(not pending, "previous_session_requires_recovery")
            local = moment.astimezone(timezone(timedelta(hours=8)))
            if mode == "scheduled":
                require(
                    schedule_authorized or (state / "schedule-enabled.json").exists(),
                    "schedule_not_enabled"
                )
                due = local.replace(hour=3, minute=30, second=0, microsecond=0)
                require(
                    due <= local < due + timedelta(minutes=30),
                    "outside_schedule_window",
                )
                require(
                    not any(
                        record.get("scheduled_day") == local.date().isoformat()
                        and record.get("error_code") != "preview-active-deferred"
                        and record.get("outcome") != "deferred-active-preview"
                        for _, record in history
                    ),
                    "scheduled_day_already_attempted",
                )
            require(
                all(
                    shutil.disk_usage(path).free >= 512 * 1024**2
                    for path in (state, store)
                ),
                "insufficient_disk_space",
            )
            original = (bound_services(profile, docker) if site_bindings is None
                        else site_bindings.services(profile, docker))
            if mode == "scheduled":
                require(
                    all(original[s]["running"] for s in SERVICES),
                    "site_already_in_maintenance",
                )
            session_id = new_session_id or uuid4().hex
            run_dir = runs / session_id
            require(not os.path.lexists(run_dir), "backup_session_already_exists")
            run_dir.mkdir(mode=0o700)
            fsync_directory(runs)
            record = {
                "format": "gift-ledger-backup-ops-run-v1",
                "session_id": session_id,
                "project": profile["project"],
                "instance_id": profile["instance_id"],
                "backup_id": uuid4().hex,
                "verify_id": uuid4().hex,
                "mode": mode,
                "started_at": moment.isoformat(),
                "original": original,
                "scheduled_day": local.date().isoformat()
                if mode == "scheduled"
                else None,
            }
            save_run(run_dir / "run.json", record, "preflight")
        journal_path = run_dir / "run.json"
        worker, stopped, leased, manifest = None, False, False, None
        try:
            if site_bindings is None:
                bound_volumes(profile, docker)
            else:
                site_bindings.check_run(profile, docker, original)
            worker = worker_factory(
                profile, docker, record, run_dir, recover=mode == "recover"
            )
            record["worker_id"] = worker.container_id
            save_run(journal_path, record, "worker-created")
            ready = worker.receive("ready", timeout=2400)
            leased = True
            cleanup_only = bool(ready.get("session_completed"))
            check_worker_status(profile, ready["status"])
            save_run(journal_path, record, "lease-ready")
            if not ready.get("session_completed"):
                for role in ("web", "api"):
                    current = docker.inspect(profile["containers"][role]["name"])
                    require(
                        current["id"] == original[role]["id"],
                        "service_identity_changed_during_run",
                    )
                    if current["running"]:
                        require(
                            original[role]["running"],
                            "previously_stopped_service_changed",
                        )
                        save_run(journal_path, record, "stopping-" + role)
                        stopped = True
                        docker.call("stop", "--time", "30", current["id"])
                    require(
                        not docker.inspect(current["id"])["running"],
                        "application_stop_failed",
                    )
            save_run(journal_path, record, "applications-stopped")
            worker.send("run")
            # Core dump/encrypt/decrypt each have a 600s subprocess limit. The
            # enclosing wait must cover their sum, SQL drain and validation.
            result = worker.receive("verified", timeout=2400)
            check_worker_status(profile, result["status"])
            require(
                result.get("safe_to_restore") is True, "worker_cannot_prove_resume_safe"
            )
            record["outcome"] = result.get("outcome")
            record["resources"] = result.get("resources", {})
            record["quarantine_counts"] = result.get("quarantine_counts", {})
            if result.get("backup") and result.get("verify"):
                require(
                    result["backup"]["operation_id"] == record["backup_id"]
                    and result["verify"]["operation_id"] == record["verify_id"]
                    and all(
                        result["backup"].get(key) == profile[key]
                        for key in ("project", "instance_id", "server_uuid")
                    ),
                    "published_backup_identity_mismatch",
                )
                ciphertext = (
                    run_dir / "exchange" / result["backup"]["details"]["ciphertext"]
                )
                manifest = backup_store.publish(
                    store,
                    ciphertext,
                    result["backup"],
                    result["verify"],
                    protected=record["mode"] == "manual",
                )
                record["bundle_id"] = manifest["backup_operation_id"]
                save_json(
                    state / "bundle-manifests" / (record["bundle_id"] + ".json"),
                    manifest,
                )
            else:
                require(
                    result.get("outcome")
                    in (
                        "aborted-before-backup",
                        "already-finished",
                        "deferred-active-preview",
                    ),
                    "worker_outcome_invalid",
                )
                if record.get("bundle_id"):
                    manifest = backup_store.inspect_bundle(
                        store / "artifacts" / record["bundle_id"]
                    )
                    require(
                        bundle_matches_run(manifest, record, profile),
                        "recovery_bundle_mismatch",
                    )
                    if cleanup_only and result["outcome"] == "already-finished":
                        # The worker finished earlier. This retry verified the existing bundle.
                        record["outcome"] = "verified"
            save_run(journal_path, record, "verified")
            if not cleanup_only:
                restore_services(profile, docker, original)
            save_run(journal_path, record, "services-restored")
            worker.send("finish")
            finished = worker.receive("finished")
            record["private_cleanup_pending"] = bool(
                finished.get("private_cleanup_pending")
            )
            record["private_cleanup_errors"] = finished.get(
                "private_cleanup_errors", []
            )
            record["private_cleanup_counts"] = finished.get("private_cleanup_counts", {})
            record["resources_at_finish"] = finished.get("resources", record["resources"])
            if manifest is not None:
                retire_exchange(run_dir, manifest)
            save_json(
                state / "last-result.json",
                {
                    "success": bool(record.get("bundle_id")),
                    "project": profile["project"],
                    "instance_id": profile["instance_id"],
                    "server_uuid": profile["server_uuid"],
                    "mode": record["mode"],
                    "scheduled_day": record.get("scheduled_day"),
                    "session_id": session_id,
                    "bundle_id": record.get("bundle_id"),
                    "completed_at": utcnow().isoformat(),
                    "local_verified": bool(record.get("bundle_id")),
                    "services_restored": True,
                    "cloud_upload_verified": False,
                    "private_cleanup_pending": record["private_cleanup_pending"],
                    "resources": record["resources_at_finish"],
                    "outcome": record["outcome"],
                },
            )
            # Consumers may rely on both files after seeing 'finished'. Publish the receipt
            # first so a power loss before that point leaves the same session resumable.
            save_run(journal_path, record, "finished")
            if site_bindings is not None and manifest and not record["private_cleanup_pending"]:
                # Keep success independently of the last attempt, including after a later failure.
                save_json(state / "last-verified-backup.json", {
                    **{k: profile[k] for k in ("project", "instance_id", "server_uuid")},
                    "last": {"state": "verified", "at": manifest["completed_at"], "backup_id": record["bundle_id"]},
                })
            # Persist a new site's very first success independently of web/DSM schedule/host sampling.
            # If this is an upgrade, the dispatcher imports the original first result from older journals.
            first_path = state / "first-verified-backup.json"
            if (site_bindings is not None and manifest and not record["private_cleanup_pending"]
                    and not os.path.lexists(first_path)
                    and not any(prior.get("instance_id") == profile["instance_id"]
                                and prior.get("project") == profile["project"]
                                and prior.get("stage") == "finished" and prior.get("bundle_id")
                                and prior.get("private_cleanup_pending") is False for _, prior in history)):
                try:
                    save_json(first_path, {
                        "project": profile["project"], "instance_id": profile["instance_id"],
                        "server_uuid": profile["server_uuid"], "first": {
                            "state": "verified", "at": manifest["completed_at"], "backup_id": record["bundle_id"]
                        },
                    })
                except OSError:
                    pass  # The verified manifest and finished journal still allow one-time backfill.

            try:
                # The first backup with a staged key must leave historical, old-key artifacts in place.
                record["retention"] = ({"success": True, "status": "key_change_preserved_history"}
                                       if site_bindings is not None and profile.get("key_change_session") == session_id
                                       else apply_safe_retention(profile))
                save_json(journal_path, record)
            except (OSError, ValueError, BackupOpsError, backup_store.BackupStoreError):
                record["retention"] = {
                    "success": False,
                    "code": "retention_held_check_status",
                }
                save_json(journal_path, record)
            return record
        except BaseException as error:
            record["error_code"] = getattr(
                error, "code", "coordinator_interrupted_preserve_evidence"
            )
            needs_recovery = bool(
                mode == "recover"
                or leased
                or stopped
                or (
                    worker is not None
                    and getattr(error, "session_started", None) is not False
                )
            )
            save_run(
                journal_path,
                record,
                "needs-recovery" if needs_recovery else "preflight-failed",
            )
            save_json(
                state / "last-result.json",
                {
                    "success": False,
                    "session_id": session_id,
                    "error_code": record["error_code"],
                    "at": utcnow().isoformat(),
                    "needs_recovery": needs_recovery,
                },
            )
            if isinstance(error, BackupOpsError) and not needs_recovery:
                # The persisted coordinator journal proves no service stop or lease occurred.
                error.preflight_only = True
            raise
        finally:
            if worker:
                try:
                    record["worker_removed"] = worker.close()
                except (OSError, ValueError, BackupOpsError):
                    record["worker_removed"] = False
                save_json(journal_path, record)


def safe_retention_plan(profile, *, now=None):
    state = Path(profile["state_dir"])
    holds = []
    for path, manifest in backup_store.list_bundles(profile["store_dir"]):
        ack = no_links(state / "cloud-readbacks" / (path.name + ".json"))
        confirmed = False
        if ack.exists():
            value = read_json(ack)
            confirmed = (
                value.get("owner_reported_origin") == "cloud"
                and value.get("readback_verified") is True
                and value.get("decryption_verified") is True
                and value.get("manifest_sha256")
                == hashlib.sha256(backup_store.canonical(manifest)).hexdigest()
            )
        if not confirmed:
            holds.append(path.name)
    return backup_store.retention_plan(
        profile["store_dir"], now or utcnow(), hold_ids=holds
    )


def apply_safe_retention(profile, *, now=None):
    plan = safe_retention_plan(profile, now=now)
    removed = backup_store.apply_retention(profile["store_dir"], plan)
    return {
        "success": True,
        "removed": [path.name for path in removed],
        "kept": len(plan["keep"]),
        "cloud_unconfirmed_holds": len(plan["hold_ids"]),
        "plan_sha256": plan["plan_sha256"],
    }


def audit_external(profile, bundle_dir, *, origin="copy", docker=None):
    """Read back and actually decrypt a retrieved historical bundle, offline.

    Cloud origin is an explicit owner assertion. It is never inferred from a
    local success or the Cloud Sync icon. No database or application mounts.
    """
    require(origin in ("copy", "cloud", "mobile"), "copy_origin_invalid")
    profile = validate_profile(profile)
    source = no_links(bundle_dir)
    manifest = backup_store.inspect_bundle(source)
    require(
        all(
            manifest.get(key) == profile[key]
            for key in ("project", "instance_id", "server_uuid")
        ),
        "offsite_source_identity_mismatch",
    )
    for forbidden in (
        Path(profile["store_dir"]),
        Path(profile["state_dir"]),
        Path(profile["installation_dir"]),
        Path(profile["toolkit_root"]),
    ):
        require(
            source != forbidden and forbidden not in source.parents,
            "external_copy_must_be_separate",
        )
    state = mkdir(profile["state_dir"])
    expected_path = (
        state / "bundle-manifests" / (manifest["backup_operation_id"] + ".json")
    )
    expected = read_json(expected_path)
    require(expected == manifest, "external_copy_expected_manifest_mismatch")
    # The first readback is host-independent of the Linux decryption pass.
    backup_export.audit_copy(source, expected)
    docker = docker or Docker(profile["docker_bin"], profile.get("docker_endpoint"))
    audit_id = uuid4().hex
    name = profile["project"] + "-backup-audit-" + audit_id
    toolkit = Path(profile["toolkit_root"])
    with host_lock(state):
        bound_volumes(profile, docker, roles=("backup_identity",))
        args = [
            "create",
            "--name",
            name,
            "--label",
            "org.gift-ledger.backup-session=" + audit_id,
            "--label", "com.docker.compose.project=" + profile["project"],
            "--restart",
            "no",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            *private_worker_resources(),
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/var/lib/mysql:rw,noexec,nosuid,nodev,size=16m",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=2m",
            "--log-opt",
            "max-file=2",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        for src, dst in (
            (toolkit / "gui", "/opt/gift-ledger-gui"),
            (toolkit / "gui_v2", "/opt/gift-ledger-gui-v2"),
            (toolkit / "assets", "/opt/gift-ledger-release"),
            (toolkit / "backup_ops", "/opt/gift-ledger-backup-ops"),
            (source, "/work/bundle"),
            (expected_path, "/work/expected.json"),
        ):
            args.extend(
                [
                    "--mount",
                    "type=bind,src=" + str(no_links(src)) + ",dst=" + dst + ",readonly",
                ]
            )
        args.extend(
            [
                "--mount",
                "type=volume,src="
                + profile["project"]
                + "_backup_identity,dst=/work/identity,volume-nocopy,readonly",
                "--entrypoint",
                "/opt/venv/bin/python3.12",
                profile["maintenance_image"],
                "/opt/gift-ledger-backup-ops/backup_audit.py",
                "--bundle-dir",
                "/work/bundle",
                "--expected-manifest",
                "/work/expected.json",
                "--json",
            ]
        )
        container_id = docker.call(*args)
        require(SHA.fullmatch(container_id), "audit_container_id_invalid")
        audit_record = {"format": "gift-ledger-backup-audit-run-v1", "audit_id": audit_id,
                        "container_id": container_id, "project": profile["project"],
                        "backup_id": manifest["backup_operation_id"], "origin": origin,
                        "stage": "created", "recorded_at": utcnow().isoformat()}
        audit_path = state / "audits" / (audit_id + ".json")
        save_json(audit_path, audit_record, new=True)
        try:
            result = json.loads(
                docker.call("start", "--attach", container_id, timeout=720)
            )
            require(
                result.get("success") is True
                and result.get("decryption_verified") is True
                and result.get("readback_verified") is True
                and result.get("restoration_verified") is False
                and all(
                    result.get(key) == manifest[key]
                    for key in (
                        "project",
                        "instance_id",
                        "server_uuid",
                        "backup_operation_id",
                    )
                ),
                "external_decryption_audit_failed",
            )
            # Bind the result to our separately retained expected manifest.
            result.update(
                owner_reported_origin=origin,
                origin_independently_verified=False,
                manifest_sha256=hashlib.sha256(
                    backup_store.canonical(manifest)
                ).hexdigest(),
            )
            save_json(state / "copy-readbacks" / (audit_id + ".json"), result, new=True)
            if origin == "cloud":
                save_json(
                    state
                    / "cloud-readbacks"
                    / (manifest["backup_operation_id"] + ".json"),
                    result,
                )
            audit_record["stage"] = "completed"
            return result
        except BaseException as error:
            audit_record.update(stage="failed", code=getattr(error, "code", "external_audit_interrupted"))
            save_json(audit_path, audit_record)
            raise
        finally:
            found = docker.inspect(container_id)
            require(
                found["id"] == container_id and found["session"] == audit_id,
                "audit_container_identity_changed",
            )
            if found["running"]:
                docker.call("stop", "--time", "5", container_id)
            docker.call("rm", container_id)
            audit_record["container_removed"] = True
            save_json(audit_path, audit_record)


def status(profile, *, now=None, live=False, docker=None, site_probe=None,
           schedule_authorized=False, startup_audit=False, schedule_started_at=None):
    profile = validate_profile(profile)
    state = Path(profile["state_dir"])
    last = (
        read_json(state / "last-result.json")
        if (state / "last-result.json").exists()
        else None
    )
    enabled = schedule_authorized or (state / "schedule-enabled.json").exists()
    moment = (now or utcnow()).astimezone(timezone(timedelta(hours=8)))
    day_due = moment.replace(hour=4, minute=15, second=0, microsecond=0)
    bundles = backup_store.list_bundles(profile["store_dir"])
    history = run_records(state)
    scheduled = [record for _, record in history
                 if record.get("mode") == "scheduled"
                 and record.get("scheduled_day") == moment.date().isoformat()]
    verified_scheduled = []
    manifests = {path.name: manifest for path, manifest in bundles}
    for _, record in history:
        if (record.get("mode") == "scheduled"
                and record.get("stage") == "finished" and record.get("outcome") in ("verified", "already-finished")
                and record.get("project") == profile["project"]
                and record.get("instance_id") == profile["instance_id"]
                and bundle_matches_run(manifests.get(record.get("bundle_id")), record, profile)
                and record.get("private_cleanup_pending") is not True
                and record.get("worker_removed") is True):
            verified_scheduled.append(record)
    scheduled_success = [r["session_id"] for r in verified_scheduled
                         if r.get("scheduled_day") == moment.date().isoformat()]
    last_run_failed = bool(last and last.get("success") is not True)
    missed = enabled and moment >= day_due and (not scheduled_success or last_run_failed)
    audit = None
    if startup_audit or schedule_started_at is not None:
        try:
            started = datetime.fromisoformat(schedule_started_at.replace("Z", "+00:00"))
            require(started.tzinfo is not None and started.utcoffset() is not None,
                    "aware_schedule_start_required")
        except (AttributeError, TypeError, ValueError):
            raise BackupOpsError("aware_schedule_start_required") from None
        # A boot before the watch deadline reviews yesterday. The ordinary
        # watch reviews today, and only owes a backup if bound by 03:30.
        latest_due = day_due - timedelta(days=1) if startup_audit and moment < day_due else day_due
        expected = enabled and started <= latest_due.replace(hour=3, minute=30)
        due_day = latest_due.date().isoformat()
        success = any(r.get("scheduled_day") == due_day for r in verified_scheduled)
        missed = moment >= latest_due and expected and (not success or last_run_failed)
        if startup_audit:
            audit = {"scheduled_day": due_day, "schedule_expected": expected,
                     "scheduled_success": success, "missed_or_failed": missed,
                     "binding_confirmed_at": schedule_started_at}
    live_identity_verified = database_identity_verified = False
    if live:
        driver = docker or Docker(profile["docker_bin"], profile.get("docker_endpoint"))
        bound_services(profile, driver)
        bound_volumes(profile, driver)
        live_identity_verified = True
        if site_probe is not None:
            check_worker_status(profile, site_probe(profile))
            database_identity_verified = True
    pending = [
        record["session_id"]
        for _, record in history
        if record.get("stage") not in ("finished", "preflight-failed")
    ]
    cleanup_pending = [
        record["session_id"]
        for _, record in history
        if record.get("private_cleanup_pending")
    ]
    total = sum(
        sum(item.stat().st_size for item in path.iterdir()) for path, _ in bundles
    )
    retention = safe_retention_plan(profile, now=now)
    pending_root = no_links(Path(profile["store_dir"]) / "pending")
    pending_publications = 0
    if pending_root.exists():
        require(pending_root.is_dir(), "pending_directory_invalid")
        for entry in pending_root.iterdir():
            no_links(entry)
            require(entry.is_dir(), "pending_member_invalid")
            pending_publications += 1
    free_bytes = min(shutil.disk_usage(path).free for path in (state, Path(profile["store_dir"])))
    return {
        "format": "gift-ledger-backup-ops-status-v1",
        "schedule_enabled": enabled,
        "schedule": profile["schedule"],
        "project": profile["project"],
        "instance_id": profile["instance_id"],
        "server_uuid": profile["server_uuid"],
        "last_result": last,
        "scheduled_day": moment.date().isoformat(),
        "scheduled_attempts_today": len(scheduled),
        "scheduled_success_today": bool(scheduled_success),
        "scheduled_success_sessions": scheduled_success,
        "live_identity_verified": live_identity_verified,
        "database_identity_verified": database_identity_verified,
        "missed_or_failed": missed,
        "last_run_failed": last_run_failed,
        "needs_recovery": pending,
        "local_verified_bundles": len(bundles),
        "private_cleanup_pending": cleanup_pending,
        "artifact_bytes": total,
        "capacity_review_required": total >= 1024**3 or free_bytes < 512 * 1024**2,
        "host_free_bytes": free_bytes,
        "pending_publications": pending_publications,
        "cloud_unconfirmed_holds": len(retention["hold_ids"]),
        "cloud_upload": "未独立确认",
        "cloud_readback": "使用取回审计入口确认",
        "checked_at": (now or utcnow()).isoformat(),
        **({"startup_audit": audit} if startup_audit else {}),
    }


def describe(action, result):
    if action in {"run", "recover"}:
        if result.get("bundle_id"):
            return "本地加密备份及实际解密核验通过；本站已按原运行状态恢复。云端上传仍需单独确认。"
        return "本站已按原运行状态恢复；本次没有生成新的可靠备份，请查看原因。"
    if action in {"status", "watch"}:
        last = result.get("last_result") or {}
        return (
            "最近一轮："
            + ("成功" if last.get("success") else "尚无成功结果或本轮未完成")
            + "；本地可靠备份 "
            + str(result["local_verified_bundles"])
            + " 套；"
            + "未确认云端取回而暂留 "
            + str(result["cloud_unconfirmed_holds"])
            + " 套；"
            + ("存在漏跑或失败，请处理。" if result["missed_or_failed"] else "")
            + ("存在未完成维护，请按原会话恢复。" if result["needs_recovery"] else "")
            + ("数据库实时身份尚未核验；请使用通用计划入口检查。"
               if action == "watch" and not result.get("database_identity_verified") else "")
            + "百度云上传状态未独立确认。"
        )
    if action in {"audit-copy", "export-copy", "export"}:
        return (
            "成套文件回读校验通过；此步骤没有实际解密或恢复。移动盘须再用系统安全弹出。"
        )
    if action == "audit-external":
        return (
            "外部副本回读及实际解密核验通过；没有执行数据库恢复。云端来源由所有者指明。"
        )
    if action in {"retention-plan", "retention-apply"}:
        return (
            "按日14、周8、月12及重要/最后成功/外部未确认保护规则处理；不删除云端文件。"
        )
    return "操作完成。详情如下；本命令不会替您创建DSM计划任务。"


def main(argv=None):
    parser = argparse.ArgumentParser(description="往来有礼自动备份和加密离机保存")
    sub = parser.add_subparsers(dest="action", required=True)
    enroll = sub.add_parser("enroll", help="只登记已有安装，不启用定时任务")
    for name in (
        "installation",
        "status-file",
        "key-record",
        "workspace",
        "docker-bin",
    ):
        enroll.add_argument("--" + name, required=True)
    enroll.add_argument(
        "--preview-policy",
        choices=("block", "quarantine-expired", "fixed-window"),
        default="fixed-window",
    )
    enroll.add_argument("--human", action="store_true", help="显示中文摘要")
    enroll.add_argument("--target-manifest", help="新生成站点的完整目标合同")
    enroll.add_argument("--target-sha256", help="生成站点时核对的目标合同摘要")
    enroll.add_argument("--profile-output", help="另存本机备份配置；不覆盖已登记配置，站点及历史路径仍由目标合同限定")
    enroll.add_argument("--docker-endpoint", help="仅新站合同可绑定的本机Docker端点")
    for name in (
        "run",
        "status",
        "watch",
        "enable",
        "disable",
        "recover",
        "retention-plan",
        "export",
        "set-preview-policy",
        "retention-apply",
        "audit-external",
    ):
        cmd = sub.add_parser(name)
        cmd.add_argument("--profile", required=True)
        cmd.add_argument("--human", action="store_true", help="显示中文摘要")
        if name == "status":
            cmd.add_argument("--live", action="store_true", help="同时实时核验容器及卷身份")
        if name == "run":
            cmd.add_argument("--scheduled", action="store_true")
        if name == "recover":
            cmd.add_argument("--session", required=True)
        if name == "set-preview-policy":
            cmd.add_argument(
                "--policy",
                choices=("block", "quarantine-expired", "fixed-window"),
                required=True,
            )
        if name == "audit-external":
            cmd.add_argument("--bundle", required=True)
            cmd.add_argument(
                "--origin", choices=("cloud", "mobile", "copy"), default="copy"
            )
        if name == "export":
            cmd.add_argument("--backup", required=True)
            cmd.add_argument("--destination", required=True)
    audit = sub.add_parser("audit-copy")
    audit.add_argument("--bundle", required=True)
    audit.add_argument("--expected")
    audit.add_argument("--human", action="store_true")
    export_copy = sub.add_parser(
        "export-copy", help="Windows离机成套复制及回读，无需NAS安装档案"
    )
    export_copy.add_argument("--bundle", required=True)
    export_copy.add_argument("--destination", required=True)
    export_copy.add_argument("--human", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "enroll":
            profile_path = create_profile(
                args.installation,
                args.status_file,
                args.key_record,
                args.workspace,
                args.docker_bin,
                profile_path=args.profile_output,
                preview_policy=args.preview_policy,
                target_manifest=args.target_manifest,
                target_sha256=args.target_sha256,
                docker_endpoint=args.docker_endpoint,
            )
            result = {"profile": str(profile_path), "schedule_enabled": False}
        elif args.action == "audit-copy":
            expected = read_json(args.expected) if args.expected else None
            result = backup_export.audit_copy(args.bundle, expected)
        elif args.action == "export-copy":
            result = backup_export.export_bundle(args.bundle, args.destination)
        else:
            profile = validate_profile(read_json(args.profile))
            if args.action in ("run", "recover"):
                result = run(
                    profile,
                    mode="recover"
                    if args.action == "recover"
                    else ("scheduled" if args.scheduled else "manual"),
                    session_id=getattr(args, "session", None),
                )
            elif args.action in ("status", "watch"):
                result = status(profile, live=args.action == "watch" or getattr(args, "live", False))
            elif args.action == "retention-plan":
                result = safe_retention_plan(profile)
            elif args.action == "retention-apply":
                with host_lock(profile["state_dir"]):
                    require(
                        not status(profile)["needs_recovery"],
                        "previous_session_requires_recovery",
                    )
                    result = apply_safe_retention(profile)
            elif args.action == "set-preview-policy":
                with host_lock(profile["state_dir"]):
                    require(
                        not status(profile)["needs_recovery"],
                        "previous_session_requires_recovery",
                    )
                    profile["preview_policy"] = args.policy
                    save_json(args.profile, validate_profile(profile))
                    installation_notes(args.profile, profile)
                    result = {"preview_policy": args.policy}
            elif args.action == "export":
                require(ID.fullmatch(args.backup), "backup_id_invalid")
                result = backup_export.export_bundle(
                    Path(profile["store_dir"]) / "artifacts" / args.backup,
                    args.destination,
                )
            elif args.action == "audit-external":
                result = audit_external(profile, args.bundle, origin=args.origin)
            elif args.action == "enable":
                require(not profile.get("target_contract"), "generated_site_requires_active_binding")
                bound_services(profile, Docker(profile["docker_bin"], profile.get("docker_endpoint")))
                current_status = status(profile)
                require(
                    not current_status["needs_recovery"]
                    and current_status["last_result"]
                    and current_status["last_result"].get("success"),
                    "successful_manual_acceptance_required",
                )
                save_json(
                    Path(profile["state_dir"]) / "schedule-enabled.json",
                    {"enabled_at": utcnow().isoformat(), "project": profile["project"]},
                )
                result = {
                    "enabled": True,
                    "meaning": "仅允许调度入口执行；仍须由所有者配置DSM任务",
                }
            else:
                target = no_links(Path(profile["state_dir"]) / "schedule-enabled.json")
                if target.exists():
                    target.unlink()
                result = {"enabled": False}
        if args.human:
            print(describe(args.action, result))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.action == "watch":
            return (
                1
                if (
                    result["missed_or_failed"]
                    or result["needs_recovery"]
                    or result["capacity_review_required"]
                    or result["private_cleanup_pending"]
                    or result["pending_publications"]
                    or result.get("last_run_failed")
                    or (profile.get("target_contract") and not result.get("database_identity_verified"))
                )
                else 0
            )
        if args.action == "run" and result.get("outcome") in (
            "aborted-before-backup",
            "deferred-active-preview",
        ):
            return 3
        return 0
    except (
        BackupOpsError,
        backup_store.BackupStoreError,
        backup_export.BackupExportError,
        windows_path_budget.WindowsPathBudgetError,
    ) as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "code": error.code,
                    "说明": (
                        "Windows工作目录过深，后续备份文件会超过本版路径长度上限。"
                        "全新安装请选择更短的独立目录，例如 D:\\GiftLedger\\sites，再重新生成材料并预检；"
                        "已有站点不要直接改名或移动，应保留原目录按迁移流程处理。"
                        "本次没有为此修改系统长路径设置。"
                        if error.code == "windows_path_budget_exceeded" else
                        "操作未完成。保留记录，按手册查明原因；不要删除维护锁或失败记录。"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(
            '{"success":false,"code":"input_or_environment_invalid","说明":"输入或环境不符合要求，请保留现场。"}',
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
