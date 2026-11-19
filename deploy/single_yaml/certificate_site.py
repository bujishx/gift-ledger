"""Inspect or renew the selected Compose site's HTTPS certificate."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import backup_ops as ops
import backup_site as site
from certificate_policy import LEAF_DAYS, renewal_start
import certificate_auto

SCRIPT = "/opt/gift-ledger-install/certificate_site.py"
PYTHON = "/opt/venv/bin/python3.12"
ROOT_FILE = "/work/public-root.cer"
TERMINAL = {"completed", "rolled-back"}
ROLE_MOUNTS = {"root_config": "/work/site", "app_secrets": "/work/app-secrets",
               "db_secrets": "/work/db-secrets", "tls_config": "/work/tls"}
require = ops.require


class RestoredWebError(ops.BackupOpsError):
    def __init__(self, reason):
        super().__init__("certificate_renewal_failed_original_web_restored")
        self.reason = reason


def run_worker(action, operation, instance, old_leaf, root_sha):
    import certificate_renewal as engine
    project = os.environ["GIFTLEDGER_PROJECT"]
    _, module, _ = engine.tools()
    root = engine.PATHS["root"]
    identity = engine.read_json(module, root / "site/control/identity.json")
    require(identity["project"] == project and identity["tls"].get("profile") == "site-ca-v1",
            "certificate_site_ca_required")
    require(instance == identity["instance_id"] or (instance == "-" and action == "inspect"),
            "certificate_instance_mismatch")
    require(root_sha == identity["tls"]["root_sha256"] or (root_sha == "-" and action == "inspect"),
            "certificate_root_changed")
    require(ops.ID.fullmatch(operation), "certificate_operation_id_invalid")
    tx = root / "maintenance" / ("certificate-" + operation)

    def call(mode):
        return engine.run(project, mode, identity["instance_id"], operation, web_stopped=mode != "inspect")

    if action == "inspect":
        return call("inspect")
    require(action in ("apply", "recover", "rollback"), "certificate_worker_action_invalid")
    if tx.exists():
        # The private transaction can survive the host or the caller. A partial write
        # rolls back, a completed one is verified, and neither signs another leaf.
        call("recover")
        if action == "rollback" and (tx / "completed.json").exists() and not (tx / "rollback-completed.json").exists():
            call("rollback")
    elif action == "apply":
        require(identity["tls"]["certificate_sha256"] == old_leaf, "certificate_original_changed")
        call("renew")
    result = call("inspect")
    require(result["instance_id"] == instance and result["root_sha256"] == root_sha,
            "certificate_identity_changed")
    committed = (tx / "completed.json").exists() and not (tx / "rollback-completed.json").exists()
    if committed:
        receipt = engine.read_json(module, tx / "completed.json")
        require(receipt["certificate_sha256"] == result["certificate_sha256"]
                and result["certificate_sha256"] != old_leaf, "certificate_result_unverified")
    else:
        require(result["certificate_sha256"] == old_leaf, "certificate_original_changed")
    return {**result, "outcome": "renewed" if committed else "original"}


def probe(ip, port, root_sha, leaf_sha):
    root = Path(ROOT_FILE).read_bytes()
    require(hashlib.sha256(root).hexdigest() == root_sha, "certificate_public_root_changed")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=root)
    # Use the same IP and published port as the owner's URL. Trust only the
    # site's public CA and verify both the hostname and the exact served leaf.
    deadline = time.monotonic() + 20
    while True:
        try:
            with socket.create_connection((ip, int(port)), timeout=2) as conn:
                with context.wrap_socket(conn, server_hostname=ip) as tls:
                    require(hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() == leaf_sha,
                            "certificate_live_leaf_mismatch")
                    return {"certificate_sha256": leaf_sha, "root_sha256": root_sha}
        except ssl.SSLCertVerificationError:
            raise ops.BackupOpsError("certificate_live_chain_invalid") from None
        except OSError:
            if time.monotonic() >= deadline:
                raise ops.BackupOpsError("certificate_web_unreachable") from None
            time.sleep(0.5)


class Job:
    def __init__(self, bindings):
        self.bindings = bindings
        self.docker = bindings.docker
        self.project = bindings.project
        self.image = bindings.coordinator["image"]
        self.station = bindings.station

    def web(self, expected=None):
        web = site.inspect_service(self.docker, self.project, "web")
        self.bindings.service_mounts(self.project, "web", web["mounts"], None)
        if expected:
            require(web["id"] == expected, "certificate_web_identity_changed")
        web["running"] = site.check_web_endpoint(
            self.docker, web["id"], os.environ["GIFTLEDGER_SITE_IP"], os.environ["GIFTLEDGER_HTTPS_PORT"],
            "certificate_site_port_mismatch")
        return web

    def helper(self, command, mounts, network, name=None, write=False):
        user = "65534:65534" if command[0] == "probe" else "0:0"
        args = ["run", "--rm", "--pull", "never", "--user", user, "--network", network,
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--tmpfs", "/var/lib/mysql:rw,noexec,nosuid,nodev,size=16m",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m", "--log-driver", "none"]
        if name:
            args.extend(["--name", name])
        if write:
            args.extend(["--cap-add", "CHOWN", "--cap-add", "FOWNER"])
        for variable in ("GIFTLEDGER_PROJECT", "GIFTLEDGER_SITE_IP", "GIFTLEDGER_HTTPS_PORT"):
            args.extend(["--env", variable + "=" + os.environ[variable]])
        for target, (source, writable) in mounts.items():
            args.extend(["--mount", site.mount_argument(target, site.mount("bind", source, writable))])
        result = json.loads(self.docker.call(*args, "--entrypoint", PYTHON, self.image, SCRIPT,
                                             *command, timeout=120))
        if result.get("ok") is not True:
            code = result.get("error")
            raise ops.BackupOpsError(code if isinstance(code, str) and re.fullmatch("[a-z0-9_-]{1,100}", code)
                                     else "certificate_helper_failed")
        return result["result"]

    def worker(self, action, record):
        write = action != "inspect"
        mounts = {target: (self.station / "data" / role, write and role in ("root_config", "tls_config"))
                  for role, target in ROLE_MOUNTS.items()}
        return self.helper(["worker", action, record["operation"], record.get("instance_id", "-"),
                            record.get("old_leaf", "-"), record.get("root", "-")], mounts, "none",
                           self.project + "-certificate-" + record["operation"], write)

    def verify_live(self, web, root, leaf):
        require(self.web(web)["running"], "certificate_web_state_unconfirmed")
        proof = self.helper(["probe", os.environ["GIFTLEDGER_SITE_IP"], os.environ["GIFTLEDGER_HTTPS_PORT"], root, leaf],
                            {ROOT_FILE: (self.station / "data/本站根证书.cer", False)},
                            "host")
        require(proof == {"certificate_sha256": leaf, "root_sha256": root}, "certificate_live_proof_invalid")

    def running(self, expected, want):
        web = self.web(expected)
        if web["running"] != want:
            args = ("start", expected) if want else ("stop", "--time", "30", expected)
            try:
                self.docker.call(*args)
            except ops.BackupOpsError:
                raise ops.BackupOpsError("certificate_web_start_failed" if want else "certificate_web_stop_failed") from None
        require(self.web(expected)["running"] == want, "certificate_web_state_unconfirmed")


def check_other_operations():
    restore = site.STATE / "restore.json"
    if restore.exists():
        state = ops.read_json(restore)
        require(state["stage"] in ("completed", "rejected") and
                not (state.get("identity_role") == "restore_identity" and state["stage"] == "completed"
                     and not state.get("transfer_key_removed")), "unfinished_restore_blocks_certificate")
    if (site.STATE / "runs").exists():
        require(all(r["stage"] in ("finished", "preflight-failed") and not r.get("private_cleanup_pending")
                    for _, r in ops.run_records(site.STATE)), "unfinished_backup_blocks_certificate")


def root_covers_renewal(result, now):
    start = renewal_start(now.replace(microsecond=0), datetime.fromisoformat(result["not_before"]))
    return start + timedelta(days=LEAF_DAYS) <= datetime.fromisoformat(result["root_not_after"])


def coordinate(job, action, journal):
    record = ops.read_json(journal) if journal.exists() else None
    if record and record["stage"] not in TERMINAL:
        require(record["project"] == job.project and record["station"] == str(job.station)
                and ops.ID.fullmatch(record["operation"]) and record["image"] == job.image,
                "certificate_unfinished_target_changed")
        if action == "inspect":
            return {"status": "unfinished", "project": job.project, "operation_id": record["operation"],
                    "stage": record["stage"], "说明": "再次运行同一站点的 renew 命令继续本次操作。"}
        job.web(record["web"])
    else:
        operation = uuid4().hex
        web = job.web()
        old = job.worker("inspect", {"operation": operation})
        if action == "inspect":
            return {**old, "web_running": web["running"]}
        current = action == "auto-renew" and not certificate_auto.renewal_due(old["not_after"])
        require(current or web["running"], "certificate_web_must_be_online_to_verify")
        require(root_covers_renewal(old, datetime.now(timezone.utc)), "site_root_certificate_replacement_required")
        if current:
            return {"status": "current"}
        record = {"stage": "prepared", "operation": operation, "project": job.project,
                  "station": str(job.station), "image": job.image, "web": web["id"],
                  "instance_id": old["instance_id"], "root": old["root_sha256"],
                  "old_leaf": old["certificate_sha256"], "old_not_after": old["not_after"]}
        ops.save_json(journal, record)

    def save(stage, **fields):
        record.update(fields, stage=stage)
        ops.save_json(journal, record)

    web = record["web"]
    require(record["stage"] in ("prepared", "applying", "recovering", "activating", "rolling-back", "restoring"),
            "certificate_stage_invalid")
    if record["stage"] in ("prepared", "applying", "recovering"):
        job.running(web, False)
        if record["stage"] != "recovering":
            save("applying")
            try:
                result = job.worker("apply", record)
            except Exception as exc:
                save("recovering", failure=error_code(exc))
        if record["stage"] == "recovering":
            result = job.worker("recover", record)
        if result["outcome"] == "renewed":
            require(result["root_sha256"] == record["root"] and result["instance_id"] == record["instance_id"]
                    and result["certificate_sha256"] != record["old_leaf"]
                    and result["not_after"] > record["old_not_after"], "certificate_renewal_proof_invalid")
            save("activating", result=result)
        else:
            save("restoring", result=result)
    if record["stage"] == "activating":
        try:
            job.running(web, True)
            job.verify_live(web, record["root"], record["result"]["certificate_sha256"])
            require(job.web(web)["running"], "certificate_web_state_unconfirmed")
        except Exception as exc:
            save("rolling-back", failure=error_code(exc))
        else:
            save("completed")
            return {**record["result"], "status": "completed", "live_https_verified": True,
                    "previous_not_after": record["old_not_after"]}
    if record["stage"] == "rolling-back":
        job.running(web, False)
        result = job.worker("rollback", record)
        require(result["outcome"] == "original", "certificate_rollback_unverified")
        save("restoring", result=result)
    require(record["stage"] == "restoring", "certificate_stage_invalid")
    job.running(web, True)
    save("rolled-back")
    raise RestoredWebError(record.get("failure"))


def run(action):
    require(action in ("inspect", "renew", "auto-renew"), "certificate_action_invalid")
    project = os.environ["GIFTLEDGER_PROJECT"]
    site.single_yaml_site.project_host(project)
    d = ops.Docker("/usr/bin/docker", "unix:///var/run/docker.sock")
    job = Job(site.SiteBindings(d, project, service="certificate"))
    if action == "inspect":
        result = coordinate(job, action, site.STATE / "certificate.json")
    else:
        try:
            with ops.host_lock(site.STATE):
                check_other_operations()
                result = coordinate(job, action, site.STATE / "certificate.json")
        except ops.BackupOpsError as exc:
            if action != "auto-renew" or exc.code != "another_run_is_active":
                raise
            result = {"status": "deferred"}
    return {**result, "url": site.single_yaml_site.site_url(os.environ["GIFTLEDGER_SITE_IP"])}


def error_code(exc):
    code = getattr(exc, "code", None)
    if not code and type(exc).__name__ == "CertificateRenewalError":
        code = str(exc)
    return code if isinstance(code, str) and re.fullmatch("[a-z0-9_-]{1,100}", code) else "certificate_operation_failed"


ERROR_HELP = {
    "certificate_site_port_mismatch": "维护命令中的地址或 HTTPS 端口与本站 Web 的端口映射不符。请核对“网站地址.txt”和该项目的 Web 端口映射，再执行本站命令。",
    "site_root_certificate_replacement_required": f"本站根证书的剩余有效期不足以签发完整的 {LEAF_DAYS} 天网站证书。请保留现有密钥，安排更换根证书及各设备的信任配置。",
    "certificate_web_must_be_online_to_verify": "本站 Web 目前已停止。请先在 Container Manager 启动本站 Web，确认原网址可以连接后再续签。",
    "certificate_web_unreachable": "无法连接本站发布的 HTTPS 端口。请核对 Web 日志、端口映射，以及群晖能否访问本站网址。",
    "certificate_live_chain_invalid": "原网址出示的证书未通过本站根证书或 IP 核验。请核对网址和系统时间，并按本站维护说明核对根证书指纹。",
    "certificate_live_leaf_mismatch": "原网址出示的证书和本次签发的证书不一致。请检查该端口是否指向本站 Web。",
    "certificate_public_root_changed": "公开根证书与本站登记的根指纹不一致。请按本站维护说明，对照 prepare 日志核对“本站根证书.cer”。",
    "certificate_or_clock_is_in_future": "当前系统时间早于已登记证书的签发时间。请核对群晖的时间和时区，再续签。",
    "certificate_web_start_failed": "本站 Web 启动失败。请在 Container Manager 查看本项目 Web 的日志和端口占用，处理后重新运行同一续签命令。",
    "certificate_web_stop_failed": "本站 Web 未能正常停止。请在 Container Manager 查看本项目 Web 的状态和日志，然后重新运行同一续签命令。",
    "certificate_web_state_unconfirmed": "未能确认本站 Web 的运行状态。请查看本项目 Web 的状态和日志，然后重新运行同一续签命令。",
    "certificate_unfinished_target_changed": "未完成的续签记录与当前项目、目录或工具版本不一致。请用原项目当时的维护命令和版本继续。",
    "certificate_web_identity_changed": "上次操作对应的 Web 容器已经改变。请保留当前记录，核对项目和容器后再处理。",
    "unfinished_backup_blocks_certificate": "本站还有未结束的备份。请先完成该备份的核验和清理，再续签。",
    "unfinished_restore_blocks_certificate": "本站还有未结束的恢复。请先完成该次恢复或通过恢复工具取消，再续签。",
    "certificate_site_ca_required": "此项目缺少本站根证书签发配置。请核对项目名和项目目录，并查看该安装版本的证书维护指南。",
}


def failure_message(exc):
    code = error_code(exc)
    if isinstance(exc, RestoredWebError):
        reason = exc.reason
        advice = ERROR_HELP.get(reason, "请查看任务输出，并用本站证书查看命令核对当前证书的到期时间。")
        detail = f"（{reason}）" if reason and re.fullmatch("[a-z0-9_-]{1,100}", reason) else ""
        return f"本次续签未成功。已恢复原证书，本站 Web 已重新运行。{advice}{detail}（{code}）"
    advice = ERROR_HELP.get(code, "请执行本站维护说明中的证书查看命令。若显示操作未完成，请在同一项目中重新运行原续签命令。")
    return f"证书操作未完成。{advice}（{code}）"


def print_status(result):
    if result["status"] == "completed":
        print("本站 HTTPS 证书已续签，已通过原网址核验新证书。")
    elif result["status"] == "unfinished":
        print("上次证书操作未完成。请重新运行同一项目的续签命令，工具会继续处理本次操作。")
    if "not_after" in result:
        print("网站证书到期时间：" + result["not_after"])
        print("根证书到期时间：" + result["root_not_after"])
        print("根证书 SHA-256：" + result["root_sha256"])
        now = datetime.now(timezone.utc)
        if not root_covers_renewal(result, now):
            print(ERROR_HELP["site_root_certificate_replacement_required"])
        elif datetime.fromisoformat(result["not_after"]) <= now:
            print("网站证书已到期。请执行本站维护说明中的证书续签命令，然后重新打开原网址。")
        elif certificate_auto.renewal_due(result["not_after"], now):
            print(f"网站证书将在 {certificate_auto.RENEW_BEFORE.days} 天内到期。请检查本站自动续签任务的运行结果，或执行本站维护说明中的证书续签命令。")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:2] in (["worker"], ["probe"]):
        try:
            function = run_worker if sys.argv[1] == "worker" else probe
            result = {"ok": True, "result": function(*sys.argv[2:])}
        except Exception as exc:
            result = {"ok": False, "error": error_code(exc)}
        print(json.dumps(result, ensure_ascii=False))
    else:
        try:
            require(len(sys.argv) == 2, "certificate_action_required")
            result = run(sys.argv[1])
            if sys.argv[1] != "auto-renew" or not certificate_auto.report_daily_result(result):
                print_status(result)
        except Exception as exc:
            sys.exit(failure_message(exc))
