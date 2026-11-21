"""Persistent offline backup session; Docker control remains with the host.

The private shared maintenance lease and durable session journal span host stop,
backup, real decryption verification, and host service restoration. Only public
JSON crosses stdin/stdout. A lost host/worker never erases unfinished evidence.
"""

import argparse
import importlib.util
import json
import os
import select
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

MAINTENANCE = Path("/opt/gift-ledger-gui-v2/maintenance.py")
MAX_COMMAND = 65536
# The host publishes and checks API/Web health before issuing finish. Keep the
# lease longer than both 120-second health windows plus bounded publication.
COMMAND_TIMEOUT = 900


class BackupWorkerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise BackupWorkerError(code)


def load_maintenance():
    spec = importlib.util.spec_from_file_location(
        "backup_session_maintenance", MAINTENANCE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_resource_meter():
    spec = importlib.util.spec_from_file_location(
        "backup_session_resource_meter", Path(__file__).with_name("resource_meter.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ResourceMeter()


def parse_command(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_COMMAND, "command_too_large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "command_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise BackupWorkerError("command_invalid_json") from None
    require(
        isinstance(value, dict)
        and set(value) == {"command"}
        and isinstance(value["command"], str)
        and value["command"] in {"run", "finish"},
        "command_invalid",
    )
    return value["command"]


class CommandReader:
    def __init__(
        self,
        descriptor,
        *,
        timeout=COMMAND_TIMEOUT,
        waiter=None,
        reader=None,
        clock=None,
    ):
        self.descriptor = descriptor
        self.timeout = timeout
        self.waiter = waiter or select.select
        self.reader = reader or os.read
        self.clock = clock or time.monotonic
        self.pending = b""

    def read(self):
        deadline = self.clock() + self.timeout
        while b"\n" not in self.pending:
            require(len(self.pending) <= MAX_COMMAND, "command_too_large")
            remaining = deadline - self.clock()
            require(remaining > 0, "host_command_timeout")
            ready, _, _ = self.waiter([self.descriptor], [], [], remaining)
            require(bool(ready), "host_command_timeout")
            chunk = self.reader(
                self.descriptor, min(4096, MAX_COMMAND + 1 - len(self.pending))
            )
            require(bool(chunk), "host_disconnected_preserve_session")
            self.pending += chunk
        raw, self.pending = self.pending.split(b"\n", 1)
        return parse_command(raw)


class _HistoryWithoutSession:
    def __init__(self, control, entries, own):
        self.entries, self.own = entries, own
        # Certificate history validates a reconciled probe against the original
        # site's private identity. Keep that real root when filtering our lease.
        self.parent = control.parent

    def iterdir(self):
        return (entry for entry in self.entries if entry != self.own)


class _PreviewGuard:
    def __init__(self, module, snapshots, app_data):
        self.module, self.snapshots = module, tuple(snapshots)
        self.directories = (app_data / "imports", app_data / "personal-restores")
        require(
            {snapshot.directory for snapshot in self.snapshots}
            == set(self.directories),
            "preview_snapshot_roots_invalid",
        )

    def verify(self):
        for snapshot in self.snapshots:
            require(
                self.module.validate_snapshot(snapshot) is True,
                "preview_snapshot_changed",
            )
        return True


class Session:
    def __init__(
        self,
        module,
        args,
        *,
        history_checker=None,
        lease_opener=None,
        tmp_parent=Path("/tmp"),
    ):
        self.m = module
        self.args = args
        self.history_checker = history_checker
        self.lease_opener = lease_opener
        self.tmp_parent = Path(tmp_parent)
        self.lease = None
        self.temporary = None
        self.started = None
        self.completed = None
        self.verified_result = None
        self.context = None
        self.preview_tool = None
        self.preview_summary = {}
        self.preview_snapshots = []
        self.quarantine_counts = {}
        self.operation = module.CONTROL / args.session

    def _options(self):
        return {
            "recipient": self.args.recipient,
            "backup_operation_id": self.args.backup_id,
            "verify_operation_id": self.args.verify_id,
            "preview_policy": getattr(self.args, "preview_policy", "fixed-window"),
        }

    def _fixed_window(self):
        return self._options()["preview_policy"] in {
            "fixed-window",
            "quarantine-expired",
        }

    def _preview(self):
        if self.preview_tool is None:
            path = Path(__file__).with_name("preview_policy.py")
            spec = importlib.util.spec_from_file_location(
                "backup_worker_preview_policy", path
            )
            self.preview_tool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.preview_tool)
        return self.preview_tool

    def _scan_previews(self, *, allow_current_work=False):
        policy = self._options()["preview_policy"]
        require(
            policy in {"block", "quarantine-expired", "fixed-window"},
            "preview_policy_invalid",
        )
        now = datetime.now(timezone.utc)
        reports, expired = {}, 0
        snapshots = []
        for kind in ("imports", "personal-restores"):
            if self._fixed_window() and not allow_current_work:
                try:
                    snapshot = self._preview().stable_snapshot(
                        self.m.APP_DATA / kind, now, 30, kind=kind
                    )
                except self._preview().PreviewPolicyError:
                    raise BackupWorkerError("preview-hard-block") from None
                snapshots.append(snapshot)
                report = snapshot.summary()
            else:
                report = self._preview().scan(
                    self.m.APP_DATA / kind, now, 30, kind=kind
                )
            reports[kind] = {
                "state": report["state"],
                "counts": report["counts"],
                "blocked": report.get("blocked", False),
                "reasons": report.get("reasons", []),
            }
            expired += report["counts"]["expired_pending_pairs"]
        self.preview_summary = reports
        self.preview_snapshots = snapshots
        if not allow_current_work:
            blocked = [report for report in reports.values() if report["blocked"]]
            waiting_reasons = {
                "processing-preview-present",
                "unexpired-pending-preview",
            }
            only_waiting = bool(blocked) and all(
                set(report["reasons"]) <= waiting_reasons
                and not any(
                    report["counts"][key]
                    for key in (
                        "unknown_members",
                        "invalid_metadata",
                        "unsafe_members",
                        "orphan_groups",
                    )
                )
                for report in blocked
            )
            require(not blocked or only_waiting, "preview-hard-block")
            require(not only_waiting, "preview-active-deferred")
            require(
                expired == 0 or self._fixed_window(),
                "expired_preview_requires_quarantine_policy",
            )
        return expired

    def _metadata_status(self):
        context = self._context()
        engine = self.m.engine_for(context)
        try:
            with engine.connect() as connection:
                flags = self.m.database_guard(connection, context)
            self.m.verify_schema(engine, context)
            fingerprint = self.m.fingerprint(engine, context)
            return {
                "format": "gift-ledger-backup-preflight-v1",
                "version": 1,
                "success": True,
                "project": self.args.project,
                "instance_id": self.args.instance_id,
                "server_uuid": self.args.server_uuid,
                "schema_revision": fingerprint["schema_revision"],
                "fingerprint": fingerprint,
                "app_files": None,
                "readonly": flags,
                "application_files_pending_quarantine": True,
                "applications_stopped_verified": False,
            }
        finally:
            engine.dispose()

    def _ready_status(self):
        expired = self._scan_previews(allow_current_work=self.completed is not None)
        if expired or any(
            report["counts"]["members"] for report in self.preview_summary.values()
        ):
            return self._metadata_status()
        return self.m.inspect_site(self.args.project)

    def _quarantine_previews(self):
        expired = self._scan_previews()
        if not expired:
            return
        require(self._fixed_window(), "preview_policy_invalid")
        self._database_safe(stopped=True)
        parent = self.operation / "private-quarantine-sets"
        if not parent.exists():
            parent.mkdir(mode=0o700)
        self.m.directory(parent, private=True)
        for kind, report in self.preview_summary.items():
            if not report["counts"]["expired_pending_pairs"]:
                continue
            destination = parent / kind
            require(
                not destination.exists(), "preview_quarantine_requires_manual_recovery"
            )
            destination.mkdir(mode=0o700)
            self._preview().quarantine_expired(
                self.m.APP_DATA / kind,
                destination,
                datetime.now(timezone.utc),
                30,
                kind=kind,
                applications_stopped=True,
                db_guard_verified=True,
                allow_preserved_work=True,
            )
        self._scan_previews()
        require(
            not any(
                report["counts"]["expired_pending_pairs"]
                for report in self.preview_summary.values()
            ),
            "preview_quarantine_not_complete",
        )
        self._check_quarantine_history()

    def _check_quarantine_history(self):
        parent = self.operation / "private-quarantine-sets"
        if not parent.exists():
            return
        self.m.directory(parent, private=True)
        for entry in parent.iterdir():
            require(
                entry.name in {"imports", "personal-restores"},
                "preview_quarantine_unknown_member",
            )
            self.m.directory(entry, private=True)
            quarantine = entry / "private-quarantine"
            self.m.directory(quarantine, private=True)
            receipt = self.m.json_file(quarantine / "QUARANTINE.json", private=True)
            status = receipt.get("status")
            require(
                receipt.get("format") == "gift-ledger-preview-quarantine-v1"
                and receipt.get("kind") == entry.name
                and status in {"quarantined", "purging", "purge-failed", "purged"}
                and receipt.get("readback_verified") is True
                and receipt.get("source_removal_completed") is True,
                "preview_quarantine_requires_manual_recovery",
            )
            if status != "quarantined":
                completed = self.m.completed_operation(
                    self.args.session,
                    self.context,
                    expected_action="automation-session",
                )
                require(
                    completed.get("details", {}).get("outcome") == "verified",
                    "preview_purge_without_successful_session",
                )
                if status == "purged":
                    require(
                        {item.name for item in quarantine.iterdir()}
                        == {"QUARANTINE.json"},
                        "preview_purged_tree_changed",
                    )
            counts = {
                key: receipt.get(key)
                for key in ("candidate_pairs", "copied_files", "removed_source_files")
            }
            require(
                all(type(value) is int and value >= 0 for value in counts.values()),
                "preview_quarantine_receipt_invalid",
            )
            self.quarantine_counts[entry.name] = counts

    def _context(self):
        context = self.m.local_context(self.args.project)
        require(
            context["identity"]["instance_id"] == self.args.instance_id
            and context["server_uuid"] == self.args.server_uuid,
            "session_site_identity_mismatch",
        )
        if self.started is not None:
            require(
                context["layout"]["layout_sha256"] == self.started["layout_sha256"]
                and context["legacy"].ASSETS_MANIFEST_SHA256
                == self.started["assets_sha256"],
                "session_configuration_changed",
            )
        return context

    def _check_keys(self):
        m = self.m
        folder = m.IDENTITY.parent
        m.directory(folder, private=True)
        require(
            not (folder / "import-failed.json").exists(), "backup_key_import_failed"
        )
        receipt = m.json_file(folder / "identity-imported.json", private=True)
        key_tool = m.load_module(
            "backup_session_key_receipt", Path(m.__file__).with_name("key_transfer.py")
        )
        key_tool.validate_receipt(receipt, self.args.project, self.args.instance_id)
        require(
            receipt["backup_recipient"] == self.args.recipient,
            "backup_recipient_not_imported",
        )
        require(
            receipt["started_sha256"] == m.file_sha(folder / "import-started.json")
            and receipt["identity_sha256"] == m.file_sha(m.IDENTITY),
            "backup_key_import_changed",
        )

    def _history(self):
        self._check_quarantine_history()
        self.m.require(
            not (self.m.CONTROL / "active.json").exists(),
            "previous_operation_incomplete_or_failed",
        )
        own = self.operation if self.args.recover or self.started is not None else None
        if self.history_checker is not None:
            self.history_checker(own)
            return
        renewal = self.m.load_module(
            "backup_session_certificate_history",
            Path(self.m.__file__).with_name("certificate_renewal.py"),
        )
        _, prepare, _ = renewal.tools()
        entries = self.m.history_entries(self.m.CONTROL)
        if self.context["identity"].get("tls", {}).get("profile") == "site-ca-v1":
            # local_context authenticated the profile. The site's signing key
            # is private configuration, not a backup session or renewal journal.
            issuer = self.m.CONTROL / "site-ca"
            require(issuer in entries, "backup_site_ca_directory_missing")
            prepare._metadata(issuer, 0o700, directory=True)
            key = issuer / "issuing-key.pem"
            require(self.m.history_entries(issuer) == [key], "backup_site_ca_members_invalid")
            prepare._metadata(key, 0o600)
            entries.remove(issuer)
        control = _HistoryWithoutSession(self.m.CONTROL, entries, own)
        renewal.check_history(prepare, control)
        # Existing non-session receipts must be genuinely bound, not just present.
        for entry in entries:
            if self.m.OPERATION.fullmatch(entry.name) and entry != self.operation:
                started = self.m.json_file(entry / "started.json", private=True)
                if started.get("format") == "gift-ledger-isolated-probe-request-v1":
                    # local_context has already authenticated the generated
                    # TARGET and installed its pinned module search path.
                    import target
                    import isolated_probe_worker
                    contract = target.runtime_target(self.args.project)
                    if (entry / "RECONCILED.json").exists():
                        isolated_probe_worker.reconciled_history(self.m, self.context, entry, contract)
                    else:
                        isolated_probe_worker.completed_history(self.m, self.context, entry, contract)
                else:
                    self.m.completed_operation(entry.name, self.context)
            elif entry.name.startswith("certificate-"):
                for filename in (
                    "started.json",
                    "completed.json",
                    "rollback-completed.json",
                    "aborted.json",
                ):
                    path = entry / filename
                    if path.exists():
                        receipt = self.m.json_file(path, private=True)
                        require(
                            receipt.get("project") == self.args.project
                            and receipt.get("instance_id") == self.args.instance_id,
                            "certificate_history_identity_mismatch",
                        )

    def _open_lease(self):
        if self.lease_opener is not None:
            return self.lease_opener()
        import fcntl

        path = self.m.CONTROL / "operation.lock"
        self.m.no_links(path)
        if not path.exists():
            try:
                self.m.exclusive_bytes(path, b"")
            except FileExistsError:
                pass
        self.m.read_file(path, mode=0o600, private=True)
        before = path.lstat()
        descriptor = os.open(str(path), os.O_RDWR | os.O_NOFOLLOW)
        lease = os.fdopen(descriptor, "r+b")
        try:
            info = os.fstat(lease.fileno())
            require(
                (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino)
                and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and (info.st_uid, info.st_gid) == (0, 0),
                "session_lease_invalid",
            )
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupWorkerError("maintenance_operation_busy") from None
            return lease
        except BaseException:
            lease.close()
            raise

    def _journal(self, name, value):
        target = self.operation / name
        require(not target.exists(), "session_journal_already_exists")
        self.m.directory(self.operation, private=True)
        temporary = self.operation / (".session-record-" + uuid4().hex)
        self.m.exclusive_json(temporary, value)
        require(not target.exists(), "session_journal_already_exists")
        os.rename(str(temporary), str(target))
        if os.name != "nt":
            descriptor = os.open(str(self.operation), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _check_started(self, value):
        expected = {
            "format": self.m.FORMAT,
            "version": 1,
            "project": self.args.project,
            "action": "automation-session",
            "operation_id": self.args.session,
            "instance_id": self.args.instance_id,
            "server_uuid": self.args.server_uuid,
            "request_sha256": self.m.sha(self.m.json_bytes(self._options())),
            "assets_sha256": self.context["legacy"].ASSETS_MANIFEST_SHA256,
            "layout_sha256": self.context["layout"]["layout_sha256"],
            "options": self._options(),
            "status": "started",
            "success": False,
        }
        require(
            all(value.get(key) == wanted for key, wanted in expected.items()),
            "session_journal_identity_mismatch",
        )
        flags = value.get("readonly_before")
        require(
            isinstance(flags, dict)
            and set(flags) == {"read_only", "super_read_only"}
            and all(type(flag) is bool for flag in flags.values()),
            "session_initial_flags_invalid",
        )
        require(
            not (self.operation / "failed.json").exists(),
            "session_failed_preserve_evidence",
        )

    def _start_session(self, initial_status, *, recovered_before_start=False):
        m, args = self.m, self.args
        require(
            not any(
                os.path.lexists(str(m.CONTROL / operation))
                for operation in (args.session, args.backup_id, args.verify_id)
            ),
            "recovery_session_missing_with_core_history"
            if recovered_before_start
            else "session_operation_already_exists",
        )
        self.operation.mkdir(mode=0o700)
        self.started = {
            "format": m.FORMAT,
            "version": 1,
            "project": args.project,
            "action": "automation-session",
            "operation_id": args.session,
            "instance_id": args.instance_id,
            "server_uuid": args.server_uuid,
            "request_sha256": m.sha(m.json_bytes(self._options())),
            "assets_sha256": self.context["legacy"].ASSETS_MANIFEST_SHA256,
            "layout_sha256": self.context["layout"]["layout_sha256"],
            "options": self._options(),
            "started_at": m.utc_now(),
            "status": "started",
            "success": False,
            "readonly_before": initial_status["readonly"],
        }
        if recovered_before_start:
            # This only closes the original host journal. It never authorizes
            # new core work or treats a vanished private operation as success.
            self.started["recovered_before_session_start"] = True
        self._journal("started.json", self.started)

    def prepare(self):
        m, args = self.m, self.args
        m.allowed_project(args.project)
        require(
            all(
                isinstance(value, str) and m.OPERATION.fullmatch(value)
                for value in (args.session, args.backup_id, args.verify_id)
            )
            and len({args.session, args.backup_id, args.verify_id}) == 3,
            "session_operation_ids_invalid",
        )
        require(
            m.canonical_uuid(args.instance_id) and m.canonical_uuid(args.server_uuid),
            "session_uuid_invalid",
        )
        require(
            isinstance(args.recipient, str) and m.RECIPIENT.fullmatch(args.recipient),
            "session_recipient_invalid",
        )
        self.context = self._context()
        self._check_keys()
        if not args.recover:
            self._ready_status()
            self.m.require_tmpfs(self.tmp_parent)
            self.m.backup_capacity(self.tmp_parent, self.context)
        self._history()
        self.lease = self._open_lease()
        self.context = self._context()
        self._check_keys()
        self._history()
        if args.recover:
            if not os.path.lexists(str(self.operation)):
                initial_status = self._ready_status()
                require(
                    initial_status.get("readonly")
                    == {"read_only": False, "super_read_only": False},
                    "database_not_in_normal_mode",
                )
                self._start_session(initial_status, recovered_before_start=True)
            else:
                m.directory(self.operation, private=True)
                self.started = m.json_file(
                    self.operation / "started.json", private=True
                )
                self._check_started(self.started)
                if (self.operation / "completed.json").exists():
                    self.completed = m.completed_operation(
                        args.session, self.context, expected_action="automation-session"
                    )
                initial_status = self._ready_status()
        else:
            initial_status = self._ready_status()
            self._start_session(initial_status)
        return {
            "event": "ready",
            "session_id": args.session,
            "status": initial_status,
            "recovery": bool(args.recover),
            "session_completed": self.completed is not None,
            "preview_policy": self._options()["preview_policy"],
            "preview_counts": self.preview_summary,
            "quarantine_counts": self.quarantine_counts,
        }

    def _database_safe(self, stopped=False):
        context = self._context()
        engine = self.m.engine_for(context)
        try:
            with engine.connect() as connection:
                flags = self.m.database_guard(connection, context)
                if stopped:
                    self.m.no_application_sessions(connection)
                    transactions = connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM information_schema.innodb_trx "
                        "WHERE trx_mysql_thread_id <> CONNECTION_ID()"
                    ).scalar_one()
                    require(
                        type(transactions) is int and transactions == 0,
                        "database_transactions_not_drained",
                    )
            require(
                flags == self.started["readonly_before"], "database_flags_not_restored"
            )
            return flags
        finally:
            engine.dispose()

    def _drain_database(self):
        seconds = getattr(self.args, "drain_timeout_seconds", 60)
        require(
            type(seconds) is int and 1 <= seconds <= 300,
            "database_drain_timeout_invalid",
        )
        deadline = time.monotonic() + seconds
        while True:
            try:
                return self._database_safe(stopped=True)
            except Exception as error:
                if getattr(error, "code", None) not in {
                    "application_sessions_present_stop_gui",
                    "database_transactions_not_drained",
                }:
                    raise
                remaining = deadline - time.monotonic()
                require(remaining > 0, "database_drain_timeout")
                time.sleep(min(0.25, remaining))

    def _request_workspace(self):
        if self.temporary is None:
            self.m.require_tmpfs(self.tmp_parent)
            self.temporary = tempfile.TemporaryDirectory(
                prefix="backup-session-", dir=str(self.tmp_parent)
            )
            os.chmod(self.temporary.name, 0o700)
            self.m.REQUEST = Path(self.temporary.name) / "request.json"

    def _request(self, action, operation_id, status, options):
        self._request_workspace()
        request = {
            "format": "gift-ledger-gui-v2-request",
            "version": 1,
            "action": action,
            "operation_id": operation_id,
            "project": self.args.project,
            "instance_id": self.args.instance_id,
            "server_uuid": self.args.server_uuid,
            "schema_revision": status["schema_revision"],
            "options": options,
        }
        self.m.valid_request(request, self.args.project, action)
        target = self.m.REQUEST
        self.m.no_links(target)
        temporary = target.parent / (".request-" + operation_id)
        self.m.exclusive_json(temporary, request)
        os.replace(str(temporary), str(target))

    def _execute(self, action, operation_id):
        self.m.execute(
            self.args.project, action, _lease=self.lease, _session_id=self.args.session
        )
        return self.m.completed_operation(
            operation_id, self.context, expected_action=action
        )

    def _copy_ciphertext(self, backup):
        self._request_workspace()
        details = backup["details"]
        expected = "backup-" + self.args.backup_id + ".glgui.age"
        require(
            details["ciphertext"] == expected, "backup_ciphertext_identity_mismatch"
        )
        source, target = self.m.EXCHANGE / expected, self.m.REQUEST.parent / expected
        require(
            self.m.file_sha(source) == details["ciphertext_sha256"],
            "backup_ciphertext_hash_mismatch",
        )
        if target.exists():
            require(
                self.m.file_sha(target) == details["ciphertext_sha256"],
                "request_ciphertext_changed",
            )
            return
        self.m.no_links(target)
        with source.open("rb") as origin, target.open("xb") as destination:
            os.chmod(str(target), 0o600)
            shutil.copyfileobj(origin, destination, 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        require(
            self.m.file_sha(target) == details["ciphertext_sha256"],
            "request_ciphertext_changed",
        )

    def _check_pair(self, backup, verification, status):
        before, after = backup["details"], verification["details"]
        require(
            after.get("backup_operation_id") == self.args.backup_id
            and verification["operation_id"] == self.args.verify_id
            and after.get("encryption_roundtrip_verified") is True
            and after.get("reliable_backup_verified") is True
            and before.get("readonly_restored") is True
            and after.get("readonly_restored") is True,
            "session_backup_verification_invalid",
        )
        for key in (
            "ciphertext",
            "ciphertext_sha256",
            "archive_sha256",
            "fingerprint",
            "app_files",
            "source",
        ):
            require(before.get(key) == after.get(key), "session_backup_pair_mismatch")
        require(
            before["source"] == self.m.source_metadata(self.context)
            and before["fingerprint"] == status["fingerprint"]
            and before["app_files"] == status["app_files"],
            "session_backup_source_changed",
        )

    def run(self):
        require(
            self.lease is not None and self.started is not None, "session_not_prepared"
        )
        require(self.verified_result is None, "session_run_already_received")
        self._history()
        backup_path = self.m.CONTROL / self.args.backup_id
        verify_path = self.m.CONTROL / self.args.verify_id
        if self.started.get("recovered_before_session_start"):
            require(
                not os.path.lexists(str(backup_path))
                and not os.path.lexists(str(verify_path)),
                "recovered_prestart_session_must_abort",
            )
        abort_before_backup = self.args.recover and not backup_path.exists()
        if self.completed is None and not abort_before_backup:
            self._drain_database()
        else:
            self._database_safe(stopped=False)
        if self.completed is not None:
            status = self._ready_status()
            self.verified_result = {
                "event": "verified",
                "session_id": self.args.session,
                "outcome": "already-finished",
                "safe_to_restore": True,
                "backup": None,
                "verify": None,
                "status": status,
                "quarantine_counts": self.quarantine_counts,
            }
            return self.verified_result
        if abort_before_backup:
            require(not verify_path.exists(), "verify_without_backup_preserve_evidence")
            self.verified_result = {
                "event": "verified",
                "session_id": self.args.session,
                "outcome": "aborted-before-backup",
                "safe_to_restore": True,
                "backup": None,
                "verify": None,
                "status": self._ready_status(),
                "quarantine_counts": self.quarantine_counts,
            }
            return self.verified_result
        try:
            self._quarantine_previews()
        except BackupWorkerError as error:
            if (
                error.code != "preview-active-deferred"
                or backup_path.exists()
                or verify_path.exists()
            ):
                raise
            self._database_safe(stopped=True)
            self.verified_result = {
                "event": "verified",
                "session_id": self.args.session,
                "outcome": "deferred-active-preview",
                "safe_to_restore": True,
                "backup": None,
                "verify": None,
                "status": self._metadata_status(),
                "preview_counts": self.preview_summary,
                "quarantine_counts": self.quarantine_counts,
            }
            return self.verified_result
        if self._fixed_window():
            guard = _PreviewGuard(
                self._preview(), self.preview_snapshots, self.m.APP_DATA
            )
            with self.m.automation_preview_scope(
                self.context, self.lease, self.args.session, guard
            ):
                return self._run_backup_sequence(backup_path, verify_path)
        return self._run_backup_sequence(backup_path, verify_path)

    def _run_backup_sequence(self, backup_path, verify_path):
        status = self.m.inspect_site(self.args.project)
        if backup_path.exists():
            require(self.args.recover, "backup_operation_already_exists")
            backup = self.m.completed_operation(
                self.args.backup_id, self.context, expected_action="backup"
            )
            require(
                backup["details"]["fingerprint"] == status["fingerprint"]
                and backup["details"]["app_files"] == status["app_files"],
                "recovery_source_changed",
            )
        else:
            self._request(
                "backup",
                self.args.backup_id,
                status,
                {"recipient": self.args.recipient, "backup_type": "manual"},
            )
            backup = self._execute("backup", self.args.backup_id)
        self._copy_ciphertext(backup)
        if verify_path.exists():
            require(self.args.recover, "verify_operation_already_exists")
            verification = self.m.completed_operation(
                self.args.verify_id, self.context, expected_action="verify"
            )
        else:
            self._request(
                "verify",
                self.args.verify_id,
                status,
                {
                    "ciphertext": backup["details"]["ciphertext"],
                    "sha256": backup["details"]["ciphertext_sha256"],
                },
            )
            verification = self._execute("verify", self.args.verify_id)
        self._database_safe(stopped=True)
        status = self.m.inspect_site(self.args.project)
        self._check_pair(backup, verification, status)
        self.verified_result = {
            "event": "verified",
            "session_id": self.args.session,
            "outcome": "verified",
            "safe_to_restore": True,
            "backup": self.m.public_receipt(backup),
            "verify": self.m.public_receipt(verification),
            "status": status,
            "quarantine_counts": self.quarantine_counts,
        }
        return self.verified_result

    def finish(self):
        require(
            self.verified_result is not None, "finish_before_verification_forbidden"
        )
        self._history()
        self._database_safe(stopped=False)
        if self.completed is None:
            self.completed = {
                **self.started,
                "status": "completed",
                "success": True,
                "completed_at": self.m.utc_now(),
                "started_sha256": self.m.file_sha(self.operation / "started.json"),
                "details": {
                    "outcome": self.verified_result["outcome"],
                    "backup_operation_id": self.args.backup_id,
                    "verify_operation_id": self.args.verify_id,
                    "quarantine_counts": self.quarantine_counts,
                    "readonly_restored": True,
                    "host_reported_services_restored": True,
                },
            }
            self._journal("completed.json", self.completed)
        cleanup = self._cleanup_private_previews()
        return {
            "event": "finished",
            "session_id": self.args.session,
            "outcome": self.completed["details"]["outcome"],
            "receipt": self.m.public_receipt(self.completed),
            **cleanup,
        }

    def _cleanup_private_previews(self):
        result = {
            "private_cleanup_pending": False,
            "private_cleanup_counts": {},
            "private_cleanup_errors": [],
        }
        if not self.quarantine_counts:
            return result
        if self.completed["details"]["outcome"] != "verified":
            result.update(
                {
                    "private_cleanup_pending": True,
                    "private_cleanup_errors": ["cleanup-requires-verified-backup"],
                }
            )
            return result
        try:
            backup = self.m.completed_operation(
                self.args.backup_id, self.context, expected_action="backup"
            )
            verification = self.m.completed_operation(
                self.args.verify_id, self.context, expected_action="verify"
            )
            for kind in self.quarantine_counts:
                receipt = self._preview().purge_quarantine(
                    self.operation / "private-quarantine-sets" / kind,
                    datetime.now(timezone.utc),
                    session_receipt=self.completed,
                    backup_receipt=backup,
                    verify_receipt=verification,
                    retention_days=0,
                )
                result["private_cleanup_counts"][kind] = {
                    "purged_files": receipt["purged_files"]
                }
        except Exception as error:  # noqa: BLE001 -- cleanup warnings cannot undo an already committed success
            code = getattr(error, "code", "private-cleanup-pending")
            if (
                not isinstance(code, str)
                or len(code) > 100
                or not code.replace("_", "").replace("-", "").isalnum()
            ):
                code = "private-cleanup-pending"
            result["private_cleanup_pending"] = True
            result["private_cleanup_errors"].append(code)
        name = (
            "private-cleanup-pending.json"
            if result["private_cleanup_pending"]
            else "private-cleanup-completed.json"
        )
        try:
            if not (self.operation / name).exists():
                self._journal(
                    name,
                    {
                        "format": "gift-ledger-backup-private-cleanup-v1",
                        "operation_id": self.args.session,
                        "project": self.args.project,
                        "recorded_at": self.m.utc_now(),
                        **result,
                    },
                )
        except Exception:  # noqa: BLE001 -- never expose raw private filesystem errors
            # Cleanup warnings cannot undo a verified backup or stop restored services.
            result["private_cleanup_pending"] = True
            result["private_cleanup_errors"].append("private-cleanup-receipt-pending")
        return result

    def interrupted(self, code):
        if self.started is None or self.completed is not None or self.lease is None:
            return
        try:
            path = self.operation / "interrupted.json"
            if not path.exists():
                self._journal(
                    "interrupted.json",
                    {
                        "format": self.m.FORMAT,
                        "version": 1,
                        "operation_id": self.args.session,
                        "action": "automation-session",
                        "project": self.args.project,
                        "instance_id": self.args.instance_id,
                        "server_uuid": self.args.server_uuid,
                        "status": "interrupted",
                        "success": False,
                        "error_code": code,
                        "recorded_at": self.m.utc_now(),
                    },
                )
        except Exception:  # noqa: BLE001, S110 -- durable started marker remains authoritative
            pass  # The durable started marker still blocks other maintenance.

    def close(self):
        try:
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None
        finally:
            if self.lease is not None:
                self.lease.close()
                self.lease = None


def serve(session, reader, emit):
    meter = None

    def with_resources(event):
        event = dict(event)
        try:
            event["resources"] = (
                meter.snapshot()
                if meter is not None
                else {"available": False, "error_code": "resource_meter_unavailable"}
            )
        except Exception:  # noqa: BLE001 -- optional numeric telemetry must never fail maintenance
            event["resources"] = {
                "available": False,
                "error_code": "resource_meter_unavailable",
            }
        return event

    try:
        try:
            meter = make_resource_meter()
            meter.start()
        except Exception:  # noqa: BLE001 -- missing telemetry cannot prevent a safe transaction
            meter = None
        emit(session.prepare())
        require(reader.read() == "run", "run_command_required")
        emit(with_resources(session.run()))
        require(reader.read() == "finish", "finish_command_required")
        emit(with_resources(session.finish()))
        return 0
    except BaseException as error:  # noqa: BLE001 -- all exits preserve journals and disclose fixed codes only
        code = getattr(error, "code", "backup_worker_failed_preserve_evidence")
        if (
            not isinstance(code, str)
            or len(code) > 100
            or not code.replace("_", "").replace("-", "").isalnum()
        ):
            code = "backup_worker_failed_preserve_evidence"
        session.interrupted(code)
        emit(
            {
                "event": "error",
                "session_id": session.args.session,
                "error_code": code,
                "safe_to_restore": False,
                "session_started": session.started is not None
                or session.operation.exists(),
                "preview_counts": session.preview_summary,
            }
        )
        return 2
    finally:
        try:
            session.close()
        finally:
            if meter is not None:
                try:
                    meter.stop()
                except Exception:  # noqa: BLE001, S110 -- never disclose monitoring errors or mask safety cleanup
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="私有维护会话；只输出公开状态，不控制Docker"
    )
    for name in (
        "project",
        "session",
        "instance-id",
        "server-uuid",
        "recipient",
        "backup-id",
        "verify-id",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--recover", action="store_true")
    parser.add_argument(
        "--preview-policy",
        choices=("block", "quarantine-expired", "fixed-window"),
        default="fixed-window",
    )
    parser.add_argument("--drain-timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)

    def emit(value):
        print(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            flush=True,
        )

    try:
        session = Session(load_maintenance(), args)
        return serve(session, CommandReader(sys.stdin.fileno()), emit)
    except Exception:  # noqa: BLE001 -- startup diagnostics must not expose secret-bearing exceptions
        emit(
            {
                "event": "error",
                "session_id": args.session,
                "error_code": "backup_worker_start_failed",
                "safe_to_restore": False,
                "session_started": False,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
