"""Public candidate identity and resumable GitHub release. All data is synthetic."""

import copy
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from zipfile import ZipFile
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/publication"))
import github_release
import images
import smoke


class FakeGitHub:
    repository = "example/giftledger"

    def __init__(self):
        self.r = None
        self.raw = {}
        self.fail_at = None
        self.uploads = []

    def releases(self, tag):
        return [self.get(1)] if self.r else []

    def create(self, meta):
        self.r = dict(meta, id=1, draft=True, assets=[])
        return self.get(1)

    def get(self, num):
        return copy.deepcopy(self.r)

    def read_asset(self, num):
        return self.raw[num]

    def upload(self, tag, path):
        if path.name == self.fail_at:
            raise OSError("test transport interrupted")
        n = len(self.raw) + 1
        self.uploads.append(path.name)
        self.raw[n] = path.read_bytes()
        self.r["assets"].append(
            {
                "id": n,
                "name": path.name,
                "state": "uploaded",
                "size": path.stat().st_size,
            }
        )

    def clear_incomplete(self, num):
        self.r["assets"] = [x for x in self.r["assets"] if x["id"] != num]

    def publish(self, num):
        self.r["draft"] = False


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.commit = "a" * 40
        self.refs = {
            r: "docker.io/example/giftledger-"
            + r
            + "@sha256:"
            + hashlib.sha256(r.encode()).hexdigest()
            for r in images.ROLES
        }

    def bundle(self):
        site = self.work / "release"
        images.release_files(site, self.refs, "example/giftledger", "v1.5.0")
        return site

    def test_smoke_waits_for_docker_health_after_https_is_ready(self):
        compose = ["docker", "compose", "-p", "sample"]
        ids = "a\nb\nc\n"
        starting = "db running healthy\napi running healthy\nweb running starting\n"
        healthy = starting.replace("web running starting", "web running healthy")
        with (
            mock.patch.object(
                smoke.subprocess,
                "check_output",
                side_effect=[ids, starting, ids, healthy],
            ) as docker,
            mock.patch.object(smoke, "https_ready", return_value=True) as https,
            mock.patch.object(smoke.time, "sleep") as sleep,
        ):
            smoke.wait_ready(compose, {}, "https://localhost", self.work / "ca")
        self.assertEqual(https.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(
            docker.call_args_list[0].args[0],
            [*compose, "ps", "--all", "--quiet", "db", "api", "web"],
        )

    def test_smoke_timeout_reports_only_safe_status_for_each_service(self):
        with (
            mock.patch.object(
                smoke.subprocess,
                "check_output",
                side_effect=[
                    "a b",
                    "db running healthy\napi exited unavailable_detail\n",
                ],
            ),
            mock.patch.object(smoke, "https_ready", return_value=False),
            mock.patch.object(smoke.time, "monotonic", side_effect=[0, 241]),
            self.assertRaisesRegex(
                RuntimeError,
                r"HTTPS=unready, db=running/healthy, api=exited/unknown, web=missing\)",
            ),
        ):
            smoke.wait_ready(
                ["docker", "compose"], {}, "https://localhost", self.work / "ca"
            )

    def source_with_history(self):
        source = self.work / "checkout"
        files = {name: name.encode() for name in images.DOT_PATHS}
        files["backend/tests/test_reviewed.py"] = b"reviewed"
        for name, content in {
            **files,
            "backend/tests/test_legacy.py": b"old test",
            ".git/config": b"old metadata",
            "docs/legacy.txt": b"old handoff",
        }.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        hashes = {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        }
        (source / images.MANIFEST).write_text(json.dumps(hashes))
        return source, hashes

    def test_reviewed_build_directory_excludes_historical_tests_and_files(self):
        source, hashes = self.source_with_history()
        reviewed = self.work / "reviewed"
        with mock.patch.object(images, "ROOT", source), self.assertRaises(ValueError):
            images.assert_export()
        images.stage_source(source, reviewed)
        paths = {
            p.relative_to(reviewed).as_posix()
            for p in reviewed.rglob("*")
            if p.is_file()
        }
        self.assertEqual(paths, set(hashes) | {images.MANIFEST})
        self.assertEqual((source / "docs/legacy.txt").read_bytes(), b"old handoff")
        with mock.patch.object(images, "ROOT", reviewed):
            images.assert_export()
        with self.assertRaises(ValueError):
            images.stage_source(source, reviewed)
        with self.assertRaises(ValueError):
            images.stage_source(source, source / "nested")

    def test_changed_missing_or_linked_reviewed_file_cannot_be_staged(self):
        source, _ = self.source_with_history()
        path = source / "backend/tests/test_reviewed.py"
        path.write_bytes(b"unreviewed edit")
        out = self.work / "candidate"
        with self.assertRaises(ValueError):
            images.stage_source(source, out)
        path.unlink()
        with self.assertRaises(ValueError):
            images.stage_source(source, out)
        alternate = self.work / "substitute.py"
        alternate.write_bytes(b"reviewed")
        path.symlink_to(alternate)
        with self.assertRaises(ValueError):
            images.stage_source(source, out)
        self.assertFalse(out.exists())

    def test_ambiguous_and_escaping_manifest_paths_are_rejected(self):
        source, known = self.source_with_history()
        manifest = source / images.MANIFEST
        for path in (
            "../private",
            "/outside",
            "a/./file",
            "a\\b",
            ".git/config",
            images.MANIFEST,
        ):
            with self.subTest(path=path):
                manifest.write_text(json.dumps({**known, path: "0" * 64}))
                with self.assertRaises(ValueError):
                    images.stage_source(source, self.work / "bad")
        manifest.write_text(
            json.dumps(known)[:-1] + ',".gitignore":"' + "a" * 64 + '"}'
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            images.stage_source(source, self.work / "bad")

    def test_yaml_and_helpers_match_and_assets_rebuild_identically(self):
        a = self.bundle()
        b = self.work / "rebuilt"
        images.release_files(b, self.refs, "example/giftledger", "v1.5.0")
        for f in a.iterdir():
            self.assertEqual(f.read_bytes(), (b / f.name).read_bytes())
        self.assertEqual(
            set(self.refs.values()),
            {
                line.split("image: ")[1]
                for line in (a / "docker-compose.yml").read_text().splitlines()
                if "image: " in line
            },
        )
        for path in (a / "Install.zh.md", a / "Install.en.md"):
            self.assertIn(
                "/releases/download/v1.5.0/docker-compose.yml", path.read_text()
            )
            self.assertNotIn("{GITHUB_REPOSITORY}", path.read_text())
            self.assertNotIn("/releases/latest/", path.read_text())
        with ZipFile(a / "mac-site-tools.zip") as z:
            self.assertEqual(
                z.read("docker-compose.yml"), (a / "docker-compose.yml").read_bytes()
            )
            self.assertEqual(
                set(z.namelist()),
                {
                    "docker-compose.yml",
                    "mac_backup_key.py",
                    "mac_restore.py",
                    "mac_restore_bundle.py",
                    "backup_store.py",
                    "backup_export.py",
                    "从备份恢复.md",
                    "站主找回账号.md",
                    "站主升级.md",
                },
            )

    def test_interrupted_draft_retries_without_overwriting_assets(self):
        path = self.bundle()
        api = FakeGitHub()
        api.fail_at = "mac-site-tools.zip"
        with self.assertRaises(OSError):
            github_release.deliver(api, "v1.5.0", self.commit, path)
        self.assertTrue(api.r["draft"])
        uploaded = list(api.uploads)
        api.fail_at = None
        github_release.deliver(api, "v1.5.0", self.commit, path)
        self.assertFalse(api.r["draft"])
        github_release.deliver(api, "v1.5.0", self.commit, path)
        self.assertEqual(len(api.uploads), len(github_release.EXPECTED))
        self.assertEqual(api.uploads[: len(uploaded)], uploaded)

    def test_completed_foreign_bytes_preserved_and_never_published(self):
        path = self.bundle()
        api = FakeGitHub()
        api.fail_at = "mac-site-tools.zip"
        with self.assertRaises(OSError):
            github_release.deliver(api, "v1.5.0", self.commit, path)
        n = api.r["assets"][0]["id"]
        api.raw[n] = b"x" * len(api.raw[n])
        previous = list(api.uploads)
        with self.assertRaises(ValueError):
            github_release.deliver(api, "v1.5.0", self.commit, path)
        self.assertEqual(previous, api.uploads)
        self.assertTrue(api.r["draft"])

    def test_conflicting_remote_tag_stops_before_writing_any_role(self):
        records = {r: {"manifest": images.digest(r.encode())} for r in images.ROLES}
        conflicts = [
            SimpleNamespace(returncode=1, stderr=b"manifest unknown")
            for _ in images.ROLES[:-1]
        ]
        conflicts.append(SimpleNamespace(returncode=0, stdout=b"different content"))
        with (
            mock.patch.object(images, "verify", return_value=records),
            mock.patch.object(images.subprocess, "run", side_effect=conflicts),
            mock.patch.object(images, "run") as push,
        ):
            with self.assertRaisesRegex(ValueError, "different content"):
                images.publish(self.work, self.commit, "example", "v1.5.0")
            push.assert_not_called()

    def test_oci_commit_manifests_layers_and_corruption(self):
        dest = self.work / "images"
        blobs = dest / "blobs/sha256"
        blobs.mkdir(parents=True)

        def store(raw, mt):
            sha = hashlib.sha256(raw).hexdigest()
            (blobs / sha).write_bytes(raw)
            return {"digest": "sha256:" + sha, "size": len(raw), "mediaType": mt}

        def encoded(obj):
            return json.dumps(obj, sort_keys=True).encode()

        manifest_type = "application/vnd.oci.image.manifest.v1+json"
        records = {}
        entries = []
        for role in images.ROLES:
            layer = store(role.encode(), "application/vnd.oci.image.layer.v1.tar")
            config = store(
                encoded(
                    {
                        "os": "linux",
                        "architecture": "amd64",
                        "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
                    }
                ),
                "application/vnd.oci.image.config.v1+json",
            )
            ref = store(
                encoded(
                    {
                        "schemaVersion": 2,
                        "mediaType": manifest_type,
                        "config": config,
                        "layers": [layer],
                    }
                ),
                manifest_type,
            )
            records[role] = {"config": config["digest"], "manifest": ref["digest"]}
            entries.append(
                dict(ref, annotations={"org.opencontainers.image.ref.name": role})
            )
        (dest / "index.json").write_bytes(
            encoded({"schemaVersion": 2, "manifests": entries})
        )
        manifest = self.work / images.MANIFEST
        manifest.write_bytes(b"{}\n")
        root_patch = mock.patch.object(images, "ROOT", self.work)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        (self.work / "CANDIDATE.json").write_bytes(
            encoded(
                {
                    "commit": self.commit,
                    "images": records,
                    "platform": "linux/amd64",
                    "source_manifest_sha256": images.digest(manifest.read_bytes()),
                }
            )
        )
        self.assertEqual(images.verify(self.work, self.commit), records)
        with self.assertRaises(ValueError):
            images.verify(self.work, "b" * 40)
        original = manifest.read_bytes()
        manifest.write_bytes(b"changed reviewed source set")
        with self.assertRaisesRegex(ValueError, "reviewed source"):
            images.verify(self.work, self.commit)
        manifest.write_bytes(original)
        (blobs / layer["digest"][7:]).write_bytes(secrets.token_bytes(layer["size"]))
        with self.assertRaises(ValueError):
            images.verify(self.work, self.commit)
