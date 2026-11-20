"""Only synthetic sites and generated ephemeral identities. Full crypto checks run in the Linux backup image."""
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
for directory in ("deploy/single_yaml", "deploy/release/backup_ops", "deploy/release/gui_v2"):
    sys.path.insert(0, str(REPO / directory))
import backup_ops as ops
import backup_site as site
import key_lifecycle as lifecycle
import key_lifecycle_worker as worker
import key_transfer as keys
import restore_file_check
import test_backup_store as fixtures


class RotationGuards(unittest.TestCase):
    def test_retry_failed_dsm_registration_uses_same_saved_candidate_but_fresh_engine_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve(); origin = "7" * 32
            profile = {"project": "fixture", "instance_id": str(uuid4())}
            host = Mock(project="fixture", station=state.parent)
            host.directories.return_value = (state, state)
            old = dict(stage="preflight-failed", session_id=origin, **profile)
            path = state / "runs" / origin / "run.json"
            ops.save_json(path, old)
            row = dict(operation=origin, project="fixture", instance=profile["instance_id"],
                       stage="testing", recipient="already-saved")
            with patch.object(site,"previous_worker",return_value=None), patch.object(lifecycle.dsm_backup, "run",
                        side_effect=RuntimeError("stop at synthetic engine entry")) as engine:
                with self.assertRaisesRegex(RuntimeError, "synthetic engine"):
                    lifecycle.candidate_backup(host,profile,{"platform":"dsm", "instance":row["instance"]},row)
            trial = row["trial"]
            self.assertNotEqual(trial,origin)
            self.assertEqual(ops.read_json(path),old)
            self.assertEqual(ops.read_json(state/lifecycle.JOURNAL),row)
            engine.assert_called_once_with("fixture",state.parent,expected_instance=row["instance"],
                                           key_change=origin,engine_session=trial,recover=False)

    def test_completed_trial_uses_distinct_run_and_last_result_records(self):
        case = fixtures.StoreTests()
        with tempfile.TemporaryDirectory() as d:
            case.root = Path(d).resolve(); case.store = case.root / "store"; case.sequence = 0
            _, backup, verify, manifest = case.sample()
            state = case.root / "state"; sid = "9" * 32
            marker = {k:manifest[k] for k in ("project","instance_id","server_uuid")}
            journal = state / "runs" / sid / "run.json"
            ops.save_json(journal, dict(marker, stage="finished", session_id=sid, backup_id=backup["operation_id"],
                                       verify_id=verify["operation_id"], bundle_id=backup["operation_id"],
                                       private_cleanup_pending=False))
            last = dict(marker, bundle_id=backup["operation_id"], session_id=sid, success=True,
                        local_verified=True, services_restored=True, private_cleanup_pending=False)
            ops.save_json(state / "last-result.json", last)
            host = object.__new__(site.SiteBindings)
            host.directories = lambda: (state, case.store)
            self.assertEqual(lifecycle.candidate_backup(host,marker,{"platform":"ubuntu"},{"operation":sid}),
                             backup["operation_id"])
            ops.save_json(state / "last-result.json", dict(last,session_id="a"*32))
            with self.assertRaises(ops.BackupOpsError):
                lifecycle.candidate_backup(host,marker,{"platform":"ubuntu"},{"operation":sid})

    def test_only_specific_candidate_and_completed_recipient_can_backup(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d).resolve()
            identity = {"project":"qa", "instance":"i", "operation":"1"*32, "original":"old", "recipient":"new"}
            host = object.__new__(site.SiteBindings)
            host.project = "qa"
            host.directories = lambda: (state, state)
            profile = {"project":"qa","instance_id":"i","recipient":"old"}
            ops.save_json(state/"site.json", {"backup_recipient":"old"})
            ops.save_json(state/"key-change.json", dict(identity, stage="testing"))
            with self.assertRaises(ops.BackupOpsError):
                host.check_key_binding(profile, None)
            candidate = dict(profile, recipient="new", key_change_session="1"*32, key_change_operation="1"*32)
            with self.assertRaises(ops.BackupOpsError):
                host.check_key_binding(candidate, "2"*32)
            host.check_key_binding(candidate, "1"*32)
            ops.save_json(state/"key-change.json", dict(identity,stage="committing"))
            with self.assertRaises(ops.BackupOpsError):
                host.check_key_binding(candidate, "1"*32)
            ops.save_json(state/"site.json", {"backup_recipient":"new"})
            ops.save_json(state/"key-change.json", dict(identity,stage="completed"))
            with self.assertRaises(ops.BackupOpsError):
                host.check_key_binding(profile, None)
            host.check_key_binding(dict(profile,recipient="new"), None)


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0 and Path(keys.AGE).is_file(),
                     "requires root in the isolated Linux backup image with age")
class ActualAge(unittest.TestCase):
    def test_recover_and_rotate_preserve_historical_ciphertext_and_resume_partial_commit(self):
        case = fixtures.StoreTests()
        with tempfile.TemporaryDirectory(dir="/tmp") as d, ExitStack() as stack:
            case.root = Path(d); case.store = case.root / "store"; case.sequence = 0
            old = case.root / "daily"
            candidate = case.root / "candidate"
            old.mkdir(mode=0o700); candidate.mkdir(mode=0o700)
            stack.enter_context(patch.object(worker, "DAILY", old))
            stack.enter_context(patch.object(worker, "CANDIDATE", candidate))
            cipher, backup, verify, _ = case.sample(publish=False)
            project, instance, server = (backup[k] for k in ("project", "instance_id", "server_uuid"))
            original = worker.generate(old, project, instance, uuid4().hex)
            old_copy = case.root/"owners-old.agekey"
            old_copy.write_bytes((old/"identity.txt").read_bytes()); old_copy.chmod(0o600)
            receiver = case.root/"receiver"
            keys.command([keys.KEYGEN, "-o", str(receiver)])
            public = keys.derive_recipient(receiver)
            plaintext = os.urandom(worker.CHECK_BLOCK + 93)
            cip = base64.b64decode(worker.encrypt(original["backup_recipient"], b"probe"))
            self.assertTrue(cip.startswith(b"age-encryption.org"))
            encoded = subprocess.run([keys.AGE, "-r", original["backup_recipient"]], input=plaintext,
                                     check=True, capture_output=True).stdout
            cipher.write_bytes(encoded)
            for rec in (backup, verify):
                rec["details"]["fingerprint"]["tables"] = {t:{"rows":0,"sha256":"d"*64} for t in restore_file_check.TABLES}
                rec["details"].update(ciphertext_sha256=worker.sha(encoded), archive_sha256=worker.sha(plaintext))
            ops.backup_store.publish(case.store, cipher, backup, verify)
            source = case.store/"artifacts"/backup["operation_id"]
            stack.enter_context(patch.object(worker, "SOURCE", source))
            base = [project, instance, uuid4().hex, public, "https://192.0.2.7:9443"]
            result = worker.perform(["recover", *base, backup["operation_id"], server])
            def decode(key, wrapped):
                return subprocess.run([keys.AGE,"-d","-i",str(key)], input=base64.b64decode(wrapped),
                                      capture_output=True, check=True).stdout
            returned = json.loads(decode(receiver, result["sealed"]))
            self.assertEqual(returned["identity"].encode(), old_copy.read_bytes())
            self.assertEqual(returned["verified"]["bytes"], len(encoded))
            self.assertNotIn("AGE-SECRET-KEY-", json.dumps(result))
            job = uuid4().hex
            new = worker.perform(["prepare",project,instance,job,public,"https://192.0.2.7:9443"])
            returned = json.loads(decode(receiver, new["sealed"]))
            saved = case.root/"saved-new.agekey"; saved.write_text(returned["identity"]); saved.chmod(0o600)
            self.assertEqual(hashlib.sha256(decode(saved, returned["challenge"])).hexdigest(), new["challenge_hash"])
            with self.assertRaises(subprocess.CalledProcessError):
                decode(old_copy, returned["challenge"])
            # Crash immediately after the first durable rename: remaining fields still hold old values.
            (old/"identity.txt").write_bytes((candidate/"identity.txt").read_bytes())
            fresh = worker.perform(["commit", project, instance, job, json.dumps(new["old_hashes"])])
            self.assertEqual(fresh["recipient"], keys.derive_recipient(saved))
            self.assertEqual(plaintext, subprocess.run([keys.AGE,"-d","-i",str(old_copy)], input=encoded,
                                                      check=True, capture_output=True).stdout)
            with self.assertRaises(keys.KeyErrorSafe):
                worker.perform(["recover", *base, backup["operation_id"], server])
