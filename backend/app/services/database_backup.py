# ruff: noqa: RUF002, RUF003
"""管理员整库备份的核心：mysqldump 执行与 ``.gladminbackup`` 包生成／严格解析。

本模块只负责与 MySQL 8.4 官方 ``mysqldump`` 客户端交互、生成事务一致的原始 SQL 转储，
并把「严格版本化 manifest + gzip 压缩 SQL」打包成单文件 ``.gladminbackup``（ZIP 容器），
以及从磁盘或网络读取该包时做严格的结构与校验。备份索引、保留清理和异地加密复制分别由
``backup_index``、``admin_backup`` 和 ``offsite_replication`` 负责。

安全边界：

- 数据库密码绝不进入命令参数、进程列表、日志或异常信息；只写入权限 0600 的临时 MySQL
  ``defaults-extra-file``，无论成功失败都在 ``finally`` 中删除；
- 子进程使用 ``subprocess.run`` 的显式超时，任何异常都映射为稳定的中文业务错误；
- manifest 不包含数据库密码、JWT 密钥、完整连接串、绝对服务器路径或用户明细；
- 包解析把包内容视为不可信输入：拒绝空包、损坏 ZIP、加密条目、目录／符号链接、路径穿越、
  反斜杠路径、重复条目、额外成员、超限内容和校验不一致。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import secrets
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from sqlalchemy import inspect, text
from sqlalchemy.engine import URL, make_url

# ---------------------------------------------------------------- 固定包格式常量

FORMAT_NAME = "gift-ledger-admin-backup"
FORMAT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
SQL_GZ_FILENAME = "dump.sql.gz"
PACKAGE_SUFFIX = ".gladminbackup"

BACKUP_TYPE_AUTOMATIC = "automatic"
BACKUP_TYPE_MANUAL = "manual"
BACKUP_TYPE_PRE_MAINTENANCE = "pre-maintenance"
BACKUP_TYPE_REHEARSAL = "rehearsal"
BACKUP_TYPES = frozenset(
    {
        BACKUP_TYPE_AUTOMATIC,
        BACKUP_TYPE_MANUAL,
        BACKUP_TYPE_PRE_MAINTENANCE,
        BACKUP_TYPE_REHEARSAL,
    }
)

DEFAULT_MAX_SQL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_ENTRIES = 10
MAX_COMPRESSION_RATIO = 200
MAX_MANIFEST_BYTES = 1024 * 1024
_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# ---------------------------------------------------------------- 错误码

ERROR_COMMAND_TIMEOUT = "backup_command_timeout"
ERROR_COMMAND_FAILED = "backup_command_failed"
ERROR_TOOL_UNAVAILABLE = "backup_tool_unavailable"
ERROR_CONNECTION_UNAVAILABLE = "backup_connection_unavailable"
ERROR_METADATA_FAILED = "backup_metadata_failed"
ERROR_WRITE_FAILED = "backup_write_failed"

ERROR_EMPTY_FILE = "backup_empty_file"
ERROR_NOT_ZIP = "backup_not_zip"
ERROR_ZIP_CORRUPTED = "backup_zip_corrupted"
ERROR_ZIP_ENCRYPTED = "backup_zip_encrypted"
ERROR_ZIP_ENTRY_TYPE = "backup_zip_entry_type"
ERROR_ZIP_UNSAFE_PATH = "backup_zip_unsafe_path"
ERROR_ZIP_DUPLICATE_ENTRY = "backup_zip_duplicate_entry"
ERROR_ZIP_TOO_MANY_ENTRIES = "backup_zip_too_many_entries"
ERROR_ZIP_COMPRESSION_RATIO = "backup_zip_compression_ratio"
ERROR_ZIP_COMPRESSION_UNSUPPORTED = "backup_zip_compression_unsupported"
ERROR_ZIP_UNEXPECTED_FILES = "backup_zip_unexpected_files"
ERROR_JSON_INVALID = "backup_json_invalid"
ERROR_FORMAT_UNSUPPORTED = "backup_format_unsupported"
ERROR_VERSION_UNSUPPORTED = "backup_version_unsupported"
ERROR_MANIFEST_INVALID = "backup_manifest_invalid"
ERROR_SQL_SIZE_MISMATCH = "backup_sql_size_mismatch"
ERROR_SQL_SHA_MISMATCH = "backup_sql_sha_mismatch"
ERROR_SQL_TOO_LARGE = "backup_sql_too_large"


class DatabaseBackupError(Exception):
    """整库备份生成或解析的稳定业务错误，含 ``code`` 与中文 ``message``。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------- 连接信息


@dataclass(frozen=True, slots=True)
class MysqlConnection:
    """从应用数据库地址解析出的连接要素（密码不在任何日志或命令中出现）。"""

    host: str
    port: int
    user: str
    password: str | None
    database: str | None


def parse_mysql_connection(database_url: str) -> MysqlConnection:
    url: URL = make_url(database_url)
    return MysqlConnection(
        host=url.host or "127.0.0.1",
        port=url.port or 3306,
        user=url.username or "",
        password=url.password,
        database=url.database or None,
    )


# ---------------------------------------------------------------- 临时凭据文件


def _mysql_option_value(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _write_client_defaults_file(connection: MysqlConnection, directory: Path) -> Path:
    """把连接要素写入权限 0600 的临时 defaults-extra-file，返回路径；由调用方负责删除。"""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lines = ["[client]"]
    if connection.host:
        lines.append(f"host={_mysql_option_value(connection.host)}")
    if connection.port:
        lines.append(f"port={connection.port}")
    if connection.user:
        lines.append(f"user={_mysql_option_value(connection.user)}")
    if connection.password:
        lines.append(f"password={_mysql_option_value(connection.password)}")
    content = ("\n".join(lines) + "\n").encode("utf-8")

    fd, raw_path = tempfile.mkstemp(prefix=".glcred-", dir=str(directory))
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except OSError:
        _unlink_quietly(path)
        raise DatabaseBackupError(ERROR_WRITE_FAILED, "无法创建数据库临时凭据文件") from None
    return path


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 子进程封装


def _run_command(
    argv: list[str],
    *,
    timeout: int,
    stdout_file=None,
    stdin=None,
) -> None:
    try:
        result = subprocess.run(
            argv,
            stdout=stdout_file,
            stderr=subprocess.PIPE,
            stdin=stdin,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DatabaseBackupError(ERROR_TOOL_UNAVAILABLE, "找不到数据库工具，请检查安装") from exc
    except subprocess.TimeoutExpired as exc:
        raise DatabaseBackupError(ERROR_COMMAND_TIMEOUT, "数据库命令执行超时") from exc
    except OSError as exc:
        raise DatabaseBackupError(ERROR_COMMAND_FAILED, "数据库命令执行失败") from exc
    if result.returncode != 0:
        # 明确错误映射；不把 stderr 原文透出（可能含环境信息），只保留错误类型。
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes)
            else str(result.stderr or "")
        )
        match = re.search(r"\bERROR\s+(\d+)\b", stderr)
        code = int(match.group(1)) if match else None
        message = {
            1045: "数据库登录失败，请检查备份账号和权限",
            1049: "目标数据库不存在",
            2002: "无法连接数据库服务，请检查服务状态",
            2003: "无法连接数据库服务，请检查服务状态",
            2013: "数据库连接中断，请检查服务状态后重试",
        }.get(code, "数据库命令执行失败")
        raise DatabaseBackupError(ERROR_COMMAND_FAILED, message)


def run_mysqldump(
    connection: MysqlConnection,
    *,
    database: str,
    mysqldump_path: str,
    output_path: Path,
    work_dir: Path,
    timeout: int,
) -> None:
    """执行 mysqldump，把事务一致 SQL 写入 ``output_path``（原始 SQL 文件）。"""
    defaults_file = _write_client_defaults_file(connection, work_dir)
    try:
        argv = [
            mysqldump_path,
            f"--defaults-extra-file={defaults_file}",
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--default-character-set=utf8mb4",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
            database,
        ]
        with output_path.open("xb") as output_file:
            os.chmod(output_path, 0o600)
            _run_command(argv, timeout=timeout, stdout_file=output_file)
    finally:
        _unlink_quietly(defaults_file)


def run_mysql_import(
    connection: MysqlConnection,
    *,
    database: str,
    mysql_path: str,
    sql_stream,
    work_dir: Path,
    timeout: int,
) -> None:
    """把 SQL 通过 mysql 客户端导入目标库；密码只进入临时凭据文件。"""
    defaults_file = _write_client_defaults_file(connection, work_dir)
    try:
        argv = [
            mysql_path,
            f"--defaults-extra-file={defaults_file}",
            "--default-character-set=utf8mb4",
            database,
        ]
        _run_command(argv, timeout=timeout, stdin=sql_stream)
    finally:
        _unlink_quietly(defaults_file)


def run_mysql_script(
    connection: MysqlConnection,
    *,
    mysql_path: str,
    statement: str,
    work_dir: Path,
    timeout: int,
) -> None:
    """执行单条管理 SQL（如建库）；密码只进入临时凭据文件。"""
    defaults_file = _write_client_defaults_file(connection, work_dir)
    try:
        argv = [
            mysql_path,
            f"--defaults-extra-file={defaults_file}",
            "--default-character-set=utf8mb4",
            "--batch",
            "--skip-column-names",
            "-e",
            statement,
        ]
        _run_command(argv, timeout=timeout)
    finally:
        _unlink_quietly(defaults_file)


def run_mysql_query(
    connection: MysqlConnection,
    *,
    mysql_path: str,
    statement: str,
    work_dir: Path,
    timeout: int,
) -> str:
    """执行查询并返回输出；密码只进入临时凭据文件。"""
    defaults_file = _write_client_defaults_file(connection, work_dir)
    try:
        argv = [
            mysql_path,
            f"--defaults-extra-file={defaults_file}",
            "--default-character-set=utf8mb4",
            "--batch",
            "--skip-column-names",
            "-e",
            statement,
        ]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DatabaseBackupError(ERROR_TOOL_UNAVAILABLE, "找不到数据库工具") from exc
        except subprocess.TimeoutExpired as exc:
            raise DatabaseBackupError(ERROR_COMMAND_TIMEOUT, "数据库命令执行超时") from exc
        if result.returncode != 0:
            raise DatabaseBackupError(ERROR_COMMAND_FAILED, "数据库命令执行失败")
        return result.stdout.decode("utf-8", errors="replace")
    finally:
        _unlink_quietly(defaults_file)


def get_mysqldump_version(mysqldump_path: str, *, timeout: int) -> str:
    try:
        result = subprocess.run(
            [mysqldump_path, "--version"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise DatabaseBackupError(ERROR_TOOL_UNAVAILABLE, "找不到数据库备份工具") from exc
    if result.returncode != 0:
        raise DatabaseBackupError(ERROR_TOOL_UNAVAILABLE, "找不到数据库备份工具")
    lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        raise DatabaseBackupError(ERROR_TOOL_UNAVAILABLE, "无法识别数据库备份工具版本")
    first_line = lines[0]
    version = re.search(r"\bVer\s+([0-9A-Za-z._-]+)", first_line)
    if version is None:
        raise DatabaseBackupError(ERROR_TOOL_UNAVAILABLE, "无法识别数据库备份工具版本")
    return f"mysqldump Ver {version.group(1)}"


# ---------------------------------------------------------------- 数据库元数据


def gather_database_metadata(engine) -> dict:
    """读取逻辑库名、alembic 版本和每张表行数（只读，不修改数据）。"""
    try:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT DATABASE()")).scalar_one()
            alembic_version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            table_names = inspect(connection).get_table_names()
            tables: dict[str, int] = {}
            for table_name in sorted(table_names):
                count = connection.execute(
                    text(f"SELECT COUNT(*) FROM `{table_name}`")
                ).scalar_one()
                tables[table_name] = int(count)
    except Exception as exc:
        raise DatabaseBackupError(ERROR_METADATA_FAILED, "读取数据库元数据失败") from exc
    return {"database": database, "alembic_version": alembic_version, "tables": tables}


# ---------------------------------------------------------------- 哈希与压缩


def sha256_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def gzip_file(source: Path, target: Path) -> None:
    with source.open("rb") as raw, target.open("xb") as target_stream:
        os.chmod(target, 0o600)
        with gzip.GzipFile(fileobj=target_stream, mode="wb", compresslevel=6) as compressed:
            while True:
                chunk = raw.read(1024 * 1024)
                if not chunk:
                    break
                compressed.write(chunk)


def sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


# ---------------------------------------------------------------- 备份编号


def new_backup_id() -> str:
    return secrets.token_urlsafe(18)


# ---------------------------------------------------------------- 包生成


def build_admin_backup_package(
    *,
    manifest: dict,
    gzip_sql_path: Path,
) -> bytes:
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(_zip_info(MANIFEST_FILENAME), manifest_bytes)
        # SQL 已经是 gzip，用 STORED 避免二次压缩。
        archive.write(gzip_sql_path, arcname=SQL_GZ_FILENAME, compress_type=ZIP_STORED)
    return buffer.getvalue()


def write_admin_backup_package(
    *,
    manifest: dict,
    gzip_sql_path: Path,
    output_path: Path,
) -> None:
    """把备份包直接写入磁盘，避免整库备份在内存中形成第二份完整副本。"""

    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    try:
        fd = os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(_zip_info(MANIFEST_FILENAME), manifest_bytes)
            archive.write(gzip_sql_path, arcname=SQL_GZ_FILENAME, compress_type=ZIP_STORED)
        os.chmod(output_path, 0o600)
    except (OSError, ValueError, BadZipFile):
        _unlink_quietly(output_path)
        raise DatabaseBackupError(ERROR_WRITE_FAILED, "无法写入备份文件") from None


def _zip_info(filename: str) -> ZipInfo:
    info = ZipInfo(filename)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    info.date_time = (1980, 1, 1, 0, 0, 0)
    return info


# ---------------------------------------------------------------- 包解析


@dataclass(frozen=True, slots=True)
class ParsedAdminBackup:
    """严格解析并校验后的整库备份元数据。"""

    format_name: str
    format_version: int
    backup_id: str
    backup_type: str
    created_at: datetime
    database: str
    schema_revision: str
    application_version: str
    mysqldump_version: str
    sql_sha256: str
    sql_size: int
    alembic_version: str
    tables: dict[str, int]


def _is_windows_absolute(name: str) -> bool:
    return len(name) >= 2 and name[0].isalpha() and name[1] == ":"


def _check_entry_name(entry: ZipInfo) -> None:
    name = entry.filename
    if "\\" in name:
        raise DatabaseBackupError(ERROR_ZIP_UNSAFE_PATH, "备份包含反斜杠路径条目，已拒绝解析")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _is_windows_absolute(normalized):
        raise DatabaseBackupError(ERROR_ZIP_UNSAFE_PATH, "备份包含绝对路径条目，已拒绝解析")
    if ".." in normalized.split("/"):
        raise DatabaseBackupError(ERROR_ZIP_UNSAFE_PATH, "备份包含上级目录穿越条目，已拒绝解析")


def _check_entry_type(entry: ZipInfo) -> None:
    if entry.is_dir():
        raise DatabaseBackupError(ERROR_ZIP_ENTRY_TYPE, "备份包含目录条目，已拒绝解析")
    mode = (entry.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise DatabaseBackupError(ERROR_ZIP_ENTRY_TYPE, "备份包含符号链接条目，已拒绝解析")


def _check_compression_ratio(entry: ZipInfo) -> None:
    if entry.compress_size <= 0 or entry.file_size <= entry.compress_size * MAX_COMPRESSION_RATIO:
        return
    raise DatabaseBackupError(ERROR_ZIP_COMPRESSION_RATIO, "备份压缩比例异常，已拒绝解析")


def _parse_json_bytes(data: bytes, context: str) -> dict:
    if data.startswith(b"\xef\xbb\xbf"):
        raise DatabaseBackupError(ERROR_JSON_INVALID, f"{context} 含 BOM，已拒绝解析")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DatabaseBackupError(ERROR_JSON_INVALID, f"{context} 不是合法 JSON") from exc
    if type(value) is not dict:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, f"{context} 结构不正确")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _expect_string(obj: dict, key: str) -> str:
    value = obj.get(key)
    if type(value) is not str:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "manifest 字段不正确")
    return value


def _expect_int(obj: dict, key: str) -> int:
    value = obj.get(key)
    if type(value) is not int or type(value) is bool:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "manifest 字段不正确")
    return value


def _expect_tables(obj: dict) -> dict[str, int]:
    value = obj.get("tables")
    if type(value) is not dict:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "manifest 的 tables 字段不正确")
    result: dict[str, int] = {}
    for name, count in value.items():
        if type(name) is not str or type(count) is not int or type(count) is bool or count < 0:
            raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "manifest 的 tables 字段不正确")
        result[name] = count
    return result


def _verify_sql_member(
    content: bytes | Path,
    *,
    expected_size: int,
    expected_sha: str,
    max_sql_bytes: int,
) -> None:
    """重新打开包流式解压 SQL 成员，核对大小与 SHA-256，防止解压炸弹与内容不一致。"""
    try:
        archive = ZipFile(io.BytesIO(content) if isinstance(content, bytes) else content)
    except BadZipFile as exc:
        raise DatabaseBackupError(ERROR_NOT_ZIP, "文件不是有效的整库备份包") from exc
    with archive:
        try:
            stream = archive.open(SQL_GZ_FILENAME, "r")
        except (BadZipFile, KeyError, RuntimeError, EOFError) as exc:
            raise DatabaseBackupError(ERROR_ZIP_CORRUPTED, "备份压缩包已损坏，无法读取") from exc
        with stream, gzip.GzipFile(fileobj=stream, mode="rb") as gz:
            digest = sha256()
            size = 0
            try:
                while True:
                    chunk = gz.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_sql_bytes:
                        raise DatabaseBackupError(
                            ERROR_SQL_TOO_LARGE, "备份内 SQL 解压后超过允许大小"
                        )
                    digest.update(chunk)
            except DatabaseBackupError:
                raise
            except (BadZipFile, OSError, EOFError, RuntimeError, zlib.error) as exc:
                raise DatabaseBackupError(
                    ERROR_ZIP_CORRUPTED, "备份压缩包已损坏，无法读取"
                ) from exc
            if size != expected_size:
                raise DatabaseBackupError(ERROR_SQL_SIZE_MISMATCH, "备份 SQL 大小校验失败")
            if digest.hexdigest() != expected_sha:
                raise DatabaseBackupError(ERROR_SQL_SHA_MISMATCH, "备份 SQL 校验失败")


def parse_admin_backup(
    content: bytes | Path,
    *,
    max_sql_bytes: int = DEFAULT_MAX_SQL_BYTES,
) -> ParsedAdminBackup:
    """严格解析并校验 ``.gladminbackup`` 字节，不访问数据库。"""
    if isinstance(content, bytes):
        if not content:
            raise DatabaseBackupError(ERROR_EMPTY_FILE, "备份文件是空的")
        zip_source = io.BytesIO(content)
    else:
        try:
            if content.stat().st_size == 0:
                raise DatabaseBackupError(ERROR_EMPTY_FILE, "备份文件是空的")
        except OSError as exc:
            raise DatabaseBackupError(ERROR_NOT_ZIP, "无法读取整库备份包") from exc
        zip_source = content

    try:
        archive = ZipFile(zip_source)
    except BadZipFile as exc:
        raise DatabaseBackupError(ERROR_NOT_ZIP, "文件不是有效的整库备份包") from exc

    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise DatabaseBackupError(
                ERROR_ZIP_TOO_MANY_ENTRIES, f"压缩包内有 {len(entries)} 个条目，超过上限。"
            )
        seen: set[str] = set()
        for entry in entries:
            if entry.flag_bits & 0x1:
                raise DatabaseBackupError(ERROR_ZIP_ENCRYPTED, "备份包含加密条目，已拒绝解析")
            _check_entry_type(entry)
            _check_entry_name(entry)
            normalized = entry.filename.replace("\\", "/")
            if normalized in seen:
                raise DatabaseBackupError(ERROR_ZIP_DUPLICATE_ENTRY, "备份包含重复条目，已拒绝解析")
            seen.add(normalized)
            if normalized == MANIFEST_FILENAME:
                if entry.compress_type != ZIP_DEFLATED:
                    raise DatabaseBackupError(
                        ERROR_ZIP_COMPRESSION_UNSUPPORTED, "备份包含不受支持的压缩方式"
                    )
                if entry.file_size > MAX_MANIFEST_BYTES:
                    raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "备份 manifest 超过大小上限")
                _check_compression_ratio(entry)
            elif normalized == SQL_GZ_FILENAME:
                _check_compression_ratio(entry)
                if entry.file_size > max_sql_bytes + 1024 * 1024:
                    raise DatabaseBackupError(
                        ERROR_SQL_TOO_LARGE, "备份内 SQL 压缩成员超过允许大小"
                    )
            else:
                raise DatabaseBackupError(ERROR_ZIP_UNEXPECTED_FILES, "备份包含未知成员")
        names = {entry.filename for entry in entries}
        if names != {MANIFEST_FILENAME, SQL_GZ_FILENAME}:
            raise DatabaseBackupError(
                ERROR_ZIP_UNEXPECTED_FILES,
                f"备份应只包含 {MANIFEST_FILENAME} 和 {SQL_GZ_FILENAME} 两个文件。",
            )
        try:
            manifest_bytes = archive.read(MANIFEST_FILENAME)
        except (BadZipFile, RuntimeError, KeyError, EOFError) as exc:
            raise DatabaseBackupError(ERROR_ZIP_CORRUPTED, "备份压缩包已损坏，无法读取") from exc

    manifest = _parse_json_bytes(manifest_bytes, MANIFEST_FILENAME)
    allowed_keys = {
        "format",
        "format_version",
        "backup_id",
        "backup_type",
        "created_at",
        "database",
        "schema_revision",
        "application_version",
        "mysqldump_version",
        "sql_sha256",
        "sql_size",
        "alembic_version",
        "tables",
    }
    if set(manifest) != allowed_keys:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "manifest 字段集合不正确")

    format_name = _expect_string(manifest, "format")
    if format_name != FORMAT_NAME:
        raise DatabaseBackupError(ERROR_FORMAT_UNSUPPORTED, "备份格式不受支持")
    format_version = _expect_int(manifest, "format_version")
    if format_version != FORMAT_VERSION:
        raise DatabaseBackupError(ERROR_VERSION_UNSUPPORTED, "备份格式版本不受支持")

    backup_id = _expect_string(manifest, "backup_id")
    if _BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "备份编号格式不正确")
    backup_type = _expect_string(manifest, "backup_type")
    if backup_type not in BACKUP_TYPES:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "备份类型不正确")
    created_at_text = _expect_string(manifest, "created_at")
    try:
        created_at = datetime.fromisoformat(created_at_text)
    except ValueError as exc:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "备份生成时间格式不正确") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "备份生成时间必须使用 UTC")

    sql_sha256 = _expect_string(manifest, "sql_sha256")
    if _SHA256_PATTERN.fullmatch(sql_sha256) is None:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "SQL 校验值格式不正确")
    sql_size = _expect_int(manifest, "sql_size")
    if sql_size < 0:
        raise DatabaseBackupError(ERROR_MANIFEST_INVALID, "SQL 大小不正确")

    _verify_sql_member(
        content,
        expected_size=sql_size,
        expected_sha=sql_sha256,
        max_sql_bytes=max_sql_bytes,
    )

    return ParsedAdminBackup(
        format_name=format_name,
        format_version=format_version,
        backup_id=backup_id,
        backup_type=backup_type,
        created_at=created_at,
        database=_expect_string(manifest, "database"),
        schema_revision=_expect_string(manifest, "schema_revision"),
        application_version=_expect_string(manifest, "application_version"),
        mysqldump_version=_expect_string(manifest, "mysqldump_version"),
        sql_sha256=sql_sha256,
        sql_size=sql_size,
        alembic_version=_expect_string(manifest, "alembic_version"),
        tables=_expect_tables(manifest),
    )


__all__ = [
    "BACKUP_TYPES",
    "BACKUP_TYPE_AUTOMATIC",
    "BACKUP_TYPE_MANUAL",
    "BACKUP_TYPE_PRE_MAINTENANCE",
    "BACKUP_TYPE_REHEARSAL",
    "DEFAULT_MAX_SQL_BYTES",
    "ERROR_CONNECTION_UNAVAILABLE",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "MANIFEST_FILENAME",
    "PACKAGE_SUFFIX",
    "SQL_GZ_FILENAME",
    "DatabaseBackupError",
    "MysqlConnection",
    "ParsedAdminBackup",
    "build_admin_backup_package",
    "gather_database_metadata",
    "get_mysqldump_version",
    "gzip_file",
    "new_backup_id",
    "parse_admin_backup",
    "parse_mysql_connection",
    "run_mysql_import",
    "run_mysql_query",
    "run_mysql_script",
    "run_mysqldump",
    "sha256_bytes",
    "sha256_file",
    "write_admin_backup_package",
]
