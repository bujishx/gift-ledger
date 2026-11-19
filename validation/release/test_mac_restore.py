"""Mac owner key enrollment, recovery decisions, re-entry, and streamed file transport."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/single_yaml"))
import mac_restore as client
import restore_files
import test_backup_store as store_fixture


ID = "1" * 32
HASH = "2" * 64
OP = "3" * 32
SOURCE = {"source_project": "old", "source_instance_id": "b26de830-f363-4c54-9ad4-9783855d12d4",
          "source_server_uuid": "159ae34a-4566-49dc-b251-671dabccbbf2"}
TARGET = {"project": "new", "instance_id": "ed76be70-a951-4d66-a0bb-12a8ab22cf49",
          "server_uuid": "c48d6e46-2995-4a32-bfff-e70b53dd55ba", "url": "https://192.168.10.6:28443/"}
FILES = ("BUNDLE.json", "SHA256SUMS", "backup.json", "verify.json", f"backup-{ID}.glgui.age")


class MacPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "第二份 备份.zip"
        self.path.touch()
        tty = mock.patch.object(sys.stdin, "isatty", return_value=True)
        tty.start()
        self.addCleanup(tty.stop)

    def test_paste_quote_and_finder_drag_preserve_the_selected_path(self):
        for value in [str(self.path), shlex.quote(str(self.path)), str(self.path).replace(" ", r"\ ")]:
            with self.subTest(value=value), mock.patch("builtins.input", return_value=value), \
                 mock.patch.object(client.keys.subprocess, "run") as window:
                self.assertEqual(client.choose_bundle(), self.path)
                window.assert_not_called()

    def test_foreground_file_and_directory_dialogs(self):
        for key, path, command in [("", self.path, "choose file"), ("d", self.path.parent, "choose folder")]:
            result = SimpleNamespace(returncode=0, stdout=os.fsencode(path) + b"\n")
            with mock.patch("builtins.input", return_value=key), \
                 mock.patch.object(client.keys.subprocess, "run", return_value=result) as window:
                self.assertEqual(client.choose_bundle(), path)
                script = window.call_args.args[0][-1]
                self.assertIn("activate", script)
                self.assertIn(command, script)

    def test_empty_or_failed_window_can_continue_without_restarting(self):
        for code in (0, 1):
            result = SimpleNamespace(returncode=code, stdout=b"")
            with mock.patch("builtins.input", side_effect=["", str(self.path)]), \
                 mock.patch.object(client.keys.subprocess, "run", return_value=result):
                self.assertEqual(client.keys.choose_key(), self.path)

    def test_cancel_and_missing_path_do_not_start_restore(self):
        for value in ("q", str(self.path) + ".missing"):
            with mock.patch("builtins.input", return_value=value), self.assertRaises(client.keys.EnrollmentError):
                client.choose_bundle()


class DesktopAgeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tag = "docker.io/zhjido/giftledger-backup@sha256:" + HASH
        self.image_id = "sha256:" + "b" * 64
        self.missing = b"Error response from daemon: No such image: zhjido/giftledger-backup"
        self.commands = []
        self.inspect_error = self.missing
        self.engine_code = 0
        self.pull_fails = False
        self.available_after_pull = True
        self.pair = {"age": "/user/tools/age", "age-keygen": "/user/tools/age-keygen"}
        self.native = SimpleNamespace(VERSION="v1.3.2", NativeAgeError=ValueError,
                                      cached_programs=mock.Mock(return_value={}),
                                      install_programs=mock.Mock(return_value=self.pair))
        keys = client.keys
        patch = self.stack.enter_context
        patch(mock.patch.dict(sys.modules, native_age=self.native))
        patch(mock.patch.object(sys, "platform", "darwin"))
        patch(mock.patch.object(keys, "os", SimpleNamespace(name="posix", environ={})))
        self.installed = patch(mock.patch.object(keys, "installed_age_programs", return_value={}))
        patch(mock.patch.object(keys, "desktop_executables",
                                side_effect=lambda name, extra=(): iter(["/docker"] if name == "docker" else [])))
        path_stat = Path.stat
        patch(mock.patch.object(Path, "stat", autospec=True, side_effect=lambda path, *a, **kw:
            SimpleNamespace(st_mode=stat.S_IFSOCK) if path.name == "docker.sock" else path_stat(path, *a, **kw)))
        self.tty = patch(mock.patch.object(sys.stdin, "isatty", return_value=True))
        self.prompt = patch(mock.patch("builtins.input", return_value=""))
        patch(mock.patch.object(subprocess, "run", side_effect=self.docker))

    def docker(self, args, **kwargs):
        self.assertEqual(args[0:2], ["/docker", "--host"])
        self.assertTrue(args[2].replace(chr(92), "/").endswith("/docker.sock") or args[2].startswith("npipe:"))
        command = args[3]
        self.commands.append(command)
        if command == "compose":
            services = {name: {"image": self.tag} for name in ("backup", "backup-key")}
            return subprocess.CompletedProcess(args, 0, json.dumps({"services": services}).encode(), b"")
        if command == "image":
            self.assertEqual(args[-1], self.tag)
            if self.inspect_error:
                return subprocess.CompletedProcess(args, 1, b"", self.inspect_error)
            return subprocess.CompletedProcess(args, 0, self.image_id.encode(), b"")
        if command == "info":
            return subprocess.CompletedProcess(args, self.engine_code)
        self.assertEqual(args[3:], ["pull", "--platform", "linux/amd64", self.tag])
        self.assertTrue(kwargs["check"])
        if self.pull_fails:
            raise subprocess.CalledProcessError(1, args)
        if self.available_after_pull:
            self.inspect_error = None
        return subprocess.CompletedProcess(args, 0)

    def test_enter_downloads_native_tools_once_and_registers_the_selected_identity(self):
        keys = client.keys
        selected = Path("identity.txt")
        argv = ["mac_backup_key.py", "--host", "user@192.0.2.2", "--project-dir", "/test"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(keys, "enrollment_path", return_value=(selected, False)) as choose, \
             mock.patch.object(keys, "custody", return_value=(b"example", "recipient")) as custody, \
             mock.patch.object(keys, "session"), mock.patch.object(keys, "exchange", return_value=TARGET):
            keys.main()
        choose.assert_called_once()
        self.prompt.assert_called_once()
        self.native.install_programs.assert_called_once_with()
        path, generated, age = custody.call_args.args
        self.assertEqual((path, generated, age.programs), (selected, False, self.pair))
        self.assertEqual(self.commands, [])

    def test_both_platforms_use_installed_then_verified_cache_without_docker(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                self.native.cached_programs.reset_mock()
                self.native.cached_programs.return_value = {"age": "/cache/age", "age-keygen": "/cache/age-keygen"}
                self.installed.return_value = self.pair
                self.assertEqual(client.keys.Age().programs, self.pair)
                self.native.cached_programs.assert_not_called()
                self.installed.return_value = {}
                self.assertEqual(client.keys.Age().programs, self.native.cached_programs.return_value)
                self.native.cached_programs.assert_called_once_with()
                self.assertEqual(self.commands, [])
        self.prompt.assert_not_called()
        self.native.install_programs.assert_not_called()

    def test_only_explicit_docker_choice_can_pull_and_uses_pinned_local_image(self):
        for platform, os_name in (("darwin", "posix"), ("win32", "nt")):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(client.keys, "os", SimpleNamespace(name=os_name, environ={})):
                self.commands.clear()
                self.inspect_error = self.missing
                self.prompt.reset_mock()
                self.prompt.return_value = "D"
                self.assertEqual(client.keys.Age().image, self.image_id)
                self.prompt.assert_called_once()
                self.assertEqual([cmd for cmd in self.commands if cmd in ("image", "pull")], ["image", "pull", "image"])
        self.native.install_programs.assert_not_called()

    def test_failed_native_download_allows_docker_or_exit(self):
        self.native.install_programs.side_effect = ValueError("test download failure")
        self.inspect_error = None
        self.prompt.side_effect = ["", "q"]
        with self.assertRaises(client.keys.EnrollmentError):
            client.keys.Age()
        self.assertEqual(self.commands, [])
        self.prompt.side_effect = ["", "d"]
        self.assertEqual(client.keys.Age().image, self.image_id)
        self.assertEqual(self.commands, ["compose", "image"])

    def test_noninteractive_uses_only_local_programs_or_cached_image_on_both_platforms(self):
        self.tty.return_value = False
        for platform, os_name in (("darwin", "posix"), ("win32", "nt")):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(client.keys, "os", SimpleNamespace(name=os_name, environ={})):
                self.inspect_error = self.missing
                with self.assertRaises(client.keys.EnrollmentError):
                    client.keys.Age()
                self.inspect_error = None
                self.assertEqual(client.keys.Age().image, self.image_id)
        self.prompt.assert_not_called()
        self.native.install_programs.assert_not_called()
        self.assertNotIn("pull", self.commands)

    def test_cancellation_and_unsafe_or_unavailable_docker_stop_before_download(self):
        self.prompt.return_value = "q"
        with self.assertRaises(client.keys.EnrollmentError):
            client.keys.Age()
        self.assertEqual(self.commands, [])
        self.prompt.return_value = "d"
        cases = ((self.tag, b"Cannot connect to the Docker daemon", 1),
                 (self.tag, b"Permission denied", 0),
                 ("other/backup@sha256:" + HASH, self.missing, 0),
                 ("dockerxio/zhjido/giftledger-backup@sha256:" + HASH, self.missing, 0),
                 ("zhjido/giftledger-backup:v1.6", self.missing, 0))
        for tag, error, engine_code in cases:
            with self.subTest(tag=tag, error=error):
                self.tag, self.inspect_error, self.engine_code = tag, error, engine_code
                with self.assertRaises(client.keys.EnrollmentError):
                    client.keys.Age()
        self.assertNotIn("pull", self.commands)

    def test_failed_docker_download_or_unverified_image_stops(self):
        self.prompt.return_value = "d"
        for pull_fails in (True, False):
            with self.subTest(pull_fails=pull_fails):
                self.commands.clear()
                self.pull_fails, self.available_after_pull = pull_fails, False
                with self.assertRaises(client.keys.EnrollmentError):
                    client.keys.Age()
                self.assertEqual(self.commands.count("pull"), 1)


class InstalledAgeTests(unittest.TestCase):
    def test_only_a_pair_at_the_required_version_is_used(self):
        def paths(name):
            return iter(["/old/" + name, "/package/" + name])

        versions = {"/old/age": b"v1.2.0", "/package/age": b"v1.3.2",
                    "/old/age-keygen": b"v1.2.0", "/package/age-keygen": b"v1.2.0"}
        def version(args, **kwargs):
            self.assertEqual(args[1:], ["--version"])
            return subprocess.CompletedProcess(args, 0, versions[args[0]] + b"\n", b"")

        with mock.patch.object(client.keys, "desktop_executables", side_effect=paths), \
                mock.patch.object(subprocess, "run", side_effect=version):
            self.assertEqual(client.keys.installed_age_programs("v1.3.2"), {})
            versions["/package/age-keygen"] = b"v1.3.2"
            self.assertEqual(client.keys.installed_age_programs("v1.3.2"),
                             {"age": "/package/age", "age-keygen": "/package/age-keygen"})

    def test_skips_wrappers_and_implicit_current_directory_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script, binary = root / "shims", root / "bin"
            script.mkdir()
            binary.mkdir()
            for platform, name, header in (("darwin", "age", bytes.fromhex("cffaedfe")),
                                           ("win32", "age.exe", b"MZ00")):
                for folder, content in ((script, b"#!/bin/sh"), (binary, header), (root, header)):
                    file = folder / name
                    file.write_bytes(content)
                    file.chmod(0o700)
                (script / "age.cmd").write_bytes(b"@echo v1.3.2")
                with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                        mock.patch.object(os, "get_exec_path", return_value=[".", str(root), str(script), str(binary)]), \
                        mock.patch.object(Path, "cwd", return_value=root):
                    self.assertEqual(list(client.keys.desktop_executables("age")), [str(binary / name)])


class MacEnrollmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.argv = ["mac_backup_key.py", "--host", "owner@192.0.2.20", "--project-dir", "/site",
                     "--project", "gift-test"]
        self.default = self.home / "gift-test-backup.agekey"
        self.age = self.enter_patch(mock.patch.object(client.keys, "Age"))
        self.custody = self.enter_patch(mock.patch.object(client.keys, "custody",
                                             return_value=(b"original", "recipient")))
        self.session = self.enter_patch(mock.patch.object(client.keys, "session"))
        self.exchange = self.enter_patch(mock.patch.object(client.keys, "exchange", return_value={
            "project": "gift-test", "instance_id": TARGET["instance_id"]}))
        self.enter_patch(mock.patch.object(Path, "home", return_value=self.home))
        self.enter_patch(mock.patch.object(sys.stdin, "isatty", return_value=True))

    def enter_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def invoke(self, choices=(), *args):
        with mock.patch.object(sys, "argv", [*self.argv, *args]), \
                mock.patch("builtins.input", side_effect=choices) as prompts:
            client.keys.main()
        return [call.args[0] for call in prompts.call_args_list]

    def test_menu_registers_selected_existing_file_or_generates_at_confirmed_path(self):
        old = self.home / "已有 钥匙.agekey"
        old.touch()
        for choices, path, generate in ((["2", str(old)], old, False),
                                        (["1", ""], self.default, True),
                                        (["1", "我的 新钥匙.agekey"], self.home / "我的 新钥匙.agekey", True)):
            with self.subTest(choices=choices):
                prompts = self.invoke(choices)
                self.assertIn("1 生成新钥匙并登记", prompts[0].splitlines())
                self.assertIn("2 登记旧钥匙", prompts[0].splitlines())
                self.custody.assert_called_with(path, generate, self.age.return_value)
                self.assertEqual(self.exchange.call_args.args[2:], (b"original", "recipient", self.age.return_value))

    def test_existing_destination_is_preserved_and_another_name_can_be_selected(self):
        self.default.write_bytes(b"original key")
        replacement = self.home / "other.agekey"
        self.invoke(["1", "", str(self.home / "missing" / "key"), str(replacement)])
        self.custody.assert_called_once_with(replacement, True, self.age.return_value)
        self.assertEqual(self.default.read_bytes(), b"original key")

    def test_cancellation_and_invalid_choices_never_generate_or_start_ssh(self):
        for choices in ([""], ["q"], ["x"], ["1", "q"], ["2", "q"], [EOFError()]):
            with self.subTest(choices=choices), self.assertRaises(client.keys.EnrollmentError):
                self.invoke(choices)
        self.age.assert_not_called()
        self.custody.assert_not_called()
        self.session.assert_not_called()

    def test_explicit_flags_bypass_menu_and_restore_only_accepts_original_key(self):
        for flag, generate in (("--key", False), ("--generate", True)):
            self.assertEqual(self.invoke([], flag, str(self.default)), [])
            self.custody.assert_called_with(self.default, generate, self.age.return_value)
        self.default.touch()
        prompts = self.invoke([str(self.default)], "--restore-key")
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("生成新钥匙", prompts[0])
        self.custody.assert_called_with(self.default, False, self.age.return_value)
        with self.assertRaisesRegex(client.keys.EnrollmentError, "恢复须选择"):
            self.invoke([], "--restore-key", "--generate", str(self.default))

    def test_first_guide_cli_carries_the_site_binding_into_enrollment(self):
        self.argv = ["mac_backup_key.py", "--host", "owner@192.168.10.6", "--project", "gift-test",
                     "--site-url", TARGET["url"], "--instance-id", TARGET["instance_id"]]
        self.invoke(["1", ""])
        args = self.exchange.call_args.args[1]
        self.assertIsNone(args.project_dir)
        self.assertEqual((args.project, args.site_url, args.instance_id),
                         ("gift-test", TARGET["url"], TARGET["instance_id"]))

    def test_incomplete_first_guide_stops_before_asking_for_a_key(self):
        options = {"--project": "gift-test", "--site-url": TARGET["url"], "--instance-id": TARGET["instance_id"]}
        for missing in options:
            with self.subTest(missing=missing), self.assertRaises(client.keys.EnrollmentError):
                self.argv = ["mac_backup_key.py", "--host", "owner@192.168.10.6"]
                for option, value in options.items():
                    if option != missing:
                        self.argv.extend([option, value])
                self.invoke()
        self.custody.assert_not_called()
        self.session.assert_not_called()

    def test_different_instance_cannot_receive_a_key_packet(self):
        keys = client.keys
        started = {"format": "gift-ledger-gui-key-target-v1", "project": "gift-test",
                   "instance_id": SOURCE["source_instance_id"], "operation_id": OP, "started_at": "2026-11-19"}
        target = {**started, "success": True, "recipient": "age1" + "a" * 58,
                  "started_sha256": keys.digest(keys.encoded(started))}
        raw = keys.encoded({"target": target, "registered": False})
        self.assertEqual(keys.target_info(raw, "gift-test")[0], target)
        args = SimpleNamespace(host="owner@192.168.10.6", project="gift-test", project_dir=None,
                               site_dir=None, compose_file=None, site_url=TARGET["url"],
                               instance_id=TARGET["instance_id"], restore_key=False)
        worker = mock.Mock()
        worker.poll.return_value = 0
        reader = mock.Mock(returncode=0)
        reader.communicate.return_value = (raw, b"")
        with mock.patch.object(keys, "run", return_value=SimpleNamespace(
                returncode=0, stdout=b"/tmp/giftledger-key-AbCd0123\n")) as remote, \
                mock.patch.object(keys.subprocess, "Popen", side_effect=[worker, reader]), \
                mock.patch.object(keys, "packet_for") as packet, self.assertRaises(keys.EnrollmentError):
            keys._exchange(["ssh"], args, b"original", "recipient", self.age, {})
        packet.assert_not_called()
        self.assertFalse(any(len(c.args) > 3 for c in remote.call_args_list))


@unittest.skipUnless(os.name == "posix", "the discovery command runs in the SSH server's POSIX shell")
class MacProjectDiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.scratch = "/tmp/giftledger-key-AbCd0123"
        self.args = SimpleNamespace(project="gift-test", host="owner@192.168.10.6", project_dir=None,
                                    compose_file=None, site_dir=None, site_url=TARGET["url"],
                                    instance_id=TARGET["instance_id"], restore_key=False)
        self.dir = self.root / "站主's YAML $(false)"
        self.dir.mkdir()
        self.config = self.dir / "自定义 compose.yml"
        self.config.touch()
        self.station = self.root / "目录 与项目名不同"
        (self.station / "data").mkdir(parents=True)
        (self.station / "data/网站地址.txt").write_text(TARGET["url"] + "\n")
        self.rows = [
            {"id": "abc123", "project": "gift-test", "dir": str(self.dir), "config": str(self.config),
             "station": str(self.station)},
            {"id": "abc456", "project": "gift-test-old", "dir": "/other", "config": "/other/compose.yml",
             "station": "/other"},
        ]
        self.call = self.root / "compose-call"
        self.prompt = self.root / "sudo-prompts"
        sudo = self.root / "sudo"
        sudo.write_text('#!/bin/sh\n[ "$1" = -p ] || exit 1\nprintf "%s\\n" "$2" >> "$QA_SUDO_PROMPTS"\n'
                        'shift 2\nexport QA_ELEVATED=1\nexec "$@"\n')
        sudo.chmod(0o700)
        docker = self.root / "docker"
        docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
rows = json.loads(os.environ["QA_PROJECTS"])
cmd, *args = sys.argv[1:]
if os.environ["QA_NEEDS_SUDO"] == "1" and "QA_ELEVATED" not in os.environ:
    sys.exit(1)
if cmd == "info":
    sys.exit(0)
elif cmd == "ps":
    for row in rows:
        labels = ["project=" + row["project"], "service=prepare", "oneoff=False"]
        if all("label=com.docker.compose." + label in args for label in labels):
            print(row["id"])
elif cmd == "inspect":
    template = args[1]
    row = next(row for row in rows if row["id"] == args[-1])
    field = next(key for label, key in (("working_dir", "dir"), ("config_files", "config"), ("/station", "station"))
                 if label in template)
    print(row[field])
elif cmd == "compose":
    Path(os.environ["QA_COMPOSE_CALL"]).write_text(json.dumps([args, os.environ["GIFTLEDGER_SITE_DIR"]]))
else:
    sys.exit(90)
""")
        docker.chmod(0o700)

    def run_worker(self, *, needs_sudo=False):
        command = client.keys.enrollment_worker_command(self.args, self.scratch)
        env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
               "QA_PROJECTS": json.dumps(self.rows), "QA_COMPOSE_CALL": str(self.call),
               "QA_SUDO_PROMPTS": str(self.prompt), "QA_NEEDS_SUDO": str(int(needs_sudo))}
        return subprocess.run(["/bin/sh", "-c", command], cwd=self.root, env=env, capture_output=True, text=True)

    def test_unique_project_uses_its_exact_paths_in_the_registration_call(self):
        result = self.run_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        options, station = json.loads(self.call.read_text())
        self.assertEqual(options[options.index("--project-directory") + 1], str(self.dir))
        self.assertEqual(options[options.index("-f") + 1], str(self.config))
        self.assertEqual(options[options.index("-p") + 1], self.args.project)
        self.assertEqual(options[-1], "backup-key")
        self.assertEqual(station, str(self.station))

    def test_discovery_and_registration_share_a_single_sudo_prompt(self):
        result = self.run_worker(needs_sudo=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.call.exists())
        prompts = self.prompt.read_text().splitlines()
        self.assertEqual(len(prompts), 1)
        self.assertIn("ABC", prompts[0])

    def test_first_guide_rejects_missing_ambiguous_and_wrong_site_before_compose(self):
        for problem in ("wrong-url", "missing-project", "ambiguous-project", "missing-yaml"):
            with self.subTest(problem=problem):
                if problem == "wrong-url":
                    self.args.site_url = TARGET["url"].replace("28443", "28444")
                elif problem == "missing-project":
                    self.args.project = "missing"
                elif problem == "ambiguous-project":
                    self.rows.append({**self.rows[0], "id": "def123"})
                else:
                    self.config.unlink()
                self.assertNotEqual(self.run_worker().returncode, 0)
                self.assertFalse(self.call.exists())
                self.args.site_url = TARGET["url"]
                self.args.project = "gift-test"
                self.rows = self.rows[:2]


class FakeSite:
    def __init__(self):
        self.log = []
        self.created = True
        self.status = {"stage": "none"}
        self.proof = {"eligible": True, "full_decryption_verified": True, "source": SOURCE,
                      "target": TARGET, "operation_id": OP}
        self.result = {"source": SOURCE, "target": TARGET, "operation_id": OP, "backup_operation_id": ID,
                       "gift_count": 2, "full_database_and_app_files_verified": True}

    def upload(self, bundle):
        self.log.append("upload")

    def compose(self, service, *args, incoming=False):
        if service == "restore-files":
            action, bid, digest = args
            assert bid == ID and digest == HASH and incoming == (action == "stage")
            self.log.append(action)
            return {"action": action, "backup_id": bid, "manifest_sha256": digest,
                    "created": self.created if incoming else False}
        assert service == "restore" and args[0] == ID
        self.log.append(args[1:])
        if "--status" in args:
            return self.status
        if "--check-only" in args:
            return self.proof
        assert args[-2:] == ("--operation", OP)
        if "--cancel" in args:
            return {"cancelled": True, "operation_id": OP}
        return self.result


class MacRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.b = SimpleNamespace(backup_id=ID, manifest_sha256=HASH, filenames=FILES, source=SOURCE, gift_count=2)
        self.args = SimpleNamespace(project="new", host="unused", project_dir="/srv/site with spaces")
        self.site = FakeSite()
        self.enroll = mock.Mock(return_value={"project": "new", "instance_id": TARGET["instance_id"]})
        self.ask = mock.Mock(return_value=True)

    def run_flow(self):
        def finish(remote, bundle, operation, verify, announce, retired, waiting):
            result = verify(remote.compose("restore", bundle.backup_id, "--operation", operation))
            retired(remote.compose("restore-files", "retire-owned", bundle.backup_id, bundle.manifest_sha256))
            return result
        return client.recover(self.b, self.args, self.site, self.enroll, self.ask, finish=finish)

    def test_confirm_once_and_retire_only_our_exact_copy(self):
        result = self.run_flow()
        self.assertEqual(result["gift_count"], 2)
        self.enroll.assert_called_once()
        self.ask.assert_called_once()
        for expected in [TARGET["url"], TARGET["instance_id"], "2 笔"]:
            self.assertIn(expected, self.ask.call_args.args[0])
        self.assertEqual(self.site.log, [("--status",), "upload", "stage", ("--check-only",),
                                         ("--operation", OP), "retire-owned"])

    def test_preexisting_copy_is_retained(self):
        self.site.created = False
        self.run_flow()
        self.assertEqual(self.site.log[-1], "retire-owned")

    def test_site_guide_options_select_existing_yaml_and_data_and_match_receipt(self):
        self.args.compose_file = "/data/演练 '一'/站点.yml"
        self.args.site_dir = "/data/两 个目录/站点"
        self.args.site_url = TARGET["url"].replace("https://", "HTTPS://")
        command = shlex.split(client.keys.compose_command(self.args, ["run", "--rm", "backup-key"]))
        self.assertIn("GIFTLEDGER_SITE_DIR=" + self.args.site_dir, command)
        self.assertIn("GIFTLEDGER_HTTPS_PORT=28443", command)
        self.assertEqual(command[command.index("-f") + 1], self.args.compose_file)
        self.run_flow()
        self.args.site_url = "https://192.168.10.6:28444/"
        self.site.log.clear()
        with self.assertRaisesRegex(client.keys.EnrollmentError, "网址"):
            self.run_flow()
        self.assertNotIn(("--operation", OP), self.site.log)

    @unittest.skipUnless(os.name == "posix", "Linux maintenance service")
    def test_restore_status_binds_supplied_url_to_the_projects_actual_https_mapping(self):
        root = Path(__file__).resolve().parents[2]
        with mock.patch.object(sys, "path", [str(root / "deploy/release/backup_ops"), *sys.path]):
            import restore_site
        ip = "192.0.2.20"
        actual = [{"HostIp": ip, "HostPort": "16447"}]
        other = [{"HostIp": ip, "HostPort": "26443"}]
        profile = {"containers": {"web": {"id": "fixture-web"}}}
        for wanted_ip, wanted_port, runtime, running, should_match in (
            (ip, "26443", actual, True, False), ("192.0.2.21", "16447", actual, True, False),
            (ip, "16447", other, True, False), (ip, "16447", actual, True, True),
            (ip, "16447", None, False, True),
        ):
            with self.subTest(ip=wanted_ip, port=wanted_port, runtime=runtime, running=running), \
                 mock.patch.dict(os.environ, {"GIFTLEDGER_PROJECT": "fixture", "GIFTLEDGER_SITE_IP": wanted_ip,
                                              "GIFTLEDGER_HTTPS_PORT": wanted_port}), \
                 mock.patch.object(restore_site.site, "SiteBindings") as bindings, \
                 mock.patch.object(restore_site.ops, "Docker") as docker, \
                 mock.patch.object(restore_site, "restore_status", return_value={"stage": "none"}) as status:
                bindings.return_value.runtime_profile.return_value = profile
                docker.return_value.call.return_value = json.dumps([{"443/": actual}, {"443/tcp": runtime}, running])
                if should_match:
                    self.assertEqual(restore_site.restore(ID, "restore_identity", status=True,
                                                          expected_web=(wanted_ip, wanted_port)), {"stage": "none"})
                    status.assert_called_once()
                else:
                    with self.assertRaisesRegex(restore_site.ops.BackupOpsError, "restore_site_port_mismatch"):
                        restore_site.restore(ID, "restore_identity", status=True, expected_web=(wanted_ip, wanted_port))
                    status.assert_not_called()
                self.assertEqual(docker.return_value.call.call_args.args[-1], "fixture-web")

    def test_cancel_binds_preflight_then_retires_managed_input(self):
        self.ask.return_value = False
        self.assertIsNone(self.run_flow())
        self.assertEqual(self.site.log[-2:], [("--cancel", "--operation", OP), "retire-owned"])

    def test_already_completed_uses_no_new_key_or_approval(self):
        self.site.status = {**self.site.proof, "stage": "completed", "bundle_sha256": HASH,
                            "result": self.site.result, "temporary_key_cleanup_pending": True}
        self.run_flow()
        self.enroll.assert_not_called()
        self.ask.assert_not_called()
        self.assertEqual(self.site.log, [("--status",), ("--operation", OP), "retire-owned"])

    def test_resume_after_writes_requires_same_operation_and_no_second_key(self):
        self.site.status = {**self.site.proof, "stage": "applying", "bundle_sha256": HASH,
                            "identity_role": "restore_identity"}
        self.run_flow()
        self.assertEqual(self.site.log, [("--status",), ("--operation", OP), "retire-owned"])
        self.enroll.assert_not_called()
        self.ask.return_value = False
        self.site.log.clear()
        self.assertIsNone(self.run_flow())
        self.assertEqual(self.site.log, [("--status",)])

    def test_wrong_source_and_wrong_restored_count_stop(self):
        self.site.proof["source"] = {}
        with self.assertRaises(client.keys.EnrollmentError):
            self.run_flow()
        self.ask.assert_not_called()
        self.site.proof["source"] = SOURCE
        self.site.result["gift_count"] = 3
        with self.assertRaises(client.keys.EnrollmentError):
            self.run_flow()
        self.assertNotIn("retire-owned", self.site.log)

    def test_failed_enrollment_retires_only_without_an_active_restore(self):
        self.enroll.side_effect = client.keys.EnrollmentError("wrong key")
        with self.assertRaises(client.keys.EnrollmentError):
            self.run_flow()
        self.assertEqual(self.site.log[-2:], [("--status",), "retire-owned"])
        def fail_after_start():
            self.site.status = {"stage":"applying"}
            raise OSError("disconnected")
        self.site.log.clear()
        self.enroll.side_effect = fail_after_start
        with self.assertRaises(OSError):
            self.run_flow()
        self.assertNotIn("retire-owned", self.site.log)

    def test_streams_exact_five_files_and_reads_sudo_compose_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.b.folder = base
            for i, name in enumerate(FILES):
                (base / name).write_bytes(bytes([i]) * 8000)
            (base / "ssh").write_text("#!/bin/sh\nexec /bin/sh -c \"$3\"\n")
            (base / "sudo").write_text('#!/bin/sh\n[ "$1" = -p ] && shift 2\nexport TEST_BODY=1\nexec "$@"\n')
            (base / "docker").write_text('#!/bin/sh\n[ "$1" != info ] || exit 1\n'
                                         'test "$TEST_BODY" = 1 && printf \'{"args":true}\\n\'\n')
            for cmd in ("ssh", "sudo", "docker"):
                (base / cmd).chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": str(base) + os.pathsep + os.environ["PATH"]}):
                with client.remote_files([str(base / "ssh")], self.args, self.b) as remote:
                    scratch = Path(remote.scratch)
                    self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
                    remote.upload(self.b)
                    folder = scratch / "five"
                    self.assertEqual({f.name for f in folder.iterdir()}, set(FILES))
                    for name in FILES:
                        self.assertEqual((folder / name).read_bytes(), (base / name).read_bytes())
                        self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600)
                    self.assertEqual(remote.compose("restore", ID, "--status"), {"args": True})
                self.assertFalse(scratch.exists())

    def test_final_write_and_cleanup_share_authorization_after_verified_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # A native sudo timestamp is normally scoped to the SSH terminal. Use
            # one marker per SSH process to count prompts without a real password.
            (base / "ssh").write_text('#!/bin/sh\nexport QA_TTY=$$\nexec /bin/sh -c "$3"\n')
            (base / "sudo").write_text('#!/bin/sh\n[ "$1" = -p ] || exit 1\n'
                'if [ ! -e "$QA_DIR/$QA_TTY" ]; then echo "$2" >> "$QA_DIR/prompts"; touch "$QA_DIR/$QA_TTY"; fi\n'
                'shift 2\nexec "$@"\n')
            (base / "docker").write_text('#!/bin/sh\n[ "$1" != info ] || exit 1\n'
                'echo "$*" >> "$QA_DIR/operations"\nprintf \'{"ok":true}\\n\'\n')
            for name in ("ssh", "sudo", "docker"):
                (base / name).chmod(0o700)
            remote = client.Remote([str(base / "ssh")], self.args, tmp)
            announce = mock.Mock()
            retired = mock.Mock()
            def verify(value):
                self.assertNotIn("retire-owned", (base / "operations").read_text())
                self.assertEqual(value, {"ok": True})
                return value
            with mock.patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"], "QA_DIR": tmp}):
                result = client.finish_in_one_session(remote, self.b, OP, verify, announce, retired, client.waiting)
            self.assertEqual(result, {"ok": True})
            announce.assert_called_once_with(result)
            retired.assert_called_once_with(result)
            self.assertEqual(len((base / "prompts").read_text().splitlines()), 1)
            self.assertIn("retire-owned", (base / "operations").read_text())


class ManagedRestoreCopies(unittest.TestCase):
    def setUp(self):
        items = store_fixture.StoreTests()
        items.setUp()
        self.addCleanup(items.doCleanups)
        _, backup, _, _ = items.sample()
        self.backup_id = backup["operation_id"]
        self.source = items.store / "artifacts" / self.backup_id
        self.root = items.root / "new-station"
        self.root.mkdir(mode=0o700)
        self.copy = self.root / self.backup_id
        self.digest = hashlib.sha256((self.source / "BUNDLE.json").read_bytes()).hexdigest()

    def action(self, mode):
        return restore_files.stage_or_retire(mode, self.backup_id, self.digest, root=self.root, incoming=self.source)

    def test_identical_user_supplied_or_replaced_directory_is_preserved(self):
        shutil.copytree(self.source, self.copy)
        self.assertFalse(self.action("stage")["created"])
        self.assertTrue(self.action("retire-owned")["preserved"])
        shutil.rmtree(self.copy)
        self.assertTrue(self.action("stage")["created"])
        self.copy.rename(self.root / "original")
        shutil.copytree(self.source, self.copy)
        self.assertTrue(self.action("retire-owned")["preserved"])
        self.assertTrue(self.copy.is_dir())

    def test_changed_file_is_preserved(self):
        self.assertTrue(self.action("stage")["created"])
        self.assertFalse(self.action("stage")["created"])
        original = self.copy / "verify.json"
        original.rename(self.root / "original-verify")
        original.write_bytes((self.root / "original-verify").read_bytes())
        self.assertTrue(self.action("retire-owned")["preserved"])
        original.unlink()
        (self.root / "original-verify").rename(original)
        # This replacement changed ctime even after restoring the same inode.
        self.assertTrue(self.action("retire-owned")["preserved"])

    def test_owned_partial_retirement_and_repeated_retirement(self):
        self.action("stage")
        partial = self.root / f".retiring-{self.backup_id}-{self.digest}"
        self.copy.rename(partial)
        (partial / "verify.json").unlink()
        self.assertTrue(self.action("retire-owned")["retired"])
        self.assertFalse(partial.exists())
        self.assertFalse(self.action("retire-owned")["retired"])


if __name__ == "__main__":
    unittest.main()
