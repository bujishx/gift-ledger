# ruff: noqa: RUF002
"""整库恢复的本机维护命令：恢复到新的空数据库并核对结果。

恢复流程（对应 D064/D065）：

1. 校验备份包（离线解析与 SHA-256）；
2. 把备份恢复到**新的空数据库**；
3. 核对 ``alembic_version``、表清单和各表行数；
4. 只读核对恢复库位于当前 Alembic head，且 ORM 没有待迁移结构差异；
5. 输出下一步冒烟测试清单，等待人工切换应用数据库。

安全边界：

- 目标库名由操作者明确输入两次或使用 ``--confirm-target`` 完全一致确认；
- 永久拒绝目标 ``gift_ledger_v2``、``contact_db``、空名称和非允许前缀；正式维护恢复目标只允许
  ``gift_ledger_v2_restore_...``，自动测试只允许 ``gift_ledger_v2_test_admin_backup_...``；
- 默认拒绝已经存在或非空的目标库，绝不自动 drop 正式库或任意数据库；
- 数据库密码只进入临时凭据文件，绝不进入命令参数或日志；
- 恢复命令只准备和验证新库，绝不改 ``.env``、重启服务或切换正式连接。
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import URL

from app.core.database import get_engine
from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.services.database_backup import (
    DEFAULT_MAX_SQL_BYTES,
    SQL_GZ_FILENAME,
    DatabaseBackupError,
    MysqlConnection,
    gather_database_metadata,
    parse_admin_backup,
    run_mysql_import,
    run_mysql_query,
    run_mysql_script,
)

ALLOWED_RESTORE_PREFIX = "gift_ledger_v2_restore_"
ALLOWED_TEST_PREFIX = "gift_ledger_v2_test_admin_backup_"
FORBIDDEN_TARGETS = frozenset({"gift_ledger_v2", "contact_db", ""})
_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ALEMBIC_CONFIG = _BACKEND_DIR / "alembic.ini"


class RestoreError(Exception):
    """整库恢复的稳定业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    """恢复核对结果。"""

    target: str
    alembic_version: str
    expected_alembic_version: str
    tables_match: bool
    table_count: int
    smoke_checklist: tuple[str, ...]


def validate_restore_target(name: str, *, allow_test: bool) -> str:
    """校验并规范化恢复目标库名；不合法时抛 ``RestoreError``。"""
    if name in FORBIDDEN_TARGETS:
        raise RestoreError("RESTORE_TARGET_FORBIDDEN", "该目标库名被永久禁止，请重新指定")
    if _TARGET_PATTERN.fullmatch(name) is None:
        raise RestoreError("RESTORE_TARGET_INVALID", "目标库名只能包含字母、数字和下划线")
    required_prefix = ALLOWED_TEST_PREFIX if allow_test else ALLOWED_RESTORE_PREFIX
    if not name.startswith(required_prefix):
        raise RestoreError(
            "RESTORE_TARGET_PREFIX_INVALID",
            f"目标库名必须以 {required_prefix} 开头",
        )
    return name


def _confirm_target(target: str, confirm_target: str | None) -> None:
    if confirm_target is None or confirm_target != target:
        raise RestoreError(
            "RESTORE_TARGET_CONFIRM_MISMATCH", "两次输入的目标库名不一致，已拒绝执行"
        )


def _target_table_count(
    connection: MysqlConnection,
    *,
    target: str,
    mysql_path: str,
    work_dir: Path,
    timeout: int,
) -> int:
    """返回目标库中的表数量；目标库不存在时返回 -1。"""
    if not _database_exists(
        connection, target=target, mysql_path=mysql_path, work_dir=work_dir, timeout=timeout
    ):
        return -1
    quoted = target.replace("`", "")
    output = run_mysql_query(
        connection,
        mysql_path=mysql_path,
        statement=(
            f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '{quoted}'"
        ),
        work_dir=work_dir,
        timeout=timeout,
    )
    try:
        return int(output.strip())
    except ValueError as exc:
        raise RestoreError("RESTORE_PROBE_FAILED", "读取目标库状态失败") from exc


def _database_exists(
    connection: MysqlConnection,
    *,
    target: str,
    mysql_path: str,
    work_dir: Path,
    timeout: int,
) -> bool:
    quoted = target.replace("`", "")
    output = run_mysql_query(
        connection,
        mysql_path=mysql_path,
        statement=(
            f"SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = '{quoted}'"
        ),
        work_dir=work_dir,
        timeout=timeout,
    )
    try:
        return int(output.strip()) > 0
    except ValueError as exc:
        raise RestoreError("RESTORE_PROBE_FAILED", "读取目标库状态失败") from exc


def _extract_sql_gz(package_path: Path, work_dir: Path) -> Path:
    """从备份包中提取 ``dump.sql.gz`` 到受控临时目录。"""
    target = work_dir / SQL_GZ_FILENAME
    try:
        with ZipFile(package_path) as archive:
            if SQL_GZ_FILENAME not in archive.namelist():
                raise RestoreError("RESTORE_PACKAGE_INVALID", "备份包缺少 SQL 成员")
            with archive.open(SQL_GZ_FILENAME, "r") as source, target.open("wb") as dest:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    dest.write(chunk)
    except (BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise RestoreError("RESTORE_PACKAGE_INVALID", "备份包损坏，无法提取 SQL") from exc
    return target


def _gunzip_to_sql(gz_path: Path, work_dir: Path) -> Path:
    """把 ``dump.sql.gz`` 解压为普通 SQL 文件，供 mysql 客户端以原始文件描述符读取。"""
    sql_path = work_dir / "dump.sql"
    try:
        with gzip.open(gz_path, "rb") as source, sql_path.open("wb") as dest:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                dest.write(chunk)
    except OSError as exc:
        raise RestoreError("RESTORE_PACKAGE_INVALID", "备份包损坏，无法解压 SQL") from exc
    return sql_path


def restore_prepare(
    *,
    connection: MysqlConnection,
    package_path: Path,
    target: str,
    confirm_target: str | None,
    allow_test: bool,
    mysql_path: str,
    work_dir: Path,
    timeout: int,
    max_sql_bytes: int = DEFAULT_MAX_SQL_BYTES,
) -> str:
    """把备份恢复到新的空数据库，返回目标库名；绝不改 ``.env`` 或切换正式连接。"""
    target = validate_restore_target(target, allow_test=allow_test)
    _confirm_target(target, confirm_target)
    parse_admin_backup(package_path, max_sql_bytes=max_sql_bytes)

    existing_tables = _target_table_count(
        connection, target=target, mysql_path=mysql_path, work_dir=work_dir, timeout=timeout
    )
    if existing_tables >= 0:
        raise RestoreError("RESTORE_TARGET_EXISTS", "目标库已经存在，请改用一个全新的库名")
    if existing_tables == -1:
        run_mysql_script(
            connection,
            mysql_path=mysql_path,
            statement=(
                f"CREATE DATABASE `{target}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
            ),
            work_dir=work_dir,
            timeout=timeout,
        )

    gz_path = _extract_sql_gz(package_path, work_dir)
    sql_path: Path | None = None
    try:
        sql_path = _gunzip_to_sql(gz_path, work_dir)
        with sql_path.open("rb") as sql_stream:
            run_mysql_import(
                connection,
                database=target,
                mysql_path=mysql_path,
                sql_stream=sql_stream,
                work_dir=work_dir,
                timeout=timeout,
            )
    finally:
        try:
            gz_path.unlink(missing_ok=True)
        except OSError:
            pass
        if sql_path is not None:
            try:
                sql_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def verify_restored_database(
    *,
    connection: MysqlConnection,
    target: str,
    expected_alembic_version: str,
    expected_tables: dict[str, int],
) -> RestoreVerification:
    """核对恢复库的 Alembic 版本、表清单和各表行数，返回冒烟清单。"""
    validate_restore_target(target, allow_test=target.startswith(ALLOWED_TEST_PREFIX))
    url = URL.create(
        drivername="mysql+pymysql",
        username=connection.user or None,
        password=connection.password,
        host=connection.host,
        port=connection.port,
        database=target,
        query={"charset": "utf8mb4"},
    )
    engine = get_engine(url.render_as_string(hide_password=False), 10)
    try:
        metadata = gather_database_metadata(engine)
    except DatabaseBackupError as exc:
        raise RestoreError("RESTORE_VERIFY_FAILED", "读取恢复库元数据失败") from exc

    tables_match = metadata["tables"] == expected_tables
    alembic_match = metadata["alembic_version"] == expected_alembic_version
    if not alembic_match or not tables_match:
        raise RestoreError(
            "RESTORE_VERIFY_MISMATCH",
            "恢复库的 Alembic 版本或数据数量与备份声明不一致",
        )

    return RestoreVerification(
        target=target,
        alembic_version=metadata["alembic_version"],
        expected_alembic_version=expected_alembic_version,
        tables_match=True,
        table_count=len(metadata["tables"]),
        smoke_checklist=(
            "以测试账号登录恢复库对应的应用实例",
            "查看首页汇总、查账三种视图与四类统计是否与备份一致",
            "新增一笔流水并确认方向推导正确",
            "确认普通账号之间数据仍完全隔离",
            "全部通过后，由人工把应用数据库切换到该恢复库",
        ),
    )


def structural_schema_differences(differences):
    """字段注释不会改变可恢复结构；类型、默认值、约束等差异仍必须拒绝。"""
    result = []
    for difference in differences:
        if isinstance(difference, list):
            nested = structural_schema_differences(difference)
            if nested:
                result.append(nested)
        elif not (
            isinstance(difference, tuple) and difference and difference[0] == "modify_comment"
        ):
            result.append(difference)
    return result


def verify_restored_schema_head(
    *,
    connection: MysqlConnection,
    target: str,
) -> str:
    """只读核对恢复库位于当前 Alembic head，且 ORM 不存在待迁移结构差异。"""

    validate_restore_target(target, allow_test=target.startswith(ALLOWED_TEST_PREFIX))
    url = URL.create(
        drivername="mysql+pymysql",
        username=connection.user or None,
        password=connection.password,
        host=connection.host,
        port=connection.port,
        database=target,
        query={"charset": "utf8mb4"},
    )
    engine = get_engine(url.render_as_string(hide_password=False), 10)
    try:
        config = Config(str(_ALEMBIC_CONFIG))
        current_head = ScriptDirectory.from_config(config).get_current_head()
        if current_head is None:
            raise RestoreError("RESTORE_SCHEMA_CHECK_FAILED", "无法读取当前 Alembic head")

        with engine.connect() as database_connection:
            restored_revision = database_connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if restored_revision != current_head:
                raise RestoreError(
                    "RESTORE_SCHEMA_OUTDATED",
                    "恢复库不是当前数据库版本，禁止使用当前应用执行冒烟或切换",
                )
            context = MigrationContext.configure(
                database_connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            differences = structural_schema_differences(compare_metadata(context, Base.metadata))
            if differences:
                raise RestoreError(
                    "RESTORE_SCHEMA_DRIFT",
                    "恢复库结构与当前应用模型不一致，禁止继续",
                )
    except RestoreError:
        raise
    except Exception as exc:
        raise RestoreError("RESTORE_SCHEMA_CHECK_FAILED", "恢复库结构检查失败") from exc
    finally:
        engine.dispose()

    return str(current_head)


def drop_test_database(
    *,
    connection: MysqlConnection,
    target: str,
    mysql_path: str,
    work_dir: Path,
    timeout: int,
) -> None:
    """删除本次演练明确创建、且通过测试前缀保护的隔离测试库；绝不删除正式或恢复前缀库。"""
    validate_restore_target(target, allow_test=True)
    run_mysql_script(
        connection,
        mysql_path=mysql_path,
        statement=f"DROP DATABASE IF EXISTS `{target}`",
        work_dir=work_dir,
        timeout=timeout,
    )


__all__ = [
    "ALLOWED_RESTORE_PREFIX",
    "ALLOWED_TEST_PREFIX",
    "RestoreError",
    "RestoreVerification",
    "drop_test_database",
    "restore_prepare",
    "validate_restore_target",
    "verify_restored_database",
    "verify_restored_schema_head",
]
