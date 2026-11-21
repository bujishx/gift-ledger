"""Initialize an owner-selected private site directory using the existing S011 installer."""

import configparser
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile

import owner_guide


DATA = Path("/station/data")
ROLES = ("root_config", "app_secrets", "db_secrets", "tls_config", "mysql_data", "app_data")
ADDRESS = owner_guide.ADDRESS
LEGACY_CERTIFICATE = "本站公开证书.crt"
ROOT_CERTIFICATE = owner_guide.ROOT_CERTIFICATE
DOCUMENT_RECEIPT = ".giftledger-public-documents.json"
DOCUMENT_LIMIT = 512 * 1024


class SiteError(ValueError):
    """An operator-facing message that contains no private values."""


def project_host(project):
    if project != os.environ["GIFTLEDGER_PROJECT"] or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise SiteError("站点项目名与安装设置不符")
    address = ipaddress.IPv4Address(os.environ["GIFTLEDGER_SITE_IP"])
    if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise SiteError("请填写服务器实际的 IPv4 地址")
    return str(address)


def site_url(host):
    port = int(os.environ["GIFTLEDGER_HTTPS_PORT"])
    if not 1 <= port <= 65535:
        raise SiteError("HTTPS 端口无效")
    return f"https://{host}" if port == 443 else f"https://{host}:{port}"


def create_private_directory(path):
    """Prepare only a new child. Never alter ACLs on the containing share."""
    path.mkdir(mode=0o700)
    # On DSM the first chmod switches this new directory from inherited DSM ACL
    # to Linux permissions. A mode supplied only to mkdir does not do that.
    path.chmod(0o700)
    for acl in os.listxattr(path, follow_symlinks=False):
        if acl in {"system.posix_acl_access", "system.posix_acl_default"}:
            os.removexattr(path, acl, follow_symlinks=False)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise SiteError("新建的私有目录权限不符，请检查站点目录")


def prepare_maintenance_mounts():
    """DSM requires all API bind sources to exist before starting the API."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @contextmanager
    def child(parent, name, owners):
        try:
            fd = os.open(name, flags, dir_fd=parent)
            created = False
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                created = True
            except FileExistsError:
                created = False
            fd = os.open(name, flags, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if info.st_uid not in owners or (created and (info.st_uid != 0 or os.listdir(fd))):
                raise SiteError("本站维护挂载目录的所有者或内容异常")
            if created:
                # DSM can inherit a share ACL even when mkdir requested 0700.
                os.fchmod(fd, 0o700)
                for attr in os.listxattr(fd):
                    if attr in {"system.posix_acl_access", "system.posix_acl_default"}:
                        os.removexattr(fd, attr)
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700 or any("acl" in v.lower() for v in os.listxattr(fd)):
                    raise SiteError("新建的本站维护挂载目录含额外授权")
                os.fsync(parent)
            yield fd
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise SiteError("本站维护挂载目录在启动时发生变化")
        finally:
            os.close(fd)

    station = os.open(DATA.parent, flags)
    try:
        with child(station, ".giftledger-maintenance", {0}) as ipc:
            names = os.listdir(ipc)
            if names and "requests" not in names:
                raise SiteError("已有维护目录缺少请求文件夹，请核对本站资料")
            with child(ipc, "requests", {0}):
                pass
        # After host enrollment the existing owner store belongs to the API.
        with child(station, ".giftledger-owner", {0, 10001}):
            pass
    finally:
        os.close(station)


def check_directory():
    DATA.mkdir(mode=0o755, exist_ok=True)
    if not stat.S_ISDIR(DATA.lstat().st_mode):
        raise SiteError("站点的数据目录必须是实际目录")
    names = {child.name for child in DATA.iterdir()}
    public = {ADDRESS, LEGACY_CERTIFICATE, ROOT_CERTIFICATE, owner_guide.FILENAME}
    for role in names.intersection(ROLES):
        if not stat.S_ISDIR((DATA / role).lstat().st_mode):
            raise SiteError("站点数据目录不能包含链接或非目录文件")
    prepared = DATA / "root_config" / "layout" / "prepare-completed.json"
    established = prepared.is_file()
    if established:
        identity = read_identity(os.environ["GIFTLEDGER_PROJECT"])
        public.update(owner_guide.public_name(identity["project"], identity["instance_id"], name)
                      for name in (ADDRESS, ROOT_CERTIFICATE, owner_guide.FILENAME))
        public.add(owner_guide.GUIDES)
        # DSM creates this index beside an established station's documents.
        if "@eaDir" in names and stat.S_ISDIR((DATA / "@eaDir").lstat().st_mode):
            public.add("@eaDir")
    if names - (set(ROLES) | public):
        raise SiteError("所选数据目录还有其他文件，请选择本站专用的空目录")
    if not established:
        if names.intersection(public):
            raise SiteError("发现已有站点资料，请核对原数据目录")
        # Some Compose engines precreate empty bind sources. Refuse foreign
        # contents before creating anything else in this installation.
        missing = set(ROLES) - names
        if missing and any(any((DATA / role).iterdir()) for role in names.intersection(ROLES)):
            raise SiteError("数据目录已有文件，请核对原站点")
        for role in ROLES:
            if role in missing:
                create_private_directory(DATA / role)
        return
    if not set(ROLES).issubset(names):
        raise SiteError("原站点缺少数据目录，已停止启动")
    control = DATA / "root_config" / "site" / "control"
    completed = control / "initialize-completed.json"
    if (control / "initialize-failed.json").exists():
        raise SiteError("上次建库失败，请保留数据并核对安装日志")
    if completed.is_file():
        expected = json.loads(completed.read_text())["server_uuid"]
        identity = configparser.ConfigParser()
        identity.read(DATA / "mysql_data" / "auto.cnf")
        if identity.get("auto", "server-uuid", fallback="") != expected:
            raise SiteError("数据目录与已安装的数据库不匹配")
        if not (DATA / "app_data" / "imports").is_dir():
            raise SiteError("原应用数据目录不完整")


def publish_file(path, content, limit, mode, temporary_dir):
    with opened_directory(path.parent) as parent:
        try:
            if read_regular(path.name, limit, parent=parent) == content:
                return
        except FileNotFoundError:
            pass
        with NamedTemporaryFile(dir=temporary_dir, delete=False) as output:
            tmp = Path(output.name)
            try:
                output.write(content)
                output.flush()
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
                os.replace(tmp, path.name, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                tmp.unlink(missing_ok=True)


@contextmanager
def opened_directory(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def read_regular(path, limit, *, parent=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SiteError("站点公开资料路径异常，请核对站点目录") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise SiteError("站点公开资料或回退记录异常，请核对站点目录")
        with open(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) != info.st_size:
            raise SiteError("站点文件在核对期间发生变化")
        return data
    finally:
        os.close(fd)


def read_identity(project):
    path = DATA / "root_config/site/control/identity.json"
    if path.resolve() != path:
        raise SiteError("站点身份路径包含链接")
    identity = json.loads(read_regular(path, 65536))
    if identity.get("project") != project:
        raise SiteError("项目与已安装的站点身份不符")
    owner_guide.public_name(project, identity["instance_id"], ADDRESS)
    return identity


def public_path(project, instance, name):
    named = DATA / owner_guide.public_name(project, instance, name)
    return named if os.path.lexists(named) else DATA / name


def publish_public(identity, name, content):
    limit = DOCUMENT_LIMIT if name == owner_guide.FILENAME else 16384
    named = owner_guide.public_name(identity["project"], identity["instance_id"], name)
    scratch = DATA / "root_config/maintenance"
    publish_file(DATA / named, content, limit, 0o644, scratch)
    # Previously installed v1.7 host programs and scheduled tasks still read
    # the fixed names until those host tools are upgraded.
    if os.path.lexists(DATA / ADDRESS):
        publish_file(DATA / name, content, limit, 0o644, scratch)


def guide_names(project, instance):
    return {"安装前检查端口与网络.txt", owner_guide.public_name(project, instance, "如何彻底删除本站.txt")}


def check_guide_dir(directory, allowed):
    if (not stat.S_ISDIR(directory.lstat().st_mode)
            or any(child.name not in allowed for child in directory.iterdir())):
        raise SiteError("站主指南目录含其他文件或链接，请核对后继续")


def publish_guide(project, url, identity, *, refresh=False):
    # A routine prepare gets the Compose environment again, without the host-only
    # labels read by the operator's refresh command. Cache those paths privately.
    directory = owner_guide.host_path(os.environ.get("GIFTLEDGER_PROJECT_DIR"))
    station = owner_guide.station_path(directory, os.environ.get("GIFTLEDGER_SITE_DIR") or ".")
    config = owner_guide.host_path(os.environ.get("GIFTLEDGER_GUIDE_CONFIG_FILE"))
    # Keep host-specific paths outside the installer's strictly checked
    # certificate journal and outside an exported database backup.
    state = DATA.parent / ".giftledger-backup-state"
    cache = state / "owner-guide-paths.json"
    private_state = False
    if os.path.lexists(state):
        meta = state.lstat()
        private_state = (stat.S_ISDIR(meta.st_mode) and stat.S_IMODE(meta.st_mode) == 0o700
                         and meta.st_uid == os.geteuid())
    binding = {"project": project, "instance_id": identity["instance_id"]}
    if refresh:
        if not private_state or not all((directory, station, config)):
            raise SiteError("请复制本站说明中的完整目录核对命令，以读取宿主机路径")
        record = dict(binding, project_dir=str(directory), site_dir=str(station), config_file=str(config))
        publish_file(cache, json.dumps(record, ensure_ascii=False).encode(), 32768, 0o600, state)
    elif private_state and cache.is_file() and not cache.is_symlink() and cache.stat().st_size <= 32768:
        try:
            record = json.loads(cache.read_bytes())
            host_paths = [owner_guide.host_path(record[key]) for key in ("project_dir", "site_dir", "config_file")]
            if (all(record.get(key) == value for key, value in binding.items()) and all(host_paths)
                    and all(current in (None, saved) for current, saved in zip((directory, station, config), host_paths))):
                directory, station, config = host_paths
        except (ValueError, KeyError, TypeError, AttributeError):
            # A corrupt cache never prevents opening the site or host rediscovery.
            pass
    text = owner_guide.render(project, url, identity, project_dir=str(directory or ""),
                              site_dir=str(station or "."), config_file=str(config or ""),
                              owner_login=os.environ.get("GIFTLEDGER_OWNER_MAINTENANCE_GUIDE") == "1")
    publish_public(identity, owner_guide.FILENAME, text.encode())
    import station_guides
    guides = station_guides.documents(project, url, identity["instance_id"], project_dir=directory,
                                      site_dir=station, config_file=config)
    guide_dir = DATA / owner_guide.GUIDES
    allowed = guide_names(project, identity["instance_id"])
    if set(guides) != allowed:
        raise SiteError("本站指南清单与项目不符")
    guide_dir.mkdir(mode=0o755, exist_ok=True)
    check_guide_dir(guide_dir, allowed)
    scratch = DATA / "root_config/maintenance"
    files = {owner_guide.public_name(project, identity["instance_id"], owner_guide.FILENAME):
             hashlib.sha256(text.encode()).hexdigest()}
    for name, content in guides.items():
        publish_file(guide_dir / name, content, DOCUMENT_LIMIT, 0o644, scratch)
        files[owner_guide.GUIDES + "/" + name] = hashlib.sha256(content).hexdigest()
    receipt = dict(binding, files=files)
    publish_file(DATA.parent / DOCUMENT_RECEIPT, json.dumps(receipt, sort_keys=True).encode(), 8192, 0o600, scratch)


def rollback_public(project, url, identity):
    instance = identity["instance_id"]
    guide_dir = DATA / owner_guide.GUIDES
    receipt_path = DATA.parent / DOCUMENT_RECEIPT
    keys = [ADDRESS, owner_guide.FILENAME]
    if "profile" in identity["tls"]:
        keys.append(ROOT_CERTIFICATE)
    named = {key: DATA / owner_guide.public_name(project, instance, key) for key in keys}
    # On restart the old files may already exist and some verified new files
    # may have been removed. The original receipt remains until the last unlink.
    contents = {key: read_regular(public_path(project, instance, key), DOCUMENT_LIMIT) for key in keys}
    if contents[ADDRESS].decode().strip() != url + "/":
        raise SiteError("站点网址不符，已停止还原旧版文件名")
    if ROOT_CERTIFICATE in contents and hashlib.sha256(contents[ROOT_CERTIFICATE]).hexdigest() != identity["tls"]["root_sha256"]:
        raise SiteError("根证书与本站登记的指纹不符")
    if os.path.lexists(receipt_path):
        receipt = json.loads(read_regular(receipt_path, 8192))
        allowed = guide_names(project, instance)
        expected = {owner_guide.GUIDES + "/" + name for name in allowed} | {named[owner_guide.FILENAME].name}
        if (receipt.get("project") != project or receipt.get("instance_id") != instance
                or set(receipt["files"]) != expected):
            raise SiteError("回退记录与本站或本站文件清单不符")
        if os.path.lexists(guide_dir):
            check_guide_dir(guide_dir, allowed)
        for name, digest in receipt["files"].items():
            path = DATA / name
            if os.path.lexists(path) and hashlib.sha256(read_regular(path, DOCUMENT_LIMIT)).hexdigest() != digest:
                raise SiteError("站主文档已被编辑，请先另存并核对后再回退")
    elif not any(os.path.lexists(p) for p in [guide_dir, *named.values()]):
        return
    else:
        raise SiteError("缺少本站文档回退记录，请先用新版 prepare 核对本站")
    for key, content in contents.items():
        publish_file(DATA / key, content, DOCUMENT_LIMIT, 0o644, DATA / "root_config/maintenance")
    for path in named.values():
        path.unlink(missing_ok=True)
    if os.path.lexists(guide_dir):
        # Pin the directory while removing just the two known files.
        with opened_directory(guide_dir) as parent:
            for name in allowed:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.fsync(parent)
            guide_dir.rmdir()
    receipt_path.unlink()


def main():
    # Serialize prepare, refresh and downgrade across one-off Compose runs.
    # Lock the existing station directory without leaving a file in shared data.
    if sys.argv[1:] == ["initialize"]:
        return run()
    with opened_directory(DATA.parent) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return run()


def run():
    project = os.environ["GIFTLEDGER_PROJECT"]
    url = site_url(project_host(project))
    if sys.argv[1:] in (["refresh-guide"], ["rollback-public-files"]):
        identity = read_identity(project)
        instance = identity["instance_id"]
        if read_regular(public_path(project, instance, ADDRESS), 1024).decode().strip() != url + "/":
            raise SiteError("该维护说明的项目或网址与已安装的站点不符")
        if sys.argv[1:] == ["rollback-public-files"]:
            check_directory()
            rollback_public(project, url, identity)
            print("已恢复本站旧版公开文件名，可以按回退指南切换 YAML。")
            return 0
        publish_guide(project, url, identity, refresh=True)
        print(f"项目 {project} 的 data/{owner_guide.public_name(project, instance, owner_guide.FILENAME)} 已更新。")
        return 0
    if sys.argv[1:] == ["prepare"]:
        from prepare_volumes import prepare_volumes
        check_directory()
        prepare_volumes(
            project,
            root=DATA / "root_config",
            app_secrets=DATA / "app_secrets",
            tls=DATA / "tls_config",
            db_secrets=DATA / "db_secrets",
            data_roots={
                "mysql_data": DATA / "mysql_data",
                "app_data": DATA / "app_data",
                "mysql_socket": Path("/work/mysql"),
            },
        )
        identity = read_identity(project)
        if os.environ.get("GIFTLEDGER_OWNER_MAINTENANCE_GUIDE") == "1":
            if identity.get("project") != project:
                raise SiteError("本站维护挂载的项目与站点身份不符")
            prepare_maintenance_mounts()
        if "profile" in identity["tls"]:
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding
            root = x509.load_pem_x509_certificate((DATA / "tls_config/ca-chain.pem").read_bytes())
            publish_public(identity, ROOT_CERTIFICATE, root.public_bytes(Encoding.DER))
            print(f"本站根证书 SHA-256：{identity['tls']['root_sha256']}", flush=True)
        else:
            publish_file(DATA / LEGACY_CERTIFICATE, (DATA / "tls_config/fullchain.pem").read_bytes(),
                         16384, 0o644, DATA / "root_config/maintenance")
        publish_public(identity, ADDRESS, (url + "/\n").encode())
        publish_guide(project, url, identity)
        print(f"往来有礼网站地址：{url}/", flush=True)
        return 0
    if sys.argv[1:] == ["initialize"]:
        from initialize_volumes import run
        os.environ["GIFT_LEDGER_TRUSTED_ORIGINS"] = url
        return run(project)
    raise SiteError("未知的安装操作")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Database-driver exceptions can contain connection or SQL parameters.
        # Only show our own actionable messages and the legacy safe error codes.
        if isinstance(exc, SiteError):
            print(exc, file=sys.stderr)
        elif type(exc).__name__ in {"VolumePreparationError", "PreparationError", "InitializationError"}:
            print(f"站点安装检查未通过：{exc}", file=sys.stderr)
        else:
            print(f"站点安装未完成，请核对站点设置及数据目录。({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
