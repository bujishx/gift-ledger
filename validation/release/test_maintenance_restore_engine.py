"""Host restore uses release proofs and scoped lifecycle control even if its API is offline."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
for src in ("deploy/single_yaml", "deploy/release/backup_ops"):
    sys.path.insert(0, str(ROOT / src))
import maintenance_restore as engine
import restore_site
from restore_input import RecoveryError


class RestoreHost(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.state = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.inputs = self.state / "inputs"
        self.inputs.mkdir()
        self.cfg = {"project": "qa", "instance_id": "site-instance", "origin": "https://192.0.2.3:16453"}
        self.ids = {"db": "d", "api": "a", "web": "b"}
        self.backup, self.manifest = "3"*32, "6"*64
        self.auth = object.__new__(engine.Authority)
        self.auth.package = None
        self.auth.state, self.auth.inputs = self.state, self.inputs
        self.auth.project, self.auth.site = "qa", self.cfg
        self.auth.manifest, self.auth.key_operation = self.manifest, "1"*32
        self.auth.server_uuid, self.auth.docker = "db-uuid", Mock()
        self.target = {**{k: self.cfg[k] for k in ("project", "instance_id")}, "server_uuid": "db-uuid",
                       "url": self.cfg["origin"] + "/", "leaf_pem_sha256": "7"*64,
                       "root_pem_sha256": "8"*64, "identity_sha256": "9"*64}
        self.auth.release_key = Mock(return_value=True)
        self.auth.check_key = Mock()
        self.auth.host = Mock()
        self.profile = {"project": "qa", "schema_revision": "schema_v2_s011", "containers": {}}
        self.original = {}
        for role, char in self.ids.items():
            entry = {"id": char*64, "name": "qa-"+role+"-1", "image": "sha256:"+char*64, "mounts": []}
            self.profile["containers"][role] = entry
            self.original[role] = dict(entry, name="/"+entry["name"], running=True, project="qa", service=role)
        self.auth.host.runtime_profile.return_value = self.profile
        self.auth.host.services.return_value = self.original
        selected = self.selected = Mock()
        selected.bundle_hash = self.manifest
        selected.options.return_value = {"source_operation_id": self.backup, "source_instance_id": "source"}
        selected.backup = {"details": {"fingerprint": {"tables": {"gift_records": {"rows": 1}}}}}
        stack.enter_context(patch.object(restore_site, "RestoreInput", return_value=selected))
        self.job = stack.enter_context(patch.object(restore_site, "Job")).return_value
        self.stop = stack.enter_context(patch.object(restore_site, "stop_original"))
        self.resume = stack.enter_context(patch.object(restore_site.ops, "restore_services"))

    def action(self, **options):
        return restore_site.run_bound(self.auth.host, self.auth.docker, "qa", self.backup, "restore_identity",
                                      self.state, self.inputs, authority=self.auth, **options)

    def eligible(self):
        self.job.run.return_value = {"target": self.target}
        result = self.action(check_only=True)
        self.assertEqual(result["gift_count"], 1)
        self.assertTrue(result["eligible"] and result["full_decryption_verified"])
        return result["operation_id"]

    def sql_result(self, op):
        return {"target": self.target, "source": self.selected.options(), "operation_id": op,
                "backup_operation_id": self.backup, "full_database_and_app_files_verified": True,
                "gift_count": 1, "cash_amount_all_gift_records": "16.88"}

    def test_invalid_confirmation_or_site_rejected_before_stopping(self):
        with self.assertRaisesRegex(RecoveryError, "restore_web_preflight_required"):
            self.action(operation="5"*32)
        self.selected.bundle_hash = "1"*64
        with self.assertRaisesRegex(RecoveryError, "restore_web_manifest_changed"):
            self.action(check_only=True)
        self.selected.bundle_hash = self.manifest
        self.job.run.return_value = {"target": dict(self.target, instance_id="wrong-site")}
        with self.assertRaisesRegex(RecoveryError, "restore_web_target_changed"):
            self.action(check_only=True)
        self.stop.assert_not_called()
        self.resume.assert_not_called()
        self.auth.release_key.assert_called_once()

    def test_checks_packet_lease_and_actual_amount_before_resuming(self):
        op = self.eligible()
        self.auth.key_operation = "f"*32
        with self.assertRaisesRegex(RecoveryError, "restore_web_job_changed"):
            self.action(operation=op)
        self.stop.assert_not_called()
        self.auth.key_operation = "1"*32
        self.job.run.side_effect = [{}, self.sql_result(op)]
        result = self.action(operation=op)
        self.assertEqual(result["cash_amount_all_gift_records"], "16.88")
        self.assertTrue(result["full_database_and_app_files_verified"])
        self.stop.assert_called_once()
        self.resume.assert_called_once()
        self.auth.release_key.assert_called_once()
        self.assertFalse(self.action(status=True)["temporary_key_cleanup_pending"])

    def test_target_populated_between_preview_and_confirmation_is_rejected_before_stop(self):
        op = self.eligible()
        self.job.run.side_effect = RecoveryError("restore_requires_empty_target")
        with self.assertRaisesRegex(RecoveryError, "restore_requires_empty_target"):
            self.action(operation=op)
        current = self.action(status=True)
        self.assertEqual((current["stage"], current["operation_id"]), ("rejected", op))
        self.stop.assert_not_called()
        self.resume.assert_not_called()
        self.auth.release_key.assert_called_once()

    def test_partial_commit_reentry_verifies_only_and_keeps_services_stopped_without_proof(self):
        op = self.eligible()
        self.job.run.side_effect = [{}, RecoveryError("private_sql_failure")]
        with self.assertRaisesRegex(RecoveryError, "private_sql_failure"):
            self.action(operation=op)
        self.assertEqual(self.action(status=True)["stage"], "applying")
        self.job.run.reset_mock()
        self.job.run.side_effect = [RecoveryError("restore_completion_not_proven")]
        with self.assertRaisesRegex(RecoveryError, "restore_completion_not_proven"):
            self.action(operation=op)
        self.job.run.assert_called_once_with("verify")
        self.resume.assert_not_called()
        self.auth.release_key.assert_not_called()
        self.job.run.reset_mock()
        self.job.run.side_effect = [self.sql_result(op)]
        self.action(operation=op)
        self.job.run.assert_called_once_with("verify")
        self.resume.assert_called_once()

    def test_changed_source_receipt_keeps_target_offline(self):
        op = self.eligible()
        for proof in ({"source": {}}, {"operation_id": "e"*32}, {"gift_count": 2}, {"cash_amount_all_gift_records": "NaN"}):
            with self.subTest(proof=proof):
                self.job.run.side_effect = ([{}, dict(self.sql_result(op), **proof)] if self.action(status=True)["stage"] == "eligible"
                                            else [dict(self.sql_result(op), **proof)])
                with self.assertRaisesRegex(RecoveryError, "restore_web_receipt_mismatch"):
                    self.action(operation=op)
                self.resume.assert_not_called()
                self.auth.release_key.assert_not_called()

    def test_proven_empty_error_can_restart_and_release_the_key(self):
        op = self.eligible()
        error = RecoveryError("empty_target_preserved")
        error.before_writes = True
        self.job.run.side_effect = [{}, error]
        with self.assertRaisesRegex(RecoveryError, "empty_target_preserved"):
            self.action(operation=op)
        self.assertEqual(self.action(status=True)["stage"], "rejected")
        self.resume.assert_called_once()
        self.auth.release_key.assert_called_once()

    def test_temporary_volume_requires_both_roles_project_and_this_web_operation(self):
        del self.auth.release_key
        name = "qa_restore_identity"
        labels = {"com.docker.compose.project":"qa", "org.gift-ledger.gui-v2-project":"qa",
                  "com.docker.compose.volume":"restore_identity", "org.gift-ledger.gui-v2-role":"restore_identity",
                  engine.KEY_LABEL: self.auth.key_operation}
        for bad in ({engine.KEY_LABEL:"5"*32}, {"com.docker.compose.project":"sibling"},
                    {"org.gift-ledger.gui-v2-role":"backup_identity"}):
            self.auth.docker.call.side_effect = [name, json.dumps([{"Name":name,"Driver":"local","Labels":dict(labels, **bad)}])]
            with self.subTest(bad=bad), self.assertRaisesRegex(RecoveryError, "restore_temporary_key_not_owned"):
                self.auth.release_key()
            self.assertFalse(any(row.args[:2] == ("volume","rm") for row in self.auth.docker.call.call_args_list))

    def test_native_stop_and_restart_require_distinct_acknowledged_proofs(self):
        op = self.eligible()
        record = restore_site.ops.read_json(self.state / "restore.json")
        self.auth.package = Mock()
        dock = self.auth.docker
        dock.call.return_value = json.dumps("local-test-engine")
        running = {r:True for r in self.ids}
        def inspect(cid):
            role = next(r for r,c in self.ids.items() if c*64 == cid)
            return dict(self.original[role], running=running[role])
        dock.inspect.side_effect = inspect
        events = Mock()
        events.prepare.return_value = {"initial_channel_verified":True}
        events.retire.return_value = {"retired":True}
        def lifecycle(role, value, up):
            running[role] = up
            return {"success":True,"event_barrier_verified":True}
        events.stop.side_effect = lambda r,b:lifecycle(r,b,False)
        events.start.side_effect = lambda r,b:lifecycle(r,b,True)
        with patch.object(engine.provider,"EventProvider",return_value=events), patch.object(engine.io,"checked_directory"), \
                patch.object(engine.dsm_backup,"EventBinding"), patch.object(engine.dsm_backup,"EventEnvironment"):
            with self.auth.lifecycle(self.profile,record) as driver:
                driver.call("stop","--time","30",self.ids["web"]*64)
                with self.assertRaisesRegex(engine.adapter.PlannedStopError, "adapter_worker_proof_required"):
                    driver.call("start",self.ids["web"]*64)
                self.auth.finish_observing(driver)
                record.update(stage="verified", result=self.sql_result(op))
                self.auth.allow_resume(driver,record,"verified")
                driver.call("start",self.ids["web"]*64)
        events.stop.assert_called_once()
        events.start.assert_called_once()
        self.assertFalse(any(x.args[0] in ("stop","start") for x in dock.call.call_args_list))


if __name__ == "__main__":
    unittest.main()
