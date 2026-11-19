"""Exercise the distributed site-owner client on a native Windows filesystem."""

import os
from contextlib import ExitStack
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

class WindowsTools(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        here = Path(__file__).resolve().parent
        if (here / "GiftLedger.cmd").is_file():
            # The field receipt must exercise the actual extracted delivery.
            self.tools = here
        else:
            root = here.parents[1]
            sys.path.insert(0, str(root / "deploy/publication"))
            self.addCleanup(sys.path.remove, str(root / "deploy/publication"))
            import images

            self.tools = self.root / "站主 tools"
            yaml = (root / "deploy/single_yaml/docker-compose.yml").read_bytes()
            archive = self.root / "windows-site-tools.zip"
            images.write_site_tools_zip(archive, yaml, windows=True)
            with ZipFile(archive) as package:
                package.extractall(self.tools)
        with mock.patch.object(sys, "path", [str(self.tools), *sys.path]), mock.patch.dict(sys.modules):
            for name in ("windows_site", "mac_backup_key", "mac_restore", "mac_restore_bundle", "backup_store", "backup_export"):
                sys.modules.pop(name, None)
            self.client = importlib.import_module("windows_site")

    @unittest.skipUnless(os.name == "nt", "Windows shell required")
    def test_packaged_double_click_entry_supports_cli(self):
        result = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", "GiftLedger.cmd --help"],
            cwd=self.tools, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"--host", result.stdout)
        for option in (b"--site-url", b"--site-dir", b"--compose-file"):
            self.assertIn(option, result.stdout)

    @unittest.skipUnless(os.name == "nt", "NTFS ACLs required")
    def test_generated_key_readback_and_refusal_of_shared_acl(self):
        path = self.root / "合成测试.agekey"
        secret = b"AGE-SECRET-KEY-1" + b"A" * 58 + b"\n"
        public = b"age1" + b"q" * 58
        def age_keygen(program, arguments=(), data=None):
            self.assertEqual(program, "age-keygen")
            if not arguments:
                return secret
            self.assertEqual((arguments, data), (["-y"], secret))
            return public
        age = mock.Mock(**{"run.side_effect": age_keygen})
        first = self.client.custody(path, True, age)
        self.assertEqual(first, (secret, public.decode("ascii")))
        self.assertEqual(self.client.custody(path, False, age), first)
        subprocess.run(["icacls.exe", str(path), "/grant", "*S-1-5-32-545:(R)"],
                       check=True, capture_output=True)
        with self.assertRaises((ValueError, PermissionError, subprocess.CalledProcessError)):
            self.client.custody(path, False, age)
        self.assertEqual(path.read_bytes(), secret)

    def test_genuine_prompt_follows_one_home_notice_and_other_errors_are_preserved(self):
        client = self.client.keys
        host = 'siteowner@nas.example'
        warning = b'Could not chdir to home directory /var/services/homes/siteowner: No such file or directory\r\n'
        prompt = '请输入服务器管理员密码：'
        other = 'Could not chdir to home directory /var/services/homes/other: Permission denied\n'
        chunks = (warning[:8], warning[8:] + warning, other.encode(), prompt.encode(), b'')
        reader = mock.Mock()
        reader.read.side_effect = chunks
        screen = io.StringIO()
        with mock.patch.object(client, '_explained_home', set()):
            client._show_ssh_output(reader, screen, host, 'siteowner')
        self.assertEqual(screen.getvalue().count('群晖 SSH 用户没有个人目录'), 1)
        self.assertTrue(screen.getvalue().endswith(other + prompt))

    def test_windows_sudo_prompt_identifies_account_input_mode_and_no_echo(self):
        import re
        keys = self.client.keys
        with mock.patch.object(os, "name", "nt"):
            command = keys.sudo_command("true", "登记日常备份钥匙")
        # Plink sends an ASCII command. The remote printf reconstructs the UTF-8 prompt for sudo.
        octets = re.search(r"printf '((?:\\[0-7]{3})+)'", command).group(1)
        prompt = bytes(int(part, 8) for part in octets.split("\\")[1:]).decode("utf-8")
        self.assertIn("登记日常备份钥匙", prompt)
        self.assertIn("核对输入法", prompt)
        self.assertIn("英文", prompt)
        self.assertIn("%p", prompt)
        self.assertIn("不回显", prompt)

    def test_success_notice_identifies_the_verified_site_and_survives_dialog_failure(self):
        product = self.client.restore
        site = 'https://test.invalid/'
        with mock.patch.object(os, 'name', 'nt'), mock.patch.object(product, 'windows_dialog', side_effect=OSError) as box, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as text:
            product.announce_verified({'gift_count': 3, 'target': {'url': site}})
        title, body, flags = box.call_args.args
        self.assertIn('恢复成功', title)
        self.assertIn(site, body)
        self.assertIn('3 笔', body)
        self.assertIn('收尾', text.getvalue())

    def test_windows_confirmation_preserves_target_and_defaults_to_cancel(self):
        import inspect

        confirm = inspect.signature(self.client.restore.recover).parameters["confirm"].default
        with mock.patch.object(os, "name", "nt"), mock.patch.object(self.client.restore.ctypes, "WinDLL", create=True) as winlib, \
                mock.patch("builtins.input", side_effect=AssertionError("console input attempted")):
            dialog = winlib.return_value.MessageBoxW
            message = "源站 test，共 3 笔。\n目标网址 https://example.invalid/\n目标项目 target，实例 example。"
            for code, approved in ((1, True), (2, False)):
                dialog.return_value = code
                self.assertIs(confirm(message), approved)
                owner, body, title, flags = dialog.call_args.args
                self.assertIsNone(owner)
                self.assertIn(message, body)
                self.assertIn("确认恢复", title)
                self.assertEqual(flags & 0xf, 1)  # OK / Cancel
                self.assertEqual(flags & 0x300, 0x100)  # Second button is the default.
            for code in (0, 6):
                dialog.return_value = code
                with self.assertRaisesRegex(self.client.keys.EnrollmentError, "确认窗口"):
                    confirm(message)

    def test_restore_console_mode_uses_exact_original_flags_even_on_error(self):
        with mock.patch.object(self.client.ctypes, "WinDLL", create=True) as winlib:
            kernel = winlib.return_value
            kernel.GetStdHandle.return_value = 42
            def get_mode(handle, value):
                value._obj.value = 0x1b1
                return 1
            kernel.GetConsoleMode.side_effect = get_mode
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with self.client.preserve_console_input_mode():
                    kernel.SetConsoleMode.assert_not_called()
                    raise RuntimeError("interrupted")
            kernel.SetConsoleMode.assert_called_once_with(42, 0x1b1)
            kernel.SetConsoleMode.reset_mock()
            kernel.GetConsoleMode.side_effect = None
            kernel.GetConsoleMode.return_value = 0
            with self.client.preserve_console_input_mode():
                pass
            kernel.SetConsoleMode.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows entry point required")
    def test_result_file_requires_verified_result_and_never_replaces_a_receipt(self):
        destination = self.root / "result.json"
        args = ["GiftLedger.cmd", "restore", "--host", "operator@nas", "--project", "ledger",
                "--project-dir", "/srv/ledger", "--backup", str(self.root), "--result-file", str(destination)]
        result = {"operation_id": "operation", "target": {"project": "ledger", "instance_id": "instance",
                  "url": "https://example.invalid/"}, "gift_count": 3, "backup_operation_id": "backup",
                  "source": {"source_project": "source"}, "full_database_and_app_files_verified": True,
                  "extra": "must not be exported"}
        with mock.patch.object(sys, "argv", args), mock.patch.object(self.client, "preserve_console_input_mode"), \
                mock.patch.object(self.client.restore, "restore_selected", side_effect=[None, result]) as run:
            self.client.main()
            self.assertFalse(destination.exists())
            self.client.main()
            receipt = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(receipt, {"operation_id": "operation", "project": "ledger", "instance_id": "instance",
                             "url": "https://example.invalid/", "gift_count": 3, "backup_id": "backup",
                             "source_project": "source", "full_database_and_app_files_verified": True})
            snapshot = destination.read_bytes()
            with self.assertRaisesRegex(self.client.keys.EnrollmentError, "已存在"):
                self.client.main()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(destination.read_bytes(), snapshot)

    def mock_plink(self, probe_results):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(self.client, "find_plink", return_value="plink.exe"))
        stack.enter_context(mock.patch.object(self.client, "system_directory", return_value=self.root))
        stack.enter_context(mock.patch.object(self.client.time, "sleep"))
        stack.enter_context(mock.patch.object(subprocess, "CREATE_NEW_CONSOLE", 0x10, create=True))
        probes = stack.enter_context(mock.patch.object(subprocess, "run", side_effect=probe_results))
        start = stack.enter_context(mock.patch.object(subprocess, "Popen"))
        start.return_value.poll.return_value = None
        return probes, start

    def test_menu_accepts_ssh_host_and_nonstandard_port_after_bad_input(self):
        args = SimpleNamespace(action=None, host=None, port=None, project_dir=None, project=None,
                               site_url=None, site_dir=None, compose_file=None)
        answers = ["2", "https://nas.example:8443", "https://192.0.2.20:9443/", "owner@nas.example", "abc", "0", "65536", "2222",
                   "/volume1/docker/ledger", "test-ledger"]
        with mock.patch("builtins.input", side_effect=answers) as prompts:
            self.client.request_site(args)
        self.assertEqual(prompts.call_count, len(answers))
        self.assertEqual((args.action, args.host, args.port), ("restore", "owner@nas.example", 2222))
        self.assertEqual((args.project_dir, args.project), ("/volume1/docker/ledger", "test-ledger"))
        self.assertEqual(args.site_url, "https://192.0.2.20:9443/")

    def test_enrollment_menu_selects_new_or_existing_key_before_ssh(self):
        path = self.root / "backup.agekey"
        for choice, picker, generate in (("1", "select-new", True), ("2", "select-key", False)):
            with self.subTest(choice=choice), ExitStack() as stack:
                patch = stack.enter_context
                patch(mock.patch.object(sys, "argv", ["GiftLedger.cmd"]))
                patch(mock.patch.object(self.client, "os", SimpleNamespace(name="nt")))
                answers = ["1", "owner@nas", "", "/volume1/docker/ledger", "ledger", choice]
                prompts = patch(mock.patch("builtins.input", side_effect=answers))
                select = patch(mock.patch.object(self.client, "choose", return_value=path))
                age = patch(mock.patch.object(self.client.keys, "Age")).return_value
                custody = patch(mock.patch.object(self.client, "custody", side_effect=OSError("test file")))
                patch(mock.patch("sys.stdout", new_callable=io.StringIO))
                with self.assertRaisesRegex(OSError, "test file"):
                    self.client.main()
                enrollment = prompts.call_args.args[0]
                self.assertIn("1 生成新钥匙并登记", enrollment.splitlines())
                self.assertIn("2 登记旧钥匙", enrollment.splitlines())
                select.assert_called_once_with(picker)
                custody.assert_called_once_with(path, generate, age)

    def test_restore_menu_requests_only_the_existing_key_for_the_backup(self):
        answers = ["2", "https://192.0.2.20:9443/", "owner@nas", "", "/volume1/docker/ledger", "ledger"]
        backup = self.root / "old.zip"
        key = self.root / "original.agekey"
        with ExitStack() as stack:
            patch = stack.enter_context
            patch(mock.patch.object(sys, "argv", ["GiftLedger.cmd"]))
            patch(mock.patch.object(self.client, "os", SimpleNamespace(name="nt")))
            prompts = patch(mock.patch("builtins.input", side_effect=answers))
            patch(mock.patch("sys.stdout", new_callable=io.StringIO))
            pick = patch(mock.patch.object(self.client, "choose", side_effect=[backup, key]))
            age = patch(mock.patch.object(self.client.keys, "Age")).return_value
            custody = patch(mock.patch.object(self.client, "custody", return_value=(b"test", "public")))
            patch(mock.patch.object(self.client, "preserve_console_input_mode"))
            patch(mock.patch.object(self.client.restore, "restore_selected",
                                    side_effect=lambda args, source, session, load_key: load_key()))
            self.client.main()
        self.assertEqual(prompts.call_count, len(answers))
        self.assertEqual(pick.call_args_list, [mock.call("select-backup"), mock.call("select-key")])
        custody.assert_called_once_with(key, False, age)

    def test_default_ssh_port_and_explicit_port_never_prompt_batch_users(self):
        for action, port, answers, expected in [
            (None, None, ["1", ""], 22), (None, 2222, ["2", "https://192.0.2.20:9443/"], 2222),
            ("restore", None, [], 22), ("enroll", 65535, [], 65535),
        ]:
            with self.subTest(action=action, port=port):
                args = SimpleNamespace(action=action, host="owner@nas", port=port,
                                       project_dir="/srv/site", project="ledger", site_url=None,
                                       site_dir=None, compose_file=None)
                with mock.patch("builtins.input", side_effect=answers) as prompts:
                    self.client.request_site(args)
                self.assertEqual(prompts.call_count, len(answers))
                self.assertEqual(args.port, expected)

    def test_restore_menu_requires_target_url_before_connecting_or_choosing_a_backup(self):
        with mock.patch.object(sys, "argv", ["GiftLedger.cmd"]), \
             mock.patch.object(self.client, "os", SimpleNamespace(name="nt")), \
             mock.patch("builtins.input", side_effect=["2", ""]), \
             mock.patch.object(self.client, "choose") as choose, mock.patch.object(self.client, "session") as ssh, \
             self.assertRaisesRegex(self.client.keys.EnrollmentError, "已退出"):
            self.client.main()
        choose.assert_not_called()
        ssh.assert_not_called()

    def test_windows_guide_command_passes_site_paths_and_expected_port_to_remote_status(self):
        args = ["GiftLedger.cmd", "restore", "--project", "test-ledger", "--site-url", "https://192.0.2.20:9443/",
                "--project-dir", "/srv/project files", "--site-dir", "/srv/station", "--compose-file", "/srv/custom.yml",
                "--backup", "fixture.zip"]
        with mock.patch.object(sys, "argv", args), mock.patch.object(self.client, "os", SimpleNamespace(name="nt")), \
             mock.patch("builtins.input", side_effect=["operator", "2222"]) as prompts, \
             mock.patch.object(self.client, "preserve_console_input_mode"), \
             mock.patch.object(self.client.restore, "restore_selected", return_value=None) as run:
            self.client.main()
        self.assertEqual(prompts.call_count, 2)
        options = run.call_args.args[0]
        self.assertEqual((options.host, options.port), ("operator@192.0.2.20", 2222))
        remote = self.client.restore.Remote([], options, "/tmp/gl-fixture")
        status = remote.compose_script(["restore", "1" * 32, "--status"], output="/tmp/receipt")
        self.assertIn("--project-directory '/srv/project files' -f /srv/custom.yml -p test-ledger", status)
        self.assertIn("GIFTLEDGER_SITE_DIR=/srv/station", status)
        self.assertIn("--status --expect-web 192.0.2.20 9443", status)
        stage = remote.compose_script(["restore-files", "stage"], output="/tmp/receipt")
        self.assertNotIn("--expect-web", stage)

    def test_slow_login_waits_for_a_working_channel_and_leaves_stdin_to_the_owner(self):
        ok = subprocess.CompletedProcess([], 0)
        timeout = subprocess.TimeoutExpired("plink", 10)
        probes, start = self.mock_plink([subprocess.CompletedProcess([], 1), timeout, ok, timeout, ok, ok])
        with self.client.session("operator@nas", 22) as channel:
            self.assertEqual(channel[-1], "-batch")
        self.assertEqual([call.args[0][-1] for call in probes.call_args_list],
                         ["operator@nas", "operator@nas", "operator@nas", "true", "operator@nas", "true"])
        for call in probes.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertIn("-batch", call.args[0])
        self.assertNotIn("-batch", start.call_args.args[0])
        self.assertEqual(start.call_args.kwargs, {"creationflags": subprocess.CREATE_NEW_CONSOLE})
        start.return_value.terminate.assert_called_once()

    def test_reuses_working_upstream_without_closing_it(self):
        ok = subprocess.CompletedProcess([], 0)
        probes, start = self.mock_plink([ok, ok])
        with self.client.session("operator@nas", 2222) as channel:
            self.assertEqual(channel[channel.index("-P") + 1], "2222")
            self.assertEqual(probes.call_args.args[0][-3:], ["-T", "operator@nas", "true"])
        start.assert_not_called()

    def test_unready_existing_upstream_times_out_without_claiming_or_closing_it(self):
        _, start = self.mock_plink([subprocess.CompletedProcess([], 0), subprocess.TimeoutExpired("plink", 10)])
        with mock.patch.object(self.client.time, "monotonic", side_effect=[0, 241]):
            with self.assertRaisesRegex(self.client.keys.EnrollmentError, "会话就绪超时"):
                with self.client.session("operator@nas", 22):
                    self.fail("Session yielded before a remote command succeeded")
        start.assert_not_called()

    def test_receipt_readers_never_compete_for_sudo_input(self):
        keys = self.client.keys
        channel = ["plink.exe", "-share", "-batch"]
        for registered in (False, True):
            with self.subTest(registered=registered), ExitStack() as stack:
                args = SimpleNamespace(host="operator@nas", project="test", project_dir="/test", restore_key=registered)
                stack.enter_context(mock.patch.object(keys, "run", return_value=subprocess.CompletedProcess(
                    [], 0, b"/tmp/giftledger-key-12345678")))
                stack.enter_context(mock.patch.object(keys, "target_info", return_value=({}, registered)))
                stack.enter_context(mock.patch.object(keys, "packet_for", return_value={"request": {}}))
                verified = stack.enter_context(mock.patch.object(keys, "verify_receipt"))
                stack.enter_context(mock.patch.object(keys.sys, "stdin", **{"isatty.return_value": True}))
                worker = mock.Mock()
                worker.wait.return_value = worker.poll.return_value = 0
                reader = mock.Mock(returncode=0)
                reader.communicate.side_effect = [(b"target", b""), (b"receipt", b"")]
                reader.poll.return_value = 0
                start = stack.enter_context(mock.patch.object(subprocess, "Popen", side_effect=[worker, reader, reader]))
                self.assertIs(keys.exchange(channel, args, b"synthetic", "public", None), verified.return_value)
                foreground, target, receipt = start.call_args_list
                self.assertNotIn("stdin", foreground.kwargs)
                self.assertIn("-t", foreground.args[0])
                for call in (target, receipt):
                    self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
                    self.assertIn("-T", call.args[0])

    def test_enrollment_releases_ssh_output_even_if_remote_cancel_times_out(self):
        keys = self.client.keys
        remote_timeout = subprocess.TimeoutExpired("ssh", 20)
        worker = mock.Mock()
        worker.poll.return_value = None
        worker.wait.side_effect = [subprocess.TimeoutExpired("ssh", 5), 0]
        reader = mock.Mock(returncode=1)
        reader.poll.return_value = 1
        reader.communicate.return_value = (b"", b"")
        args = SimpleNamespace(host="operator@nas", project="ledger", project_dir="/srv/ledger", restore_key=True)
        calls = [subprocess.CompletedProcess([], 0, b"/tmp/giftledger-key-12345678"),
                 remote_timeout, subprocess.CompletedProcess([], 0)]
        with mock.patch.object(keys, "run", side_effect=calls) as commands, \
                mock.patch.object(subprocess, "Popen", side_effect=[worker, reader]), \
                self.assertRaises(subprocess.TimeoutExpired):
            keys.exchange(["plink.exe"], args, b"synthetic", "public", None)
        self.assertIn(": > cancel", commands.call_args_list[1].args[2])
        self.assertIn("rmdir", commands.call_args_list[2].args[2])
        worker.kill.assert_called_once_with()
        self.assertEqual(worker.wait.call_args_list, [mock.call(timeout=5), mock.call()])

    def test_closed_backup_picker_accepts_quoted_zip_manifest_or_directory(self):
        folder = self.root / "备份 副本"
        folder.mkdir()
        manifest = folder / "BUNDLE.json"
        manifest.touch()
        archive = self.root / "下载 备份.ZIP"
        archive.touch()
        for path, expected in ((archive, archive), (manifest, folder), (folder, folder)):
            with self.subTest(path=path), ExitStack() as stack:
                patch = stack.enter_context
                patch(mock.patch.object(self.client, "system_directory", return_value=self.root))
                spawn = patch(mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2, b"")))
                patch(mock.patch.object(sys, "stdin", **{"isatty.return_value": True}))
                answer = patch(mock.patch("builtins.input", return_value=f'"{path}"'))
                self.assertEqual(self.client.choose("select-backup"), expected)
                self.assertIn("文件窗口已关闭", answer.call_args.args[0])
                self.assertEqual(spawn.call_count, 1)

    def test_failed_key_picker_can_reopen_then_paste_after_another_cancel(self):
        key = self.root / "之前保管的 钥匙.agekey"
        key.touch()
        results = [subprocess.CompletedProcess([], 1, b""), subprocess.CompletedProcess([], 2, b"")]
        with mock.patch.object(self.client, "system_directory", return_value=self.root), \
                mock.patch.object(subprocess, "run", side_effect=results) as picker, \
                mock.patch.object(sys, "stdin", **{"isatty.return_value": True}), \
                mock.patch("builtins.input", side_effect=["", str(self.root / "missing"), f'"{key}"']) as answer:
            self.assertEqual(self.client.choose("select-key"), key)
        prompts = [call.args[0] for call in answer.call_args_list]
        self.assertIn("未能打开文件窗口", prompts[0])
        self.assertIn("回车重新打开窗口", prompts[0])
        self.assertIn("文件窗口已关闭", prompts[1])
        self.assertIn("找不到这份已有钥匙", prompts[2])
        self.assertEqual(picker.call_count, 2)

    def test_new_key_picker_rejects_occupied_paths_and_accepts_a_new_filename(self):
        old = self.root / "已有 钥匙.agekey"
        old.write_bytes(b"existing")
        new = self.root / "新 钥匙.agekey"
        answers = [str(self.root / "missing" / new.name), f'"{new}"']
        with mock.patch.object(self.client, "key_helper", return_value=str(old).encode("utf-8")) as picker, \
                mock.patch.object(sys, "stdin", **{"isatty.return_value": True}), \
                mock.patch("builtins.input", side_effect=answers) as answer:
            self.assertEqual(self.client.choose("select-new"), new)
        self.assertIn("已有文件", answer.call_args_list[0].args[0])
        self.assertIn("找不到保存新钥匙的文件夹", answer.call_args_list[1].args[0])
        self.assertEqual(old.read_bytes(), b"existing")
        self.assertFalse(new.exists())
        picker.assert_called_once_with("select-new")

    def test_picker_retry_offers_a_deliberate_exit_and_batch_flag(self):
        for action, flag in (("select-backup", "--backup"), ("select-key", "--key"), ("select-new", "--generate")):
            with self.subTest(action=action), ExitStack() as stack:
                patch = stack.enter_context
                patch(mock.patch.object(self.client, "key_helper", side_effect=FileNotFoundError))
                stdin = patch(mock.patch.object(sys, "stdin", **{"isatty.return_value": True}))
                answer = patch(mock.patch("builtins.input", return_value="Q"))
                with self.assertRaisesRegex(self.client.keys.EnrollmentError, "已退出文件选择"):
                    self.client.choose(action)
                self.assertIn("输入 q 退出", answer.call_args.args[0])
                stdin.isatty.return_value = False
                with self.assertRaisesRegex(self.client.keys.EnrollmentError, flag):
                    self.client.choose(action)
                answer.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
