"""在 Mac 上把一份往来有礼备份恢复到独立的新站。"""

import argparse
from contextlib import contextmanager
from functools import partial
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import zipfile
from urllib.parse import urlsplit
from uuid import UUID

import mac_backup_key as keys
from mac_restore_bundle import backup_export, open_bundle, store


def choose_bundle():
    dialog = """
ObjC.import('AppKit');
var panel = $.NSOpenPanel.openPanel;
panel.title = '选择往来有礼五文件备份目录或 ZIP';
panel.canChooseDirectories = true;
panel.canChooseFiles = true;
panel.allowsMultipleSelection = false;
panel.allowedFileTypes = ['zip'];
panel.runModal === 1 ? ObjC.unwrap(panel.URL.path) : '';
"""
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", dialog],
                            capture_output=True, check=True)
    path = os.fsdecode(result.stdout).rstrip("\n")
    keys.check(path, "已取消选择备份。")
    return Path(path)


class Remote:
    def __init__(self, channel, args, scratch):
        self.channel, self.args, self.scratch = channel, args, scratch
        self.uploaded = ()

    def upload(self, bundle):
        self.uploaded = tuple(self.scratch + "/five/" + name for name in bundle.filenames)
        free = keys.run(self.channel, self.args.host, "LC_ALL=C df -Pk " + self.scratch
                        + " | awk 'NR==2 {print $4}'")
        needed = sum((bundle.folder / name).stat().st_size for name in bundle.filenames)
        keys.check(free.returncode == 0 and free.stdout.strip().isdigit()
                   and int(free.stdout) * 1024 >= needed + 5 * 4096, "目标主机临时目录可用空间不足。")
        for name in bundle.filenames:
            fd = os.open(bundle.folder / name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(fd)
                keys.check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "已核对的备份文件发生了变化。")
                path = shlex.quote(self.scratch + "/five/" + name)
                subprocess.run([*self.channel, "-T", self.args.host, "umask 077; set -C; cat > " + path],
                               stdin=source, stdout=subprocess.DEVNULL, check=True)

    def compose(self, *arguments, incoming=False):
        options = ["compose", "--project-directory", self.args.project_dir, "-f",
                   self.args.project_dir.rstrip("/") + "/docker-compose.yml", "-p", self.args.project,
                   "run", "--rm", "-T", "--no-deps"]
        if incoming:
            options.extend(["-v", self.scratch + "/five:/work/incoming:ro"])
        command = '"$d" ' + shlex.join([*options, *arguments])
        outfile = shlex.quote(self.scratch + "/result")
        script = ('umask 077; d=/var/packages/ContainerManager/target/usr/bin/docker; '
                  'if [ ! -x "$d" ]; then d=$(command -v docker) || exit 1; fi; '
                  'if "$d" info >/dev/null 2>&1; then ' + command + '; else sudo ' + command + '; fi > ' + outfile)
        if incoming:
            script += " && rm -f " + shlex.join(self.uploaded)
        # Leave stdin and stderr with the terminal for genuine system sudo prompts.
        # Keep structured stdout separate from Docker progress and prompts.
        subprocess.run([*self.channel, "-tt" if sys.stdin.isatty() else "-T", self.args.host, script], check=True)
        read = keys.run(self.channel, self.args.host, 'test "$(wc -c < ' + outfile + ')" -le 65536 && cat ' + outfile)
        keys.check(read.returncode == 0, "未能读回本站操作回执。")
        result = json.loads(read.stdout)
        keys.check(isinstance(result, dict), "本站回执格式无效。")
        return result


@contextmanager
def remote_files(channel, args, bundle):
    init = keys.run(channel, args.host, "umask 077 && mktemp -d /tmp/giftledger-restore-XXXXXXXX")
    scratch = init.stdout.decode("ascii").strip()
    keys.check(init.returncode == 0 and re.fullmatch(r"/tmp/giftledger-restore-[A-Za-z0-9]{8}", scratch),
               "无法在目标主机上建立私有临时目录。")
    try:
        created = keys.run(channel, args.host, "umask 077 && mkdir " + scratch + "/five")
        keys.check(created.returncode == 0, "无法建立本站备份文件投放目录。")
        yield Remote(channel, args, scratch)
    finally:
        inputs = [scratch + "/five/" + name for name in bundle.filenames]
        cleanup = keys.run(channel, args.host, "rm -f " + shlex.join([*inputs, scratch + "/result"])
                           + " && if [ -d " + scratch + "/five ]; then rmdir " + scratch + "/five; fi && rmdir " + scratch)
        keys.check(cleanup.returncode == 0, "待清理的本次 SSH 临时目录为 " + scratch)


def check_target(target, args, bundle):
    keys.check(isinstance(target, dict) and target.get("project") == args.project, "目标项目与所选项目不符。")
    for field in ("instance_id", "server_uuid"):
        value = target[field]
        keys.check(isinstance(value, str) and str(UUID(value)) == value and value != bundle.source["source_" + field],
                   "目标实例须独立于源站。")
    address = urlsplit(target["url"])
    keys.check(address.scheme == "https" and address.hostname and address.path in ("", "/")
               and not (address.username or address.password or address.query or address.fragment)
               and not re.search(r"[\s\x00-\x1f\x7f]", target["url"]), "目标回执中的网址无效。")
    return target


def check_result(result, args, bundle, operation, target):
    keys.check(result.get("full_database_and_app_files_verified") is True
               and result.get("source") == bundle.source and result.get("target") == target
               and result.get("backup_operation_id") == bundle.backup_id and result.get("operation_id") == operation
               and type(result.get("gift_count")) is int and result["gift_count"] == bundle.gift_count,
               "恢复回执的来源、目标或笔数未与所选备份相符。请保留本站回执。")
    check_target(target, args, bundle)
    return result


def copy_files(remote, action, bundle):
    result = remote.compose("restore-files", action, bundle.backup_id, bundle.manifest_sha256, incoming=action == "stage")
    keys.check(result.get("action") == action and result.get("backup_id") == bundle.backup_id
               and result.get("manifest_sha256") == bundle.manifest_sha256 and type(result.get("created")) is bool,
               "本站投放或回收回执与备份不符。")
    if action == "retire-owned" and result.get("preserved"):
        print(f"本站原有或已变动的副本保留在 restore-input/{bundle.backup_id}/。")


def ask_owner(message):
    print(message)
    try:
        return input("确认后输入“恢复”并回车，直接回车可退出：").strip() == "恢复"
    except (EOFError, KeyboardInterrupt):
        return False


@contextmanager
def cleanup_rejected(remote, bundle):
    try:
        yield
    except (Exception, KeyboardInterrupt):
        # Only the server's durable journal can establish that no writes started.
        try:
            now = remote.compose("restore", bundle.backup_id, "--status")
            if now["stage"] == "none" or (now["stage"] == "rejected" and now["bundle_sha256"] == bundle.manifest_sha256):
                copy_files(remote, "retire-owned", bundle)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            print("请用同一份备份及目标重新打开工具，以复核现场。", file=sys.stderr)
        raise


def recover(bundle, args, remote, enroll, confirm=ask_owner, *, status=None):
    def restore(*options):
        return remote.compose("restore", bundle.backup_id, *options)

    if status is None:
        status = restore("--status")
    stage = status["stage"]
    if stage not in ("none", "rejected"):
        keys.check(status.get("bundle_sha256") == bundle.manifest_sha256 and status.get("source") == bundle.source
                   and isinstance(status.get("operation_id"), str) and keys.OP.fullmatch(status["operation_id"]),
                   "未完成或已完成的操作与所选备份不符。")
    if stage == "completed":
        operation, target = status["operation_id"], status["target"]
        result = check_result(status["result"], args, bundle, operation, target)
        if status.get("temporary_key_cleanup_pending"):
            result = check_result(restore("--operation", operation), args, bundle, operation, target)
        copy_files(remote, "retire-owned", bundle)
        return result
    if stage in ("none", "rejected"):
        remote.upload(bundle)
        with cleanup_rejected(remote, bundle):
            copy_files(remote, "stage", bundle)
    else:
        keys.check(stage in ("preflight", "eligible", "stopping", "applying", "verified")
                   and status.get("identity_role") == "restore_identity", "请从原来的恢复方式继续这次操作。")
    started = stage in ("stopping", "applying", "verified")
    expected = ["--operation", status["operation_id"]] if stage in ("preflight", "eligible") else []
    with cleanup_rejected(remote, bundle):
        receipt = enroll() if stage in ("none", "rejected") else None
        proof = status if started else restore("--check-only", *expected)
    operation = proof["operation_id"]
    keys.check(isinstance(operation, str) and keys.OP.fullmatch(operation), "恢复操作编号无效。")
    if proof.get("already_restored") is True:
        target = proof["target"]
        check_result(proof, args, bundle, operation, target)
        result = check_result(restore("--operation", operation), args, bundle, operation, target)
        copy_files(remote, "retire-owned", bundle)
        return result
    keys.check(proof.get("source") == bundle.source and (started or (proof.get("eligible") is True
               and proof.get("full_decryption_verified") is True)), "所选备份与目标站预检回执不符。")
    target = check_target(proof["target"], args, bundle)
    if receipt is not None:
        keys.check(receipt.get("project") == args.project and receipt.get("instance_id") == target["instance_id"],
                   "临时原钥登记的目标实例与预检不符。")
    if not confirm(f"源站 {bundle.source['source_project']}，共 {bundle.gift_count} 笔。\n"
                   f"目标网址 {target['url']}\n目标项目 {args.project}，实例 {target['instance_id']}。"
                   + ("\n将继续上次已开始的恢复。" if started else "")):
        if not started:
            stopped = restore("--cancel", "--operation", operation)
            keys.check(stopped.get("cancelled") is True and stopped.get("operation_id") == operation, "取消回执不符。")
            copy_files(remote, "retire-owned", bundle)
        return None
    with cleanup_rejected(remote, bundle):
        completed = restore("--operation", operation)
    result = check_result(completed, args, bundle, operation, target)
    copy_files(remote, "retire-owned", bundle)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", nargs="?", type=Path, help="五文件目录或 ZIP，省略会打开文件选择框")
    parser.add_argument("--key", type=Path, help="源站的 age 原钥匙，省略会在交接时打开文件选择框")
    parser.add_argument("--host", required=True, help="SSH 用户名@目标主机")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--project-dir", required=True, help="目标站 YAML 所在的绝对路径")
    parser.add_argument("--project", default="gift-ledger", help="目标项目名")
    args = parser.parse_args()
    keys.remote_args(args.host, args.project_dir, args.project)
    keys.check(1 <= args.port <= 65535, "SSH 端口无效。")
    args.restore_key = True
    with open_bundle(args.backup or choose_bundle()) as bundle, keys.session(args.host, args.port) as channel:
        with remote_files(channel, args, bundle) as remote:
            status = remote.compose("restore", bundle.backup_id, "--status")
            enroll = None
            if status.get("stage") in ("none", "rejected"):
                path = args.key or keys.choose_key()
                age = keys.Age()
                secret, recipient = keys.custody(path, False, age)
                enroll = partial(keys.exchange, channel, args, secret, recipient, age)
            result = recover(bundle, args, remote, enroll, status=status)
    if result is None:
        print("已退出。")
    else:
        print(f"已核对 {result['gift_count']} 笔往来，网址 {result['target']['url']}")


if __name__ == "__main__":
    try:
        main()
    except (store.BackupStoreError, backup_export.BackupExportError, zipfile.BadZipFile) as exc:
        sys.exit("所选备份的五份文件或压缩包未通过核对，请使用完整副本。")
    except keys.EnrollmentError as exc:
        sys.exit(str(exc))
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        if type(exc) is ValueError:
            sys.exit(str(exc))
        sys.exit("恢复未完成。请核对终端提示，使用同一备份和目标再次打开本工具。")
