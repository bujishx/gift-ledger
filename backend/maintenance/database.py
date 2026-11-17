"""Fail-closed database identity checks and lossless rehearsal comparisons.

Every operation is restricted to explicitly labelled, isolated rehearsal instances.
The production hostname ``db`` is deliberately unavailable. Credentials are read
from files and never accepted as command arguments or included in results.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from uuid import UUID

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

from app.db import models as _models  # noqa: F401
from app.db.base import Base
from app.services.database_backup import MysqlConnection
from app.services.database_restore import structural_schema_differences

SCOPE = "isolated-rehearsal"
DATABASE = "gift_ledger_v2"
META_DATABASE = "gift_ledger_maintenance_meta"
CURRENT_REVISION = "schema_v2_s011"
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_CONFIG_KEYS = frozenset({
    "format", "version", "scope", "run_id", "role", "host", "port", "user",
    "password_file", "expected_server_uuid", "database", "work_root",
})


class MaintenanceError(Exception):
    """Stable, credential-free error suitable for persisted operation results."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise MaintenanceError(code, message)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("CONFIG_INVALID", "配置含重复字段")
        result[key] = value
    return result


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except MaintenanceError:
        raise
    except (OSError, UnicodeError, ValueError):
        _fail("CONFIG_INVALID", "无法读取合法配置文件")


@dataclass(frozen=True, slots=True)
class MaintenanceConfig:
    scope: str
    run_id: str
    role: str
    host: str
    port: int
    user: str
    password_file: Path
    expected_server_uuid: str
    database: str
    work_root: Path

    @property
    def scratch_database(self) -> str:
        return f"gift_ledger_v2_restore_{self.run_id}"

    @property
    def connection(self) -> MysqlConnection:
        try:
            if self.password_file.is_symlink() or not self.password_file.is_file():
                _fail("CREDENTIAL_UNAVAILABLE", "维护凭据文件不可用")
            password = self.password_file.read_text(encoding="utf-8").strip()
            if not password or len(password.encode()) > 4096 or "\x00" in password:
                _fail("CREDENTIAL_UNAVAILABLE", "维护凭据文件不可用")
        except (OSError, UnicodeError):
            _fail("CREDENTIAL_UNAVAILABLE", "维护凭据文件不可用")
        return MysqlConnection(self.host, self.port, self.user, password, self.database)


def load_config(path: Path) -> MaintenanceConfig:
    data = read_json(Path(path))
    if not isinstance(data, dict) or set(data) != _CONFIG_KEYS:
        _fail("CONFIG_INVALID", "维护配置字段集合不正确")
    if data["format"] != "gift-ledger-maintenance-config" or type(data["version"]) is not int:
        _fail("CONFIG_INVALID", "维护配置格式不正确")
    if data["version"] != 1:
        _fail("CONFIG_INVALID", "维护配置版本不受支持")
    if data["scope"] != SCOPE or os.environ.get("MAINTENANCE_SCOPE") != SCOPE:
        _fail("SCOPE_FORBIDDEN", "本工具仅允许独立合成恢复演练")
    if not isinstance(data["run_id"], str) or not _RUN_ID.fullmatch(data["run_id"]):
        _fail("CONFIG_INVALID", "演练编号必须为16位小写十六进制")
    expected_host = {"source": "source-db", "restore": "restore-db"}.get(data["role"])
    if expected_host is None or data["host"] != expected_host:
        _fail("HOST_FORBIDDEN", "数据库主机与演练角色不匹配")
    if type(data["port"]) is not int or data["port"] != 3306:
        _fail("CONFIG_INVALID", "演练只允许内部MySQL端口")
    if data["database"] != DATABASE or data["work_root"] != "/work":
        _fail("CONFIG_INVALID", "演练数据库或工作目录不受允许")
    if not isinstance(data["user"], str) or not _IDENTIFIER.fullmatch(data["user"]):
        _fail("CONFIG_INVALID", "维护账号名称不正确")
    try:
        uuid_value = str(UUID(data["expected_server_uuid"]))
        if uuid_value != data["expected_server_uuid"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail("CONFIG_INVALID", "数据库预期身份不是规范UUID")
    try:
        portable_path = PurePosixPath(data["password_file"])
    except TypeError:
        _fail("CONFIG_INVALID", "维护密码必须来自容器保密文件")
    if not portable_path.is_absolute() or portable_path.parent != PurePosixPath("/run/secrets"):
        _fail("CONFIG_INVALID", "维护密码必须来自容器保密文件")
    password_file = Path(data["password_file"])
    return MaintenanceConfig(
        scope=data["scope"], run_id=data["run_id"], role=data["role"], host=data["host"],
        port=data["port"], user=data["user"], password_file=password_file,
        expected_server_uuid=uuid_value, database=DATABASE, work_root=Path("/work"),
    )


def validate_database(config: MaintenanceConfig, database: str | None) -> None:
    if database is not None and database not in {DATABASE, config.scratch_database}:
        _fail("DATABASE_FORBIDDEN", "目标数据库不属于本次演练")
    if database == config.scratch_database and config.role != "restore":
        _fail("ROLE_FORBIDDEN", "源实例不能作为恢复目标")


def require_guard(engine, config: MaintenanceConfig) -> dict:
    """Read only technical identity metadata before any business data operation."""
    if os.environ.get("MAINTENANCE_SCOPE") != SCOPE or config.scope != SCOPE:
        _fail("SCOPE_FORBIDDEN", "本工具仅允许独立合成恢复演练")
    if config.host != {"source": "source-db", "restore": "restore-db"}.get(config.role):
        _fail("HOST_FORBIDDEN", "数据库主机与演练角色不匹配")
    try:
        with engine.connect() as connection:
            actual_uuid = str(connection.execute(text("SELECT @@server_uuid")).scalar_one())
            markers = connection.execute(text(
                "SELECT singleton_id, run_id, role, server_uuid, scope FROM "
                "gift_ledger_maintenance_meta.instance_identity"
            )).mappings().all()
        if len(markers) != 1:
            _fail("IDENTITY_MISMATCH", "演练实例身份标记不唯一")
        marker = markers[0]
        if (marker["singleton_id"] != 1 or marker["run_id"] != config.run_id
                or marker["role"] != config.role or marker["scope"] != SCOPE
                or marker["server_uuid"] != config.expected_server_uuid
                or actual_uuid != config.expected_server_uuid):
            _fail("IDENTITY_MISMATCH", "演练实例身份与受信配置不匹配")
    except MaintenanceError:
        raise
    except Exception:
        _fail("IDENTITY_UNAVAILABLE", "无法核对独立数据库实例身份")
    return {"run_id": config.run_id, "role": config.role, "scope": SCOPE,
            "server_uuid": config.expected_server_uuid}


@contextmanager
def guarded_engine(config: MaintenanceConfig, database: str | None = None):
    validate_database(config, database)
    connection = config.connection
    url = URL.create("mysql+pymysql", username=connection.user, password=connection.password,
                     host=connection.host, port=connection.port, database=database,
                     query={"charset": "utf8mb4"})
    engine = create_engine(url, connect_args={"connect_timeout": 10}, pool_pre_ping=True)
    try:
        require_guard(engine, config)
        yield engine
    finally:
        engine.dispose()


def canonical_value(value):
    """Type tags avoid collisions; never persist source values, only their digest."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat(timespec="microseconds")]
    if isinstance(value, timedelta):
        return ["timedelta", str(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", bytes(value).hex()]
    if isinstance(value, float) and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    _fail("FINGERPRINT_TYPE_UNSUPPORTED", "数据库包含无法规范化的数据类型")


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        _fail("SCHEMA_UNSUPPORTED", "数据库结构包含不受支持的标识符")
    return f"`{value}`"


def database_fingerprint(engine, config: MaintenanceConfig) -> dict:
    require_guard(engine, config)
    try:
        with engine.connect() as connection:
            connection.execute(text("SET time_zone = '+00:00'"))
            connection.commit()
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("START TRANSACTION WITH CONSISTENT SNAPSHOT"))
            inspector = inspect(connection)
            tables = sorted(inspector.get_table_names())
            expected = set(Base.metadata.tables) | {"alembic_version"}
            if set(tables) != expected:
                _fail("SCHEMA_UNSUPPORTED", "业务表清单与当前应用不一致")
            revisions = connection.execute(text("SELECT version_num FROM alembic_version")).all()
            if revisions != [(CURRENT_REVISION,)]:
                _fail("VERSION_INCOMPATIBLE", "仅允许当前S011演练数据库")
            output = {}
            for name in tables:
                columns = [column["name"] for column in inspector.get_columns(name)]
                primary = inspector.get_pk_constraint(name)["constrained_columns"]
                if not primary:
                    _fail("SCHEMA_UNSUPPORTED", "全列校验要求每张表具有主键")
                digest = hashlib.sha256()
                digest.update(_json_bytes(columns) + b"\n")
                count = 0
                query = f"SELECT {','.join(map(_identifier, columns))} FROM {_identifier(name)}"
                query += f" ORDER BY {','.join(map(_identifier, primary))}"
                rows = connection.execution_options(stream_results=True).execute(text(query))
                for row in rows:
                    digest.update(_json_bytes([canonical_value(value) for value in row]) + b"\n")
                    count += 1
                rows.close()
                output[name] = {"rows": count, "sha256": digest.hexdigest()}
            connection.rollback()
        return {"algorithm": "sha256-canonical-v1", "schema_revision": CURRENT_REVISION,
                "tables": output}
    except MaintenanceError:
        raise
    except Exception:
        _fail("FINGERPRINT_FAILED", "数据库全列校验失败")


def verify_schema(engine, config: MaintenanceConfig) -> str:
    require_guard(engine, config)
    try:
        backend_dir = Path(__file__).resolve().parents[1]
        alembic = Config(str(backend_dir / "alembic.ini"))
        head = ScriptDirectory.from_config(alembic).get_current_head()
        if head != CURRENT_REVISION:
            _fail("VERSION_INCOMPATIBLE", "维护程序迁移头与S011不一致")
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if revision != head:
                _fail("VERSION_INCOMPATIBLE", "数据库迁移版本与程序不一致")
            context = MigrationContext.configure(connection, opts={
                "compare_type": True, "compare_server_default": True,
            })
            differences = structural_schema_differences(compare_metadata(context, Base.metadata))
            if differences:
                _fail("SCHEMA_DRIFT", "数据库结构与当前应用模型不一致")
    except MaintenanceError:
        raise
    except Exception:
        _fail("SCHEMA_CHECK_FAILED", "无法完成数据库结构校验")
    return str(head)


def verify_relationships(engine, config: MaintenanceConfig) -> dict:
    """Check every physical foreign key plus user-scoped dictionary ownership."""
    require_guard(engine, config)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            constraints = 0
            for table in sorted(inspector.get_table_names()):
                for foreign_key in inspector.get_foreign_keys(table):
                    referenced_schema = foreign_key.get("referred_schema")
                    if referenced_schema not in {None, connection.engine.url.database}:
                        _fail("REFERENCE_INVALID", "恢复库含跨库业务引用")
                    child = foreign_key["constrained_columns"]
                    parent = foreign_key["referred_columns"]
                    conditions = " AND ".join(
                        f"c.{_identifier(a)} = p.{_identifier(b)}"
                        for a, b in zip(child, parent, strict=True)
                    )
                    present = " AND ".join(f"c.{_identifier(a)} IS NOT NULL" for a in child)
                    query = (
                        f"SELECT COUNT(*) FROM {_identifier(table)} c LEFT JOIN "
                        f"{_identifier(foreign_key['referred_table'])} p ON {conditions} "
                        f"WHERE {present} AND p.{_identifier(parent[0])} IS NULL"
                    )
                    if connection.execute(text(query)).scalar_one() != 0:
                        _fail("REFERENCE_INVALID", "恢复库存在孤儿业务引用")
                    constraints += 1
            for table, column, dictionary in (
                ("contacts", "relation_type_id", "relationship_types"),
                ("event_records", "relative_type_id", "kinship_types"),
                ("event_records", "event_type_id", "event_types"),
            ):
                query = (f"SELECT COUNT(*) FROM `{table}` c JOIN `{dictionary}` d "
                         f"ON c.`{column}` = d.id WHERE d.user_id IS NOT NULL "
                         "AND d.user_id <> c.user_id")
                if connection.execute(text(query)).scalar_one() != 0:
                    _fail("REFERENCE_INVALID", "恢复库存在跨用户字典引用")
            invalid_direction = connection.execute(text(
                "SELECT COUNT(*) FROM gift_records g JOIN event_records e "
                "ON e.user_id=g.user_id AND e.id=g.event_record_id "
                "JOIN contacts host ON host.user_id=e.user_id AND host.id=e.contact_id "
                "JOIN contacts giver ON giver.user_id=g.user_id AND giver.id=g.contact_id "
                "WHERE giver.id=host.id OR (host.relation_type_id<>1 "
                "AND giver.relation_type_id<>1)"
            )).scalar_one()
            if invalid_direction:
                _fail("REFERENCE_INVALID", "恢复库存在不符合自动收支规则的记账")
        return {"foreign_keys_checked": constraints, "orphan_references": 0,
                "cross_user_dictionary_references": 0, "invalid_gift_direction": 0}
    except MaintenanceError:
        raise
    except Exception:
        _fail("REFERENCE_CHECK_FAILED", "无法完成业务关联校验")


def verify_safety_owners(engine, config: MaintenanceConfig, metadata_paths: list[Path]) -> int:
    """A preserved safety file must still have an owner in the restored database."""
    require_guard(engine, config)
    try:
        with engine.connect() as connection:
            for path in metadata_paths:
                payload = read_json(path)
                if type(payload.get("user_id")) is not int or payload["user_id"] <= 0:
                    _fail("SAFETY_OWNER_INVALID", "个人安全副本所属账号不正确")
                count = connection.execute(text("SELECT COUNT(*) FROM users WHERE id=:user_id"),
                                           {"user_id": payload["user_id"]}).scalar_one()
                if count != 1:
                    _fail("SAFETY_OWNER_INVALID", "个人安全副本所属账号不在恢复数据库中")
    except MaintenanceError:
        raise
    except Exception:
        _fail("SAFETY_OWNER_CHECK_FAILED", "无法核对个人安全副本所属账号")
    return len(metadata_paths)
