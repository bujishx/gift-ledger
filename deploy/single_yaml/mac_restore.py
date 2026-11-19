"""在 Mac 上把一份往来有礼备份恢复到独立的新站。"""

import argparse
from contextlib import contextmanager
import ctypes
from functools import partial
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
import zipfile
from urllib.parse import urlsplit
from uuid import UUID

import mac_backup_key as keys
from mac_restore_bundle import backup_export, open_bundle, store


@contextmanager
def waiting(step):
    print(f"\n{step}。请稍候。", file=sys.stdout if os.name == "nt" else sys.stderr, flush=True)
    if os.name != "nt":
        yield
        return
    # Refresh only the window title while SSH owns the input and password prompts.
    if not sys.stdout.isatty():
        yield
        return
    kernel = ctypes.windll.kernel32
    old = ctypes.create_unicode_buffer(32768)
    kernel.GetConsoleTitleW(old, len(old))
    prefix = "往来有礼 · " + step + " · 已用 "
    if not kernel.SetConsoleTitleW(prefix + "00:00"):
        yield
        return
    started = time.monotonic()
    finished = threading.Event()

    def update_elapsed():
        while not finished.wait(1):
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            kernel.SetConsoleTitleW(f"{prefix}{minutes:02d}:{seconds:02d}")

    ticker = threading.Thread(target=update_elapsed, daemon=True)
    ticker.start()
    try:
        yield
    finally:
        finished.set()
        ticker.join()
        kernel.SetConsoleTitleW(old.value)


def windows_dialog(title, body, flags):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dialog = user32.MessageBoxW
    dialog.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    dialog.restype = ctypes.c_int
    return dialog(None, body, title, flags | 0x10000 | 0x40000)  # MB_SETFOREGROUND | MB_TOPMOST


def announce_verified(result):
    if os.name != "nt":
        return
    body = (f"目标站已恢复，并已核对全站 {result['gift_count']} 笔往来。\n{result['target']['url']}\n\n"
            "点击「确定」回到主窗口查看临时文件的清理情况。若服务器要求再次授权，请按主窗口提示输入 sudo 密码。"
            "收尾后请用原网站账号登录核账。")
    print("\n恢复成功。" + body, flush=True)
    try:
        opened = windows_dialog("往来有礼 · 恢复成功", body, 0x40)  # MB_OK | MB_ICONINFORMATION
    except Exception:
        # A failed desktop notification must never interrupt remote cleanup.
        opened = False
    if not opened:
        print("成功提示窗口未能打开，继续收尾。", flush=True)


def choose_bundle():
    return keys.choose_local_path(backup=True)


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
            path_on_disk = bundle.folder / name
            before = path_on_disk.lstat()
            keys.check(stat.S_ISREG(before.st_mode) and not getattr(before, "st_file_attributes", 0) & 0x400,
                       "请选择普通备份文件。")
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(path_on_disk, flags)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(fd)
                keys.check(os.path.samestat(before, info) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                           "已核对的备份文件发生了变化。")
                path = shlex.quote(self.scratch + "/five/" + name)
                with keys.ssh_output(self.args.host) as streams:
                    subprocess.run([*self.channel, "-T", self.args.host, "umask 077; set -C; cat > " + path],
                                   stdin=source, stdout=subprocess.DEVNULL, check=True, **streams)

    def compose_script(self, arguments, *, output, incoming=False):
        options = ["run", "--rm", "-T", "--no-deps"]
        if incoming:
            options.extend(["-v", self.scratch + "/five:/work/incoming:ro"])
        command = keys.compose_command(self.args, [*options, *arguments], default_file=True)
        if arguments[0] == "restore-files":
            purpose = "投放本次备份" if arguments[1] == "stage" else "清理本次临时文件"
        elif "--check-only" in arguments:
            purpose = "执行恢复预检"
        elif "--status" in arguments:
            purpose = "检查目标站恢复状态"
        elif "--cancel" in arguments:
            purpose = "撤回未执行的恢复"
        else:
            purpose = "写入并核验账本，或继续本次恢复收尾"
        script = ('umask 077; d=/var/packages/ContainerManager/target/usr/bin/docker; '
                  'if [ ! -x "$d" ]; then d=$(command -v docker) || exit 1; fi; '
                  'if "$d" info >/dev/null 2>&1; then ' + command + '; else '
                  + keys.sudo_command(command, purpose) + '; fi > ' + output)
        if incoming:
            script += " && rm -f " + shlex.join(self.uploaded)
        return script

    def compose(self, *arguments, incoming=False):
        outfile = shlex.quote(self.scratch + "/result")
        script = self.compose_script(arguments, output=outfile, incoming=incoming)
        # Leave stdin and stderr with the terminal for genuine system sudo prompts.
        # Keep structured stdout separate from Docker progress and prompts.
        with keys.ssh_output(self.args.host, terminal=True) as streams:
            task = subprocess.run([*self.channel, keys.terminal_option(self.channel), self.args.host, script], **streams)
            keys.check(task.returncode == 0, "目标站操作未完成，请查看上方的步骤名称、授权或 Docker 错误，再用同一备份和目标重试。")
        return self.read_result(outfile)

    def read_result(self, outfile):
        read = keys.run(self.channel, self.args.host, 'test "$(wc -c < ' + outfile + ')" -le 65536 && cat ' + outfile)
        keys.check(read.returncode == 0, "未能读回本站操作回执。")
        result = json.loads(read.stdout)
        keys.check(isinstance(result, dict), "本站回执格式无效。")
        return result


# A fresh 0700 restore scratch directory belongs to a single invocation.
FINISH_FILES = ("finish-writing", "finish-verified", "finish-ack", "finish-abort")
GATE_EXPIRED = 74


def finish_in_one_session(remote, bundle, operation, verify, announce, check_retired, waiting):
    channel, host, scratch = remote.channel, remote.args.host, remote.scratch
    writing, ready, ack, abort = (shlex.quote(scratch + "/" + n) for n in FINISH_FILES)
    result = shlex.quote(scratch + "/result")
    apply = remote.compose_script(("restore", bundle.backup_id, "--operation", operation), output=writing)
    retire = remote.compose_script(("restore-files", "retire-owned", bundle.backup_id,
                                    bundle.manifest_sha256), output=result)
    # Only two fixed product commands run; each is authorized by the host's
    # native sudo. No shell or sudo password is passed through the handshake.
    script = (apply + " || exit 1; mv " + writing + " " + ready + " || exit 1; "
              + "i=0; while [ ! -f " + ack + " ]; do "
              + "[ ! -f " + abort + " ] || exit 1; "
              + '[ "$i" -lt 90 ] || exit ' + str(GATE_EXPIRED) + "; sleep 1; i=$((i+1)); done; "
              + retire + " || exit 1")
    with keys.ssh_output(host, terminal=True) as streams:
        with subprocess.Popen([*channel, keys.terminal_option(channel), host, script], **streams) as worker:
            try:
                with waiting("目标站正在恢复账本并核验结果，请保持窗口打开"):
                    raw = wait_for_apply(remote, ready, worker)
                    checked = verify(json.loads(raw))
                    # Release the worker only after the normal local proof
                    # checks. Do this before a possibly long-lived success UI.
                    try:
                        granted = keys.run(channel, host, "umask 077; set -C; : > " + ack)
                        keys.check(granted.returncode == 0, "清理许可未送达。")
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        announce(checked)
                        raise keys.EnrollmentError("账本已恢复并核验。无法确认清理许可已送达，请用原入口续办收尾。") from exc
                announce(checked)
                with waiting("正在清理本次临时备份文件"):
                    exit_code = worker.wait()
                    if exit_code == GATE_EXPIRED:
                        # The fixed worker exited without invoking cleanup.
                        # Resume only the idempotent, ownership-checked cleanup.
                        receipt = remote.compose("restore-files", "retire-owned", bundle.backup_id,
                                                 bundle.manifest_sha256)
                    else:
                        keys.check(exit_code == 0, "账本已恢复并核验。临时文件清理未完成，请从原入口继续收尾。")
                        receipt = remote.read_result(result)
                    check_retired(receipt)
                return checked
            except BaseException:
                if worker.poll() is None:
                    try:
                        keys.run(channel, host, "umask 077; : > " + abort)
                    finally:
                        try:
                            worker.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            worker.terminate()
                raise


def wait_for_apply(remote, ready, worker):
    # The noninteractive poll cannot consume sudo keystrokes. The normal
    # SSH command timeout is 20s, so use bounded waits of at most 5s each.
    script = ("i=0; while [ ! -f " + ready + ' ] && [ "$i" -lt 5 ]; do '
              + 'sleep 1; i=$((i+1)); done; [ -f ' + ready + ' ] || exit 75; '
              + 'test "$(wc -c < ' + ready + ')" -le 65536 && cat ' + ready)
    while True:
        probe = keys.run(remote.channel, remote.args.host, script)
        if probe.returncode == 0:
            keys.check(0 < len(probe.stdout) <= 65536, "恢复回执内容不完整。")
            return probe.stdout
        keys.check(probe.returncode == 75, "无法取回恢复回执。请保留目标站与备份，使用原入口复核。")
        if worker.poll() is not None:
            # The result may have appeared just after the last probe.
            reread = keys.run(remote.channel, remote.args.host, 'test -f ' + ready
                              + ' && test "$(wc -c < ' + ready + ')" -le 65536 && cat ' + ready)
            keys.check(reread.returncode == 0 and 0 < len(reread.stdout) <= 65536,
                       "未收到写入成功回执。请查看系统提示，并使用同一备份和目标复核。")
            return reread.stdout


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
        if os.name == "nt":
            inputs.extend(scratch + "/" + name for name in FINISH_FILES)
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
    if getattr(args, "site_url", None):
        expected = urlsplit(args.site_url)
        keys.check((address.hostname, address.port or 443) == (expected.hostname, expected.port or 443),
                   "恢复目标的网址与本站维护说明不符。")
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
    step = "正在准备备份文件" if action == "stage" else "正在清理本次临时备份文件"
    with waiting(step):
        result = remote.compose("restore-files", action, bundle.backup_id, bundle.manifest_sha256, incoming=action == "stage")
    check_copy_result(action, bundle, result)


def check_copy_result(action, bundle, result):
    keys.check(result.get("action") == action and result.get("backup_id") == bundle.backup_id
               and result.get("manifest_sha256") == bundle.manifest_sha256 and type(result.get("created")) is bool,
               "本站投放或回收回执与备份不符。")
    if action == "retire-owned" and result.get("preserved"):
        print(f"本站原有或已变动的副本保留在 restore-input/{bundle.backup_id}/。")


def ask_owner(message):
    print(message)
    if os.name == "nt":
        print("请在弹出的「确认恢复」窗口核对目标并选择确定或取消。", flush=True)
        result = windows_dialog("往来有礼 · 确认恢复", message + "\n\n确认将这份备份恢复到上述目标站？",
                                0x01 | 0x30 | 0x100)  # MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2
        keys.check(result in (1, 2), "Windows 恢复确认窗口未能正常返回。请保留现场，使用相同的备份和目标重试。")
        return result == 1  # IDOK; Escape and closing the dialog return IDCANCEL.
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


def recover(bundle, args, remote, enroll, confirm=ask_owner, *, status=None, finish=finish_in_one_session):
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
        announce_verified(result)
        if status.get("temporary_key_cleanup_pending"):
            with waiting("正在清理本次临时恢复钥匙"):
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
        receipt = None
        if stage in ("none", "rejected"):
            with waiting("正在向目标站登记本次恢复钥匙"):
                receipt = enroll()
        proof = status
        if not started:
            with waiting("正在预检目标站并核验备份"):
                proof = restore("--check-only", *expected)
    operation = proof["operation_id"]
    keys.check(isinstance(operation, str) and keys.OP.fullmatch(operation), "恢复操作编号无效。")
    if proof.get("already_restored") is True:
        target = proof["target"]
        announce_verified(check_result(proof, args, bundle, operation, target))
        with waiting("正在处理已恢复操作的收尾"):
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
    # After confirmation, keep the staged bundle for a retry if the final
    # proof is missing or invalid. The verified handoff owns its cleanup.
    return finish(remote, bundle, operation,
                  lambda completed: check_result(completed, args, bundle, operation, target),
                  announce_verified, lambda result: check_copy_result("retire-owned", bundle, result),
                  waiting)


def restore_selected(args, backup, session, load_key):
    args.restore_key = True
    if os.name == "nt":
        print("服务器需要授权时，主窗口会提示输入管理员密码。输完按回车，程序会自动继续；等待时可在标题栏查看当前步骤和已用时间。", flush=True)
    with open_bundle(backup) as bundle, session(args.host, args.port) as channel:
        with remote_files(channel, args, bundle) as remote:
            with waiting("正在读取目标站恢复状态"):
                status = remote.compose("restore", bundle.backup_id, "--status")
            enroll = None
            if status.get("stage") in ("none", "rejected"):
                age, secret, recipient = load_key()
                enroll = partial(keys.exchange, channel, args, secret, recipient, age)
            result = recover(bundle, args, remote, enroll, status=status)
    if result is None:
        print("已退出。")
    else:
        print(f"已核对 {result['gift_count']} 笔往来，网址 {result['target']['url']}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", nargs="?", type=Path, help="五文件目录或 ZIP，省略可拖入路径或打开文件选择框")
    parser.add_argument("--key", type=Path, help="源站的 age 原钥匙，省略可在交接时拖入路径或打开文件选择框")
    parser.add_argument("--host", required=True, help="SSH 用户名@目标主机")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--project-dir", required=True, help="目标站 YAML 所在的绝对路径")
    parser.add_argument("--project", default="gift-ledger", help="目标项目名")
    keys.add_site_options(parser)
    args = parser.parse_args()
    keys.remote_args(args.host, args.project_dir, args.project)
    keys.check(1 <= args.port <= 65535, "SSH 端口无效。")

    def load_key():
        path = args.key or keys.choose_key()
        age = keys.Age()
        return (age, *keys.custody(path, False, age))

    restore_selected(args, args.backup or choose_bundle(), keys.session, load_key)


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
