"""Exercise verified local age installation without a network or live credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/single_yaml"))
import native_age as age


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    def geturl(self):
        return "https://release-assets.githubusercontent.com/fixture"


class NativeAgeTests(unittest.TestCase):
    def setUp(self):
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(mock.patch.object(Path, "home", return_value=self.home))
        stack.enter_context(mock.patch.dict(os.environ, LOCALAPPDATA=str(self.home)))

    def fixture(self, key="darwin-amd64", duplicate=False):
        data = (b"fixture executable one", b"fixture executable two")
        windows = key.startswith("windows-")
        names = ["age/age", "age/age-keygen"]
        if windows:
            names = [name + ".exe" for name in names]
        files = [*zip(names, data), ("../../should-not-extract", b"ignored")]
        if duplicate:
            files.append((names[0], data[0]))
        buf = io.BytesIO()
        if windows:
            with zipfile.ZipFile(buf, mode="w") as archive:
                for name, contents in files:
                    archive.writestr(name, contents)
        else:
            with tarfile.open(fileobj=buf, mode="w:gz") as archive:
                for name, contents in files:
                    info = tarfile.TarInfo(name)
                    info.size = len(contents)
                    archive.addfile(info, io.BytesIO(contents))
        raw = buf.getvalue()
        self.stack.enter_context(mock.patch.object(age, "_platform_key", return_value=key))
        self.stack.enter_context(mock.patch.dict(age.RELEASES, {key: (sha(raw), *map(sha, data))}))
        return raw

    def test_platform_matrix_and_unsupported_hosts(self):
        for system, machine, expected in (
                ("darwin", "x86_64", "darwin-amd64"), ("darwin", "arm64", "darwin-arm64"),
                ("win32", "AMD64", "windows-amd64"), ("win32", "aarch64", "windows-arm64"),
                ("win32", "i386", None), ("linux", "x86_64", None)):
            with self.subTest(system=system, machine=machine), mock.patch.object(sys, "platform", system), \
                    mock.patch("platform.machine", return_value=machine), mock.patch.dict(os.environ):
                os.environ.pop("PROCESSOR_ARCHITEW6432", None)
                if expected:
                    self.assertEqual(age._platform_key(), expected)
                else:
                    with self.assertRaises(age.NativeAgeError):
                        age.cached_programs()

    def test_windows_and_mac_install_only_both_named_members_and_reuse_offline(self):
        for key in ("darwin-arm64", "windows-amd64"):
            with self.subTest(key=key):
                raw = self.fixture(key)
                with mock.patch.object(age, "urlopen", return_value=Response(raw)) as download:
                    self.assertFalse(age.cached_programs())
                    programs = age.install_programs()
                    self.assertEqual(programs, age.install_programs())
                    request = download.call_args.args[0]
                    self.assertTrue(request.full_url.startswith(
                        f"https://github.com/FiloSottile/age/releases/download/{age.VERSION}/age-{age.VERSION}-{key}."))
                    download.assert_called_once()
                directory = Path(programs["age"]).parent
                self.assertEqual(directory.name, key)
                self.assertEqual(directory.parent.name, age.VERSION)
                self.assertEqual(sorted(p.name for p in directory.iterdir()),
                                 sorted(Path(p).name for p in programs.values()))
                self.assertFalse(list(self.home.rglob("should-not-extract")))
                with mock.patch.object(age, "urlopen", side_effect=AssertionError("used network")):
                    self.assertEqual(age.cached_programs(), programs)
                with mock.patch.object(age, "VERSION", "v1.2.1"):
                    self.assertEqual(age.cached_programs(), {})
                self.assertTrue(Path(programs["age"]).is_file())

    def test_tampered_cache_requires_new_validated_download(self):
        raw = self.fixture()
        with mock.patch.object(age, "urlopen", side_effect=lambda *a, **kw: Response(raw)):
            programs = age.install_programs()
            binary = Path(programs["age-keygen"])
            original = binary.read_bytes()
            binary.write_bytes(b"modified")
            self.assertEqual(age.cached_programs(), {})
            self.assertEqual(age.install_programs(), programs)
            self.assertEqual(binary.read_bytes(), original)

    def test_download_reports_progress_and_offline_reuse_is_quiet(self):
        raw = self.fixture()
        output = io.StringIO()
        with mock.patch.object(age, "urlopen", return_value=Response(raw)), \
                mock.patch.object(age.time, "monotonic", side_effect=[0, 6]), redirect_stdout(output):
            age.install_programs()
            messages = output.getvalue()
            self.assertIn("正在从 GitHub 下载", messages)
            self.assertIn("已下载", messages)
            self.assertIn("已通过核验", messages)
            age.install_programs()
            self.assertEqual(output.getvalue(), messages)

    def test_wrong_archive_or_member_checksum_does_not_install(self):
        raw = self.fixture()
        for problem in ("archive", "program"):
            with self.subTest(problem=problem), mock.patch.dict(age.RELEASES), \
                    mock.patch.object(age, "urlopen", return_value=Response(raw)):
                digest, first, last = age.RELEASES["darwin-amd64"]
                if problem == "archive":
                    digest = "0" * 64
                else:
                    last = "0" * 64
                age.RELEASES["darwin-amd64"] = (digest, first, last)
                with self.assertRaisesRegex(age.NativeAgeError, "SHA-256"):
                    age.install_programs()
                self.assertFalse(age.cached_programs())
                self.assertFalse(list(self.home.rglob("age-keygen")))

    def test_duplicate_tar_member_rejected_before_publication(self):
        raw = self.fixture(duplicate=True)
        with mock.patch.object(age, "urlopen", return_value=Response(raw)), self.assertRaises(age.NativeAgeError):
            age.install_programs()
        self.assertFalse(age.cached_programs())

    def test_interrupted_publish_repaired_on_retry(self):
        raw = self.fixture()
        replace = os.replace

        def interrupt(src, dst):
            if dst.name == "age-keygen":
                raise OSError("simulated interrupted install")
            replace(src, dst)

        with mock.patch.object(age, "urlopen", side_effect=lambda *a, **kw: Response(raw)):
            with mock.patch.object(os, "replace", side_effect=interrupt), self.assertRaises(age.NativeAgeError):
                age.install_programs()
            self.assertFalse(age.cached_programs())
            self.assertEqual(len(age.install_programs()), 2)
            self.assertFalse(list(self.home.rglob(".install-*")))

    def test_concurrent_publishers_converge_on_one_verified_pair(self):
        raw = self.fixture()
        barrier = threading.Barrier(2)

        def download(*args, **kwargs):
            barrier.wait(timeout=5)
            return Response(raw)

        with mock.patch.object(age, "urlopen", side_effect=download), ThreadPoolExecutor(2) as pool:
            installs = [pool.submit(age.install_programs) for _ in range(2)]
            results = [job.result(timeout=10) for job in installs]
        self.assertEqual(results, [age.cached_programs()] * 2)
        self.assertEqual(len(results[0]), 2)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX symlink")
    def test_redirected_cache_directory_is_not_used(self):
        self.fixture()
        base = self.home / "Library/Application Support"
        base.mkdir(parents=True)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (base / "GiftLedger").symlink_to(elsewhere)
        with self.assertRaises(age.NativeAgeError):
            age.install_programs()
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
