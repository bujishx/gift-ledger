"""Exercise bounded host decisions without a DSM, Docker socket or a real site's key."""
import contextlib
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(os.name == "posix", "the host dispatcher requires a POSIX host")
class EnrollmentWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        sys.path[:0] = [str(root / "backend/app/services"), str(root / "deploy/single_yaml")]
        try:
            global job, host, wire
            import maintenance_enrollment as job
            import maintenance_host as host
            import maintenance_enrollment_protocol as wire
        finally:
            del sys.path[:2]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.exchange = self.base / "exchange"
        self.exchange.mkdir()
        self.op = "a" * 32
        self.instance = "de436cc3-3693-49e6-97e3-61ba69444c4d"
        self.cfg = dict(project="test-site", instance=self.instance)
        self.helper = Mock()
        self.helper.cfg = self.cfg
        self.helper.name.side_effect = lambda operation: job.Helper.name(self.helper, operation)
        self.helper.image = "sha256:" + "1" * 64
        self.helper.docker.call.return_value = "0"
        self.req_fd = os.open(self.base, os.O_DIRECTORY)
        self.addCleanup(os.close, self.req_fd)
        self.handles = {"requests": self.req_fd}
        self.binding = dict(project=self.cfg["project"], instance_id=self.instance)
        self.record = dict(public=dict(self.binding, operation_id=self.op, state="queued"),
                           deadline=time.time() + 190, container="3" * 64)
        started = dict(self.binding, format="gift-ledger-gui-key-target-v1", operation_id="b" * 32,
                       started_at="2026-11-19T12:13:14+00:00")
        self.target = dict(started, success=True, started_sha256=wire.digest(wire.encode(started)), recipient="age1" + "a"*58)
        self.request = dict(self.binding, format="gift-ledger-gui-key-import-request-v1", operation_id=self.op,
                            target_recipient=self.target["recipient"], backup_recipient="age1" + "d"*58,
                            ciphertext="key-transfer-" + self.op + ".age", ciphertext_sha256="9"*64)
        self.receipt = dict(self.binding, format="gift-ledger-gui-key-import-completed-v1", operation_id=self.op,
                            success=True, backup_recipient=self.request["backup_recipient"], identity_sha256="2"*64,
                            request_sha256=wire.digest(wire.encode(self.request)),
                            started_sha256=wire.digest(wire.encode({"operation_id": self.op, "success": False})))

    def readback(self, registered, receipt):
        payloads = {"target.json": {"registered": registered, "target": self.target}, "receipt.json": receipt}
        with patch.object(job, "private_output", side_effect=lambda exchange, name: payloads.get(name)):
            return job.readback(self.exchange, self.helper, self.record)

    def test_only_completed_helper_receipt_and_matching_site_binding_count(self):
        with self.assertRaisesRegex(ValueError, "unrequested_key_import_receipt"):
            self.readback(False, self.receipt)
        self.record["request"] = self.request
        for changes in ({"operation_id": "c" * 32}, {"backup_recipient": "age1" + "z" * 58},
                        {"request_sha256": "c" * 64}, {"instance_id": "some-other-site"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.readback(False, dict(self.receipt, **changes))
        self.helper.committed.assert_not_called()
        self.assertEqual(self.readback(False, self.receipt), self.receipt)
        self.helper.committed.assert_called_once_with(self.receipt)

    def test_replayed_archived_id_does_not_replace_receipt_or_start_helper(self):
        old = self.base / "jobs" / ("enrollment-" + self.op + ".json")
        old.parent.mkdir()
        old.write_bytes(b"original terminal receipt")
        self.record["public"].update(state="succeeded", operation_id="f" * 32)
        with patch.object(job, "marker", return_value=self.op), patch.object(job, "retire") as retire, \
                patch.object(job, "Helper") as helper:
            ran = job.tick(host, self.base, self.handles, self.cfg, self.record, Mock(), "approved image")
        self.assertFalse(ran)
        self.assertEqual(old.read_bytes(), b"original terminal receipt")
        self.assertEqual(self.record["public"]["operation_id"], "f" * 32)
        helper.assert_not_called()
        retire.assert_called_once()

    def test_orphan_cleanup_requires_the_actual_original_site_labels(self):
        helper = object.__new__(job.Helper)
        helper.cfg, helper.image, helper.docker = self.cfg, self.helper.image, Mock()
        operation = self.op
        actual = {"id": "6" * 64, "name": "/" + helper.name(operation), "image": helper.image, "session": operation,
                  "status": "created", "running": False}
        helper.docker.inspect.return_value = actual
        for project, instance, accepted in (("another-station", self.instance, False), (self.cfg["project"], "other", False),
                                            (self.cfg["project"], self.instance, True)):
            with self.subTest(accepted=accepted, project=project, instance=instance):
                labels = {"org.gift-ledger.gui-v2-project": project, "org.gift-ledger.maintenance-instance": instance}
                helper.docker.call.reset_mock()
                helper.docker.call.side_effect = [actual["id"], json.dumps(labels), ""]
                if accepted:
                    helper.retire_original(operation, None)
                    helper.docker.call.assert_called_with("rm", actual["id"])
                else:
                    with self.assertRaisesRegex(ValueError, "enrollment_helper_site_mismatch"):
                        helper.retire_original(operation, None)
                    self.assertFalse(any(call.args[0] in {"wait", "rm"} for call in helper.docker.call.call_args_list))

    def test_status_outputs_remain_independently_parseable_single_json_documents(self):
        backup = {"state":"succeeded", "operation_id":"b" * 32}
        public = {"state":"idle", "operation_id":None}
        (self.base / "result.json").write_text(json.dumps(backup))
        (self.base / "enrollment.json").write_text(json.dumps({"public": public, "private_journal_marker": True}))
        for action, expected in (("status", backup), ("enrollment-status", public)):
            with self.subTest(action=action), patch.object(host.os, "geteuid", return_value=0), \
                    patch.object(host.sys, "flags", types.SimpleNamespace(isolated=True)), \
                    patch.object(host, "read_config"), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(host.main([action, "--state", str(self.base)]), 0)
                self.assertEqual(json.loads(out.getvalue()), expected)

    def test_existing_material_can_be_read_back_without_new_sealed_packet(self):
        self.record["public"]["operation_id"] = "4" * 32
        self.assertEqual(self.readback(True, self.receipt), self.receipt)
        self.assertTrue(self.record["public"]["registered"])
        self.assertNotIn("request", self.record)

    def test_running_again_after_packet_never_replays_when_helper_exited_without_receipt(self):
        self.record["request"] = self.request
        self.record["public"].update(target=self.target, registered=False, state="verifying")
        self.helper.docker.inspect.return_value = dict(id=self.record["container"], session=self.op,
            name="/" + self.helper.name(self.op), image=self.helper.image, running=False, status="exited", exit_code=0)
        with patch.object(job, "readback", return_value=None), patch.object(job, "sealed_packet", return_value=None), \
                patch.object(job, "write_state") as saved:
            job.exchange_keys(host, self.base, self.handles, self.helper, self.record, self.exchange)
        self.assertEqual(self.record["public"]["state"], "needs_review")
        self.assertEqual(self.record["public"]["code"], "enrollment_receipt_missing")
        saved.assert_called_once()
        self.assertFalse((self.exchange / "packet.json").exists())

    def test_timeout_without_packet_never_writes_key(self):
        self.record["deadline"] = time.time() - 1
        with patch.object(job, "readback", return_value=None), patch.object(job, "write_state"):
            job.exchange_keys(host, self.base, self.handles, self.helper, self.record, self.exchange)
        self.assertEqual(self.record["public"]["state"], "expired")
        self.assertTrue((self.exchange / "cancel").is_file())
        self.assertFalse((self.exchange / "packet.json").exists())

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0 and Path("/usr/local/bin/age").is_file()
                         and Path("/usr/local/bin/age-keygen").is_file(),
                         "requires an isolated Linux backup image with its native age tools")
    def test_native_age_cipher_and_committed_identity_match_public_receipt(self):
        import key_transfer as keys

        script = Path(__file__).resolve().parents[2] / "deploy/single_yaml/backup_key.py"
        spec = importlib.util.spec_from_file_location("local_enrollment_helper", script)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        local_key, transfer, identity, scratch = [self.base / name for name in ("local", "transfer", "identity", "scratch")]
        state = self.base / "state"
        for directory in (transfer, identity, scratch, state):
            directory.mkdir(mode=0o700)
        subprocess.run([keys.KEYGEN, "-o", str(local_key)], capture_output=True, check=True)
        secret, = [line + b"\n" for line in local_key.read_bytes().splitlines() if line.startswith(b"AGE-SECRET-")]
        recipient = subprocess.run([keys.KEYGEN, "-y"], input=secret, capture_output=True, check=True).stdout.decode().strip()
        with patch.object(keys, "EXCHANGE", self.exchange), patch.object(keys, "TRANSFER", transfer), \
                patch.object(keys, "IDENTITY", identity), patch.object(helper, "STATE", state), \
                patch.object(keys, "project_identity", return_value=self.instance):
            transport, target = helper.transport_target(self.cfg["project"], self.instance, scratch, False)
            wire.check_target(target, self.cfg["project"], self.instance)
            public = dict(self.binding, operation_id=self.op, target_recipient=target["recipient"], backup_recipient=recipient)
            payload = dict(public, format="gift-ledger-gui-key-transfer-payload-v1", identity=secret.decode("ascii"))
            cipher = subprocess.run([keys.AGE, "--encrypt", "--recipient", target["recipient"]],
                                    input=wire.encode(payload), capture_output=True, check=True).stdout
            request = dict(public, format="gift-ledger-gui-key-import-request-v1",
                           ciphertext="key-transfer-" + self.op + ".age", ciphertext_sha256=wire.digest(cipher))
            filename = "家庭备份.agekey"
            packet = {"ciphertext_b64": base64.b64encode(cipher).decode(), "request": request, "filename": filename}
            self.assertEqual(wire.check_packet(packet, self.op, target), request)
            (self.exchange / "packet.json").write_bytes(wire.encode(packet))
            proof = helper.receive(identity, self.cfg["project"], self.instance, transport, target, scratch)
            wire.check_receipt(proof, target, request)
            self.assertEqual(proof["identity_sha256"], wire.digest(secret))
            self.assertEqual(proof, helper.committed(identity, self.cfg["project"], self.instance))
            self.assertEqual(json.loads((state / "key-label.json").read_bytes()),
                             dict(self.binding, recipient=recipient, filename=filename))
            self.assertNotIn(secret.rstrip(), wire.encode(proof))
            self.assertNotIn(secret.rstrip(), (self.exchange / "packet.json").read_bytes())

    def test_resource_flags_use_explicit_engine_capabilities_and_known_dsm_only(self):
        caps = dict(memory=True, pids=True, quota=True, period=True, cpuset=True, cpus=4,
                    security=["name=seccomp,profile=builtin"])
        helper = object.__new__(job.Helper)
        helper.docker = Mock()
        helper.dsm = False
        helper.docker.call.return_value = json.dumps(caps)
        self.assertEqual(helper.limits(), ["--memory=128m", "--cpus=1", "--pids-limit=32"])
        caps.update(pids=False, quota=False, period=False, security=[])
        helper.docker.call.return_value = json.dumps(caps)
        with patch.object(os, "sched_getaffinity", return_value={2, 3}, create=True):
            with self.assertRaisesRegex(ValueError, "enrollment_dsm_engine_required"):
                helper.limits()
            helper.dsm = True
            self.assertEqual(helper.limits(), ["--memory=128m", "--cpuset-cpus=2"])
            for changes in ({"memory": False}, {"cpuset": False}, {"pids": None},
                            {"security":["name=seccomp,profile=unconfined"]}):
                helper.docker.call.return_value = json.dumps(dict(caps, **changes))
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    helper.limits()

    def test_interrupted_registration_review_does_not_import_and_releases_claim(self):
        self.record["public"].update(state="needs_review", registered=False, target=self.target)
        self.record["request"] = self.request
        self.helper.state = self.base
        self.helper.docker.call.return_value = "0"
        self.helper.start.return_value = "5" * 64
        archive = types.SimpleNamespace(ops=types.SimpleNamespace(host_lock=lambda _: contextlib.nullcontext()))
        for registered in [True, False]:
            self.record["public"]["state"] = "needs_review"
            evidence = {"target.json": dict(target=self.target, registered=registered),
                        "receipt.json": self.receipt if registered else None}
            with self.subTest(registered=registered), patch.object(job, "current", return_value=self.record), \
                    patch.object(job, "Helper", return_value=self.helper), patch.object(job, "write_state"), \
                    patch.object(job, "private_output", side_effect=lambda path, name:evidence[name]), patch.object(job, "retire"):
                result = job.review(host, self.base, self.handles, self.cfg, self.op, archive, self.helper.image)
                self.assertEqual(result["state"], "succeeded" if registered else "failed")
                self.assertFalse(any(self.base.glob("*/packet.json")))


if __name__ == "__main__":
    unittest.main()
