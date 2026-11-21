"""One-shot GUI installation into an explicitly NEW private MySQL instance.

This maintenance entry runs as root in the fixed API image. It has no Docker API
and uses only the new project's shared Unix socket. It never repairs, resumes,
resets, drops, or restores a database. A completed re-entry verifies read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

SITE = Path("/work/site")
ASSETS = Path("/opt/gift-ledger-release")
BACKEND = Path("/app/backend")
APP_DATA = Path("/var/lib/gift-ledger")
SOCKET = Path("/var/run/mysqld/mysqld.sock")
DATABASE = "gift_ledger_v2"
APPLICATION_USER = "gift_ledger_app"
REVISION = "schema_v2_s011"
FORMAT = "gift-ledger-gui-initialize-v1"
SYSTEM_SCHEMAS = {"mysql", "sys", "performance_schema", "information_schema"}
ASSETS_MANIFEST_SHA256 = "1ac62e84e60851b4a328324f5717019c257abe93f6c34c9a3e630bc82dcf3500"
ASSET_DIGESTS = {
    "schema_v2_s004.sql": "409df42d6fb9246501fc0eff2162785e160a65e110d1eb116bbf67de9c1ff551",
    "schema_v2_s010.sql": "b065e345b5e644dc1ce943b24e2940c9c40f1ae663dbe2608a5812f8e96a5c79",
    "schema_v2_s011.sql": "2b0d32ab70f0dd6defb5ac963396498cca228be2ca78df19ca17a6c774558f77",
    "verify_schema.py": "485b79611d0e663cde5adae77b6eb81e7b16461f94554c7c86ff413fe4264956",
}
MIGRATION_DIGESTS = {
    "schema_v2_s004_baseline.py": (
        "c3228726820dd4c95a81b80e326864624e65aae7804bf83bd870dc113ee1e62e"
    ),
    "schema_v2_s005_contact_identity.py": (
        "5354de531dd3a754520c9ad181924e1cfd4a97fb8d559c76c84b94c89850768f"
    ),
    "schema_v2_s006_system_dictionaries.py": (
        "292bc6bd187e80d653ad44e16d7c8d167e3719021c221d7c5d3c9bd37931995e"
    ),
    "schema_v2_s007_contact_identity_v2.py": (
        "c4d3c64e3225234d6863ac720a66216ee30478632f8c64d8e390731ab8a32701"
    ),
    "schema_v2_s008_event_identity.py": (
        "0cf924bfb4bea1bf0fdace3d0534e3d0c566ef27ec417f372164d0ad4cdec617"
    ),
    "schema_v2_s009_dictionary_labels.py": (
        "09dd5471f8e7d69498f33401b427a927b318caabccec8361223b36767b60ae9e"
    ),
    "schema_v2_s010_legacy_date_precision.py": (
        "b198c4a296f11ce1f9cfb610e0979d666a70a65e0333ba0d75e6b57710f1cf09"
    ),
    "schema_v2_s011_grandchildren.py": (
        "de0280d43751819a8540b2c39f1fd8099cb04ba293a195f5b185df93b0dcd331"
    ),
}
SECRET_FILES = {
    "mysql_root_password": ("secrets/mysql-root-password.txt", "0400"),
    "mysql_app_password": ("secrets/mysql-app-password.txt", "0440"),
    "jwt_secret": ("secrets/jwt-secret.txt", "0440"),
}
FILE_MODES = {
    **{relative: mode for relative, mode in SECRET_FILES.values()},
    "certs/fullchain.pem": "0644",
    "certs/privkey.pem": "0600",
    "certs/ca-chain.pem": "0644",
}
APP_DIRECTORIES = ("imports", "personal-restores", "personal-safety-backups", "admin-backups")
INITIALIZE_FILES = {f"initialize-{value}.json" for value in ("started", "failed", "completed")}
PREPARE_FILES = {"identity.json", "prepare-started.json", "prepare-completed.json"}


class InitializationError(RuntimeError):
    """Only fixed, public reason codes are safe to print."""


def require(condition, code):
    if not condition:
        raise InitializationError(code)


def utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def project_allowed(value):
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        from single_yaml_site import project_host
        project_host(value)
        return True
    if ((Path(__file__).absolute().parent.parent / "gui_v2/PORTABLE-DSM.json").exists()
            or Path("/opt/gift-ledger-gui-v2/PORTABLE-DSM.json").exists()
            or isinstance(value, str) and re.fullmatch(r"gift-ledger-dsm-[0-9a-f]{16}", value)):
        candidate = Path(__file__).absolute().parent.parent / "gui_v2" / "portable_dsm_profile.py"
        if not candidate.is_file():
            candidate = Path("/opt/gift-ledger-gui-v2/portable_dsm_profile.py")
        try:
            if candidate.is_symlink() or not candidate.is_file():
                return False
            spec = importlib.util.spec_from_file_location("initialize_portable_dsm_profile", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.load(value)
            return True
        except (OSError, ValueError, ImportError, AttributeError):
            return False
    if isinstance(value, str) and re.fullmatch(r"gift-ledger-site-[0-9a-f]{16}", value):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "operations_v3"))
        sys.path.insert(0, "/opt/gift-ledger-ops")
        from contract_bridge import authorize_target

        try:
            authorize_target(value)
        except (OSError, ValueError):
            return False
        return True
    return (
        value in ("gift-ledger-nas-prod", "gift-ledger-nas-restore-check",
                  "gift-ledger-dsm9-demo-20261026", "gift-ledger-dsm9-restore-20261026")
        or re.fullmatch(r"gift-ledger-gui-test-[0-9a-f]{16}", value) is not None
    )


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def check_no_extended_acl(path):
    require(
        hasattr(os, "listxattr") and hasattr(os, "O_NOFOLLOW"), "private_acl_cannot_be_inspected"
    )
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise InitializationError("private_acl_cannot_be_inspected") from exc
    require(
        not any(
            ("syno" in name.casefold() and "acl" in name.casefold())
            or name in {"system.posix_acl_access", "system.posix_acl_default"}
            for name in names
        ),
        "private_extended_acl_not_allowed",
    )


def check_directory(path, *, private=False, owner=None):
    # Input/control directories must never be links. Fixed OS paths such as
    # /var/run are allowed to resolve through the reviewed image's /run symlink.
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), "directory_type_invalid")
    if private:
        require(stat.S_IMODE(info.st_mode) == 0o700, "directory_permissions_invalid")
    if owner is not None:
        require((info.st_uid, info.st_gid) == owner, "directory_owner_invalid")
    if private or owner is not None:
        check_no_extended_acl(path)
    return info


def read_file(path, *, mode=None, private_owner=False, maximum=1024 * 1024):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "file_type_or_link_invalid")
    if mode is not None:
        require(stat.S_IMODE(before.st_mode) == mode, "file_permissions_invalid")
    if private_owner:
        require((before.st_uid, before.st_gid) == (0, 0), "file_owner_invalid")
        check_no_extended_acl(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        require(
            (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino),
            "file_identity_changed",
        )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        require(len(value) <= maximum, "file_size_invalid")
        after = os.fstat(descriptor)
        require(
            (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "file_changed_during_read",
        )
    finally:
        os.close(descriptor)
    return value


def json_file(path):
    raw = read_file(path, mode=0o600, private_owner=True, maximum=65536)
    value = json.loads(raw)
    require(isinstance(value, dict), "record_type_invalid")
    return value, sha256(raw)


def exclusive_record(path, value):
    """No replacement, truncate, unlink, or rename can erase failure evidence."""
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return sha256(raw)


def validate_prepare(site, project, secret_dir):
    require(project_allowed(project), "project_not_allowed")
    check_directory(site, private=True, owner=(0, 0))
    for name in ("secrets", "certs", "control"):
        check_directory(site / name, private=True, owner=(0, 0))
    require(
        {entry.name for entry in site.iterdir()} == {"secrets", "certs", "control"},
        "unknown_site_entries",
    )
    control = site / "control"
    found = {entry.name for entry in control.iterdir()}
    require(
        PREPARE_FILES <= found <= PREPARE_FILES | INITIALIZE_FILES,
        "unknown_or_incomplete_control_entries",
    )
    identity, identity_hash = json_file(control / "identity.json")
    require(
        set(identity)
        == {
            "format",
            "project",
            "instance_id",
            "app_version",
            "schema_revision",
            "created_at",
            "tls",
            "files",
        },
        "identity_fields_invalid",
    )
    require(
        identity["format"] == "gift-ledger-gui-site-v1" and identity["project"] == project,
        "prepared_project_mismatch",
    )
    require(canonical_uuid(identity["instance_id"]), "prepared_instance_id_invalid")
    require(
        identity["app_version"] == "v1.5.0" and identity["schema_revision"] == REVISION,
        "prepared_release_mismatch",
    )
    require(
        isinstance(identity["files"], dict) and set(identity["files"]) == set(FILE_MODES),
        "prepared_files_invalid",
    )
    for directory in ("secrets", "certs"):
        require(
            {entry.name for entry in (site / directory).iterdir()}
            == {Path(name).name for name in FILE_MODES if name.startswith(directory + "/")},
            "unknown_prepared_files",
        )
    for relative, mode in FILE_MODES.items():
        metadata = identity["files"][relative]
        require(set(metadata) == {"sha256", "mode", "uid", "gid"}, "prepared_file_metadata_invalid")
        require(
            metadata["mode"] == mode
            and metadata["uid"] == 0
            and metadata["gid"] == 0
            and re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is not None,
            "prepared_file_metadata_invalid",
        )
        raw = read_file(site / relative, mode=int(mode, 8), private_owner=True)
        require(sha256(raw) == metadata["sha256"], "prepared_file_digest_mismatch")
    started, started_hash = json_file(control / "prepare-started.json")
    completed, _ = json_file(control / "prepare-completed.json")
    require(
        set(started) == {"format", "project", "instance_id", "status", "started_at"},
        "prepare_started_fields_invalid",
    )
    require(
        set(completed)
        == {
            "format",
            "project",
            "instance_id",
            "status",
            "started_at",
            "completed_at",
            "identity_sha256",
            "started_sha256",
        },
        "prepare_completed_fields_invalid",
    )
    for record, status in ((started, "started"), (completed, "completed")):
        require(
            record["format"] == "gift-ledger-gui-prepare-v1"
            and record["project"] == project
            and record["instance_id"] == identity["instance_id"]
            and record["status"] == status,
            "prepare_record_identity_mismatch",
        )
    require(
        completed["identity_sha256"] == identity_hash
        and completed["started_sha256"] == started_hash
        and completed["started_at"] == started["started_at"],
        "prepare_record_digest_mismatch",
    )
    # Share the GUI preparation tool's read-only certificate/path/time contract.
    # This entry never calls prepare(), and therefore never regenerates inputs.
    spec = importlib.util.spec_from_file_location(
        "gift_ledger_gui_preparation_verifier", Path(__file__).parent / "prepare.py"
    )
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    preparation.verify_prepared(site, project)
    values = {}
    for name, (relative, mode) in SECRET_FILES.items():
        raw = read_file(secret_dir / name, mode=int(mode, 8), private_owner=True, maximum=65)
        require(
            sha256(raw) == identity["files"][relative]["sha256"], "mounted_secret_digest_mismatch"
        )
        require(re.fullmatch(rb"[0-9a-f]{64}\n?", raw) is not None, "secret_format_invalid")
        values[name] = raw.decode("ascii").rstrip("\n")
    require(len(set(values.values())) == 3, "duplicate_secrets")
    return identity, identity_hash, values


def validate_assets(assets, backend):
    check_directory(assets)
    manifest = read_file(assets / "SHA256SUMS")
    require(sha256(manifest) == ASSETS_MANIFEST_SHA256, "assets_manifest_digest_mismatch")
    require(
        manifest.decode("ascii").splitlines()
        == [f"{digest}  {name}" for name, digest in ASSET_DIGESTS.items()],
        "assets_manifest_contract_mismatch",
    )
    for name, expected in ASSET_DIGESTS.items():
        require(sha256(read_file(assets / name)) == expected, "asset_digest_mismatch")
    check_directory(backend)
    migrations = backend / "migrations" / "versions"
    check_directory(migrations)
    require(
        {entry.name for entry in migrations.glob("*.py")} == set(MIGRATION_DIGESTS),
        "migration_chain_files_mismatch",
    )
    for name, expected in MIGRATION_DIGESTS.items():
        require(
            sha256(read_file(migrations / name).replace(b"\r\n", b"\n")) == expected,
            "migration_digest_mismatch",
        )
    require(
        (backend / "alembic.ini").is_file() and not (backend / "alembic.ini").is_symlink(),
        "alembic_configuration_missing",
    )


def load_verifier(assets):
    spec = importlib.util.spec_from_file_location(
        "gift_ledger_gui_frozen_verifier", assets / "verify_schema.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_sql(sql):
    """Split the digest-locked baseline without changing quoted semicolons."""
    statements, current, quote, index, line_comment = [], [], None, 0, False
    while index < len(sql):
        char = sql[index]
        if line_comment:
            if char == "\n":
                line_comment = False
                current.append("\n")
        elif quote:
            current.append(char)
            if char == "\\" and quote != "`" and index + 1 < len(sql):
                index += 1
                current.append(sql[index])
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                    current.append(sql[index])
                else:
                    quote = None
        elif char in ("'", '"', "`"):
            quote = char
            current.append(char)
        elif sql[index : index + 2] == "--" and (index + 2 == len(sql) or sql[index + 2].isspace()):
            line_comment = True
            index += 1
        elif char == ";":
            if "".join(current).strip():
                statements.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    require(quote is None, "baseline_sql_quoting_invalid")
    require(not "".join(current).strip(), "baseline_sql_unterminated")
    return statements


def check_app_data(path, *, empty):
    check_directory(path)
    if empty:
        require(not any(path.iterdir()), "app_data_not_empty")
    else:
        check_directory(path, private=True, owner=(10001, 0))
        for name in APP_DIRECTORIES:
            check_directory(path / name, private=True, owner=(10001, 0))


def initialize_app_data(path):
    check_app_data(path, empty=True)
    os.chown(path, 10001, 0)
    os.chmod(path, 0o700)
    for name in APP_DIRECTORIES:
        child = path / name
        child.mkdir(mode=0o700)
        os.chown(child, 10001, 0)


def existing_state(control, project, instance, identity_hash):
    found = {
        name
        for name in INITIALIZE_FILES
        if (control / name).exists() or (control / name).is_symlink()
    }
    if not found:
        return None
    require(
        found == {"initialize-started.json", "initialize-completed.json"},
        "previous_initialization_incomplete_or_failed",
    )
    started, start_hash = json_file(control / "initialize-started.json")
    completed, _ = json_file(control / "initialize-completed.json")
    start_keys = {
        "format",
        "project",
        "instance_id",
        "status",
        "started_at",
        "server_uuid",
        "identity_sha256",
        "assets_sha256",
    }
    require(
        set(started) == start_keys
        and set(completed)
        == start_keys
        | {"completed_at", "started_sha256", "schema_revision", "migration_chain", "verification"},
        "initialize_record_fields_invalid",
    )
    for record, status in ((started, "started"), (completed, "completed")):
        require(
            record["format"] == FORMAT
            and record["project"] == project
            and record["instance_id"] == instance
            and record["status"] == status
            and record["identity_sha256"] == identity_hash
            and record["assets_sha256"] == ASSETS_MANIFEST_SHA256
            and canonical_uuid(record["server_uuid"]),
            "initialize_record_identity_mismatch",
        )
    require(
        completed["server_uuid"] == started["server_uuid"]
        and completed["started_at"] == started["started_at"]
        and completed["started_sha256"] == start_hash,
        "initialize_record_digest_mismatch",
    )
    require(
        completed["schema_revision"] == REVISION
        and completed["migration_chain"] == [f"schema_v2_s{number:03d}" for number in range(4, 12)]
        and completed["verification"] == "full-schema-and-empty-business",
        "initialize_completion_contract_invalid",
    )
    return completed


def socket_engine(username, password, *, database=None):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from sqlalchemy.pool import NullPool

    # No hostname for root. PyMySQL unix_socket takes precedence over the app
    # URL's canonical hostname; neither connection opens a TCP socket.
    url = URL.create(
        "mysql+pymysql",
        username=username,
        password=password,
        database=database,
        query={"unix_socket": str(SOCKET), "charset": "utf8mb4"},
    )
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
        echo=False,
        hide_parameters=True,
    )


def server_identity(root_engine, *, fresh):
    with root_engine.connect() as connection:
        server_uuid, version = connection.exec_driver_sql("SELECT @@server_uuid, VERSION()").one()
        require(
            canonical_uuid(server_uuid) and version.startswith("8.4."),
            "database_identity_or_version_invalid",
        )
        require(
            connection.exec_driver_sql("SELECT CURRENT_USER()").scalar_one() == "root@localhost",
            "root_must_be_local_socket",
        )
        schemas = set(
            connection.exec_driver_sql(
                "SELECT schema_name FROM information_schema.schemata"
            ).scalars()
        )
        expected = SYSTEM_SCHEMAS if fresh else SYSTEM_SCHEMAS | {DATABASE}
        if not fresh and os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
            schemas.discard("gift_ledger_registration")
        require(
            schemas == expected,
            "database_namespace_not_fresh" if fresh else "database_namespace_changed",
        )
        users = {
            tuple(row) for row in connection.exec_driver_sql("SELECT User,Host FROM mysql.user")
        }
        allowed = {
            ("root", "localhost"),
            ("mysql.infoschema", "localhost"),
            ("mysql.session", "localhost"),
            ("mysql.sys", "localhost"),
        }
        if not fresh:
            allowed.add((APPLICATION_USER, "%"))
        require(users == allowed, "database_account_namespace_invalid")
    return server_uuid


def verify_database(
    app_engine,
    verifier,
    assets,
    revision,
    server_uuid,
    *,
    empty=False,
    unstamped=False,
    version_only=False,
):
    contract_revision = REVISION if version_only else revision
    contract = verifier.schema_contract(
        read_file(assets / f"{contract_revision}.sql").decode("utf-8")
    )
    with app_engine.connect() as connection:
        connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        snapshot = verifier.read_snapshot(connection)
        verifier.verify_snapshot(
            snapshot,
            contract,
            revision=revision,
            expected_server_uuid=server_uuid,
            expect_empty=empty,
            unstamped=unstamped,
            version_only=version_only,
        )
    return {
        "revision": revision,
        "server_uuid": server_uuid,
        "table_count": len(snapshot["tables"]),
        "verification": "version-and-empty" if version_only else "full-schema-contract",
    }


def prepare_migration_environment(secrets, verifier, identity=None):
    from app.core.config import Settings
    from sqlalchemy.engine import URL

    url = URL.create(
        "mysql+pymysql",
        username=APPLICATION_USER,
        password=secrets["mysql_app_password"],
        host="db",
        port=3306,
        database=DATABASE,
        query={"charset": "utf8mb4", "unix_socket": str(SOCKET)},
    )
    os.environ["GIFT_LEDGER_DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["GIFT_LEDGER_JWT_SECRET"] = secrets["jwt_secret"]
    settings = Settings(_env_file=None)
    verifier.verify_settings(settings)
    require(
        settings.trusted_origin_list
        and all(origin.startswith("https://") for origin in settings.trusted_origin_list),
        "runtime_origin_invalid",
    )
    if identity is not None:
        from urllib.parse import urlsplit

        require(
            len(settings.trusted_origin_list) == 1
            and urlsplit(settings.trusted_origin_list[0]).hostname == identity["tls"]["host"],
            "prepared_tls_runtime_origin_mismatch",
        )
    return settings


def run_alembic(backend, operation, revision):
    from alembic import command
    from alembic.config import Config

    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    if operation == "stamp":
        command.stamp(config, revision)
    else:
        require(operation == "upgrade", "migration_operation_invalid")
        command.upgrade(config, revision)


def provision_registration(root_engine, legacy_open):
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        from app.db.registration import provision

        provision(root_engine, legacy_open)


def run(
    project,
    *,
    site=SITE,
    assets=ASSETS,
    backend=BACKEND,
    app_data=APP_DATA,
    secret_dir=Path("/run/secrets"),
):
    require(os.geteuid() == 0, "initialization_requires_root_utility")
    require(sys.version_info[:2] == (3, 12), "initialization_requires_fixed_python_312")
    identity, identity_hash, secrets = validate_prepare(site, project, secret_dir)
    validate_assets(assets, backend)
    control = site / "control"
    previous = existing_state(control, project, identity["instance_id"], identity_hash)
    check_app_data(app_data, empty=previous is None)
    socket_info = SOCKET.lstat()
    require(
        stat.S_ISSOCK(socket_info.st_mode) and socket_info.st_nlink == 1, "mysql_socket_invalid"
    )
    check_directory(SOCKET.parent)
    sys.path.insert(0, str(backend))
    verifier = load_verifier(assets)
    settings = prepare_migration_environment(secrets, verifier, identity)
    root_engine = socket_engine("root", secrets["mysql_root_password"])
    app_engine = socket_engine(APPLICATION_USER, secrets["mysql_app_password"], database=DATABASE)
    claimed = False
    phase = "preflight"
    base_record = None
    try:
        server_uuid = server_identity(root_engine, fresh=previous is None)
        if previous is not None:
            require(server_uuid == previous["server_uuid"], "completed_server_uuid_mismatch")
            # Business records and user dictionaries may now exist. Never rerun
            # zero-data initialization, migrations, ownership changes, or seeds.
            result = verify_database(app_engine, verifier, assets, REVISION, server_uuid)
            provision_registration(root_engine, settings.registration_enabled)
            print(
                json.dumps(
                    {
                        "status": "completed-readonly-verified",
                        "project": project,
                        "instance_id": identity["instance_id"],
                        **result,
                    }
                ),
                flush=True,
            )
            return 0
        base_record = {
            "format": FORMAT,
            "project": project,
            "instance_id": identity["instance_id"],
            "started_at": utc_now(),
            "server_uuid": server_uuid,
            "identity_sha256": identity_hash,
            "assets_sha256": ASSETS_MANIFEST_SHA256,
        }
        started_hash = exclusive_record(
            control / "initialize-started.json", {**base_record, "status": "started"}
        )
        claimed = True
        print(
            json.dumps(
                {
                    "status": "started",
                    "project": project,
                    "instance_id": identity["instance_id"],
                    "server_uuid": server_uuid,
                }
            ),
            flush=True,
        )
        # Re-check immediately before irreversible DDL, after taking the permanent
        # exclusive started marker. CREATE DATABASE has no IF NOT EXISTS.
        require(
            server_identity(root_engine, fresh=True) == server_uuid,
            "server_uuid_changed_before_ddl",
        )
        phase = "s004-baseline"
        with root_engine.connect() as connection:
            for statement in split_sql(read_file(assets / "schema_v2_s004.sql").decode("utf-8")):
                connection.exec_driver_sql(statement)
            connection.commit()
            # Same protected app schema grant as the reviewed create-app-user.sh.
            # No global privileges, root TCP account, grant option, or other DB.
            connection.exec_driver_sql(
                "CREATE USER 'gift_ledger_app'@%s IDENTIFIED BY %s",
                ("%", secrets["mysql_app_password"]),
            )
            connection.exec_driver_sql(
                "GRANT ALL PRIVILEGES ON gift_ledger_v2.* TO 'gift_ledger_app'@%s", ("%",)
            )
            connection.commit()
        phase = "app-volume"
        initialize_app_data(app_data)
        phase = "s004-verify-before-stamp"
        verify_database(
            app_engine, verifier, assets, "schema_v2_s004", server_uuid, empty=True, unstamped=True
        )
        phase = "s004-stamp"
        run_alembic(backend, "stamp", "schema_v2_s004")
        verify_database(app_engine, verifier, assets, "schema_v2_s004", server_uuid, empty=True)
        for number in range(5, 12):
            revision = f"schema_v2_s{number:03d}"
            phase = revision
            run_alembic(backend, "upgrade", revision)
            result = verify_database(
                app_engine,
                verifier,
                assets,
                revision,
                server_uuid,
                empty=True,
                version_only=number < 10,
            )
            print(json.dumps({"status": "revision-verified", **result}), flush=True)
        phase = "complete"
        exclusive_record(
            control / "initialize-completed.json",
            {
                **base_record,
                "status": "completed",
                "completed_at": utc_now(),
                "started_sha256": started_hash,
                "schema_revision": REVISION,
                "migration_chain": [f"schema_v2_s{number:03d}" for number in range(4, 12)],
                "verification": "full-schema-and-empty-business",
            },
        )
        claimed = False
        provision_registration(root_engine, settings.registration_enabled)
        print(
            json.dumps(
                {
                    "status": "completed",
                    "project": project,
                    "instance_id": identity["instance_id"],
                    "server_uuid": server_uuid,
                    "schema_revision": REVISION,
                    "verification": "full-schema-and-empty-business",
                }
            ),
            flush=True,
        )
        return 0
    except BaseException:  # Persist every failure without printing credential-bearing exceptions.
        if claimed:
            # Preserve the durable started record even if failure recording itself
            # is impossible. Subsequent starts still refuse to execute any writes.
            try:
                exclusive_record(
                    control / "initialize-failed.json",
                    {
                        **base_record,
                        "status": "failed",
                        "failed_at": utc_now(),
                        "phase": phase,
                        "error_code": "initialization_stopped_preserve_evidence",
                    },
                )
            except Exception:  # noqa: BLE001, S110 -- preserve started marker; never log raw secret-bearing failures
                pass
        raise
    finally:
        root_engine.dispose()
        app_engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="网页触发的独立全新库初始化；失败永久保留，完成重启仅只读核验"
    )
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.project)
    except BaseException as exc:  # noqa: BLE001 -- fixed public codes only; never log credentials
        # Driver/Pydantic/Alembic exceptions can contain credential-bearing URLs.
        # Only public fixed reason codes are emitted; no repr/traceback/raw SQL.
        code = (
            str(exc)
            if isinstance(exc, InitializationError)
            else "initialization_stopped_preserve_evidence"
        )
        print(
            json.dumps({"status": "failed", "error_code": code}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
