"""Provision one site's owner store or issue an expiring login code from its server."""
import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from uuid import UUID

IPC = ".giftledger-maintenance"
OWNER = ".giftledger-owner"
IPC_TARGET = "/run/giftledger-maintenance"
OWNER_TARGET = "/run/giftledger-owner"
CONFIG = "owner-site.json"
API_USER = 10001
DOCKER_SOCKET = "unix:///var/run/docker.sock"
DIR_FLAGS = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY

# Only the fixed API image code is imported. No database access or Docker socket is required.
# Docker exec attaches to the operator's terminal and does not use the container log driver.
ISSUE_IN_API = """
import json, os, sys
from pathlib import Path
if not all(os.isatty(fd) for fd in (0,1)) or (os.geteuid(),os.getegid()) != (10001,0):
    sys.exit("请在本站管理员交互终端领码。")
sys.path.insert(0,"/app/backend")
from app.services.maintenance_sessions import OwnerSessions
project, instance, origin, inode, config_inode = sys.argv[1:]
root, owner = Path("/run/giftledger-maintenance"), Path("/run/giftledger-owner")
expected = {"project":project,"instance_id":instance,"origin":origin}
try:
    config = root / "owner-site.json"
    if (str(owner.stat().st_ino) != inode or str(config.stat().st_ino) != config_inode
            or json.loads(config.read_bytes()) != expected
            or os.environ.get("GIFTLEDGER_PROJECT") != project
            or os.environ.get("GIFTLEDGER_MAINTENANCE_DIR") != str(root)
            or os.environ.get("GIFTLEDGER_OWNER_STATE_DIR") != str(owner)):
        raise ValueError()
    code = OwnerSessions(owner, **expected).issue_login_code()
except Exception:
    sys.exit("未能为指定站点签发登录码，请核对本站维护身份配置。")
print("站点维护  " + origin + "/maintenance")
print("项目  " + project + "    实例  " + instance)
print("一次性登录码  " + code, flush=True)
print("请在 10 分钟内使用。重新领码后，上一个未用码失效。")
"""


class RecoveryError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def binding(project, instance, origin):
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "项目名无效。")
    try:
        canonical = str(UUID(instance))
    except (TypeError, ValueError, AttributeError):
        raise RecoveryError("站点实例无效。") from None
    require(canonical == instance, "站点实例无效。")
    match = re.fullmatch(r"https://([0-9.]+)(?::([0-9]+))?", origin)
    require(match, "请使用本站维护说明中的 HTTPS 地址。")
    ip = str(ipaddress.IPv4Address(match[1]))
    port = int(match[2] or 443)
    suffix = "" if port == 443 else ":" + str(port)
    require(0 < port < 65536 and origin == "https://" + ip + suffix, "本站地址或端口无效。")
    return {"project": project, "instance_id": instance, "origin": origin}, ip, str(port)


def read_file(parent, name, *, private_config=False):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, "rb") as inp:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and info.st_nlink == 1 and not info.st_mode & 0o022 and info.st_size <= 65536,
                "站点身份文件无效。")
        if private_config:
            require(info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o640
                    and not any("acl" in k.lower() for k in os.listxattr(fd)), "维护配置权限不符。")
        data = inp.read(65537)
        require(len(data) == info.st_size, "站点身份文件已变化。")
        return data, info.st_ino


@contextlib.contextmanager
def directory(path):
    path = Path(path)
    require(path.is_absolute() and path != Path("/") and ".." not in path.parts, "站点目录无效。")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def verify_site_metadata(station, site):
    with directory(station / "data") as public, directory(station / "data/root_config/site/control") as control:
        raw, _ = read_file(control, "identity.json")
        identity = json.loads(raw)
        binding(site["project"], site["instance_id"], site["origin"])
        require(all(identity[k] == site[k] for k in ("project", "instance_id")), "实例、项目或网址与已准备的本站不符。")
        # This signed stand-alone helper is also run directly with python -I.
        name = f'{site["project"]}-{site["instance_id"][:8]}-网站地址.txt'
        try:
            address, _ = read_file(public, name)
        except FileNotFoundError:
            address, _ = read_file(public, "网站地址.txt")
        require(address.decode("utf-8").strip() == site["origin"] + "/",
                "实例、项目或网址与已准备的本站不符。")


def store_binding(station, site, *, setup):
    with directory(station) as root, contextlib.ExitStack() as stack:
        ipc = os.open(IPC, DIR_FLAGS, dir_fd=root)
        stack.callback(os.close, ipc)
        meta = os.fstat(ipc)
        require((meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)) == (0, 0, 0o750)
                and not any("acl" in k.lower() for k in os.listxattr(ipc)), "维护配置目录权限不符。")
        new_owner_dir = False
        if setup:
            try:
                os.mkdir(OWNER, 0o700, dir_fd=root)
                new_owner_dir = True
                os.fsync(root)
            except FileExistsError:
                pass
        fd = os.open(OWNER, DIR_FLAGS, dir_fd=root)
        stack.callback(os.close, fd)
        meta = os.fstat(fd)
        if new_owner_dir:
            require(meta.st_uid == 0 and not os.listdir(fd), "新建的站主身份目录已变化。")
            os.fchmod(fd, 0o700)
            for acl in os.listxattr(fd):
                if acl in {"system.posix_acl_access", "system.posix_acl_default"}:
                    os.removexattr(fd, acl)
            meta = os.fstat(fd)
        require(not any("acl" in k.lower() for k in os.listxattr(fd)), "站主身份目录含额外授权。")
        if setup and meta.st_uid == 0:
            require(not os.listdir(fd) and not meta.st_mode & 0o022, "已有非空或权限不符的身份目录。")
            os.fchmod(fd, 0o700)
            os.fchown(fd, API_USER, 0)
            meta = os.fstat(fd)
        require((meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)) == (API_USER, 0, 0o700),
                "站主身份目录权限不符。")
        try:
            raw, ino = read_file(ipc, CONFIG, private_config=True)
        except FileNotFoundError:
            require(setup, "站点尚未设置维护身份。")
            # Publish complete data without ever overwriting another station's binding.
            name = ".owner-site-" + secrets.token_hex(12)
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=ipc)
            try:
                with os.fdopen(out, "wb") as stream:
                    os.fchown(out, 0, 0)
                    os.fchmod(out, 0o640)
                    stream.write((json.dumps(site, sort_keys=True) + "\n").encode("utf-8"))
                    stream.flush()
                    os.fsync(out)
                try:
                    os.link(name, CONFIG, src_dir_fd=ipc, dst_dir_fd=ipc, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                os.unlink(name, dir_fd=ipc)
            os.fsync(ipc)
            raw, ino = read_file(ipc, CONFIG, private_config=True)
        require(json.loads(raw) == site, "维护身份配置属于另一个项目、实例或网址。")
        return meta.st_ino, ino


def docker_args():
    for path in ("/var/packages/ContainerManager/target/usr/bin/docker", "/usr/bin/docker", "/usr/local/bin/docker"):
        if os.access(path, os.X_OK):
            return [path, "--host", DOCKER_SOCKET]
    raise RecoveryError("未找到本机 Docker。")


def service(docker, project, role):
    command = [*docker, "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project,
               "--filter", "label=com.docker.compose.service=" + role, "--filter", "label=com.docker.compose.oneoff=False"]
    ids = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=15).splitlines()
    require(len(ids) == 1 and re.fullmatch(r"[0-9a-f]{64}", ids[0]), "无法核对本站唯一的 " + role + " 容器。")
    raw = subprocess.check_output([*docker, "inspect", "--type", "container", ids[0]], stderr=subprocess.DEVNULL, timeout=15)
    item, = json.loads(raw)
    labels = item["Config"]["Labels"]
    require(item["Id"] == ids[0] and labels["com.docker.compose.project"] == project
            and labels["com.docker.compose.service"] == role and labels["com.docker.compose.oneoff"] == "False",
            "容器不属于本站。")
    return item


def has_mount(item, target, source, writable):
    matches = [m for m in item["Mounts"] if m["Destination"] == target]
    return (len(matches) == 1 and matches[0]["Type"] == "bind" and matches[0]["Source"] == str(source)
            and matches[0]["RW"] is writable)


def verify_prepare(item, station, site, ip, port):
    env = item["Config"]["Env"]
    expect = {"GIFTLEDGER_PROJECT": site["project"], "GIFTLEDGER_SITE_IP": ip, "GIFTLEDGER_HTTPS_PORT": port}
    require(item["State"]["Status"] == "exited" and item["State"]["ExitCode"] == 0
            and item["Config"]["Cmd"] == ["prepare"] and item["HostConfig"]["ReadonlyRootfs"] is True
            and has_mount(item, "/station", station, True)
            and all([s for s in env if s.startswith(k + "=")] == [k + "=" + v] for k, v in expect.items()),
            "prepare 的挂载、项目或网址与本站不符。")


def verify_services(api, web, station, site, ip, port):
    require(api["State"]["Running"], "本站 API 正在停止。请先在服务器查询维护任务状态，待 API 恢复后再领码。")
    expect = {"GIFTLEDGER_PROJECT": site["project"], "GIFTLEDGER_SITE_IP": ip, "GIFTLEDGER_HTTPS_PORT": port,
              "GIFTLEDGER_MAINTENANCE_DIR": IPC_TARGET, "GIFTLEDGER_OWNER_STATE_DIR": OWNER_TARGET}
    env = api["Config"]["Env"]
    require(api["Config"]["User"] == "10001:0" and api["HostConfig"]["ReadonlyRootfs"] is True
            and not api["HostConfig"]["Privileged"]
            and all([s for s in env if s.startswith(k + "=")] == [k + "=" + v] for k, v in expect.items()),
            "API 用户或站点配置不符。")
    allowed = {IPC_TARGET, IPC_TARGET + "/requests", OWNER_TARGET, "/var/lib/gift-ledger", "/run/secrets"}
    info_target = "/run/giftledger-site-info"
    if any(m["Destination"] == info_target for m in api["Mounts"]):
        require(has_mount(api, info_target, station / ".giftledger-site-info", False), "本站标识挂载与该站点不符。")
        allowed.add(info_target)
    require(all((m["Type"] == "bind" and m["Destination"] in allowed)
                or (m["Type"] == "tmpfs" and m["Destination"] in {"/tmp", "/var/lib/mysql"})
                for m in api["Mounts"]), "API 含不属于本站的挂载。")
    require(has_mount(api, IPC_TARGET, station / IPC, False)
            and has_mount(api, IPC_TARGET + "/requests", station / IPC / "requests", True)
            and has_mount(api, OWNER_TARGET, station / OWNER, True)
            and has_mount(api, "/var/lib/gift-ledger", station / "data/app_data", True)
            and has_mount(api, "/run/secrets", station / "data/app_secrets", False),
            "API 的维护目录与该站点不符。")
    maps = [web["HostConfig"]["PortBindings"]]
    if web["State"]["Running"]:
        maps.append(web["NetworkSettings"]["Ports"])
    for value in maps:
        rows = [binding for key in ("443/tcp", "443/") for binding in (value.get(key) or [])]
        require(rows and all(row["HostIp"] in ("", "0.0.0.0", ip) and row["HostPort"] == port for row in rows),
                "Web 的实际 HTTPS 映射与本站不符。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="站主维护页一次性登录码")
    parser.add_argument("action", choices=("setup", "issue"))
    for key in ("project", "instance", "origin", "station"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == 0 and sys.flags.isolated, "请在本站服务器管理员终端使用维护说明中的完整命令。")
        if args.action == "issue":
            require(all(os.isatty(fd) for fd in (0, 1, 2)), "请在交互终端领码，勿重定向或写入任务日志。")
        site, ip, port = binding(args.project, args.instance, args.origin)
        station = Path(args.station)
        docker = docker_args()
        verify_prepare(service(docker, args.project, "prepare"), station, site, ip, port)
        verify_site_metadata(station, site)
        if args.action == "setup":
            store_binding(station, site, setup=True)
            print("本站维护身份目录与登录配置已就绪。")
            return 0
        api = service(docker, args.project, "api")
        verify_services(api, service(docker, args.project, "web"), station, site, ip, port)
        owner_ino, config_ino = store_binding(station, site, setup=False)
        return subprocess.call([*docker, "exec", "-it", "--user", "10001:0", api["Id"],
                                "/opt/venv/bin/python3.12", "-I", "-c", ISSUE_IN_API,
                                args.project, args.instance, args.origin, str(owner_ino), str(config_ino)])
    except (RecoveryError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, RecoveryError) else "无法核对本站身份资料或容器，请检查项目状态。"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
