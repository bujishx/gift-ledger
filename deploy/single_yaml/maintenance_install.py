"""Install or rescue one site from the release's maintenance-install.pyz on its Docker host."""
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

import maintenance_host as host
import owner_login_code as owner

BASE = Path("/root/.giftledger-maintenance")
PYTHON = "/usr/bin/python3"
SYSTEMCTL = "/usr/bin/systemctl"
ARCHIVE = "maintenance-install.pyz"
MEMBERS = ("maintenance_host.py", "ubuntu_certificate_timer.py", "owner_login_code.py", "dsm-backup.pyz")
MAX_BYTES = 8 * 1024**2
RESULT_FILES = {"backup": "result.json", "enrollment": "enrollment.json", "restore": "web-restore.json"}
WORKER = "maintenance_host.py"
UNIT_DIR = Path("/etc/systemd/system")
DSM = Path("/var/packages/ContainerManager/target/usr/bin/docker")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/root"}
need = host.require


def command(argv, *, text=False):
    result = subprocess.run([str(v) for v in argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=ENV, timeout=120)
    need(result.returncode == 0, "host_command_failed_" + Path(argv[0]).name)
    return result.stdout.decode("utf-8").strip() if text else result.stdout


def same_file(path, data, mode):
    """Only create a complete file or adopt previously installed identical root-owned bytes."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        # install.lock serializes project actions; atomically publish without replacing another file.
        # A fixed sibling also lets us finish the link/unlink pair after a power loss.
        tmp = path.with_name("." + path.name + ".giftledger-installing")
        try:
            fd = os.open(str(tmp), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(str(tmp), os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
            old = os.fstat(fd)
            if not (stat.S_ISREG(old.st_mode) and old.st_nlink == 1 and old.st_uid == 0 and not old.st_mode & 0o022):
                os.close(fd)
                raise host.HostError("untrusted_install_staging_file")
            os.ftruncate(fd, 0)
        try:
            with os.fdopen(fd, "wb") as out:
                os.fchmod(out.fileno(), mode)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(tmp, str(path), follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        host.sync_dir(path.parent)
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        tmp = path.with_name("." + path.name + ".giftledger-installing")
        if info.st_nlink == 2 and os.path.lexists(tmp) and host.identity(tmp.lstat()) == host.identity(info):
            os.unlink(tmp)
            host.sync_dir(path.parent)
            info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and (info.st_uid, info.st_gid) == (0, 0)
             and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1
             and info.st_size == len(data) and stream.read(len(data) + 1) == data, "installed_file_conflict")


def managed_dir(path):
    host.trusted_chain(path.parent)
    try:
        path.mkdir(mode=0o700)
        host.sync_dir(path.parent)
    except FileExistsError:
        pass
    host.trusted_chain(path)
    need(stat.S_IMODE(path.stat().st_mode) == 0o700, "host_install_directory_permissions")


def platform():
    if os.path.lexists("/etc.defaults/synoinfo.conf") or os.path.lexists("/var/packages/ContainerManager"):
        need(os.access(DSM, os.X_OK), "container_manager_not_ready")
        return "dsm", str(DSM)
    return "ubuntu", str(host.ubuntu_docker())


def read_package(package):
    for parent in package.parents:
        info = parent.lstat()
        sticky_tmp = parent in (Path("/tmp"), Path("/var/tmp")) and bool(info.st_mode & stat.S_ISVTX)
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
             (not info.st_mode & 0o022 or (parent != package.parent and sticky_tmp)), "untrusted_install_directory")
    fd = os.open(str(package), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
             and 0 < info.st_size <= MAX_BYTES, "untrusted_install_archive")
        data = stream.read(MAX_BYTES+1)
        need(len(data) == info.st_size, "untrusted_install_archive")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        allowed = set(MEMBERS) | {"__main__.py", "maintenance_install.py"}
        items = z.infolist()
        need(len(items) == len(allowed) and {i.filename for i in items} == allowed
             and sum(i.file_size for i in items) < MAX_BYTES, "install_archive_layout_invalid")
        return data, {n: z.read(n) for n in MEMBERS}


def verify_image_copy(driver, prepare, package):
    # docker cp to stdout is a tar. Inspect the single regular file without extracting anything.
    limit = len(package) + 65536
    def limit_export():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    with tempfile.TemporaryFile() as output:
        process = subprocess.run([*driver, "cp", prepare + ":/opt/gift-ledger-install/" + ARCHIVE, "-"],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            preexec_fn=limit_export, env=ENV, timeout=90)
        need(process.returncode == 0 and output.tell() <= limit, "prepare_install_archive_invalid")
        output.seek(0)
        with tarfile.open(fileobj=output, mode="r:") as tar:
            members = tar.getmembers()
            need(len(members) == 1 and members[0].isreg() and members[0].name in (ARCHIVE, "./" + ARCHIVE)
                 and members[0].size == len(package) and tar.extractfile(members[0]).read() == package,
                 "prepare_install_archive_mismatch")


def discover(project, instance, origin, station, package):
    binding, ip, port = owner.binding(project, instance, origin)
    kind, binary = platform()
    driver = [binary, "--host", owner.DOCKER_SOCKET]
    prepare = owner.service(driver, project, "prepare")
    owner.verify_prepare(prepare, station, binding, ip, port)
    need(prepare.get("Name") == "/" + project + "-prepare-1" and not prepare["State"].get("OOMKilled")
         and host.IMAGE.fullmatch(prepare["Image"]), "prepare_identity_mismatch")
    need(all(m["Destination"] in {"/station","/work/mysql","/tmp","/var/lib/mysql"} for m in prepare["Mounts"]),
         "unexpected_prepare_mount")
    verify_image_copy(driver, prepare["Id"], package)
    owner.verify_site_metadata(station, binding)
    owner.verify_services(owner.service(driver, project, "api"), owner.service(driver, project, "web"),
                          station, binding, ip, port)
    with owner.directory(station) as fd:
        inode = host.identity(os.fstat(fd))
    return {"project": project, "instance": instance, "origin": origin, "station": str(station),
            "station_inode": inode, "platform": kind, "image": prepare["Image"],
            "installer_sha256": hashlib.sha256(package).hexdigest()}


def read_record(root):
    host.trusted_chain(root)
    info = (root / "installation.json").lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
         and info.st_nlink == 1 and 0 < info.st_size < 4096, "invalid_host_install_record")
    record = json.loads((root / "installation.json").read_bytes())
    need(record["project"] == root.name and host.PROJECT.fullmatch(root.name)
         and host.IMAGE.fullmatch(record["image"]), "host_install_project_changed")
    return record


def bootstrap_mounts(record):
    """Seal only the empty bind sources created by Compose, before issuing any site credential."""
    with host.directories(Path(record["station"])) as station, contextlib.ExitStack() as opened:
        need(host.identity(os.fstat(station)) == record["station_inode"], "station_identity_changed")
        rows = []

        def child(parent, name, allowed):
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            fd = os.open(name, host.DIR | os.O_RDONLY, dir_fd=parent)
            opened.callback(os.close, fd)
            rows.append((fd, parent, name, allowed))
            return fd

        ipc = child(station, host.IPC, {"requests"})
        child(ipc, "requests", set())
        child(station, owner.OWNER, set())

        def validate(fd, parent, name, allowed):
            info = os.fstat(fd)
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            need(info.st_uid == 0 and host.identity(info) == host.identity(entry)
                 and set(os.listdir(fd)) <= allowed, "initial_maintenance_mount_already_in_use")

        for row in rows:
            validate(*row)
        for fd, parent, name, allowed in rows:
            # DSM uses chmod to switch new directories out of inherited ACL mode.
            os.fchmod(fd, 0o700)
            for acl in os.listxattr(fd):
                if acl in {"system.posix_acl_access", "system.posix_acl_default"}:
                    os.removexattr(fd, acl)
            need(not any("acl" in acl.lower() for acl in os.listxattr(fd))
                 and stat.S_IMODE(os.fstat(fd).st_mode) == 0o700, "unexpected_maintenance_mount_acl")
            validate(fd, parent, name, allowed)
            os.fsync(fd)


def install(root, args, package):
    data, members = read_package(package)
    record = discover(args.project, args.instance, args.origin, host.absolute(args.station), data)
    digest = hashlib.sha256(members["dsm-backup.pyz"]).hexdigest()
    record["archive_sha256"] = digest
    first = root / "installation.json"
    if first.exists():
        need(read_record(root) == record, "existing_site_or_version_differs")
    else:
        need(set(os.listdir(root)) == {"install.lock"}, "host_install_directory_in_use")
        host.atomic_file(first, host.encoded(record), 0o600)
    same_file(root / ARCHIVE, data, 0o400)
    for name, raw in members.items():
        same_file(root / name, raw, 0o400)
    cfg = {"archive": str(root / "dsm-backup.pyz"), "sha256": digest}
    archive = host.archive_module(cfg)
    if record["platform"] == "dsm":
        pkg = archive.target.Package.discover(backup_events=True)
        need(DSM.resolve() == (pkg.target / "usr/bin/docker").resolve(), "dsm_engine_changed")
        binary = DSM
    else:
        binary = host.ubuntu_docker()
    site = archive.HostSite(archive.ops.Docker(binary), args.project, record["station"])
    profile = site.runtime_profile()
    need(site.coordinator["image"] == record["image"] == profile["maintenance_image"], "prepare_image_changed")
    state = root / "state"
    if (state / "binding.json").exists():
        bound = host.read_config(state)
        need(all(bound[k] == v for k, v in dict(cfg, project=args.project, instance=args.instance,
                 station=record["station"], station_inode=record["station_inode"]).items())
             and bound.get("platform", "dsm") == record["platform"], "existing_worker_binding_differs")
        with host.ipc_handles(bound) as handles:
            # init may have persisted its private binding immediately before losing power.
            try:
                owner_bytes = host.read_at(handles["base"], "owner.sha256", 65, expected_uid=0, expected_mode=0o640)
            except FileNotFoundError:
                owner_bytes = (bound["owner_digest"]+"\n").encode()
                host.publish(handles["base"], "owner.sha256", owner_bytes)
            need(owner_bytes == (bound["owner_digest"]+"\n").encode(), "owner_registration_mismatch")
    else:
        # No legacy bearer is issued. Only the site's one-time code or configured password can authorize the owner.
        # A fixed, unpredictable digest also lets the operator retry an interrupted bootstrap.
        secret_file = root / "bootstrap-digest"
        if not secret_file.exists():
            host.atomic_file(secret_file, (secrets.token_hex(32)+"\n").encode(), 0o600)
        if not (state / "init.json").exists():
            bootstrap_mounts(record)
        host.init(argparse.Namespace(project=args.project, state=str(state), instance=args.instance,
            station=record["station"], archive=cfg["archive"], sha256=digest, platform=record["platform"],
            owner_digest=secret_file.read_text().strip()))
    binding, _, _ = owner.binding(args.project, args.instance, args.origin)
    owner.store_binding(Path(record["station"]), binding, setup=True)
    if record["platform"] == "ubuntu":
        units(root, record)
    return start(root, record)


def service(project):
    return "giftledger-maintenance-" + project + ".service"


def units(root, record):
    state, image, p = root / "state", record["image"], record["project"]
    worker = command([PYTHON, "-I", root / WORKER, "systemd-unit", "--state", state, "--key-image", image])
    cert = "giftledger-certificate-" + p
    timer = root / "ubuntu_certificate_timer.py"
    files = {service(p): worker}
    for kind in ("service", "timer"):
        files[cert+"."+kind] = command([PYTHON, "-I", timer, kind+"-unit", "--state", state, "--key-image", image])
    host.trusted_chain(UNIT_DIR)
    for name, data in files.items():
        same_file(UNIT_DIR / name, data, 0o644)
    command([SYSTEMCTL, "daemon-reload"])
    for name in files:
        loaded = command([SYSTEMCTL, "show", "--property=FragmentPath", "--value", name], text=True)
        dropins = command([SYSTEMCTL, "show", "--property=DropInPaths", "--value", name], text=True)
        need(loaded == str(UNIT_DIR / name) and not dropins, "systemd_unit_definition_changed")
    command([SYSTEMCTL, "enable", "--now", cert+".timer"])
    command([SYSTEMCTL, "enable", service(p)])


def worker_args(root, record):
    return [PYTHON, "-I", str(root / WORKER), "serve", "--state", str(root / "state"), "--persistent",
            "--key-enrollment", "--web-restore", "--key-image", record["image"]]


def worker_pids(args):
    expected = [v.encode() for v in args]
    found=[]
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid == 0 and (proc / "cmdline").read_bytes().split(b"\0") == expected + [b""] and (
                    proc / "exe").resolve() == Path(PYTHON).resolve():
                found.append(int(proc.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return found


def accepting(root, record):
    pids = worker_pids(worker_args(root, record))
    need(len(pids) <= 1, "multiple_site_workers")
    if not pids:
        return False
    with (root / "state/worker.lock").open("rb") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return worker_pids(worker_args(root, record)) == pids
    return False


def dsm_ready(record, timeout=300):
    # DSM may deliver its boot event before the site's volume and Container Manager are available.
    station = Path(record["station"])
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            identity = host.identity(station.lstat())
        except FileNotFoundError:
            identity = None
        if identity is not None:
            need(identity == record["station_inode"], "station_directory_mismatch")
            if os.access(DSM, os.X_OK):
                try:
                    probe = subprocess.run([str(DSM), "--host", owner.DOCKER_SOCKET, "version", "--format",
                                            "{{.Server.Version}}"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, env=ENV, timeout=3)
                    if probe.returncode == 0 and probe.stdout.strip():
                        return
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
        time.sleep(2)
    raise host.HostError("container_manager_or_site_volume_not_ready")


def start(root, record):
    need(not any(row.get("state") == "needs_review" for row in outcomes(root).values()),
         "original_operation_requires_review")
    if accepting(root, record):
        return
    if record["platform"] == "ubuntu":
        command([SYSTEMCTL, "start", service(record["project"])])
    else:
        if not worker_pids(worker_args(root, record)):
            dsm_ready(record)
            logfile = root / "worker.log"
            if logfile.exists() and logfile.stat().st_size > 65536:
                os.replace(str(logfile), str(root / "worker.previous.log"))
            with logfile.open("ab") as out:
                subprocess.Popen(worker_args(root, record), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                 close_fds=True, start_new_session=True, env=ENV)
    until = time.monotonic() + 15
    while time.monotonic() < until:
        if accepting(root, record):
            need(not any(row.get("state") == "needs_review" for row in outcomes(root).values()),
                 "original_operation_requires_review")
            return
        time.sleep(.25)
    raise host.HostError("worker_not_listening_check_status")


def outcomes(root):
    state = root / "state"
    info = {}
    for role, name in RESULT_FILES.items():
        path = state / name
        if path.exists():
            row = json.loads(path.read_bytes())
            public = row.get("public", row)
            info[role] = {k: public[k] for k in ("operation_id","state","code","backup_id") if k in public}
    return info


def site_command(root, action, operation=None):
    args = [PYTHON, "-I", str(root / ARCHIVE), action, "--project", root.name]
    if operation:
        args.extend(("--operation", operation))
    return shlex.join(args)


def status(root, record):
    info = outcomes(root)
    active = accepting(root, record)
    for role, row in info.items():
        op = row.get("operation_id")
        if row.get("state") == "needs_review" and isinstance(op, str) and host.HEX_ID.fullmatch(op):
            row["review_command"] = site_command(root, "review-" + role, op)
    if not active and not any(row.get("state") == "needs_review" for row in info.values()):
        info["start_command"] = site_command(root, "start")
    info.update(project=record["project"], instance_id=record["instance"], origin=record["origin"],
                worker_listening=active)
    return info


def run(args):
    need(os.geteuid() == 0 and sys.flags.isolated, "root_and_isolated_python_required")
    os.umask(0o077)
    need(host.PROJECT.fullmatch(args.project), "invalid_project")
    package = Path(sys.argv[0]).absolute()
    root = BASE / args.project
    if args.action == "install":
        managed_dir(BASE)
        managed_dir(root)
    else:
        record = read_record(root)
        need(package == root / ARCHIVE and hashlib.sha256(package.read_bytes()).hexdigest() == record["installer_sha256"],
             "use_installed_site_tool")
        if args.action == "status":
            print(json.dumps(status(root, record), ensure_ascii=False, sort_keys=True))
            return 0
        if args.action == "login-code":
            return subprocess.call([PYTHON, "-I", str(root / "owner_login_code.py"), "issue", "--project", args.project,
                    "--instance", record["instance"], "--station", record["station"], "--origin", record["origin"]], env=ENV)
    lockfd = os.open(str(root / "install.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lockfd, "r+b") as lock:
        meta = os.fstat(lockfd)
        need(stat.S_ISREG(meta.st_mode) and (meta.st_uid, meta.st_gid, meta.st_nlink) == (0, 0, 1)
             and stat.S_IMODE(meta.st_mode) == 0o600, "untrusted_install_lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "install":
            install(root, args, package)
            record = read_record(root)
            print("本站维护服务已就绪。 " + record["origin"] + "/maintenance")
            if record["platform"] == "dsm":
                print("请在 DSM 任务计划保存一个由 root 在开机时触发的任务。任务脚本为")
                print(site_command(root, "start"))
        elif args.action == "start":
            start(root, record)
            print("本站维护服务已开始监听。")
        else:
            need(host.HEX_ID.fullmatch(args.operation), "invalid_operation")
            command_args = [PYTHON, "-I", root / WORKER, args.action, "--state", root / "state",
                            "--key-image", record["image"], "--operation", args.operation]
            need(subprocess.call([str(a) for a in command_args], env=ENV) == 0, "original_job_still_requires_review")
            start(root, record)
            print("本站维护服务已恢复接单。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装本站宿主维护服务、查询离线回单或收尾原任务")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "status", "login-code", "start", "review-backup", "review-enrollment", "review-restore"):
        sub=commands.add_parser(action)
        sub.add_argument("--project", required=True)
        if action=="install":
            for flag in ("station", "instance", "origin"):
                sub.add_argument("--"+flag, required=True)
        if action.startswith("review-"):
            sub.add_argument("--operation", required=True)
    args=parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        code = str(exc) if isinstance(exc, owner.RecoveryError) else host.machine_code(exc,"host_install_environment_invalid")
        print("本站维护操作未完成。请核对本站资料和离线状态。 " + code, file=sys.stderr)
        return 1
