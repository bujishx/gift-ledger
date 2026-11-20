"""Publish just the identity of the running target and its verified restore time."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID

import single_yaml_site as site

DIRECTORY = ".giftledger-site-info"
NAME = "site.json"
PENDING = ".site-about.pending"
API_TARGET = "/run/giftledger-site-info"
RESTORE_TARGET = "/work/site-info"


def checked_directory(root, *, prepare=False):
    if not os.path.lexists(root) and prepare:
        site.create_private_directory(root)
    with site.opened_directory(root) as fd:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_gid != 0 or set(os.listdir(fd)) - {NAME, PENDING}:
            raise site.SiteError("本站标识目录归属或内容异常")
        if prepare and not os.listdir(fd):
            # DSM may create the API bind source early and inherit a share ACL.
            os.fchmod(fd, 0o750)
            for attr in os.listxattr(fd):
                if attr in {"system.posix_acl_access", "system.posix_acl_default"}:
                    os.removexattr(fd, attr)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o750 or any("acl" in attr.lower() for attr in os.listxattr(fd)):
            raise site.SiteError("本站标识目录权限异常")


def summary(binding, record, *, historical=None):
    state, time = "none", None
    if record is not None:
        state = "unknown"
        if isinstance(record, dict) and record.get("project") == binding["project"]:
            if record.get("stage") in ("rejected", "preflight", "eligible"):
                state = "none"
            result = record.get("result")
            target = result.get("target") if isinstance(result, dict) else None
            if (record.get("stage") == "completed" and isinstance(target, dict)
                    and result.get("full_database_and_app_files_verified") is True
                    and all(target.get(k) == binding[k] for k in ("project", "instance_id"))
                    and target.get("url") in (binding["origin"], binding["origin"] + "/")
                    and isinstance(record.get("operation"), str) and re.fullmatch("[0-9a-f]{32}", record["operation"])
                    and result.get("operation_id") == record["operation"]):
                state = "verified"
                time = record.get("verified_at")
                op = record.get("operation")
                if time is None and historical:
                    try:
                        proof = json.loads(site.read_regular(historical / op / "completed.json", 65536))
                        if (isinstance(proof, dict) and proof.get("action") == "restore" and proof.get("operation_id") == op
                                and proof.get("success") is True
                                and all(proof.get(k) == binding[k] for k in ("project", "instance_id"))):
                            time = proof.get("completed_at")
                    except (OSError, ValueError, site.SiteError):
                        pass
                if time:
                    try:
                        stamp = datetime.fromisoformat(time.replace("Z", "+00:00"))
                        time = stamp.astimezone(timezone.utc).isoformat() if stamp.utcoffset() is not None else None
                    except (AttributeError, TypeError, ValueError):
                        time = None
    return {**binding, "restore_state": state, "restored_at": time}


def publish(root, binding, record, *, prepare=False, historical=None):
    checked_directory(root, prepare=prepare)
    settings = summary(binding, record, historical=historical)
    if str(UUID(binding["instance_id"])) != binding["instance_id"]:
        raise site.SiteError("本站实例格式异常")
    with site.opened_directory(root) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            previous = json.loads(site.read_regular(NAME, 4096, parent=fd))
        except FileNotFoundError:
            previous = {}
        if not isinstance(previous, dict) or (previous and any(
                previous.get(k) != binding[k] for k in ("instance_id", "project"))):
            raise site.SiteError("已有站点标识属于另一个实例")
        # A completed target accepts at most one whole-site restore. A concurrent
        # prepare may have read an earlier journal snapshot before this lock.
        if previous.get("restore_state") == "verified" and not settings["restored_at"]:
            settings.update(restore_state="verified", restored_at=previous.get("restored_at"))
        if previous == settings:
            return
        try:
            old = os.stat(PENDING, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(old.st_mode) or old.st_uid != 0 or old.st_nlink != 1:
                raise site.SiteError("站点标识暂存文件异常")
            os.unlink(PENDING, dir_fd=fd)
        except FileNotFoundError:
            pass
        pending = os.open(PENDING, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=fd)
        with os.fdopen(pending, "w") as stream:
            json.dump(settings, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fchmod(stream.fileno(), 0o640)
            os.fsync(stream.fileno())
        os.rename(PENDING, NAME, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)




def update_for_restore(directory, project, record):
    """The journal and key cleanup remain authoritative if the display projection is unavailable."""
    try:
        if directory.exists():
            target = record["target"]
            binding = {"project": project, "instance_id": target["instance_id"], "origin": target["url"].rstrip("/")}
            publish(directory, binding, record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("关于本站的恢复显示暂未更新。下次 prepare 会重新核对原始恢复回执。", file=sys.stderr)


def prepare(station):
    project = os.environ["GIFTLEDGER_PROJECT"]
    identity = site.read_identity(project)
    binding = {"project": project, "instance_id": identity["instance_id"], "origin": site.site_url(site.project_host(project))}
    state = station / ".giftledger-backup-state/restore.json"
    try:
        record = json.loads(site.read_regular(state, 4 * 1024 * 1024))
    except FileNotFoundError:
        record = None
    except (ValueError, site.SiteError):
        record = {}
    publish(station / DIRECTORY, binding, record, prepare=True, historical=station / "data/root_config/maintenance")
