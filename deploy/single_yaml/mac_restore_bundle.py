"""Open a five-file GiftLedger backup folder or a ZIP that contains it."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import tempfile
import zipfile


helpers = Path(__file__).resolve().parent
if helpers.is_dir():
    if not (helpers / "backup_store.py").is_file():
        helpers = helpers.parent / "release/backup_ops"
    sys.path.insert(0, str(helpers))
import backup_export
import backup_store as store


@dataclass(frozen=True)
class Bundle:
    folder: Path
    backup_id: str
    manifest_sha256: str
    filenames: tuple
    source: dict
    gift_count: int


def inspect(folder):
    manifest = store.inspect_bundle(folder)
    raw = (folder / "backup.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["files"]["backup.json"]:
        raise ValueError("读取期间备份回执已变化。")
    backup = json.loads(raw)
    details = backup["details"]
    source = {"source_project": manifest["project"], "source_instance_id": manifest["instance_id"],
              "source_server_uuid": manifest["server_uuid"], "source_operation_id": manifest["backup_operation_id"],
              "ciphertext": details["ciphertext"], "sha256": details["ciphertext_sha256"]}
    return Bundle(folder, manifest["backup_operation_id"], store.digest(store.canonical(manifest)),
                  tuple(sorted({*manifest["files"], "BUNDLE.json", "SHA256SUMS"})), source,
                  details["fingerprint"]["tables"]["gift_records"]["rows"])


def check_zip_directory(stream):
    # Cap central metadata before ZipFile allocates it. This is a five-member
    # product archive. Some older exports add cover receipts outside backup/.
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - 65557))
    tail = stream.read()
    header = tail.rfind(b"PK\x05\x06")
    if header < 0 or len(tail) - header < 22:
        raise ValueError("ZIP 目录不完整。")
    disk, directory_disk, on_disk, entries, directory_bytes, _, comment = struct.unpack_from(
        "<4H2IH", tail, header + 4)
    if (disk or directory_disk or on_disk != entries or not 5 <= entries <= 256
            or directory_bytes > 2 * 1024**2 or len(tail) != header + 22 + comment):
        raise ValueError("请选择包含一套五文件备份的 ZIP。")
    stream.seek(0)


def extract_five(stream, folder):
    check_zip_directory(stream)
    with zipfile.ZipFile(stream) as package:
        items = package.infolist()
        manifests = [i for i in items if PurePosixPath(i.filename).name == "BUNDLE.json"
                     and "__MACOSX" not in PurePosixPath(i.filename).parts]
        if len(manifests) != 1:
            raise ValueError("ZIP 需要包含一套完整的备份。")
        parent = PurePosixPath(manifests[0].filename).parent
        chosen = [i for i in items if PurePosixPath(i.filename).parent == parent and not i.is_dir()]
        names = {PurePosixPath(i.filename).name for i in chosen}
        fixed = {"BUNDLE.json", "backup.json", "verify.json", "SHA256SUMS"}
        if len(chosen) != 5 or len(names) != 5 or not fixed.issubset(names) or not re.fullmatch(
                r"backup-[0-9a-f]{32}\.glgui\.age", next(iter(names - fixed), "")):
            raise ValueError("备份目录需要包含五份成套文件。")
        if sum(i.file_size for i in chosen) > shutil.disk_usage(folder).free:
            raise ValueError("Mac 临时目录空间不足，无法展开这份备份。")
        for item in chosen:
            path = PurePosixPath(item.filename)
            kind = stat.S_IFMT(item.external_attr >> 16)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename or kind not in (0, stat.S_IFREG)
                    or item.flag_bits & 1 or (path.name in fixed and item.file_size > store.MAX_JSON)):
                raise ValueError("ZIP 中的备份成员无效。")
            # Only fixed basenames are written into a new private directory.
            with package.open(item) as source, (folder / path.name).open("xb") as dest:
                shutil.copyfileobj(source, dest, length=1024**2)


@contextmanager
def open_bundle(path, *, temporary_directory=None):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("请选择备份原文件或其实际目录。")
    with tempfile.TemporaryDirectory(prefix="giftledger-restore-", dir=temporary_directory) as temporary:
        folder = Path(temporary).resolve()
        if path.is_dir():
            receipt = backup_export.export_bundle(path, folder)
            yield inspect(folder / receipt["backup_operation_id"])
            return
        if not stat.S_ISREG(path.lstat().st_mode) or path.suffix.lower() != ".zip":
            raise ValueError("请选择五文件备份目录或 ZIP。")
        with path.open("rb") as stream:
            extract_five(stream, folder)
        yield inspect(folder)
