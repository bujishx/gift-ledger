"""Public bundle publication/retention only; no Docker, NAS, keys or real data."""

import copy
import hashlib
import importlib.util
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "backup_store_test_tool", REPO / "deploy/release/backup_ops/backup_store.py"
)
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)
UTC = timezone.utc
NOW = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)


class StoreTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / ".runtime/backup-store-unit"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(scratch))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        self.sequence = 0

    def sample(self, completed=NOW, protected=False, publish=True):
        self.sequence += 1
        operation = "%032x" % (self.sequence * 2)
        verification = "%032x" % (self.sequence * 2 + 1)
        data = b"age-encryption.org/v1\nsynthetic-ciphertext:" + operation.encode()
        cipher = self.root / ("backup-" + operation + ".glgui.age")
        cipher.write_bytes(data)
        before = {
            "fingerprint": {
                "algorithm": "sha256-canonical-v1",
                "schema_revision": "schema_v2_s011",
                "tables": {"users": {"rows": 2, "sha256": "d" * 64}},
            },
            "app_files": {},
            "ciphertext": cipher.name,
            "ciphertext_sha256": hashlib.sha256(data).hexdigest(),
            "archive_sha256": "e" * 64,
            "schema_revision": "schema_v2_s011",
            "backup_type": "manual",
            "source": {
                "project": "gift-ledger-test",
                "instance_id": "52fc36bc-c433-498a-a773-29d6d8534a9d",
                "server_uuid": "ec1c84ec-43bf-45e9-a2d2-6214c1a23e91",
                "canonical_identity_sha256": "1" * 64,
                "layout_sha256": "c" * 64,
                "engine_identity_verified": False,
                "applications_stopped_verified": False,
            },
            "plaintext_package_validated": True,
            "encryption_roundtrip_verified": False,
            "reliable_backup_verified": False,
            "restore_verified": False,
            "applications_stopped_verified": False,
            "sql_freeze_verified": True,
            "readonly_before": {"read_only": False, "super_read_only": False},
            "readonly_restored": True,
            "automatic_backup_configured": False,
            "offsite_configured": False,
        }
        backup = {
            "format": TOOL.RECEIPT_FORMAT,
            "version": 1,
            "success": True,
            "status": "completed",
            "action": "backup",
            "operation_id": operation,
            "project": "gift-ledger-test",
            "instance_id": "52fc36bc-c433-498a-a773-29d6d8534a9d",
            "server_uuid": "ec1c84ec-43bf-45e9-a2d2-6214c1a23e91",
            "request_sha256": "a" * 64,
            "assets_sha256": "b" * 64,
            "layout_sha256": "c" * 64,
            "started_sha256": "f" * 64,
            "started_at": (completed - timedelta(seconds=30)).isoformat(),
            "completed_at": (completed - timedelta(seconds=10)).isoformat(),
            "details": before,
        }
        after = {
            key: copy.deepcopy(before[key]) for key in TOOL.VERIFY_KEYS if key in before
        }
        after.update(
            {
                "backup_operation_id": operation,
                "package_verified": True,
                "encryption_roundtrip_verified": True,
                "reliable_backup_verified": True,
            }
        )
        verify = copy.deepcopy(backup)
        verify.update(
            {
                "action": "verify",
                "operation_id": verification,
                "started_at": (completed - timedelta(seconds=5)).isoformat(),
                "completed_at": completed.isoformat(),
                "details": after,
            }
        )
        manifest = None
        if publish:
            manifest = TOOL.publish(
                self.store, cipher, backup, verify, protected=protected
            )
        return cipher, backup, verify, manifest

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(TOOL.BackupStoreError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_publish_exact_members_and_readonly_reentry(self):
        cipher, backup, verify, manifest = self.sample()
        target = self.store / "artifacts" / backup["operation_id"]
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {cipher.name, "backup.json", "verify.json", "BUNDLE.json", "SHA256SUMS"},
        )
        stamps = {entry.name: entry.stat().st_mtime_ns for entry in target.iterdir()}
        self.assertEqual(TOOL.inspect_bundle(target), manifest)
        self.assertEqual(TOOL.publish(self.store, cipher, backup, verify), manifest)
        self.assertEqual(
            stamps, {entry.name: entry.stat().st_mtime_ns for entry in target.iterdir()}
        )
        self.assertEqual(TOOL.list_bundles(self.store), [(target, manifest)])

    def test_json_path_receipts_and_duplicate_key_rejected(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        before, after = self.root / "b.json", self.root / "v.json"
        before.write_bytes(TOOL.canonical(backup))
        after.write_bytes(TOOL.canonical(verify))
        TOOL.publish(self.store, cipher, before, after)
        before.write_text('{"success":true,"success":true}', encoding="utf-8")
        self.error(
            "duplicate_json_key", TOOL.publish, self.store, cipher, before, after
        )

    def test_binding_and_success_gates(self):
        for key, value in (
            ("project", "another-project"),
            ("instance_id", "6f02751d-121a-4442-9a89-077b322d51c0"),
            ("server_uuid", "6f02751d-121a-4442-9a89-077b322d51c0"),
        ):
            cipher, backup, verify, _ = self.sample(publish=False)
            verify[key] = value
            verify["details"]["source"][key] = value
            self.error(
                "receipt_binding_mismatch",
                TOOL.publish,
                self.store,
                cipher,
                backup,
                verify,
            )
        cipher, backup, verify, _ = self.sample(publish=False)
        verify["success"] = False
        self.error(
            "receipt_not_completed", TOOL.publish, self.store, cipher, backup, verify
        )
        verify["success"] = True
        verify["details"]["reliable_backup_verified"] = False
        self.error(
            "backup_not_reliably_verified",
            TOOL.publish,
            self.store,
            cipher,
            backup,
            verify,
        )

    def test_cross_backup_or_different_digest_refused(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        verify["details"]["backup_operation_id"] = "9" * 32
        self.error(
            "backup_operation_mismatch",
            TOOL.publish,
            self.store,
            cipher,
            backup,
            verify,
        )
        verify["details"]["backup_operation_id"] = backup["operation_id"]
        verify["details"]["ciphertext_sha256"] = "9" * 64
        self.error(
            "verification_content_mismatch",
            TOOL.publish,
            self.store,
            cipher,
            backup,
            verify,
        )

    def test_receipt_unknown_fields_and_secret_values_rejected(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        backup["unexpected"] = "not-a-receipt-field"
        self.error(
            "receipt_fields_invalid", TOOL.publish, self.store, cipher, backup, verify
        )
        del backup["unexpected"]
        backup["details"]["source"]["password"] = "synthetic-forbidden"
        self.error(
            "secret_content_forbidden", TOOL.publish, self.store, cipher, backup, verify
        )
        del backup["details"]["source"]["password"]
        backup["details"]["source"]["project"] = "AGE-SECRET-KEY-SYNTHETIC"
        self.error(
            "secret_content_forbidden", TOOL.publish, self.store, cipher, backup, verify
        )

    def test_unsafe_filename_and_truncated_source_rejected(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        backup["details"]["ciphertext"] = verify["details"]["ciphertext"] = (
            "../escape.age"
        )
        self.error(
            "ciphertext_name_mismatch", TOOL.publish, self.store, cipher, backup, verify
        )
        backup["details"]["ciphertext"] = verify["details"]["ciphertext"] = cipher.name
        cipher.write_bytes(b"short")
        self.error(
            "ciphertext_format_invalid", TOOL.publish, self.store, cipher, backup, verify
        )
        self.assertFalse(self.store.exists())

    def test_bundle_truncation_unknown_member_and_wrong_leaf(self):
        cipher, backup, _, _ = self.sample()
        target = self.store / "artifacts" / backup["operation_id"]
        (target / "unexpected.txt").write_text("evidence", encoding="utf-8")
        self.error("bundle_members_invalid", TOOL.inspect_bundle, target)
        (target / "unexpected.txt").unlink()
        (target / cipher.name).write_bytes(b"truncated")
        self.error("ciphertext_format_invalid", TOOL.inspect_bundle, target)

    def test_hardlinked_source_and_members_refused(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        alias = self.root / "hardlink"
        try:
            os.link(str(cipher), str(alias))
        except PermissionError:
            self.skipTest("the Windows sandbox denies actual hardlink creation")
        self.error(
            "regular_single_link_file_required",
            TOOL.publish,
            self.store,
            cipher,
            backup,
            verify,
        )
        alias.unlink()
        TOOL.publish(self.store, cipher, backup, verify)
        target = self.store / "artifacts" / backup["operation_id"]
        os.link(str(target / "backup.json"), str(alias))
        self.error("regular_single_link_file_required", TOOL.inspect_bundle, target)

    def test_hardlink_metadata_refusal_without_link_creation(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == cipher:
                fields = {
                    key: getattr(info, key)
                    for key in ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns")
                }
                fields["st_nlink"] = 2
                return types.SimpleNamespace(**fields)
            return info

        with mock.patch.object(Path, "lstat", lstat):
            self.error(
                "regular_single_link_file_required",
                TOOL.publish,
                self.store,
                cipher,
                backup,
                verify,
            )

    def test_symlink_rejected_when_platform_allows_it(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        alias = self.root / "alias"
        try:
            alias.symlink_to(cipher)
        except OSError:
            self.skipTest(
                "symlink creation needs a Windows capability not present here"
            )
        cipher.unlink()
        cipher.symlink_to(alias)
        self.error("link_forbidden", TOOL.publish, self.store, cipher, backup, verify)

    def test_failed_publish_never_exposes_partial_artifact(self):
        cipher, backup, verify, _ = self.sample(publish=False)
        original_write = TOOL._write

        def interrupted(path, payload):
            if Path(path).name == "verify.json":
                raise OSError("synthetic interruption")
            return original_write(path, payload)

        with mock.patch.object(
            TOOL, "_write", side_effect=interrupted
        ), self.assertRaises(OSError):
            TOOL.publish(self.store, cipher, backup, verify)
        self.assertEqual(list((self.store / "artifacts").iterdir()), [])
        pending = list((self.store / "pending").iterdir())
        self.assertEqual(len(pending), 1)
        TOOL.publish(self.store, cipher, backup, verify)
        self.assertEqual(list((self.store / "pending").iterdir()), pending)

    def test_atomic_rename_receives_fully_verified_directory(self):
        cipher, backup, verify, manifest = self.sample(publish=False)
        original = TOOL.os.rename
        observed = []

        def rename(source, destination):
            observed.append(TOOL.inspect_bundle(source))
            self.assertFalse(Path(destination).exists())
            original(source, destination)

        with mock.patch.object(TOOL.os, "rename", side_effect=rename):
            manifest = TOOL.publish(self.store, cipher, backup, verify)
        self.assertEqual(observed, [manifest])

    def test_existing_different_protection_is_not_overwritten(self):
        cipher, backup, verify, _ = self.sample()
        self.error(
            "existing_bundle_mismatch",
            TOOL.publish,
            self.store,
            cipher,
            backup,
            verify,
            protected=True,
        )

    def test_manifest_or_checksum_tamper_refused(self):
        _, backup, _, _ = self.sample()
        target = self.store / "artifacts" / backup["operation_id"]
        (target / "SHA256SUMS").write_bytes(b"changed")
        self.error("bundle_manifest_or_sums_changed", TOOL.inspect_bundle, target)

    def test_daily_calendar_boundary_timezone_and_empty_day_gap(self):
        dates = [
            datetime(2026, 10, 16, 15, 59, tzinfo=UTC),
            datetime(2026, 10, 16, 16, 0, tzinfo=UTC),
            datetime(2026, 10, 16, 16, 1, tzinfo=UTC),
        ]
        ids = [self.sample(completed=date)[1]["operation_id"] for date in dates]
        plan = TOOL.retention_plan(self.store, NOW, daily=1, weekly=0, monthly=0)
        self.assertEqual(plan["keep"], [ids[-1]])
        self.assertEqual(plan["remove"], ids[:2])
        self.assertEqual(plan["reasons"][ids[-1]], ["daily:0", "last-success"])
        # Two recent calendar days do not pull in a third older success to fill a failure gap.
        old = self.sample(completed=NOW - timedelta(days=3))[1]["operation_id"]
        plan = TOOL.retention_plan(self.store, NOW, daily=2, weekly=0, monthly=0)
        self.assertIn(old, plan["remove"])

    def test_week_month_cross_year_latest_selection_and_overlap(self):
        now = datetime(2027, 1, 2, 8, tzinfo=UTC)
        dates = [
            datetime(2026, 11, 30, 6, tzinfo=UTC),
            datetime(2026, 12, 20, 6, tzinfo=UTC),
            datetime(2026, 12, 27, 6, tzinfo=UTC),
            datetime(2026, 12, 31, 6, tzinfo=UTC),
            datetime(2027, 1, 1, 6, tzinfo=UTC),
        ]
        ids = [self.sample(completed=date)[1]["operation_id"] for date in dates]
        plan = TOOL.retention_plan(self.store, now, daily=2, weekly=2, monthly=2)
        self.assertEqual(plan["keep"], sorted(ids[2:]))
        self.assertEqual(
            plan["reasons"][ids[-1]],
            ["daily:1", "weekly:0", "monthly:0", "last-success"],
        )
        self.assertEqual(plan["reasons"][ids[-2]], ["monthly:1"])
        self.assertEqual(plan["reasons"][ids[-3]], ["weekly:1"])

    def test_protected_last_success_and_zero_windows(self):
        old = self.sample(completed=NOW - timedelta(days=800), protected=True)[1][
            "operation_id"
        ]
        ordinary = self.sample(completed=NOW - timedelta(days=700))[1]["operation_id"]
        last = self.sample(completed=NOW - timedelta(days=600))[1]["operation_id"]
        plan = TOOL.retention_plan(self.store, NOW, daily=0, weekly=0, monthly=0)
        self.assertEqual(plan["keep"], sorted([old, last]))
        self.assertEqual(plan["remove"], [ordinary])
        removed = TOOL.apply_retention(self.store, plan)
        self.assertEqual([path.name for path in removed], [ordinary])
        self.assertEqual(len(TOOL.list_bundles(self.store)), 2)

    def test_unconfirmed_old_hold_survives_until_confirmed_plan_is_recomputed(self):
        old = self.sample(completed=NOW - timedelta(days=800))[1]["operation_id"]
        newest = self.sample()[1]["operation_id"]
        held = TOOL.retention_plan(self.store, NOW, daily=1, weekly=0, monthly=0,
                                   hold_ids=(old,))
        self.assertIn(old, held["keep"])
        self.assertNotIn(old, held["remove"])
        self.assertIn("external-copy-unconfirmed", held["reasons"][old])
        self.assertEqual(TOOL.apply_retention(self.store, held), [])
        # The caller has now verified the external copy and explicitly removes
        # its hold. A newly computed, bound plan may retire only this old set.
        confirmed = TOOL.retention_plan(self.store, NOW, daily=1, weekly=0, monthly=0,
                                        hold_ids=())
        self.assertEqual(confirmed["remove"], [old])
        self.assertEqual(confirmed["keep"], [newest])
        removed = TOOL.apply_retention(self.store, confirmed)
        self.assertEqual([path.name for path in removed], [old])
        self.assertEqual([path.name for path, _manifest in TOOL.list_bundles(self.store)], [newest])

    def test_unknown_external_hold_is_rejected_without_deleting_existing_bundles(self):
        _cipher, backup, _verify, _manifest = self.sample()
        self.error("retention_hold_ids_invalid", TOOL.retention_plan, self.store, NOW,
                   hold_ids=("f" * 32,))
        self.assertTrue((self.store / "artifacts" / backup["operation_id"]).is_dir())

    def test_held_plan_is_stale_after_inventory_changes_and_tamper_is_rejected(self):
        old = self.sample(completed=NOW - timedelta(days=800))[1]["operation_id"]
        self.sample(completed=NOW - timedelta(seconds=1))
        plan = TOOL.retention_plan(self.store, NOW, hold_ids=(old,))
        altered = copy.deepcopy(plan)
        altered["hold_ids"] = []
        self.error("retention_plan_hash_mismatch", TOOL.apply_retention, self.store, altered)
        self.sample()
        self.error("retention_plan_stale_or_changed", TOOL.apply_retention, self.store, plan)
        self.assertTrue((self.store / "artifacts" / old).is_dir())

    def test_future_time_and_naive_now_are_rejected(self):
        self.sample(completed=NOW + timedelta(seconds=1))
        self.error("future_bundle_timestamp", TOOL.retention_plan, self.store, NOW)
        self.error(
            "aware_now_required",
            TOOL.retention_plan,
            self.store,
            NOW.replace(tzinfo=None),
        )

    def test_pending_directory_is_never_selected_or_removed(self):
        self.sample()
        pending = self.store / "pending" / "interrupted-evidence"
        pending.mkdir()
        (pending / "unrecognized.txt").write_text("evidence", encoding="utf-8")
        plan = TOOL.retention_plan(self.store, NOW)
        TOOL.apply_retention(self.store, plan)
        self.assertTrue((pending / "unrecognized.txt").exists())

    def test_plan_rehash_does_not_authorize_removing_last_success(self):
        _, backup, _, _ = self.sample()
        plan = TOOL.retention_plan(self.store, NOW)
        plan["remove"] = [backup["operation_id"]]
        plan["keep"] = []
        del plan["plan_sha256"]
        plan["plan_sha256"] = TOOL.digest(TOOL.canonical(plan))
        self.error(
            "retention_plan_stale_or_changed", TOOL.apply_retention, self.store, plan
        )

    def test_inventory_addition_or_bundle_change_blocks_all_cleanup(self):
        _, old, _, _ = self.sample(completed=NOW - timedelta(days=700))
        self.sample()
        plan = TOOL.retention_plan(self.store, NOW)
        self.sample(completed=NOW - timedelta(seconds=1))
        self.error(
            "retention_plan_stale_or_changed", TOOL.apply_retention, self.store, plan
        )
        self.assertTrue((self.store / "artifacts" / old["operation_id"]).exists())
        plan = TOOL.retention_plan(self.store, NOW)
        (self.store / "artifacts" / old["operation_id"] / "unexpected.txt").write_text(
            "changed", encoding="utf-8"
        )
        self.error("bundle_members_invalid", TOOL.apply_retention, self.store, plan)
        self.assertTrue(
            (self.store / "artifacts" / old["operation_id"] / "backup.json").exists()
        )

    def test_plan_root_hash_and_unknown_artifact_rejected(self):
        self.sample()
        plan = TOOL.retention_plan(self.store, NOW)
        changed = copy.deepcopy(plan)
        changed["remove"] = [".."]
        self.error(
            "retention_plan_hash_mismatch", TOOL.apply_retention, self.store, changed
        )
        other = self.root / "other"
        other.mkdir()
        self.error("retention_plan_root_mismatch", TOOL.apply_retention, other, plan)
        (self.store / "artifacts" / "unknown").mkdir()
        self.error("unknown_artifact_member", TOOL.list_bundles, self.store)

    def test_mixed_instance_store_refuses_retention(self):
        self.sample()
        cipher, backup, verify, _ = self.sample(publish=False)
        backup["instance_id"] = verify["instance_id"] = (
            "6f02751d-121a-4442-9a89-077b322d51c0"
        )
        backup["details"]["source"]["instance_id"] = backup["instance_id"]
        verify["details"]["source"]["instance_id"] = verify["instance_id"]
        TOOL.publish(self.store, cipher, backup, verify)
        self.error("mixed_store_identities", TOOL.retention_plan, self.store, NOW)


if __name__ == "__main__":
    unittest.main()
