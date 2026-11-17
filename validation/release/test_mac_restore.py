"""Mac owner recovery decisions, re-entry, and streamed file transport. No NAS or Docker."""

import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/single_yaml"))
import mac_restore as client
import restore_files
import test_backup_store as store_fixture


ID = "1" * 32
HASH = "2" * 64
OP = "3" * 32
SOURCE = {"source_project": "old", "source_instance_id": "b26de830-f363-4c54-9ad4-9783855d12d4",
          "source_server_uuid": "159ae34a-4566-49dc-b251-671dabccbbf2"}
TARGET = {"project": "new", "instance_id": "ed76be70-a951-4d66-a0bb-12a8ab22cf49",
          "server_uuid": "c48d6e46-2995-4a32-bfff-e70b53dd55ba", "url": "https://192.168.10.6:28443/"}
FILES = ("BUNDLE.json", "SHA256SUMS", "backup.json", "verify.json", f"backup-{ID}.glgui.age")


class FakeSite:
    def __init__(self):
        self.log = []
        self.created = True
        self.status = {"stage": "none"}
        self.proof = {"eligible": True, "full_decryption_verified": True, "source": SOURCE,
                      "target": TARGET, "operation_id": OP}
        self.result = {"source": SOURCE, "target": TARGET, "operation_id": OP, "backup_operation_id": ID,
                       "gift_count": 2, "full_database_and_app_files_verified": True}

    def upload(self, bundle):
        self.log.append("upload")

    def compose(self, service, *args, incoming=False):
        if service == "restore-files":
            action, bid, digest = args
            assert bid == ID and digest == HASH and incoming == (action == "stage")
            self.log.append(action)
            return {"action": action, "backup_id": bid, "manifest_sha256": digest,
                    "created": self.created if incoming else False}
        assert service == "restore" and args[0] == ID
        self.log.append(args[1:])
        if "--status" in args:
            return self.status
        if "--check-only" in args:
            return self.proof
        assert args[-2:] == ("--operation", OP)
        if "--cancel" in args:
            return {"cancelled": True, "operation_id": OP}
        return self.result


class MacRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.b = SimpleNamespace(backup_id=ID, manifest_sha256=HASH, filenames=FILES, source=SOURCE, gift_count=2)
        self.args = SimpleNamespace(project="new", host="unused", project_dir="/srv/site with spaces")
        self.site = FakeSite()
        self.enroll = mock.Mock(return_value={"project": "new", "instance_id": TARGET["instance_id"]})
        self.ask = mock.Mock(return_value=True)

    def run_flow(self):
        return client.recover(self.b, self.args, self.site, self.enroll, self.ask)

    def test_confirm_once_and_retire_only_our_exact_copy(self):
        result = self.run_flow()
        self.assertEqual(result["gift_count"], 2)
        self.enroll.assert_called_once()
        self.ask.assert_called_once()
        for expected in [TARGET["url"], TARGET["instance_id"], "2 笔"]:
            self.assertIn(expected, self.ask.call_args.args[0])
        self.assertEqual(self.site.log, [("--status",), "upload", "stage", ("--check-only",),
                                         ("--operation", OP), "retire-owned"])

    def test_preexisting_copy_is_retained(self):
        self.site.created = False
        self.run_flow()
        self.assertEqual(self.site.log[-1], "retire-owned")

    def test_cancel_binds_preflight_then_retires_managed_input(self):
        self.ask.return_value = False
        self.assertIsNone(self.run_flow())
        self.assertEqual(self.site.log[-2:], [("--cancel", "--operation", OP), "retire-owned"])

    def test_already_completed_uses_no_new_key_or_approval(self):
        self.site.status = {**self.site.proof, "stage": "completed", "bundle_sha256": HASH,
                            "result": self.site.result, "temporary_key_cleanup_pending": True}
        self.run_flow()
        self.enroll.assert_not_called()
        self.ask.assert_not_called()
        self.assertEqual(self.site.log, [("--status",), ("--operation", OP), "retire-owned"])

    def test_resume_after_writes_requires_same_operation_and_no_second_key(self):
        self.site.status = {**self.site.proof, "stage": "applying", "bundle_sha256": HASH,
                            "identity_role": "restore_identity"}
        self.run_flow()
        self.assertEqual(self.site.log, [("--status",), ("--operation", OP), "retire-owned"])
        self.enroll.assert_not_called()
        self.ask.return_value = False
        self.site.log.clear()
        self.assertIsNone(self.run_flow())
        self.assertEqual(self.site.log, [("--status",)])

    def test_wrong_source_and_wrong_restored_count_stop(self):
        self.site.proof["source"] = {}
        with self.assertRaises(client.keys.EnrollmentError):
            self.run_flow()
        self.ask.assert_not_called()
        self.site.proof["source"] = SOURCE
        self.site.result["gift_count"] = 3
        with self.assertRaises(client.keys.EnrollmentError):
            self.run_flow()
        self.assertNotIn("retire-owned", self.site.log)

    def test_failed_enrollment_retires_only_without_an_active_restore(self):
        self.enroll.side_effect = client.keys.EnrollmentError("wrong key")
        with self.assertRaises(client.keys.EnrollmentError):
            self.run_flow()
        self.assertEqual(self.site.log[-2:], [("--status",), "retire-owned"])
        def fail_after_start():
            self.site.status = {"stage":"applying"}
            raise OSError("disconnected")
        self.site.log.clear()
        self.enroll.side_effect = fail_after_start
        with self.assertRaises(OSError):
            self.run_flow()
        self.assertNotIn("retire-owned", self.site.log)

    def test_streams_exact_five_files_and_reads_sudo_compose_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.b.folder = base
            for i, name in enumerate(FILES):
                (base / name).write_bytes(bytes([i]) * 8000)
            (base / "ssh").write_text("#!/bin/sh\nexec /bin/sh -c \"$3\"\n")
            (base / "sudo").write_text("#!/bin/sh\nexport TEST_BODY=1\nexec \"$@\"\n")
            (base / "docker").write_text('#!/bin/sh\n[ "$1" != info ] || exit 1\n'
                                         'test "$TEST_BODY" = 1 && printf \'{"args":true}\\n\'\n')
            for cmd in ("ssh", "sudo", "docker"):
                (base / cmd).chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": str(base) + os.pathsep + os.environ["PATH"]}):
                with client.remote_files([str(base / "ssh")], self.args, self.b) as remote:
                    scratch = Path(remote.scratch)
                    self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
                    remote.upload(self.b)
                    folder = scratch / "five"
                    self.assertEqual({f.name for f in folder.iterdir()}, set(FILES))
                    for name in FILES:
                        self.assertEqual((folder / name).read_bytes(), (base / name).read_bytes())
                        self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600)
                    self.assertEqual(remote.compose("restore", ID, "--status"), {"args": True})
                self.assertFalse(scratch.exists())


class ManagedRestoreCopies(unittest.TestCase):
    def setUp(self):
        items = store_fixture.StoreTests()
        items.setUp()
        self.addCleanup(items.doCleanups)
        _, backup, _, _ = items.sample()
        self.backup_id = backup["operation_id"]
        self.source = items.store / "artifacts" / self.backup_id
        self.root = items.root / "new-station"
        self.root.mkdir(mode=0o700)
        self.copy = self.root / self.backup_id
        self.digest = hashlib.sha256((self.source / "BUNDLE.json").read_bytes()).hexdigest()

    def action(self, mode):
        return restore_files.stage_or_retire(mode, self.backup_id, self.digest, root=self.root, incoming=self.source)

    def test_identical_user_supplied_or_replaced_directory_is_preserved(self):
        shutil.copytree(self.source, self.copy)
        self.assertFalse(self.action("stage")["created"])
        self.assertTrue(self.action("retire-owned")["preserved"])
        shutil.rmtree(self.copy)
        self.assertTrue(self.action("stage")["created"])
        self.copy.rename(self.root / "original")
        shutil.copytree(self.source, self.copy)
        self.assertTrue(self.action("retire-owned")["preserved"])
        self.assertTrue(self.copy.is_dir())

    def test_changed_file_is_preserved(self):
        self.assertTrue(self.action("stage")["created"])
        self.assertFalse(self.action("stage")["created"])
        original = self.copy / "verify.json"
        original.rename(self.root / "original-verify")
        original.write_bytes((self.root / "original-verify").read_bytes())
        self.assertTrue(self.action("retire-owned")["preserved"])
        original.unlink()
        (self.root / "original-verify").rename(original)
        # This replacement changed ctime even after restoring the same inode.
        self.assertTrue(self.action("retire-owned")["preserved"])

    def test_owned_partial_retirement_and_repeated_retirement(self):
        self.action("stage")
        partial = self.root / f".retiring-{self.backup_id}-{self.digest}"
        self.copy.rename(partial)
        (partial / "verify.json").unlink()
        self.assertTrue(self.action("retire-owned")["retired"])
        self.assertFalse(partial.exists())
        self.assertFalse(self.action("retire-owned")["retired"])


if __name__ == "__main__":
    unittest.main()
