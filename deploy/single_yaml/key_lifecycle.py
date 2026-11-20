"""Root-authorized recovery and rotation on an already bound DSM or Ubuntu host."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import uuid

import backup_ops as ops
import backup_site
import dsm_backup

JOURNAL = "key-change.json"
FIELDS = ("identity.txt", "import-started.json", "identity-imported.json")
ACTIVE = {"testing", "committing"}


def valid_private_dir(path):
    ops.no_links(path)
    st = path.lstat()
    ops.require(stat.S_ISDIR(st.st_mode) and (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (0, 0, 0o700)
                and not any("acl" in a.lower() for a in os.listxattr(path, follow_symlinks=False)),
                "untrusted_key_change_directory")


def private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    valid_private_dir(path)
    return path


def cleanup_candidate(state, operation):
    path = state / ("key-change-" + operation)
    if not path.exists():
        return
    valid_private_dir(path)
    for part in path.iterdir():
        st = part.lstat()
        ops.require(part.name in FIELDS and stat.S_ISREG(st.st_mode) and st.st_uid == 0 and st.st_nlink == 1
                    and stat.S_IMODE(st.st_mode) == 0o600, "unexpected_key_staging_file")
    for part in path.iterdir():
        part.unlink()
    path.rmdir()
    ops.fsync_directory(state)


def discard_unprepared(state, row):
    # The host lock is held. A previous 'prepared' job may already have a saved
    # key and a valid confirmation; merely closing this interaction preserves it.
    if row["stage"] == "preparing":
        current = ops.read_json(state / JOURNAL)
        ops.require(current == row, "key_change_journal_mismatch")
        row["stage"] = "cancelled"
        ops.save_json(state / JOURNAL, row)
        cleanup_candidate(state, row["operation"])


def discover(installer, root, install):
    cfg = installer.host.read_config(root / "state")
    for key in ("project", "station", "station_inode", "instance"):
        ops.require(cfg[key] == install[key], "key_site_binding_changed")
    platform, executable = installer.platform()
    ops.require(platform == install["platform"], "site_platform_changed")
    docker = ops.Docker(executable)
    site = dsm_backup.HostSite(docker, cfg["project"], cfg["station"])
    os.environ.update(site.settings)
    profile = site.profile()
    ops.require(profile["maintenance_image"] == install["image"] and profile["instance_id"] == install["instance"],
                "key_site_identity_changed")
    with installer.owner.directory(site.station) as fd:
        ops.require(installer.host.identity(os.fstat(fd)) == install["station_inode"], "key_site_directory_changed")
    binding, ip, port = installer.owner.binding(install["project"], install["instance"], install["origin"])
    installer.owner.verify_site_metadata(site.station, binding)
    backup_site.check_web_endpoint(docker, profile["containers"]["web"]["id"], ip, port, "key_site_port_mismatch")
    return site, profile


def key_volume(docker, project, role):
    volume = project + "_" + role
    obj, = json.loads(docker.call("volume", "inspect", volume))
    labels = obj["Labels"] or {}
    ops.require(obj["Name"] == volume and obj["Driver"] == "local" and not obj.get("Options")
                and labels.get("com.docker.compose.project") == labels.get("org.gift-ledger.gui-v2-project") == project
                and labels.get("com.docker.compose.volume") == labels.get("org.gift-ledger.gui-v2-role") == role,
                "key_volume_identity_mismatch")
    return volume


def helper(site, profile, action, operation, arguments, *, candidate=None, bundle=None):
    docker = site.docker
    project = profile["project"]
    volume = key_volume(docker, project, "backup_identity")
    mounts = [("/work/identity", backup_site.mount("volume", volume, action == "commit"))]
    if action in ("existing-target", "existing-import"):
        transport = key_volume(docker, project, "key_transfer")
        mounts.append(("/work/key-transfer", backup_site.mount("volume", transport, False)))
    if candidate is not None:
        valid_private_dir(candidate)
        mounts.append(("/work/candidate", backup_site.mount("bind", candidate, action in ("prepare", "existing-import"))))
    if bundle is not None:
        ops.no_links(bundle)
        mounts.append(("/work/source", backup_site.mount("bind", bundle, False)))
    options = []
    for dest, binding in mounts:
        options += ["--mount", backup_site.mount_argument(dest, binding)]
    # The selected binary, image, mounts and identity come only from this installed site's trusted binding.
    label = uuid.uuid4().hex
    name = "gl-admin-key-" + profile["instance_id"] + "-" + label
    cid = docker.call("create", "--name", name, "--network=none", "--read-only", "--restart=no",
        "--log-driver=none", "--security-opt=no-new-privileges", "--cap-drop=ALL", "--user=0:0",
        "--memory=128m", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1m,mode=0700",
        "--tmpfs", "/var/lib/mysql:rw,noexec,nosuid,nodev,size=1m",
        "--label", "org.gift-ledger.gui-v2-project=" + project,
        "--label", "org.gift-ledger.maintenance-instance=" + profile["instance_id"],
        "--label", "org.gift-ledger.backup-session=" + label,
        *options, "--entrypoint", "/opt/venv/bin/python3.12", profile["maintenance_image"],
        "/opt/gift-ledger-install/key_lifecycle_worker.py", action, project, profile["instance_id"],
        operation, *arguments)
    ops.require(ops.SHA.fullmatch(cid), "invalid_key_helper_id")
    info = docker.inspect(cid)
    ops.require(info["id"] == cid and info["name"] == "/" + name and info["image"] == profile["maintenance_image"]
                and info["session"] == label and info["status"] == "created", "key_helper_identity_changed")
    try:
        backup_site.check_mounts(info["mounts"], dict(mounts), {"/tmp", "/var/lib/mysql"})
        try:
            raw = docker.call("start", "--attach", cid, timeout=3600)
            done = docker.inspect(cid)
            ops.require(done["id"] == cid and done["status"] == "exited" and not done["oom"]
                        and done["exit_code"] == 0, "docker_command_failed")
        except ops.BackupOpsError as exc:
            if exc.code == "docker_command_failed" and action == "recover":
                raise ops.BackupOpsError("recovery_private_key_or_backup_failed_verification") from None
            raise
    finally:
        if not docker.inspect(cid)["running"]:
            docker.call("rm", cid)
    result = json.loads(raw)
    ops.require(isinstance(result, dict) and ops.RECIPIENT.fullmatch(result.get("recipient", "")),
                "key_helper_result_invalid")
    return result


def response(packet):
    sealed = packet["sealed"]
    ops.require(0 < len(base64.b64decode(sealed, validate=True)) <= 8192, "invalid_sealed_result")
    print("将下一行加密回执复制到刚才生成接收码的维护页：")
    print(sealed)


def receiver():
    value = input("粘贴当前维护页生成的接收码（以 age1 开头）：").strip()
    ops.require(ops.RECIPIENT.fullmatch(value), "invalid_public_receive_code")
    return value


def idle(site):
    state, _ = site.directories()
    backup_site.check_certificate_idle(state)
    ops.require(not any(row["stage"] not in ("finished", "preflight-failed") or row.get("private_cleanup_pending")
                        for _, row in ops.run_records(state)), "unfinished_backup_requires_review")
    path = state / "restore.json"
    ops.require(not path.exists() or ops.read_json(path)["stage"] in ("eligible", "preflight", "completed", "rejected"),
                "unfinished_restore_blocks_key_action")


def candidate_backup(site, profile, install, row):
    state, store = site.directories()
    operation = row["operation"]
    trial = row.get("trial", operation)
    journal = state / "runs" / trial / "run.json"
    previous = ops.read_json(journal) if journal.exists() else None
    if previous and previous["stage"] == "preflight-failed":
        with ops.host_lock(state):
            ops.require(previous["session_id"] == trial and previous["project"] == profile["project"]
                        and previous["instance_id"] == profile["instance_id"] and not previous.get("bundle_id")
                        and not previous.get("private_cleanup_pending")
                        and not backup_site.previous_worker(profile, site.docker, previous),
                        "failed_key_trial_requires_review")
            # Keep the original failed run and the same saved candidate; retry in a new
            # engine session so a never-started worker isn't mistaken for an interrupted one.
            row["trial"] = trial = uuid.uuid4().hex
            ops.save_json(state / JOURNAL, row)
            journal, previous = state / "runs" / trial / "run.json", None
    args = dict(engine_session=trial, recover=previous is not None)
    if previous is None or previous.get("stage") != "finished" or previous.get("private_cleanup_pending"):
        if install["platform"] == "dsm":
            dsm_backup.run(site.project, site.station, expected_instance=install["instance"],
                           key_change=operation, **args)
        else:
            backup_site.run_backup(site, backup_site.candidate_profile(site, profile, operation),
                                   site.docker, backup_site.Worker, **args)
    proof = ops.read_json(journal)
    last = ops.read_json(state / "last-result.json")
    ops.require(proof["session_id"] == trial and proof["stage"] == "finished"
                and not proof.get("private_cleanup_pending") and ops.ID.fullmatch(proof.get("bundle_id") or "")
                and last.get("session_id") == trial and last.get("bundle_id") == proof["bundle_id"]
                and last.get("local_verified") is True and last.get("success") is True
                and last.get("services_restored") is True and last.get("private_cleanup_pending") is False,
                "key_change_backup_not_verified")
    manifest = ops.backup_store.inspect_bundle(store / "artifacts" / proof["bundle_id"])
    ops.require(ops.bundle_matches_run(manifest, proof, profile), "key_change_backup_manifest_mismatch")
    return proof["bundle_id"]


def authorize(action, install):
    print("项目 " + install["project"] + "，网址 " + install["origin"] + "，实例 " + install["instance"])
    word = {"recover-key":"补领", "prepare-key-change":"准备换钥", "prepare-existing-key-change":"准备换钥", "finish-key-change":"确认换钥",
            "cancel-key-change":"取消换钥"}[action]
    ops.require(input("确认授权本站操作，请输入“" + word + "”，回车退出：").strip() == word, "operator_cancelled")


def run(installer, root, install, action):
    ops.require(os.geteuid() == 0 and os.isatty(0), "interactive_server_administrator_required")
    site, profile = discover(installer, root, install)
    state, store = site.directories()
    journal = state / JOURNAL
    authorize(action, install)
    # The caller holds the installed per-site administrator operation lock.
    row = ops.read_json(journal) if journal.exists() else None
    if row:
        ops.require(row["instance"] == install["instance"] and row["project"] == site.project
                    and ops.ID.fullmatch(row["operation"]), "key_change_journal_mismatch")
        if row["stage"] in ("completed", "cancelled"):
            cleanup_candidate(state, row["operation"])
    if action == "recover-key":
        ops.require(not row or row["stage"] not in ACTIVE, "finish_key_change_first")
        public = receiver()
        print("请输入本站 backups/artifacts 中仍可读取的备份编号。若没有匹配的备份，请先运行一次常规备份后重新补领。")
        last = state / "last-result.json"
        default = ops.read_json(last).get("bundle_id") if last.exists() else None
        backup = input("输入要核验的本站备份编号（直接回车使用最近备份 " + str(default or "无") + "）：").strip() or default
        ops.require(isinstance(backup, str) and ops.ID.fullmatch(backup), "a_matching_backup_is_required")
        with ops.host_lock(state):
            idle(site)
            selected = store / "artifacts" / backup
            records = [row for _, row in ops.run_records(state) if row.get("bundle_id") == backup]
            ops.require(selected.is_dir() and len(records) == 1 and records[0].get("stage") == "finished"
                        and not records[0].get("private_cleanup_pending") and
                        ops.bundle_matches_run(ops.backup_store.inspect_bundle(selected), records[0], profile),
                        "matching_published_site_backup_required")
            reply = helper(site, profile, "recover", uuid.uuid4().hex,
                           [public, install["origin"], backup, profile["server_uuid"]], bundle=selected)
            ops.require(reply["recipient"] == profile["recipient"], "registered_key_mismatch")
            response(reply)
        return
    if action in ("prepare-key-change", "prepare-existing-key-change"):
        existing = action == "prepare-existing-key-change"
        source = "existing" if existing else "generated"
        public = receiver()
        with ops.host_lock(state):
            idle(site)
            if not row or row["stage"] in ("completed", "cancelled"):
                row = {"project":site.project, "instance":profile["instance_id"], "operation":uuid.uuid4().hex,
                       "original":profile["recipient"], "source":source, "stage":"preparing"}
                ops.save_json(journal, row)
            ops.require(row["stage"] in ("preparing", "prepared") and row["original"] == profile["recipient"],
                        "finish_key_change_first")
            ops.require(row.get("source", "generated") == source, "cancel_other_key_change_first")
        if existing:
            target = helper(site, profile, "existing-target", row["operation"], [public, install["origin"]])
            ops.require(target["recipient"] == row["original"], "registered_key_mismatch")
            response(target)
            try:
                encrypted = input("请在同一维护页打开回执，将页面给出的加密钥匙包粘贴到这里，回车退出：").strip()
            except (EOFError, KeyboardInterrupt):
                encrypted = ""
            if not encrypted:
                with ops.host_lock(state):
                    discard_unprepared(state, row)
                print("已退出本次输入，当前登记钥匙继续使用。" +
                      (" 此前已准备的换钥仍可完成或取消。" if row["stage"] == "prepared" else ""))
                return
            ops.require(len(encrypted) <= 12000 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encrypted),
                        "encrypted_key_package_required")
        with ops.host_lock(state):
            idle(site)
            if row["stage"] == "preparing":
                cleanup_candidate(state, row["operation"])
            candidate = private_dir(state / ("key-change-" + row["operation"]))
            args = [public, install["origin"]]
            if existing:
                args.append(encrypted)
            reply = helper(site, profile, "existing-import" if existing else "prepare", row["operation"],
                           args, candidate=candidate)
            if existing and reply.get("unchanged") is True:
                ops.require(reply["recipient"] == row["original"], "invalid_candidate_receipt")
                discard_unprepared(state, row)
                print("这就是本站当前的钥匙，无需更换。" +
                      (" 此前已准备的换钥仍可完成或取消。" if row["stage"] == "prepared" else ""))
                return
            ops.require(reply["recipient"] != row["original"] and ops.SHA.fullmatch(reply["challenge_hash"])
                        and set(reply["old_hashes"]) == set(FIELDS)
                        and all(ops.SHA.fullmatch(v) for v in reply["old_hashes"].values()), "invalid_candidate_receipt")
            row.update(stage="prepared", recipient=reply["recipient"], challenge_hash=reply["challenge_hash"],
                       old_hashes=reply["old_hashes"])
            ops.save_json(journal, row)
            if existing:
                print("待启用公钥 " + row["recipient"] + "。请使用维护页给出的确认码执行完成换钥命令。")
            else:
                response(reply)
        return
    ops.require(row is not None, "prepare_key_change_first")
    if action == "cancel-key-change":
        with ops.host_lock(state):
            # A fully finished failed candidate backup may be retried with a fresh candidate.
            previous = state / "runs" / row.get("trial", row["operation"]) / "run.json"
            done = ops.read_json(previous) if previous.exists() else {}
            safe_test = row["stage"] == "testing" and (not done or
                (done["stage"] in ("preflight-failed", "finished") and not done.get("private_cleanup_pending")
                 and not done.get("bundle_id")))
            ops.require(row["stage"] in ("preparing", "prepared", "cancelled") or safe_test,
                        "rotation_requires_completion")
            idle(site)
            row["stage"] = "cancelled"
            ops.save_json(journal, row)
            cleanup_candidate(state, row["operation"])
            print("已取消，本站继续使用原钥匙。")
        return
    if row["stage"] == "completed":
        print("此次换钥已完成。已核验的首份备份编号 " + row["backup"])
        return
    ops.require(row["stage"] in ("prepared", "testing", "committing"), "prepare_key_change_first")
    if row["stage"] == "prepared":
        # Older pages sent the bare token. New pages append only the basename
        # actually selected by the owner; never treat it as a server path.
        from maintenance_enrollment_protocol import valid_filename
        saved, separator, encoded_name = input("粘贴维护页显示的本次换钥确认码：").strip().partition(".")
        if separator:
            try:
                name = base64.b64decode(encoded_name, validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                name = ""
            ops.require(valid_filename(name), "invalid_key_filename")
            row["filename"] = name
        ops.require(hmac.compare_digest(hashlib.sha256(saved.encode()).hexdigest(), row["challenge_hash"]),
                    "saved_key_confirmation_mismatch")
    if row["stage"] in ("prepared", "testing"):
        with ops.host_lock(state):
            if row["stage"] == "prepared":
                idle(site)
            row["stage"] = "testing"
            ops.save_json(journal, row)
        backup = candidate_backup(site, profile, install, row)
        with ops.host_lock(state):
            row.update(stage="committing", backup=backup)
            ops.save_json(journal, row)
    with ops.host_lock(state):
        identity = ops.read_json(state / "site.json")
        ops.require(identity["instance_id"] == install["instance"] and identity["project"] == site.project
                    and identity["backup_recipient"] in (row["original"], row["recipient"]), "key_registration_changed")
        reply = helper(site, profile, "commit", row["operation"], [json.dumps(row["old_hashes"])],
                       candidate=state / ("key-change-" + row["operation"]))
        ops.require(reply["recipient"] == row["recipient"], "candidate_verification_failed")
        ops.save_json(state / "key-label.json", {"project":row["project"], "instance_id":row["instance"],
                      "recipient":row["recipient"], "filename":row.get("filename"), "rotation_backup":row["backup"]})
        identity["backup_recipient"] = row["recipient"]
        ops.save_json(state / "site.json", identity)
        row["stage"] = "completed"
        ops.save_json(journal, row)
        cleanup_candidate(state, row["operation"])
        print("新钥匙已启用，首份备份已实际解密核验。请另存整份备份 backups/artifacts/" + row["backup"] + "/")
