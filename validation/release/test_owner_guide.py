"""Check the published instructions through their actual shell and prepare entry points."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy/single_yaml"))
import owner_guide
import single_yaml_site as site


PROJECT = "gift-test-restore"
URL = "https://192.0.2.20:9443"
IDENTITY = {"project": PROJECT, "instance_id": "0ac0fcfd-c8ee-4e5e-b6a9-a6aeb637b723",
            "tls": {"root_sha256": "e2" * 32}}

DOCKER_STUB = """#!/bin/sh
set -eu
case "$1" in
  ps)
    printf '%s\\n' "$@" > "$TEST_FILTER"
    printf '%s\\n' "$TEST_IDS"
    ;;
  inspect)
    case "$3" in
      *working_dir*) printf '%s\\n' "$TEST_DIR";;
      *config_files*) printf '%s\\n' "$TEST_CONFIG";;
      *Mounts*) printf '%s\\n' "$TEST_STATION";;
      *) exit 91;;
    esac
    ;;
  cp)
    test "$2" = "$TEST_IDS:/opt/gift-ledger-install/dsm-backup.pyz" || exit 96
    cp "$TEST_PROGRAM" "$3"
    ;;
  compose)
    printf '%s\\n' "$GIFTLEDGER_SITE_DIR" "$GIFTLEDGER_PROJECT_DIR" "$GIFTLEDGER_SITE_IP" "$GIFTLEDGER_HTTPS_PORT" "$@" > "$TEST_CALL"
    exit "${TEST_COMPOSE_EXIT:-0}"
    ;;
  *) exit 92;;
esac
"""


def command(notes, substring):
    blocks = re.findall(r"^\(\n.*?^\)$", notes, flags=re.M | re.S)
    return next(block for block in blocks if substring in block)


class OwnerGuideTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project_dir = self.root / "YAML's folder $(false)"
        self.station = self.root / "different station"
        self.config = self.project_dir / "custom file.yml"
        self.project_dir.mkdir()
        (self.station / "data").mkdir(parents=True)
        self.config.touch()
        (self.root / "docker").write_text(DOCKER_STUB)
        (self.root / "docker").chmod(0o700)
        self.calls = self.root / "called"
        self.env = dict(os.environ, PATH=str(self.root) + ":" + os.environ["PATH"],
                        TEST_IDS="abcdef012345", TEST_DIR=str(self.project_dir),
                        TEST_CONFIG=str(self.config), TEST_STATION=str(self.station),
                        TEST_CALL=str(self.calls), TEST_FILTER=str(self.root / "filter"))

    def test_host_discovery_executes_published_block_for_this_project_and_actual_yaml(self):
        notes = owner_guide.render(PROJECT, URL, IDENTITY)
        self.assertIn("查看本站目录", notes)
        self.assertIn(IDENTITY["tls"]["root_sha256"], notes)
        block = command(notes, "certificate inspect")
        subprocess.run(["/bin/sh", "-c", block], check=True, env=self.env, cwd=self.root)
        self.assertIn("label=com.docker.compose.project=" + PROJECT, (self.root / "filter").read_text())
        self.assertEqual(self.calls.read_text().splitlines(),
                         [str(self.station), str(self.project_dir), "192.0.2.20", "9443", "compose",
                          "--project-directory", str(self.project_dir), "-f", str(self.config),
                          "-p", PROJECT, "run", "--rm", "--no-deps", "certificate", "inspect"])
        subprocess.run(["/bin/sh", "-c", command(notes, "prepare refresh-guide")],
                       check=True, env=self.env, cwd=self.root, stdout=subprocess.DEVNULL)
        self.assertIn("GIFTLEDGER_GUIDE_CONFIG_FILE=" + str(self.config), self.calls.read_text().splitlines())

    def test_ambiguous_project_or_missing_yaml_stops_without_compose(self):
        notes = owner_guide.render(PROJECT, URL, IDENTITY)
        for wrong in ({"TEST_IDS":"abc\ndef"}, {"TEST_IDS":""}, {"TEST_CONFIG":str(self.root / "missing")}):
            with self.subTest(wrong=wrong):
                result = subprocess.run(["/bin/sh", "-c", command(notes, "certificate renew")],
                                        env={**self.env, **wrong}, cwd=self.root, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_known_paths_can_be_copied_into_a_non_interactive_task(self):
        notes = owner_guide.render(PROJECT, URL, IDENTITY, project_dir=str(self.project_dir),
                                   site_dir=str(self.station), config_file=str(self.config))
        self.assertNotIn("inspect -f", command(notes, "certificate inspect"))
        self.assertIn("项目文件夹    " + str(self.project_dir), notes)
        self.assertIn("原钥匙", notes)
        for build in ("24.0.2-1543", "24.0.2-1606", "CM_vendor_file_set_unreviewed"):
            self.assertIn(build, notes)
        subprocess.run(["/bin/sh", "-c", command(notes, "run --rm backup")],
                       check=True, env=self.env, cwd=self.root)
        self.assertEqual(self.calls.read_text().splitlines()[-3:], ["run", "--rm", "backup"])
        self.assertFalse((self.root / "filter").exists())

    def test_dsm_task_runs_isolated_program_from_this_projects_regular_prepare(self):
        dsm = self.root / "dsm-docker"
        dsm.write_text(DOCKER_STUB)
        dsm.chmod(0o700)
        program = self.root / "test-program"
        program.write_text('import os, sys\nfrom pathlib import Path\n'
                           'assert sys.flags.isolated\n'
                           'Path(os.environ["TEST_CALL"]).write_text("\\n".join(sys.argv))\n')
        with patch.object(owner_guide, "DSM_DOCKER", str(dsm)), patch.object(owner_guide, "DSM_PYTHON", sys.executable):
            notes = owner_guide.render(PROJECT, URL, IDENTITY, project_dir=str(self.project_dir),
                                       site_dir=str(self.station), config_file=str(self.config))
        subprocess.run(["/bin/sh", "-c", command(notes, "run --rm backup")], check=True,
                       env={**self.env, "TEST_PROGRAM":str(program)}, cwd=self.root)
        executed, *args = self.calls.read_text().splitlines()
        self.assertEqual(args, ["--project", PROJECT, "--station", str(self.station)])
        self.assertFalse(Path(executed).exists())
        self.assertIn("label=com.docker.compose.project=" + PROJECT, (self.root / "filter").read_text())

    def test_dsm_task_reminders_are_separate_lines_before_their_instructions(self):
        with patch.object(owner_guide, "shipped_digest", return_value="ab" * 32):
            notes = owner_guide.render(PROJECT, URL, IDENTITY, owner_login=True)
        install = command(notes, '"$tmp/maintenance-install.pyz" install')
        self.assertIn(install + "\n★ 创建此触发任务后，群晖重启后才保持维护页面的启用状态，建议群晖设置此任务。\n群晖还需在", notes)
        self.assertIn("\n★ 创建此任务后，群晖才能自动更新网站证书，建议群晖设置此任务。\n群晖自动续签网站证书\n", notes)

    def test_daily_certificate_task_only_calls_compose_on_explicit_due_result(self):
        program = self.root / "probe"
        program.write_text('import json, os, sys\nfrom pathlib import Path\n'
                           'assert sys.flags.isolated\n'
                           'Path(os.environ["TEST_PROBE"]).write_text(json.dumps(sys.argv[1:]))\n'
                           'sys.exit(int(os.environ["PROBE_RESULT"]))\n')
        probe_args = self.root / "probe-args"
        for paths in ({}, {"project_dir": str(self.project_dir), "site_dir": str(self.station),
                           "config_file": str(self.config)}):
            with self.subTest(paths=paths), patch.object(owner_guide, "DSM_PYTHON", sys.executable):
                notes = owner_guide.render(PROJECT, URL, IDENTITY, **paths)
                schedule = notes.split("群晖自动续签网站证书\n", 1)[1].split("续签本站 HTTPS 证书\n", 1)[0]
                self.assertIn("每天一次", schedule)
                self.assertIn("剩余 30 天或更短", schedule)
                block = command(schedule, "certificate auto-renew")
                self.assertNotIn("certificate renew", block)
                for probe_result, expected in ((0, 0), (1, 1), (3, 17)):
                    with self.subTest(probe=probe_result):
                        if self.calls.exists():
                            self.calls.unlink()
                        result = subprocess.run(["/bin/sh", "-c", block], cwd=self.root, capture_output=True,
                                                env={**self.env, "TEST_COMPOSE_EXIT": "17", "TEST_PROGRAM": str(program),
                                                     "TEST_PROBE": str(probe_args), "PROBE_RESULT": str(probe_result)})
                        self.assertEqual(result.returncode, expected, result.stderr)
                        self.assertEqual(json.loads(probe_args.read_text()), ["--check-certificate", "--project",
                                         PROJECT, "--station", str(self.station), "--url", URL + "/",
                                         "--root-sha256", IDENTITY["tls"]["root_sha256"]])
                        self.assertEqual(self.calls.exists(), probe_result == 3)
                        if self.calls.exists():
                            self.assertEqual(self.calls.read_text().splitlines(),
                                             [str(self.station), str(self.project_dir), "192.0.2.20", "9443", "compose",
                                              "--project-directory", str(self.project_dir), "-f", str(self.config), "-p",
                                              PROJECT, "run", "--rm", "--no-deps", "-T", "certificate", "auto-renew"])

    def test_refresh_upgrade_reentry_is_idempotent_and_refuses_foreign_or_linked_file(self):
        data = self.station / "data"
        (self.station / ".giftledger-backup-state").mkdir(mode=0o700)
        control = data / "root_config/site/control"
        control.mkdir(parents=True)
        (data / "root_config/maintenance").mkdir()
        (control / "identity.json").write_text(json.dumps(IDENTITY))
        (data / site.ADDRESS).write_text(URL + "/\n")
        env = {"GIFTLEDGER_PROJECT": PROJECT, "GIFTLEDGER_SITE_IP": "192.0.2.20", "GIFTLEDGER_HTTPS_PORT": "9443",
               "GIFTLEDGER_PROJECT_DIR": str(self.project_dir), "GIFTLEDGER_SITE_DIR": str(self.station),
               "GIFTLEDGER_GUIDE_CONFIG_FILE": str(self.config)}
        guide = data / owner_guide.FILENAME
        with patch.object(site, "DATA", data), patch.dict(os.environ, env), patch.object(sys, "argv", ["site", "refresh-guide"]):
            self.assertEqual(site.main(), 0)
            stamp = guide.stat().st_mtime_ns
            self.assertEqual(site.main(), 0)
            self.assertEqual(stamp, guide.stat().st_mtime_ns)
            self.assertIn(str(self.config), guide.read_text())
            # A normal restart has no host label inputs. Retain verified paths.
            with patch.dict(os.environ, {"GIFTLEDGER_PROJECT_DIR":"", "GIFTLEDGER_SITE_DIR":".",
                                         "GIFTLEDGER_GUIDE_CONFIG_FILE":""}):
                site.publish_guide(PROJECT, URL, IDENTITY)
                self.assertEqual(stamp, guide.stat().st_mtime_ns)
                self.assertIn("mac_restore.py", guide.read_text())
            with patch.dict(os.environ, {"GIFTLEDGER_PROJECT_DIR":str(self.root / "moved"),
                                         "GIFTLEDGER_GUIDE_CONFIG_FILE":""}):
                site.publish_guide(PROJECT, URL, IDENTITY)
                self.assertNotIn("mac_restore.py", guide.read_text())
            with patch.dict(os.environ, {"GIFTLEDGER_PROJECT":"other"}), self.assertRaises(site.SiteError):
                site.main()
            guide.unlink()
            guide.symlink_to(self.config)
            with self.assertRaises(site.SiteError):
                site.main()
            self.assertEqual(self.config.read_bytes(), b"")

    def test_first_backup_section_explains_both_platforms_before_and_after_path_discovery(self):
        runner = self.root / "python3"
        runner.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$TEST_CALL"\n')
        runner.chmod(0o700)
        for project_dir, config in ((None, None), (str(self.project_dir), None),
                                    (str(self.project_dir), str(self.config))):
            with self.subTest(project_dir=project_dir, config=config):
                notes = owner_guide.render(PROJECT, URL, IDENTITY, project_dir=project_dir, config_file=config)
                section = notes.split("第一次备份前登记钥匙\n", 1)[1].split("就可以运行手动备份。", 1)[0]
                windows, mac = section.split("\n\nMac\n", 1)
                self.assertIn("windows-site-tools.zip", windows)
                self.assertIn("GiftLedger.cmd", windows)
                for phrase in ("mac-site-tools.zip", "mac_backup_key.py", "Mac 终端", "上传旧钥匙登记",
                               "生成新钥匙登记", "本站备份钥匙登记通过"):
                    self.assertIn(phrase, mac)
                self.assertIn("python3 mac_backup_key.py ", mac)
                self.assertNotIn("查看本站目录", mac)
                self.assertNotIn("重新下载", mac)
                if config is None:
                    self.assertLess(notes.index("\n第一次备份前登记钥匙\n"), notes.index("\n查看本站目录\n"))
                    subprocess.run(["/bin/sh", "-c", command(mac, "mac_backup_key.py")], check=True,
                                   env=self.env, cwd=self.root, input="owner\n2202\n", text=True,
                                   stdout=subprocess.DEVNULL)
                    self.assertEqual(self.calls.read_text().splitlines(), [
                        "mac_backup_key.py", "--host", "owner@192.0.2.20", "--port", "2202",
                        "--project", PROJECT, "--instance-id", IDENTITY["instance_id"], "--site-url", URL])

    def test_mac_guide_opens_the_key_menu_and_passes_the_exact_site_for_enrollment_and_restore(self):
        notes = owner_guide.render(PROJECT, URL, IDENTITY, project_dir=str(self.project_dir),
                                   site_dir=str(self.station), config_file=str(self.config))
        runner = self.root / "python3"
        runner.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$TEST_CALL"\n')
        runner.chmod(0o700)
        self.assertEqual(notes.count("python3 mac_backup_key.py "), 1)
        for script in ("mac_backup_key.py", "mac_restore.py"):
            with self.subTest(script=script):
                subprocess.run(["/bin/sh", "-c", command(notes, script)], check=True, env=self.env,
                               cwd=self.root, input="operator\n\n", text=True, stdout=subprocess.DEVNULL)
                called = self.calls.read_text().splitlines()
                self.assertEqual(called, [script, "--host", "operator@192.0.2.20", "--port", "22",
                                 "--project-dir", str(self.project_dir), "--project", PROJECT,
                                 "--compose-file", str(self.config), "--site-dir", str(self.station), "--site-url", URL])

    def test_legacy_tls_and_unresolved_paths_never_emit_a_fake_root_or_host_path(self):
        old = owner_guide.render(PROJECT, URL, {**IDENTITY, "tls":{}}, project_dir=".", site_dir=".")
        self.assertNotIn("certificate renew", old)
        self.assertNotIn("certificate auto-renew", old)
        self.assertNotIn("根证书 SHA-256", old)
        self.assertIn("本站公开证书.crt", old)
        self.assertNotIn("/volume1/docker", old)
        self.assertNotIn(".\\GiftLedger.cmd ", old)

    def test_windows_commands_preserve_each_exact_path_for_both_actions(self):
        import windows_site

        for directory, station, config in (
            ("/srv/ledger files", "/srv/站点", "/srv/ledger files/custom.yml"),
            (str(self.project_dir), str(self.station), str(self.config)),
            ('/srv/weird"%VAR%!&', "/srv/站点", "/srv/config.yml"),
        ):
            notes = owner_guide.render(PROJECT, URL, IDENTITY, project_dir=directory, site_dir=station, config_file=config)
            for action in ("enroll", "restore"):
                with self.subTest(action=action, directory=directory):
                    prefix = f".\\GiftLedger.cmd {action} --project {PROJECT} --site-url {URL} "
                    lines = [line.removeprefix(prefix) for line in notes.splitlines() if line.startswith(prefix)]
                    self.assertEqual(len(lines), 1)
                    if directory == "/srv/ledger files":
                        self.assertEqual(lines[0], f'--project-dir "{directory}" --site-dir "{station}" --compose-file "{config}"')
                    else:
                        option, encoded = lines[0].split()
                        self.assertEqual(option, "--site-layout")
                        self.assertRegex(encoded, r"^[A-Za-z0-9_=-]+$")
                        self.assertEqual(windows_site.layout_paths(encoded), [directory, station, config])


if __name__ == "__main__":
    unittest.main()
