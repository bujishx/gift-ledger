"""Process owner-confirmed restores on the target host, independently of its web service."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

import maintenance_enrollment as enroll
import maintenance_enrollment_protocol as wire
import maintenance_restore as engine
import mac_restore_bundle as bundles
import restore_files

TERMINAL = {"succeeded", "cancelled", "expired", "failed"}
FIXED_FILES = {"backup.json", "verify.json", "BUNDLE.json", "SHA256SUMS"}
MARKER = "restore.request"


class RestoreKey(enroll.Helper):
    temporary_restore = True


def require(test, code):
    if not test:
        raise ValueError(code)


def write(host, state, handles, record):
    host.atomic_file(state / "web-restore.json", wire.encode(record), 0o600)
    publish(host, state, handles, record["public"])


def publish(host, state, handles, value):
    raw = wire.encode(value)
    if value["state"] in TERMINAL:
        name = "restore-" + value["operation_id"] + ".json"
        host.atomic_file(state / "jobs" / name, raw, 0o600)
        host.publish(handles["status"], name, raw)
    host.publish(handles["status"], "restore.json", raw)


def current(host, state, handles, cfg):
    path = state / "web-restore.json"
    if path.exists():
        record = wire.unpack(path.read_bytes())
        require((record["public"]["project"], record["public"]["instance_id"]) ==
                (cfg["project"], cfg["instance"]), "restore_site_binding_changed")
        publish(host, state, handles, record["public"])
        return record
    record = {"public": dict(project=cfg["project"], instance_id=cfg["instance"], operation_id=None, state="idle")}
    write(host, state, handles, record)
    return record


def allowed_names(names):
    return names == {"input.zip"} or (len(names) == 5 and FIXED_FILES <= names and
           re.fullmatch(r"backup-[a-f0-9]{32}\.glgui\.age", next(iter(names - FIXED_FILES), "")) is not None)


@contextmanager
def upload_dir(requests, operation, uid):
    fd = os.open("restore-upload-" + operation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=requests)
    try:
        st = os.fstat(fd)
        require(st.st_uid == uid and stat.S_IMODE(st.st_mode) == 0o700, "restore_upload_directory_changed")
        yield fd
    finally:
        os.close(fd)


def snapshot(src, directory, uid):
    names = set(os.listdir(src))
    require(allowed_names(names), "restore_upload_files_invalid")
    for name in names:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
        with os.fdopen(fd, "rb") as data:
            st = os.fstat(fd)
            require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == uid and
                    stat.S_IMODE(st.st_mode) == 0o600 and st.st_size > 0, "restore_upload_file_changed")
            require(st.st_size < shutil.disk_usage(directory).free and
                    (name not in FIXED_FILES or st.st_size <= 2 * 1024**2), "restore_upload_size_invalid")
            with (directory / name).open("xb") as dest:
                shutil.copyfileobj(data, dest, 1024**2)
                end = os.fstat(fd)
                require(end.st_size == dest.tell() == st.st_size and end.st_ctime_ns == st.st_ctime_ns,
                        "restore_upload_changed_during_copy")
    return directory / "input.zip" if names == {"input.zip"} else directory


def retire_submission(host, handles, operation):
    fd = handles["requests"]
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        if enroll.marker(host, fd, MARKER) != operation:
            return
        try:
            with upload_dir(fd, operation, host.API_UID) as up:
                names = set(os.listdir(up))
                require(allowed_names(names) or not names or
                        all(n in FIXED_FILES or n == "input.zip" or re.fullmatch(r"backup-[a-f0-9]{32}\.glgui\.age", n)
                            for n in names), "restore_upload_files_invalid")
                for name in names:
                    os.unlink(name, dir_fd=up)
            os.rmdir("restore-upload-" + operation, dir_fd=fd)
        except FileNotFoundError:
            pass
        for name in (operation + ".restore-packet", operation + ".restore-confirm", operation + ".restore-cancel", MARKER):
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


class Task:
    def __init__(self, host, state, handles, cfg, record, archive, image):
        self.host, self.state, self.handles, self.cfg = host, state, handles, cfg
        self.record, self.public = record, record["public"]
        self.archive, self.image = archive, image
        self.op = self.public["operation_id"]
        self.exchange = state / ("restore-key-" + self.op)
        self.input = Path(cfg["station"]) / "restore-input"

    def set(self, phase, **fields):
        self.public["state"] = phase
        self.public.update(fields)
        write(self.host, self.state, self.handles, self.record)

    def check_input(self):
        with self.host.directories(self.input) as root:
            st = os.fstat(root)
            require(st.st_uid == 0 and not st.st_mode & 0o022, "restore_input_not_host_owned")
            return self.host.identity(st)

    def stage(self, helper):
        self.record["input_inode"] = self.check_input()
        with tempfile.TemporaryDirectory(dir=helper.state, prefix="web-restore-upload-") as tmp:
            folder = Path(tmp)
            with upload_dir(self.handles["requests"], self.op, self.host.API_UID) as src:
                path = snapshot(src, folder, self.host.API_UID)
            with bundles.open_bundle(path, temporary_directory=helper.state) as bundle:
                require(bundle.source["source_instance_id"] != self.cfg["instance"], "restore_requires_independent_site")
                self.record["bundle"] = dict(id=bundle.backup_id, digest=bundle.manifest_sha256,
                                             source=bundle.source, count=bundle.gift_count)
                self.set("queued")
                require(self.check_input() == self.record["input_inode"], "restore_input_changed")
                # Retire only the folder this web job introduced. Preserve an existing identical copy.
                existed = os.path.lexists(self.input / bundle.backup_id)
                self.record["may_own_copy"] = not existed
                self.set("queued")
                restore_files.stage_or_retire("stage", bundle.backup_id, bundle.manifest_sha256,
                                              root=self.input, incoming=bundle.folder)

    def key_helper(self):
        return RestoreKey(self.archive, self.cfg, self.handles, self.image)

    def begin(self):
        helper = self.key_helper()
        with self.archive.ops.host_lock(helper.state):
            self.archive.site.check_certificate_idle(helper.state)
            require(all(row["stage"] in ("finished", "preflight-failed") and not row.get("private_cleanup_pending")
                        for _, row in self.archive.ops.run_records(helper.state)), "unfinished_backup_blocks_restore")
            journal = helper.state / "restore.json"
            if journal.exists():
                require(self.archive.ops.read_json(journal)["stage"] == "rejected", "target_has_another_restore")
            self.stage(helper)
            self.exchange.mkdir(mode=0o700)
            self.host.sync_dir(self.state)
            # Arm a fresh, operation-labelled volume. An older CLI/web key belongs to its own operation.
            name = self.cfg["project"] + "_restore_identity"
            require(name not in helper.docker.call("volume", "ls", "--format", "{{.Name}}").splitlines(),
                    "existing_temporary_restore_key")
            self.record["key_started"] = True
            self.set("queued")
            self.record["container"] = helper.start(self.op, self.exchange)
            self.set("queued")
            helper.docker.call("start", self.record["container"])
        self.receive_key(helper)

    def stopped(self):
        return enroll.marker(self.host, self.handles["requests"], self.op + ".restore-cancel") == self.op

    def receive_key(self, helper):
        # The same, bounded helper processes the original packet. Its transport private key is transient.
        container = self.record["container"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            target = enroll.private_output(self.exchange, "target.json")
            if target:
                require(set(target) == {"registered", "target"} and target["registered"] is False,
                        "restore_key_volume_not_empty")
                wire.check_target(target["target"], self.cfg["project"], self.cfg["instance"])
                if "target" not in self.public:
                    self.set("awaiting_packet", target=target["target"])
                require(self.public["target"] == target["target"], "restore_key_target_changed")
                packet = enroll.sealed_packet(self.host, self.handles["requests"], self.op + ".restore-packet")
                if packet is not None:
                    request = wire.check_packet(packet, self.op, target["target"])
                    require(self.record.get("request", request) == request, "restore_key_request_changed")
                    if "request" not in self.record:
                        self.record["request"] = request
                        self.set("preflighting")
                    if not (self.exchange / "packet.json").exists():
                        self.host.atomic_file(self.exchange / "packet.json", wire.encode(packet), 0o600)
                receipt = enroll.private_output(self.exchange, "receipt.json")
                if receipt:
                    require("request" in self.record, "restore_key_unrequested_receipt")
                    wire.check_receipt(receipt, target["target"], self.record["request"])
                    require(helper.docker.call("wait", container, timeout=15) == "0", "restore_key_helper_failed")
                    helper.retire_original(self.op, container)
                    self.record["received"] = True
                    self.public.pop("target", None)
                    self.set("preflighting")
                    return
            if self.stopped() and "request" not in self.record:
                break
            status = helper.docker.inspect(container)
            if not status["running"] and not (self.exchange / "receipt.json").exists():
                raise ValueError("restore_key_helper_failed")
            time.sleep(.3)
        raise ValueError("restore_key_cancelled" if self.stopped() else "restore_key_timeout")

    def run_engine(self, action):
        bundle = self.record["bundle"]
        require(self.check_input() == self.record["input_inode"], "restore_input_changed")
        return engine.run(self.cfg, self.image, action, bundle["id"], bundle["digest"],
                          operation=self.record.get("engine_op"), key_operation=self.op)

    def preview(self):
        proof = self.run_engine("preflight")
        bundle = self.record["bundle"]
        target = proof["target"]
        self.record["engine_op"] = proof["operation_id"]
        require(wire.matches(wire.OP, proof["operation_id"]) and proof["eligible"] is True and
                proof["full_decryption_verified"] is True and proof["source"] == bundle["source"] and
                (target["project"], target["instance_id"]) == (self.cfg["project"], self.cfg["instance"]) and
                proof["gift_count"] == bundle["count"], "restore_preflight_receipt_mismatch")
        self.record["deadline"] = time.time() + 1800
        self.set("eligible", preview=dict(backup_id=bundle["id"], manifest_sha256=bundle["digest"],
                 source_project=proof["source"]["source_project"], source_instance_id=proof["source"]["source_instance_id"],
                 gift_count=bundle["count"], target_url=target["url"], target_project=target["project"],
                 target_instance_id=target["instance_id"], engine_operation_id=self.record["engine_op"]))

    def cleanup(self):
        if self.record.get("key_started"):
            if not self.record.get("received"):
                if self.exchange.is_dir():
                    self.host.atomic_file(self.exchange / "cancel", b"cancel\n", 0o600)
                self.key_helper().retire_original(self.op, self.record.get("container"))
            outcome = engine.cleanup_temporary(self.cfg, self.image, self.op)
            require(outcome["absent"] is True, "temporary_restore_key_cleanup_pending")
        bundle = self.record.get("bundle")
        if bundle and self.record.get("may_own_copy"):
            require(self.check_input() == self.record["input_inode"], "restore_input_changed")
            restore_files.stage_or_retire("retire-owned", bundle["id"], bundle["digest"], root=self.input)
        if self.exchange.exists():
            for path in self.exchange.iterdir():
                require(path.name in ("cancel", "target.json", "packet.json", "receipt.json"),
                        "restore_key_exchange_changed")
                path.unlink()
            self.exchange.rmdir()
            self.host.sync_dir(self.state)

    def finish(self, phase, **fields):
        self.cleanup()
        self.public.pop("target", None)
        self.set(phase, **fields)
        retire_submission(self.host, self.handles, self.op)

    def apply(self):
        proof = self.run_engine("apply")
        preview = self.public["preview"]
        require(proof["operation_id"] == self.record["engine_op"] and
                proof["backup_operation_id"] == preview["backup_id"] and proof["gift_count"] == preview["gift_count"] and
                proof["target"]["instance_id"] == self.cfg["instance"] and proof["full_database_and_app_files_verified"] is True
                and proof["temporary_key_removed"] is True, "restore_final_receipt_mismatch")
        result = {k: proof[k] for k in ("full_database_and_app_files_verified", "gift_count",
                  "cash_amount_all_gift_records", "temporary_key_removed")}
        self.finish("succeeded", result=result)

    def engine_rejected(self):
        if not self.record.get("engine_op"):
            return False
        status = self.run_engine("status")
        bundle = self.record["bundle"]
        return (status.get("stage") == "rejected" and status.get("operation_id") == self.record["engine_op"]
                and status.get("bundle_sha256") == bundle["digest"] and status.get("source") == bundle["source"])

    def confirm(self):
        confirm = enroll.sealed_packet(self.host, self.handles["requests"], self.op + ".restore-confirm")
        if self.stopped() or time.time() >= self.record["deadline"]:
            self.run_engine("cancel")
            self.finish("cancelled" if self.stopped() else "expired")
            return True
        if confirm is None:
            return False
        require(confirm == {"operation_id": self.op, "preview": self.public["preview"]}, "restore_confirmation_target_changed")
        self.record["confirmed"] = True
        self.set("applying")
        self.apply()
        return True


def tick(host, state, handles, cfg, record, archive, image):
    public = record["public"]
    op = enroll.marker(host, handles["requests"], MARKER)
    active = public["state"] not in TERMINAL | {"idle"}
    if op is None:
        require(not active, "restore_request_marker_missing")
        return False
    if op == public["operation_id"] and public["state"] in TERMINAL:
        retire_submission(host, handles, op)
        return False
    if not active:
        if (state / "jobs" / ("restore-" + op + ".json")).exists():
            retire_submission(host, handles, op)
            return False
        record.clear()
        record["public"] = dict(project=cfg["project"], instance_id=cfg["instance"], operation_id=op, state="queued")
        write(host, state, handles, record)
    else:
        require(op == public["operation_id"], "restore_request_marker_changed")
    task = Task(host, state, handles, cfg, record, archive, image)
    try:
        if active:
            if public["state"] == "eligible":
                return task.confirm()
            if public["state"] == "needs_review":
                return False
            # An interruption never authorizes submitting another import to MySQL.
            task.set("needs_review", code="restore_interrupted")
            return True
        if task.stopped():
            task.finish("cancelled")
            return True
        task.begin()
        task.preview()
    except Exception as exc:
        code = host.machine_code(exc, "restore_failed")
        try:
            if task.public["state"] == "applying" and task.engine_rejected():
                task.finish("failed", code=code)
            elif task.public["state"] == "applying" or (task.record.get("engine_op") and task.public["state"] != "eligible"):
                task.set("needs_review", code=code)
            else:
                if task.record.get("engine_op"):
                    task.run_engine("cancel")
                task.finish("cancelled" if code == "restore_key_cancelled" else "failed", code=code)
        except Exception as cleanup_error:
            task.set("needs_review", code=host.machine_code(cleanup_error, "restore_cleanup_requires_review"))
    return True


def review(host, state, handles, cfg, archive, image, operation):
    record = current(host, state, handles, cfg)
    public = record["public"]
    require(public["operation_id"] == operation and public["state"] in ("applying", "needs_review"),
            "restore_review_operation_changed")
    require(enroll.marker(host, handles["requests"], MARKER) == operation, "restore_review_marker_changed")
    task = Task(host, state, handles, cfg, record, archive, image)
    if record.get("confirmed"):
        require(record.get("engine_op") == public["preview"]["engine_operation_id"], "restore_review_target_changed")
        # The engine owns the DB journal and checks durable evidence before permitting resumption.
        if task.engine_rejected():
            task.finish("failed", code="restore_rejected_before_writes")
        else:
            task.apply()
    else:
        if record.get("bundle") and record.get("received"):
            evidence = task.run_engine("status")
            if evidence["stage"] in ("preflight", "eligible"):
                require(evidence["bundle_sha256"] == record["bundle"]["digest"] and
                        evidence["source"] == record["bundle"]["source"], "restore_review_input_changed")
                record["engine_op"] = evidence["operation_id"]
                task.run_engine("cancel")
            else:
                require(evidence["stage"] in ("none", "rejected"), "restore_review_unexpected_stage")
        task.finish("cancelled", code="restore_interrupted_before_confirmation")
    return public
