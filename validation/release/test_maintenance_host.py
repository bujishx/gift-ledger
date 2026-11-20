"""Site-scoped dispatch, lifecycle selection, and durable evidence on DSM and Ubuntu."""
import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import zipfile

if os.name == "posix":
    import importlib.util
    script = Path(__file__).resolve().parents[2] / "deploy/single_yaml/maintenance_host.py"
    spec = importlib.util.spec_from_file_location("maintenance_host", script)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)


@unittest.skipUnless(os.name == "posix", "DSM requires POSIX file descriptors")
class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "private"
        self.state.mkdir()
        (self.state / "jobs").mkdir()
        for name in ("requests", "status"):
            (self.root / name).mkdir()
        self.op = uuid4().hex
        self.cfg = {"project": "fixture", "station": str(self.root), "instance": str(uuid4())}
        self.cfg["archive"] = str(self.root / "backup.pyz")
        handles = {key: os.open(str(self.root / key), os.O_RDONLY | os.O_DIRECTORY)
                   for key in ("requests", "status")}
        self.addCleanup(lambda: [os.close(fd) for fd in handles.values()])
        self.enter(patch.object(host, "read_config", return_value=self.cfg))
        handles['base'] = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
        self.enter(patch.object(host, "ipc_handles", side_effect=lambda _: contextlib.nullcontext(handles)))
        self.enter(patch.object(host, "API_UID", os.geteuid()))
        if os.geteuid() != 0:
            self.enter(patch.object(host.os, "fchown"))
        self.handles = handles
        self.marker = self.root / "requests" / host.REQUEST
        host.save(self.state, handles, host.result(self.cfg, None, "idle", "ready"))

    def enter(self, manager):
        self.addCleanup(manager.stop)
        return manager.start()

    def test_first_verified_backup_survives_later_attempts_and_archive_retention(self):
        state = self.root / ".giftledger-backup-state"
        binding = {"project": self.cfg["project"], "instance_id": self.cfg["instance"],
                   "server_uuid": str(uuid4())}
        manifests = state / "bundle-manifests"
        manifests.mkdir(parents=True)
        root_fd = self.handles["base"]

        def read(folder, name, limit):
            return (self.root / folder / name).read_bytes()

        def add(bid, when, target, stage="finished"):
            folder = state / "runs" / bid
            folder.mkdir(parents=True)
            run = dict(target, session_id=bid, backup_id=bid, verify_id=uuid4().hex,
                       bundle_id=bid, stage=stage, private_cleanup_pending=False)
            (folder / "run.json").write_text(json.dumps(run))
            (manifests / (bid + ".json")).write_text(json.dumps(dict(
                target, backup_operation_id=bid, verify_operation_id=run["verify_id"], completed_at=when)))

        def first():
            return host.first_verified_backup(root_fd, binding, read, lambda date: date)

        self.assertEqual(first(), {"state": "none"})
        add("a" * 32, "2026-11-01T07:00:00+00:00", dict(binding, instance_id=str(uuid4())))
        add("b" * 32, "2026-11-02T07:00:00+00:00", binding, "verified")
        self.assertEqual(first(), {"state": "none"})
        add("c" * 32, "2026-11-03T07:00:00+00:00", binding)
        add("d" * 32, "2026-11-04T07:00:00+00:00", binding)
        proof = first()
        self.assertEqual(proof, {"state":"verified", "at":"2026-11-03T07:00:00+00:00", "backup_id":"c" * 32})
        (manifests / ("c" * 32 + ".json")).unlink()
        with patch.object(host.os, "listdir", side_effect=AssertionError("rescanned archived history")):
            self.assertEqual(first(), proof)
        (state / "first-verified-backup.json").unlink()
        self.assertEqual(first(), {"state":"verified"})  # Historical timestamp unavailable.
        (state / "first-verified-backup.json").write_text(json.dumps(dict(
            binding, instance_id=str(uuid4()), first=proof)))
        with self.assertRaisesRegex(host.HostError, "foreign_first_backup"):
            first()

    def submit(self, contents=None):
        self.marker.write_text(contents or self.op + "\n")
        self.marker.chmod(0o600)

    def latest(self):
        value = json.loads((self.state / "result.json").read_text())
        self.assertEqual(value, json.loads((self.root / "status/result.json").read_text()))
        return value

    @unittest.skipUnless(os.geteuid() == 0, "uses root-owned synthetic host receipts")
    def test_snapshot_binds_disk_tls_and_verified_backup_and_excludes_private_fields(self):
        self.cfg["station_inode"] = host.identity(self.root.stat())
        for folder in ("data/mysql_data", "data/tls_config", "data/root_config/site/control", "backups",
                       ".giftledger-backup-state/runs/" + self.op):
            (self.root / folder).mkdir(mode=0o700, parents=True)
        engine = self.root / ".giftledger-backup-state"
        engine.chmod(0o700)
        leaf = b"fixture-public-certificate-bytes"
        actual = "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(leaf).decode() + "\n-----END CERTIFICATE-----\n"
        (self.root / "data/tls_config/fullchain.pem").write_text(actual)
        (self.root / "data/root_config/site/control/identity.json").write_text(json.dumps({
            "project": self.cfg["project"], "instance_id": self.cfg["instance"], "extra": "private-field",
            "tls": {"not_after": "2032-04-03T17:00:00Z", "certificate_sha256": hashlib.sha256(leaf).hexdigest()}}))
        backup = uuid4().hex
        identity = dict(project=self.cfg["project"], instance_id=self.cfg["instance"], session_id=self.op)
        path = engine / "runs" / self.op / "run.json"
        path.write_text(json.dumps(dict(identity, stage="finished", bundle_id=backup, backup_id=backup)))
        (engine / "last-result.json").write_text(json.dumps(dict(identity, bundle_id=backup, success=True,
            services_restored=True, local_verified=True, private_cleanup_pending=False,
            completed_at="2031-04-03T17:00:00Z", private_detail="private-field")))
        with patch.object(host.subprocess, "run", return_value=SimpleNamespace(returncode=0,
                        stdout=b"/fixture-web-1 fixture web running healthy\n")):
            info = host.owner_snapshot(self.cfg)
            self.assertEqual(info["certificate_expires_at"], "2032-04-03T17:00:00+00:00")
            self.assertEqual((info["web_status"], info["backup_state"], info["backup_id"]), ("ok", "succeeded", backup))
            self.assertTrue(info["decryption_verified"])
            self.assertNotIn("private-field", str(info))
            for name in ("ledger_free_bytes", "backup_free_bytes"):
                self.assertGreaterEqual(info[name], 0)
            path.write_text(json.dumps(dict(identity, instance_id=str(uuid4()))))
            (self.root / "data/tls_config/fullchain.pem").write_text("unrelated")
            info = host.owner_snapshot(self.cfg)
            self.assertNotIn("backup_state", info)
            self.assertNotIn("certificate_expires_at", info)

    def test_unavailable_optional_overview_does_not_interrupt_a_backup_receipt(self):
        value = host.result(self.cfg, self.op, "failed", "synthetic_preflight_failed")
        publish = host.publish
        def reject_optional(directory, target, content):
            if target == "backup-observation.json":
                raise OSError("unavailable optional observation")
            return publish(directory, target, content)
        with patch.object(host, "publish", side_effect=reject_optional):
            host.save(self.state, self.handles, value)
        self.assertEqual(self.latest(), value)

    def test_download_zip_only_replaces_the_previous_file_after_all_five_hashes_match(self):
        bundle_id = uuid4().hex
        bundle = self.root / bundle_id
        bundle.mkdir()
        raw = {"backup-" + bundle_id + ".glgui.age": b"synthetic cipher", "backup.json": b"backup", "verify.json": b"verify"}
        sha = lambda b: hashlib.sha256(b).hexdigest()
        store = Mock()
        store.digest = sha
        store.canonical = lambda m: json.dumps(m, sort_keys=True).encode()
        hashes = {k:sha(v) for k,v in raw.items()}
        manifest = {"backup_operation_id": bundle_id, "files": hashes}
        raw["BUNDLE.json"] = store.canonical(manifest)
        sums = dict(hashes, **{"BUNDLE.json":sha(raw["BUNDLE.json"])})
        raw["SHA256SUMS"] = "".join(v + "  " + k + "\n" for k,v in sorted(sums.items())).encode()
        for k,v in raw.items():
            (bundle / k).write_bytes(v)
        out = self.root / "status/download.zip"
        size = host.publish_download(self.handles["status"], bundle, manifest, store)
        self.assertEqual(out.stat().st_size, size)
        with zipfile.ZipFile(out) as archive:
            self.assertEqual({k:archive.read(k) for k in archive.namelist()}, raw)
        initial = out.read_bytes()
        (bundle / "verify.json").write_bytes(b"changed")
        with self.assertRaises(host.HostError):
            host.publish_download(self.handles["status"], bundle, manifest, store)
        self.assertEqual(out.read_bytes(), initial)
        self.assertFalse((out.parent / "download.part").exists())

    def test_mismatch_fails_before_pinned_code_is_loaded(self):
        self.submit()
        with patch.object(host, "archive_module") as load:
            self.assertEqual(host.serve(self.state, 1, True, "/wrong-test-site"), 1)
            load.assert_not_called()
        self.assertEqual(self.latest(), host.result(self.cfg, self.op, "failed", "station_directory_mismatch"))
        self.assertFalse(self.marker.exists())

    def test_vendor_code_is_readable_but_arbitrary_messages_are_private(self):
        self.assertEqual(host.machine_code(ValueError("CM_vendor_file_set_unreviewed"), "failed"),
                         "CM_vendor_file_set_unreviewed")
        self.assertEqual(host.machine_code(ValueError("unexpected file /private/site_name"), "failed"),
                         "failed")

    def test_exact_uuid_regular_file_required(self):
        for contents in ("/var/lib/anything\n", self.op.upper() + "\n"):
            self.submit(contents)
            with self.assertRaises(host.HostError):
                host.request_id(self.handles["requests"])
            self.marker.unlink()
        unrelated = self.root / "unrelated"
        unrelated.write_text(self.op + "\n")
        self.marker.symlink_to(unrelated)
        with self.assertRaises(host.HostError):
            host.request_id(self.handles["requests"])
        self.assertTrue(unrelated.exists())
        self.marker.unlink()
        self.submit()
        self.marker.chmod(0o644)
        with self.assertRaisesRegex(host.HostError, "invalid_request_owner"):
            host.request_id(self.handles["requests"])

    def test_process_interruption_blocks_replay(self):
        self.submit()
        archive = Mock()
        archive.run.side_effect = SystemExit(2)
        with patch.object(host, "preflight", return_value=(archive, None, archive.run)):
            with self.assertRaises(SystemExit):
                host.serve(self.state, 1, True)
            self.assertEqual(host.serve(self.state, 1, True), 1)
        self.assertEqual(archive.run.call_count, 1)
        self.assertEqual(self.latest()["state"], "needs_review")
        self.assertEqual(self.latest()["code"], "host_worker_interrupted")
        self.assertTrue(self.marker.exists())
        self.assertEqual(host.serve(self.state, 1, False, persistent=True), 78)
        self.assertEqual(archive.run.call_count, 1)

    def test_flock_excludes_another_consumer(self):
        self.submit()
        with (self.state / "worker.lock").open("a+b") as other:
            host.fcntl.flock(other.fileno(), host.fcntl.LOCK_EX)
            with self.assertRaisesRegex(host.HostError, "host_worker_busy"):
                host.serve(self.state, 1, True)
        self.assertEqual(self.latest()["state"], "idle")
        self.assertTrue(self.marker.exists())

    def test_zero_exit_without_fresh_and_matching_evidence_never_succeeds(self):
        self.submit()
        archive = Mock()
        old = uuid4().hex
        archive.run.return_value = self.root / "backups/artifacts" / old
        with patch.object(host, "preflight", return_value=(archive, old, archive.run)):
            self.assertEqual(host.serve(self.state, 1, True), 1)
        self.assertEqual(self.latest()["state"], "needs_review")
        self.assertEqual(self.latest()["code"], "fresh_backup_receipt_missing")
        self.assertTrue(self.marker.exists())

    def test_verifies_bundle_and_service_restore_receipt_before_publication(self):
        self.submit()
        archive = Mock()
        backup = uuid4().hex
        archive.run.return_value = self.root / "backups/artifacts" / backup
        manifest = {"project": "fixture", "instance_id": self.cfg["instance"], "server_uuid": str(uuid4()),
                    "backup_operation_id": backup}
        archive.ops.backup_store.inspect_bundle.return_value = manifest
        archive.ops.read_json.return_value = {**manifest, "bundle_id": backup, "success": True,
                                              "services_restored": True, "local_verified": True,
                                              "private_cleanup_pending": False}
        session = uuid4().hex
        archive.ops.read_json.return_value["session_id"] = session
        with patch.object(host, "preflight", return_value=(archive, uuid4().hex, archive.run)), \
                patch.object(host.secrets, "token_hex", return_value=session):
            self.assertEqual(host.serve(self.state, 1, True), 0)
            archive.run.assert_called_once_with()
        self.assertEqual(self.latest(), host.result(self.cfg, self.op, "succeeded", "backup_verified",
                                                  backup_id=backup, decryption_verified=True))
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(list((self.state / "jobs").iterdir())), 1)
        archive.ops.read_json.return_value["services_restored"] = False
        with self.assertRaisesRegex(host.HostError, "backup_receipt_mismatch"):
            host.receipt(self.cfg, archive.run.return_value, None, archive)

    def test_legacy_dsm_binding_never_falls_back_to_ubuntu(self):
        archive = Mock()
        archive.target.Package.discover.return_value.target = Path("/trusted/package")
        archive.HostSite.return_value.settings = {}
        archive.HostSite.return_value.directories.return_value = (self.root / "state", self.root / "backups")
        archive.HostSite.return_value.profile.return_value = {"instance_id": self.cfg["instance"]}
        archive.ops.run_records.return_value = []
        with patch.object(host, "archive_module", return_value=archive), patch.object(host, "ubuntu_docker") as linux:
            _, _, run = host.preflight(self.cfg, None)
            run()
            archive.run.assert_called_once_with("fixture", str(self.root), expected_instance=self.cfg["instance"])
            archive.target.Package.discover.side_effect = ValueError("CM_vendor_file_set_unreviewed")
            with self.assertRaisesRegex(ValueError, "CM_vendor"):
                host.preflight(self.cfg, None)
            linux.assert_not_called()

    def test_ubuntu_uses_pinned_release_worker_and_checks_live_web_mapping_before_execution(self):
        self.cfg["platform"] = "ubuntu"
        archive = Mock()
        site = archive.HostSite.return_value
        image = "sha256:" + "a" * 64
        site.settings = {"GIFTLEDGER_HTTPS_PORT": "16450"}
        site.directories.return_value = (self.root / "state", self.root / "backups")
        ip = "192.0.2.2"
        archive.site.single_yaml_site.project_host.return_value = ip
        profile = {"instance_id": self.cfg["instance"], "maintenance_image": image,
                   "containers": {"web": {"id": "b" * 64}}}
        site.profile.return_value = profile
        archive.ops.run_records.return_value = []
        self.enter(patch.dict(os.environ))
        self.enter(patch.object(host, "ubuntu_docker", return_value=Path("/usr/bin/docker")))
        self.enter(patch.object(host, "archive_module", return_value=archive))
        with self.assertRaisesRegex(host.HostError, "approved_key_image_required"):
            host.preflight(self.cfg, None)
        with self.assertRaisesRegex(host.HostError, "approved_backup_image_mismatch"):
            host.preflight(self.cfg, None, "sha256:" + "b" * 64)
        _, _, run = host.preflight(self.cfg, None, image)
        driver = archive.ops.Docker.return_value
        archive.ops.Docker.assert_called_with(Path("/usr/bin/docker"))
        archive.site.check_web_endpoint.assert_called_once_with(driver, "b" * 64, ip, "16450", "backup_site_port_mismatch")
        archive.site.run_backup.assert_not_called()
        run()
        archive.site.run_backup.assert_called_once_with(site, profile, driver, archive.site.Worker)
        archive.run.assert_not_called()
        archive.target.Package.discover.assert_not_called()

    def test_persistent_mode_accepts_requests_after_idle_timeout_and_finishes_an_active_operation(self):
        stopping = threading.Event()
        archive = Mock()
        clock = Mock(return_value=0)
        backup = uuid4().hex

        def arrive_while_idle(seconds):
            clock.return_value += 101
            self.submit()

        def finish():
            stopping.set()
            return self.root / "backups/artifacts" / backup

        archive.run.side_effect = finish
        self.enter(patch.object(host.time, "sleep", side_effect=arrive_while_idle))
        self.enter(patch.object(host.time, "monotonic", clock))
        self.enter(patch.object(host, "preflight", return_value=(archive, None, archive.run)))
        self.enter(patch.object(host, "receipt", return_value={"backup_operation_id": backup}))
        self.assertEqual(host.serve(self.state, 1, False, persistent=True, stopping=stopping), 0)
        archive.run.assert_called_once_with()
        self.assertEqual(self.latest()["code"], "backup_verified")
        self.assertFalse(self.marker.exists())

    def test_ubuntu_unit_binds_private_state_and_literal_arguments(self):
        self.cfg.update(platform="ubuntu", sha256="a" * 64)
        image = "sha256:" + "a" * 64
        self.enter(patch.object(host, "check_archive"))
        self.enter(patch.object(host, "trusted_chain"))
        self.enter(patch.object(host.Path, "lstat",
                                return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)))
        state = Path('/var/lib/ledger owner $HOST/%n/"one"')
        unit = host.systemd_unit(state, image)
        self.assertIn('"--state" "/var/lib/ledger owner $$HOST/%%n/\\"one\\""', unit)
        self.assertIn('"--persistent" "--key-enrollment" "--web-restore" "--key-image" "' + image + '"', unit)
        self.assertIn("RestartPreventExitStatus=78", unit)
        self.assertIn("After=docker.service", unit)
        with self.assertRaisesRegex(host.HostError, "invalid_service_argument"):
            host.systemd_unit(Path("/run/site\nExecStart="), image)
        self.cfg.pop("platform")
        with self.assertRaisesRegex(host.HostError, "ubuntu_binding_required"):
            host.systemd_unit(state, image)

    def test_explicit_ubuntu_binding_cannot_bypass_dsm_lifecycle(self):
        with patch.object(host.sys, "platform", "linux"), patch.object(host.os.path, "lexists", return_value=True):
            with self.assertRaisesRegex(host.HostError, "ubuntu_host_required"):
                host.ubuntu_docker()

    def review_fixture(self):
        self.submit()
        self.cfg["platform"] = "ubuntu"
        host.save(self.state, self.handles, host.result(self.cfg, self.op, "needs_review", "host_worker_interrupted"))
        session = uuid4().hex
        image = "sha256:" + "e" * 64
        claim = dict(project="fixture", instance=self.cfg["instance"], operation_id=self.op, engine_session=session,
                     armed=True, previous_bundle=uuid4().hex)
        (self.state / "jobs" / self.op).write_text(json.dumps(claim))
        archive, site, driver, resume = Mock(), Mock(), Mock(), Mock()
        profile = {"maintenance_image": image, "server_uuid": str(uuid4())}
        site.directories.return_value = (self.state, self.root / "backups")
        archive.ops.host_lock.side_effect = lambda _: contextlib.nullcontext()
        archive.site.previous_worker.return_value = None
        archive.ops.SERVICES = ("db", "api", "web")
        original = {role: dict(id=role, image=image, mounts=[], project="fixture", service=role, running=True,
                               health="healthy") for role in archive.ops.SERVICES}
        site.services.return_value = original
        bundle = uuid4().hex
        row = dict(project="fixture", instance_id=self.cfg["instance"], session_id=session, mode="manual",
                   stage="needs-recovery", original=original, backup_id=bundle, bundle_id=bundle)
        archive.ops.run_records.side_effect = lambda _: [(self.state / "runs" / session, row)]
        manifest = dict(project="fixture", instance_id=self.cfg["instance"], server_uuid=profile["server_uuid"],
                        backup_operation_id=bundle)
        archive.ops.backup_store.inspect_bundle.return_value = manifest
        archive.ops.read_json.return_value = dict(**manifest, session_id=session, bundle_id=bundle, success=True,
                                                  services_restored=True, private_cleanup_pending=False, local_verified=True)
        bind = self.enter(patch.object(host, "backup_binding", return_value=(archive, site, profile, driver, resume)))
        return archive, resume, row, image, bind

    def test_review_resumes_only_bound_session_and_restores_queue_from_verified_receipt(self):
        archive, resume, row, image, bind = self.review_fixture()
        resume.side_effect = lambda: row.update(stage="finished")
        reviewed = host.review_backup(self.state, self.cfg, self.handles, self.op, image)
        bind.assert_called_once_with(self.cfg, None, image, row["session_id"], recover=True)
        resume.assert_called_once_with()
        self.assertEqual(reviewed, self.latest())
        self.assertTrue(reviewed["decryption_verified"])
        self.assertFalse(self.marker.exists())
        self.assertEqual(host.review_backup(self.state, self.cfg, self.handles, self.op, image), reviewed)
        resume.assert_called_once_with()

    def test_review_rejects_different_pending_session_and_unbound_legacy_job(self):
        archive, resume, row, image, _ = self.review_fixture()
        row["session_id"] = uuid4().hex
        with self.assertRaisesRegex(host.HostError, "other_backup_requires_review"):
            host.review_backup(self.state, self.cfg, self.handles, self.op, image)
        (self.state / "jobs" / self.op).write_bytes(b"")
        with self.assertRaisesRegex(host.HostError, "backup_review_missing_session_binding"):
            host.review_backup(self.state, self.cfg, self.handles, self.op, image)
        resume.assert_not_called()
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.latest()["state"], "needs_review")

    def test_review_when_no_session_was_started_releases_for_next_request_without_starting_one(self):
        archive, resume, _, image, _ = self.review_fixture()
        archive.ops.run_records.side_effect = lambda _: []
        reviewed = host.review_backup(self.state, self.cfg, self.handles, self.op, image)
        self.assertEqual(reviewed["code"], "backup_interrupted_before_execution")
        self.assertFalse(self.marker.exists())
        resume.assert_not_called()

    def test_review_does_not_publish_for_mismatch_and_does_not_repeat_a_finished_job(self):
        archive, resume, row, image, _ = self.review_fixture()
        row["stage"] = "finished"
        archive.ops.read_json.return_value["session_id"] = uuid4().hex
        with self.assertRaisesRegex(host.HostError, "backup_review_receipt_changed"):
            host.review_backup(self.state, self.cfg, self.handles, self.op, image)
        self.assertTrue(self.marker.exists())
        resume.assert_not_called()


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: 1)() == 0
                     and Path("/root").is_dir(), "requires a local isolated root filesystem")
class HostFilesystem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/root", prefix="maintenance-host-test-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.station = base / "gift-qa"
        self.station.mkdir()
        self.state = base / "private"
        archive = base / "backup.pyz"
        archive.write_bytes(b"pinned test bytes")
        self.args = argparse.Namespace(project="gift-qa", station=str(self.station), state=str(self.state),
                                       archive=str(archive), instance=str(uuid4()),
                                       sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                                       platform="dsm", owner_digest="b" * 64)

    def test_operator_digest_idempotence_and_ipc_scope(self):
        host.init(self.args)
        self.args.platform = "ubuntu"
        with self.assertRaisesRegex(host.HostError, "existing_site_binding_mismatch"):
            host.init(self.args)
        self.args.platform = "dsm"
        host.init(self.args)
        self.assertFalse((self.state / "owner.token").exists())
        base = self.station / host.IPC
        self.assertEqual((base / "owner.sha256").read_text(), self.args.owner_digest + "\n")
        (base / "owner.sha256").unlink()
        host.init(self.args)
        self.assertTrue((base / "owner.sha256").is_file())
        (base / "requests").rename(base / "old")
        (base / "requests").mkdir()
        with self.assertRaisesRegex(host.HostError, "ipc_identity_changed"):
            host.serve(self.state, 1, True)
        self.args.owner_digest = "c" * 64
        with self.assertRaises(host.HostError):
            host.init(self.args)

    def test_private_key_output_and_pinned_digest(self):
        self.args.owner_digest = None
        host.init(self.args)
        saved = self.state / "owner.token"
        token = saved.read_text().strip()
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        digest = self.station / host.IPC / "owner.sha256"
        self.assertEqual(digest.read_text().strip(), hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, (self.state / "binding.json").read_text())
        Path(self.args.archive).write_bytes(b"other")
        with self.assertRaisesRegex(host.HostError, "archive_digest_mismatch"):
            host.archive_module(host.read_config(self.state))

    def test_real_preflight_mismatch_without_docker(self):
        host.init(self.args)
        marker = self.station / host.IPC / "requests" / host.REQUEST
        op = uuid4().hex
        marker.write_text(op + "\n")
        os.chown(marker, 10001, 0)
        marker.chmod(0o600)
        self.assertEqual(host.serve(self.state, 2, True, str(self.station / "mismatch")), 1)
        status = json.loads((self.state / "result.json").read_text())
        self.assertEqual((status["operation_id"], status["code"], status["state"]),
                         (op, "station_directory_mismatch", "failed"))
        self.assertFalse(marker.exists())

    def test_dsm_extended_acl_rejected_before_widening_permissions(self):
        with patch.object(host.os, "listxattr", return_value=["system.syno_acl"]):
            with self.assertRaisesRegex(host.HostError, "ipc_extended_acl"):
                host.init(self.args)
        self.assertFalse((self.station / host.IPC / "owner.sha256").exists())
        host.init(self.args)
        self.assertTrue((self.state / "binding.json").exists())


if __name__ == "__main__":
    unittest.main()
