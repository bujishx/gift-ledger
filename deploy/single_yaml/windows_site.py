"""Windows 站主工具：为 Ubuntu 或群晖站点登记备份钥匙，或将账本恢复到独立空站。"""
import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

import mac_backup_key as keys
import mac_restore as restore

HERE = Path(__file__).resolve().parent


def system_directory():
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    keys.check(0 < size < len(buffer), "未找到 Windows 系统目录。")
    return Path(buffer.value)


@contextmanager
def preserve_console_input_mode():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_for = kernel.GetStdHandle
    handle_for.argtypes = (ctypes.c_uint32,)
    handle_for.restype = ctypes.c_void_p
    get_mode = kernel.GetConsoleMode
    get_mode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    get_mode.restype = ctypes.c_int
    set_mode = kernel.SetConsoleMode
    set_mode.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    set_mode.restype = ctypes.c_int

    handle = handle_for(0xFFFFFFF6)  # STD_INPUT_HANDLE
    original = ctypes.c_uint32()
    has_console = get_mode(handle, ctypes.byref(original))
    try:
        yield
    finally:
        # Restore after all interactive SSH children have returned. Redirected input has no mode.
        if has_console and not set_mode(handle, original.value):
            message = "Windows 控制台输入模式未能复原。请关闭此窗口，重新打开同一恢复入口。"
            if sys.exc_info()[0] is None:
                raise keys.EnrollmentError(message)
            print(message, file=sys.stderr, flush=True)


def key_helper(action, path=None, data=None):
    powershell = system_directory() / "WindowsPowerShell/v1.0/powershell.exe"
    environment = os.environ.copy()
    environment["PSModulePath"] = str(powershell.parent / "Modules")
    if path is not None:
        environment["GIFTLEDGER_SELECTED_KEY"] = str(path)
    # The downloaded ZIP may have a Zone.Identifier. Run only the fixed code
    # shipped alongside this client; paths and key bytes stay out of commands.
    script = (HERE / "windows_key.ps1").read_text(encoding="utf-8")
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-Command",
                             "& {\n" + script + "\n} " + action], env=environment,
                            input=data or b"", capture_output=True)
    keys.check(result.returncode != 2, "已取消选择。")
    keys.check(result.returncode == 0, "文件操作未完成。请选择本机或直连盘上的私有钥匙文件。"
               "在“属性 → 安全”中，只给本人、SYSTEM 或管理员访问权限；生成新钥匙请选新文件名。")
    return result.stdout


def choose(action):
    selected = key_helper(action).decode("utf-8").strip()
    keys.check(selected, "已取消选择。")
    path = Path(selected)
    if action == "select-backup" and path.name.casefold() == "bundle.json":
        return path.parent
    return path


def custody(path, generate, age):
    path = Path(path).expanduser().absolute()
    if generate:
        raw = age.run("age-keygen")
        secret, recipient = keys.identity(raw, age)
        key_helper("create", path, raw)
        keys.check(raw == key_helper("read", path), "新钥匙读回不一致。")
        return secret, recipient
    stat = path.lstat()
    keys.check(stat.st_nlink == 1 and not getattr(stat, "st_file_attributes", 0) & 0x400,
               "请选择原钥匙的普通本地文件。")
    return keys.identity(key_helper("read", path), age)


def find_plink():
    portable = HERE / "plink.exe"
    if portable.is_file():
        expected = "969f36879d5716aa1a9811f43a6a6510e8f08372dbeb9695b810b9c776f39c75"
        keys.check(hashlib.sha256(portable.read_bytes()).hexdigest() == expected,
                   "随包的 plink.exe 与 PuTTY 0.85 官方 x64 版本不符。请重新解压站主工具。")
        return str(portable)
    candidates = []
    for location in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(location):
            candidates.append(Path(os.environ[location]) / "PuTTY/plink.exe")
    for path in candidates:
        if path.is_file():
            return str(path)
    return shutil.which("plink.exe")


@contextmanager
def session(host, port):
    plink = find_plink()
    ssh = system_directory() / "OpenSSH/ssh.exe"
    if ssh.is_file():
        options = [str(ssh), "-p", str(port), "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
                   "-o", "ForwardX11=no", "-o", "ConnectTimeout=10"]
        # When PuTTY is available, probe known hosts silently before falling
        # back to its interactive login. Otherwise let OpenSSH ask about a new
        # host key or key passphrase in its own console.
        auth = ["-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no"]
        probe = subprocess.run([*options, *auth, "-o", "BatchMode=yes" if plink else "BatchMode=no", host, "true"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL if plink else None)
        if probe.returncode == 0:
            batch = [*options, "-o", "BatchMode=yes"]
            if plink or subprocess.run([*batch, host, "true"], capture_output=True).returncode == 0:
                yield batch
                return
    keys.check(plink, "使用服务器登录密码时，请安装 PuTTY（含 plink.exe）后重试。"
               "也可在本机使用已有 SSH 登录密钥，先用 Windows ssh 登录一次并核对主机指纹。")
    options = [plink, "-ssh", "-P", str(port), "-a", "-x", "-share"]

    def probe(*arguments):
        try:
            result = subprocess.run([*options, "-batch", *arguments],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=10)
        except subprocess.TimeoutExpired:
            # The upstream may still be waiting for the owner's interactive login.
            return False
        return result.returncode == 0

    master = None
    try:
        shared = probe("-shareexists", host)
        if not shared:
            print("请在新打开的 SSH 窗口核对指纹并登录。如提示 Press Return，按一次回车。"
                  "登录后保持 SSH 窗口打开，回到本窗口继续操作。", flush=True)
            master = subprocess.Popen([*options, "-N", host], creationflags=subprocess.CREATE_NEW_CONSOLE)
        deadline = time.monotonic() + 240
        # A sharing endpoint can exist before the first remote session is usable.
        while not (shared and probe("-T", host, "true")):
            keys.check(master is None or master.poll() is None, "SSH 登录未完成。")
            keys.check(time.monotonic() < deadline, "等待 SSH 会话就绪超时。若窗口仍提示 Press Return，请重试并在该提示处按回车。")
            time.sleep(0.5)
            shared = probe("-shareexists", host)
        print("SSH 已成功执行远端连接检查。", flush=True)
        yield [*options, "-batch"]
    finally:
        if master is not None and master.poll() is None:
            master.terminate()
            master.wait(timeout=10)


def request_site(args):
    menu = args.action is None
    if menu:
        choice = input("1 登记日常备份钥匙\n2 从备份恢复到独立空站\n请选择：").strip()
        keys.check(choice in ("1", "2"), "已退出。")
        args.action = "enroll" if choice == "1" else "restore"
        print("先填写服务器的 SSH 登录信息。恢复确认时会显示目标站自己的 HTTPS 网址。")
    if not args.host:
        while True:
            host = input("SSH 登录地址（用户名@主机，例如 admin@192.168.1.20）：").strip()
            if keys.SSH_HOST.fullmatch(host):
                args.host = host
                break
            print("请填写服务器的 SSH 主机名或 IP，可在前面加上用户名@。网站网址和端口请勿填在这里。")
    if args.port is None and menu:
        while True:
            value = input("服务器 SSH 端口 [22]：").strip() or "22"
            try:
                port = int(value)
            except ValueError:
                port = 0
            if value.isascii() and value.isdecimal() and 1 <= port <= 65535:
                args.port = port
                break
            print("SSH 端口须为 1 到 65535 的整数。使用默认端口可直接按回车。")
    if args.port is None:
        args.port = 22
    keys.check(1 <= args.port <= 65535, "SSH 端口须为 1 到 65535 的整数。")
    args.project_dir = args.project_dir or input("服务器上的 YAML 文件夹（绝对路径）：").strip()
    args.project = args.project or input("站点项目名称 [gift-ledger]：").strip() or "gift-ledger"
    keys.remote_args(args.host, args.project_dir, args.project)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["enroll", "restore"], nargs="?")
    parser.add_argument("--host", help="SSH 用户名@主机")
    parser.add_argument("--port", type=int, help="服务器 SSH 端口（默认 22）")
    parser.add_argument("--project-dir", help="服务器上 YAML 所在的绝对路径")
    parser.add_argument("--project", help="Compose 或 Container Manager 中的项目名称")
    parser.add_argument("--backup", type=Path, help="五文件备份目录或 ZIP")
    parser.add_argument("--result-file", type=Path, help="将核验通过的恢复结果写入新的本机 JSON 文件")
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--key", type=Path, help="备份原钥文件")
    key.add_argument("--generate", type=Path, help="保存新钥的文件路径（首次登记）")
    args = parser.parse_args()
    keys.check(os.name == "nt", "请从 Windows 电脑运行此入口。")
    request_site(args)
    if args.action == "restore":
        keys.check(not args.generate, "恢复需要该备份对应的原钥匙。")
        if args.result_file:
            keys.check(not args.result_file.exists(), "指定的恢复结果文件已存在，请保留原件并选择新文件名。")
        backup = args.backup or choose("select-backup")

        def load_key():
            path = args.key or choose("select-key")
            age = keys.Age()
            return (age, *custody(path, False, age))

        with preserve_console_input_mode():
            result = restore.restore_selected(args, backup, session, load_key)
        if args.result_file and result is not None:
            receipt = {"operation_id": result["operation_id"], "project": result["target"]["project"],
                       "instance_id": result["target"]["instance_id"], "url": result["target"]["url"],
                       "gift_count": result["gift_count"], "backup_id": result["backup_operation_id"],
                       "source_project": result["source"]["source_project"],
                       "full_database_and_app_files_verified": result["full_database_and_app_files_verified"]}
            with args.result_file.open("x", encoding="utf-8") as output:
                json.dump(receipt, output, ensure_ascii=False, indent=2)
                output.write("\n")
        return
    keys.check(not args.result_file, "结果文件选项仅用于恢复。")
    keys.check(not args.backup, "登记日常备份钥匙不需要备份包。")
    if not args.generate and not args.key:
        if keys.choose_enrollment_method(args.project) == "generate":
            args.generate = choose("select-new")
        else:
            args.key = choose("select-key")
    path = args.generate or args.key
    age = keys.Age()
    secret, recipient = custody(path, bool(args.generate), age)
    args.restore_key = False
    with session(args.host, args.port) as channel:
        print("正在登记本站备份钥匙；若出现 sudo 密码提示，请在本窗口输入。", flush=True)
        try:
            receipt = keys.exchange(channel, args, secret, recipient, age)
        except subprocess.TimeoutExpired as error:
            raise keys.EnrollmentError(f"SSH 已连接，但登记阶段有子进程在 {error.timeout} 秒内未结束。请保留此提示。") from error
    print(f"本站备份钥匙登记通过。项目 {receipt['project']}，站点 {receipt['instance_id']}。")
    print(f"请单独保管原钥匙 {path}。")


if __name__ == "__main__":
    try:
        main()
    except (restore.store.BackupStoreError, restore.backup_export.BackupExportError, zipfile.BadZipFile):
        sys.exit("所选备份的五份文件或压缩包未通过核对，请使用完整副本。")
    except keys.EnrollmentError as error:
        sys.exit(str(error))
    except (KeyboardInterrupt, EOFError):
        sys.exit("已退出。可以用同一备份和目标继续。")
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        sys.exit(f"操作未完成（{type(error).__name__}）。请核对 SSH 登录、Docker 权限、备份与钥匙，使用同一目标重试。")
