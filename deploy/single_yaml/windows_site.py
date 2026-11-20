"""Windows 站主工具：为 Ubuntu 或群晖站点登记备份钥匙，或将账本恢复到独立空站。"""
import argparse
import base64
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
    if action.startswith("select-"):
        if result.returncode == 2:
            return b""
        keys.check(result.returncode == 0, "未能打开文件窗口。")
        return result.stdout
    keys.check(result.returncode == 0, "文件操作未完成。请选择本机或直连盘上的私有钥匙文件。"
               "在“属性 → 安全”中，只给本人、SYSTEM 或管理员访问权限；生成新钥匙请选新文件名。")
    return result.stdout


def selected_path(action, value):
    # Explorer may wrap a dragged path in quotes. Keep spaces and backslashes literal.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    keys.check(value, "路径为空。")
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        raise keys.EnrollmentError("无法识别这个路径，请粘贴完整路径。") from None
    if action == "select-backup":
        if path.is_dir():
            keys.check((path / "BUNDLE.json").is_file(), "这个目录里没有 BUNDLE.json。")
        else:
            keys.check(path.is_file(), "找不到这个备份文件。")
            if path.name.casefold() == "bundle.json":
                return path.parent
            keys.check(path.suffix.casefold() == ".zip", "请选择备份 ZIP、BUNDLE.json 或五文件目录。")
    elif action == "select-new":
        keys.check(not os.path.lexists(path), "这个位置已有文件，请换一个新钥匙文件名。")
        keys.check(path.parent.is_dir(), "找不到保存新钥匙的文件夹。")
    else:
        keys.check(path.is_file(), "找不到这份已有钥匙文件。")
    return path


def choose(action):
    label, option = {
        "select-backup": ("备份 ZIP、BUNDLE.json 或五文件目录", "--backup"),
        "select-key": ("已有钥匙文件", "--key"),
        "select-new": ("新钥匙的保存路径（含文件名）", "--generate"),
    }[action]
    value = ""
    while True:
        try:
            if not value:
                value = key_helper(action).decode("utf-8").strip()
                keys.check(value, "文件窗口已关闭。")
            return selected_path(action, value)
        except keys.EnrollmentError as error:
            message = str(error)
        except (OSError, ValueError, subprocess.SubprocessError):
            message = "未能打开文件窗口或读取所选路径。"
        keys.check(sys.stdin.isatty(), f"{message}可用 {option} 指定完整路径。")
        value = keys.read_selection(f"{message}\n可拖入或粘贴{label}，回车重新打开窗口，输入 q 退出：")
        keys.check(value.casefold() != "q", "已退出文件选择。")


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
            print(f"正在通过 SSH 登录 {host}。请在新窗口核对主机指纹。如需密码，输入该服务器账号的密码并回车（不回显）。"
                  "如提示 Press Return，按一次回车。"
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
        print("请准备服务器的 SSH 登录信息。恢复时还需要目标站 data 中“网站地址.txt”里的完整网址。新版文件名含项目名和实例短号。")
    if args.action == "restore" and not args.site_url and (menu or not any((args.host, args.project_dir, args.project))):
        while True:
            value = input("粘贴这个站的完整 HTTPS 网址（直接回车退出）：").strip()
            keys.check(value, "已退出。")
            try:
                keys.site_endpoint(value)
            except keys.EnrollmentError as exc:
                print(exc)
                continue
            args.site_url = value
            break
    address = keys.site_endpoint(args.site_url)[0] if args.site_url else None
    prompt_port = menu or (address and not args.host)
    if not args.host:
        while True:
            if address:
                login = input(f"SSH 登录账号（本站 {address}，也可输入 用户名@SSH主机）：").strip()
                host = login if "@" in login else login + "@" + address
            else:
                host = input("SSH 登录地址（用户名@主机，例如 admin@192.168.1.20）：").strip()
            if keys.SSH_HOST.fullmatch(host):
                args.host = host
                break
            print("请填写服务器的 SSH 主机名或 IP，可在前面加上用户名@。网站网址和端口请勿填在这里。")
    if args.port is None and prompt_port:
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
    for path in (args.compose_file, args.site_dir):
        if path:
            keys.remote_args(args.host, path, args.project)


def layout_paths(encoded):
    # A batch launcher can reinterpret %, ! or quotes even inside arguments.
    # The site guide uses one data-only argument for paths containing shell syntax.
    try:
        paths = json.loads(base64.b64decode(encoded, altchars=b'-_', validate=True).decode("utf-8"))
        if isinstance(paths, list) and len(paths) == 3 and all(isinstance(p, str) and p for p in paths):
            return paths
    except (ValueError, UnicodeError):
        pass
    raise argparse.ArgumentTypeError("请重新复制本站维护说明里的 Windows 命令。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["enroll", "restore"], nargs="?")
    parser.add_argument("--host", help="SSH 用户名@主机")
    parser.add_argument("--port", type=int, help="服务器 SSH 端口（默认 22）")
    parser.add_argument("--project-dir", help="服务器上 YAML 所在的绝对路径")
    parser.add_argument("--project", help="Compose 或 Container Manager 中的项目名称")
    keys.add_site_options(parser)
    parser.add_argument("--site-layout", type=layout_paths, help=argparse.SUPPRESS)
    parser.add_argument("--backup", type=Path, help="五文件备份目录或 ZIP")
    parser.add_argument("--result-file", type=Path, help="将核验通过的恢复结果写入新的本机 JSON 文件")
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--key", type=Path, help="备份原钥文件")
    key.add_argument("--generate", type=Path, help="保存新钥的文件路径（首次登记）")
    args = parser.parse_args()
    keys.check(os.name == "nt", "请从 Windows 电脑运行此入口。")
    if args.site_layout:
        keys.check(not any((args.project_dir, args.site_dir, args.compose_file)), "请只使用一份本站路径。")
        args.project_dir, args.site_dir, args.compose_file = args.site_layout
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
        print("正在登记本站备份钥匙。若服务器要求管理员授权，请按本窗口的提示输入密码。", flush=True)
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
