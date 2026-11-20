"""Check one site's live TLS from a DSM scheduled task without starting a container."""
from datetime import datetime, timezone
import hashlib
import os
import socket
import ssl
import sys

import backup_ops as ops
import certificate_auto
import certificate_site
from dsm_events import io
import single_yaml_site


NEEDS_COORDINATOR = 3


def live_certificate(context, ip, port):
    try:
        with socket.create_connection((ip, port), timeout=5) as conn, context.wrap_socket(conn, server_hostname=ip) as peer:
            return hashlib.sha256(peer.getpeercert(binary_form=True)).hexdigest(), peer.getpeercert()
    except ssl.SSLCertVerificationError:
        raise
    except OSError:
        raise ops.BackupOpsError("certificate_web_unreachable") from None


def check(host, url, root_sha):
    ip = host.settings["GIFTLEDGER_SITE_IP"]
    port = int(host.settings["GIFTLEDGER_HTTPS_PORT"])
    data = host.station / "data"
    state, _ = host.directories()
    expected_url = single_yaml_site.site_url(ip) + "/"
    identity = ops.read_json(ops.no_links(data / "root_config/site/control/identity.json"))
    ops.require(identity["project"] == host.project, "certificate_identity_changed")

    def published(name):
        path = data / single_yaml_site.owner_guide.public_name(host.project, identity["instance_id"], name)
        return ops.no_links(path if os.path.lexists(path) else data / name)

    actual_url = io.read_regular(published(single_yaml_site.ADDRESS), 1024).decode().strip()
    ops.require(url == expected_url == actual_url, "certificate_site_port_mismatch")
    root = io.read_regular(published(single_yaml_site.ROOT_CERTIFICATE), 16384)
    ops.require(hashlib.sha256(root).hexdigest() == root_sha, "certificate_public_root_changed")
    journal = state / "certificate.json"
    if os.path.lexists(journal) and ops.read_json(journal)["stage"] not in certificate_site.TERMINAL:
        return NEEDS_COORDINATOR

    tls = identity["tls"]
    ops.require(identity["project"] == host.project and tls.get("profile") == "site-ca-v1"
                and tls["host"] == ip and tls["root_sha256"] == root_sha, "certificate_identity_changed")
    leaf_end = datetime.fromisoformat(tls["not_after"])
    ops.require(leaf_end.tzinfo is not None, "certificate_identity_changed")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=root)
    roots = context.get_ca_certs()
    ops.require(len(roots) == 1, "certificate_public_root_changed")
    root_end = datetime.fromtimestamp(ssl.cert_time_to_seconds(roots[0]["notAfter"]), timezone.utc)
    now = datetime.now(timezone.utc)
    if not certificate_site.root_covers_renewal({"not_before": tls["not_before"],
                                                "root_not_after": root_end.isoformat()}, now):
        print(f"本站根证书到期时间为 {root_end.isoformat()}，已不足以签发一整年的网站证书。"
              f"当前网站证书到期时间为 {leaf_end.isoformat()}。请按本站维护说明提前迁站并核对新根证书。", file=sys.stderr)
        return 1

    web = certificate_site.Job(host).web()
    ops.require(web["running"], "certificate_web_must_be_online_to_verify")
    try:
        leaf_sha, metadata = live_certificate(context, ip, port)
    except ssl.SSLCertVerificationError as exc:
        if exc.verify_code != 10 or leaf_end > now:
            raise ops.BackupOpsError("certificate_live_chain_invalid") from None
        # An expired peer fails ordinary TLS. Pin its exact DER fingerprint before
        # handing off to the coordinator which verifies its stored signature.
        pin_only = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        pin_only.check_hostname = False
        pin_only.verify_mode = ssl.CERT_NONE
        leaf_sha, _ = live_certificate(pin_only, ip, port)
        ops.require(leaf_sha == tls["certificate_sha256"], "certificate_live_leaf_mismatch")
        return NEEDS_COORDINATOR
    ops.require(leaf_sha == tls["certificate_sha256"]
                and ssl.cert_time_to_seconds(metadata["notAfter"]) == leaf_end.timestamp(),
                "certificate_live_leaf_mismatch")
    return NEEDS_COORDINATOR if certificate_auto.renewal_due(tls["not_after"], now) else 0


def run(host, url, root_sha):
    os.environ.update(host.settings)
    single_yaml_site.project_host(host.project)
    state, _ = host.directories()
    try:
        with ops.host_lock(state):
            return check(host, url, root_sha)
    except ops.BackupOpsError as exc:
        if exc.code == "another_run_is_active":
            certificate_auto.report_daily_result({"status": "deferred"})
            return 0
        raise
