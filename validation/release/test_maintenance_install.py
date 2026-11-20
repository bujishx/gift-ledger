"""Verify the release's isolated site installer and offline rescue, without contacting a machine."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "deploy/single_yaml"
sys.path.insert(0, str(SITE))
import build_maintenance_install as builder
if os.name == "posix":
    import maintenance_install as install
    import single_yaml_site as site


class Package(unittest.TestCase):
    def test_deterministic_archive_includes_matching_scripts_and_checked_inner_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inner = base / "inner.pyz"
            inner.write_bytes(b"frozen test engine")
            a, b = base / "a.pyz", base / "b.pyz"
            builder.build(inner, a)
            builder.build(inner, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with zipfile.ZipFile(a) as archive:
                self.assertEqual(archive.read("dsm-backup.pyz"), inner.read_bytes())
                for name in ("maintenance_install.py", "maintenance_host.py", "owner_login_code.py", "ubuntu_certificate_timer.py"):
                    self.assertEqual(archive.read(name), (SITE / name).read_bytes())
            if os.name == "posix":
                result = subprocess.run([sys.executable, "-IB", str(a), "--help"], check=True, capture_output=True)
                self.assertIn(b"review-backup", result.stdout)


@unittest.skipUnless(os.name == "posix", "host requires Linux")
class Protocol(unittest.TestCase):
    def test_archive_mismatch_or_link_never_trusted(self):
        payload = b"fixed release content"
        def tar_writer(name, data, link=False):
            def run(argv, **kwargs):
                self.assertEqual(argv[-3:], ["cp", "a"*64+":/opt/gift-ledger-install/maintenance-install.pyz", "-"])
                with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    if link:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = "other.pyz"
                        entry.size = 0
                    archive.addfile(entry, io.BytesIO(data))
                return Mock(returncode=0)
            return run
        for name, data, linked, accepted in (
            (install.ARCHIVE, payload, False, True), (install.ARCHIVE, b"changed", False, False),
            ("other.pyz", payload, False, False), (install.ARCHIVE, b"", True, False)):
            with self.subTest(name=name, linked=linked), patch.object(install.subprocess, "run", side_effect=tar_writer(name,data,linked)):
                if accepted:
                    install.verify_image_copy(["/usr/bin/docker","--host","unix:///var/run/docker.sock"], "a"*64, payload)
                else:
                    with self.assertRaisesRegex(install.host.HostError, "prepare_install_archive_mismatch"):
                        install.verify_image_copy(["/usr/bin/docker"], "a"*64, payload)

    def test_rescue_is_offline_and_names_only_this_site_and_job(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(install, "accepting", return_value=False):
            root = Path(tmp)/"fixture"
            (root/"state").mkdir(parents=True)
            record = dict(project="fixture", instance="site", origin="https://192.0.2.2:8443")
            op = uuid4().hex
            (root/"state/result.json").write_text(json.dumps(dict(operation_id=op, state="needs_review", secret="discard")))
            (root/"state/enrollment.json").write_text(json.dumps(dict(public=dict(operation_id=op,state="needs_review"),private="private")))
            (root/"state/web-restore.json").write_text(json.dumps(dict(public=dict(operation_id=op,state="needs_review"))))
            report = install.status(root,record)
            for role in ("backup","enrollment","restore"):
                self.assertEqual(report[role]["review_command"], install.site_command(root, "review-"+role, op))
            self.assertNotIn("start_command",report)
            self.assertNotIn("secret",json.dumps(report))
            self.assertNotIn("private",json.dumps(report))
            with patch.object(install,"command") as cmd, self.assertRaisesRegex(install.host.HostError,"original_operation_requires_review"):
                install.start(root,record)
            cmd.assert_not_called()
            self.assertFalse(report["worker_listening"])

    def test_dsm_repeat_start_never_launches_an_extra_worker(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(install, "accepting", return_value=True), \
                patch.object(install.subprocess,"Popen") as launch:
            install.start(Path(tmp), {"platform":"dsm"})
            launch.assert_not_called()

    def test_dsm_boot_waits_for_engine_but_rejects_a_different_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            station = Path(tmp)
            record = {"station": tmp, "station_inode": install.host.identity(station.stat())}
            with patch.object(install.os, "access", return_value=True), patch.object(install.time, "sleep") as sleep, \
                    patch.object(install.subprocess, "run", side_effect=[
                        Mock(returncode=1), Mock(returncode=0, stdout=b"20.10.23\n")]) as probe:
                install.dsm_ready(record)
                self.assertEqual(probe.call_count, 2)
                sleep.assert_called_once()
                record["station_inode"] = [0, 0]
                with self.assertRaisesRegex(install.host.HostError, "station_directory_mismatch"):
                    install.dsm_ready(record)
                self.assertEqual(probe.call_count, 2)


@unittest.skipUnless(os.name=="posix" and getattr(os,"geteuid",lambda:1)()==0 and Path("/root").is_dir(),
                     "requires an isolated Linux root test container")
class Filesystem(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir="/root")
        self.addCleanup(temp.cleanup)
        self.home=Path(temp.name)

    def test_release_prepare_creates_required_api_mounts_before_exiting(self):
        data = self.home / "station/data"
        identity = data / "root_config/site/control/identity.json"
        identity.parent.mkdir(parents=True)
        identity.write_text(json.dumps({"project":"gift-qa","instance_id":"41fa55d3-9628-4a44-8093-6bb7cf316fe8","tls":{}}))
        (data / "root_config/maintenance").mkdir()
        cert = data / "tls_config/fullchain.pem"
        cert.parent.mkdir()
        cert.write_text("test certificate")
        env={"GIFTLEDGER_PROJECT":"gift-qa","GIFTLEDGER_SITE_IP":"192.0.2.12","GIFTLEDGER_HTTPS_PORT":"8443"}
        with patch.object(site,"DATA",data), patch.dict(os.environ,env), patch.object(sys,"argv",["site","prepare"]), \
                patch.dict(sys.modules,{"prepare_volumes":Mock()}), patch.object(site,"check_directory"), \
                patch.object(site,"publish_public"), patch.object(site,"publish_guide"):
            os.environ.pop("GIFTLEDGER_OWNER_MAINTENANCE_GUIDE", None)
            self.assertEqual(site.main(),0)
            ipc=data.parent/".giftledger-maintenance"
            self.assertFalse(ipc.exists())
            os.environ["GIFTLEDGER_OWNER_MAINTENANCE_GUIDE"]="1"
            self.assertEqual(site.main(),0)
            requests=ipc/"requests"
            owner=data.parent/".giftledger-owner"
            for path in (ipc,requests,owner):
                self.assertEqual((path.stat().st_uid,stat.S_IMODE(path.stat().st_mode)),(0,0o700))
            ipc.chmod(0o750)
            requests.chmod(0o770)
            os.chown(owner,10001,0)
            value=owner/"existing-owner-state"
            value.write_bytes(b"keep")
            previous=[p.stat() for p in (ipc,requests,owner)]
            self.assertEqual(site.main(),0)
            for path,info in zip((ipc,requests,owner),previous):
                now=path.stat()
                self.assertEqual((now.st_ino,now.st_uid,now.st_mode),(info.st_ino,info.st_uid,info.st_mode))
            self.assertEqual(value.read_bytes(),b"keep")

    def test_prepare_refuses_foreign_directory_or_link_without_changing_it(self):
        station=self.home/"station"
        station.mkdir()
        ipc=station/".giftledger-maintenance"
        ipc.mkdir()
        (ipc/"foreign").write_bytes(b"keep")
        with patch.object(site, "DATA", station/"data"):
            with self.assertRaisesRegex(site.SiteError,"缺少请求文件夹"):
                site.prepare_maintenance_mounts()
            self.assertFalse((ipc/"requests").exists())
            self.assertEqual((ipc/"foreign").read_bytes(), b"keep")
            (ipc/"foreign").unlink()
            (ipc/"requests").symlink_to(self.home)
            with self.assertRaises(OSError):
                site.prepare_maintenance_mounts()
            self.assertFalse((station/".giftledger-owner").exists())

    def test_first_install_seals_only_empty_compose_mounts_with_inherited_acl(self):
        station = self.home / "station"
        access = station / install.host.IPC
        requests = access / "requests"
        requests.mkdir(parents=True)
        private = station / install.owner.OWNER
        private.mkdir()
        # Give the three binds and their parent an actual Linux inheritable access entry for uid 4242.
        acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", kind, perm, ident) for kind, perm, ident in (
            (1, 7, 0xffffffff), (2, 7, 4242), (4, 5, 0xffffffff), (16, 7, 0xffffffff), (32, 0, 0xffffffff)))
        for path in (station, access, requests, private):
            for attr in ("system.posix_acl_access", "system.posix_acl_default"):
                os.setxattr(path, attr, acl)
        record = {"station": str(station), "station_inode": install.host.identity(station.stat())}
        foreign = private / "existing"
        foreign.write_bytes(b"keep")
        with self.assertRaisesRegex(install.host.HostError, "already_in_use"):
            install.bootstrap_mounts(record)
        self.assertEqual(os.getxattr(private, "system.posix_acl_default"), acl)
        self.assertEqual(foreign.read_bytes(), b"keep")
        foreign.unlink()
        install.bootstrap_mounts(record)
        for path in (access, requests, private):
            self.assertFalse(any("acl" in s for s in os.listxattr(path)))
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(os.getxattr(station, "system.posix_acl_default"), acl)
        os.setxattr(private, "user.fixture_unknown_acl", b"preserve")
        with self.assertRaisesRegex(install.host.HostError, "unexpected_maintenance_mount_acl"):
            install.bootstrap_mounts(record)
        self.assertEqual(os.getxattr(private, "user.fixture_unknown_acl"), b"preserve")

    def test_repeat_install_adopts_identical_content_and_rejects_changed_or_linked_units(self):
        target=self.home/"giftledger-maintenance-test.service"
        install.same_file(target,b"first",0o644)
        original=target.stat().st_ino
        install.same_file(target,b"first",0o644)
        self.assertEqual(target.stat().st_ino,original)
        with self.assertRaisesRegex(install.host.HostError,"installed_file_conflict"):
            install.same_file(target,b"other",0o644)
        self.assertEqual(target.read_bytes(),b"first")
        other=self.home/"foreign"
        other.symlink_to(target)
        with self.assertRaises(OSError):
            install.same_file(other,b"changed",0o644)
        self.assertEqual(target.read_bytes(),b"first")
        os.link(target,self.home/("." + target.name + ".giftledger-installing"))
        install.same_file(target,b"first",0o644)
        self.assertEqual(target.stat().st_nlink,1)

    def test_install_can_finish_partial_initialization_and_repeat_while_worker_is_locked(self):
        inner=self.home/"inner.pyz"
        inner.write_bytes(b"pinned fake engine")
        package=self.home/"release.pyz"
        builder.build(inner,package)
        station=self.home/"station"; station.mkdir()
        root=self.home/"gift-qa"; root.mkdir(mode=0o700)
        (root/"install.lock").touch(mode=0o600)
        args=argparse.Namespace(project="gift-qa",instance=str(uuid4()),origin="https://192.0.2.2:8443",station=str(station))
        record=dict(project=args.project,instance=args.instance,origin=args.origin,station=args.station,
                    station_inode=install.host.identity(station.stat()), platform="ubuntu", image="sha256:"+"a"*64,
                    installer_sha256=hashlib.sha256(package.read_bytes()).hexdigest())
        archive=Mock()
        coordinator=archive.HostSite.return_value
        coordinator.coordinator={"image":record["image"]}
        coordinator.runtime_profile.return_value={"maintenance_image":record["image"]}
        with patch.object(install,"discover",side_effect=lambda *args:dict(record)), \
                patch.object(install.host,"archive_module",return_value=archive), \
                patch.object(install.host,"ubuntu_docker",return_value="/usr/bin/docker"), \
                patch.object(install,"units") as units,patch.object(install,"start") as start:
            with patch.object(install.owner,"store_binding",side_effect=RuntimeError("fixture interrupted")):
                with self.assertRaisesRegex(RuntimeError,"fixture interrupted"):
                    install.install(root,args,package)
            units.assert_not_called()
            identity=(root/"state/binding.json").read_bytes()
            (station/".giftledger-maintenance/owner.sha256").unlink()
            with (root/"state/worker.lock").open("rb") as held:
                install.fcntl.flock(held.fileno(),install.fcntl.LOCK_EX)
                install.install(root,args,package)
                install.install(root,args,package)
            self.assertEqual((root/"state/binding.json").read_bytes(),identity)
            self.assertEqual(start.call_count,2)
            owner=(station/".giftledger-maintenance/owner-site.json")
            self.assertEqual(json.loads(owner.read_bytes()),dict(project=args.project,instance_id=args.instance,origin=args.origin))
            self.assertFalse((root/"state/owner.token").exists())
            record["instance"]=str(uuid4())
            with self.assertRaisesRegex(install.host.HostError,"existing_site_or_version_differs"):
                install.install(root,args,package)
            self.assertEqual((root/"state/binding.json").read_bytes(),identity)

    def test_same_site_upgrade_can_resume_partial_tool_replacement_with_owner_identity_intact(self):
        root = self.home/"fixture"; root.mkdir(mode=0o700)
        state = root/"state"; state.mkdir(mode=0o700); (state/"worker.lock").touch(mode=0o600)
        station = self.home/"station"; station.mkdir(mode=0o700)
        (station/".giftledger-backup-state").mkdir(mode=0o700)
        def version(tag):
            inner = self.home/("inner-"+tag)
            inner.write_bytes(tag.encode())
            package = self.home/(tag+".pyz"); builder.build(inner,package)
            data, members = install.read_package(package)
            record = dict(project="fixture",instance=instance,origin="https://192.0.2.41:9443",station=str(station),
                          station_inode=install.host.identity(station.stat()),platform="dsm",image="sha256:"+tag*64,
                          archive_sha256=hashlib.sha256(inner.read_bytes()).hexdigest(),
                          installer_sha256=hashlib.sha256(data).hexdigest())
            return record, data, members
        instance = str(uuid4())
        previous, original, older = version("a")
        record, data, members = version("b")
        for name, raw in {install.ARCHIVE:original,**older}.items():
            install.same_file(root/name,raw,0o400)
        binding = {k:previous[k] for k in ("project","instance","station","station_inode")}
        binding.update(archive=str(root/"dsm-backup.pyz"),sha256=previous["archive_sha256"],owner_digest="f"*64)
        install.host.atomic_file(state/"binding.json",install.host.encoded(binding),0o600)
        install.host.atomic_file(root/"installation.json",install.host.encoded(previous),0o600)
        actual = install.replace_owned
        # A stale DSM registration was rejected before a worker existed. A host
        # upgrade may preserve the owner's existing staged key and its test attempt.
        work = station / ".giftledger-backup-state"; trial = uuid4().hex
        (work/"runs"/trial).mkdir(parents=True)
        change = dict(project="fixture",instance=instance,operation=trial,stage="testing")
        install.host.atomic_file(work/"key-change.json",install.host.encoded(change),0o600)
        failed = dict(project="fixture",instance_id=instance,session_id=trial,stage="preflight-failed")
        journal = work/"runs"/trial/"run.json"
        install.host.atomic_file(journal,install.host.encoded(dict(failed,worker_id="a"*64)),0o600)
        with self.assertRaisesRegex(install.host.HostError,"finish_site_tasks_before_upgrade"):
            install.upgrade_installed(root,previous,record,data,members)
        install.host.atomic_file(journal,install.host.encoded(failed),0o600)
        def interrupted(path, before, after, mode):
            actual(path,before,after,mode)
            if path.name == "dsm-backup.pyz":
                raise RuntimeError("fixture power loss")
        with patch.object(install, "worker_pids", return_value=[]), patch.object(install, "replace_owned", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "power loss"):
                install.upgrade_installed(root,previous,record,data,members)
        self.assertEqual(install.read_record(root),previous)
        with patch.object(install, "worker_pids", return_value=[]):
            install.upgrade_installed(root,previous,record,data,members)
        self.assertEqual(install.read_record(root),record)
        self.assertEqual(json.loads((work/"key-change.json").read_bytes()),change)
        self.assertEqual(install.host.read_config(state),dict(binding,sha256=record["archive_sha256"]))
        self.assertEqual((root/("previous-"+previous["installer_sha256"])/install.ARCHIVE).read_bytes(),original)

    def test_foreign_installation_rejected_before_initializing_site(self):
        pkg=self.home/"release.pyz"
        inner=self.home/"inner.pyz"; inner.write_bytes(b"fixture")
        builder.build(inner,pkg)
        root=self.home/"site"; root.mkdir()
        (root/"foreign.file").write_bytes(b"keep")
        args=argparse.Namespace(project="site",instance=str(uuid4()),origin="https://192.0.2.1",station=str(self.home))
        with patch.object(install,"discover",return_value={}),patch.object(install.host,"init") as init:
            with self.assertRaisesRegex(install.host.HostError,"host_install_directory_in_use"):
                install.install(root,args,pkg)
        init.assert_not_called()
        self.assertEqual((root/"foreign.file").read_bytes(),b"keep")


if __name__ == "__main__":
    unittest.main()
