import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SITE = Path(__file__).resolve().parents[2] / "deploy/single_yaml"
sys.path.insert(0, str(SITE))
spec = importlib.util.spec_from_file_location("installer_about", SITE / "site_about.py")
about = importlib.util.module_from_spec(spec)
spec.loader.exec_module(about)
BINDING = {"project":"test-family", "instance_id":"1d1a53b9-1ca8-4c41-9625-c7a21897a42d",
           "origin":"https://192.0.2.10:8443"}
OP = "a" * 32


class AboutSite(unittest.TestCase):
    def test_old_web_or_cli_journal_displays_only_this_completed_restore(self):
        target = dict(project=BINDING["project"], instance_id=BINDING["instance_id"], url=BINDING["origin"] + "/")
        record = dict(project=target["project"], stage="completed", operation=OP,
                      result=dict(target=target, full_database_and_app_files_verified=True, operation_id=OP,
                                  cash_amount_all_gift_records="567.89"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operation = root / OP
            operation.mkdir()
            proof = dict(project=target["project"], instance_id=target["instance_id"], action="restore",
                         operation_id=OP, success=True, completed_at="2026-10-03T08:00:00+08:00")
            (operation / "completed.json").write_text(json.dumps(proof))
            actual = about.summary(BINDING, record, historical=root)
            self.assertEqual(actual, dict(BINDING, restore_state="verified", restored_at="2026-10-03T00:00:00+00:00"))
            (operation / "completed.json").write_text(json.dumps(dict(proof,instance_id="wrong")))
            self.assertIsNone(about.summary(BINDING, record, historical=root)["restored_at"])
            self.assertEqual(about.summary(BINDING, dict(record,stage="applying"))["restore_state"], "unknown")
            self.assertEqual(about.summary(BINDING, dict(record,project="other"))["restore_state"], "unknown")
            self.assertEqual(about.summary(BINDING, None)["restore_state"], "none")
            for invalid in ([], False, dict(record, result="malformed"), dict(record, operation="invalid")):
                self.assertEqual(about.summary(BINDING, invalid)["restore_state"], "unknown")
            self.assertEqual(about.summary(BINDING, dict(record,stage="rejected"))["restore_state"], "none")

    def test_display_version_does_not_override_verified_runtime_format(self):
        repository = SITE.parents[1]
        template = (SITE / "docker-compose.yml").read_text()
        self.assertIn('GIFTLEDGER_BUILD_VERSION: "开发版"', template)
        self.assertNotIn("GIFT_LEDGER_APP_VERSION:", template)
        self.assertIn('app_version: str = "1.5.0"', (repository / "backend/app/core/config.py").read_text())
        self.assertIn('settings.app_version == "1.5.0"', (repository / "deploy/release/assets/verify_schema.py").read_text())


    def test_unavailable_about_store_cannot_abort_verified_restore_or_its_cleanup(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.object(about, "publish", side_effect=OSError("full")):
            with patch("sys.stderr") as err:
                about.update_for_restore(Path(tmp), BINDING["project"], {"target": {
                    "url": BINDING["origin"], "instance_id": BINDING["instance_id"]}})
                self.assertIn("关于本站", str(err.write.call_args_list))
