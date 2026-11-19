#!/usr/bin/env python3
"""在 Mac 登记已有备份钥匙或生成新钥匙，经 SSH 核对本站的登记回执。"""

import argparse
import base64
import codecs
from contextlib import contextmanager
import ctypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

RELEASE_YAML = Path(__file__).resolve().with_name("docker-compose.yml")
RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")
SHA = re.compile(r"[0-9a-f]{64}")
OP = re.compile(r"[0-9a-f]{32}")
SSH_HOST = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}@)?[A-Za-z0-9][A-Za-z0-9_.-]{0,252}")


class EnrollmentError(ValueError):
    pass


def check(condition, message):
    if not condition:
        raise EnrollmentError(message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Age:
    def __init__(self):
        self.programs = {name: shutil.which(name) for name in ("age", "age-keygen")}
        self.docker = shutil.which("docker")
        if sys.platform == "darwin" and not self.docker:
            desktop = (Path.home() / ".docker/bin/docker", Path("/Applications/Docker.app/Contents/Resources/bin/docker"))
            self.docker = next((str(path) for path in desktop if path.is_file() and os.access(path, os.X_OK)), None)
        if os.name == "nt":
            self.endpoint = "npipe:////./pipe/docker_engine"
        else:
            socket = next((p for p in (Path.home() / ".docker/run/docker.sock", Path("/var/run/docker.sock"))
                           if p.exists() and stat.S_ISSOCK(p.stat().st_mode)), None)
            self.endpoint = "unix://" + str(socket) if socket else None
        self.docker_env = {k: v for k, v in os.environ.items() if k not in
                           {"DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
        self.image = None
        if not all(self.programs.values()) and self.docker and self.endpoint:
            check(RELEASE_YAML.is_file(), "请将站主工具与同一版本的 docker-compose.yml 放在一起。")
            docker = [self.docker, "--host", self.endpoint]
            config = subprocess.run([*docker, "compose", "-f", str(RELEASE_YAML), "--profile", "maintenance",
                                     "config", "--format", "json"],
                                    capture_output=True, timeout=20, env=self.docker_env)
            check(config.returncode == 0, "无法读取同一发行件中的 docker-compose.yml。")
            services = json.loads(config.stdout)["services"]
            tag = services["backup-key"]["image"]
            check(tag == services["backup"]["image"] and not tag.endswith(":latest") and
                  re.fullmatch(r"[a-z0-9][a-z0-9./:_-]+(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})", tag),
                  "此版本的 YAML 缺少一致、固定的备份镜像。")
            inspect_image = [*docker, "image", "inspect", "--format", "{{.Id}}", tag]
            result = subprocess.run(inspect_image, capture_output=True, timeout=15, env=self.docker_env)
            if os.name == "nt" and result.returncode and re.fullmatch(
                    r"(?:docker.io/)?zhjido/giftledger-backup(?::[\w.-]+)?@sha256:[0-9a-f]{64}", tag):
                print("正在为本台 Windows 下载本发行版的备份工具镜像。", flush=True)
                subprocess.run([*docker, "pull", "--platform", "linux/amd64", tag],
                               env=self.docker_env, timeout=600, check=True)
                result = subprocess.run(inspect_image, capture_output=True, timeout=15, env=self.docker_env)
            if result.returncode == 0 and re.fullmatch(rb"sha256:[0-9a-f]{64}", result.stdout.strip()):
                self.image = result.stdout.decode("ascii").strip()
        check(all(self.programs.values()) or self.image,
              "需要本机 age 和 age-keygen，或已运行的 Docker Desktop 与已载入的本站备份镜像。")

    def run(self, name, args=(), data=None):
        if all(self.programs.values()):
            command = [self.programs[name], *args]
        else:
            # Force a local Desktop socket. A remote Docker context cannot receive a key.
            command = [self.docker, "--host", self.endpoint, "run", "--rm", "--pull=never",
                       "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges", "--tmpfs", "/var/lib/mysql",
                       "--log-driver", "none", "--platform", "linux/amd64",
                       "--entrypoint", "/usr/local/bin/" + name, self.image, *args]
        result = subprocess.run(command, input=data or b"", capture_output=True, timeout=50, env=self.docker_env)
        check(result.returncode == 0, "本机 age 未能处理钥匙。请核对钥匙格式、本机 age 或已载入的备份镜像。")
        return result.stdout


def local_disk(parent):
    # Darwin statvfs omits MNT_LOCAL. Ask getattrlist for ATTR_VOL_MOUNTFLAGS.
    attrs = ctypes.create_string_buffer(struct.pack("=HHIIIII", 5, 0, 0, 0x80004000, 0, 0, 0))
    result = ctypes.create_string_buffer(8)
    status = ctypes.CDLL(None).getattrlist(os.fsencode(parent), attrs, result, 8, 0)
    length, flags = struct.unpack("=II", result.raw)
    return status == 0 and length == 8 and bool(flags & 0x1000)


def private_file(fd, path):
    info = os.fstat(fd)
    check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
          and info.st_size <= 16384 and stat.S_IMODE(info.st_mode) & 0o077 == 0,
          "原钥匙须为本人拥有的私有文件。可在 Finder 的“显示简介”中移除其他用户权限。")
    if sys.platform == "darwin":
        acl = subprocess.run(["/bin/ls", "-lde", str(path)], capture_output=True, check=True)
        check(not re.search(rb"(?m)^\s*\d+:.*\ballow\b", acl.stdout),
              "原钥匙含额外的访问授权，请在 Finder 中核对权限。")
    current = path.lstat()
    check((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), "钥匙文件已变化，请重新选择。")


def custody(path, generate, age):
    path = path.expanduser().absolute()
    # /tmp and /var on macOS are symlinks to /private. Normalize the parent,
    # then open the selected filename itself with O_NOFOLLOW.
    parent = path.parent.resolve(strict=True)
    path = parent / path.name
    if sys.platform == "darwin":
        check(local_disk(parent), "请选择 Mac 本机或本机直连磁盘保管原钥匙。")
    if generate:
        check(not os.path.lexists(path), "此文件已存在，可用 --key 选取已有原钥匙。")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            created = os.fstat(fd)
            try:
                # Verify the filesystem actually enforced private access before
                # generating or writing the secret (e.g. removable disks).
                private_file(fd, path)
                generated = age.run("age-keygen")
                check(len(generated) <= 16384 and b"AGE-SECRET-KEY-1" in generated, "生成的原钥匙格式无效。")
                out.write(generated)
                out.flush()
                os.fsync(out.fileno())
            except BaseException:
                if path.exists() and os.path.samestat(created, path.lstat()):
                    path.unlink()
                raise
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as key:
        private_file(fd, path)
        raw = key.read(16385)
    return identity(raw, age)


def identity(raw, age):
    check(len(raw) <= 16384, "钥匙文件已变化，请重新选择。")
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.startswith(b"#")]
    check(len(lines) == 1 and re.fullmatch(rb"AGE-SECRET-KEY-1[A-Z0-9]{40,100}", lines[0]),
          "请选择含一把 age 原钥匙的文件。")
    secret = lines[0] + b"\n"
    recipient = age.run("age-keygen", ["-y"], secret).decode("ascii").strip()
    check(RECIPIENT.fullmatch(recipient), "原钥匙公钥校验失败。")
    return secret, recipient


def target_info(raw, project):
    obj = json.loads(raw)
    target = obj["target"]
    check(type(obj.get("registered")) is bool and
          set(target) == {"format", "project", "instance_id", "operation_id", "started_at",
                          "recipient", "started_sha256", "success"} and
          target["format"] == "gift-ledger-gui-key-target-v1" and target["success"] is True and
          target["project"] == project and str(uuid.UUID(target["instance_id"])) == target["instance_id"]
          and OP.fullmatch(target["operation_id"]) and RECIPIENT.fullmatch(target["recipient"]),
          "网站身份或交接公钥校验失败。")
    started = {k: target[k] for k in ("format", "project", "instance_id", "operation_id", "started_at")}
    check(digest(encoded(started)) == target["started_sha256"], "网站交接回执摘要不符。")
    return target, obj["registered"]


def packet_for(target, secret, recipient, age):
    public = {"operation_id": uuid.uuid4().hex, "project": target["project"],
              "instance_id": target["instance_id"], "target_recipient": target["recipient"],
              "backup_recipient": recipient}
    payload = {**public, "format": "gift-ledger-gui-key-transfer-payload-v1", "identity": secret.decode("ascii")}
    cipher = age.run("age", ["--encrypt", "--recipient", target["recipient"]], encoded(payload))
    request = {**public, "format": "gift-ledger-gui-key-import-request-v1",
               "ciphertext": "key-transfer-" + public["operation_id"] + ".age", "ciphertext_sha256": digest(cipher)}
    return {"request": request, "ciphertext_b64": base64.b64encode(cipher).decode("ascii")}


def verify_receipt(raw, target, secret, recipient, request=None):
    entry = json.loads(raw)
    check(set(entry) == {"format", "success", "project", "instance_id", "operation_id", "backup_recipient",
                         "request_sha256", "started_sha256", "identity_sha256"} and
          entry["format"] == "gift-ledger-gui-key-import-completed-v1" and entry["success"] is True and
          entry["project"] == target["project"] and entry["instance_id"] == target["instance_id"] and
          entry["backup_recipient"] == recipient and entry["identity_sha256"] == digest(secret) and
          OP.fullmatch(entry["operation_id"]) and SHA.fullmatch(entry["request_sha256"]) and
          digest(encoded({"operation_id": entry["operation_id"], "success": False})) == entry["started_sha256"],
          "本站已登记的钥匙与所选原钥匙或回执不符。")
    if request:
        check(entry["operation_id"] == request["operation_id"] and entry["request_sha256"] == digest(encoded(request)),
              "本次登记请求与网站回执不符。")
    return entry


def supplied_path(text):
    # Finder drags may be shell-escaped; pasted ordinary filenames may contain spaces.
    path = Path(text).expanduser()
    if path.exists():
        return path
    try:
        words = shlex.split(text)
    except ValueError:
        words = []
    if len(words) == 1 and Path(words[0]).expanduser().exists():
        return Path(words[0]).expanduser()
    raise EnrollmentError("找不到该路径，请从访达重新拖入文件或粘贴完整路径。")


def read_selection(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise EnrollmentError("已取消选择。") from None


def choose_local_path(backup=False):
    label = "五文件备份目录或 ZIP" if backup else "备份原钥匙"
    prompt = f"请选择{label}。回车打开文件窗口，也可将路径拖入或粘贴在这里"
    if backup:
        prompt += "；输入 d 打开目录窗口"
    interactive = sys.stdin.isatty()
    folder = False
    if interactive:
        value = read_selection(prompt + "（输入 q 退出）：")
        check(value.lower() != "q", "已取消选择。")
        folder = backup and value.lower() == "d"
        if value and not folder:
            return supplied_path(value)
    selection = "choose folder" if folder else "choose file"
    # Standard Additions hosts its own foreground picker; a bare NSOpenPanel
    # in a background osascript process can disappear on its first mouse click.
    dialog = (f'activate\ntry\nreturn POSIX path of ({selection} with prompt "选择{label}")\n'
              'on error number -128\nreturn ""\nend try')
    result = subprocess.run(["/usr/bin/osascript", "-e", dialog], capture_output=True)
    path = os.fsdecode(result.stdout).rstrip("\n")
    if result.returncode == 0 and path:
        return Path(path)
    message = "文件窗口已关闭" if result.returncode == 0 else "未能打开文件窗口"
    check(interactive, message + "。请用命令行指定文件路径。")
    value = read_selection(message + "。可拖入或粘贴路径继续，直接回车退出：")
    check(value, "已取消选择。")
    return supplied_path(value)


def choose_key():
    return choose_local_path()


def choose_enrollment_method(project):
    choice = read_selection(f"首次为项目 {project} 登记日常备份钥匙，请选一种方式。\n"
                            "1 生成新钥匙并登记\n"
                            "2 登记旧钥匙\n"
                            "请输入 1 或 2，直接回车或输入 q 退出：")
    check(choice and choice.lower() != "q", "已退出登记。")
    check(choice in ("1", "2"), "请输入 1 或 2 选择登记方式。")
    return "generate" if choice == "1" else "existing"


def enrollment_path(args):
    if args.generate:
        return args.generate, True
    if args.key or args.restore_key:
        return args.key or choose_key(), False
    if choose_enrollment_method(args.project) == "existing":
        return choose_key(), False

    home = Path.home()
    default = home / (args.project + "-backup.agekey")
    while True:
        value = read_selection(f"新钥匙默认保存在 {default}。直接回车使用此路径，也可输入其他文件名或完整路径（q 退出）：")
        check(value.lower() != "q", "已退出登记。")
        path = home / os.path.expanduser(value) if value else default
        if os.path.lexists(path):
            print("这个路径已有文件。请输入其他文件名，或退出后选择“登记旧钥匙”使用原文件。")
        elif not path.parent.is_dir():
            print("找不到这个文件夹。请粘贴已有文件夹下的完整路径，或只输入一个文件名，保存在 Mac 个人目录。")
        else:
            return path, True


def remote_args(host, project_dir, project):
    check(SSH_HOST.fullmatch(host),
          "请填写 SSH 用户名和主机地址，例如 admin@192.168.1.20。")
    check(project_dir.startswith("/") and not re.search(r"[\x00-\x1f\x7f:]", project_dir)
          and ".." not in project_dir.split("/"), "请输入服务器上项目的绝对路径。")
    check(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "项目名不符合 Compose 规则。")


def add_site_options(parser):
    parser.add_argument("--compose-file", help="本站说明中的服务器 YAML 完整路径")
    parser.add_argument("--site-dir", help="本站说明中的服务器站点文件夹")
    parser.add_argument("--site-url", help="本站说明中的 HTTPS 网址")


def compose_command(args, arguments, *, default_file=False):
    options = ["compose", "--project-directory", args.project_dir]
    config = getattr(args, "compose_file", None)
    station = getattr(args, "site_dir", None)
    for path in (config, station):
        if path:
            remote_args(args.host, path, args.project)
    if config or default_file:
        options.extend(["-f", config or args.project_dir.rstrip("/") + "/docker-compose.yml"])
    settings = []
    if station:
        settings.append("GIFTLEDGER_SITE_DIR=" + station)
    value = getattr(args, "site_url", None)
    if value:
        url = urlsplit(value)
        check(url.scheme == "https" and url.path in ("", "/") and url.hostname and not
              (url.username or url.password or url.query or url.fragment), "本站网址须为 HTTPS IPv4 地址。")
        address = ipaddress.IPv4Address(url.hostname)
        port = 443 if url.port is None else url.port
        check(1 <= port <= 65535, "本站 HTTPS 端口无效。")
        settings.extend(["GIFTLEDGER_SITE_IP=" + str(address), "GIFTLEDGER_HTTPS_PORT=" + str(port)])
    prefix = "/usr/bin/env " + shlex.join(settings) + " " if settings else ""
    return prefix + '"$d" ' + shlex.join([*options, "-p", args.project, *arguments])


@contextmanager
def session(host, port):
    with tempfile.TemporaryDirectory(prefix="gl-key-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp") as folder:
        socket = str(Path(folder) / "s")
        base = ["ssh", "-p", str(port), "-S", socket, "-o", "ClearAllForwardings=yes",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ConnectTimeout=10"]
        print(f"正在通过 SSH 登录 {host}。需要密码时请输入这个服务器账号的密码，输入不回显。", file=sys.stderr, flush=True)
        login = subprocess.run([*base, "-o", "ControlPersist=no", "-M", "-fNT", host])
        check(login.returncode == 0, "SSH 登录未完成，请核对服务器地址、端口和登录账号。")
        channel = [*base, "-o", "BatchMode=yes", "-o", "ProxyCommand=false", "-o", "ControlMaster=no"]
        try:
            yield channel
        finally:
            subprocess.run([*base, "-O", "exit", host], capture_output=True, timeout=10)


def run(channel, host, command, data=None):
    return subprocess.run([*channel, "-T", host, command], input=data or b"", capture_output=True, timeout=20)


def terminal_option(channel):
    if not sys.stdin.isatty():
        return "-T"
    return "-t" if Path(channel[0]).name.lower() == "plink.exe" else "-tt"


_explained_home = set()
_home_lock = threading.Lock()


def _show_ssh_output(reader, output, host, username):
    # Hold only bytes that could still be the exact DSM login warning. In
    # particular, forward prompts and terminal escape sequences without waiting for LF.
    warning = (b"Could not chdir to home directory /var/services/homes/" + username.encode("ascii")
               + b": No such file or directory")
    pending, line_start = b"", True
    decode = codecs.getincrementaldecoder("utf-8")(errors="backslashreplace").decode
    while chunk := reader.read(4096):
        data = pending + chunk
        pending = b""
        while data:
            newline = data.find(b"\n")
            end = len(data) if newline < 0 else newline + 1
            piece, data = data[:end], data[end:]
            if line_start and piece in (warning + b"\n", warning + b"\r\n", warning + b"\r\r\n"):
                with _home_lock:
                    if host not in _explained_home:
                        output.write("群晖 SSH 用户没有个人目录，正在使用指定的项目目录。\n")
                        _explained_home.add(host)
            elif line_start and newline < 0 and (warning + b"\r\r\n").startswith(piece):
                pending = piece
                break
            else:
                output.write(decode(piece))
            line_start = newline >= 0
        output.flush()
    output.write(decode(pending, final=True))
    output.flush()


@contextmanager
def ssh_output(host, *, terminal=False):
    username, sep, _ = host.partition("@")
    output = sys.stderr
    if (os.name != "nt" or not sep or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", username)
            or not sys.stderr.isatty() or (terminal and not sys.stdout.isatty())):
        yield {}
        return
    first, last = os.pipe()
    errors = []

    def forward():
        try:
            with os.fdopen(first, "rb", buffering=0) as reader:
                _show_ssh_output(reader, output, host, username)
        except OSError as exc:
            errors.append(exc)

    with os.fdopen(last, "wb", buffering=0) as writer:
        thread = threading.Thread(target=forward, name="ssh-display")
        thread.start()
        try:
            # The SSH client inherits the original console input for sudo.
            # With a remote PTY, remote stderr travels via SSH stdout.
            yield {"stdout": writer, "stderr": writer} if terminal else {"stderr": writer}
        finally:
            writer.close()
            thread.join()
            if errors and sys.exc_info()[0] is None:
                raise errors[0]


def sudo_command(command, purpose):
    if os.name != "nt":
        prompt = f"需要管理员授权{purpose}。请核对输入法（英文密码选 ABC），输入服务器账号 %p 的密码并回车（不回显）："
        return "sudo -p " + shlex.quote(prompt) + " " + command
    # Keep the remote command ASCII across Plink code pages; the real sudo owns input.
    prompt = f"\a【需要操作】{purpose}。请输入服务器管理员密码（输入不回显），然后按回车："
    encoded_prompt = "".join(f"\\{byte:03o}" for byte in prompt.encode("utf-8"))
    return "sudo -p \"$(printf '" + encoded_prompt + "')\" " + command


def exchange(channel, args, secret, recipient, age):
    with ssh_output(args.host, terminal=True) as streams:
        return _exchange(channel, args, secret, recipient, age, streams)


def _exchange(channel, args, secret, recipient, age, streams):
    host = args.host
    scratch = run(channel, host, "umask 077 && mktemp -d /tmp/giftledger-key-XXXXXXXX")
    scratch_name = scratch.stdout.decode("ascii").strip()
    check(scratch.returncode == 0 and re.fullmatch(r"/tmp/giftledger-key-[A-Za-z0-9]{8}", scratch_name),
          "无法为本站建立临时加密交接目录。")
    cd = "cd " + shlex.quote(scratch_name) + " && "
    worker = None
    try:
        docker = ('d=/var/packages/ContainerManager/target/usr/bin/docker; '
                  'if [ ! -x "$d" ]; then d=$(command -v docker) || exit 1; fi; ')
        cmd = 'cd ' + shlex.quote(args.project_dir) + ' || exit 1; ' + docker
        service_name = 'restore-key' if args.restore_key else 'backup-key'
        service = compose_command(args, ["run", "--rm", "-T", "--no-deps", "-v",
                                         scratch_name + ":/work/exchange", service_name])
        purpose = "登记本站恢复用临时钥匙" if args.restore_key else "登记本站备份钥匙"
        cmd += 'if "$d" info >/dev/null 2>&1; then exec ' + service + '; else exec ' + sudo_command(service, purpose) + '; fi'
        worker = subprocess.Popen([*channel, terminal_option(channel), host, cmd], **streams)

        def read_when_ready(name):
            # One multiplexed connection per wait, regardless of how long sudo takes.
            script = ('cd ' + shlex.quote(scratch_name) + ' || exit 1; i=0; while [ ! -f ' + name + ' ] && [ "$i" -lt 180 ]; do '
                      'sleep 1; i=$((i+1)); done; test -f ' + name + ' && cat ' + name)
            # The receipt poller needs no input. Only the interactive worker should
            # read console keystrokes while the owner answers sudo.
            reader = subprocess.Popen([*channel, "-T", host, script], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                end = time.monotonic() + 185
                while time.monotonic() < end:
                    try:
                        stdout, _ = reader.communicate(timeout=0.5)
                        check(reader.returncode == 0 and 0 < len(stdout) <= 32768,
                              "无法取得网站登记回执，请核对管理权限和项目目录。")
                        return stdout
                    except subprocess.TimeoutExpired:
                        if worker.poll() not in (0, None):
                            raise EnrollmentError("网站登记容器未能完成，请查看上方终端信息。")
                raise EnrollmentError("等待网站登记回执超时。")
            finally:
                if reader.poll() is None:
                    reader.terminate()
                    reader.communicate()

        target, registered = target_info(read_when_ready("target.json"), args.project)
        packet = None
        if not registered:
            packet = packet_for(target, secret, recipient, age)
            write = run(channel, host, cd + "umask 077 && cat > packet.upload && mv packet.upload packet.json", encoded(packet))
            check(write.returncode == 0, "加密交接未送达。")
        entry = verify_receipt(read_when_ready("receipt.json"), target, secret, recipient,
                               packet["request"] if packet else None)
        check(worker.wait(timeout=20) == 0, "网站登记容器未正常退出。")
        return entry
    finally:
        try:
            if worker is not None and worker.poll() is None:
                try:
                    run(channel, host, cd + ": > cancel")
                finally:
                    # The local SSH child owns the output pipe, even if the
                    # independent connection used to cancel just timed out.
                    try:
                        worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait()
        finally:
            cleanup = run(channel, host, cd + "rm -f target.json target.writing packet.upload packet.json receipt.json receipt.writing cancel && rmdir " + shlex.quote(scratch_name))
            check(cleanup.returncode == 0, "临时交接目录待清理：" + scratch_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH 用户名@服务器")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--project-dir", required=True, help="服务器上单份 YAML 所在目录")
    parser.add_argument("--project", default="gift-ledger", help="Container Manager 或 Compose 中的项目名")
    add_site_options(parser)
    parser.add_argument("--restore-key", action="store_true", help="将源备份的原钥仅用于目标站这次恢复")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--key", type=Path, help="直接指定已有 age 私钥文件。未指定方式时，首次登记会提示选择旧钥匙或新钥匙")
    group.add_argument("--generate", type=Path, metavar="FILE", help="在 Mac 本地生成一把原钥匙，新文件必须尚不存在")
    args = parser.parse_args()
    remote_args(args.host, args.project_dir, args.project)
    check(1 <= args.port <= 65535, "SSH 端口无效。")
    check(not (args.restore_key and args.generate), "恢复须选择源备份对应的已有原钥匙。")
    path, generate = enrollment_path(args)
    age = Age()
    secret, recipient = custody(path, generate, age)
    print(f"已在本机核对原钥匙，准备向 {args.project} 登记并等待回执。", file=sys.stderr, flush=True)
    try:
        with session(args.host, args.port) as channel:
            receipt = exchange(channel, args, secret, recipient, age)
    except (EnrollmentError, OSError, subprocess.SubprocessError):
        print("若本次登记中断，重试时请保留这把原钥匙，使用 --key " + shlex.quote(str(path)) + "。", file=sys.stderr)
        if not args.restore_key:
            print("若本站已登记过另一把钥匙，请选择本站原先登记的 .agekey 原文件。", file=sys.stderr)
        raise
    if args.restore_key:
        print(f"本次恢复钥匙已核对。目标项目 {receipt['project']}，站点 {receipt['instance_id']}。")
    else:
        print(f"本站备份钥匙登记通过。项目 {receipt['project']}，站点 {receipt['instance_id']}。")
        print("原钥匙保存在 " + str(path) + "。本站现在可以自行解密核验备份。")


if __name__ == "__main__":
    try:
        main()
    except EnrollmentError as exc:
        sys.exit(str(exc))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        sys.exit(f"登记未完成（{type(exc).__name__}）。请核对本机钥匙路径、SSH 管理权限及项目目录。")
