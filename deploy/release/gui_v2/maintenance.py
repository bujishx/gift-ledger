"""GUI-started manual maintenance for NEW private-volume installations only.

No Docker socket, TCP database connection, scheduler, deletion of existing
instances, or hidden production switching is supported. Public exchange folders
receive ciphertext and non-secret receipts only. Plaintext lives in tmpfs.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_STORED, ZipFile, ZipInfo

UTC = timezone.utc

ROOT = Path("/work/site")
SITE = ROOT / "site"
LAYOUT = ROOT / "layout"
CONTROL = ROOT / "maintenance"
REQUEST = Path("/work/request/request.json")
EXCHANGE = Path("/work/exchange")
IDENTITY = Path("/work/identity/identity.txt")
APP_DATA = Path("/work/app-data")
SOCKET = Path("/work/mysql/mysqld.sock")
ASSETS = Path("/opt/gift-ledger-release")
BACKEND = Path("/app/backend")
LEGACY = Path("/opt/gift-ledger-gui/initialize.py")
DATABASE = "gift_ledger_v2"
APP_USER = "gift_ledger_app"
SUPPORTED = {"schema_v2_s010", "schema_v2_s011"}
TABLES = {
    "alembic_version",
    "users",
    "contacts",
    "event_records",
    "gift_records",
    "legacy_import_batches",
    "relationship_types",
    "kinship_types",
    "event_types",
}
BUSINESS = {
    "users",
    "contacts",
    "event_records",
    "gift_records",
    "legacy_import_batches",
}
DIRECTORIES = {
    "imports",
    "personal-restores",
    "personal-safety-backups",
    "admin-backups",
}
ACTION_OPTIONS = {
    "backup": {"recipient", "backup_type"},
    "verify": {"ciphertext", "sha256"},
    "restore": {
        "ciphertext",
        "sha256",
        "source_project",
        "source_instance_id",
        "source_server_uuid",
        "source_operation_id",
    },
    "upgrade": {
        "from_revision",
        "to_revision",
        "backup_operation_id",
        "verify_operation_id",
    },
}
REQUEST_KEYS = {
    "format",
    "version",
    "operation_id",
    "action",
    "project",
    "instance_id",
    "server_uuid",
    "schema_revision",
    "options",
}
FORMAT = "gift-ledger-gui-v2-operation"
SET_FORMAT = "gift-ledger-gui-v2-recovery-set"
MAX_BYTES = 2 * 1024**3
MAX_FILES = 4096
MAX_JSON = 1024**2
MAX_HISTORY_ENTRIES = 20000
_AUTOMATION_PREVIEW_CONTEXT = None
_RESTORE_INPUT = None
HEX = re.compile(r"^[0-9a-f]{64}$")
OPERATION = re.compile(r"^[0-9a-f]{32}$")
RECIPIENT = re.compile(r"^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CONFIG_FILES = {
    "configuration/secrets/mysql-root-password.txt": "secrets/mysql-root-password.txt",
    "configuration/secrets/mysql-app-password.txt": "secrets/mysql-app-password.txt",
    "configuration/secrets/jwt-secret.txt": "secrets/jwt-secret.txt",
    "configuration/certs/fullchain.pem": "certs/fullchain.pem",
    "configuration/certs/ca-chain.pem": "certs/ca-chain.pem",
    "configuration/certs/privkey.pem": "certs/privkey.pem",
    "configuration/control/identity.json": "control/identity.json",
    "configuration/control/prepare-started.json": "control/prepare-started.json",
    "configuration/control/prepare-completed.json": "control/prepare-completed.json",
    "configuration/control/initialize-started.json": "control/initialize-started.json",
    "configuration/control/initialize-completed.json": "control/initialize-completed.json",
}


class GuiMaintenanceError(RuntimeError):
    """A stable public reason; never include raw SQL, credentials or user data."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise GuiMaintenanceError(code)


def utc_now():
    return datetime.now(UTC).isoformat()


def json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def dsm9_profile(value):
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        return None
    if isinstance(value, str) and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", value):
        path = Path(__file__).absolute().parent / "portable_dsm_profile.py"
        spec = importlib.util.spec_from_file_location("maintenance_portable_profile", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.load(value)
    if not isinstance(value, str) or not value.startswith("gift-ledger-dsm9-"):
        return None
    path = Path(__file__).resolve().parent / "dsm9_profile.py"
    spec = importlib.util.spec_from_file_location("maintenance_dsm9_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module if value in module.PROJECTS else None


def allowed_project(value):
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        from single_yaml_site import project_host
        try:
            project_host(value)
        except (KeyError, TypeError, ValueError):
            raise GuiMaintenanceError("single_yaml_project_mismatch") from None
        return
    if (Path(__file__).absolute().parent / "PORTABLE-DSM.json").exists():
        require(dsm9_profile(value) is not None and isinstance(value,str)
                and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}",value), "portable_project_not_in_contract")
        return
    bridge = generated_contract_bridge()
    if bridge is not None:
        try:
            bridge.authorize_target(value)
        except (OSError, ValueError):
            require(False, "generated_target_contract_invalid")
        return
    if isinstance(value, str) and re.fullmatch(r"gift-ledger-site-[0-9a-f]{16}", value):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "operations_v3"))
        sys.path.insert(0, "/opt/gift-ledger-ops")
        from contract_bridge import authorize_target

        try:
            authorize_target(value, allow_source=True)
        except (OSError, ValueError):
            require(False, "generated_target_contract_invalid")
        return
    require(
        value in ("gift-ledger-nas-prod", "gift-ledger-nas-restore-check")
        or dsm9_profile(value) is not None
        or (
            isinstance(value, str)
            and re.fullmatch(r"gift-ledger-gui-test-[0-9a-f]{16}", value)
        ),
        "project_forbidden",
    )


def generated_contract_bridge():
    if not any(name in os.environ for name in
               ("GIFT_LEDGER_TARGET_SHA256", "GIFT_LEDGER_TARGET_FILE")):
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "operations_v3"))
    sys.path.insert(0, "/opt/gift-ledger-ops")
    import contract_bridge
    return contract_bridge


def backup_reference_profile():
    """Read-only imported-source authorization never grants operational access."""
    directory = Path(__file__).absolute().parent
    if not (directory / "PORTABLE-DSM.json").exists():
        return None
    spec = importlib.util.spec_from_file_location("maintenance_backup_reference", directory / "portable_dsm_profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read(directory)


def allowed_backup_reference(value):
    if _RESTORE_INPUT is not None:
        require(value == _RESTORE_INPUT.backup["project"], "selected_restore_source_mismatch")
        return
    profile = backup_reference_profile()
    if profile is not None and profile.BACKUP_SOURCE and value == profile.SOURCE_PROJECT:
        return
    bridge = generated_contract_bridge() if profile is None else None
    if bridge is not None:
        try:
            bridge.authorize_backup_reference(value)
        except (OSError, ValueError):
            require(False, "generated_backup_reference_invalid")
        return
    allowed_project(value)


def pinned_restore_source(options):
    if _RESTORE_INPUT is not None:
        require(options == _RESTORE_INPUT.options(), "selected_restore_source_mismatch")
        return
    profile = backup_reference_profile()
    if profile is None:
        bridge = generated_contract_bridge()
        if bridge is None:
            return
        try:
            spec = bridge.authorize_backup_reference(options.get("source_project"))
        except (OSError, ValueError):
            require(False, "generated_backup_reference_invalid")
        source = spec.get("source")
        require(isinstance(source, dict) and spec["purpose"] in ("drill", "migration", "disaster", "independent"),
                "generated_restore_source_binding_mismatch")
        expected = {"source_project": source["project"], "source_instance_id": source["instance_id"],
                    "source_server_uuid": source["server_uuid"], "source_operation_id": source["backup_operation_id"]}
        require(all(options.get(key) == val for key, val in expected.items()),
                "generated_restore_source_binding_mismatch")
        return
    if not profile.BACKUP_SOURCE:
        return
    source = profile.BACKUP_SOURCE
    expected = {"source_project": source["project"], "source_instance_id": source["instance_id"],
                "source_server_uuid": source["server_uuid"], "source_operation_id": source["backup_operation_id"],
                "ciphertext": source["ciphertext"], "sha256": source["ciphertext_sha256"]}
    require(all(options.get(key) == val for key, val in expected.items())
            and options.get("source_schema_revision", "schema_v2_s011") == source["schema_revision"],
            "imported_backup_binding_mismatch")


@contextmanager
def single_yaml_restore_input(selected):
    """Bind only read-only source references; target operations retain their own project checks."""
    global _RESTORE_INPUT
    from restore_input import RestoreInput
    require(os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml" and _RESTORE_INPUT is None
            and type(selected) is RestoreInput, "single_yaml_restore_scope_invalid")
    _RESTORE_INPUT = selected
    try:
        yield
    finally:
        _RESTORE_INPUT = None


def no_links(path):
    for item in (path.absolute(), *path.absolute().parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        require(
            not stat.S_ISLNK(metadata.st_mode)
            and not getattr(metadata, "st_file_attributes", 0) & 0x400,
            "linked_path_forbidden",
        )


def no_acl(path):
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except (OSError, AttributeError, NotImplementedError):
        raise GuiMaintenanceError("acl_inspection_unavailable") from None
    require(not any("acl" in name.lower() for name in names), "extended_acl_forbidden")


def directory(path, *, private=False, owner=None):
    no_links(path)
    metadata = path.lstat()
    require(stat.S_ISDIR(metadata.st_mode), "directory_required")
    if private or owner is not None:
        no_acl(path)
    if private:
        require(
            stat.S_IMODE(metadata.st_mode) == 0o700
            and (metadata.st_uid, metadata.st_gid) == (0, 0),
            "private_directory_invalid",
        )
    if owner is not None:
        require((metadata.st_uid, metadata.st_gid) == owner, "directory_owner_invalid")


def read_file(path, *, mode=None, private=False, maximum=MAX_JSON):
    no_links(path)
    metadata = path.lstat()
    require(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_size <= maximum,
        "regular_file_required",
    )
    if private:
        no_acl(path)
        require(
            (metadata.st_uid, metadata.st_gid) == (0, 0), "private_file_owner_invalid"
        )
    if mode is not None:
        require(stat.S_IMODE(metadata.st_mode) == mode, "private_file_mode_invalid")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(
        len(raw) <= maximum and len(raw) == metadata.st_size,
        "file_changed_or_too_large",
    )
    return raw


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_field")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: require(False, "invalid_json_number"),
        )
    except (ValueError, UnicodeError, TypeError):
        raise GuiMaintenanceError("invalid_json") from None


def json_file(path, *, private=False):
    return parse_json(read_file(path, mode=0o600 if private else None, private=private))


def file_sha(path):
    no_links(path)
    metadata = path.lstat()
    require(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_size <= MAX_BYTES,
        "regular_file_required",
    )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            digest.update(block)
    return digest.hexdigest()


def exclusive_bytes(path, raw, *, mode=0o600):
    no_links(path)
    with path.open("xb") as stream:
        os.chmod(path, mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return sha(raw)


def exclusive_json(path, value):
    return exclusive_bytes(path, json_bytes(value) + b"\n")


def valid_request(value, project, action):
    allowed_project(project)
    dsm9 = dsm9_profile(project)
    require(
        dsm9 is None or project != dsm9.SOURCE_PROJECT or action != "restore",
        "dsm9_source_restore_forbidden",
    )
    require(
        not (project == "gift-ledger-nas-restore-check"
             or (dsm9 is not None and dsm9.is_restore_project(project))) or action != "upgrade",
        "restore_check_upgrade_forbidden",
    )
    require(
        isinstance(value, dict)
        and set(value) == REQUEST_KEYS
        and value["format"] == "gift-ledger-gui-v2-request"
        and type(value["version"]) is int
        and value["version"] == 1,
        "request_format_invalid",
    )
    require(
        value["project"] == project
        and value["action"] == action
        and action in ACTION_OPTIONS,
        "request_action_or_project_mismatch",
    )
    require(
        isinstance(value["operation_id"], str)
        and OPERATION.fullmatch(value["operation_id"]),
        "operation_id_invalid",
    )
    require(
        canonical_uuid(value["instance_id"]) and canonical_uuid(value["server_uuid"]),
        "request_instance_identity_invalid",
    )
    require(value["schema_revision"] in SUPPORTED, "request_schema_incompatible")
    options = value["options"]
    require(
        isinstance(options, dict) and (set(options) == ACTION_OPTIONS[action]
            or action == "restore" and set(options) == ACTION_OPTIONS[action] | {"source_schema_revision"}
            and options["source_schema_revision"] == "schema_v2_s010"),
        "request_options_invalid",
    )
    if action == "backup":
        require(
            isinstance(options["recipient"], str)
            and RECIPIENT.fullmatch(options["recipient"])
            and options["backup_type"] in {"manual", "pre-maintenance"},
            "backup_recipient_or_type_invalid",
        )
    elif action in {"verify", "restore"}:
        require(
            isinstance(options["ciphertext"], str)
            and re.fullmatch(r"backup-[0-9a-f]{32}\.glgui\.age", options["ciphertext"])
            and isinstance(options["sha256"], str)
            and HEX.fullmatch(options["sha256"]),
            "ciphertext_identity_invalid",
        )
        if action == "restore":
            require(
                dsm9 is None or options["source_project"] == dsm9.SOURCE_PROJECT,
                "dsm9_restore_source_forbidden",
            )
            allowed_backup_reference(options["source_project"])
            pinned_restore_source(options)
            require(
                canonical_uuid(options["source_instance_id"])
                and canonical_uuid(options["source_server_uuid"])
                and isinstance(options["source_operation_id"], str)
                and OPERATION.fullmatch(options["source_operation_id"]),
                "restore_source_identity_invalid",
            )
            require(
                value["schema_revision"] == "schema_v2_s011"
                and value["instance_id"] != options["source_instance_id"]
                and value["server_uuid"] != options["source_server_uuid"],
                "restore_source_target_identical_or_incompatible",
            )
    elif action == "upgrade":
        require(
            value["schema_revision"] == "schema_v2_s010"
            and options["from_revision"] == "schema_v2_s010"
            and options["to_revision"] == "schema_v2_s011"
            and all(
                isinstance(options[key], str) and OPERATION.fullmatch(options[key])
                for key in ("backup_operation_id", "verify_operation_id")
            ),
            "upgrade_not_approved_s010_s011",
        )
    return value


def load_module(name, path):
    require(path.is_file() and not path.is_symlink(), "tooling_missing")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local_context(project):
    allowed_project(project)
    require(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        "fixed_linux_root_utility_required",
    )
    require(sys.version_info[:2] == (3, 12), "fixed_python_312_required")
    directory(ROOT, private=True)
    directory(CONTROL, private=True)
    layout_tool = load_module(
        "gui_v2_layout_verifier", Path(__file__).parent / "prepare_volumes.py"
    )
    layout = layout_tool.verify_layout(project)
    legacy = load_module("gui_legacy_initialization_verifier", LEGACY)
    identity, identity_sha, passwords = legacy.validate_prepare(
        SITE, project, ROOT / "bindings"
    )
    legacy.validate_assets(ASSETS, BACKEND)
    completed = legacy.existing_state(
        SITE / "control", project, identity["instance_id"], identity_sha
    )
    require(completed is not None, "completed_initialization_required")
    require(
        layout["instance_id"] == identity["instance_id"]
        and layout["canonical_identity_sha256"] == identity_sha,
        "layout_canonical_identity_mismatch",
    )
    socket_metadata = SOCKET.lstat()
    require(
        stat.S_ISSOCK(socket_metadata.st_mode) and socket_metadata.st_nlink == 1,
        "private_mysql_socket_invalid",
    )
    no_links(SOCKET)
    sys.path.insert(0, str(BACKEND))
    return {
        "project": project,
        "identity": identity,
        "identity_sha256": identity_sha,
        "layout": layout,
        "legacy": legacy,
        "passwords": passwords,
        "server_uuid": completed["server_uuid"],
    }


def engine_for(context, database=DATABASE):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from sqlalchemy.pool import NullPool

    return create_engine(
        URL.create(
            "mysql+pymysql",
            username="root",
            password=context["passwords"]["mysql_root_password"],
            database=database,
            query={"unix_socket": str(SOCKET), "charset": "utf8mb4"},
        ),
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
        echo=False,
        hide_parameters=True,
    )


def database_guard(connection, context, *, revision=None, importer=None):
    server_uuid, version, username, readonly, super_readonly = (
        connection.exec_driver_sql(
            "SELECT @@server_uuid, VERSION(), CURRENT_USER(), @@global.read_only, @@global.super_read_only"
        ).one()
    )
    require(
        server_uuid == context["server_uuid"]
        and version.startswith("8.4.")
        and username == "root@localhost",
        "database_identity_or_transport_mismatch",
    )
    schemas = set(connection.exec_driver_sql("SHOW DATABASES").scalars())
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        schemas.discard("gift_ledger_registration")
    require(
        schemas
        == {"information_schema", "mysql", "performance_schema", "sys", DATABASE},
        "database_namespace_changed",
    )
    accounts = {
        tuple(row)
        for row in connection.exec_driver_sql("SELECT User,Host FROM mysql.user")
    }
    expected = {
        ("root", "localhost"),
        ("mysql.infoschema", "localhost"),
        ("mysql.session", "localhost"),
        ("mysql.sys", "localhost"),
        (APP_USER, "%"),
    }
    if importer is not None:
        expected.add((importer, "localhost"))
    require(accounts == expected, "database_account_namespace_changed")
    actual = connection.exec_driver_sql(
        "SELECT version_num FROM alembic_version"
    ).scalar_one()
    require(
        actual in SUPPORTED and (revision is None or actual == revision),
        "database_revision_mismatch",
    )
    return {"read_only": bool(readonly), "super_read_only": bool(super_readonly)}


def identifier(value):
    require(
        isinstance(value, str) and IDENTIFIER.fullmatch(value), "sql_identifier_invalid"
    )
    return "`" + value + "`"


def fingerprint(engine, context):
    from maintenance.database import canonical_value
    from sqlalchemy import inspect, text

    with engine.connect() as connection:
        database_guard(connection, context)
        connection.exec_driver_sql("SET time_zone='+00:00'")
        connection.commit()
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        inspector = inspect(connection)
        tables = sorted(inspector.get_table_names())
        require(set(tables) == TABLES, "database_table_namespace_changed")
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        output = {}
        for table in tables:
            columns = [column["name"] for column in inspector.get_columns(table)]
            primary = inspector.get_pk_constraint(table)["constrained_columns"]
            require(bool(primary), "database_primary_key_missing")
            digest = hashlib.sha256(json_bytes(columns) + b"\n")
            query = (
                f"SELECT {','.join(map(identifier, columns))} FROM {identifier(table)} "
            )
            query += f"ORDER BY {','.join(map(identifier, primary))}"
            result = connection.execution_options(stream_results=True).execute(
                text(query)
            )
            count = 0
            for row in result:
                digest.update(
                    json_bytes([canonical_value(value) for value in row]) + b"\n"
                )
                count += 1
            result.close()
            output[table] = {"rows": count, "sha256": digest.hexdigest()}
        connection.rollback()
    return {
        "algorithm": "sha256-canonical-v1",
        "schema_revision": revision,
        "tables": output,
    }


def verify_schema(engine, context, *, revision=None, empty=False):
    from sqlalchemy import inspect, text

    legacy = context["legacy"]
    with engine.connect() as connection:
        database_guard(connection, context, revision=revision)
        actual = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    verifier = legacy.load_verifier(ASSETS)
    legacy.verify_database(
        engine, verifier, ASSETS, actual, context["server_uuid"], empty=empty
    )
    with engine.connect() as connection:
        for catalog, field in (
            ("TRIGGERS", "TRIGGER_SCHEMA"),
            ("ROUTINES", "ROUTINE_SCHEMA"),
            ("EVENTS", "EVENT_SCHEMA"),
        ):
            require(
                connection.exec_driver_sql(
                    f"SELECT COUNT(*) FROM information_schema.{catalog} "
                    f"WHERE {field}=DATABASE()"
                ).scalar_one()
                == 0,
                "extra_database_object_forbidden",
            )
        inspector = inspect(connection)
        checked = 0
        for table in inspector.get_table_names():
            for relation in inspector.get_foreign_keys(table):
                require(
                    relation.get("referred_schema") in {None, DATABASE},
                    "cross_schema_reference",
                )
                child, parent = (
                    relation["constrained_columns"],
                    relation["referred_columns"],
                )
                condition = " AND ".join(
                    f"c.{identifier(a)}=p.{identifier(b)}"
                    for a, b in zip(child, parent, strict=True)
                )
                present = " AND ".join(f"c.{identifier(a)} IS NOT NULL" for a in child)
                query = (
                    f"SELECT COUNT(*) FROM {identifier(table)} c LEFT JOIN "
                    f"{identifier(relation['referred_table'])} p ON {condition} "
                    f"WHERE {present} AND p.{identifier(parent[0])} IS NULL"
                )
                require(
                    connection.execute(text(query)).scalar_one() == 0,
                    "orphan_reference",
                )
                checked += 1
        for table, field, dictionary in (
            ("contacts", "relation_type_id", "relationship_types"),
            ("event_records", "relative_type_id", "kinship_types"),
            ("event_records", "event_type_id", "event_types"),
        ):
            require(
                connection.exec_driver_sql(
                    f"SELECT COUNT(*) FROM `{table}` c "
                    f"JOIN `{dictionary}` d ON c.`{field}`=d.id WHERE d.user_id IS NOT NULL "
                    "AND d.user_id<>c.user_id"
                ).scalar_one()
                == 0,
                "cross_user_dictionary_reference",
            )
        require(
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM gift_records g "
                "JOIN event_records e ON e.user_id=g.user_id AND e.id=g.event_record_id "
                "JOIN contacts h ON h.user_id=e.user_id AND h.id=e.contact_id "
                "JOIN contacts c ON c.user_id=g.user_id AND c.id=g.contact_id "
                "WHERE c.id=h.id OR (h.relation_type_id<>1 AND c.relation_type_id<>1)"
            ).scalar_one()
            == 0,
            "invalid_gift_direction",
        )
    return {
        "foreign_keys_checked": checked,
        "orphan_references": 0,
        "cross_user_dictionary_references": 0,
        "invalid_gift_direction": 0,
    }


def safe_member(name):
    require(
        isinstance(name, str)
        and 0 < len(name) <= 240
        and "\\" not in name
        and ":" not in name,
        "archive_path_invalid",
    )
    require(
        all(
            part not in {"", ".", ".."}
            and not part.endswith((".", " "))
            and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            and part.split(".")[0].upper()
            not in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for part in name.split("/")
        ),
        "archive_path_invalid",
    )
    asset_names = {
        "assets/" + filename
        for filename in (
            "schema_v2_s004.sql",
            "schema_v2_s010.sql",
            "schema_v2_s011.sql",
            "verify_schema.py",
            "SHA256SUMS",
        )
    }
    app_file = name.startswith("app-data/") and len(name.split("/")) >= 3
    if app_file:
        require(
            name.split("/")[1] in {"personal-safety-backups", "admin-backups"},
            "archive_transient_or_unknown_application_member",
        )
    require(
        name == "database.gladminbackup"
        or name in CONFIG_FILES
        or name == "configuration/layout.json"
        or name in asset_names
        or app_file,
        "archive_member_forbidden",
    )


def application_files(path=APP_DATA, *, empty=False):
    directory(path, owner=(10001, 0))
    require(stat.S_IMODE(path.stat().st_mode) == 0o700, "application_root_mode_invalid")
    guard = _current_preview_guard(path)
    require(
        {item.name for item in path.iterdir()} <= DIRECTORIES,
        "application_root_namespace_invalid",
    )
    output = {}
    for item in path.rglob("*"):
        no_links(item)
        no_acl(item)
        metadata = item.lstat()
        require(
            (metadata.st_uid, metadata.st_gid) == (10001, 0),
            "application_file_owner_invalid",
        )
        if stat.S_ISDIR(metadata.st_mode):
            require(
                stat.S_IMODE(metadata.st_mode) == 0o700,
                "application_directory_mode_invalid",
            )
            continue
        require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600,
            "application_file_invalid",
        )
        relative = item.relative_to(path).as_posix()
        if guard is not None and relative.split("/")[0] in {
            "imports",
            "personal-restores",
        }:
            # Still enforce the original owner/mode/link/ACL checks above. Only
            # exact, stable application preview caches may stay out of archives.
            continue
        require(
            relative.split("/")[0] not in {"imports", "personal-restores"},
            "incomplete_application_work_present",
        )
        name = "app-data/" + relative
        safe_member(name)
        output[name] = {"size": metadata.st_size, "sha256": file_sha(item)}
    require(not empty or not output, "restore_application_target_nonempty")
    require(
        len(output) <= MAX_FILES
        and sum(item["size"] for item in output.values()) <= MAX_BYTES,
        "application_files_too_large",
    )
    if guard is not None:
        require(guard.verify() is True, "automation_preview_snapshot_changed")
    return output


def no_application_sessions(connection):
    require(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM information_schema.processlist "
            "WHERE USER='gift_ledger_app'"
        ).scalar_one()
        == 0,
        "application_sessions_present_stop_gui",
    )


def read_flags(connection):
    readonly, super_readonly = connection.exec_driver_sql(
        "SELECT @@global.read_only,@@global.super_read_only"
    ).one()
    require(
        (int(readonly), int(super_readonly)) in {(0, 0), (1, 0), (1, 1)},
        "readonly_flags_invalid",
    )
    return {"read_only": bool(readonly), "super_read_only": bool(super_readonly)}


def set_flags(connection, flags):
    require(
        set(flags) == {"read_only", "super_read_only"}
        and all(type(value) is bool for value in flags.values())
        and (flags["read_only"] or not flags["super_read_only"]),
        "readonly_flags_invalid",
    )
    # MySQL forbids changing read_only with a pending transaction in this
    # session. GET_LOCK is a session lock and survives this commit.
    connection.commit()
    connection.exec_driver_sql("SET GLOBAL super_read_only=OFF")
    connection.exec_driver_sql(
        "SET GLOBAL read_only=" + ("ON" if flags["read_only"] else "OFF")
    )
    if flags["super_read_only"]:
        connection.exec_driver_sql("SET GLOBAL super_read_only=ON")
    connection.commit()
    require(read_flags(connection) == flags, "readonly_flags_restore_failed")


@contextmanager
def database_lease(engine, context, *, frozen=False):
    with engine.connect() as connection:
        database_guard(connection, context)
        require(
            connection.exec_driver_sql(
                "SELECT GET_LOCK(%s,0)",
                ("gift-ledger-gui-v2-" + context["identity"]["instance_id"],),
            ).scalar_one()
            == 1,
            "database_maintenance_lock_busy",
        )
        previous = read_flags(connection)
        try:
            no_application_sessions(connection)
            if frozen:
                set_flags(connection, {"read_only": True, "super_read_only": True})
            yield connection, previous
        finally:
            # A transport loss or changed server identity makes this fail, leaving
            # the permanent failed lease. Never publish successful completion.
            database_guard(connection, context)
            set_flags(connection, previous)
            require(
                connection.exec_driver_sql(
                    "SELECT RELEASE_LOCK(%s)",
                    ("gift-ledger-gui-v2-" + context["identity"]["instance_id"],),
                ).scalar_one()
                == 1,
                "database_maintenance_unlock_failed",
            )
            connection.commit()


def frozen_guard(connection, context):
    database_guard(connection, context)
    require(
        read_flags(connection) == {"read_only": True, "super_read_only": True},
        "backup_database_freeze_lost",
    )
    no_application_sessions(connection)


def mysql_defaults(work, password, *, username="root"):
    require(
        re.fullmatch(r"[0-9a-f]{64}", password) is not None
        and IDENTIFIER.fullmatch(username),
        "mysql_temporary_credentials_invalid",
    )
    path = work / ("client-" + secrets.token_hex(8) + ".cnf")
    exclusive_bytes(
        path,
        (
            "[client]\nuser="
            + username
            + "\npassword="
            + password
            + "\nprotocol=SOCKET\nsocket="
            + str(SOCKET)
            + "\ndefault-character-set=utf8mb4\n"
        ).encode(),
    )
    return path


def run_tool(arguments, *, stdin=None, stdout=None, timeout=600):
    try:
        result = subprocess.run(
            arguments,
            stdin=stdin or subprocess.DEVNULL,
            stdout=stdout or subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GuiMaintenanceError("maintenance_subprocess_failed") from None
    require(result.returncode == 0, "maintenance_subprocess_failed")


@contextmanager
def mysql_stream(arguments, *, importing=False):
    """Give the caller one pipe; always reap the child, including on disk exhaustion."""
    with subprocess.Popen(arguments, stdin=subprocess.PIPE if importing else subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL if importing else subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        timeout = threading.Timer(600, process.kill)
        timeout.daemon = True
        timeout.start()
        pipe = process.stdin if importing else process.stdout
        try:
            yield pipe
            pipe.close()
            require(process.wait() == 0, "maintenance_subprocess_failed")
        finally:
            timeout.cancel()
            if process.poll() is None:
                process.kill()
            pipe.close()


def compressed_dump(work, context, destination=None):
    """Measure or write exactly the gzip stream used by the existing .gladminbackup format."""
    defaults = mysql_defaults(work, context["passwords"]["mysql_root_password"])
    digest = hashlib.sha256()
    size = compressed = 0
    compressor = zlib.compressobj(wbits=31)
    try:
        with mysql_stream(["mysqldump", "--defaults-extra-file=" + str(defaults), "--protocol=SOCKET",
                           "--single-transaction", "--quick", "--hex-blob", "--default-character-set=utf8mb4",
                           "--no-tablespaces", "--set-gtid-purged=OFF", "--skip-triggers", "--skip-add-locks",
                           "--routines=false", "--events=false", "--tz-utc", DATABASE]) as source:
            while block := source.read(1024**2):
                size += len(block)
                require(size <= MAX_BYTES, "database_dump_empty_or_too_large")
                digest.update(block)
                part = compressor.compress(block)
                compressed += len(part)
                if destination is not None:
                    destination.write(part)
            part = compressor.flush()
            compressed += len(part)
            if destination is not None:
                destination.write(part)
        require(size > 0, "database_dump_empty_or_too_large")
        return size, digest.hexdigest(), compressed
    finally:
        defaults.unlink(missing_ok=True)


def backup_capacity(work, context):
    """Take a read-only measurement before the coordinator can stop the website."""
    with tempfile.TemporaryDirectory(dir=work) as directory:
        _, _, compressed = compressed_dump(Path(directory), context)
    # The freeze checks the exact included set again; previews that might be quarantined
    # are conservatively counted without opening any of their private contents.
    app_size = sum(path.lstat().st_size for path in APP_DATA.rglob("*") if not path.is_symlink() and path.is_file())
    assets = sum((ASSETS / name).stat().st_size for name in (*context["legacy"].ASSET_DIGESTS, "SHA256SUMS"))
    estimated = compressed + app_size + assets + 4 * 1024**2
    # The largest phase holds the ciphertext, decrypted ZIP and checked members at once.
    require(shutil.disk_usage(work).free >= 3 * estimated + 8 * 1024**2, "backup_private_workspace_too_small")
    exchange_free = shutil.disk_usage(EXCHANGE).free
    store_free = int(os.environ.get("GIFTLEDGER_BACKUP_DESTINATION_FREE_BYTES", exchange_free))
    require(min(exchange_free, store_free) >= 2 * estimated + 32 * 1024**2, "backup_destination_space_low")
    return estimated


def age(arguments):
    run_tool(["age", *arguments])


def age_identity(path=IDENTITY):
    directory(path.parent, private=True)
    raw = read_file(path, mode=0o600, private=True, maximum=512)
    require(
        re.fullmatch(rb"AGE-SECRET-KEY-[0-9A-Z]{59}\n?", raw) is not None,
        "age_private_identity_invalid",
    )
    return path


def ciphertext_path(options):
    # Public File Station input may have DSM ACL. It remains untrusted ciphertext;
    # no permission weakening or secret material is written to that shared folder.
    directory = _RESTORE_INPUT.folder if _RESTORE_INPUT is not None else REQUEST.parent
    path = directory / options["ciphertext"]
    try:
        no_links(path)
        require(
            file_sha(path) == options["sha256"] and path.stat().st_size > 64,
            "ciphertext_digest_mismatch",
        )
        with path.open("rb") as stream:
            require(
                stream.read(22) == b"age-encryption.org/v1\n", "ciphertext_format_invalid"
            )
    except FileNotFoundError:
        raise GuiMaintenanceError("ciphertext_missing") from None
    except OSError:
        raise GuiMaintenanceError("ciphertext_unreadable") from None
    return path


INPUT_MESSAGES = {
    "ciphertext_missing": "缺少备份密文：请把请求指定的 backup-….glgui.age 与 request.json 放在同一请求目录。REQUEST_INDEX.json 是本机清单，不能代替备份。",
    "ciphertext_unreadable": "无法读取备份密文，请核对上传是否完整及文件访问权限。不要放宽私有卷权限。",
    "ciphertext_digest_mismatch": "备份密文与请求中的摘要不符或文件不完整，请核对同一套备份的准确文件。",
    "ciphertext_format_invalid": "所选文件不是有效的 age 密文，不能用JSON清单、明文或私钥代替备份。",
}


def preflight_ciphertext(request, request_sha):
    """Reject incomplete public inputs before claiming a durable operation.

    Existing failure/history checks must run first. The handler checks the file
    again before decrypting, so a later input change still fails closed.
    """
    if request["action"] not in {"verify", "restore"}:
        return
    try:
        ciphertext_path(request["options"])
    except GuiMaintenanceError as error:
        if error.code in INPUT_MESSAGES:
            publish_json(
                "input-rejected-" + request["operation_id"] + "-" + error.code + ".json",
                {
                    "format": "gift-ledger-gui-input-check-v1",
                    "success": False,
                    "status": "input-rejected",
                    "maintenance_started": False,
                    "project": request["project"],
                    "instance_id": request["instance_id"],
                    "action": request["action"],
                    "operation_id": request["operation_id"],
                    "request_sha256": request_sha,
                    "required_ciphertext": request["options"]["ciphertext"],
                    "error_code": error.code,
                    "message": INPUT_MESSAGES[error.code],
                },
            )
        raise


def validate_fingerprint(value):
    require(
        isinstance(value, dict)
        and set(value) == {"algorithm", "schema_revision", "tables"}
        and value["algorithm"] == "sha256-canonical-v1"
        and value["schema_revision"] in SUPPORTED
        and isinstance(value["tables"], dict)
        and set(value["tables"]) == TABLES,
        "archive_fingerprint_invalid",
    )
    for item in value["tables"].values():
        require(
            isinstance(item, dict)
            and set(item) == {"rows", "sha256"}
            and type(item["rows"]) is int
            and item["rows"] >= 0
            and isinstance(item["sha256"], str)
            and HEX.fullmatch(item["sha256"]),
            "archive_fingerprint_invalid",
        )


def archive_metadata(manifest):
    require(
        isinstance(manifest, dict)
        and set(manifest)
        == {
            "format",
            "version",
            "created_at",
            "operation_id",
            "backup_type",
            "source",
            "fingerprint",
            "app_files",
            "files",
            "assets_sha256",
            "application_version",
        }
        and manifest["format"] == SET_FORMAT
        and type(manifest["version"]) is int
        and manifest["version"] == 1
        and manifest["application_version"] == "1.5.0"
        and isinstance(manifest["operation_id"], str)
        and OPERATION.fullmatch(manifest["operation_id"])
        and manifest["backup_type"] in {"manual", "pre-maintenance"},
        "archive_manifest_invalid",
    )
    try:
        moment = datetime.fromisoformat(manifest["created_at"])
        require(
            moment.utcoffset() == datetime.now(UTC).utcoffset(),
            "archive_timestamp_invalid",
        )
    except (TypeError, ValueError):
        raise GuiMaintenanceError("archive_timestamp_invalid") from None
    source = manifest["source"]
    require(
        isinstance(source, dict)
        and set(source)
        == {
            "project",
            "instance_id",
            "server_uuid",
            "canonical_identity_sha256",
            "layout_sha256",
            "engine_identity_verified",
            "applications_stopped_verified",
        },
        "archive_source_invalid",
    )
    allowed_backup_reference(source["project"])
    require(
        canonical_uuid(source["instance_id"])
        and canonical_uuid(source["server_uuid"])
        and all(
            isinstance(source[key], str) and HEX.fullmatch(source[key])
            for key in ("canonical_identity_sha256", "layout_sha256")
        )
        and source["engine_identity_verified"] is False
        and source["applications_stopped_verified"] is False,
        "archive_source_invalid",
    )
    validate_fingerprint(manifest["fingerprint"])
    require(
        isinstance(manifest["files"], dict) and isinstance(manifest["app_files"], dict),
        "archive_file_metadata_invalid",
    )
    require(
        {
            key: value
            for key, value in manifest["files"].items()
            if key.startswith("app-data/")
        }
        == manifest["app_files"],
        "archive_application_files_mismatch",
    )
    return manifest


def verify_archive(path, work, context, *, extract=False):
    from app.services.database_backup import parse_admin_backup

    require(path.stat().st_size <= MAX_BYTES, "archive_too_large")
    target = work / ("extracted-" + secrets.token_hex(8))
    target.mkdir(mode=0o700)
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        require(
            len(names) == len(set(names))
            and 0 < len(names) <= MAX_FILES + 20
            and "manifest.json" in names,
            "archive_duplicate_or_missing_member",
        )
        header = archive.getinfo("manifest.json")
        require(header.file_size <= MAX_JSON, "archive_manifest_too_large")
        manifest = archive_metadata(parse_json(archive.read("manifest.json")))
        required = {
            "database.gladminbackup",
            "configuration/layout.json",
            *CONFIG_FILES,
            *("assets/" + name for name in context["legacy"].ASSET_DIGESTS),
            "assets/SHA256SUMS",
        }
        require(
            required <= set(manifest["files"])
            and set(names) == {"manifest.json", *manifest["files"]},
            "archive_member_set_invalid",
        )
        require(
            manifest["assets_sha256"] == context["legacy"].ASSETS_MANIFEST_SHA256,
            "archive_assets_incompatible",
        )
        total = 0
        for entry in entries:
            mode = entry.external_attr >> 16
            require(
                entry.compress_type == ZIP_STORED
                and not entry.flag_bits & 1
                and stat.S_IFMT(mode) in {0, stat.S_IFREG}
                and not entry.is_dir(),
                "archive_unsupported_entry_type",
            )
            if entry.filename == "manifest.json":
                continue
            safe_member(entry.filename)
            expected = manifest["files"][entry.filename]
            require(
                isinstance(expected, dict)
                and set(expected) == {"size", "sha256"}
                and type(expected["size"]) is int
                and expected["size"] >= 0
                and expected["size"] == entry.file_size
                and isinstance(expected["sha256"], str)
                and HEX.fullmatch(expected["sha256"]),
                "archive_file_metadata_invalid",
            )
            total += entry.file_size
            require(total <= MAX_BYTES, "archive_expanded_too_large")
            outgoing = target / entry.filename
            outgoing.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest, count = hashlib.sha256(), 0
            with archive.open(entry) as source, outgoing.open("xb") as destination:
                os.chmod(outgoing, 0o600)
                while block := source.read(1024**2):
                    count += len(block)
                    require(count <= entry.file_size, "archive_member_overrun")
                    digest.update(block)
                    destination.write(block)
            require(
                count == expected["size"] and digest.hexdigest() == expected["sha256"],
                "archive_actual_digest_mismatch",
            )
        package = target / "database.gladminbackup"
        parsed = parse_admin_backup(package)
        require(
            parsed.database == DATABASE
            and parsed.schema_revision in SUPPORTED
            and parsed.schema_revision == manifest["fingerprint"]["schema_revision"]
            and parsed.tables
            == {
                name: value["rows"]
                for name, value in manifest["fingerprint"]["tables"].items()
            },
            "archive_database_package_mismatch",
        )
        verify_archived_configuration(target, manifest, context)
        # Always fully extract and hash into tmpfs. No ZIP member is executed and
        # parse_admin_backup validates the inner compressed SQL size and digest.
    if not extract:
        shutil.rmtree(target)
    return manifest, target if extract else None


def verify_archived_site_ca(leaf, root_pem, identity, archived_at):
    """Authenticate the historical public chain at backup time, without the CA key."""
    import ipaddress
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from cryptography.x509.verification import PolicyBuilder, Store

    tls = identity["tls"]
    host = tls["host"]
    ip = ipaddress.IPv4Address(host)
    root = x509.load_pem_x509_certificate(root_pem)
    root.verify_directly_issued_by(root)
    # Standard server-purpose, signature, name-constraint and validity checks
    # use this archive's CA and timestamp. No machine trust store is involved.
    moment = datetime.fromisoformat(archived_at)
    policy = PolicyBuilder().store(Store([root])).time(moment).max_chain_depth(0)
    policy.build_server_verifier(x509.IPAddress(ip)).verify(leaf, [])
    root_ext = root.extensions.get_extension_for_class
    leaf_ext = leaf.extensions.get_extension_for_class
    begin, end = leaf.not_valid_before_utc, leaf.not_valid_after_utc
    lifetime = end - begin
    issued_to = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GiftLedger Site CA " + identity["instance_id"])])
    scope = x509.NameConstraints([x509.IPAddress(ipaddress.ip_network(f"{ip}/32"))], None)
    require(
        root.public_bytes(serialization.Encoding.PEM) == root_pem
        and root.subject == issued_to and root.issuer == issued_to
        and isinstance(root.public_key(), rsa.RSAPublicKey) and root.public_key().key_size == 3072
        and isinstance(leaf.public_key(), rsa.RSAPublicKey) and leaf.public_key().key_size == 2048
        and root_ext(x509.BasicConstraints).value == x509.BasicConstraints(True, 0)
        and root_ext(x509.KeyUsage).value.key_cert_sign
        and root_ext(x509.NameConstraints).value == scope
        and leaf_ext(x509.BasicConstraints).value == x509.BasicConstraints(False, None)
        and not leaf_ext(x509.KeyUsage).value.key_cert_sign
        and leaf_ext(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
        and leaf_ext(x509.SubjectAlternativeName).value == x509.SubjectAlternativeName([x509.IPAddress(ip)])
        and root.not_valid_before_utc <= begin and end <= root.not_valid_after_utc
        and moment < end and lifetime in (timedelta(days=90), timedelta(days=365))
        and tls == {
            "profile": "site-ca-v1", "host": str(ip), "validity_days": lifetime.days,
            "not_before": begin.isoformat(timespec="seconds"), "not_after": end.isoformat(timespec="seconds"),
            "certificate_sha256": leaf.fingerprint(hashes.SHA256()).hex(),
            "root_sha256": root.fingerprint(hashes.SHA256()).hex(),
        },
        "archive_tls_key_or_identity_mismatch",
    )


def verify_archived_configuration(target, manifest, context):
    identity_raw = read_file(target / "configuration/control/identity.json")
    identity = parse_json(identity_raw)
    source = manifest["source"]
    require(
        sha(identity_raw) == source["canonical_identity_sha256"]
        and identity.get("format") == "gift-ledger-gui-site-v1"
        and identity.get("project") == source["project"]
        and identity.get("instance_id") == source["instance_id"]
        and identity.get("app_version") == "v1.5.0"
        and identity.get("schema_revision") == "schema_v2_s011",
        "archive_configuration_identity_mismatch",
    )
    require(
        set(identity.get("files", {})) == set(context["legacy"].FILE_MODES),
        "archive_canonical_file_set_invalid",
    )
    values, certificate_files = [], {}
    for relative, metadata in identity.get("files", {}).items():
        require(
            "configuration/" + relative in CONFIG_FILES,
            "archive_secret_namespace_invalid",
        )
        raw = read_file(target / "configuration" / relative, maximum=MAX_JSON)
        require(
            isinstance(metadata, dict)
            and set(metadata) == {"sha256", "mode", "uid", "gid"}
            and sha(raw) == metadata.get("sha256")
            and metadata["mode"] == context["legacy"].FILE_MODES[relative]
            and type(metadata["uid"]) is int
            and type(metadata["gid"]) is int
            and metadata["uid"] == 0
            and metadata["gid"] == 0,
            "archive_secret_identity_mismatch",
        )
        if relative.startswith("secrets/"):
            require(
                re.fullmatch(rb"[0-9a-f]{64}\n", raw) is not None,
                "archive_secret_invalid",
            )
            values.append(raw)
        else:
            certificate_files[relative] = raw
    require(
        len(values) == 3 and len(set(values)) == 3,
        "archive_secrets_missing_or_duplicate",
    )
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        certificate = x509.load_pem_x509_certificate(
            certificate_files["certs/fullchain.pem"]
        )
        key = serialization.load_pem_private_key(
            certificate_files["certs/privkey.pem"], password=None
        )
        public = lambda value: value.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        require(public(certificate.public_key()) == public(key.public_key()), "archive_tls_key_or_identity_mismatch")
        tls = identity.get("tls", {})
        leaf_pem, root_pem = certificate_files["certs/fullchain.pem"], certificate_files["certs/ca-chain.pem"]
        if "profile" in tls:
            require(tls["profile"] == "site-ca-v1" and certificate.public_bytes(serialization.Encoding.PEM) == leaf_pem,
                    "archive_tls_key_or_identity_mismatch")
            verify_archived_site_ca(certificate, root_pem, identity, manifest["created_at"])
        else:
            # The old self-signed certificate fingerprint is over PEM bytes.
            require(root_pem == leaf_pem and tls.get("certificate_sha256") == sha(leaf_pem),
                    "archive_tls_key_or_identity_mismatch")
    except GuiMaintenanceError:
        raise
    except Exception:  # noqa: BLE001 -- map all parser failures without disclosing private material
        raise GuiMaintenanceError("archive_tls_configuration_invalid") from None
    require(
        file_sha(target / "configuration/layout.json") == source["layout_sha256"],
        "archive_layout_identity_mismatch",
    )
    for name, expected in context["legacy"].ASSET_DIGESTS.items():
        require(
            file_sha(target / "assets" / name) == expected,
            "archive_asset_digest_mismatch",
        )
    require(
        file_sha(target / "assets/SHA256SUMS")
        == context["legacy"].ASSETS_MANIFEST_SHA256,
        "archive_asset_digest_mismatch",
    )


def source_metadata(context):
    return {
        "project": context["project"],
        "instance_id": context["identity"]["instance_id"],
        "server_uuid": context["server_uuid"],
        "canonical_identity_sha256": context["identity_sha256"],
        "layout_sha256": context["layout"]["layout_sha256"],
        "engine_identity_verified": False,
        "applications_stopped_verified": False,
    }


def backup(request, context, engine, work):
    from app.services.database_backup import (
        FORMAT_NAME,
        FORMAT_VERSION,
        get_mysqldump_version,
        new_backup_id,
        parse_admin_backup,
        write_admin_backup_package,
    )

    with database_lease(engine, context, frozen=True) as (connection, previous):
        frozen_guard(connection, context)
        verify_schema(engine, context, revision=request["schema_revision"])
        before = fingerprint(engine, context)
        files_before = application_files()
        compressed, package = work / "dump.sql.gz", work / "database.gladminbackup"
        with compressed.open("xb") as destination:
            os.chmod(compressed, 0o600)
            sql_size, sql_sha, _ = compressed_dump(work, context, destination)
        frozen_guard(connection, context)
        require(
            fingerprint(engine, context) == before
            and application_files() == files_before,
            "backup_source_changed",
        )
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "backup_id": new_backup_id(),
            "backup_type": request["options"]["backup_type"],
            "created_at": utc_now(),
            "database": DATABASE,
            "schema_revision": before["schema_revision"],
            "application_version": "1.5.0",
            "mysqldump_version": get_mysqldump_version("mysqldump", timeout=30),
            "sql_sha256": sql_sha,
            "sql_size": sql_size,
            "alembic_version": before["schema_revision"],
            "tables": {table: item["rows"] for table, item in before["tables"].items()},
        }
        write_admin_backup_package(
            manifest=manifest, gzip_sql_path=compressed, output_path=package
        )
        compressed.unlink()
        parse_admin_backup(package)
        files = {
            "database.gladminbackup": package,
            **{name: SITE / relative for name, relative in CONFIG_FILES.items()},
            "configuration/layout.json": LAYOUT / "layout.json",
            **{
                "assets/" + name: ASSETS / name
                for name in (*context["legacy"].ASSET_DIGESTS, "SHA256SUMS")
            },
            **{
                name: APP_DATA / name.removeprefix("app-data/") for name in files_before
            },
        }
        hashes = {}
        for name, path in files.items():
            safe_member(name)
            no_links(path)
            # Reject a mistakenly included recovery key anywhere in the archive.
            tail = b""
            with path.open("rb") as stream:
                while block := stream.read(1024**2):
                    require(
                        b"AGE-SECRET-KEY-" not in tail + block,
                        "recovery_key_in_backup_forbidden",
                    )
                    tail = block[-32:]
            hashes[name] = {"size": path.stat().st_size, "sha256": file_sha(path)}
        require(
            sum(item["size"] for item in hashes.values()) <= MAX_BYTES,
            "complete_backup_too_large",
        )
        envelope = {
            "format": SET_FORMAT,
            "version": 1,
            "created_at": utc_now(),
            "operation_id": request["operation_id"],
            "backup_type": request["options"]["backup_type"],
            "source": source_metadata(context),
            "fingerprint": before,
            "app_files": files_before,
            "files": hashes,
            "assets_sha256": context["legacy"].ASSETS_MANIFEST_SHA256,
            "application_version": "1.5.0",
        }
        archive_path = work / "complete.glgui"
        with ZipFile(archive_path, "x", compression=ZIP_STORED, strict_timestamps=False) as archive:
            os.chmod(archive_path, 0o600)
            for name, path in files.items():
                archive.write(path, name)
            entry = ZipInfo("manifest.json")
            entry.create_system, entry.external_attr = 3, 0o100600 << 16
            archive.writestr(entry, json_bytes(envelope))
        verify_archive(archive_path, work, context)
        encrypted = work / "complete.glgui.age"
        age(
            [
                "--encrypt",
                "--recipient",
                request["options"]["recipient"],
                "--output",
                str(encrypted),
                str(archive_path),
            ]
        )
        os.chmod(encrypted, 0o600)
        require(encrypted.stat().st_size > 64, "encrypted_backup_empty")
        frozen_guard(connection, context)
        require(
            fingerprint(engine, context) == before
            and application_files() == files_before,
            "backup_source_changed",
        )
        require(
            all(
                path.stat().st_size == hashes[name]["size"]
                and file_sha(path) == hashes[name]["sha256"]
                for name, path in files.items()
            ),
            "backup_configuration_or_asset_changed",
        )
        filename = "backup-" + request["operation_id"] + ".glgui.age"
        result = {
            "fingerprint": before,
            "app_files": files_before,
            "ciphertext": filename,
            "ciphertext_sha256": file_sha(encrypted),
            "archive_sha256": file_sha(archive_path),
            "schema_revision": before["schema_revision"],
            "backup_type": request["options"]["backup_type"],
            "source": source_metadata(context),
            "plaintext_package_validated": True,
            "encryption_roundtrip_verified": False,
            "reliable_backup_verified": False,
            "restore_verified": False,
            "applications_stopped_verified": False,
            "sql_freeze_verified": True,
            "readonly_before": previous,
            "automatic_backup_configured": False,
            "offsite_configured": False,
        }
    # Publish only after original GLOBAL flags and SQL lease are verified restored.
    no_links(EXCHANGE / filename)
    with (
        encrypted.open("rb") as source,
        (EXCHANGE / filename).open("xb") as destination,
    ):
        os.chmod(EXCHANGE / filename, 0o644)
        shutil.copyfileobj(source, destination, 1024**2)
        destination.flush()
        os.fsync(destination.fileno())
    require(
        file_sha(EXCHANGE / filename) == result["ciphertext_sha256"],
        "published_ciphertext_mismatch",
    )
    result["readonly_restored"] = True
    return result


def decrypted_archive(request, work, context, *, extract=False):
    incoming = ciphertext_path(request["options"])
    key = age_identity()
    plaintext = work / "decrypted.glgui"
    age(
        ["--decrypt", "--identity", str(key), "--output", str(plaintext), str(incoming)]
    )
    os.chmod(plaintext, 0o600)
    manifest, extracted = verify_archive(plaintext, work, context, extract=extract)
    archive_sha = file_sha(plaintext)
    plaintext.unlink()
    if _RESTORE_INPUT is not None:
        _RESTORE_INPUT.check_archive(manifest, archive_sha)
    return manifest, extracted, archive_sha


def verify_backup(request, context, engine, work):
    manifest, _, archive_sha = decrypted_archive(request, work, context)
    require(
        manifest["source"] == source_metadata(context)
        and manifest["fingerprint"]["schema_revision"] == request["schema_revision"],
        "verification_source_mismatch",
    )
    backup_id = manifest["operation_id"]
    receipt = completed_operation(backup_id, context, expected_action="backup")
    details = receipt["details"]
    require(
        details["archive_sha256"] == archive_sha
        and details["ciphertext"] == request["options"]["ciphertext"]
        and details["ciphertext_sha256"] == request["options"]["sha256"]
        and details["fingerprint"] == manifest["fingerprint"]
        and details["app_files"] == manifest["app_files"],
        "backup_receipt_mismatch",
    )
    with database_lease(engine, context, frozen=True) as (connection, previous):
        frozen_guard(connection, context)
        verify_schema(engine, context, revision=request["schema_revision"])
        require(
            fingerprint(engine, context) == manifest["fingerprint"]
            and application_files() == manifest["app_files"],
            "verified_backup_source_changed",
        )
    return {
        "backup_operation_id": backup_id,
        "ciphertext": request["options"]["ciphertext"],
        "ciphertext_sha256": request["options"]["sha256"],
        "archive_sha256": archive_sha,
        "fingerprint": manifest["fingerprint"],
        "app_files": manifest["app_files"],
        "source": source_metadata(context),
        "package_verified": True,
        "encryption_roundtrip_verified": True,
        "reliable_backup_verified": True,
        "restore_verified": False,
        "readonly_before": previous,
        "readonly_restored": True,
        "applications_stopped_verified": False,
        "automatic_backup_configured": False,
        "offsite_configured": False,
    }


def stream_sql(package, metadata, destination=None):
    """Validate line starts and the complete dump in bounded memory before allowing any SQL."""
    forbidden = re.compile(rb"^(?:\\[!.]|(?:system|source)\s)", re.IGNORECASE)
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    looking = True
    with ZipFile(package) as archive, archive.open("dump.sql.gz") as zipped, gzip.GzipFile(fileobj=zipped) as source:
        # Extended INSERT lines can exceed one MiB. Only their first seven
        # non-indentation bytes are needed to recognise a forbidden command.
        while block := source.readline(64 * 1024):
            size += len(block)
            require(size <= metadata.sql_size and size <= MAX_BYTES, "restore_sql_overrun")
            digest.update(block)
            if looking:
                text = block if prefix else block.lstrip()
                prefix += text[:7-len(prefix)]
                require(not forbidden.match(prefix), "sql_client_directive_forbidden")
                looking = len(prefix) < 7
            if block.endswith(b"\n"):
                looking, prefix = True, b""
            if destination is not None:
                destination.write(block)
    require(size == metadata.sql_size and digest.hexdigest() == metadata.sql_sha256, "restore_sql_digest_mismatch")


def lock_application_account(connection):
    value = connection.exec_driver_sql(
        "SELECT account_locked FROM mysql.user "
        "WHERE User='gift_ledger_app' AND Host=%s",
        ("%",),
    ).scalar_one()
    require(value in {"Y", "N"}, "application_account_state_invalid")
    connection.exec_driver_sql("ALTER USER 'gift_ledger_app'@%s ACCOUNT LOCK", ("%",))
    connection.commit()
    no_application_sessions(connection)
    return value


def restore_application_account(connection, previous):
    require(previous in {"Y", "N"}, "application_account_state_invalid")
    connection.exec_driver_sql(
        "ALTER USER 'gift_ledger_app'@%s ACCOUNT "
        + ("LOCK" if previous == "Y" else "UNLOCK"),
        ("%",),
    )
    connection.commit()
    value = connection.exec_driver_sql(
        "SELECT account_locked FROM mysql.user "
        "WHERE User='gift_ledger_app' AND Host=%s",
        ("%",),
    ).scalar_one()
    require(value == previous, "application_account_restore_failed")


def restore(request, context, engine, work):
    manifest, extracted, archive_sha = decrypted_archive(
        request, work, context, extract=True
    )
    source = manifest["source"]
    pinned_restore_source(request["options"])
    profile = backup_reference_profile()
    if profile is not None and profile.BACKUP_SOURCE:
        require(archive_sha == profile.BACKUP_SOURCE["archive_sha256"], "imported_backup_archive_mismatch")
    source_revision = request["options"].get("source_schema_revision", "schema_v2_s011")
    expected = {
        key.removeprefix("source_"): request["options"][key]
        for key in ("source_project", "source_instance_id", "source_server_uuid")
    }
    require(
        all(source[key] == value for key, value in expected.items())
        and manifest["operation_id"] == request["options"]["source_operation_id"]
        and source["instance_id"] != context["identity"]["instance_id"]
        and source["server_uuid"] != context["server_uuid"]
        and source_revision in SUPPORTED
        and manifest["fingerprint"]["schema_revision"] == source_revision
        and (source_revision == "schema_v2_s011" or manifest["backup_type"] == "pre-maintenance"),
        "restore_source_mismatch_or_identical",
    )
    from app.services.database_backup import parse_admin_backup

    package = extracted / "database.gladminbackup"
    metadata = parse_admin_backup(package)
    stream_sql(package, metadata)
    target_configuration = {
        name: file_sha(SITE / relative) for name, relative in CONFIG_FILES.items()
    }
    imported_user = "restore_" + secrets.token_hex(8)
    imported_password = secrets.token_hex(32)
    account_previous = None
    with database_lease(engine, context) as (connection, previous):
        verify_schema(engine, context, revision="schema_v2_s011", empty=True)
        target_data = fingerprint(engine, context)
        require(
            all(target_data["tables"][table]["rows"] == 0 for table in BUSINESS),
            "restore_business_target_nonempty",
        )
        application_files(empty=True)
        require(
            previous == {"read_only": False, "super_read_only": False},
            "restore_target_not_writable_fresh",
        )
        account_previous = lock_application_account(connection)
        # Only this fresh, already initialized and seed-only schema is rebuilt.
        # This restricted socket account has no global, FILE or grant privileges.
        connection.exec_driver_sql(
            "CREATE USER %s@'localhost' IDENTIFIED BY %s",
            (imported_user, imported_password),
        )
        connection.exec_driver_sql(
            "GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,ALTER,INDEX,"
            "REFERENCES ON gift_ledger_v2.* TO %s@'localhost'",
            (imported_user,),
        )
        connection.commit()
        defaults = mysql_defaults(work, imported_password, username=imported_user)
        try:
            with mysql_stream(["mysql", "--defaults-extra-file=" + str(defaults), "--protocol=SOCKET",
                               "--binary-mode", "--default-character-set=utf8mb4", DATABASE], importing=True) as target:
                stream_sql(package, metadata, target)
        finally:
            defaults.unlink(missing_ok=True)
        database_guard(
            connection, context, revision=source_revision, importer=imported_user
        )
        connection.exec_driver_sql("DROP USER %s@'localhost'", (imported_user,))
        connection.commit()
        verify_schema(engine, context, revision=source_revision)
        require(
            fingerprint(engine, context) == manifest["fingerprint"],
            "restored_database_mismatch",
        )
        for name, expected_file in manifest["app_files"].items():
            target = APP_DATA / name.removeprefix("app-data/")
            no_links(target)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (
                (extracted / name).open("rb") as incoming,
                target.open("xb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing, 1024**2)
            os.chmod(target, 0o600)
            os.chown(target, 10001, 0)
            require(
                file_sha(target) == expected_file["sha256"],
                "restored_application_file_mismatch",
            )
        for path in [
            APP_DATA,
            *(item for item in APP_DATA.rglob("*") if item.is_dir()),
        ]:
            os.chmod(path, 0o700)
            os.chown(path, 10001, 0)
        require(
            application_files() == manifest["app_files"],
            "restored_application_files_mismatch",
        )
        require(
            {name: file_sha(SITE / relative) for name, relative in CONFIG_FILES.items()}
            == target_configuration,
            "restored_target_configuration_changed",
        )
        restore_application_account(connection, account_previous)
    return {
        "fingerprint": manifest["fingerprint"],
        "app_files": manifest["app_files"],
        "source": source,
        "source_operation_id": manifest["operation_id"],
        "source_schema_revision": source_revision,
        "archive_sha256": archive_sha,
        "restored": True,
        "new_empty_initialized_schema_rebuilt": True,
        "existing_business_data_overwritten": False,
        "source_configuration_installed": False,
        "target_configuration_preserved": True,
        "target_configuration_sha256": target_configuration,
        "readonly_restored": True,
        "applications_started": False,
        "automatic_backup_configured": False,
        "offsite_configured": False,
    }


def kinship_evidence(engine):
    from maintenance.database import canonical_value
    from sqlalchemy import inspect, text

    with engine.connect() as connection:
        columns = [
            column["name"]
            for column in inspect(connection).get_columns("kinship_types")
        ]
        rows = (
            connection.execute(
                text(
                    "SELECT "
                    + ",".join(map(identifier, columns))
                    + " FROM kinship_types ORDER BY id"
                )
            )
            .mappings()
            .all()
        )
    custom, systems = hashlib.sha256(json_bytes(columns)), {}
    count = 0
    for row in rows:
        if row["user_id"] is None:
            systems[row["system_code"]] = {
                key: row[key]
                for key in ("id", "system_code", "type_name", "sort_order", "is_active")
            }
        else:
            custom.update(
                json_bytes([canonical_value(row[name]) for name in columns]) + b"\n"
            )
            count += 1
    return {
        "custom_sha256": custom.hexdigest(),
        "custom_rows": count,
        "systems": systems,
    }


def verify_upgrade_content(before, after, old, new):
    require(
        before["schema_revision"] == "schema_v2_s010"
        and after["schema_revision"] == "schema_v2_s011",
        "upgrade_revision_mismatch",
    )
    for table in TABLES - {"alembic_version", "kinship_types"}:
        require(
            before["tables"][table] == after["tables"][table],
            "upgrade_business_changed",
        )
    require(
        old["custom_sha256"] == new["custom_sha256"]
        and old["custom_rows"] == new["custom_rows"]
        and set(new["systems"]) == set(old["systems"]) | {"kinship.grandchildren"},
        "upgrade_custom_dictionary_or_system_identity_changed",
    )
    for code, row in old["systems"].items():
        require(
            new["systems"][code]
            == row | {"sort_order": row["sort_order"] + (row["sort_order"] >= 13)},
            "upgrade_existing_dictionary_changed",
        )
    new_row = new["systems"]["kinship.grandchildren"]
    require(
        new_row["system_code"] == "kinship.grandchildren"
        and new_row["type_name"] == "之孙辈"
        and new_row["sort_order"] == 13
        and new_row["is_active"] == 1,
        "upgrade_new_dictionary_invalid",
    )


def completed_operation(operation_id, context, *, expected_action=None):
    require(
        isinstance(operation_id, str) and OPERATION.fullmatch(operation_id),
        "operation_id_invalid",
    )
    path = CONTROL / operation_id
    directory(path, private=True)
    require(not (path / "failed.json").exists(), "previous_operation_failed")
    started = json_file(path / "started.json", private=True)
    completed = json_file(path / "completed.json", private=True)
    require(
        completed.get("format") == FORMAT
        and completed.get("version") == 1
        and completed.get("success") is True
        and completed.get("operation_id") == operation_id
        and completed.get("project") == context["project"]
        and completed.get("instance_id") == context["identity"]["instance_id"]
        and completed.get("server_uuid") == context["server_uuid"]
        and completed.get("started_sha256") == file_sha(path / "started.json")
        and completed.get("request_sha256") == started.get("request_sha256")
        and (expected_action is None or completed.get("action") == expected_action),
        "completed_operation_identity_invalid",
    )
    return completed


def upgrade(request, context, engine, work):
    from sqlalchemy.engine import URL

    backup_receipt = completed_operation(
        request["options"]["backup_operation_id"], context, expected_action="backup"
    )
    verification = completed_operation(
        request["options"]["verify_operation_id"], context, expected_action="verify"
    )
    old_backup, proof = backup_receipt["details"], verification["details"]
    require(
        old_backup["backup_type"] == "pre-maintenance"
        and old_backup["schema_revision"] == "schema_v2_s010"
        and proof["backup_operation_id"] == backup_receipt["operation_id"]
        and proof["reliable_backup_verified"] is True
        and proof["archive_sha256"] == old_backup["archive_sha256"]
        and proof["ciphertext_sha256"] == old_backup["ciphertext_sha256"],
        "upgrade_reliable_prebackup_required",
    )
    require(
        file_sha(EXCHANGE / old_backup["ciphertext"])
        == old_backup["ciphertext_sha256"],
        "upgrade_prebackup_unavailable",
    )
    with database_lease(engine, context, frozen=True) as (connection, previous):
        frozen_guard(connection, context)
        verify_schema(engine, context, revision="schema_v2_s010")
        before = fingerprint(engine, context)
        files_before = application_files()
        require(
            before == old_backup["fingerprint"] == proof["fingerprint"]
            and files_before == old_backup["app_files"] == proof["app_files"],
            "upgrade_prebackup_source_changed",
        )
        old_kinship = kinship_evidence(engine)
        # Verified fixed historical migration only. Block new application logins,
        # remove super-read-only for the root socket migration, keep read_only ON
        # so unprivileged writers still cannot write even if a GUI start is tried.
        set_flags(connection, {"read_only": False, "super_read_only": False})
        account_previous = lock_application_account(connection)
        set_flags(connection, {"read_only": True, "super_read_only": False})
        original = os.environ.get("GIFT_LEDGER_DATABASE_URL")
        original_jwt = os.environ.get("GIFT_LEDGER_JWT_SECRET")
        try:
            os.environ["GIFT_LEDGER_DATABASE_URL"] = URL.create(
                "mysql+pymysql",
                username="root",
                password=context["passwords"]["mysql_root_password"],
                database=DATABASE,
                query={"unix_socket": str(SOCKET), "charset": "utf8mb4"},
            ).render_as_string(hide_password=False)
            os.environ["GIFT_LEDGER_JWT_SECRET"] = context["passwords"]["jwt_secret"]
            context["legacy"].run_alembic(BACKEND, "upgrade", "schema_v2_s011")
        finally:
            for key, value in (
                ("GIFT_LEDGER_DATABASE_URL", original),
                ("GIFT_LEDGER_JWT_SECRET", original_jwt),
            ):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        verify_schema(engine, context, revision="schema_v2_s011")
        after = fingerprint(engine, context)
        verify_upgrade_content(before, after, old_kinship, kinship_evidence(engine))
        require(
            application_files() == files_before, "upgrade_application_files_changed"
        )
        set_flags(connection, {"read_only": False, "super_read_only": False})
        restore_application_account(connection, account_previous)
    return {
        "from_revision": "schema_v2_s010",
        "to_revision": "schema_v2_s011",
        "fingerprint_before": before,
        "fingerprint": after,
        "app_files": files_before,
        "backup_operation_id": backup_receipt["operation_id"],
        "verify_operation_id": verification["operation_id"],
        "business_content_preserved": True,
        "custom_dictionaries_preserved": True,
        "readonly_before": previous,
        "readonly_restored": True,
        "applications_started": False,
        "automatic_backup_configured": False,
        "offsite_configured": False,
    }


def require_tmpfs(path=Path("/tmp")):
    directory(path)
    # A read-only utility root filesystem cannot silently fall back to persistent
    # plaintext. Both compose templates mount /tmp as a bounded private tmpfs.
    lines = Path("/proc/self/mountinfo").read_text().splitlines()
    require(
        any(
            line.split(" - ", 1)[1].split()[0] == "tmpfs"
            and line.split(" - ", 1)[0].split()[4] == str(path)
            for line in lines
        ),
        "plaintext_tmpfs_required",
    )


def public_receipt(receipt):
    return {key: value for key, value in receipt.items() if key != "request"}


def history_entries(control):
    """Bound directory enumeration without deleting durable private history.

    Three automation receipts per day fit more than twelve years. Individual
    started/completed records retain the existing bounded JSON reader.
    """
    entries = []
    for entry in control.iterdir():
        entries.append(entry)
        require(
            len(entries) <= MAX_HISTORY_ENTRIES, "maintenance_history_limit_exceeded"
        )
    return entries


def publish_json(name, value):
    directory(EXCHANGE)
    require(re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name), "public_output_name_invalid")
    target = EXCHANGE / name
    raw = json_bytes(value) + b"\n"
    if target.exists():
        require(read_file(target) == raw, "public_receipt_already_exists_different")
        return
    exclusive_bytes(target, raw, mode=0o644)


def inspect_site(project):
    context = local_context(project)
    engine = engine_for(context)
    try:
        with engine.connect() as connection:
            flags = database_guard(connection, context)
        verify_schema(engine, context)
        data = fingerprint(engine, context)
        result = {
            "format": "gift-ledger-gui-v2-status",
            "version": 1,
            "success": True,
            "project": project,
            "instance_id": context["identity"]["instance_id"],
            "server_uuid": context["server_uuid"],
            "schema_revision": data["schema_revision"],
            "application_version": "1.5.0",
            "assets_sha256": context["legacy"].ASSETS_MANIFEST_SHA256,
            "layout_sha256": context["layout"]["layout_sha256"],
            "canonical_identity_sha256": context["identity_sha256"],
            "fingerprint": data,
            "app_files": application_files(),
            "readonly": flags,
            "applications_stopped_verified": False,
            "automatic_backup_configured": False,
            "offsite_configured": False,
        }
        filename = "status-" + result["instance_id"] + ".json"
        target = EXCHANGE / filename
        if target.exists() and read_file(target) != json_bytes(result) + b"\n":
            filename = (
                "status-"
                + result["instance_id"]
                + "-"
                + data["schema_revision"]
                + "-"
                + sha(json_bytes(result))[:16]
                + ".json"
            )
        publish_json(filename, result)
        return result
    finally:
        engine.dispose()


def automation_session_lock(context, lease, session_id, request):
    """Duplicate only this site's private shared lock for a bound backup session.

    The caller retains the original open file description and its flock until
    services have safely resumed. This internal entry point never unlocks it,
    bypasses another transaction, or permits restore/upgrade in a backup session.
    """
    require(
        isinstance(session_id, str)
        and OPERATION.fullmatch(session_id)
        and isinstance(request, dict)
        and request.get("action") in {"backup", "verify"}
        and isinstance(request.get("operation_id"), str)
        and OPERATION.fullmatch(request["operation_id"]),
        "automation_session_invalid",
    )
    path = CONTROL / "operation.lock"
    read_file(path, mode=0o600, private=True)
    try:
        descriptor = lease.fileno()
        actual = os.fstat(descriptor)
        expected = path.lstat()
    except (AttributeError, OSError, ValueError):
        raise GuiMaintenanceError("automation_lease_invalid") from None
    require(
        stat.S_ISREG(actual.st_mode)
        and actual.st_nlink == 1
        and stat.S_IMODE(actual.st_mode) == 0o600
        and (actual.st_uid, actual.st_gid) == (0, 0)
        and (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino),
        "automation_lease_invalid",
    )
    operation = CONTROL / session_id
    directory(operation, private=True)
    started = json_file(operation / "started.json", private=True)
    require(
        started.get("format") == FORMAT
        and type(started.get("version")) is int
        and started["version"] == 1
        and started.get("action") == "automation-session"
        and started.get("status") == "started"
        and started.get("success") is False
        and started.get("operation_id") == session_id
        and started.get("project") == context["project"]
        and started.get("instance_id") == context["identity"]["instance_id"]
        and started.get("server_uuid") == context["server_uuid"]
        and isinstance(started.get("request_sha256"), str)
        and HEX.fullmatch(started["request_sha256"])
        and not (operation / "failed.json").exists()
        and not (operation / "completed.json").exists(),
        "automation_session_identity_invalid",
    )
    options = started.get("options", {})
    require(
        isinstance(options, dict)
        and options.get(request["action"] + "_operation_id") == request["operation_id"]
        and request["operation_id"] != session_id,
        "automation_session_operation_mismatch",
    )
    return os.fdopen(os.dup(descriptor), "r+b")


def _current_preview_guard(path):
    scope = _AUTOMATION_PREVIEW_CONTEXT
    if scope is None:
        return None
    require(
        Path(path).absolute() == APP_DATA.absolute(), "automation_preview_root_mismatch"
    )
    lock = automation_session_lock(
        scope["context"], scope["lease"], scope["session_id"], scope["request"]
    )
    try:
        import fcntl

        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuiMaintenanceError("maintenance_operation_busy") from None
        require(scope["guard"].verify() is True, "automation_preview_snapshot_changed")
        return scope["guard"]
    finally:
        lock.close()


@contextmanager
def automation_preview_scope(context, lease, session_id, guard):
    """Internal fixed-window scope; legacy/manual maintenance stays strict.

    The worker enters only after actual host stop and database transaction drain.
    Every file inventory revalidates the private lease and exact metadata guards.
    This scope cannot authorize restore/upgrade or a second session's operation.
    """
    global _AUTOMATION_PREVIEW_CONTEXT
    require(_AUTOMATION_PREVIEW_CONTEXT is None, "automation_preview_scope_nested")
    expected = {APP_DATA / "imports", APP_DATA / "personal-restores"}
    require(
        set(getattr(guard, "directories", ())) == expected
        and callable(getattr(guard, "verify", None)),
        "automation_preview_guard_invalid",
    )
    started = json_file(CONTROL / session_id / "started.json", private=True)
    request = {
        "action": "backup",
        "operation_id": started.get("options", {}).get("backup_operation_id"),
    }
    duplicate = automation_session_lock(context, lease, session_id, request)
    _AUTOMATION_PREVIEW_CONTEXT = {
        "context": context,
        "lease": duplicate,
        "session_id": session_id,
        "request": request,
        "guard": guard,
    }
    try:
        _current_preview_guard(APP_DATA)
        yield
        require(guard.verify() is True, "automation_preview_snapshot_changed")
    finally:
        _AUTOMATION_PREVIEW_CONTEXT = None
        duplicate.close()


def execute(project, action, *, _lease=None, _session_id=None):
    import fcntl

    context = local_context(project)
    if _AUTOMATION_PREVIEW_CONTEXT is not None:
        require(
            _lease is not None
            and _session_id == _AUTOMATION_PREVIEW_CONTEXT["session_id"]
            and action in {"backup", "verify"},
            "automation_preview_session_mismatch",
        )
    request_raw = read_file(REQUEST)
    request = valid_request(parse_json(request_raw), project, action)
    require(
        request["instance_id"] == context["identity"]["instance_id"]
        and request["server_uuid"] == context["server_uuid"],
        "request_site_identity_mismatch",
    )
    request_sha = sha(request_raw)
    operation_dir = CONTROL / request["operation_id"]
    directory(EXCHANGE)
    lock_path = CONTROL / "operation.lock"
    no_links(lock_path)
    if _lease is not None or _session_id is not None:
        require(
            _lease is not None and _session_id is not None, "automation_lease_invalid"
        )
        lock = automation_session_lock(context, _lease, _session_id, request)
    elif lock_path.exists():
        read_file(lock_path, mode=0o600, private=True)
        lock = lock_path.open("r+b")
    else:
        exclusive_bytes(lock_path, b"")
        lock = lock_path.open("r+b")
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuiMaintenanceError("maintenance_operation_busy") from None
        require(
            not (CONTROL / "active.json").exists(),
            "previous_operation_incomplete_or_failed",
        )
        for previous_operation in history_entries(CONTROL):
            if previous_operation.is_dir() and OPERATION.fullmatch(
                previous_operation.name
            ):
                no_links(previous_operation)
                if _lease is not None and previous_operation.name == _session_id:
                    continue
                if (previous_operation / "RECONCILED.json").exists():
                    import target
                    history = load_module("maintenance_failed_probe_history",
                                          Path(__file__).with_name("failed_probe_history.py"))
                    contract = target.runtime_target(project)
                    require(isinstance(contract, dict) and contract.get("project") == project,
                            "probe_recovery_history_target_required")
                    directory(previous_operation, private=True)
                    history.read_reconciled_history(previous_operation,
                        read_bytes=lambda item: read_file(item, mode=0o600, private=True),
                        expected={"project": project, "engine_id": contract.get("engine_id"),
                                  "target_sha256": history.digest(contract),
                                  "instance_id": context["identity"]["instance_id"],
                                  "server_uuid": context["server_uuid"]})
                    continue
                require(
                    not (previous_operation / "failed.json").exists()
                    and (previous_operation / "completed.json").exists(),
                    "previous_operation_incomplete_or_failed",
                )
        if operation_dir.exists():
            completed = completed_operation(
                request["operation_id"], context, expected_action=action
            )
            require(
                completed["request_sha256"] == request_sha, "completed_request_changed"
            )
            engine = engine_for(context)
            try:
                verify_schema(
                    engine,
                    context,
                    revision=completed["details"]["fingerprint"]["schema_revision"],
                )
                require(
                    fingerprint(engine, context) == completed["details"]["fingerprint"]
                    and application_files() == completed["details"]["app_files"],
                    "completed_operation_data_changed",
                )
                if action == "backup":
                    require(
                        file_sha(EXCHANGE / completed["details"]["ciphertext"])
                        == completed["details"]["ciphertext_sha256"],
                        "completed_ciphertext_changed",
                    )
                publish_json(
                    "completed-" + request["operation_id"] + ".json",
                    public_receipt(completed),
                )
                return {
                    "status": "completed-readonly-verified",
                    "operation_id": request["operation_id"],
                    "project": project,
                    "action": action,
                }
            finally:
                engine.dispose()
        # Missing/wrong public uploads must not create started/active/failed
        # records. This does not clear or bypass any earlier failed operation.
        preflight_ciphertext(request, request_sha)
        require_tmpfs()
        no_links(operation_dir)
        operation_dir.mkdir(mode=0o700)
        base = {
            "format": FORMAT,
            "version": 1,
            "project": project,
            "action": action,
            "operation_id": request["operation_id"],
            "instance_id": request["instance_id"],
            "server_uuid": request["server_uuid"],
            "request_sha256": request_sha,
            "assets_sha256": context["legacy"].ASSETS_MANIFEST_SHA256,
            "layout_sha256": context["layout"]["layout_sha256"],
            "started_at": utc_now(),
        }
        started_sha = exclusive_json(
            operation_dir / "started.json",
            {**base, "success": False, "status": "started"},
        )
        exclusive_json(CONTROL / "active.json", {**base, "started_sha256": started_sha})
        engine = engine_for(context)
        try:
            with engine.connect() as connection:
                database_guard(connection, context, revision=request["schema_revision"])
            with tempfile.TemporaryDirectory(prefix="gui-v2-", dir="/tmp") as temporary:
                work = Path(temporary)
                os.chmod(work, 0o700)
                handlers = {
                    "backup": backup,
                    "verify": verify_backup,
                    "restore": restore,
                    "upgrade": upgrade,
                }
                details = handlers[action](request, context, engine, work)
            require(
                read_file(REQUEST) == request_raw, "request_changed_during_operation"
            )
            result = {
                **base,
                "status": "completed",
                "success": True,
                "completed_at": utc_now(),
                "started_sha256": started_sha,
                "details": details,
            }
            exclusive_json(operation_dir / "completed.json", result)
            (CONTROL / "active.json").rename(operation_dir / "lease-completed.json")
            publish_json(
                "completed-" + request["operation_id"] + ".json", public_receipt(result)
            )
            return result
        except BaseException as error:
            failure = {
                **base,
                "status": "failed",
                "success": False,
                "failed_at": utc_now(),
                "started_sha256": started_sha,
                "error_code": error.code
                if isinstance(error, GuiMaintenanceError)
                else "maintenance_failed_preserve_evidence",
            }
            try:
                exclusive_json(operation_dir / "failed.json", failure)
                publish_json("failed-" + request["operation_id"] + ".json", failure)
            except Exception:  # noqa: BLE001, S110 -- keep started/active markers; never print raw secret-bearing errors
                pass
            raise
        finally:
            engine.dispose()
    finally:
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="网页触发的独立新站手动维护；无自动备份或离机配置"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--action", choices=["inspect", *ACTION_OPTIONS], required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            inspect_site(args.project)
            if args.action == "inspect"
            else execute(args.project, args.action)
        )
        print(
            json.dumps(
                {
                    "success": True,
                    "project": args.project,
                    "action": args.action,
                    "status": result.get("status", "completed"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except BaseException as error:  # noqa: BLE001 -- only stable public codes may reach terminal logs
        print(
            json.dumps(
                {
                    "success": False,
                    "project": args.project,
                    "action": args.action,
                    "error_code": error.code
                    if isinstance(error, GuiMaintenanceError)
                    else "maintenance_failed_preserve_evidence",
                    **({"message": INPUT_MESSAGES[error.code]}
                       if isinstance(error, GuiMaintenanceError)
                       and error.code in INPUT_MESSAGES else {}),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
