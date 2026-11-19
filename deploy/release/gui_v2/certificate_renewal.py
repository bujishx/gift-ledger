"""Offline, journalled renewal of a site's HTTPS certificate.

The owner stops Web in DSM before running this one-shot utility. No Docker API,
network, database, external certificate, forced unlock or scheduler is provided.
Old material is verified with the unchanged preparation validators at the old
certificate's midpoint; current-time validation remains mandatory for new TLS.
The private transaction keeps original bytes, including derived identity hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

ROOT = Path("/work/site")
PATHS = {"root": ROOT, "app_secrets": Path("/work/app-secrets"),
         "tls": Path("/work/tls"), "db_secrets": Path("/work/db-secrets")}
FORMAT = "gift-ledger-gui-v2-certificate-renewal-v1"
OPERATION = re.compile(r"[0-9a-f]{32}")
TLS_NAMES = ("fullchain.pem", "privkey.pem", "ca-chain.pem")
RECORDS = ("site/control/identity.json", "site/control/prepare-completed.json",
           "site/control/initialize-started.json", "site/control/initialize-completed.json",
           "layout/layout.json", "layout/prepare-completed.json")


class CertificateRenewalError(RuntimeError):
    """Only fixed, nonsecret reason codes may leave the process."""


def require(condition, code):
    if not condition:
        raise CertificateRenewalError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(name, source):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tools():
    layout = load("certificate_layout", Path(__file__).with_name("prepare_volumes.py"))
    prepare = layout.legacy()
    initialize = load("certificate_initialize", layout.LEGACY_PREPARE.with_name("initialize.py"))
    return layout, prepare, initialize


def targets(paths):
    result = {"root/" + name: (paths["root"] / name, 0o600) for name in RECORDS}
    for name in TLS_NAMES:
        mode = 0o600 if name == "privkey.pem" else 0o644
        result["root/site/certs/" + name] = (paths["root"] / "site/certs" / name, mode)
        result["tls/" + name] = (paths["tls"] / name, mode)
    return result


def read_json(module, path):
    return module._record(path)[0]


def publish_record(module, path, value):
    """Publish complete JSON under the already held shared maintenance lease.

    Unlike a link/unlink publication, rename cannot leave a two-link journal on
    power loss. The single-writer lease and private directory exclude competing
    publishers; preexisting journals are always refused. A crash can leave only
    a harmless private pending record, never a partially published commit.
    """
    require(not os.path.lexists(path), "certificate_journal_already_exists")
    temporary_parent = path.parent
    if path.name == "active.json":
        # Keep an interrupted publication inside its exact transaction. An
        # unknown member in the shared maintenance root must stay a stop reason.
        require(OPERATION.fullmatch(value.get("operation_id", "")),
                "certificate_operation_id_invalid")
        temporary_parent = path.parent / ("certificate-" + value["operation_id"])
        module._metadata(temporary_parent, 0o700, directory=True)
    temporary = temporary_parent / (".certificate-record-" + uuid.uuid4().hex)
    module._new_file(temporary, module._json_bytes(value), 0o600)
    require(not os.path.lexists(path), "certificate_journal_already_exists")
    os.rename(temporary, path)
    module._metadata(path, 0o600)
    if temporary_parent != path.parent:
        module._sync_directory(temporary_parent)
    module._sync_directory(path.parent)


def validate_receipt(module, operation, base, filename, manifest_sha=None):
    value = read_json(module, operation / filename)
    statuses = {"completed.json": "completed", "rollback-completed.json": "rolled-back",
                "aborted.json": "aborted-before-live-writes"}
    require(all(value.get(k) == v for k, v in base.items())
            and value.get("status") == statuses[filename],
            "certificate_resolution_receipt_invalid")
    if manifest_sha is not None:
        require(value.get("manifest_sha256") == manifest_sha,
                "certificate_resolution_manifest_mismatch")
    return value


def verify(project, paths, *, moment, overlay=None, midpoint=False):
    """Use independent module instances; never relax a shared/global verifier.

    Overlay verification validates a proposed or saved entire configuration with
    the original exact-file, permission, alias, manifest and TLS checks. Physical
    metadata is still checked, even for overlaid bytes. Only the old certificate's
    time is evaluated at its own midpoint, never at an owner supplied CLI time.
    """
    layout, prepare, initialize = tools()
    overlay = overlay or {}
    original_read = prepare._read_safe

    def read(path, mode):
        raw = original_read(path, mode)
        return overlay.get(path, raw)

    prepare._read_safe = read
    certificate = x509.load_pem_x509_certificate(read(paths["root"] / "site/certs/fullchain.pem", 0o644))
    if midpoint:
        # Do not mistake a future-dated certificate/system clock for expiry.
        require(moment >= certificate.not_valid_before_utc, "certificate_or_clock_is_in_future")
        validation_time = certificate.not_valid_before_utc + (
            certificate.not_valid_after_utc - certificate.not_valid_before_utc) / 2
    else:
        validation_time = moment

    def verify_prepared(site, actual_project):
        prepare._require_platform_root()
        host = prepare._project_host(actual_project)
        prepare._no_links_in_path(site)
        prepare._assert_no_extended_acl(site)
        return prepare._verify_completed(site, actual_project, host, validation_time)

    prepare.verify_prepared = verify_prepared
    layout.legacy = lambda: prepare
    result = layout.verify_layout(project, **paths)
    identity_raw = read(paths["root"] / "site/control/identity.json", 0o600)
    identity = json.loads(identity_raw, object_pairs_hook=prepare._unique_object)
    # Initialization journals bind identity and each other. Check both using the
    # unchanged original checker before deriving their new SHA256 values.
    initialize.json_file = lambda path: (
        json.loads(read(path, 0o600), object_pairs_hook=prepare._unique_object),
        sha(read(path, 0o600)),
    )
    initialized = initialize.existing_state(paths["root"] / "site/control", project,
                                             identity["instance_id"], sha(identity_raw))
    require(initialized is not None, "completed_initialization_required")
    result.update({"tls": identity["tls"], "server_uuid": initialized["server_uuid"],
                   "certificate_currently_usable": moment >= certificate.not_valid_before_utc
                   and moment + timedelta(days=1) < certificate.not_valid_after_utc})
    result["ca_details"] = {}
    if identity["tls"].get("profile") == "site-ca-v1":
        root = x509.load_pem_x509_certificate(read(paths["root"] / "site/certs/ca-chain.pem", 0o644))
        result["ca_details"] = {"root_sha256": identity["tls"]["root_sha256"],
                                "root_not_after": root.not_valid_after_utc.isoformat(timespec="seconds")}
    return result


@contextmanager
def lease(module, root):
    import fcntl

    module._no_links_in_path(root)
    module._metadata(root, 0o700, directory=True)
    control = root / "maintenance"
    module._metadata(control, 0o700, directory=True)
    path = control / "operation.lock"
    if not os.path.lexists(path):
        try:
            module._new_file(path, b"", 0o600)
        except FileExistsError:
            pass
    module._read_safe(path, 0o600)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        require(os.fstat(fd).st_ino == path.lstat().st_ino, "maintenance_lock_changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CertificateRenewalError("maintenance_operation_busy") from None
        yield control
    finally:
        os.close(fd)


def check_history(module, control, *, own=None):
    for path in control.iterdir():
        if path.name in {"operation.lock", "active.json"}:
            continue
        if path.name == "site-ca" and os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
            identity = read_json(module, control.parent / "site/control/identity.json")
            require(identity["tls"].get("profile") == "site-ca-v1", "unexpected_site_ca_directory")
            module._metadata(path, 0o700, directory=True)
            require({item.name for item in path.iterdir()} == {"issuing-key.pem"}, "site_ca_key_directory_invalid")
            module._metadata(path / "issuing-key.pem", 0o600)
            continue
        if OPERATION.fullmatch(path.name):
            module._metadata(path, 0o700, directory=True)
            if (path / "RECONCILED.json").exists():
                # Retain the failed original records; only a fully bound closure
                # may let an unrelated certificate operation proceed.
                spec = importlib.util.spec_from_file_location("certificate_failed_probe_history",
                    Path(__file__).with_name("failed_probe_history.py"))
                history = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(history)
                started = read_json(module, path / "started.json")
                identity = read_json(module, control.parent / "site/control/identity.json")
                initialized = read_json(module, control.parent / "site/control/initialize-completed.json")
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "operations_v3"))
                sys.path.insert(0, "/opt/gift-ledger-ops")
                import target
                contract = target.runtime_target(started.get("project"))
                require(isinstance(contract, dict) and identity.get("project") == started.get("project")
                        and contract.get("project") == started.get("project"),
                        "probe_recovery_history_target_required")
                history.read_reconciled_history(path,
                    read_bytes=lambda item: module._read_safe(item, 0o600),
                    expected={"project": started["project"], "engine_id": contract.get("engine_id"),
                              "target_sha256": history.digest(contract), "instance_id": identity.get("instance_id"),
                              "server_uuid": initialized.get("server_uuid")})
                continue
            require(not os.path.lexists(path / "failed.json")
                    and os.path.lexists(path / "completed.json"),
                    "previous_maintenance_incomplete_or_failed")
        elif path.name.startswith("certificate-"):
            require(OPERATION.fullmatch(path.name.removeprefix("certificate-")),
                    "unknown_certificate_transaction")
            module._metadata(path, 0o700, directory=True)
            if path == own:
                continue
            resolutions = [name for name in ("completed.json", "rollback-completed.json", "aborted.json")
                           if os.path.lexists(path / name)]
            require(resolutions, "previous_certificate_transaction_unresolved")
            for filename in resolutions:
                value = read_json(module, path / filename)
                require(value.get("format") == FORMAT
                        and value.get("operation_id") == path.name.removeprefix("certificate-")
                        and value.get("action") == "certificate-renewal"
                        and value.get("status") == {"completed.json": "completed",
                            "rollback-completed.json": "rolled-back",
                            "aborted.json": "aborted-before-live-writes"}[filename],
                        "certificate_resolution_receipt_invalid")
                if filename != "aborted.json":
                    require(value.get("manifest_sha256")
                            == sha(module._read_safe(path / "manifest.json", 0o600)),
                            "certificate_resolution_manifest_mismatch")
        else:
            raise CertificateRenewalError("unknown_maintenance_member_preserve_evidence")


def plan(module, project, paths, moment):
    entries = targets(paths)
    before = {name: module._read_safe(path, mode) for name, (path, mode) in entries.items()}
    after = dict(before)
    host = module._project_host(project)
    identity = json.loads(before["root/site/control/identity.json"], object_pairs_hook=module._unique_object)
    if "profile" in identity["tls"]:
        import site_ca
        from certificate_policy import renewal_start
        require(os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml"
                and identity["tls"]["profile"] == site_ca.PROFILE, "certificate_profile_unsupported")
        root = x509.load_pem_x509_certificate(before["tls/ca-chain.pem"])
        old_start = datetime.fromisoformat(identity["tls"]["not_before"])
        start = renewal_start(moment, old_start)
        require(start <= moment, "certificate_or_clock_is_in_future")
        require(start + timedelta(days=site_ca.LEAF_DAYS) <= root.not_valid_after_utc,
                "site_root_certificate_replacement_required")
        key = module._read_safe(paths["root"] / "maintenance/site-ca/issuing-key.pem", 0o600)
        files, metadata = site_ca.sign_leaf(host, root, serialization.load_pem_private_key(key, None), start)
        require(files["certs/ca-chain.pem"] == before["tls/ca-chain.pem"], "site_root_certificate_changed")
        generated = {name: files["certs/" + name] for name in TLS_NAMES}
    else:
        certificate, private, metadata = module._new_tls(host, moment)
        generated = {"fullchain.pem": certificate, "privkey.pem": private, "ca-chain.pem": certificate}
        module._verify_tls({"certs/" + n: raw for n, raw in generated.items()}, metadata, host, moment)
    for name, raw in generated.items():
        after["root/site/certs/" + name] = raw
        after["tls/" + name] = raw

    def record(name):
        return json.loads(after["root/" + name], object_pairs_hook=module._unique_object)

    def save(name, value):
        after["root/" + name] = module._json_bytes(value)
        return sha(after["root/" + name])

    identity["tls"] = metadata
    for name, raw in generated.items():
        identity["files"]["certs/" + name]["sha256"] = sha(raw)
    identity_sha = save("site/control/identity.json", identity)
    complete = record("site/control/prepare-completed.json")
    complete["identity_sha256"] = identity_sha
    save("site/control/prepare-completed.json", complete)
    started = record("site/control/initialize-started.json")
    started["identity_sha256"] = identity_sha
    started_sha = save("site/control/initialize-started.json", started)
    complete = record("site/control/initialize-completed.json")
    complete.update(identity_sha256=identity_sha, started_sha256=started_sha)
    save("site/control/initialize-completed.json", complete)
    layout = record("layout/layout.json")
    layout["canonical_identity_sha256"] = identity_sha
    for name, raw in generated.items():
        layout["roles"]["tls_config"]["files"][name]["sha256"] = sha(raw)
    layout_sha = save("layout/layout.json", layout)
    complete = record("layout/prepare-completed.json")
    complete["layout_sha256"] = layout_sha
    save("layout/prepare-completed.json", complete)
    return before, after


def snapshot(module, operation, base, before, after):
    files = {}
    for index, name in enumerate(before):
        files[name] = {}
        for state, values in (("before", before), ("after", after)):
            filename = f"{state}-{index:02d}.bin"
            module._new_file(operation / filename, values[name], 0o600)
            files[name][state] = {"file": filename, "sha256": sha(values[name])}
    manifest = {**base, "files": files}
    publish_record(module, operation / "manifest.json", manifest)
    return manifest


def saved(module, operation, base, paths):
    manifest, raw = module._record(operation / "manifest.json")
    require(set(manifest) == set(base) | {"files"}
            and all(manifest[k] == value for k, value in base.items()),
            "certificate_transaction_identity_mismatch")
    require(set(manifest["files"]) == set(targets(paths)), "certificate_transaction_targets_invalid")
    values = {"before": {}, "after": {}}
    for index, name in enumerate(targets(paths)):
        require(set(manifest["files"][name]) == {"before", "after"}, "certificate_snapshot_invalid")
        for state in values:
            item = manifest["files"][name][state]
            require(set(item) == {"file", "sha256"} and item["file"] == f"{state}-{index:02d}.bin",
                    "certificate_snapshot_invalid")
            data = module._read_safe(operation / item["file"], 0o600)
            require(sha(data) == item["sha256"], "certificate_snapshot_digest_mismatch")
            values[state][name] = data
    return manifest, raw, values


def overlay(paths, values):
    return {targets(paths)[name][0]: raw for name, raw in values.items()}


def pending(path, operation_id, index):
    return path.parent / f".certificate-pending-{operation_id}-{index:02d}"


def cleanup_pending(module, paths, operation_id, values):
    for index, (name, (path, mode)) in enumerate(targets(paths).items()):
        temporary = pending(path, operation_id, index)
        if os.path.lexists(temporary):
            raw = module._read_safe(temporary, mode)
            require(any(values[state][name].startswith(raw) for state in ("before", "after")),
                    "certificate_pending_file_unrecognized")
            temporary.unlink()
            module._sync_directory(temporary.parent)


def replace(module, path, raw, mode, temporary):
    module._metadata(path, mode)
    module._new_file(temporary, raw, mode)
    os.replace(temporary, path)
    module._sync_directory(path.parent)
    require(module._read_safe(path, mode) == raw, "certificate_replace_readback_failed")


def apply(module, paths, operation_id, values, state, *, checkpoint=None):
    for index, (name, (path, mode)) in enumerate(targets(paths).items()):
        current = module._read_safe(path, mode)
        require(current in (values["before"][name], values["after"][name]),
                "certificate_live_file_unrecognized")
    for index, (name, (path, mode)) in enumerate(targets(paths).items()):
        current = module._read_safe(path, mode)
        require(current in (values["before"][name], values["after"][name]),
                "certificate_live_file_unrecognized")
        if current != values[state][name]:
            replace(module, path, values[state][name], mode, pending(path, operation_id, index))
        if checkpoint:
            checkpoint("replaced", index)


def receipt(module, operation, filename, base, **details):
    value = {**base, "recorded_at": datetime.now(UTC).isoformat(), **details}
    path = operation / filename
    if os.path.lexists(path):
        existing = read_json(module, path)
        require(all(existing.get(k) == v for k, v in {**base, **details}.items()),
                "certificate_receipt_conflict")
        return existing
    publish_record(module, path, value)
    return value


def release_active(module, control, operation):
    active = control / "active.json"
    if os.path.lexists(active):
        # A durable resolved receipt must exist before a shared lease disappears.
        active.rename(operation / ("lease-resolved-" + uuid.uuid4().hex + ".json"))
        module._sync_directory(operation)
        module._sync_directory(control)


def run(project, action, instance_id, operation_id, *, web_stopped=False,
        paths=None, now=None, checkpoint=None):
    paths = dict(paths or PATHS)
    _, module, _ = tools()
    module._require_platform_root()
    module._project_host(project)
    require(set(paths) == set(PATHS), "certificate_volume_paths_invalid")
    require(str(uuid.UUID(instance_id)) == instance_id, "certificate_instance_invalid")
    require(OPERATION.fullmatch(operation_id), "certificate_operation_id_invalid")
    require(action in {"renew", "recover", "rollback", "inspect"}, "certificate_action_invalid")
    require(action == "inspect" or web_stopped, "owner_must_stop_web_before_certificate_write")
    moment = (now or datetime.now(UTC)).replace(microsecond=0)
    require(moment.tzinfo is not None, "certificate_time_invalid")
    base = {"format": FORMAT, "project": project, "instance_id": instance_id,
            "operation_id": operation_id, "action": "certificate-renewal"}
    if action == "inspect":
        # A preflight is entirely read-only, including its first invocation.
        # Concurrent changes fail the original digest checks instead of being
        # hidden by a newly created lock file.
        control = paths["root"] / "maintenance"
        module._metadata(control, 0o700, directory=True)
        check_history(module, control)
        require(not os.path.lexists(control / "active.json"),
                "previous_operation_incomplete_or_failed")
        result = verify(project, paths, moment=moment, midpoint=True)
        require(result["instance_id"] == instance_id, "certificate_instance_mismatch")
        return {**base, "status": "inspected",
                "not_before": result["tls"]["not_before"], "not_after": result["tls"]["not_after"],
                "certificate_sha256": result["tls"]["certificate_sha256"], **result["ca_details"],
                "certificate_currently_usable": result["certificate_currently_usable"]}
    with lease(module, paths["root"]) as control:
        operation = control / ("certificate-" + operation_id)
        check_history(module, control, own=operation)
        active_path = control / "active.json"
        active = read_json(module, active_path) if os.path.lexists(active_path) else None
        if action == "renew":
            require(active is None, "previous_operation_incomplete_or_failed")
            result = verify(project, paths, moment=moment, midpoint=True)
            require(result["instance_id"] == instance_id, "certificate_instance_mismatch")
            require(not os.path.lexists(operation), "certificate_operation_id_already_used")
            before, after = plan(module, project, paths, moment)
            verify(project, paths, moment=moment, overlay=overlay(paths, after))
            module._secure_directory(operation, create=True)
            module._sync_directory(control)
            publish_record(module, operation / "started.json", base)
            snapshot(module, operation, base, before, after)
            manifest_sha = sha(module._read_safe(operation / "manifest.json", 0o600))
            publish_record(module, active_path, {**base, "manifest_sha256": manifest_sha,
                                                "target_state": "after"})
            values = {"before": before, "after": after}
            try:
                if checkpoint:
                    checkpoint("prepared", -1)
                apply(module, paths, operation_id, values, "after", checkpoint=checkpoint)
                result = verify(project, paths, moment=moment)
                require(result["instance_id"] == instance_id, "certificate_instance_mismatch")
                outcome = receipt(module, operation, "completed.json", base, status="completed",
                                  manifest_sha256=manifest_sha, not_after=result["tls"]["not_after"],
                                  certificate_sha256=result["tls"]["certificate_sha256"],
                                  **result["ca_details"],
                                  owner_reported_web_stopped=True, web_stopped_independently_verified=False)
            except Exception:
                # Keep the fixed failure marker permanently. A verified rollback
                # can resolve this certificate transaction without deleting it.
                receipt(module, operation, "failed.json", base, status="failed",
                        error_code="certificate_renewal_failed", manifest_sha256=manifest_sha)
                try:
                    cleanup_pending(module, paths, operation_id, values)
                    apply(module, paths, operation_id, values, "before")
                    result = verify(project, paths, moment=moment, midpoint=True)
                    receipt(module, operation, "rollback-completed.json", base, status="rolled-back",
                            manifest_sha256=manifest_sha,
                            certificate_currently_usable=result["certificate_currently_usable"])
                    release_active(module, control, operation)
                except Exception:
                    raise CertificateRenewalError("certificate_recovery_required_preserve_evidence") from None
                raise CertificateRenewalError("certificate_renewal_failed_original_bytes_restored") from None
            release_active(module, control, operation)
            return outcome

        require(os.path.lexists(operation), "certificate_transaction_missing")
        module._metadata(operation, 0o700, directory=True)
        if os.path.lexists(operation / "started.json"):
            require(read_json(module, operation / "started.json") == base,
                    "certificate_transaction_identity_mismatch")
        else:
            require(not os.path.lexists(operation / "manifest.json") and active is None
                    and action == "recover", "certificate_started_record_missing")
            # A crash before publishing started.json did not touch live files.
            # Record its binding before resolving it, so the read-only health
            # checker can also recognize this aborted transaction afterwards.
            publish_record(module, operation / "started.json", base)
            return receipt(module, operation, "aborted.json", base,
                           status="aborted-before-live-writes")
        if active is not None:
            require(all(active.get(k) == v for k, v in base.items())
                    and set(active) == set(base) | {"manifest_sha256", "target_state"}
                    and active["target_state"] in {"before", "after"},
                    "another_maintenance_transaction_is_active")
        if not os.path.lexists(operation / "manifest.json"):
            require(active is None and action == "recover", "certificate_manifest_missing")
            return receipt(module, operation, "aborted.json", base, status="aborted-before-live-writes")
        _, manifest_raw, values = saved(module, operation, base, paths)
        manifest_sha = sha(manifest_raw)
        require(active is None or active["manifest_sha256"] == manifest_sha,
                "certificate_active_manifest_mismatch")
        cleanup_pending(module, paths, operation_id, values)
        # The old snapshot must still pass all original structure/secret/identity
        # contracts before any recovery write. Then constrain every actual file
        # to the exact before/after states of this particular transaction.
        original = verify(project, paths, moment=moment,
                          overlay=overlay(paths, values["before"]), midpoint=True)
        require(original["instance_id"] == instance_id, "certificate_snapshot_instance_mismatch")
        if action == "recover" and os.path.lexists(operation / "rollback-completed.json"):
            validate_receipt(module, operation, base, "rollback-completed.json", manifest_sha)
            require(all(module._read_safe(path, mode) == values["before"][name]
                        for name, (path, mode) in targets(paths).items()),
                    "rolled_back_certificate_state_changed")
            result = verify(project, paths, moment=moment, midpoint=True)
            release_active(module, control, operation)
            return {**base, "status": "rolled-back-state-verified",
                    "certificate_currently_usable": result["certificate_currently_usable"]}
        if (action == "recover" and os.path.lexists(operation / "completed.json")
                and (active is None or active["target_state"] == "after")):
            validate_receipt(module, operation, base, "completed.json", manifest_sha)
            require(all(module._read_safe(path, mode) == values["after"][name]
                        for name, (path, mode) in targets(paths).items()),
                    "committed_certificate_state_changed")
            result = verify(project, paths, moment=moment, midpoint=True)
            outcome = receipt(module, operation, "recovered-commit.json", base,
                              status="committed-state-verified", manifest_sha256=manifest_sha)
            # Resolution receipts are immutable history. Recompute usability on
            # every invocation, so a repeat recovery after 90 days neither
            # conflicts with the first receipt nor implies the TLS is current.
            outcome = {**outcome,
                       "certificate_currently_usable": result["certificate_currently_usable"],
                       "not_after": result["tls"]["not_after"]}
        else:
            require(action != "rollback" or os.path.lexists(operation / "completed.json"),
                    "explicit_rollback_requires_completed_transaction")
            if action == "rollback":
                validate_receipt(module, operation, base, "completed.json", manifest_sha)
                require(active is None, "rollback_requires_no_active_transaction")
                require(all(module._read_safe(path, mode) == values["after"][name]
                            for name, (path, mode) in targets(paths).items()),
                        "rollback_refuses_changed_or_later_certificate")
                publish_record(module, active_path, {**base, "manifest_sha256": manifest_sha,
                                                    "target_state": "before"})
            apply(module, paths, operation_id, values, "before")
            result = verify(project, paths, moment=moment, midpoint=True)
            outcome = receipt(module, operation, "rollback-completed.json", base, status="rolled-back",
                              manifest_sha256=manifest_sha,
                              certificate_currently_usable=result["certificate_currently_usable"])
        release_active(module, control, operation)
        return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description="内层证书受控续期；先在DSM停止Web，保留回退记录。")
    parser.add_argument("--project", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--action", required=True, choices=["inspect", "renew", "recover", "rollback"])
    parser.add_argument("--web-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.project, args.action, args.instance_id, args.operation_id,
                     web_stopped=args.web_stopped)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except BaseException as error:  # Never print exception text, paths or PEM.
        code = str(error) if isinstance(error, CertificateRenewalError) else "certificate_check_failed_preserve_evidence"
        print(json.dumps({"status": "failed", "error_code": code,
                          "说明": "先停止；保留现场和失败记录，不手改指纹或删除锁。"}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
