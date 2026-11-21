"""Actual service transactions on two SQLite sessions, without production data."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.base import Base
from app.db.models import Contact, EventRecord, GiftRecord, RelationshipType, User
from app.db.registration import DATABASE, personal_restore_receipts
from app.services.event_dates import event_date_fields, parse_event_date
from app.services.ledger import LedgerValidationError
from app.services.ledger_records import event_revision, gift_revision, update_event, update_gift
from app.services.personal_backup import (
    PersonalBackupError,
    PersonalBackupLimits,
    build_personal_backup,
    parse_personal_backup,
)
from app.services.personal_restore import (
    PersonalRestoreError,
    analyze_personal_restore,
    recent_restore_receipts,
    replace_personal_ledger,
    verify_restore_password,
)
from app.services.personal_restore_preview_store import (
    PersonalRestorePreviewStore,
    PersonalRestorePreviewStoreError,
)
from app.services.personal_safety_backup_store import PersonalSafetyBackupStore

PASSWORD = "a long test passphrase"


@pytest.fixture
def site(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}").execution_options(
        schema_translate_map={DATABASE: None},
    )
    # Mirror the production column names and types used by ORM reads and writes.
    with engine.begin() as db:
        for table in Base.metadata.sorted_tables:
            columns = []
            for col in table.columns:
                if col.name == "id":
                    columns.append("id INTEGER PRIMARY KEY AUTOINCREMENT")
                    continue
                typename = {int: "INTEGER", bytes: "BLOB", bool: "INTEGER", Decimal: "NUMERIC"}.get(
                    col.type.python_type,
                    "TEXT",
                )
                columns.append(f"`{col.name}` {typename}")
            db.exec_driver_sql(f"CREATE TABLE {table.name} ({','.join(columns)})")
        personal_restore_receipts.create(db)
        for table, values in (
            (
                "relationship_types",
                ((1, "我", "relationship.self"), (2, "朋友", "relationship.friend")),
            ),
            ("kinship_types", ((1, "本人", "kinship.self"),)),
            ("event_types", ((1, "生日", "event.birthday"),)),
        ):
            db.exec_driver_sql(
                f"INSERT INTO {table} (id,type_name,system_code) VALUES (?,?,?)", list(values)
            )
    with Session(engine) as db:
        stamp = datetime(2024, 1, 1)
        encoded = hash_password(PASSWORD)
        for uid in (1, 2):
            db.add(
                User(
                    id=uid,
                    username=f"user{uid}",
                    created_at=stamp,
                    password_hash=encoded,
                    is_active=1,
                    is_admin=0,
                )
            )
            db.add(
                Contact(
                    id=uid,
                    user_id=uid,
                    name="本人",
                    relation_type_id=1,
                    is_active=1,
                    created_at=stamp,
                )
            )
        db.add(
            Contact(id=3, user_id=1, name="朋友", relation_type_id=2, is_active=1, created_at=stamp)
        )
        db.add(
            EventRecord(
                id=1,
                user_id=1,
                contact_id=1,
                relative_type_id=1,
                event_type_id=1,
                created_at=stamp,
                **event_date_fields(parse_event_date("2024-01-01"), source="web"),
            )
        )
        db.add(
            GiftRecord(
                id=1,
                user_id=1,
                event_record_id=1,
                contact_id=3,
                created_at=stamp,
                amount=Decimal("100.00"),
            )
        )
        db.commit()
    yield engine
    engine.dispose()


def backup(db):
    return build_personal_backup(db, 1, application_version="1.5.0").content


def confirm(db, package, revision, directory, **kwargs):
    return replace_personal_ledger(
        db,
        1,
        package,
        credential=verify_restore_password(db.get(User, 1), PASSWORD),
        expected_revision=revision,
        safety_backup_store=PersonalSafetyBackupStore(directory),
        application_version="1.5.0",
        **kwargs,
    )


def test_two_editors_and_legacy_request_cannot_overwrite_new_amount(site):
    with Session(site) as a, Session(site) as b:
        gift_rev = gift_revision(a.get(GiftRecord, 1))
        event_rev = event_revision(a.get(EventRecord, 1))
        changed = dict(
            giver_contact_id=None,
            gift_details=None,
            remark=None,
            amount=Decimal("200.00"),
            fields={"amount"},
        )
        update_gift(a, 1, 1, **changed, expected_revision=gift_rev)
        for revision, fields in (
            (gift_rev, {"amount"}),
            (gift_rev, {"amount", "remark"}),
            (None, {"remark"}),
        ):
            with pytest.raises(LedgerValidationError, match="旧版页面"):
                update_gift(
                    b,
                    1,
                    1,
                    **(changed | {"amount": Decimal("100.00"), "remark": "草稿", "fields": fields}),
                    expected_revision=revision,
                )
        gift = b.get(GiftRecord, 1)
        assert gift.amount == 200 and gift.remark is None
        event_change = dict(
            host_contact_id=None,
            kinship_type_id=None,
            event_type_id=None,
            event_date=None,
            remark="改正",
            fields={"remark"},
        )
        update_event(a, 1, 1, **event_change, expected_revision=event_rev)
        with pytest.raises(LedgerValidationError, match="旧版页面"):
            update_event(b, 1, 1, **event_change, expected_revision=event_rev)


@pytest.mark.parametrize("kind", ["amount", "dictionary", "new_gift"])
def test_changed_ledger_cancels_before_safety_file_or_db_replacement(site, tmp_path, kind):
    with Session(site) as db:
        source = backup(db)
        revision = analyze_personal_restore(db, 1, source).ledger_revision
    with Session(site) as other:
        if kind == "amount":
            other.get(GiftRecord, 1).amount = Decimal("201.00")
        elif kind == "dictionary":
            other.add(RelationshipType(user_id=1, type_name="新关系", is_active=1, sort_order=1))
        else:
            other.add(
                GiftRecord(
                    user_id=1,
                    event_record_id=1,
                    contact_id=4,
                    amount=Decimal("5.00"),
                    created_at=datetime(2024, 1, 2),
                )
            )
        other.commit()
    with Session(site) as db, pytest.raises(PersonalRestoreError, match="预览后"):
        confirm(db, source, revision, tmp_path / "safety")
    assert not (tmp_path / "safety").exists()
    with Session(site) as db:
        assert recent_restore_receipts(db, db.get(User, 1)) == []
        if kind == "amount":
            assert db.get(GiftRecord, 1).amount == 201


def test_confirmation_response_lost_fresh_session_retrieves_committed_receipt(site, tmp_path):
    with Session(site) as db:
        package = backup(db)
        revision = analyze_personal_restore(db, 1, package).ledger_revision
        store = PersonalRestorePreviewStore(tmp_path / "preview")
        token, _ = store.create(user_id=1, content=package, ledger_revision=revision)
    with Session(site) as db:
        db.get(Contact, 2).remark = "另一个账号的变更"
        db.commit()
    store = PersonalRestorePreviewStore(tmp_path / "preview")
    claimed = store.claim(token, user_id=1)
    with Session(site) as db:
        # Intentionally discard the return value after the actual commit.
        confirm(db, claimed.content, claimed.ledger_revision, tmp_path / "safety")
    claimed.discard()
    with pytest.raises(PersonalRestorePreviewStoreError):
        store.claim(token, user_id=1)
    with Session(site) as db:
        receipts = recent_restore_receipts(db, db.get(User, 1))
        assert len(receipts) == 1 and receipts[0]["counts"]["gifts"] == 1
        assert recent_restore_receipts(db, db.get(User, 2)) == []
        assert db.get(Contact, 2).remark == "另一个账号的变更"
        safety = PersonalSafetyBackupStore(tmp_path / "safety").load(
            receipts[0]["safety_backup_token"], user_id=1
        )
        assert parse_personal_backup(safety.content).gifts[0].amount == 100
        db.execute(
            personal_restore_receipts.update().values(
                expires_at=int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
            )
        )
        db.commit()
        assert recent_restore_receipts(db, db.get(User, 1))[0]["safety_backup_token"] is None


def test_current_restore_limits_apply_to_export_safety_backup_and_old_packages(site, tmp_path):
    with Session(site) as db:
        old = backup(db)
        # The pre-change v3 format and old version metadata remain readable.
        with ZipFile(BytesIO(old)) as src:
            contents = {name: src.read(name) for name in src.namelist()}
        manifest = json.loads(contents["manifest.json"])
        manifest["schema_revision"] = "schema_v2_s010"
        contents["manifest.json"] = json.dumps(manifest).encode()
        buf = BytesIO()
        with ZipFile(buf, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
        assert analyze_personal_restore(db, 1, buf.getvalue()).incoming.gifts == 1
        db.get(Contact, 3).remark = "A" * 30_000
        db.commit()
        with pytest.raises(PersonalBackupError):
            build_personal_backup(
                db,
                1,
                application_version="1.5.0",
                limits=PersonalBackupLimits(max_file_bytes=len(old) - 1),
            )
        revision = analyze_personal_restore(db, 1, old).ledger_revision
        with pytest.raises(PersonalRestoreError, match="安全备份"):
            confirm(
                db,
                old,
                revision,
                tmp_path / "safety",
                limits=PersonalBackupLimits(max_file_bytes=len(old)),
            )
        assert db.get(Contact, 3).remark == "A" * 30_000
        assert recent_restore_receipts(db, db.get(User, 1)) == []


def test_policy_shared_by_migration_export_preview_confirm_and_safety(site, tmp_path):
    with Session(site) as db:
        for row in (
            db.get(Contact, 3),
            db.get(Contact, 1),
            db.get(EventRecord, 1),
            db.get(GiftRecord, 1),
        ):
            row.remark = "A" * 60_000
        db.commit()
        normal = backup(db)
        # Recreate a valid old ZIP that used ordinary DEFLATE even on repetitive data.
        with ZipFile(BytesIO(normal)) as src:
            original = {item: src.read(item) for item in src.namelist()}
        buf = BytesIO()
        with ZipFile(buf, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in original.items():
                archive.writestr(name, data)
        legacy = buf.getvalue()
        with pytest.raises(PersonalRestoreError, match="压缩比例"):
            analyze_personal_restore(db, 1, legacy)
        assert parse_personal_backup(normal).gifts[0].remark == "A" * 60_000
        tight = PersonalBackupLimits(max_records=2)
        with pytest.raises(PersonalBackupError, match="记录"):
            build_personal_backup(db, 1, application_version="1.5.0", limits=tight)
        with pytest.raises(PersonalRestoreError, match="记录"):
            analyze_personal_restore(db, 1, normal, limits=tight)
        migration = PersonalBackupLimits(max_records=8, max_compression_ratio=1100)
        preview = analyze_personal_restore(db, 1, legacy, limits=migration)
        result = confirm(db, legacy, preview.ledger_revision, tmp_path / "safety", limits=migration)
        assert result.counts.gifts == 1
        assert (
            recent_restore_receipts(db, db.get(User, 1))[0]["safety_backup_token"]
            == result.safety_backup_token
        )


def test_lan_http_receipt_recovery_from_new_login(site, tmp_path):
    from fastapi.testclient import TestClient

    from app.api.dependencies import get_authentication_session, get_database_session
    from app.core.config import Settings
    from app.db.registration import metadata
    from app.main import create_app
    from app.services.registration_limits import password_capacity

    origin = "https://192.0.2.9:16443"
    public = "https://ledger.example.test"
    settings = Settings(
        environment="test",
        trusted_origins=origin,
        reverse_proxy_origin=public,
        jwt_secret="0123456789abcdef" * 4,
        personal_restore_temp_dir=tmp_path / "preview",
        personal_safety_backup_dir=tmp_path / "safety",
        _env_file=None,
    )
    with site.begin() as con:
        metadata.create_all(con)
    app = create_app(settings)

    def database():
        with Session(site) as db:
            yield db

    app.dependency_overrides[get_database_session] = database
    app.dependency_overrides[get_authentication_session] = database
    base = "/api/v1"
    with Session(site) as db:
        package = backup(db)

    def signed_in(client, username):
        login = client.post(
            base + "/auth/login",
            headers={"Origin": origin},
            json={"username": username, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        return {"Origin": origin, "X-CSRF-Token": client.cookies["gift_ledger_csrf"]}

    def verify(client, headers, purpose, **context):
        grant = client.post(
            base + "/auth/sensitive-actions/verify",
            headers=headers,
            json={"password": PASSWORD, "purpose": purpose, **context},
        )
        assert grant.status_code == 200, grant.text
        return headers | {"X-Operation-Grant": grant.json()["grant"]}

    with TestClient(app, base_url=origin) as first:
        headers = signed_in(first, "user1")
        preview = first.post(
            base + "/restores/personal/preview",
            headers=headers,
            files={"file": ("old.giftledger", package, "application/octet-stream")},
        )
        assert preview.status_code == 200, preview.text
        confirm_path = base + f"/restores/personal/{preview.json()['preview_token']}/confirm"
        with password_capacity():
            busy = first.post(confirm_path, headers=headers, json={"password": PASSWORD})
        assert busy.status_code == 429 and busy.json()["error"]["code"] == "LOGIN_BUSY"
        assert (
            first.post(confirm_path, headers=headers, json={"password": PASSWORD}).status_code
            == 200
        )
    receipts_url = base + "/restores/personal/receipts"
    with TestClient(app, base_url=origin) as reopened:
        headers = signed_in(reopened, "user1")
        assert reopened.post(receipts_url, headers=headers).status_code == 403
        assert reopened.post(public + receipts_url, headers={"Origin": public}).status_code == 404
        receipts = reopened.post(
            receipts_url, headers=verify(reopened, headers, "restore_receipts")
        )
        assert receipts.status_code == 200, receipts.text
        token = receipts.json()[0]["safety_backup_token"]
        downloaded = reopened.get(
            base + "/restores/personal/safety-backups/" + token,
            headers=verify(reopened, headers, "safety_backup", safety_backup_token=token),
        )
        assert downloaded.status_code == 200, downloaded.text
        assert parse_personal_backup(downloaded.content).gifts[0].amount == 100
        assert (
            reopened.post(confirm_path, headers=headers, json={"password": PASSWORD}).status_code
            == 404
        )
    with TestClient(app, base_url=origin) as different:
        headers = signed_in(different, "user2")
        assert (
            different.post(
                receipts_url, headers=verify(different, headers, "restore_receipts")
            ).json()
            == []
        )


def test_receipt_write_failure_rolls_back_ledger_and_password(site, tmp_path):
    with Session(site) as db:
        original = backup(db)
        db.get(GiftRecord, 1).amount = Decimal("549.00")
        db.commit()
        revision = analyze_personal_restore(db, 1, original).ledger_revision
        password_before = db.get(User, 1).password_hash
    with site.begin() as con:
        con.exec_driver_sql(
            "CREATE TRIGGER interrupt_receipt BEFORE INSERT ON personal_restore_receipts "
            "BEGIN SELECT RAISE(FAIL,'synthetic transaction failure'); END"
        )
    with Session(site) as db, pytest.raises(PersonalRestoreError):
        confirm(db, original, revision, tmp_path / "safety")
    with Session(site) as db:
        assert db.get(GiftRecord, 1).amount == Decimal("549.00")
        owner = db.get(User, 1)
        assert owner.password_hash == password_before
        assert recent_restore_receipts(db, owner) == []
