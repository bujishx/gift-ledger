"""A site's Ubuntu timer uses its trusted binding even when the working directory changes."""
from argparse import Namespace
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SITE = Path(__file__).resolve().parents[2] / "deploy/single_yaml"
sys.path.insert(0, str(SITE))
import maintenance_host as dispatcher
import owner_guide
import ubuntu_certificate_timer as timer


PROJECT = "ledger-two"
IMAGE = "sha256:" + "c" * 64
ROOT = "d" * 64
URL = "https://192.0.2.9:16445/"
INSTANCE = "6badcab5-e80c-486b-aba3-4604b680a67c"


class UbuntuCertificateTimerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        station = Path(tmp.name).resolve()
        self.cfg = {"project": PROJECT, "platform": "ubuntu", "archive": "/opt/ledger/backup.pyz",
                    "station": str(station), "station_inode": dispatcher.identity(station.stat()), "instance": INSTANCE}
        self.args = Namespace(state="/var/lib/gift ledger/100% $x", key_image=IMAGE, action="run", url=URL, root_sha256=ROOT)
        self.archive, self.checker = Mock(), Mock()
        self.checker.__file__ = self.cfg["archive"] + "/dsm_certificate.py"
        self.checker.NEEDS_COORDINATOR = 3
        self.checker.certificate_site.PYTHON = "/opt/venv/bin/python3.12"
        self.checker.certificate_site.SCRIPT = "/opt/gift-ledger-install/certificate_site.py"
        self.site = self.archive.HostSite.return_value
        self.site.project, self.site.station, self.site.coordinator = PROJECT, station, {"image": IMAGE}
        self.site.settings = {"GIFTLEDGER_PROJECT": PROJECT, "GIFTLEDGER_SITE_IP": "192.0.2.9", "GIFTLEDGER_HTTPS_PORT": "16445"}
        self.site.directories.return_value = (station / ".giftledger-backup-state", station / "backups")
        self.site.docker.argv.side_effect = lambda *args: ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args]
        self.site.docker.environment.return_value = {}
        self.archive.site.STATE = Path("/work/backup-state")
        self.archive.site.mount.side_effect = lambda kind, source, writable: (kind, str(source), writable)
        self.archive.site.mount_argument.side_effect = lambda target, bind: f"type={bind[0]},src={bind[1]},dst={target}"
        self.archive.site.single_yaml_site.site_url.return_value = URL[:-1]
        self.record = {"project": PROJECT, "instance_id": INSTANCE, "tls": {"root_sha256": ROOT}}
        self.archive.ops.read_json.return_value = self.record
        for scope in (patch.object(dispatcher, "read_config", return_value=self.cfg),
                      patch.object(dispatcher, "ubuntu_docker", return_value="/usr/bin/docker"),
                      patch.object(dispatcher, "archive_module", return_value=self.archive),
                      patch.dict(sys.modules, {"dsm_certificate": self.checker}), patch.dict(os.environ)):
            scope.start()
            self.addCleanup(scope.stop)

    def invoke(self):
        return timer.execute(dispatcher, self.args)

    def test_units_pin_site_url_image_and_ca_and_escape_literal_paths(self):
        self.args.action = "service-unit"
        with redirect_stdout(io.StringIO()) as text:
            self.assertEqual(self.invoke(), 0)
        service = text.getvalue()
        for expected in ('Type=oneshot', '"/var/lib/gift ledger/100%% $$x"',
                         f'"{URL}"', f'"{IMAGE}"', f'"{ROOT}"'):
            self.assertIn(expected, service)
        self.assertNotIn("docker-compose", service)
        self.assertNotIn("/bin/sh", service)
        self.args.action = "timer-unit"
        with redirect_stdout(io.StringIO()) as text:
            self.assertEqual(self.invoke(), 0)
        entry = text.getvalue()
        for expected in ("OnCalendar=*-*-* 03:15:00", "Persistent=true", "WantedBy=timers.target",
                         f"Unit=giftledger-certificate-{PROJECT}.service"):
            self.assertIn(expected, entry)
        self.assertNotEqual(entry, timer.timer_unit("second-project"))
        notes = owner_guide.render(PROJECT, URL[:-1], self.record)
        self.assertIn(f"sudo systemctl enable --now giftledger-certificate-{PROJECT}.timer", notes)
        self.assertIn(f"sudo journalctl -u giftledger-certificate-{PROJECT}.service", notes)

    def test_current_or_failed_probe_never_invokes_coordinator_or_accesses_backup_registration(self):
        with patch.object(timer.subprocess, "run") as run:
            for code in (0, 1):
                self.checker.run.return_value = code
                self.assertEqual(self.invoke(), code)
                self.checker.run.assert_called_with(self.site, URL, ROOT)
            run.assert_not_called()
            self.site.profile.assert_not_called()

    def test_due_uses_only_pinned_single_site_and_propagates_real_exit_status(self):
        self.checker.run.return_value = 3
        with patch.object(timer.subprocess, "run", return_value=SimpleNamespace(returncode=48)) as run:
            self.assertEqual(self.invoke(), 48)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "run"])
        self.assertEqual(argv[-4:], [self.checker.certificate_site.PYTHON, IMAGE, self.checker.certificate_site.SCRIPT, "auto-renew"])
        self.assertIn("--label=com.docker.compose.project=" + PROJECT, argv)
        self.assertIn("GIFTLEDGER_HTTPS_PORT=16445", argv)
        self.assertIn(f"type=bind,src={self.site.station}/.giftledger-backup-state,dst=/work/backup-state", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--network=none", argv)
        self.assertNotIn("compose", argv)

    def test_rejects_other_target_before_probe(self):
        for mapping, key, value, code in ((self.cfg, "platform", "dsm", "ubuntu_binding_required"),
                (self.cfg, "station_inode", [0, 0], "station_identity_changed"),
                (self.record, "instance_id", "another", "maintenance_instance_mismatch"),
                (self.site.coordinator, "image", "sha256:" + "1" * 64, "approved_backup_image_mismatch"),
                (self.record["tls"], "root_sha256", "0" * 64, "certificate_public_root_changed"),
                (vars(self.args), "url", "https://192.0.2.9:16443/", "certificate_site_port_mismatch")):
            with self.subTest(code=code), patch.dict(mapping, {key: value}), self.assertRaisesRegex(dispatcher.HostError, code):
                self.invoke()
        self.checker.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
