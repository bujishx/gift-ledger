"""Pinned host dispatcher for a single site's first daily backup key enrollment."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import time

import maintenance_enrollment_protocol as wire
import owner_login_code as owner


def require(test, code):
    if not test:
        raise ValueError(code)


def marker(host, fd, name):
    try:
        raw = host.read_at(fd, name, 33, expected_uid=host.API_UID)
    except FileNotFoundError:
        return None
    require(re.fullmatch(b"[0-9a-f]{32}\n", raw), "invalid_enrollment_marker")
    return raw[:-1].decode("ascii")


def sealed_packet(host, fd, name):
    try:
        meta = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    require(0 < meta.st_size <= wire.MAX_PACKET, "invalid_enrollment_packet_size")
    return wire.unpack(host.read_at(fd, name, meta.st_size, expected_uid=host.API_UID))


def private_output(exchange, name):
    try:
        fd = os.open(str(exchange / name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as inp:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022 and 0 < info.st_size <= 4096, "invalid_enrollment_helper_output")
        raw = inp.read(4097)
        require(info.st_size == len(raw), "invalid_enrollment_helper_output")
        return wire.unpack(raw)


class Helper:
    """All mounts, argv and the immutable image come from the host's verified site."""
    temporary_restore = False

    def __init__(self, archive, cfg, handles, key_image):
        self.archive, self.cfg = archive, cfg
        binary = Path(owner.docker_args()[0])
        self.dsm = str(binary) == "/var/packages/ContainerManager/target/usr/bin/docker"
        if self.dsm:
            pkg = archive.target.Package.discover(backup_events=True)
            require(binary.resolve() == (pkg.target / "usr/bin/docker").resolve(), "enrollment_dsm_engine_required")
        else:
            require(binary == Path("/usr/bin/docker"), "enrollment_unknown_docker_binary")
            for path in (binary.resolve(), *binary.resolve().parents):
                meta = path.stat()
                require(meta.st_uid == 0 and not meta.st_mode & 0o022, "enrollment_untrusted_docker_binary")
        self.docker = archive.ops.Docker(binary)
        self.site = archive.HostSite(self.docker, cfg["project"], cfg["station"])
        self.image = self.site.runtime_profile()["maintenance_image"]
        require(self.image == key_image, "approved_key_image_mismatch")
        os.environ.update(self.site.settings)
        ip = archive.site.single_yaml_site.project_host(cfg["project"])
        origin = archive.site.single_yaml_site.site_url(ip)
        binding, _, port = owner.binding(cfg["project"], cfg["instance"], origin)
        station = self.site.station
        with owner.directory(station) as fd:
            require([os.fstat(fd).st_dev, os.fstat(fd).st_ino] == cfg["station_inode"], "station_identity_changed")
        raw, _ = owner.read_file(handles["base"], owner.CONFIG, private_config=True)
        require(wire.unpack(raw) == binding, "enrollment_owner_site_mismatch")
        owner.verify_site_metadata(station, binding)
        command = self.docker.argv()
        api = owner.service(command, cfg["project"], "api")
        web = owner.service(command, cfg["project"], "web")
        owner.verify_services(api, web, station, binding, ip, port)
        self.state, _ = self.site.directories()
        with owner.directory(self.state) as fd:
            info = os.fstat(fd)
            require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, 0o700),
                    "invalid_backup_state_permissions")

    def limits(self):
        template = ('{"memory":{{json .MemoryLimit}},"quota":{{json .CPUCfsQuota}},'
                    '"period":{{json .CPUCfsPeriod}},"cpuset":{{json .CPUSet}},"pids":{{json .PidsLimit}},'
                    '"cpus":{{json .NCPU}},"security":{{json .SecurityOptions}}}')
        caps = json.loads(self.docker.call("info", "--format", template))
        require(caps["memory"] is True and isinstance(caps["cpus"], int) and caps["cpus"] > 0, "enrollment_memory_capability_required")
        flags = ["--memory=128m"]
        if caps["quota"] is True and caps["period"] is True:
            flags.append("--cpus=1")
        else:
            require(caps["cpuset"] is True, "enrollment_cpu_capability_required")
            # Keep one of the CPUs that this host process is actually allowed to run on.
            flags.append("--cpuset-cpus=" + str(min(os.sched_getaffinity(0))))
        if caps["pids"] is True:
            flags.append("--pids-limit=32")
        else:
            require(caps["pids"] is False, "enrollment_pids_capability_unknown")
            # Only the reviewed DSM package identity may use the fixed bounded helper on a kernel
            # that explicitly reports no PID controller. This is not a fallback after create errors.
            require(self.dsm, "enrollment_dsm_engine_required")
        seccomp = [s for s in (caps["security"] or []) if s.startswith("name=seccomp")]
        require(not any("unconfined" in s for s in seccomp) and (seccomp or self.dsm),
                "enrollment_default_process_filter_required")
        return flags

    def volume(self, operation=None):
        p = self.cfg["project"]
        role = "restore_identity" if self.temporary_restore else "backup_identity"
        name = p + "_" + role
        labels = {"com.docker.compose.project": p, "com.docker.compose.volume": role,
                  "org.gift-ledger.gui-v2-project": p, "org.gift-ledger.gui-v2-role": role}
        if self.temporary_restore:
            require(wire.matches(wire.OP, operation), "invalid_restore_operation")
            labels["org.gift-ledger.maintenance-restore-operation"] = operation
        flags = [arg for k, v in labels.items() for arg in ("--label", k + "=" + v)]
        # Docker create is idempotent. Reject any pre-existing unrelated volume, custom options or driver.
        require(self.docker.call("volume", "create", "--driver", "local", *flags, name) == name,
                "enrollment_volume_unavailable")
        item, = json.loads(self.docker.call("volume", "inspect", name))
        require(item["Name"] == name and item["Driver"] == "local" and not item.get("Options")
                and all((item.get("Labels") or {}).get(k) == v for k, v in labels.items()),
                "enrollment_volume_mismatch")
        return name

    def name(self, operation):
        # Distinct sites may submit the same operation ID on a shared Docker engine.
        return ("gl-restore-key-" if self.temporary_restore else "gl-key-") + self.cfg["instance"] + "-" + operation

    def start(self, operation, exchange):
        p = self.cfg["project"]
        entries = [(self.site.station / "data/root_config", "/work/site", True),
                   (exchange, "/work/exchange", False)]
        if not self.temporary_restore:
            entries.append((self.state, "/work/backup-state", False))
        flags = []
        for source, dest, readonly in entries:
            self.archive.ops.no_links(source)
            require("," not in str(source), "enrollment_host_path_unusable")
            flags.extend(("--mount", "type=bind,src=" + str(source) + ",dst=" + dest + (",readonly" if readonly else "")))
        limits = self.limits()
        if self.temporary_restore:
            volume = self.volume(operation)
            mountpoint = "/work/restore-identity"
            command = "restore-key"
        else:
            volume = self.volume()
            mountpoint = "/work/identity"
            command = "enroll"
            flags.extend(("--tmpfs", "/work/key-transfer:rw,noexec,nosuid,nodev,size=1m,mode=0700"))
        env = dict(self.site.settings, GIFTLEDGER_EXPECTED_INSTANCE=self.cfg["instance"])
        for key, val in env.items():
            flags.extend(("--env", key + "=" + val))
        image = self.image
        container = self.docker.call("create", "--name", self.name(operation), "--network", "none",
            "--restart=no", "--read-only", "--cap-drop=ALL", "--cap-add=CHOWN", "--cap-add=FOWNER",
            "--cap-add=DAC_OVERRIDE", "--security-opt=no-new-privileges", "--no-healthcheck", "--log-driver=json-file",
            "--log-opt=max-size=256k", "--log-opt=max-file=2", *limits, "--user=0:0", "--label", "org.gift-ledger.gui-v2-project=" + p,
            "--label", "org.gift-ledger.maintenance-instance=" + self.cfg["instance"],
            "--label", "org.gift-ledger.backup-session=" + operation,
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--tmpfs", "/var/lib/mysql:rw,noexec,nosuid,nodev,size=1m",
            "--mount", "type=volume,src=" + volume + ",dst=" + mountpoint, *flags,
            "--entrypoint", "/opt/venv/bin/python3.12", image, "/opt/gift-ledger-install/backup_key.py", command)
        require(wire.matches(wire.SHA, container), "invalid_enrollment_container")
        found = self.docker.inspect(container)
        require(found["id"] == container and found["image"] == image and found["session"] == operation
                and found["name"] == "/" + self.name(operation) and found["status"] == "created", "invalid_enrollment_container")
        return container

    def retire_original(self, operation, expected_id):
        name = self.name(operation)
        ids = self.docker.call("ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$").splitlines()
        if not ids:
            return
        require(len(ids) == 1 and wire.matches(wire.SHA, ids[0]) and (not expected_id or ids[0] == expected_id),
                "enrollment_helper_identity_changed")
        item = self.docker.inspect(ids[0])
        labels = json.loads(self.docker.call("inspect", "--type", "container", "--format", "{{json .Config.Labels}}", ids[0]))
        require(labels.get("org.gift-ledger.gui-v2-project") == self.cfg["project"]
                and labels.get("org.gift-ledger.maintenance-instance") == self.cfg["instance"],
                "enrollment_helper_site_mismatch")
        require(item["id"] == ids[0] and item["name"] == "/" + name and item["image"] == self.image
                and item["session"] == operation, "enrollment_helper_identity_changed")
        if item["running"]:
            # A submitted packet may still be committing. Wait for the original bounded helper.
            self.docker.call("wait", ids[0], timeout=205)
        self.docker.call("rm", ids[0])

    def committed(self, receipt):
        profile = self.site.profile()
        require(profile["instance_id"] == self.cfg["instance"] and profile["project"] == self.cfg["project"]
                and profile["recipient"] == receipt["backup_recipient"], "enrollment_site_commit_mismatch")


def write_state(host, private, handles, record):
    host.atomic_file(private / "enrollment.json", wire.encode(record), 0o600)
    sync_public(host, private, handles, record)


def sync_public(host, private, handles, record):
    public = record["public"]
    raw = wire.encode(public)
    if public["state"] in wire.TERMINAL:
        name = "enrollment-" + public["operation_id"] + ".json"
        host.atomic_file(private / "jobs" / name, raw, 0o600)
        host.publish(handles["status"], name, raw)
    host.publish(handles["status"], "enrollment.json", raw)


def update(host, private, handles, record, state, **fields):
    record["public"].update(state=state, **fields)
    write_state(host, private, handles, record)


def retire(host, handles, operation):
    fd = handles["requests"]
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        if marker(host, fd, "enroll.request") == operation:
            for name in (operation + ".packet", operation + ".cancel", "enroll.request"):
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
            os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def readback(exchange, helper, record):
    out = private_output(exchange, "target.json")
    if out is None:
        return None
    cfg = helper.cfg
    require(out.keys() == {"registered", "target"} and type(out["registered"]) is bool, "invalid_enrollment_target")
    wire.check_target(out["target"], cfg["project"], cfg["instance"])
    public = record["public"]
    if "target" in public:
        require(public["target"] == out["target"] and public["registered"] == out["registered"], "enrollment_target_changed")
    public.update(target=out["target"], registered=out["registered"])
    receipt = private_output(exchange, "receipt.json")
    if receipt:
        require(out["registered"] or "request" in record, "unrequested_key_import_receipt")
        wire.check_receipt(receipt, out["target"], record.get("request"))
        helper.committed(receipt)
    return receipt


def exchange_keys(host, private, handles, helper, record, exchange):
    """Resume the original helper after host restart. Forward at most the original sealed packet."""
    public = record["public"]
    operation = public["operation_id"]
    reqs = handles["requests"]
    container = record["container"]
    deadline = time.monotonic() + max(0, min(200, record["deadline"] - time.time()))
    inspect_at = 0
    while True:
        receipt = readback(exchange, helper, record)
        if receipt is not None:
            # Success requires the helper's real receipt and a matching committed site binding.
            require(helper.docker.call("wait", container, timeout=10) == "0", "enrollment_helper_exit_mismatch")
            helper.docker.call("rm", container)
            update(host, private, handles, record, "succeeded", receipt=receipt)
            return
        if time.monotonic() >= deadline:
            if "request" in record:
                raise ValueError("enrollment_commit_requires_review")
            finish = "expired"
        elif marker(host, reqs, operation + ".cancel") == operation and "request" not in record:
            finish = "cancelled"
        else:
            finish = None
        if finish is not None:
            host.atomic_file(exchange / "cancel", b"cancel\n", 0o600)
            # If this was already registered, read the actual receipt before classifying as cancelled.
            exit_status = helper.docker.call("wait", container, timeout=12)
            receipt = readback(exchange, helper, record)
            if receipt:
                require(exit_status == "0", "enrollment_helper_exit_mismatch")
                helper.docker.call("rm", container)
                update(host, private, handles, record, "succeeded", receipt=receipt)
                return
            helper.docker.call("rm", container)
            update(host, private, handles, record, finish)
            return
        if public.get("target") and not public["registered"]:
            if public["state"] == "queued":
                update(host, private, handles, record, "awaiting_packet")
            packet = sealed_packet(host, reqs, operation + ".packet")
            if packet is not None:
                req = wire.check_packet(packet, operation, public["target"])
                require(record.get("request", req) == req, "enrollment_request_changed")
                if "request" not in record:
                    record["request"] = req
                    update(host, private, handles, record, "verifying")
                if not (exchange / "packet.json").exists():
                    host.atomic_file(exchange / "packet.json", wire.encode(packet), 0o600)
        # Poll the receipt first. An exited helper cannot be taken as proof of successful enrollment.
        if time.monotonic() >= inspect_at:
            found = helper.docker.inspect(container)
            require(found["id"] == container and found["session"] == operation
                    and found["image"] == helper.image
                    and found["name"] == "/" + helper.name(operation), "enrollment_helper_identity_changed")
            if not found["running"]:
                receipt = readback(exchange, helper, record)
                if receipt is not None and found["status"] == "exited" and found["exit_code"] == 0:
                    helper.docker.call("rm", container)
                    update(host, private, handles, record, "succeeded", receipt=receipt)
                else:
                    # No receipt after a sealed packet may mean a crash after committing the private copy.
                    helper.docker.call("rm", container)
                    terminal = "needs_review" if "request" in record else (
                        "expired" if time.time() >= record["deadline"] - 22 else "failed")
                    update(host, private, handles, record, terminal, code="enrollment_receipt_missing")
                return
            inspect_at = time.monotonic() + 1
        time.sleep(0.25)


def current(host, private, handles, cfg):
    path = private / "enrollment.json"
    if path.exists():
        data = wire.unpack(path.read_bytes())
        require((data["public"]["project"], data["public"]["instance_id"]) == (cfg["project"], cfg["instance"]),
                "enrollment_site_changed")
        sync_public(host, private, handles, data)
        return data
    record = {"public": {"project": cfg["project"], "instance_id": cfg["instance"], "operation_id": None, "state": "idle"}}
    write_state(host, private, handles, record)
    return record


def tick(host, private, handles, cfg, record, archive, key_image):
    public = record["public"]
    if public["state"] == "needs_review":
        return False
    operation = marker(host, handles["requests"], "enroll.request")
    if public["state"] in wire.ACTIVE:
        require(operation == public["operation_id"], "pending_enrollment_marker_changed")
    if operation is None:
        return False
    if public["state"] in wire.TERMINAL and public["operation_id"] == operation:
        retire(host, handles, operation)
        return False
    exchange = private / ("exchange-" + operation)
    fresh = public["state"] not in wire.ACTIVE
    if fresh:
        if (private / "jobs" / ("enrollment-" + operation + ".json")).exists():
            # A stale or directly replayed marker cannot overwrite an older verified receipt.
            retire(host, handles, operation)
            return False
        public = dict(project=cfg["project"], instance_id=cfg["instance"], operation_id=operation, state="queued")
        record.clear()
        record.update(public=public, deadline=time.time() + 200)
        write_state(host, private, handles, record)
    try:
        helper = Helper(archive, cfg, handles, key_image)
        with archive.ops.host_lock(helper.state):
            archive.site.check_certificate_idle(helper.state)
            require(all(row["stage"] in ("finished", "preflight-failed") and not row.get("private_cleanup_pending")
                        for _, row in archive.ops.run_records(helper.state)), "previous_backup_requires_review")
            restore = helper.state / "restore.json"
            require(not os.path.lexists(restore) or archive.ops.read_json(restore)["stage"]
                    in ("preflight", "eligible", "completed", "rejected"), "unfinished_restore_blocks_enrollment")
            if fresh:
                exchange.mkdir(mode=0o700)
                host.sync_dir(private)
                if marker(host, handles["requests"], operation + ".cancel") == operation:
                    update(host, private, handles, record, "cancelled")
                else:
                    record["container"] = helper.start(operation, exchange)
                    write_state(host, private, handles, record)
                    helper.docker.call("start", record["container"])
            if public["state"] in wire.ACTIVE:
                if not record.get("container"):
                    # A crash may leave a named, created helper before Docker returned its ID.
                    # Its state was persisted before start; never start it again or replay the request.
                    helper.retire_original(operation, None)
                    raise ValueError("enrollment_interrupted_before_start")
                exchange_keys(host, private, handles, helper, record, exchange)
    except Exception as exc:
        code = host.machine_code(exc, "enrollment_failed")
        # After private commit may have started, leave the sealed envelope and original receipt for host review.
        terminal = "needs_review" if "container" in record or "request" in record else "failed"
        update(host, private, handles, record, terminal, code=code)
    if public["state"] != "needs_review":
        retire(host, handles, operation)
        # Only unlink known files in this root-private exchange. Keep uncertain evidence private.
        if exchange.is_dir() and not set(p.name for p in exchange.iterdir()) - {"receipt.json", "target.json", "cancel", "packet.json"}:
            for p in exchange.iterdir():
                p.unlink()
            exchange.rmdir()
    return True


def review(host, private, handles, cfg, operation, archive, key_image):
    """Read back committed material via a fresh helper that never receives a sealed envelope."""
    record = current(host, private, handles, cfg)
    value = record["public"]
    require(value["state"] == "needs_review" and value["operation_id"] == operation, "enrollment_review_id_mismatch")
    helper = Helper(archive, cfg, handles, key_image)
    with archive.ops.host_lock(helper.state):
        helper.retire_original(operation, record.get("container"))
        # No browser request is mapped to this root-only directory or this temporary helper.
        with tempfile.TemporaryDirectory(prefix=".enrollment-review-", dir=private) as scratch:
            exchange = Path(scratch)
            review_op = uuid.uuid4().hex
            container = helper.start(review_op, exchange)
            helper.docker.call("start", container)
            try:
                deadline = time.monotonic() + 20
                out = private_output(exchange, "target.json")
                while out is None and time.monotonic() < deadline:
                    time.sleep(0.25)
                    out = private_output(exchange, "target.json")
                require(out and type(out["registered"]) is bool, "enrollment_review_target_unavailable")
                wire.check_target(out["target"], cfg["project"], cfg["instance"])
                if not out["registered"]:
                    host.atomic_file(exchange / "cancel", b"cancel\n", 0o600)
                exit_status = helper.docker.call("wait", container, timeout=15)
                proof = private_output(exchange, "receipt.json")
                if out["registered"]:
                    require(exit_status == "0" and proof is not None, "enrollment_review_receipt_missing")
                    wire.check_receipt(proof, out["target"], record.get("request"))
                    helper.committed(proof)
                    value.setdefault("target", out["target"])
                    value["registered"] = "request" not in record
                    update(host, private, handles, record, "succeeded", code="enrollment_commit_verified", receipt=proof)
                else:
                    require(proof is None and not os.path.lexists(helper.state / "site.json"), "enrollment_review_inconsistent")
                    update(host, private, handles, record, "failed", code="enrollment_not_committed")
            finally:
                helper.retire_original(review_op, container)
        retire(host, handles, operation)
    return record["public"]
