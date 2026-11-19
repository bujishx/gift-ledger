"""Verify and cache the official age CLI pair for the current desktop user."""

import hashlib
import http.client
import os
from pathlib import Path
import platform
import stat
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile


AGE_VERSION = "1.3.2"
VERSION = "v" + AGE_VERSION
# Source: https://github.com/FiloSottile/age/releases/expanded_assets/v1.3.2
# Each tuple pins the official archive and its two executable members, in order.
RELEASES = {
    "darwin-amd64": (
        "1d1e4bc66e1427edad7739ae7616157de0e79db8b6d2a1497d7d9925fb06a539",
        "0c2fbcffab91d0af398162a8ed792e096916c8c47c3c400e303904431103f492",
        "33c25cc60fdfbb27438f885e0e9f6e2e2c29c0a64449738bff57b6aebe42f67f",
    ),
    "darwin-arm64": (
        "e2020b073c44f692685a24d6abc378817eb81ffaaf49fd0531ef8565f767f2f5",
        "4012dfc2725883beafb710894af4f599b7a94f8c8e0f51f02cc96ab8df33915e",
        "c16e229245123d0ad27442317461d63915416cad0294395cd19ca93feb3211ea",
    ),
    "windows-amd64": (
        "f48d8f8f9ebe903ab5027ed067652f2cc1db94bc206976430133b905dcd8e8c7",
        "2821a4ed191da07372acd302e5f6feae7a7985e285e1417765ebe74025af45f0",
        "1549c7049be32695594bedd09bbd352a94b6013a9d5c43364f3c6cd7a09ab61c",
    ),
    "windows-arm64": (
        "fae351336c8d5f30f93cc3ccdec16d1ed7851d68cdd6656d41955cc31a447c9d",
        "e20a1438fc1b6951045f4aca2afc456f61e1cc311224809e352d82fc4be489a1",
        "395c5371e3ff985fa4df47f05bb7b5aa0095fb5875f588ffc8bb0cb1da53b8ab",
    ),
}
PROGRAM_NAMES = ("age", "age-keygen")
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_PROGRAM_BYTES = 64 * 1024 * 1024


class NativeAgeError(ValueError):
    pass


def _platform_key():
    system = {"darwin": "darwin", "win32": "windows"}.get(sys.platform)
    machine = platform.machine().lower()
    if system == "windows":
        machine = os.environ.get("PROCESSOR_ARCHITEW6432", machine).lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    key = f"{system}-{arch}"
    if key not in RELEASES:
        raise NativeAgeError(f"本版本没有适用于 {sys.platform}/{machine} 的原生 age 工具包。")
    return key


def _is_link(info):
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _cache_directory(key, create):
    if key.startswith("windows-"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        base = Path.home() / "Library/Application Support"
    if not base.is_absolute():
        raise NativeAgeError("找不到当前用户的本机应用程序目录。")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    folder = base
    for part in ("GiftLedger", "age", VERSION, key):
        folder = folder / part
        if create:
            folder.mkdir(mode=0o700, exist_ok=True)
        info = folder.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise NativeAgeError("原生 age 缓存目录异常，请核对当前用户的应用程序目录。")
        if os.name == "posix":
            forbidden = 0o022 if part == "GiftLedger" else 0o077
            if info.st_uid != os.getuid() or info.st_mode & forbidden:
                raise NativeAgeError("原生 age 缓存目录的所有者或权限不符，请核对当前用户的应用程序目录。")
    return folder


def _expected_programs(key):
    extension = ".exe" if key.startswith("windows-") else ""
    return [(name, name + extension, digest) for name, digest in zip(PROGRAM_NAMES, RELEASES[key][1:])]


def _matches(path, digest):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (_is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROGRAM_BYTES
            or (os.name == "posix" and (info.st_uid != os.getuid() or not info.st_mode & 0o100
                                         or info.st_mode & 0o022))):
        return False
    contents = hashlib.sha256()
    with path.open("rb") as src:
        if not os.path.samestat(info, os.fstat(src.fileno())):
            return False
        for block in iter(lambda: src.read(256 * 1024), b""):
            contents.update(block)
    return contents.hexdigest() == digest


def cached_programs():
    """Return both pinned executable paths, or {} if either is missing or damaged."""
    key = _platform_key()
    try:
        folder = _cache_directory(key, create=False)
        programs = {}
        for name, filename, digest in _expected_programs(key):
            path = folder / filename
            if not _matches(path, digest):
                return {}
            programs[name] = str(path)
        return programs
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise NativeAgeError("无法读取当前用户的原生 age 缓存。") from exc


def _download(key, destination):
    suffix = ".zip" if key.startswith("windows-") else ".tar.gz"
    url = f"https://github.com/FiloSottile/age/releases/download/{VERSION}/age-{VERSION}-{key}{suffix}"
    digest = hashlib.sha256()
    size = 0
    print(f"正在从 GitHub 下载适用于 {key} 的原生 age {VERSION}。", flush=True)
    started = time.monotonic()
    next_report = started + 5
    try:
        with urlopen(Request(url, headers={"User-Agent": "GiftLedger"}), timeout=15) as remote, destination.open("xb") as out:
            if not remote.geturl().startswith("https://"):
                raise NativeAgeError("age 发行包的下载地址未使用 HTTPS。")
            for block in iter(lambda: remote.read1(256 * 1024), b""):
                size += len(block)
                now = time.monotonic()
                if size > MAX_ARCHIVE_BYTES or now - started > 300:
                    raise NativeAgeError("age 发行包下载超时或大小超限。")
                digest.update(block)
                out.write(block)
                if now >= next_report:
                    print(f"已下载 {size / 1_000_000:.1f} MB。", flush=True)
                    next_report = now + 5
    except (OSError, http.client.HTTPException) as exc:
        raise NativeAgeError(f"下载 age {VERSION} 失败，请核对到 GitHub 的网络和本机磁盘空间。") from exc
    if digest.hexdigest() != RELEASES[key][0]:
        raise NativeAgeError(f"age {VERSION} 发行包 SHA-256 核验失败。")


def _stage_programs(key, archive, stage):
    windows = key.startswith("windows-")
    with (zipfile.ZipFile(archive) if windows else tarfile.open(archive, "r:gz")) as bundle:
        entries = bundle.infolist() if windows else bundle.getmembers()
        for _, filename, expected in _expected_programs(key):
            member_name = "age/" + filename
            matches = [e for e in entries if (e.filename if windows else e.name) == member_name]
            if len(matches) != 1:
                raise NativeAgeError(f"age 发行包中未找到唯一的 {filename}。")
            entry = matches[0]
            if windows:
                mode = stat.S_IFMT(entry.external_attr >> 16)
                regular = not entry.is_dir() and mode in (0, stat.S_IFREG)
                size = entry.file_size
            else:
                regular = entry.isfile()
                size = entry.size
            if not regular or not 0 < size <= MAX_PROGRAM_BYTES:
                raise NativeAgeError(f"age 发行包中的 {filename} 文件类型或大小不符。")
            path = stage / filename
            with (bundle.open(entry) if windows else bundle.extractfile(entry)) as src, path.open("xb") as out:
                digest = hashlib.sha256()
                for block in iter(lambda: src.read(256 * 1024), b""):
                    digest.update(block)
                    out.write(block)
            if digest.hexdigest() != expected:
                raise NativeAgeError(f"{filename} 的 SHA-256 核验失败。")
            path.chmod(0o700)


def install_programs():
    """Download the release once and publish both verified binaries for this user."""
    existing = cached_programs()
    if existing:
        return existing
    key = _platform_key()
    try:
        folder = _cache_directory(key, create=True)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=folder) as temporary:
            stage = Path(temporary)
            archive = stage / "release"
            _download(key, archive)
            _stage_programs(key, archive, stage)
            # Every writer installs identical pinned bytes. Readers require both valid
            # executables, so an interrupted publish is repaired by the next attempt.
            for _, filename, digest in _expected_programs(key):
                path = folder / filename
                if not _matches(path, digest):
                    try:
                        os.replace(stage / filename, path)
                    except PermissionError:
                        # Another invocation can publish and start this .exe on Windows.
                        if not _matches(path, digest):
                            raise
        programs = cached_programs()
        if not programs:
            raise NativeAgeError("本机缓存中的 age 工具未通过完整核验。")
        print(f"原生 age {VERSION} 和 age-keygen 已通过核验，可以使用。", flush=True)
        return programs
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise NativeAgeError(f"无法将 age {VERSION} 安装到当前用户的工具目录。") from exc
