"""Release and offline guides use the same bytes. Destructive examples run only with a mock Docker."""
from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "deploy/single_yaml"
sys.path.insert(0, str(SITE))
import station_guides


PROJECT = "family-check"
INSTANCE = "12341234-4444-4444-9444-123412341234"


def blocks(text):
    return re.findall(r"^\(\n.*?^\)$", text, flags=re.MULTILINE | re.DOTALL)


class Guides(unittest.TestCase):
    def test_docs_in_zipimport_and_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "guides.pyz"
            with zipfile.ZipFile(package, "x") as archive:
                for name in ("station_guides.py", "owner_guide.py", station_guides.NETWORK_GUIDE):
                    archive.write(SITE / name, name)
                archive.writestr("__main__.py", (
                    "import sys; from station_guides import documents\n"
                    f"docs=documents('{PROJECT}', 'https://192.0.2.10:8443', '{INSTANCE}')\n"
                    "sys.stdout.buffer.write(docs['安装前检查端口与网络.txt'])\n"))
            raw = subprocess.check_output([sys.executable, "-I", str(package)])
            self.assertEqual(raw, (SITE / station_guides.NETWORK_GUIDE).read_bytes())
            for command in blocks(raw.decode()):
                subprocess.run(["/bin/sh", "-n"], input=command, text=True, check=True)

    def test_port_listing_checks_stopped_containers_and_host_tcp(self):
        command = blocks((SITE / station_guides.NETWORK_GUIDE).read_text())[0]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            driver = folder / "docker"
            driver.write_text("""#!/bin/sh
[ "$1" = --host ] && [ "$2" = unix:///var/run/docker.sock ] || exit 2
shift 2
case "$*" in
    'ps -aq') printf 'a\\nb\\n' ;;
    inspect*' a') echo 'family running 192.0.2.1 8443 -> 443/tcp' ;;
    inspect*' b') echo 'family2 exited 0.0.0.0 8444 -> 443/tcp' ;;
    *) exit 2 ;;
esac
""")
            driver.chmod(0o755)
            ss = folder / "ss"
            ss.write_text("#!/bin/sh\n[ \"$1\" = -lnt ] && echo 'LISTEN 0 128 0.0.0.0:22'\n")
            ss.chmod(0o755)
            command = command.replace("d=" + station_guides.owner_guide.DSM_DOCKER, "d=" + shlex.quote(str(driver)))
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ["PATH"])
            found = subprocess.check_output(["/bin/sh", "-c", command], env=env, text=True)
            self.assertIn("0.0.0.0:22", found)
            self.assertIn("running 192.0.2.1 8443", found)
            self.assertIn("exited 0.0.0.0 8444", found)

    def test_retire_command_binds_the_instance_and_preserves_other_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            station = base / "path with space and '"
            control = station / "data/root_config/site/control"
            control.mkdir(parents=True)
            config = station / "docker-compose.yml"
            config.touch()
            log = base / "docker.log"
            driver = base / "docker"
            driver.write_text("""#!/bin/sh
case "$1" in
  ps) echo f00ba4 ;;
  inspect)
    case "$3" in
      *working_dir*) echo "$STATION" ;;
      *config_files*) echo "$CONFIG" ;;
      *) echo "$STATION" ;;
    esac ;;
  compose) printf '%s\\n' "$@" > "$LOG" ;;
  *) exit 2 ;;
esac
""")
            driver.chmod(0o755)
            notes = station_guides.documents(PROJECT, "https://192.0.2.10:8443/", INSTANCE,
                                             project_dir=station, site_dir=station, config_file=config)
            self.assertEqual(set(notes), {"family-check-12341234-如何彻底删除本站.txt", station_guides.NETWORK_GUIDE})
            contents = notes["family-check-12341234-如何彻底删除本站.txt"].decode()
            self.assertIn(str(station), contents)
            commands = blocks(contents)
            self.assertEqual(len(commands), 4)
            for command in commands:
                subprocess.run(["/bin/sh", "-n"], input=command, text=True, check=True)
            down = next(x for x in commands if "down --volumes" in x)
            down = down.replace("d=" + station_guides.owner_guide.DSM_DOCKER, "d=" + shlex.quote(str(driver)))
            down = down.replace('"$(id -u)"', '"0"').replace("/usr/bin/python3 -I", shlex.quote(sys.executable) + " -I")
            env = dict(os.environ, STATION=str(station), CONFIG=str(config), LOG=str(log))
            def run(identity, answer):
                (control / "identity.json").write_text(json.dumps(dict(project=PROJECT, instance_id=identity)))
                return subprocess.run(["/bin/sh", "-c", down], input=answer+"\n", text=True,
                                      env=env, capture_output=True)
            self.assertNotEqual(run("recreated-site", PROJECT).returncode, 0)
            self.assertFalse(log.exists())
            self.assertNotEqual(run(INSTANCE, "other-project").returncode, 0)
            self.assertFalse(log.exists())
            self.assertEqual(run(INSTANCE, PROJECT).returncode, 0)
            self.assertEqual(log.read_text().splitlines(),
                             ["compose", "--project-directory", str(station), "-f", str(config),
                              "-p", PROJECT, "--profile", "maintenance", "down", "--volumes"])


if __name__ == "__main__":
    unittest.main()
