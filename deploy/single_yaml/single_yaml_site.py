"""Initialize an owner-selected private site directory using the existing S011 installer."""

import configparser
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile


DATA = Path("/station/data")
ROLES = ("root_config", "app_secrets", "db_secrets", "tls_config", "mysql_data", "app_data")
ADDRESS = "网站地址.txt"
LEGACY_CERTIFICATE = "本站公开证书.crt"
ROOT_CERTIFICATE = "本站根证书.cer"


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


def check_directory():
    DATA.mkdir(mode=0o755, exist_ok=True)
    if not stat.S_ISDIR(DATA.lstat().st_mode):
        raise SiteError("站点的数据目录必须是实际目录")
    names = {child.name for child in DATA.iterdir()}
    public = {ADDRESS, LEGACY_CERTIFICATE, ROOT_CERTIFICATE}
    if names - (set(ROLES) | public):
        raise SiteError("所选数据目录还有其他文件，请选择本站专用的空目录")
    for role in names.intersection(ROLES):
        if not stat.S_ISDIR((DATA / role).lstat().st_mode):
            raise SiteError("站点数据目录不能包含链接或非目录文件")
    prepared = DATA / "root_config" / "layout" / "prepare-completed.json"
    if not prepared.is_file():
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


def publish_public(name, content):
    path = DATA / name
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
            raise SiteError("公开资料路径异常，请核对站点目录")
        if path.read_bytes() == content:
            return
    with NamedTemporaryFile(dir=DATA / "root_config" / "maintenance", delete=False) as output:
        tmp = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def main():
    project = os.environ["GIFTLEDGER_PROJECT"]
    url = site_url(project_host(project))
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
        identity = json.loads((DATA / "root_config/site/control/identity.json").read_bytes())
        if "profile" in identity["tls"]:
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding
            root = x509.load_pem_x509_certificate((DATA / "tls_config/ca-chain.pem").read_bytes())
            publish_public(ROOT_CERTIFICATE, root.public_bytes(Encoding.DER))
            print(f"本站根证书 SHA-256：{identity['tls']['root_sha256']}", flush=True)
        else:
            publish_public(LEGACY_CERTIFICATE, (DATA / "tls_config/fullchain.pem").read_bytes())
        publish_public(ADDRESS, (url + "/\n").encode())
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
