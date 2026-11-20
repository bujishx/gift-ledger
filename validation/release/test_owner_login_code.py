"""Verify issuer binding, absence of secret output in noninteractive execution, and Ubuntu/DSM mounts."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "deploy/single_yaml"
sys.path.insert(0, str(SOURCE))
import owner_guide
if os.name == "posix":
    import owner_login_code as rescue

PROJECT = "gift-owner-qa"
INSTANCE = "0ac0fcfd-c8ee-4e5e-b6a9-a6aeb637b723"
URL = "https://192.0.2.20:16450"
STATION = Path("/volume1/docker/owner-qa")


class Guide(unittest.TestCase):
    def test_only_opted_in_guide_hashes_exact_shipped_helper(self):
        identity = {"instance_id": INSTANCE, "tls": {}}
        self.assertNotIn("owner_login_code.py", owner_guide.render(PROJECT, URL, identity))
        # In production prepare reads the bundled archive from its read-only image.
        # Build the same archive outside the source tree for a source checkout.
        import build_dsm_backup
        import build_maintenance_install
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / "dsm-backup.pyz"
            archive = Path(temp) / "maintenance-install.pyz"
            build_dsm_backup.build(backup)
            build_maintenance_install.build(backup, archive)
            installer_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            original = owner_guide.shipped_digest
            with patch.object(owner_guide, "shipped_digest",
                              side_effect=lambda name: installer_digest if name == archive.name else original(name)):
                guide = owner_guide.render(PROJECT, URL, identity, owner_login=True)
        self.assertIn(installer_digest, guide)
        for action in ("start", "status"):
            self.assertIn(f"/root/.giftledger-maintenance/{PROJECT}/maintenance-install.pyz {action} --project {PROJECT}", guide)
        self.assertIn(URL + "/maintenance", guide)
        for action in ("上传旧钥匙登记", "生成新钥匙登记", "查看备份结果", "下载已核验的备份 ZIP",
                       "上传并预检", "确认恢复到本站"):
            self.assertIn(action, guide)
        command = owner_guide.login_code_command(PROJECT, URL, INSTANCE)
        self.assertIn(command, guide)
        self.assertIn(hashlib.sha256((SOURCE / "owner_login_code.py").read_bytes()).hexdigest(), command)
        for field in (URL, INSTANCE, PROJECT, "/opt/gift-ledger-install/owner_login_code.py", "sha256sum -c -"):
            self.assertIn(field, command)
        if os.name == "posix":
            subprocess.run(["/bin/sh", "-n"], input=command, text=True, check=True)


@unittest.skipUnless(os.name == "posix", "Server command is for Linux")
class DockerContracts(unittest.TestCase):
    def setUp(self):
        self.site, self.ip, self.port = rescue.binding(PROJECT, INSTANCE, URL)
        self.env = [f"{k}={v}" for k, v in [
            ("GIFTLEDGER_PROJECT", PROJECT), ("GIFTLEDGER_SITE_IP", self.ip),
            ("GIFTLEDGER_HTTPS_PORT", self.port), ("GIFTLEDGER_MAINTENANCE_DIR", rescue.IPC_TARGET),
            ("GIFTLEDGER_OWNER_STATE_DIR", rescue.OWNER_TARGET),
        ]]
        self.prepare = {"State": {"Status": "exited", "ExitCode": 0},
                        "Config": {"Env": self.env, "Cmd":["prepare"]},
                        "HostConfig": {"ReadonlyRootfs": True}, "Mounts": [
                            {"Source": str(STATION), "Destination": "/station", "Type": "bind", "RW": True}]}
        self.api = {"State": {"Running": True}, "Config": {"User": "10001:0", "Env": self.env},
                    "HostConfig": {"Privileged": False, "ReadonlyRootfs": True}, "Mounts": [
                        {"Source": str(STATION / source), "Destination": dest, "Type": "bind", "RW": rw}
                        for source, dest, rw in [
                            (rescue.IPC, rescue.IPC_TARGET, False),
                            (rescue.IPC + "/requests", rescue.IPC_TARGET + "/requests", True),
                            (rescue.OWNER, rescue.OWNER_TARGET, True),
                            ("data/app_data", "/var/lib/gift-ledger", True),
                            ("data/app_secrets", "/run/secrets", False),
                        ]]}
        port = [{"HostIp": self.ip, "HostPort": self.port}]
        self.web = {"State": {"Running": True}, "HostConfig": {"PortBindings": {"443/": port}},
                    "NetworkSettings": {"Ports": {"443/tcp": port}}}

    def verify(self, api=None):
        rescue.verify_services(api or self.api, self.web, STATION, self.site, self.ip, self.port)

    def test_active_and_stopped_web_mappings(self):
        rescue.verify_prepare(self.prepare, STATION, self.site, self.ip, self.port)
        self.verify()
        self.web["State"]["Running"] = False
        self.web["NetworkSettings"]["Ports"] = {}
        self.verify()
        self.web["HostConfig"]["PortBindings"]["443/"][0]["HostPort"] = "16449"
        with self.assertRaises(rescue.RecoveryError):
            self.verify()

    def test_wrong_site_shadowed_code_or_secrets_rejected(self):
        cases = []
        for idx in range(len(self.api["Mounts"])):
            other = copy.deepcopy(self.api)
            other["Mounts"][idx]["Source"] += "-wrong"
            cases.append(other)
        other = copy.deepcopy(self.api)
        other["Mounts"][0]["RW"] = True
        cases.append(other)
        other = copy.deepcopy(self.api)
        other["Mounts"].append({"Destination": "/app/backend", "Type": "bind", "Source": "/tmp", "RW": True})
        cases.append(other)
        for data in cases:
            with self.subTest(mounts=data["Mounts"]), self.assertRaises(rescue.RecoveryError):
                self.verify(data)

    def test_wrong_origin_or_stopped_api_rejected(self):
        for origin in ("http://192.0.2.20:16450", "https://192.0.2.20:443", URL + "/", URL + "?x=1"):
            with self.assertRaises(rescue.RecoveryError):
                rescue.binding(PROJECT, INSTANCE, origin)
        self.api["State"]["Running"] = False
        with self.assertRaisesRegex(rescue.RecoveryError, "API"):
            self.verify()

    def test_noninteractive_issuer_stops_before_any_docker_call(self):
        argv = ["issue", "--project", PROJECT, "--instance", INSTANCE, "--origin", URL, "--station", str(STATION)]
        with patch.object(rescue.os, "geteuid", return_value=0), patch.object(rescue.sys, "flags") as flags, \
                patch.object(rescue.os, "isatty", return_value=False), patch.object(rescue, "docker_args") as docker:
            flags.isolated = True
            with patch.object(rescue.sys, "stderr") as err:
                self.assertEqual(rescue.main(argv), 1)
                self.assertIn("交互终端", str(err.write.call_args_list))
            docker.assert_not_called()


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: 1)() == 0 and sys.platform == "linux",
                     "Requires isolated Linux root filesystem")
class IdentityFilesystem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.station = Path(self.tmp.name)
        self.site = rescue.binding(PROJECT, INSTANCE, URL)[0]
        self.ipc = self.station / rescue.IPC
        self.ipc.mkdir(mode=0o750)
        self.ipc.chmod(0o750)
        self.owner = self.station / rescue.OWNER

    def configure(self, setup=True):
        return rescue.store_binding(self.station, self.site, setup=setup)

    def test_fresh_empty_site_setup_is_idempotent(self):
        ids = self.configure()
        self.assertEqual(self.configure(False), ids)
        self.assertEqual(self.configure(), ids)
        self.assertEqual((self.owner.stat().st_uid, self.owner.stat().st_mode & 0o777), (10001, 0o700))
        self.assertEqual((self.ipc / rescue.CONFIG).stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads((self.ipc / rescue.CONFIG).read_bytes()), self.site)
        with self.assertRaises(rescue.RecoveryError):
            rescue.store_binding(self.station, dict(self.site, origin="https://192.0.2.20:16449"), setup=True)
        self.assertEqual(self.configure(False), ids)

    def test_reject_existing_extra_acl_and_never_follow_swapped_owner(self):
        self.owner.mkdir()
        real = os.listxattr
        def with_acl(fd):
            if os.fstat(fd).st_ino == self.owner.stat().st_ino:
                return ["system.posix_acl_access"]
            return real(fd)
        with patch.object(os, "listxattr", side_effect=with_acl), patch.object(os, "removexattr") as remove:
            with self.assertRaisesRegex(rescue.RecoveryError, "额外授权"):
                self.configure()
            remove.assert_not_called()
        self.assertEqual(self.owner.stat().st_uid, 0)
        self.owner.rename(self.owner.with_name("other"))
        self.owner.symlink_to(self.owner.with_name("other"))
        with self.assertRaises(OSError):
            self.configure()
        self.assertFalse((self.ipc / rescue.CONFIG).exists())

    def test_existing_site_metadata_must_match(self):
        control = self.station / "data/root_config/site/control"
        control.mkdir(parents=True)
        (control / "identity.json").write_text(json.dumps(self.site))
        (control / "identity.json").chmod(0o600)
        addr = self.station / "data/网站地址.txt"
        addr.write_text(URL + "/\n")
        addr.chmod(0o644)
        rescue.verify_site_metadata(self.station, self.site)
        with self.assertRaises(rescue.RecoveryError):
            rescue.verify_site_metadata(self.station, dict(self.site, instance_id="wrong"))
        addr.write_text("https://192.0.2.20:16449/")
        with self.assertRaises(rescue.RecoveryError):
            rescue.verify_site_metadata(self.station, self.site)


if __name__ == "__main__":
    unittest.main()
