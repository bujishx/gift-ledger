"""Synthetic data for the explicitly guarded, isolated maintenance rehearsal.

This helper is not an application feature. It never clears existing data, and it
refuses to seed a database that is not empty or not identified as the source of
the current rehearsal. The test password comes from a separately mounted secret.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.security import validate_new_password
from app.db.base import Base
from app.db.models import EventRecord, GiftRecord, LegacyImportBatch
from app.schemas.dictionaries import DictionaryCategory
from app.services.auth import register_user
from app.services.contacts import create_contact, set_contact_active
from app.services.database_restore import structural_schema_differences
from app.services.dictionaries import create_dictionary_item
from app.services.event_dates import event_date_fields, parse_event_date
from app.services.ledger import create_ledger_entry
from app.services.personal_backup import build_personal_backup, parse_personal_backup
from app.services.personal_safety_backup_store import PersonalSafetyBackupStore
from app.services.profile import create_self_profile
from maintenance.database import guarded_engine, load_config, require_guard

USERNAMES = ("phase2_owner", "phase2_other")
EXPECTED_USERS = {
    "phase2_owner": {
        "display_name": "演练本人甲",
        "contact_count": 3,
        "event_count": 6,
        "gift_count": 6,
        "income_amount": "680.00",
        "expense_amount": "200.00",
        "pure_goods_count": 1,
        "custom_dictionaries": {
            "relationship": ["演练甲自定义关系"],
            "kinship": ["演练甲自定义称谓"],
            "event": ["演练甲自定义事件"],
        },
    },
    "phase2_other": {
        "display_name": "演练本人乙",
        "contact_count": 2,
        "event_count": 1,
        "gift_count": 1,
        "income_amount": "100.00",
        "expense_amount": "0.00",
        "pure_goods_count": 0,
        "custom_dictionaries": {
            "relationship": ["演练乙自定义关系"],
            "kinship": [],
            "event": [],
        },
    },
}
BUSINESS_TABLES = (
    "users", "contacts", "event_records", "gift_records", "legacy_import_batches",
)
ALL_TABLES = (
    *BUSINESS_TABLES, "relationship_types", "kinship_types", "event_types", "alembic_version",
)


def read_test_password(path: Path) -> str:
    """Only the specifically mounted synthetic account secret is accepted."""
    if str(path) != "/run/secrets/synthetic_user_password" or path.is_symlink():
        raise ValueError("The synthetic password must use the dedicated test secret")
    if not path.is_file() or not 8 <= path.stat().st_size <= 512:
        raise ValueError("The synthetic password secret is missing or invalid")
    password = path.read_text(encoding="utf-8").strip()
    validate_new_password(password)
    return password


def work_file(path: Path, work_root: Path) -> Path:
    """Keep references and evidence inside the mounted rehearsal work directory."""
    resolved_root = work_root.resolve(strict=True)
    resolved = path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ValueError("Evidence paths must stay inside the rehearsal work directory")
    if path.is_symlink():
        raise ValueError("Evidence paths cannot be symbolic links")
    return resolved


def write_new_result(path: Path, work_root: Path, result: dict) -> None:
    destination = work_file(path, work_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also prevents an old successful report being overwritten.
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        os.chmod(destination, 0o600)
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _seed_accounts(session, password: str) -> int:
    owner = register_user(session, USERNAMES[0], password)
    self_owner = create_self_profile(session, owner.id, "演练本人甲", "合成测试区")
    custom = {
        category: create_dictionary_item(
            session, category, owner.id,
            EXPECTED_USERS[USERNAMES[0]]["custom_dictionaries"][category.value][0],
        )
        for category in DictionaryCategory
    }
    ordinary = create_contact(
        session, owner.id, name="演练联系人甲",
        relationship_type_id=custom[DictionaryCategory.RELATIONSHIP].id,
        location="合成测试区", phone=None, remark="完全合成资料",
    )
    inactive = create_contact(
        session, owner.id, name="演练停用联系人", relationship_type_id=2,
        location=None, phone=None, remark="检查停用状态能否恢复",
    )
    create_ledger_entry(
        session, owner.id, host_contact_id=ordinary.id, kinship_type_id=1,
        event_type_id=1, event_date=date(2024, 6, 1), event_remark="演练支出",
        giver_contact_id=None, amount=Decimal("200.00"), gift_details=None,
        gift_remark="合成支出",
    )
    create_ledger_entry(
        session, owner.id, host_contact_id=self_owner.id,
        kinship_type_id=custom[DictionaryCategory.KINSHIP].id,
        event_type_id=custom[DictionaryCategory.EVENT].id,
        event_date=date(2024, 5, 20), event_remark="演练收入",
        giver_contact_id=ordinary.id, amount=Decimal("600.00"), gift_details=None,
        gift_remark="合成收入",
    )
    create_ledger_entry(
        session, owner.id, host_contact_id=self_owner.id, kinship_type_id=1,
        event_type_id=2, event_date=date(2024, 7, 1), event_remark="演练纯实物",
        giver_contact_id=ordinary.id, amount=Decimal("0.00"),
        gift_details="合成测试苹果一箱", gift_remark="零元实物也有往来",
    )
    set_contact_active(session, owner.id, inactive.id, is_active=False)
    # The normal web service correctly refuses partial dates. As in the existing
    # MySQL backup fixture, only the isolated legacy representatives use the ORM.
    for value, remark, amount in (
        ("2010", "演练旧账仅知年份", "50.00"),
        ("2011-03", "演练旧账仅知年月", "25.00"),
        ("不详", "演练旧账日期不详", "5.00"),
    ):
        legacy_event = EventRecord(
            user_id=owner.id, contact_id=self_owner.id, relative_type_id=1,
            event_type_id=1, remark=remark, **event_date_fields(parse_event_date(value)),
        )
        session.add(legacy_event)
        session.flush()
        session.add(GiftRecord(
            user_id=owner.id, event_record_id=legacy_event.id, contact_id=ordinary.id,
            amount=Decimal(amount), remark="合成旧账",
        ))
    session.add(LegacyImportBatch(
        user_id=owner.id, batch_id="b" * 64,
        result_json='{"created_gifts":3,"synthetic_rehearsal":true}',
    ))
    session.commit()

    other = register_user(session, USERNAMES[1], password)
    self_other = create_self_profile(session, other.id, "演练本人乙", "独立合成测试区")
    other_relationship = create_dictionary_item(
        session, DictionaryCategory.RELATIONSHIP, other.id, "演练乙自定义关系",
    )
    other_contact = create_contact(
        session, other.id, name="演练联系人乙",
        relationship_type_id=other_relationship.id, location=None, phone=None,
        remark="用于验证两用户隔离",
    )
    create_ledger_entry(
        session, other.id, host_contact_id=self_other.id, kinship_type_id=1,
        event_type_id=1, event_date=date(2023, 1, 1), event_remark="演练乙独立事件",
        giver_contact_id=other_contact.id, amount=Decimal("100.00"),
        gift_details=None, gift_remark="乙独立收入",
    )
    return owner.id


def _initialize_version(engine) -> str:
    """Register the current version only for a guarded, already-empty source."""
    migration_config = Config()
    migration_config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    scripts = ScriptDirectory.from_config(migration_config)
    if scripts.get_current_head() != "schema_v2_s011":
        raise ValueError("This rehearsal helper requires the S011 application schema")
    with engine.begin() as connection:
        if "alembic_version" in inspect(connection).get_table_names():
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if revision != "schema_v2_s011":
                raise ValueError("An existing schema version must never be restamped")
        else:
            MigrationContext.configure(connection).stamp(scripts, "schema_v2_s011")
        migration_context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        if structural_schema_differences(compare_metadata(migration_context, Base.metadata)):
            raise ValueError("The initialized source differs from the S011 application schema")
    return "schema_v2_s011"


def _prepare_source_app_volume(root: Path) -> None:
    """Fix only the fresh, fixed synthetic mount, before any files are created."""
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise ValueError("Preparing the synthetic source volume requires maintenance root")
    if root.as_posix() != "/source-app-data" or root.is_symlink() or not root.is_dir():
        raise ValueError("Only the fixed isolated source application mount is permitted")
    if any(root.iterdir()):
        raise ValueError("Existing source application files must never be changed or replaced")
    # A volume first mounted at this maintenance-only path does not inherit
    # /var/lib/gift-ledger's existing application UID from the unchanged API image.
    os.chown(root, 10001, 0)
    os.chmod(root, 0o700)


def _create_app_file_sample(factory, owner_id: int) -> dict:
    root = Path("/source-app-data")
    _prepare_source_app_volume(root)
    directory = root / "personal-safety-backups"
    if directory.exists() and (directory.is_symlink() or any(directory.iterdir())):
        raise ValueError("Existing personal safety backups must never be replaced")
    with factory() as session:
        backup = build_personal_backup(session, owner_id, application_version="1.5.0")
    parse_personal_backup(backup.content)
    store = PersonalSafetyBackupStore(directory)
    token, _expires = store.save(user_id=owner_id, content=backup.content)
    if store.load(token, user_id=owner_id).content != backup.content:
        raise ValueError("The synthetic personal safety backup did not read back correctly")
    files = sorted(directory.iterdir())
    if len(files) != 2:
        raise ValueError("Unexpected synthetic personal safety backup files")
    # Match the existing application UID; metadata tokens are never printed.
    os.chown(directory, 10001, 0)
    for item in files:
        os.chown(item, 10001, 0)
    return {
        "category": "personal-safety-backups", "file_count": len(files),
        "byte_count": sum(item.stat().st_size for item in files),
        "content_sha256": sha256(backup.content).hexdigest(),
        "readback_verified": True,
        "source_volume_application_uid": 10001,
        "source_volume_private_mode": "0700",
    }


def seed_rehearsal(config, password_file: Path) -> dict:
    if (config.role, config.host, config.user) != ("source", "source-db", "root"):
        raise ValueError("Seeding is restricted to the isolated source root configuration")
    password = read_test_password(password_file)
    with guarded_engine(config, "gift_ledger_v2") as engine:
        with engine.connect() as connection:
            for table in BUSINESS_TABLES:
                if connection.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one() != 0:
                    raise ValueError("Existing business data must never be cleared or reseeded")
            for table in ("relationship_types", "kinship_types", "event_types"):
                if connection.execute(text(
                    f"SELECT COUNT(*) FROM `{table}` WHERE user_id IS NOT NULL"
                )).scalar_one() != 0:
                    raise ValueError("Existing custom dictionaries must never be overwritten")
        require_guard(engine, config)
        revision = _initialize_version(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with factory() as session:
            owner_id = _seed_accounts(session, password)
        require_guard(engine, config)
        app_files = _create_app_file_sample(factory, owner_id)
        require_guard(engine, config)
        with engine.connect() as connection:
            counts = {
                table: int(connection.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one())
                for table in ALL_TABLES
            }
    for table, count in {"users": 2, "contacts": 5, "event_records": 7,
                         "gift_records": 7, "legacy_import_batches": 1}.items():
        if counts[table] != count:
            raise ValueError("Synthetic data generation did not meet its expected counts")
    return {
        "format": "gift-ledger-synthetic-rehearsal-data", "version": 1,
        "run_id": config.run_id, "schema_revision": revision,
        "synthetic_only": True, "counts": counts, "expected_users": EXPECTED_USERS,
        "app_files": app_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the guarded synthetic maintenance source")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result_path = work_file(args.result, Path(config.work_root))
        if result_path.exists():
            raise ValueError("An existing result cannot be overwritten")
        result = seed_rehearsal(config, args.password_file)
        write_new_result(result_path, Path(config.work_root), result)
    except Exception as exc:
        # Never print native database errors or supplied credentials.
        print(json.dumps({"success": False, "operation": "synthetic-seed",
                          "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"success": True, "operation": "synthetic-seed",
                      "run_id": config.run_id, "counts": result["counts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
