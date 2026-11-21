"""Single-site DSM and Ubuntu maintenance dispatcher. Run on the host with /usr/bin/python3 -I.

Only the presence of requests/backup.request crosses the application's write
boundary. Target, executable digest and authoritative progress stay with root.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID
import zipfile

FORMAT = "giftledger-maintenance-status-v1"
IPC = ".giftledger-maintenance"
REQUEST = "backup.request"
API_UID = 10001
NO_LINK = os.O_NOFOLLOW
DIR = os.O_DIRECTORY | NO_LINK
HEX_ID = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
RELEASE_IMAGES_LABEL = "io.giftledger.release-images"
RELEASE_SERVICES = {"prepare": "backup", "initialize": "installer", "db": "db", "api": "api", "web": "web"}
RELEASE_VERSION = re.compile(r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2,3}")


class HostError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise HostError(code)


def absolute(raw):
    path = Path(raw)
    require(path.is_absolute() and path != Path("/") and ".." not in path.parts, "invalid_host_path")
    return path


def trusted_chain(path):
    """The code and private state may only live under root-controlled directories."""
    for item in [*reversed(path.parents), path]:
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and info.st_mode & 0o022 == 0, "private_parent_not_trusted")


def identity(info):
    return [info.st_dev, info.st_ino]


def sync_dir(directory):
    fd = os.open(str(directory), os.O_RDONLY | DIR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def atomic_file(path, content, mode):
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=".maintenance-")
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, str(path))
        sync_dir(path.parent)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def exclusive_file(path, content, mode):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | NO_LINK, mode)
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), mode)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    sync_dir(path.parent)


def read_config(state):
    trusted_chain(state)
    require(stat.S_IMODE(state.stat().st_mode) == 0o700, "private_state_permissions_invalid")
    # Root owns this private tree, including both names used below.
    return json.loads((state / "binding.json").read_text(encoding="utf-8"))


def check_archive(path, expected):
    trusted_chain(path.parent)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0
            and SHA.fullmatch(expected), "archive_permissions_invalid")
    require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, "archive_digest_mismatch")


@contextlib.contextmanager
def directories(path, parent=None):
    """Open each component without following links, including mutable DSM share ancestors."""
    if parent is None:
        fd, parts = os.open("/", os.O_RDONLY | DIR), path.parts[1:]
    else:
        require(not path.is_absolute() and ".." not in path.parts, "invalid_station_subdirectory")
        fd, parts = os.dup(parent), path.parts
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def ipc_handles(config, *, setup=False):
    station = Path(config["station"])
    with directories(station) as root, contextlib.ExitStack() as stack:
        require(identity(os.fstat(root)) == config["station_inode"], "station_identity_changed")
        handles = {}
        for key, mode in (("base", 0o750), ("requests", 0o770), ("status", 0o750)):
            parent, name = (root, IPC) if key == "base" else (handles["base"], key)
            if setup:
                try:
                    os.mkdir(name, mode, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            fd = os.open(name, os.O_RDONLY | DIR, dir_fd=parent)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            require(info.st_uid == 0, "ipc_root_not_trusted")
            require(not any("acl" in name.lower() for name in os.listxattr(fd)), "ipc_extended_acl")
            if setup:
                require(not info.st_mode & 0o022 or
                        (key == "requests" and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == mode),
                        "ipc_directory_writable_by_others")
                os.fchown(fd, 0, 0)
                os.fchmod(fd, mode)
            else:
                require(identity(info) == config["ipc_inodes"][key] and info.st_gid == 0
                        and stat.S_IMODE(info.st_mode) == mode, "ipc_identity_changed")
            handles[key] = fd
        yield handles


def initialize_locked(state, cfg, owner_digest):
    pending = state / "init.json"
    if pending.exists():
        require(json.loads(pending.read_text(encoding="utf-8")) == dict(cfg, owner_digest=owner_digest),
                "incomplete_initialization_for_other_site")
    else:
        require(set(p.name for p in state.iterdir()) == {"worker.lock"}, "private_state_already_in_use")
        atomic_file(pending, encoded(dict(cfg, owner_digest=owner_digest)), 0o600)
    token_file = state / "owner.token"
    if owner_digest is None:
        if not token_file.exists():
            atomic_file(token_file, (secrets.token_hex(32) + "\n").encode("ascii"), 0o600)
        token = token_file.read_text(encoding="ascii").strip()
        require(SHA.fullmatch(token), "invalid_private_owner_token")
        owner_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    with ipc_handles(cfg, setup=True) as handles:
        require(set(os.listdir(handles["base"])) <= {"owner.sha256", "requests", "status"}
                and not os.listdir(handles["requests"]) and set(os.listdir(handles["status"])) <= {"result.json"},
                "ipc_already_in_use")
        previous = os.listdir(handles["status"])
        idle = result(cfg, None, "idle", "ready")
        if previous:
            require(read_at(handles["status"], "result.json", len(encoded(idle)), expected_mode=0o640,
                            expected_uid=0) == encoded(idle), "previous_site_status_exists")
        cfg["ipc_inodes"] = {key: identity(os.fstat(fd)) for key, fd in handles.items()}
        (state / "jobs").mkdir(mode=0o700, exist_ok=True)
        save(state, handles, idle)
        digest = (owner_digest + "\n").encode("ascii")
        try:
            existing = read_at(handles["base"], "owner.sha256", 65, expected_uid=0, expected_mode=0o640)
        except FileNotFoundError:
            existing = None
        require(existing is None or existing == digest, "existing_owner_registration_mismatch")
        cfg["owner_digest"] = owner_digest
        atomic_file(state / "binding.json", encoded(cfg), 0o600)
        if existing is None:
            publish(handles["base"], "owner.sha256", digest)


def init(args):
    state, station, archive = map(absolute, (args.state, args.station, args.archive))
    require(PROJECT.fullmatch(args.project) and str(UUID(args.instance)) == args.instance, "invalid_site_identity")
    require(args.owner_digest is None or SHA.fullmatch(args.owner_digest), "invalid_owner_digest")
    require(station != state and station not in state.parents and state not in station.parents,
            "private_state_overlaps_station")
    trusted_chain(state.parent)
    check_archive(archive, args.sha256)
    with directories(station) as fd:
        cfg = {"project": args.project, "station": str(station), "archive": str(archive),
               "sha256": args.sha256, "instance": args.instance, "station_inode": identity(os.fstat(fd))}
        if args.platform == "ubuntu":
            cfg["platform"] = "ubuntu"
    state.mkdir(mode=0o700, exist_ok=True)
    trusted_chain(state)
    require(stat.S_IMODE(state.stat().st_mode) == 0o700, "private_state_permissions_invalid")
    sync_dir(state.parent)
    with (state / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (state / "binding.json").exists():
            current = read_config(state)
            require(current.get("platform", "dsm") == args.platform and
                    all(current.get(k) == v for k, v in cfg.items()), "existing_site_binding_mismatch")
            with ipc_handles(current) as handles:
                digest = args.owner_digest
                if digest is None:
                    token = (state / "owner.token").read_text(encoding="ascii").strip()
                    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
                require(current["owner_digest"] == digest, "owner_registration_mismatch")
                data = (digest + "\n").encode("ascii")
                try:
                    require(read_at(handles["base"], "owner.sha256", 65, expected_uid=0, expected_mode=0o640)
                            == data, "owner_registration_mismatch")
                except FileNotFoundError:
                    publish(handles["base"], "owner.sha256", data)
        else:
            initialize_locked(state, cfg, args.owner_digest)


def read_at(directory, name, size, *, expected_uid=None, expected_mode=0o600):
    fd = os.open(name, os.O_RDONLY | NO_LINK | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size == size, "invalid_request_file")
        if expected_uid is not None:
            require(info.st_uid == expected_uid and stat.S_IMODE(info.st_mode) == expected_mode,
                    "invalid_request_owner")
        data = os.read(fd, size + 1)
        require(len(data) == size, "invalid_request_size")
        return data
    finally:
        os.close(fd)


def request_id(fd):
    try:
        raw = read_at(fd, REQUEST, 33, expected_uid=API_UID)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HostError("invalid_request_file") from exc
    require(re.fullmatch(b"[0-9a-f]{32}\n", raw), "invalid_request_id")
    return raw[:-1].decode("ascii")


def publish(directory, target, content):
    name = ".result-" + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NO_LINK, 0o640, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchown(out.fileno(), 0, 0)
            os.fchmod(out.fileno(), 0o640)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def result(config, operation, state, code, **extra):
    return {"format": FORMAT, "project": config["project"], "operation_id": operation,
            "state": state, "code": code, **extra}


def save(state, handles, value):
    # Commit host truth first, so a crash before or after the public rename can
    # be repaired without replaying the operation.
    raw = encoded(value)
    private = state / "result.json"
    changed = not private.exists() or private.read_bytes() != raw
    if changed:
        atomic_file(private, raw, 0o600)
        if value["state"] in ("succeeded", "failed", "needs_review"):
            try:
                publish(handles["status"], "backup-observation.json", encoded({
                    "project": value["project"], "operation_id": value["operation_id"], "state": value["state"],
                    "observed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}))
            except OSError:
                pass  # The ordinary result below remains authoritative if the optional observation fails.
    publish(handles["status"], "result.json", raw)


def receipt(config, bundle, previous_bundle, archive, engine_session=None):
    station = Path(config["station"])
    state = station / ".giftledger-backup-state"
    backup_id = bundle.name
    require(HEX_ID.fullmatch(backup_id) and bundle == station / "backups" / "artifacts" / backup_id
            and backup_id != previous_bundle, "fresh_backup_receipt_missing")
    ops = archive.ops
    last = ops.read_json(state / "last-result.json")
    manifest = ops.backup_store.inspect_bundle(bundle)  # validates five files and both actual decrypt/dump receipts
    require(manifest["project"] == config["project"] and manifest["instance_id"] == config["instance"]
            and manifest["backup_operation_id"] == backup_id and last.get("project") == manifest["project"]
            and last.get("instance_id") == manifest["instance_id"] and last.get("server_uuid") == manifest["server_uuid"]
            and last.get("bundle_id") == backup_id and last.get("success") is True
            and last.get("services_restored") is True and last.get("local_verified") is True
            and last.get("private_cleanup_pending") is False
            and (engine_session is None or last.get("session_id") == engine_session), "backup_receipt_mismatch")
    return manifest


def archive_module(cfg):
    archive_path = Path(cfg["archive"])
    check_archive(archive_path, cfg["sha256"])
    # The host starts this standalone file with python -I. The only added code
    # path is the immutable root-owned candidate archive with the approved hash.
    if str(archive_path) not in sys.path:
        sys.path.insert(0, str(archive_path))
    import dsm_backup
    require(str(dsm_backup.__file__).startswith(str(archive_path) + "/"), "unexpected_archive_module")
    return dsm_backup


def ubuntu_docker():
    # A DSM upgrade or a missing vendor binary must never silently switch lifecycle providers.
    require(sys.platform == "linux" and not os.path.lexists("/var/packages/ContainerManager")
            and not os.path.lexists("/etc.defaults/synoinfo.conf"), "ubuntu_host_required")
    path = Path("/usr/bin/docker")
    trusted_chain(path.parent)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and os.access(path, os.X_OK), "ubuntu_docker_not_trusted")
    return path


def release_metadata(labels):
    raw = labels.get(RELEASE_IMAGES_LABEL)
    if raw in (None, ""):
        return None  # Development Compose and older releases have no published digest inventory.
    require(isinstance(raw, str) and len(raw) < 2048, "invalid_release_image_manifest")
    manifest = json.loads(raw)
    require(isinstance(manifest, dict) and set(manifest) == {"namespace", "version", "digests"}
            and manifest["namespace"] == "zhjido" and isinstance(manifest["version"], str)
            and len(manifest["version"]) <= 128 and RELEASE_VERSION.fullmatch(manifest["version"])
            and isinstance(manifest["digests"], dict) and set(manifest["digests"]) == set(RELEASE_SERVICES.values())
            and all(isinstance(v, str) and IMAGE.fullmatch(v) for v in manifest["digests"].values()),
            "invalid_release_image_manifest")
    return manifest


def tag_release_images(cfg, key_image):
    """Work entirely with local Docker objects. Validate the whole site before assigning any tag."""
    release = cfg["release_images"]
    project = cfg["project"]
    names = {project + "-" + service + "-1": service for service in RELEASE_SERVICES}
    binary = ("/var/packages/ContainerManager/target/usr/bin/docker"
              if cfg.get("platform", "dsm") == "dsm" else str(ubuntu_docker()))

    def docker(*args):
        return subprocess.check_output([binary, "--host", "unix:///var/run/docker.sock", *args], timeout=10,
                     stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env={"PATH": "/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C"})

    def canonical(reference):
        return reference[len("docker.io/"):] if reference.startswith("docker.io/") else reference

    containers = json.loads(docker("container", "inspect", *names))
    require({row["Name"] for row in containers} == {"/" + name for name in names}
            and len(containers) == len(names), "release_site_container_mismatch")
    plan = {}
    sources = {}
    for row in containers:
        config = row["Config"]
        labels = config["Labels"]
        service = names[row["Name"][1:]]
        require(labels.get("com.docker.compose.project") == project and labels.get("com.docker.compose.service") == service
                and labels.get("com.docker.compose.oneoff") == "False", "release_site_container_mismatch")
        if service == "prepare":
            require(row["Image"] == key_image and release_metadata(labels) == release
                    and [m.get("Source") for m in row["Mounts"] if m["Destination"] == "/station" and m["Type"] == "bind"]
                    == [cfg["station"]], "release_prepare_binding_changed")
        if service == "api":
            require("GIFTLEDGER_BUILD_VERSION=" + release["version"][1:] in config["Env"], "release_api_version_mismatch")
        role = RELEASE_SERVICES[service]
        repository = "zhjido/giftledger-" + role
        digest = release["digests"][role]
        source = config["Image"]
        # DSM/Compose normally keep the fully qualified Hub name; offline acceptance uses a loopback registry.
        require(re.fullmatch(r"(?:(?:docker\.io|127\.0\.0\.1:[0-9]{1,5})/)?" + repository + "@" + digest, source)
                and IMAGE.fullmatch(row["Image"]), "release_source_mismatch")
        sources[canonical(source)] = row["Image"]
        plan[repository + ":" + release["version"]] = row["Image"]
    images = json.loads(docker("image", "inspect", *sources))
    require(len(images) == len(sources), "release_digest_mismatch")
    for (source, image_id), image in zip(sources.items(), images):
        repo_digests = {canonical(ref) for ref in image.get("RepoDigests") or ()}
        require(image["Id"] == image_id and source in repo_digests, "release_digest_mismatch")
    installed = {}
    for line in docker("image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}").decode().splitlines():
        reference, image_id = line.split()
        reference = canonical(reference)
        if reference in plan:
            require(image_id == plan[reference], "release_tag_conflict_" + reference.split("giftledger-")[1].split(":")[0])
            installed[reference] = image_id
    for reference, image_id in plan.items():
        if reference not in installed:
            docker("image", "tag", image_id, reference)


def refresh_release_tags(state, cfg, key_image):
    if not cfg.get("release_images"):
        return
    report = {"version": cfg["release_images"]["version"]}
    try:
        with directories(Path(cfg["station"])) as station:
            require(identity(os.fstat(station)) == cfg["station_inode"], "station_identity_changed")
        # Every installed site on this engine uses the same root-controlled parent directory.
        # Serialize the preflight and writes across concurrently starting site workers.
        with directories(state.parent.parent) as base:
            fcntl.flock(base, fcntl.LOCK_EX | fcntl.LOCK_NB)
            tag_release_images(cfg, key_image)
        report["status"] = "verified"
    except (HostError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        report.update(status="pending", reason=machine_code(exc, "release_image_check_unavailable"))
    evidence = state / "image-tags.json"
    if not evidence.exists() or evidence.read_bytes() != encoded(report):
        atomic_file(evidence, encoded(report), 0o600)


def backup_binding(cfg, expected_station, key_image=None, engine_session=None, recover=False):
    if expected_station is not None:
        require(str(absolute(expected_station)) == cfg["station"], "station_directory_mismatch")
    archive = archive_module(cfg)
    options = {}
    if engine_session is not None:
        require(getattr(archive, "WEB_BACKUP_SESSION_BINDING", None) == 1, "backup_archive_upgrade_required")
        options = {"engine_session": engine_session, "recover": recover}
    platform = cfg.get("platform", "dsm")
    require(platform in ("dsm", "ubuntu"), "unknown_maintenance_platform")
    if platform == "ubuntu":
        require(isinstance(key_image, str) and IMAGE.fullmatch(key_image), "approved_key_image_required")
        binary = ubuntu_docker()
    else:
        binary = archive.target.Package.discover(backup_events=True).target / "usr/bin/docker"
    docker = archive.ops.Docker(binary)
    host = archive.HostSite(docker, cfg["project"], cfg["station"])
    os.environ.update(host.settings)
    ip = archive.site.single_yaml_site.project_host(cfg["project"])
    profile = host.profile()
    require(profile["instance_id"] == cfg["instance"], "maintenance_instance_mismatch")
    if platform == "ubuntu":
        require(profile["maintenance_image"] == key_image, "approved_backup_image_mismatch")
        archive.site.check_web_endpoint(docker, profile["containers"]["web"]["id"], ip,
                                        host.settings["GIFTLEDGER_HTTPS_PORT"], "backup_site_port_mismatch")
        run = partial(archive.site.run_backup, host, profile, docker, archive.site.Worker, **options)
    else:
        run = partial(archive.run, cfg["project"], cfg["station"], expected_instance=cfg["instance"], **options)
    return archive, host, profile, docker, run


def preflight(cfg, expected_station, key_image=None, engine_session=None):
    archive, host, _, _, run = backup_binding(cfg, expected_station, key_image, engine_session)
    state, _ = host.directories()
    require(all(row["stage"] in ("finished", "preflight-failed") and not row.get("private_cleanup_pending")
                for _, row in archive.ops.run_records(state)), "previous_backup_requires_review")
    last = state / "last-result.json"
    previous = archive.ops.read_json(last).get("bundle_id") if os.path.lexists(last) else None
    return archive, previous, run


def machine_code(exc, fallback):
    code = exc.args[0] if isinstance(exc, HostError) else getattr(exc, "code", None)
    if code is None and type(exc) is ValueError and len(exc.args) == 1:
        # DSM's vendor capability checks raise ValueError with short symbolic
        # identifiers, including CM_vendor_file_set_unreviewed.
        candidate = exc.args[0]
        if isinstance(candidate, str) and "_" in candidate:
            code = candidate
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code) else fallback


def consume(state, config, handles, operation, expected_station, key_image=None):
    path = state / "jobs" / operation
    claim = {"project": config["project"], "instance": config["instance"], "operation_id": operation,
             "engine_session": secrets.token_hex(16), "armed": False}
    try:
        exclusive_file(path, encoded(claim), 0o600)
    except FileExistsError:
        value = result(config, operation, "needs_review", "operation_already_claimed")
        save(state, handles, value)
        return value
    save(state, handles, result(config, operation, "running", "validating"))
    started = False
    try:
        archive, previous, run = preflight(config, expected_station, key_image, claim["engine_session"])
        claim.update(armed=True, previous_bundle=previous)
        atomic_file(path, encoded(claim), 0o600)
        save(state, handles, result(config, operation, "running", "backup_in_progress"))
        started = True
        bundle = run()
        value = verified_result(config, handles, operation, bundle, previous, archive, claim["engine_session"])
    except Exception as exc:
        code = machine_code(exc, "backup_failed" if started else "host_preflight_failed")
        preflight_capacity = (getattr(exc, "preflight_only", False) is True and code in {
            "backup_private_workspace_too_small", "backup_destination_space_low",
            "maintenance_memory_headroom_low", "maintenance_memory_unavailable"})
        stage = "needs_review" if (started and not preflight_capacity) or code == "previous_backup_requires_review" else "failed"
        value = result(config, operation, stage, code)
    save(state, handles, value)
    return value


def verified_result(cfg, handles, operation, bundle, previous, archive, session):
    manifest = receipt(cfg, bundle, previous, archive, session)
    value = result(cfg, operation, "succeeded", "backup_verified",
                   backup_id=manifest["backup_operation_id"], decryption_verified=True)
    if "owner-site.json" in os.listdir(handles["base"]):
        try:
            value["download_bytes"] = publish_download(handles["status"], bundle, manifest, archive.ops.backup_store)
        except (OSError, HostError, zipfile.LargeZipFile):
            value["download_unavailable"] = True
    return value


def review_backup(state, cfg, handles, operation, key_image):
    """Finish only the engine session assigned durably to the original web request."""
    require(isinstance(operation, str) and HEX_ID.fullmatch(operation) and
            isinstance(key_image, str) and IMAGE.fullmatch(key_image), "invalid_backup_review")
    latest = json.loads((state / "result.json").read_text())
    require(latest["operation_id"] == operation and latest["project"] == cfg["project"], "backup_review_id_mismatch")
    if latest["state"] in ("succeeded", "failed"):
        retire_marker(handles["requests"], operation)
        return latest
    require(latest["state"] in ("running", "needs_review") and request_id(handles["requests"]) == operation,
            "backup_review_marker_changed")
    raw = (state / "jobs" / operation).read_bytes()
    require(0 < len(raw) <= 4096, "backup_review_missing_session_binding")
    claim = json.loads(raw)
    session = claim.get("engine_session")
    require((claim.get("project"), claim.get("instance"), claim.get("operation_id")) ==
            (cfg["project"], cfg["instance"], operation) and isinstance(session, str) and HEX_ID.fullmatch(session)
            and type(claim.get("armed")) is bool, "backup_review_binding_changed")
    archive, site, profile, docker, resume = backup_binding(cfg, None, key_image, session, recover=True)
    require(profile["maintenance_image"] == key_image, "approved_backup_image_mismatch")
    engine, store = site.directories()
    if cfg.get("platform", "dsm") == "dsm" and os.path.lexists(engine / "dsm-events/active.json"):
        reports = engine / "dsm-events"
        archive.io.checked_directory(reports)
        with archive.ops.host_lock(reports):
            archive.reconcile_events(reports, archive.target.Package.discover(backup_events=True), docker)

    def inspect():
        owned = None
        for _, row in archive.ops.run_records(engine):
            if row["session_id"] == session:
                require(row["project"] == cfg["project"] and row["instance_id"] == cfg["instance"] and
                        row["mode"] == "manual" and claim["armed"], "backup_review_session_changed")
                owned = row
            else:
                require(row["stage"] in ("finished", "preflight-failed") and not row.get("private_cleanup_pending"),
                        "other_backup_requires_review")
        if owned and (owned["stage"] not in ("finished", "preflight-failed") or owned.get("private_cleanup_pending")):
            return None
        require(not archive.site.previous_worker(profile, docker, owned or {"session_id": session}),
                "backup_review_worker_still_present")
        if owned is None:
            # The engine writes its journal before starting a helper, taking a lease, or stopping a service.
            return result(cfg, operation, "failed", "backup_interrupted_before_execution")
        current = site.services(profile, docker)
        require(set(owned["original"]) == set(current) == set(archive.ops.SERVICES), "backup_review_services_changed")
        for role, original in owned["original"].items():
            require(all(current[role][field] == original[field] for field in
                        ("id", "image", "mounts", "project", "service", "running"))
                    and (not original["running"] or original["health"] != "healthy" or current[role]["health"] == "healthy"),
                    "backup_review_services_changed")
        if owned["stage"] == "preflight-failed":
            return result(cfg, operation, "failed", "backup_failed_before_site_stop")
        last = archive.ops.read_json(engine / "last-result.json")
        require(last.get("session_id") == session and last.get("project") == cfg["project"] and
                last.get("instance_id") == cfg["instance"] and last.get("server_uuid") == profile["server_uuid"]
                and last.get("services_restored") is True and last.get("private_cleanup_pending") is False,
                "backup_review_receipt_changed")
        if not owned.get("bundle_id"):
            require(last.get("success") is False and last.get("local_verified") is False and not last.get("bundle_id"),
                    "backup_review_receipt_changed")
            return result(cfg, operation, "failed", "backup_finished_without_bundle")
        require(HEX_ID.fullmatch(owned["bundle_id"]) and owned["backup_id"] == owned["bundle_id"],
                "backup_review_bundle_changed")
        return verified_result(cfg, handles, operation, store / "artifacts" / owned["bundle_id"],
                               claim["previous_bundle"], archive, session)

    with archive.ops.host_lock(engine):
        value = inspect()
    if value is None:
        # run_backup acquires the coordinator lock again and only resumes the named pending journal.
        try:
            resume()
        except Exception as exc:
            if machine_code(exc, "") != "backup_not_published":
                raise
        with archive.ops.host_lock(engine):
            value = inspect()
            require(value is not None, "backup_review_incomplete")
    save(state, handles, value)
    retire_marker(handles["requests"], operation)
    return value


def publish_download(directory, bundle, manifest, store):
    """Publish the last verified five-file bundle atomically to the read-only status mount."""
    backup_id = manifest["backup_operation_id"]
    hashes = dict(manifest["files"])
    require(set(hashes) == {"backup-" + backup_id + ".glgui.age", "backup.json", "verify.json"},
            "unexpected_download_members")
    hashes["BUNDLE.json"] = store.digest(store.canonical(manifest))
    sums = "".join(digest + "  " + name + "\n" for name, digest in sorted(hashes.items()))
    hashes["SHA256SUMS"] = store.digest(sums.encode("ascii"))
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with directories(bundle) as src, contextlib.ExitStack() as stack:
        files = {}
        total = 0
        for name in hashes:
            f = stack.enter_context(os.fdopen(os.open(name, flags, dir_fd=src), "rb"))
            meta = os.fstat(f.fileno())
            require(stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1, "bundle_member_changed")
            files[name] = f
            total += meta.st_size
        space = os.fstatvfs(directory)
        require(space.f_bavail * space.f_frsize > total + 1024 * 1024, "download_space_low")
        # Only the host owns this status directory, and the worker lock serializes publications.
        partial = "download.part"
        try:
            os.unlink(partial, dir_fd=directory)
        except FileNotFoundError:
            pass
        fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL | NO_LINK, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w+b") as output:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as package:
                    for name, f in files.items():
                        digest = hashlib.sha256()
                        with package.open(name, "w", force_zip64=True) as member:
                            for block in iter(lambda: f.read(1024 * 1024), b""):
                                digest.update(block)
                                member.write(block)
                        require(digest.hexdigest() == hashes[name], "bundle_changed_during_download")
                output.flush()
                os.fchown(output.fileno(), 0, 0)
                os.fchmod(output.fileno(), 0o640)
                os.fsync(output.fileno())
                size = output.tell()
            os.replace(partial, "download.zip", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            return size
        finally:
            try:
                os.unlink(partial, dir_fd=directory)
            except FileNotFoundError:
                pass


def retire_marker(directory, operation):
    # Directory is pinned; unlink never follows a request symlink. If the writer
    # replaced it during processing, preserve the new request for inspection.
    if request_id(directory) == operation:
        os.unlink(REQUEST, dir_fd=directory)
        os.fsync(directory)


def key_state_revision(cfg):
    """Detect commits by an external enrolled CLI/rotation/DSM task without running docker inspect each poll."""
    try:
        with directories(Path(cfg["station"])) as root:
            require(identity(os.fstat(root)) == cfg["station_inode"], "station_identity_changed")
            with directories(Path(".giftledger-backup-state"), root) as folder:
                stamps = []
                for name in ("site.json", "key-label.json", "key-change.json", "last-result.json",
                             "last-failed-backup-attempt.json"):
                    try:
                        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
                        stamps.append((info.st_ino, info.st_mtime_ns))
                    except FileNotFoundError:
                        stamps.append(None)
                return tuple(stamps)
    except (HostError, OSError):
        return None


def owner_snapshot(cfg):
    """Collect public facts from the named site's host volumes without exposing their contents."""
    with directories(Path(cfg["station"])) as root:
        require(identity(os.fstat(root)) == cfg["station_inode"], "station_identity_changed")
        return sample_owner_site(cfg, root)


def key_observation(cfg, root, owned_file, time_field):
    """The enrollment receipt is a past operation. The engine binding is the active key."""
    state = ".giftledger-backup-state"
    control = json.loads(owned_file("data/root_config/site/control", "identity.json", 65536))
    binding = (cfg["project"], cfg["instance"])
    require((control["project"], control["instance_id"]) == binding, "foreign_site")
    try:
        with directories(Path(state), root) as fd:
            info = os.fstat(fd)
            require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, "untrusted_engine")
        entry = json.loads(owned_file(state, "site.json", 4096))
    except FileNotFoundError:
        # An installed, bound station before its first enrollment has no site.json.
        # Leftover converted run history needs review instead of offering a new enrollment.
        try:
            with directories(Path(state + "/runs"), root) as folder:
                if os.listdir(folder):
                    return {"state": "unknown"}
        except FileNotFoundError:
            pass
        return {"state": "unregistered", "first": {"state": "none"}}
    recipient = entry["backup_recipient"]
    require((entry["project"], entry["instance_id"]) == binding and
            isinstance(recipient, str) and re.fullmatch(r"age1[0-9a-z]{58}", recipient) and
            str(UUID(entry["server_uuid"])) == entry["server_uuid"], "untrusted_key_binding")
    result = {"state": "registered", "recipient": recipient}
    rotated_backup = None
    try:
        label = json.loads(owned_file(state, "key-label.json", 4096))
        if (label["project"], label["instance_id"], label["recipient"]) == (*binding, recipient):
            result["filename"] = label.get("filename")
            rotated_backup = label.get("rotation_backup")
    except (HostError, KeyError, OSError, TypeError, ValueError):
        pass
    try:
        result["first"] = first_verified_backup(root, entry, owned_file, time_field)
    except (HostError, KeyError, OSError, TypeError, ValueError):
        result["first"] = {"state": "unknown"}
    try:
        if not rotated_backup:  # v1.8.0 recorded the first backup in its committed rotation journal.
            change = json.loads(owned_file(state, "key-change.json", 16384))
            if (change.get("project"), change.get("instance"), change.get("recipient"), change.get("stage")) == (
                    cfg["project"], cfg["instance"], recipient, "completed"):
                rotated_backup = change.get("backup")
        if rotated_backup and isinstance(rotated_backup, str) and HEX_ID.fullmatch(rotated_backup):
            # Both the label and the completed journal are root-owned and are only committed after proof.
            result["current_key_backup"] = {"state": "verified"}
            manifest = json.loads(owned_file(state + "/bundle-manifests", rotated_backup + ".json", 65536))
            require(all(manifest[k] == entry[k] for k in ("project", "instance_id", "server_uuid"))
                    and manifest["backup_operation_id"] == rotated_backup, "foreign_rotation_manifest")
            checked = time_field(manifest.get("completed_at"))
            if checked:
                result["current_key_backup"].update(at=checked, backup_id=rotated_backup)
    except (HostError, KeyError, OSError, TypeError, ValueError):
        pass
    return result


def first_verified_backup(root, entry, owned_file, time_field, *, latest=False):
    state = ".giftledger-backup-state"
    binding = {k: entry[k] for k in ("project", "instance_id", "server_uuid")}
    field = "last" if latest else "first"
    receipt = field + "-verified-backup.json"
    try:
        prior = json.loads(owned_file(state, receipt, 4096))
        require(all(prior[k] == v for k, v in binding.items()) and prior[field]["state"] == "verified",
                "foreign_first_backup")
        return prior[field]
    except FileNotFoundError:
        pass
    try:
        with directories(Path(state + "/runs"), root) as folder:
            sessions = os.listdir(folder)
    except FileNotFoundError:
        return {"state": "none"}
    verified = []
    complete = True
    for session in sessions:
        if not HEX_ID.fullmatch(session):
            complete = False
            continue
        try:
            run = json.loads(owned_file(state + "/runs/" + session, "run.json", 131072))
            if (run["project"], run["instance_id"]) != (entry["project"], entry["instance_id"]):
                continue
            require(run["session_id"] == session, "invalid_run_binding")
            bid = run.get("bundle_id")
            if run["stage"] != "finished" or not bid or run.get("private_cleanup_pending") is not False:
                continue
            require(HEX_ID.fullmatch(bid) and run["backup_id"] == bid, "invalid_verified_backup")
            checked = None
            try:
                manifest = json.loads(owned_file(state + "/bundle-manifests", bid + ".json", 65536))
                require(all(manifest[k] == v for k, v in binding.items())
                        and manifest["backup_operation_id"] == bid and manifest["verify_operation_id"] == run["verify_id"],
                        "foreign_backup_manifest")
                checked = time_field(manifest.get("completed_at"))
            except FileNotFoundError:
                # A finished host run only gets a bundle ID after actual decryption and verified publication.
                # The archive or the old timestamp may have been removed independently.
                pass
            verified.append((checked, bid))
        except (HostError, KeyError, OSError, TypeError, ValueError):
            complete = False
    if not verified:
        return {"state": "none" if complete else "unknown"}
    result = {"state": "verified"}
    if complete and all(moment for moment, _ in verified):
        moment, bid = max(verified) if latest else min(verified)
        result.update(at=moment, backup_id=bid)
    if complete and not latest:
        # One-time legacy backfill. Normal retention deletes ciphertext, never this site-bound receipt.
        with directories(Path(state), root) as directory:
            publish(directory, receipt, encoded(dict(binding, first=result)))
    return result


BACKUP_ALERT_DAYS = 7


def backup_alert_settings(state):
    try:
        with directories(state) as folder:
            size = os.stat("backup-alerts.json", dir_fd=folder, follow_symlinks=False).st_size
            require(0 < size <= 4096, "invalid_backup_alert_settings")
            raw = read_at(folder, "backup-alerts.json", size, expected_uid=0)
        settings = json.loads(raw)
    except FileNotFoundError:
        return {"days": BACKUP_ALERT_DAYS, "heartbeat": ""}
    require(isinstance(settings, dict) and type(settings.get("days")) is int and
            1 <= settings["days"] <= 365 and isinstance(settings.get("heartbeat"), str), "invalid_backup_alert_settings")
    return settings


def backup_health(data, last, web, days, now):
    """Joined host receipts, with separate freshness and last-attempt signals."""
    def instant(value):
        if isinstance(value, str):
            try:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if moment.tzinfo is not None and moment <= now:
                    return moment
            except ValueError:
                pass
        return None

    verified = instant(last.get("at"))
    attempted = instant(data.get("backup_checked_at"))
    if data.get("backup_state") == "succeeded" and data.get("decryption_verified") is True and attempted:
        verified = max(verified, attempted) if verified else attempted
    issues = []
    engine_failed = data.get("backup_state") in ("failed", "needs_review")
    web_failed = web.get("state") in ("failed", "needs_review")
    early = instant(data.get("failed_attempt_at"))
    observed = instant(web.get("observed_at"))
    if (engine_failed and (not verified or not attempted or attempted >= verified)) or (
            web_failed and observed and (not verified or observed >= verified)) or (
            early and (not verified or early > verified)):
        issues.append("failed")
    if verified:
        if (now - verified).total_seconds() >= days * 86400:
            issues.append("overdue")
    elif last.get("state") == "none":
        issues.append("never")
    else:
        issues.append("unknown")
    return {"days": days, "last_verified_at": verified.isoformat() if verified else None, "issues": issues}


def sample_owner_site(cfg, root):
    sampled = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = {"format": "giftledger-owner-overview-v1", "project": cfg["project"],
            "instance": cfg["instance"], "sampled_at": sampled}
    def owned_file(folder, name, limit):
        with directories(Path(folder), root) as parent:
            fd = os.open(name, os.O_RDONLY | NO_LINK | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                        and not info.st_mode & 0o022 and info.st_size <= limit, "snapshot_source_mismatch")
                return stream.read(limit + 1)

    def time_field(value):
        if isinstance(value, str) and len(value) <= 40:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat()
        return None

    try:
        data["backup_key"] = key_observation(cfg, root, owned_file, time_field)
    except (HostError, KeyError, OSError, TypeError, ValueError):
        data["backup_key"] = {"state": "unknown"}
    for key, folder in (("ledger_free_bytes", "data/mysql_data"), ("backup_free_bytes", "backups")):
        try:
            with directories(Path(folder), root) as fd:
                usage = os.fstatvfs(fd)
                data[key] = usage.f_bavail * usage.f_frsize
        except OSError:
            pass
    try:
        control = json.loads(owned_file("data/root_config/site/control", "identity.json", 65536))
        require((control["project"], control["instance_id"]) == (cfg["project"], cfg["instance"]), "foreign_site")
        chain = owned_file("data/tls_config", "fullchain.pem", 65536).decode("ascii")
        leaf, end, _ = chain.partition("-----END CERTIFICATE-----")
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf + end)).hexdigest()
        if digest == control["tls"]["certificate_sha256"]:
            data["certificate_expires_at"] = time_field(control["tls"]["not_after"])
    except (HostError, KeyError, OSError, TypeError, ValueError):
        pass
    state = ".giftledger-backup-state"
    try:
        with directories(Path(state), root) as fd:
            meta = os.fstat(fd)
            require(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o700, "untrusted_engine")
        try:
            last = json.loads(owned_file(state, "last-result.json", 65536))
        except FileNotFoundError:
            data["backup_state"] = "idle"
        else:
            session = last["session_id"]
            require(isinstance(session, str) and HEX_ID.fullmatch(session), "invalid_engine_session")
            run = json.loads(owned_file(state + "/runs/" + session, "run.json", 131072))
            require((run["project"], run["instance_id"], run["session_id"]) == (cfg["project"], cfg["instance"], session),
                    "foreign_site_backup")
            if last.get("success") is True:
                bid = last["bundle_id"]
                require((last["project"], last["instance_id"]) == (cfg["project"], cfg["instance"]) and
                        isinstance(bid, str) and HEX_ID.fullmatch(bid) and run["bundle_id"] == run["backup_id"] == bid and
                        run["stage"] == "finished" and last["services_restored"] is True and
                        last["local_verified"] is True and last["private_cleanup_pending"] is False, "unverified_backup")
                data.update(backup_state="succeeded", backup_id=bid, decryption_verified=True,
                            backup_checked_at=time_field(last.get("completed_at")))
            elif last.get("success") is False:
                data.update(backup_state="needs_review" if last.get("needs_recovery") else "failed",
                            backup_checked_at=time_field(last.get("at") or last.get("completed_at")))
                if last.get("local_verified") is False:
                    data["decryption_verified"] = False
    except (HostError, KeyError, OSError, TypeError, ValueError):
        pass
    latest = {"state": "unknown"}
    if data["backup_key"]["state"] == "unregistered":
        latest = {"state": "none"}
    elif data["backup_key"]["state"] == "registered":
        try:
            entry = json.loads(owned_file(state, "site.json", 4096))
            require((entry["project"], entry["instance_id"]) == (cfg["project"], cfg["instance"]), "foreign_site")
            try:
                failed = json.loads(owned_file(state, "last-failed-backup-attempt.json", 4096))
                if all(failed[key] == entry[key] for key in ("project", "instance_id", "server_uuid")):
                    data["failed_attempt_at"] = time_field(failed.get("attempted_at"))
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                pass
            latest = first_verified_backup(root, entry, owned_file, time_field, latest=True)
        except (HostError, KeyError, OSError, TypeError, ValueError):
            pass
    observation = {}
    try:
        item = json.loads(owned_file(IPC + "/status", "backup-observation.json", 4096))
        if item.get("project") == cfg["project"]:
            observation = item
    except (HostError, KeyError, OSError, TypeError, ValueError):
        pass
    data["backup_health"] = backup_health(data, latest, observation,
                                          cfg.get("backup_alert_days", BACKUP_ALERT_DAYS), datetime.now(timezone.utc))
    try:
        binary = ("/var/packages/ContainerManager/target/usr/bin/docker"
                  if cfg.get("platform", "dsm") == "dsm" else str(ubuntu_docker()))
        project = cfg["project"]
        template = ('{{.Name}} {{index .Config.Labels "com.docker.compose.project"}} '
                    '{{index .Config.Labels "com.docker.compose.service"}} {{.State.Status}} '
                    '{{if .State.Health}}{{.State.Health.Status}}{{end}}')
        name = project + "-web-1"
        run = subprocess.run([binary, "--host", "unix:///var/run/docker.sock", "inspect", "--type", "container",
                              "--format", template, name], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=2, env={"PATH": "/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C"})
        values = run.stdout.decode("ascii").split()
        if run.returncode == 0 and len(values) >= 4 and values[:3] == ["/" + name, project, "web"]:
            if values[3] != "running" or values[4:] == ["unhealthy"]:
                data["web_status"] = "unavailable"
            elif values[4:] == ["healthy"]:
                data["web_status"] = "ok"
    except (HostError, OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return data


def serve(state, wait, once, expected_station=None, key_enrollment=False, key_image=None, web_restore=False,
          *, persistent=False, stopping=None):
    cfg = read_config(state)
    needs_image = key_enrollment or cfg.get("platform") == "ubuntu"
    require((not needs_image and key_image is None) or
            (needs_image and isinstance(key_image, str) and IMAGE.fullmatch(key_image)),
            "approved_key_image_required")
    require(not web_restore or key_enrollment, "restore_requires_pinned_key_helper")
    require(1 <= wait <= 3600 and not (once and persistent), "invalid_idle_wait")
    review_exit = 78 if persistent else 1
    with (state / "worker.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HostError("host_worker_busy") from exc
        with ipc_handles(cfg) as handles:
            latest = json.loads((state / "result.json").read_text(encoding="utf-8"))
            if latest["state"] == "running":
                latest = result(cfg, latest["operation_id"], "needs_review", "host_worker_interrupted")
            save(state, handles, latest)
            if latest["state"] == "needs_review":
                return review_exit
            if key_enrollment:
                require(expected_station is None or str(absolute(expected_station)) == cfg["station"],
                        "station_directory_mismatch")
                archive = archive_module(cfg)
                import maintenance_enrollment as enroll
                require(str(enroll.__file__).startswith(cfg["archive"] + "/"), "unexpected_enrollment_module")
                host = sys.modules[__name__]
                record = enroll.current(host, state, handles, cfg)
            if web_restore:
                import maintenance_restore_queue as restore
                require(str(restore.__file__).startswith(cfg["archive"] + "/"), "unexpected_restore_queue_module")
                recovery = restore.current(host, state, handles, cfg)
            deadline = time.monotonic() + wait
            next_sample = next_key_check = next_image_check = 0.0
            last_key_revision = None
            while (persistent or time.monotonic() < deadline) and not (stopping and stopping.is_set()):
                if persistent and key_enrollment and time.monotonic() >= next_image_check:
                    refresh_release_tags(state, cfg, key_image)
                    next_image_check = time.monotonic() + 300
                if persistent and key_enrollment and time.monotonic() >= next_key_check:
                    revision = key_state_revision(cfg)
                    if revision != last_key_revision:
                        next_sample = 0.0
                        last_key_revision = revision
                    next_key_check = time.monotonic() + 3
                if persistent and key_enrollment and time.monotonic() >= next_sample:
                    try:
                        settings = backup_alert_settings(state)
                        publish(handles["status"], "overview.json", encoded(owner_snapshot(dict(cfg, backup_alert_days=settings["days"]))))
                    except (HostError, OSError, ValueError, TypeError, KeyError, OverflowError):
                        pass
                    next_sample = time.monotonic() + 120
                if web_restore:
                    if recovery["public"]["state"] == "needs_review":
                        return review_exit
                    if restore.tick(host, state, handles, cfg, recovery, archive, key_image):
                        next_sample = 0.0
                        if once and recovery["public"]["state"] in restore.TERMINAL:
                            return 0 if recovery["public"]["state"] == "succeeded" else 1
                        deadline = time.monotonic() + wait
                        continue
                    if recovery["public"]["state"] == "eligible":
                        # Reserve this site until the owner confirms or its preview expires.
                        deadline = time.monotonic() + wait
                        time.sleep(.25)
                        continue
                if key_enrollment:
                    if record["public"]["state"] == "needs_review":
                        return review_exit
                    if enroll.tick(host, state, handles, cfg, record, archive, key_image):
                        next_sample = 0.0
                        if once:
                            return 0 if record["public"]["state"] == "succeeded" else 1
                        deadline = time.monotonic() + wait
                        continue
                try:
                    operation = request_id(handles["requests"])
                except HostError as exc:
                    save(state, handles, result(cfg, None, "needs_review", machine_code(exc, "invalid_request")))
                    return review_exit
                if operation is None:
                    time.sleep(0.25)
                    continue
                if (operation == latest["operation_id"] and latest["state"] in ("succeeded", "failed")):
                    retire_marker(handles["requests"], operation)
                    continue
                latest = consume(state, cfg, handles, operation, expected_station, key_image)
                next_sample = 0.0
                if latest["state"] == "needs_review":
                    return review_exit
                retire_marker(handles["requests"], operation)
                if once:
                    return 0 if latest["state"] == "succeeded" else 1
                deadline = time.monotonic() + wait
    return 0


def unit_arg(value):
    """Quote one literal ExecStart argument for systemd's own parser (no shell)."""
    value = str(value)
    require(not any(ord(ch) < 32 or ord(ch) == 127 for ch in value), "invalid_service_argument")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def systemd_unit(state, key_image):
    cfg = read_config(state)
    require(cfg.get("platform") == "ubuntu" and PROJECT.fullmatch(cfg["project"]), "ubuntu_binding_required")
    require(isinstance(key_image, str) and IMAGE.fullmatch(key_image), "approved_key_image_required")
    check_archive(Path(cfg["archive"]), cfg["sha256"])
    script = absolute(__file__)
    trusted_chain(script.parent)
    info = script.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            "host_worker_script_not_trusted")
    args = ("/usr/bin/python3", "-I", script, "serve", "--state", state, "--persistent",
            "--key-enrollment", "--web-restore", "--key-image", key_image)
    command = "sudo /usr/bin/python3 -I " + shlex.quote(str(script))
    bound = " --state " + shlex.quote(str(state))
    unit_name = "giftledger-maintenance-" + cfg["project"] + ".service"
    return f"""[Unit]
Description=GiftLedger site maintenance ({cfg["project"]})
Requires=docker.service
After=docker.service
PartOf=docker.service

# 若备份中断且服务以 78 退出，先在终端运行下一行，查看原 operation_id：
# {command} status{bound}
# 将显示的原编号填到下一条命令。工具核验或收尾原备份，成功退出后重新启动本站维护服务：
# {command} review-backup{bound} --key-image {key_image} --operation 原编号 && sudo systemctl start {unit_name}

[Service]
Type=simple
User=root
UMask=0077
NoNewPrivileges=yes
ExecStart={" ".join(unit_arg(arg) for arg in args)}
Restart=on-failure
RestartSec=15s
RestartPreventExitStatus=78
KillMode=mixed
TimeoutStopSec=2h

[Install]
WantedBy=multi-user.target
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="群晖和 Ubuntu 的本站维护任务与离线状态查询")
    sub = parser.add_subparsers(dest="action", required=True)
    init_cmd = sub.add_parser("init")
    for name in ("project", "station", "instance", "archive", "sha256"):
        init_cmd.add_argument("--" + name, required=True)
    init_cmd.add_argument("--owner-digest", help="已由站主电脑生成的随机访问凭据的 SHA-256")
    init_cmd.add_argument("--platform", choices=("dsm", "ubuntu"), default="dsm", help="本站宿主机平台")
    run = sub.add_parser("serve")
    run.add_argument("--wait", type=int, default=180, help="等待新请求的最长秒数；已启动的备份会执行至收尾")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="处理一个请求后退出")
    mode.add_argument("--persistent", action="store_true", help="保持监听，停止服务时等正在执行的操作收尾")
    run.add_argument("--key-enrollment", action="store_true", help="接受本站站主网页提交的密封登记请求")
    run.add_argument("--web-restore", action="store_true", help="接受本站站主确认的整站恢复")
    run.add_argument("--key-image", help="已核准的本站备份工具镜像内容 ID")
    run.add_argument("--expect-station", help="额外核对宿主机预期的站点目录")
    review = sub.add_parser("review-enrollment", help="中断后只读核验本站已经登记的钥匙")
    review.add_argument("--operation", required=True, help="待核验的原登记编号")
    review.add_argument("--key-image", required=True, help="已核准的本站备份工具镜像内容 ID")
    status_cmd = sub.add_parser("status", help="从本机私有回执查看最新任务状态")
    backup_review = sub.add_parser("review-backup", help="只核对或收尾原编号的备份，确认后恢复接单")
    backup_review.add_argument("--operation", required=True)
    backup_review.add_argument("--key-image", required=True, help="已核准的本站备份工具镜像内容 ID")
    enrollment_status = sub.add_parser("enrollment-status", help="查看最近一次网页钥匙登记的公开回执")
    rescue = sub.add_parser("review-restore", help="按原编号核对或继续已确认的恢复")
    rescue.add_argument("--operation", required=True)
    rescue.add_argument("--key-image", required=True)
    restore_status = sub.add_parser("restore-status", help="查看本站网页恢复的离线回执")
    unit = sub.add_parser("systemd-unit", help="生成 Ubuntu 本站的 systemd 维护服务")
    unit.add_argument("--key-image", required=True, help="已核准的本站备份工具镜像内容 ID")
    for command in (init_cmd, run, status_cmd, backup_review, review, enrollment_status, rescue, restore_status, unit):
        command.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == 0 and sys.flags.isolated, "root_and_isolated_python_required")
        state = absolute(args.state)
        if args.action == "init":
            init(args)
            print("本站维护入口已登记。" if args.owner_digest else
                  "本站维护入口已登记。访问凭据保存在宿主机私有目录内的 owner.token。")
        elif args.action == "serve":
            stopping = threading.Event()
            if args.persistent:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: stopping.set())
            return serve(state, args.wait, args.once, args.expect_station, args.key_enrollment, args.key_image,
                         args.web_restore, persistent=args.persistent, stopping=stopping)
        elif args.action == "systemd-unit":
            print(systemd_unit(state, args.key_image), end="")
        elif args.action == "review-backup":
            cfg = read_config(state)
            with (state / "worker.lock").open("a+b") as lock, ipc_handles(cfg) as handles:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                value = review_backup(state, cfg, handles, args.operation, args.key_image)
                print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        elif args.action == "restore-status":
            read_config(state)
            require((state / "web-restore.json").is_file(), "web_restore_not_enabled")
            print(json.dumps(json.loads((state / "web-restore.json").read_bytes())["public"], ensure_ascii=False))
        elif args.action == "review-restore":
            cfg = read_config(state)
            require(HEX_ID.fullmatch(args.operation) and re.fullmatch("sha256:[0-9a-f]{64}", args.key_image),
                    "invalid_restore_review")
            with (state / "worker.lock").open("a+b") as lock, ipc_handles(cfg) as handles:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                archive = archive_module(cfg)
                import maintenance_restore_queue as restore
                require(str(restore.__file__).startswith(cfg["archive"] + "/"), "unexpected_restore_queue_module")
                value = restore.review(sys.modules[__name__], state, handles, cfg, archive, args.key_image, args.operation)
                print(json.dumps(value, ensure_ascii=False))
        elif args.action == "review-enrollment":
            cfg = read_config(state)
            require(HEX_ID.fullmatch(args.operation) and re.fullmatch("sha256:[0-9a-f]{64}", args.key_image),
                    "invalid_enrollment_review")
            with (state / "worker.lock").open("a+b") as lock, ipc_handles(cfg) as handles:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                archive = archive_module(cfg)
                import maintenance_enrollment as enroll
                require(str(enroll.__file__).startswith(cfg["archive"] + "/"), "unexpected_enrollment_module")
                value = enroll.review(sys.modules[__name__], state, handles, cfg, args.operation, archive, args.key_image)
                print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        elif args.action == "enrollment-status":
            read_config(state)
            require((state / "enrollment.json").is_file(), "key_enrollment_not_enabled")
            public = json.loads((state / "enrollment.json").read_bytes())["public"]
            print(json.dumps(public, ensure_ascii=False, sort_keys=True))
        else:
            read_config(state)
            print((state / "result.json").read_text(encoding="utf-8").strip())
    except Exception as exc:
        print("维护任务未完成。原因：" + machine_code(exc, "host_environment_invalid"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
