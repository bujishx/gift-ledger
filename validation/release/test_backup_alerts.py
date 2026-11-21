"""Local, site-scoped reminders and opt-in delivery. No outgoing network in tests."""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


@unittest.skipUnless(os.name == "posix", "host maintenance runs on Linux")
class BackupAlerts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).resolve().parents[2] / "deploy/single_yaml"
        for name in ("maintenance_host", "owner_login_code", "maintenance_install"):
            spec = importlib.util.spec_from_file_location(name, folder / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, getattr(cls, "modules", {})):
                spec.loader.exec_module(module)
            cls.modules = {**getattr(cls, "modules", {}), name: module}
        cls.host = cls.modules["maintenance_host"]
        cls.install = cls.modules["maintenance_install"]
        cls.now = datetime.datetime(2026, 11, 21, 8, tzinfo=datetime.timezone.utc)

    def test_freshness_after_failed_later_attempt_and_after_recovery(self):
        recent = self.now - datetime.timedelta(hours=1)
        boundary = self.now - datetime.timedelta(days=7)
        result = self.host.backup_health(
            {"backup_state":"failed","backup_checked_at":recent.isoformat()},
            {"state":"verified","at":boundary.isoformat()}, {}, 7, self.now)
        self.assertEqual(result["issues"], ["failed","overdue"])
        self.assertEqual(result["last_verified_at"], boundary.isoformat())
        current = {"backup_state":"succeeded","backup_checked_at":self.now.isoformat(),"decryption_verified":True}
        before = {"state":"needs_review","observed_at":recent.isoformat()}
        self.assertEqual(self.host.backup_health(current, {}, before, 7, self.now)["issues"], [])
        self.assertEqual(self.host.backup_health({}, {"state":"none"}, {}, 7, self.now)["issues"], ["never"])
        self.assertEqual(self.host.backup_health({}, {}, {}, 7, self.now)["issues"], ["unknown"])

    def test_delivery_pins_https_to_configured_host_and_posts_no_backup_fields(self):
        mocked = MagicMock()
        mocked.getresponse.return_value.status = 204
        with patch.object(self.install.http.client, "HTTPSConnection", return_value=mocked) as conn:
            self.install.send_heartbeat("", False)
            conn.assert_not_called()
            self.install.send_heartbeat("https://monitor.test/ping/fixture", True)
            conn.assert_called_once_with("monitor.test", timeout=8)
            mocked.request.assert_called_once_with("POST", "/ping/fixture/fail", body=b"")
            mocked.getresponse.return_value.status = 302
            with self.assertRaises(OSError):
                self.install.send_heartbeat("https://monitor.test/ping/fixture", False)
        for value in ("http://monitor.test/x", "https://user:pw@monitor.test/x", "https://monitor.test/x?q=1"):
            with self.assertRaises(self.host.HostError):
                self.install.heartbeat_url(value)

    def test_scheduled_delivery_state_is_not_repeated_until_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            health = {"issues":["failed"],"last_verified_at":None}
            with (patch.object(self.host, "backup_alert_settings", return_value={"days":7,"heartbeat":""}),
                  patch.object(self.host, "read_config", return_value={}),
                  patch.object(self.host, "owner_snapshot", return_value={"backup_health":health}),
                  patch.object(self.install, "verified_bytes", side_effect=lambda path, mode:path.read_bytes()),
                  patch.object(self.install.time, "time", side_effect=[100000,100020,190000,190020]),
                  patch("builtins.print")):
                self.assertEqual(self.install.check_backup_alerts(root, True), 1)
                self.assertEqual(self.install.check_backup_alerts(root, True), 0)
                self.assertEqual(self.install.check_backup_alerts(root, True), 1)
                health["issues"] = []
                self.assertEqual(self.install.check_backup_alerts(root, True), 0)
                self.assertEqual(json.loads((state/"backup-alert-last.json").read_text())["issues"], [])

    def test_bound_native_backup_fails_before_journal_and_concurrent_attempt_is_ignored(self):
        from types import SimpleNamespace

        folder = Path(__file__).resolve().parents[2] / "deploy"
        with patch.object(sys, "path", [str(folder / "single_yaml"), str(folder / "release/backup_ops"), *sys.path]):
            import backup_site
        ops = backup_site.ops
        with tempfile.TemporaryDirectory() as tmp:
            area = Path(tmp).resolve()
            state, store = area / "state", area / "store"
            state.mkdir()
            store.mkdir()
            identity = {"project": "demo", "instance_id": "test", "server_uuid": "server"}
            profile = dict(identity, state_dir=str(state), store_dir=str(store))
            site = SimpleNamespace(registration=lambda: identity, directories=lambda: (state, store),
                                   validate=lambda value: value)
            filename = state / "last-failed-backup-attempt.json"
            # Hold the real coordinator lock. A duplicate command remains a refusal, no failure receipt.
            with ops.host_lock(state), self.assertRaisesRegex(ops.BackupOpsError, "another_run_is_active"):
                backup_site.run_backup(site, profile, object(), None)
            self.assertFalse(filename.exists())
            with self.assertRaisesRegex(ops.BackupOpsError, "fixture_refusal"):
                with backup_site.observe_backup_attempt(site, "different_instance"):
                    raise ops.BackupOpsError("fixture_refusal")
            self.assertFalse(filename.exists())
            with patch.object(ops.shutil, "disk_usage", return_value=SimpleNamespace(free=100)), patch.object(ops, "utcnow", return_value=self.now):
                with self.assertRaisesRegex(ops.BackupOpsError, "insufficient_disk_space"):
                    backup_site.run_backup(site, profile, object(), None)
            self.assertFalse((state / "last-result.json").exists())
            self.assertEqual(list((state / "runs").iterdir()), [])
            failed = json.loads(filename.read_text())
            self.assertEqual(failed, dict(identity, attempted_at=self.now.isoformat()))
            before = self.now - datetime.timedelta(days=1)
            after = self.now + datetime.timedelta(microseconds=1)
            clock = after + datetime.timedelta(seconds=1)
            for success, expected in ((before, ["failed"]), (after, [])):
                health = self.host.backup_health(
                    {"failed_attempt_at": failed["attempted_at"]},
                    {"state": "verified", "at": success.isoformat()}, {}, 7, clock)
                self.assertEqual(health["issues"], expected)
