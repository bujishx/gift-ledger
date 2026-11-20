"""Exercise first install, reentry, upgrade and rollback of the owner's public files."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "deploy/single_yaml"), str(ROOT / "deploy/release/backup_ops")]
import backup_site
import owner_guide
import single_yaml_site as site


class SiteDocuments(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name).resolve() / "data"
        self.data.mkdir()
        for role in site.ROLES:
            (self.data / role).mkdir()
        self.project = "gift-test"
        self.instance = "ee25db16-1188-43bd-b56f-4f2015c144c7"
        self.url = "https://192.0.2.37:16445"
        self.cert = b"temporary test stand-in for a DER certificate"
        self.identity = {"project": self.project, "instance_id": self.instance,
                         "tls": {"profile": "site-ca-v1", "root_sha256": hashlib.sha256(self.cert).hexdigest()}}
        self.control = self.data / "root_config/site/control"
        self.control.mkdir(parents=True)
        (self.control / "identity.json").write_text(json.dumps(self.identity))
        layout = self.data / "root_config/layout"
        layout.mkdir()
        (layout / "prepare-completed.json").write_text("{}")
        (self.data / "root_config/maintenance").mkdir()
        patches = [patch.object(site, "DATA", self.data), patch.dict(os.environ, {
            "GIFTLEDGER_PROJECT": self.project, "GIFTLEDGER_SITE_IP": "192.0.2.37",
            "GIFTLEDGER_HTTPS_PORT": "16445", "GIFTLEDGER_OWNER_MAINTENANCE_GUIDE": "0"})]
        for change in patches:
            change.start()
            self.addCleanup(change.stop)

    def named(self, base):
        return self.data / owner_guide.public_name(self.project, self.instance, base)

    def publish(self):
        site.publish_public(self.identity, site.ROOT_CERTIFICATE, self.cert)
        site.publish_public(self.identity, site.ADDRESS, (self.url + "/\n").encode())
        site.publish_guide(self.project, self.url, self.identity)

    def rollback(self):
        with patch.object(sys, "argv", ["backup_site.py", "rollback-public-files"]):
            backup_site.main()

    def test_fresh_files_and_real_rollback_entry_are_repeatable(self):
        self.publish()
        names = set(site.ROLES) | {owner_guide.GUIDES}
        names.update(self.named(base).name for base in (site.ADDRESS, owner_guide.FILENAME, site.ROOT_CERTIFICATE))
        self.assertEqual({p.name for p in self.data.iterdir()}, names)
        site.check_directory()
        certificate = self.named(site.ROOT_CERTIFICATE)
        stamp = certificate.stat().st_mtime_ns
        self.publish()
        self.assertEqual(stamp, certificate.stat().st_mtime_ns)
        self.assertIn(certificate.name, self.named(owner_guide.FILENAME).read_text())
        self.assertEqual(set(os.listdir(self.data / owner_guide.GUIDES)), site.guide_names(self.project, self.instance))
        self.rollback()
        self.rollback()
        self.assertEqual(set(os.listdir(self.data)), set(site.ROLES) | {site.ADDRESS, owner_guide.FILENAME, site.ROOT_CERTIFICATE})
        self.assertEqual((self.data / site.ROOT_CERTIFICATE).read_bytes(), self.cert)
        self.assertFalse((self.data.parent / site.DOCUMENT_RECEIPT).exists())
        self.publish()
        site.check_directory()
        for name in (site.ADDRESS, owner_guide.FILENAME, site.ROOT_CERTIFICATE):
            self.assertEqual(self.named(name).read_bytes(), (self.data / name).read_bytes())

    def test_different_instances_do_not_share_a_public_name(self):
        self.publish()
        other = owner_guide.public_name(self.project, "2d30a7ab-4e4a-423b-9231-f59b819dafde", site.ADDRESS)
        (self.data / other).write_bytes(b"another instance\n")
        with self.assertRaisesRegex(site.SiteError, "其他文件"):
            site.check_directory()

    def test_rollback_preserves_owner_edits_and_refuses_extra_members(self):
        self.publish()
        doc = self.data / owner_guide.GUIDES / "安装前检查端口与网络.txt"
        original = doc.read_bytes()
        doc.write_bytes(b"notes added by owner")
        with self.assertRaisesRegex(site.SiteError, "已被编辑"):
            self.rollback()
        self.assertTrue(self.named(site.ADDRESS).is_file())
        self.assertEqual(doc.read_bytes(), b"notes added by owner")
        doc.write_bytes(original)
        foreign = doc.with_name("用户自己的笔记.txt")
        foreign.write_text("留存")
        with self.assertRaisesRegex(site.SiteError, "其他文件"):
            self.rollback()
        self.assertTrue(foreign.exists())

    def test_foreign_certificate_and_link_are_rejected(self):
        self.publish()
        cert = self.named(site.ROOT_CERTIFICATE)
        cert.write_bytes(b"other certificate")
        with self.assertRaisesRegex(site.SiteError, "指纹"):
            self.rollback()
        guide = self.named(owner_guide.FILENAME)
        guide.unlink()
        guide.symlink_to(cert)
        with self.assertRaises(site.SiteError):
            self.publish()


if __name__ == "__main__":
    unittest.main()
