"""Generate and run one Ubuntu site's daily TLS timer using its private host binding."""
import argparse
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys


def load_dispatcher():
    # python -I deliberately excludes the script directory from module search.
    # Load just the trusted sibling; never import code from the station share.
    script = Path(__file__).absolute()
    sibling = script.with_name("maintenance_host.py")
    for path in (script, sibling, *script.parents):
        info = path.lstat()
        valid_kind = stat.S_ISDIR(info.st_mode) if path in script.parents else stat.S_ISREG(info.st_mode)
        if info.st_uid != 0 or info.st_mode & 0o022 or not valid_kind:
            raise ValueError("host_worker_script_not_trusted")
    spec = importlib.util.spec_from_file_location("giftledger_host", sibling)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timer_unit(project):
    return f"""[Unit]
Description=Daily GiftLedger HTTPS certificate check ({project})

[Timer]
OnCalendar=*-*-* 03:15:00
AccuracySec=1s
Persistent=true
Unit=giftledger-certificate-{project}.service

[Install]
WantedBy=timers.target
"""


def service_unit(dispatcher, project, state, image, url, root):
    args = ("/usr/bin/python3", "-I", Path(__file__).absolute(), "run", "--state", state,
            "--key-image", image, "--url", url, "--root-sha256", root)
    return f"""[Unit]
Description=Check and renew GiftLedger HTTPS certificate ({project})
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
UMask=0077
NoNewPrivileges=yes
ExecStart={" ".join(dispatcher.unit_arg(arg) for arg in args)}
"""


def renew(archive, site, image, checker):
    # Use the same coordinator as the release's certificate service, without
    # executing an editable Compose file under a root timer. It takes the site's
    # ordinary backup/restore/certificate lock and rechecks the renewal window.
    args = ["run", "--rm", "--pull=never", "--network=none", "--read-only", "--user=0:0",
            "--restart=no", "--no-healthcheck", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--memory=256m", "--log-driver=none", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--tmpfs=/var/lib/mysql:rw,noexec,nosuid,nodev,size=16m",
            "--label=com.docker.compose.project=" + site.project, "--label=com.docker.compose.service=certificate",
            "--label=com.docker.compose.oneoff=True"]
    for target, source in (("/var/run/docker.sock", "/var/run/docker.sock"),
                           (str(archive.site.STATE), site.directories()[0])):
        args.extend(("--mount", archive.site.mount_argument(target, archive.site.mount("bind", source, True))))
    for key, value in site.settings.items():
        args.extend(("--env", f"{key}={value}"))
    args.extend(("--entrypoint", checker.certificate_site.PYTHON, image, checker.certificate_site.SCRIPT, "auto-renew"))
    return subprocess.run(site.docker.argv(*args), stdin=subprocess.DEVNULL, env=site.docker.environment()).returncode


def execute(dispatcher, args):
    state = dispatcher.absolute(args.state)
    cfg = dispatcher.read_config(state)
    dispatcher.require(cfg.get("platform") == "ubuntu" and dispatcher.PROJECT.fullmatch(cfg["project"]),
                       "ubuntu_binding_required")
    dispatcher.require(dispatcher.IMAGE.fullmatch(args.key_image), "approved_key_image_required")
    if args.action == "timer-unit":
        print(timer_unit(cfg["project"]), end="")
        return 0

    archive = dispatcher.archive_module(cfg)
    import dsm_certificate as checker
    dispatcher.require(str(checker.__file__).startswith(cfg["archive"] + "/"), "unexpected_certificate_module")
    docker = archive.ops.Docker(dispatcher.ubuntu_docker())
    with dispatcher.directories(Path(cfg["station"])) as fd:
        dispatcher.require(dispatcher.identity(os.fstat(fd)) == cfg["station_inode"], "station_identity_changed")
        site = archive.HostSite(docker, cfg["project"], cfg["station"])
        dispatcher.require(site.coordinator["image"] == args.key_image, "approved_backup_image_mismatch")
        os.environ.update(site.settings)
        ip = archive.site.single_yaml_site.project_host(site.project)
        url = archive.site.single_yaml_site.site_url(ip) + "/"
        identity = archive.ops.read_json(site.station / "data/root_config/site/control/identity.json")
        dispatcher.require(identity.get("instance_id") == cfg["instance"] and identity.get("project") == site.project,
                           "maintenance_instance_mismatch")
        root = identity["tls"].get("root_sha256")
        dispatcher.require(isinstance(root, str) and dispatcher.SHA.fullmatch(root), "certificate_public_root_changed")
        if args.action == "service-unit":
            print(service_unit(dispatcher, site.project, state, args.key_image, url, root), end="")
            return 0
        dispatcher.require(args.url is None or args.url == url, "certificate_site_port_mismatch")
        dispatcher.require(args.root_sha256 is None or args.root_sha256 == root, "certificate_public_root_changed")
        result = checker.run(site, url, root)
        if result == checker.NEEDS_COORDINATOR:
            return renew(archive, site, args.key_image, checker)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ubuntu 本站 HTTPS 自动续签")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("service-unit", "timer-unit", "run"):
        sub = commands.add_parser(action)
        sub.add_argument("--state", required=True)
        sub.add_argument("--key-image", required=True)
        if action == "run":
            sub.add_argument("--url")
            sub.add_argument("--root-sha256")
    args = parser.parse_args(argv)
    dispatcher = None
    try:
        if os.geteuid() or not sys.flags.isolated:
            raise ValueError("root_and_isolated_python_required")
        dispatcher = load_dispatcher()
        return execute(dispatcher, args)
    except Exception as exc:
        code = dispatcher.machine_code(exc, "certificate_environment_invalid") if dispatcher else "host_worker_not_trusted"
        print("Ubuntu 证书任务未完成。原因：" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
