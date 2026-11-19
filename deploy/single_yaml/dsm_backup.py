"""Run a single-YAML backup as a Synology Task Scheduler root task."""
import argparse
import json
import os
from pathlib import Path
import pkgutil
import re
import sys
from uuid import uuid4

import backup_ops as ops
import backup_site as site
from dsm_events import adapter, io, observer, provider, target


class HostSite(site.SiteBindings):
    """Bind the host runner to the same prepare service, station and Docker engine."""
    def __init__(self, docker, project, expected_station=None):
        self.docker, self.project, self.service = docker, project, "backup"
        ops.require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "invalid_project")
        ids = docker.call("ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.project=" + project,
                          "--filter", "label=com.docker.compose.service=prepare",
                          "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}").splitlines()
        ops.require(len(ids) == 1 and ops.SHA.fullmatch(ids[0]), "prepare_missing_or_ambiguous")
        found = self.coordinator = docker.inspect(ids[0])
        ops.require(found["id"] == ids[0] and found["name"] == "/" + project + "-prepare-1"
                    and found["project"] == project and found["service"] == "prepare"
                    and found["status"] == "exited" and found["exit_code"] == 0 and not found["oom"]
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", found["image"]), "prepare_identity_mismatch")
        mounts = [item for item in found["mounts"] if item["Destination"] == "/station" and item["Type"] == "bind"]
        ops.require(len(mounts) == 1, "prepare_station_missing")
        self.station = Path(mounts[0]["Source"])
        ops.require(self.station.is_absolute() and self.station != Path("/") and
                    (expected_station is None or Path(expected_station) == self.station), "station_directory_mismatch")
        ops.no_links(self.station)
        site.check_mounts(found["mounts"], {
            "/station": site.mount("bind", self.station, True),
            "/work/mysql": site.mount("volume", project + "_mysql_socket", True),
        }, {"/tmp", "/var/lib/mysql"})
        contract = '{"cmd":{{json .Config.Cmd}},"readonly":{{json .HostConfig.ReadonlyRootfs}},"env":{{json .Config.Env}}}'
        config = json.loads(docker.call("inspect", "--type", "container", "--format", contract, ids[0]))
        ops.require(config["cmd"] == ["prepare"] and config["readonly"] is True, "prepare_command_mismatch")
        settings = {}
        names = {"GIFTLEDGER_PROJECT", "GIFTLEDGER_SITE_IP", "GIFTLEDGER_HTTPS_PORT"}
        for item in config["env"]:
            key, _, value = item.partition("=")
            if key in names:
                ops.require(key not in settings, "duplicate_site_setting")
                settings[key] = value
        ops.require(settings.keys() == names and settings["GIFTLEDGER_PROJECT"] == project, "prepare_site_mismatch")
        self.settings = settings

    def directories(self):
        return self.station / site.STATE_NAME, self.station / site.STORE_NAME


class EventBinding(target.Binding):
    """Use the current single-YAML identities with the verified DSM vendor package."""
    def __init__(self, profile, package):
        self.PROJECT = profile["project"]
        self.ENGINE = profile["engine_id"]
        self.INSTANCE = profile["instance_id"]
        self.IDS = {role: row["id"] for role, row in profile["containers"].items()}
        self.package = package

    def validate_package(self):
        current = target.Package.discover(backup_events=True)
        ops.require(current.identity() == self.package.identity() and current.build == self.package.build,
                    "container_manager_changed_during_backup")


class EventEnvironment(io.Environment):
    previous_worker_id = None

    def check_other_operations(self, driver, binding):
        # A separate test restore site may have the source site's name as its prefix.
        for key in ("com.docker.compose.project", "org.gift-ledger.gui-v2-project"):
            ids = driver.call("ps", "--no-trunc", "--filter", "label=" + key + "=" + binding.PROJECT,
                              "--format", "{{.ID}}", timeout=15).splitlines()
            allowed = set(binding.IDS.values())
            if self.previous_worker_id:
                allowed.add(self.previous_worker_id)
            ops.require(set(ids).issubset(allowed), "another_target_operation_running")


def reconcile_events(reports, package, docker):
    """Never replay an unacknowledged action or forget a bounded observer after interruption."""
    active = reports / "active.json"
    if not os.path.lexists(active):
        return
    label = ops.read_json(active)["run"]
    ops.require(re.fullmatch(r"gl-events-[0-9a-f]{32}", label), "dsm_event_record_invalid")
    previous = reports / label
    io.checked_directory(previous)
    try:
        path = previous / "events.json"
        events = ops.read_json(path) if os.path.lexists(path) else {}
        path = previous / "actions.json"
        actions = ops.read_json(path)["actions"] if os.path.lexists(path) else []
        for action in actions:
            if not action.get("acknowledged"):
                subject = events.get("subjects", {}).get(action["role"], {})
                ops.require(action["action"] == "stop" and subject.get("event_barrier_verified") is True,
                            "previous_dsm_action_requires_observation")
        if events and not events.get("retired"):
            for role, subject in events["subjects"].items():
                ops.require(role in ("web", "api"), "dsm_event_record_invalid")
                hook = package.variable / "hook/event" / ("zz-gift-ledger-" + label + "-" + role + ".py")
                ops.require(not os.path.lexists(hook), "previous_dsm_event_resources_remain")
                for kind in ("initial", "after"):
                    name = subject["config"]["marker_names"][kind]
                    ops.require(re.fullmatch(label + "-" + role + "-" + kind + "-[0-9a-f]{12}", name),
                                "dsm_event_record_invalid")
                    ids = docker.call("ps", "-a", "--no-trunc", "--filter", "name=^/" + name + "$",
                                      "--format", "{{.ID}}")
                    ops.require(not ids and not os.path.lexists(package.profiles / (name + ".config")),
                                "previous_dsm_event_resources_remain")
    except Exception:
        print("上次群晖操作需要核对，请按此目录中的事件和操作回执检查服务、观察器及标记容器：" + str(previous),
              file=sys.stderr)
        raise
    active.unlink()
    ops.fsync_directory(reports)


def run(project, expected_station=None):
    package = target.Package.discover(backup_events=True)
    docker = ops.Docker(package.target / "usr/bin/docker")
    host = HostSite(docker, project, expected_station)
    os.environ.update(host.settings)
    site.single_yaml_site.project_host(project)
    profile = host.profile()
    profile["engine_id"] = json.loads(docker.call("info", "--format", "{{json .ID}}"))
    ops.require(isinstance(profile["engine_id"], str) and profile["engine_id"], "docker_engine_identity_missing")
    binding = EventBinding(profile, package)
    state, _ = host.directories()
    reports = ops.mkdir(state / "dsm-events")
    io.checked_directory(reports)
    with ops.host_lock(reports):
        reconcile_events(reports, package, docker)
        run_with_events(host, profile, docker, package, binding, reports)


def run_with_events(host, profile, docker, package, binding, reports):
    label = "gl-events-" + uuid4().hex
    report = reports / label
    report.mkdir(mode=0o700)
    io.checked_directory(report)
    environment = EventEnvironment(package)
    events = provider.EventProvider(
        docker, profile, report, environment, observer,
        pkgutil.get_data("dsm_events", "observer.py"), binding, io,
        lambda value: ops.save_json(report / "events.json", value),
        run_label=label, start_allowed=lambda now: True,
    )
    planned = adapter.BackupStopAdapter(docker, profile, events, lambda value: ops.save_json(report / "actions.json", value))
    wrapped = adapter.wrap_worker_factory(ops, planned, site.Worker)
    ops.save_json(reports / "active.json", {"run": label}, new=True)

    def worker_factory(profile, driver, journal, run_dir, recover=False):
        if recover:
            environment.previous_worker_id = site.previous_worker(profile, docker, journal)
        return wrapped(profile, driver, journal, run_dir, recover=recover)

    try:
        bundle = site.run_backup(host, profile, planned, worker_factory)
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            ops.require(events.retire()["retired"], "dsm_event_cleanup_incomplete")
        except Exception:
            print("群晖事件观察器清理未完成。请查看本站维护回执 " + str(report), file=sys.stderr)
            if not failed:
                raise
    reconcile_events(reports, package, docker)
    site.report_backup(bundle)


def main(argv=None):
    parser = argparse.ArgumentParser(description="群晖任务计划程序使用的本站备份工具")
    parser.add_argument("--project", required=True)
    parser.add_argument("--station", help="核对 prepare 挂载的站点目录")
    parser.add_argument("--check-certificate", action="store_true", help="核对本站证书及是否需要续签")
    parser.add_argument("--url", help="维护说明中的本站网址")
    parser.add_argument("--root-sha256", help="维护说明中的本站根证书指纹")
    args = parser.parse_args(argv)
    if args.check_certificate:
        import certificate_site
        import dsm_certificate
        if not (args.station and args.url and re.fullmatch(r"[a-f0-9]{64}", args.root_sha256 or "")):
            parser.error("证书检查需要本站目录、网址及根证书 SHA-256 指纹")
        try:
            docker = ops.Docker("/var/packages/ContainerManager/target/usr/bin/docker")
            host = HostSite(docker, args.project, args.station)
            return dsm_certificate.run(host, args.url, args.root_sha256)
        except Exception as exc:
            print(certificate_site.failure_message(exc), file=sys.stderr)
            return 1
    if args.url is not None or args.root_sha256 is not None:
        parser.error("网址及根证书指纹仅用于证书检查")
    try:
        run(args.project, args.station)
    except Exception as exc:
        code = getattr(exc, "code", str(exc))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", str(code)):
            code = "dsm_backup_environment_invalid"
        if code == "backup_key_registration_required":
            print("请先完成本站备份钥匙登记，再运行备份。", file=sys.stderr)
        else:
            print("群晖备份任务未完成。原因：" + code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
