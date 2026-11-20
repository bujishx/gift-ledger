"""Exercise the release images on a disposable Linux runner."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

from images import ROOT, ROLES, local, render

sys.path.insert(0, str(ROOT / "deploy/single_yaml"))
import mac_backup_key as keys


SERVICES = ("db", "api", "web")


def runtime_health(compose, env):
    states = dict.fromkeys(SERVICES, "missing")
    try:
        ids = subprocess.check_output(
            [*compose, "ps", "--all", "--quiet", *SERVICES],
            env=env,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).split()
        if not ids:
            return states
        inspect_format = (
            '{{index .Config.Labels "com.docker.compose.service"}} {{.State.Status}} '
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
        )
        rows = subprocess.check_output(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                inspect_format,
                *ids,
            ],
            env=env,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).splitlines()
    except (OSError, subprocess.SubprocessError):
        return dict.fromkeys(SERVICES, "inspection unavailable")

    for line in rows:
        fields = line.split()
        if len(fields) != 3 or fields[0] not in states:
            continue
        name, state, health = fields
        if states[name] != "missing":
            states[name] = "multiple containers"
            continue
        if state not in {
            "created",
            "running",
            "paused",
            "restarting",
            "removing",
            "exited",
            "dead",
        }:
            state = "unknown"
        if health not in {"starting", "healthy", "unhealthy", "none"}:
            health = "unknown"
        states[name] = f"{state}/{health}"
    return states


def https_ready(url, ca):
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(ca.read_bytes()))
        for path in ("/", "/api/v1/health/ready"):
            with urllib.request.urlopen(
                url + path, context=context, timeout=3
            ) as response:
                if response.status != 200:
                    return False
    except (OSError, ValueError):
        return False
    return True


def wait_ready(compose, env, url, ca):
    end = time.monotonic() + 240
    while True:
        states = runtime_health(compose, env)
        https_ok = https_ready(url, ca)
        if https_ok and all(states[name] == "running/healthy" for name in SERVICES):
            return
        if time.monotonic() >= end:
            diagnostics = ", ".join(f"{name}={states[name]}" for name in SERVICES)
            raise RuntimeError(
                f"Site readiness timed out (HTTPS={'ok' if https_ok else 'unready'}, {diagnostics})"
            )
        time.sleep(2)


def verify_backup_age(compose, env):
    # The selected release source may predate the native desktop tools.
    if not (ROOT / "deploy/single_yaml/native_age.py").is_file():
        return
    from native_age import AGE_VERSION

    config = subprocess.check_output(
        [*compose, "--profile", "maintenance", "config", "--format", "json"],
        env=env, timeout=30,
    )
    image = json.loads(config)["services"]["backup"]["image"]
    for tool in ("age", "age-keygen"):
        version = subprocess.check_output(
            ["docker", "run", "--rm", "--pull=never", "--platform", "linux/amd64",
             "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--tmpfs", "/var/lib/mysql",
             "--entrypoint", "/usr/local/bin/" + tool, image, "--version"],
            text=True, env=env, timeout=60,
        ).strip()
        if version != "v" + AGE_VERSION:
            raise RuntimeError(f"Backup image {image}: {tool} reported {version!r}, expected v{AGE_VERSION}")
        print(f"Backup image {tool} {version}", flush=True)


def enroll(compose, env, work):
    folder = work / "exchange"
    folder.mkdir(mode=0o700)
    p = subprocess.Popen(
        [
            *compose,
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "-v",
            f"{folder}:/work/exchange",
            "backup-key",
        ],
        env=env,
    )
    try:
        target_file = folder / "target.json"
        for _ in range(120):
            if target_file.exists():
                break
            if p.poll() is not None:
                raise RuntimeError("Key enrollment exited early")
            time.sleep(1)
        target, registered = keys.target_info(
            target_file.read_bytes(), env["COMPOSE_PROJECT_NAME"]
        )
        if registered:
            raise RuntimeError("Candidate site already had an identity")
        # The CI key exists only in memory and the fresh project's disposable volume.
        age = keys.Age()
        secret = next(
            line + b"\n"
            for line in age.run("age-keygen").splitlines()
            if line.startswith(b"AGE-SECRET-KEY-")
        )
        recipient = age.run("age-keygen", ["-y"], secret).decode().strip()
        packet = keys.packet_for(target, secret, recipient, age)
        pending = folder / "packet.upload"
        pending.write_bytes(keys.encoded(packet))
        pending.rename(folder / "packet.json")
        if p.wait(timeout=180) != 0:
            raise RuntimeError("Candidate key registration failed")
        keys.verify_receipt(
            (folder / "receipt.json").read_bytes(),
            target,
            secret,
            recipient,
            packet["request"],
        )
    finally:
        if p.poll() is None:
            (folder / "cancel").touch()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.terminate()
                p.wait(timeout=10)


def smoke(yaml, pull):
    with tempfile.TemporaryDirectory(prefix="giftledger-ci-") as temp:
        work = Path(temp)
        station = work / "site"
        station.mkdir()
        config = station / "docker-compose.yml"
        config.write_text(yaml)
        name = "giftledger-ci-" + secrets.token_hex(5)
        env = dict(
            os.environ,
            COMPOSE_PROJECT_NAME=name,
            GIFTLEDGER_SITE_IP="127.0.0.1",
            GIFTLEDGER_HTTPS_PORT="16443",
        )
        cmd = [
            "docker",
            "compose",
            "--project-directory",
            str(station),
            "-f",
            str(config),
            "-p",
            name,
        ]

        def compose(*args):
            subprocess.run([*cmd, *args], env=env, check=True)

        try:
            compose("--profile", "maintenance", "config", "--quiet")
            if pull:
                compose("--profile", "maintenance", "pull")
            verify_backup_age(cmd, env)
            compose("up", "-d")
            url = "https://127.0.0.1:16443"
            # The CI user can read the published DER, but the prepare-owned
            # private identity directory must remain inaccessible.
            data = station / "data"
            if (ROOT / "deploy/single_yaml/station_guides.py").is_file():
                roots = [p for p in data.glob(name + "-????????-本站根证书.cer")
                         if re.fullmatch(re.escape(name) + r"-[a-f0-9]{8}-本站根证书\.cer", p.name)
                         and p.is_file() and not p.is_symlink()]
                if len(roots) != 1:
                    raise RuntimeError("Expected one public root certificate for this project")
                ca, = roots
            else:
                ca = data / "本站根证书.cer"
            wait_ready(cmd, env, url, ca)
            enroll(cmd, env, work)
            wait_ready(cmd, env, url, ca)
            compose("run", "--rm", "-T", "backup")
            wait_ready(cmd, env, url, ca)
            backups = list((station / "backups/artifacts").glob("*/BUNDLE.json"))
            if len(backups) != 1 or len(list(backups[0].parent.iterdir())) != 5:
                raise RuntimeError("No complete five-file backup")
        finally:
            compose("--profile", "maintenance", "down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--published", type=Path)
    args = p.parse_args()
    smoke(
        args.published.read_text()
        if args.published
        else render({r: local(r) for r in ROLES}),
        bool(args.published),
    )
