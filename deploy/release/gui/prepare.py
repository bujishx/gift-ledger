"""Prepare ONLY a new private GUI deployment directory inside /work/site.

The owner starts this one-shot container from DSM. It has no network or Docker
socket. Existing sites, inherited ACLs and interrupted preparations are refused;
no database is contacted. A completed preparation can only be verified again.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SITE = Path("/work/site")
APP_VERSION = "v1.5.0"
SCHEMA_REVISION = "schema_v2_s011"
IDENTITY_FORMAT = "gift-ledger-gui-site-v1"
PREPARE_FORMAT = "gift-ledger-gui-prepare-v1"
FILE_MODES = {
    "secrets/mysql-root-password.txt": 0o400,
    "secrets/mysql-app-password.txt": 0o440,
    "secrets/jwt-secret.txt": 0o440,
    "certs/fullchain.pem": 0o644,
    "certs/privkey.pem": 0o600,
    "certs/ca-chain.pem": 0o644,
}
PREPARE_FILES = {"identity.json", "prepare-started.json", "prepare-completed.json"}
INITIALIZE_FILES = {
    "initialize-started.json", "initialize-failed.json", "initialize-completed.json",
}


class PreparationError(RuntimeError):
    """A deliberately nonsecret error code safe to record or print."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require_platform_root() -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise PreparationError("requires-linux-container-root")
    if sys.version_info[:2] != (3, 12):
        raise PreparationError("requires-python-3.12")
    if not hasattr(os, "listxattr") or not hasattr(os, "O_NOFOLLOW"):
        raise PreparationError("filesystem-security-inspection-unavailable")


def _project_host(project: str) -> str:
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
        from single_yaml_site import project_host
        return project_host(project)
    raise PreparationError("single-yaml-profile-required")


def _no_links_in_path(path: Path) -> None:
    if not path.is_absolute():
        raise PreparationError("site-path-not-absolute")
    for candidate in [*reversed(path.parents), path]:
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PreparationError("site-path-is-not-real-directory")


def _assert_no_extended_acl(path: Path) -> None:
    # chmod alone does not prove that DSM's inherited extended ACL is private.
    # Refuse rather than altering shared parents or silently discarding their ACL.
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except (AttributeError, OSError, TypeError) as exc:
        raise PreparationError("extended-acl-cannot-be-inspected") from exc
    if any("syno" in name.casefold() and "acl" in name.casefold() or
           name in {"system.posix_acl_access", "system.posix_acl_default"}
           for name in names):
        raise PreparationError("extended-acl-requires-owner-private-directory")


def _metadata(path: Path, mode: int, *, directory: bool = False) -> os.stat_result:
    value = path.lstat()
    required_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not required_type(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise PreparationError("prepared-path-type-invalid")
    if not directory and value.st_nlink != 1:
        raise PreparationError("prepared-file-is-hard-linked")
    if value.st_uid != 0 or value.st_gid != 0 or stat.S_IMODE(value.st_mode) != mode:
        raise PreparationError("prepared-path-permissions-invalid")
    _assert_no_extended_acl(path)
    return value


def _secure_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, exist_ok=False)
    _assert_no_extended_acl(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PreparationError("site-path-is-not-real-directory")
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _metadata(path, 0o700, directory=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_file(path: Path, content: bytes, mode: int) -> None:
    """Exclusive, durable creation; never follows or replaces an existing file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, mode)
        if os.fstat(descriptor).st_nlink != 1:
            raise PreparationError("prepared-file-is-hard-linked")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _metadata(path, mode)
    _sync_directory(path.parent)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _new_record(path: Path, value: dict[str, Any]) -> None:
    """Publish a complete JSON atomically without overwriting another journal."""
    temporary = path.parent / (".pending-" + uuid.uuid4().hex + ".json")
    _new_file(temporary, _json_bytes(value), 0o600)
    # link is atomic and fails when the destination exists; replace/rename could
    # overwrite a conflicting journal. The final file must have one hard link.
    os.link(temporary, path, follow_symlinks=False)
    temporary.unlink()
    _metadata(path, 0o600)
    _sync_directory(path.parent)


def _read_safe(path: Path, mode: int) -> bytes:
    before = _metadata(path, mode)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_nlink) != (after.st_dev, after.st_ino, after.st_nlink):
            raise PreparationError("prepared-file-changed-during-inspection")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise PreparationError("prepared-file-too-large")
        return content
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PreparationError("duplicate-json-key")
        value[key] = item
    return value


def _record(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _read_safe(path, 0o600)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise PreparationError("prepared-record-invalid") from exc
    if not isinstance(value, dict):
        raise PreparationError("prepared-record-invalid")
    return value, raw


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _new_tls(host: str, now: datetime) -> tuple[bytes, bytes, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    sans: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))] if host == "localhost" else [x509.IPAddress(ipaddress.ip_address(host))]
    not_before = now - timedelta(minutes=5)
    not_after = not_before + timedelta(days=90)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                    key_encipherment=True, data_encipherment=False,
                                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                                    encipher_only=False, decipher_only=False), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    private = private_key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())
    return pem, private, {"host": host, "validity_days": 90,
                          "not_before": _utc(certificate.not_valid_before_utc),
                          "not_after": _utc(certificate.not_valid_after_utc),
                          "certificate_sha256": hashlib.sha256(pem).hexdigest()}


def _verify_tls(files: dict[str, bytes], expected: dict[str, Any], host: str, now: datetime) -> None:
    try:
        pem = files["certs/fullchain.pem"]
        certificate = x509.load_pem_x509_certificate(pem)
        key = serialization.load_pem_private_key(files["certs/privkey.pem"], password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
            raise PreparationError("tls-private-key-invalid")
        public_bytes = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if public_bytes(key.public_key()) != public_bytes(certificate.public_key()):
            raise PreparationError("tls-key-certificate-mismatch")
        if certificate.issuer != certificate.subject or files["certs/ca-chain.pem"] != pem:
            raise PreparationError("tls-new-self-signed-certificate-invalid")
        certificate.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes,
                                        padding.PKCS1v15(), certificate.signature_hash_algorithm)
        actual_sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        wanted = {x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))} if host == "localhost" else {x509.IPAddress(ipaddress.ip_address(host))}
        if set(actual_sans) != wanted:
            raise PreparationError("tls-host-invalid")
        if certificate.not_valid_after_utc - certificate.not_valid_before_utc != timedelta(days=90):
            raise PreparationError("tls-validity-period-invalid")
        if now < certificate.not_valid_before_utc or now + timedelta(days=1) >= certificate.not_valid_after_utc:
            raise PreparationError("tls-not-valid-or-expiring")
        if ExtendedKeyUsageOID.SERVER_AUTH not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
            raise PreparationError("tls-server-purpose-invalid")
        actual = {"host": host, "validity_days": 90,
                  "not_before": _utc(certificate.not_valid_before_utc),
                  "not_after": _utc(certificate.not_valid_after_utc),
                  "certificate_sha256": hashlib.sha256(pem).hexdigest()}
        if actual != expected:
            raise PreparationError("tls-manifest-mismatch")
    except PreparationError:
        raise
    except Exception as exc:
        raise PreparationError("tls-inspection-failed") from exc


def _verify_site_tls(site: Path, files: dict[str, bytes], expected: dict[str, Any], host: str, now: datetime) -> None:
    if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml" and "profile" in expected:
        import site_ca
        if expected["profile"] != site_ca.PROFILE:
            raise PreparationError("unknown-site-ca-profile")
        key_dir = site.parent / "maintenance/site-ca"
        _no_links_in_path(key_dir)
        _metadata(key_dir, 0o700, directory=True)
        key = _read_safe(key_dir / "issuing-key.pem", 0o600)
        try:
            site_ca.verify(files, key, host, now, expected)
        except Exception as exc:
            raise PreparationError("site-ca-verification-failed") from exc
    else:
        _verify_tls(files, expected, host, now)


def _verify_completed(site: Path, project: str, host: str, now: datetime) -> dict[str, str]:
    if os.path.lexists(site / "control/prepare-failed.json") or not os.path.lexists(site / "control/prepare-completed.json"):
        raise PreparationError("preparation-incomplete-or-failed-no-rerun")
    _metadata(site, 0o700, directory=True)
    if {item.name for item in site.iterdir()} != {"secrets", "certs", "control"}:
        raise PreparationError("prepared-site-has-unknown-files")
    for name in ("secrets", "certs", "control"):
        _metadata(site / name, 0o700, directory=True)
    if {item.name for item in (site / "secrets").iterdir()} != {Path(name).name for name in FILE_MODES if name.startswith("secrets/")}:
        raise PreparationError("prepared-secrets-directory-invalid")
    if {item.name for item in (site / "certs").iterdir()} != {Path(name).name for name in FILE_MODES if name.startswith("certs/")}:
        raise PreparationError("prepared-certs-directory-invalid")
    journal_files = {item.name for item in (site / "control").iterdir()}
    if not PREPARE_FILES <= journal_files or not journal_files <= PREPARE_FILES | INITIALIZE_FILES:
        raise PreparationError("preparation-incomplete-or-unknown-journal")
    for name in journal_files:
        _metadata(site / "control" / name, 0o600)
    identity, identity_raw = _record(site / "control/identity.json")
    started, started_raw = _record(site / "control/prepare-started.json")
    completed, _ = _record(site / "control/prepare-completed.json")
    try:
        instance = str(uuid.UUID(identity["instance_id"]))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PreparationError("prepared-instance-identity-invalid") from exc
    if instance != identity["instance_id"] or identity.get("project") != project:
        raise PreparationError("prepared-instance-identity-invalid")
    if set(identity) != {"format", "project", "instance_id", "app_version", "schema_revision", "created_at", "tls", "files"} or identity.get("format") != IDENTITY_FORMAT or identity.get("app_version") != APP_VERSION or identity.get("schema_revision") != SCHEMA_REVISION:
        raise PreparationError("prepared-release-identity-invalid")
    basic = {"format": PREPARE_FORMAT, "project": project, "instance_id": instance}
    if started != {**basic, "status": "started", "started_at": identity["created_at"]}:
        raise PreparationError("prepared-started-record-invalid")
    expected_completed = {**basic, "status": "completed", "started_at": identity["created_at"],
                          "completed_at": completed.get("completed_at"),
                          "identity_sha256": hashlib.sha256(identity_raw).hexdigest(),
                          "started_sha256": hashlib.sha256(started_raw).hexdigest()}
    if completed != expected_completed:
        raise PreparationError("prepared-completed-record-invalid")
    try:
        if datetime.fromisoformat(completed["completed_at"]) < datetime.fromisoformat(identity["created_at"]):
            raise PreparationError("prepared-journal-time-invalid")
    except (TypeError, ValueError) as exc:
        raise PreparationError("prepared-journal-time-invalid") from exc
    files = {name: _read_safe(site / name, mode) for name, mode in FILE_MODES.items()}
    expected_files = {name: {"sha256": hashlib.sha256(raw).hexdigest(), "mode": f"{FILE_MODES[name]:04o}", "uid": 0, "gid": 0} for name, raw in files.items()}
    if identity.get("files") != expected_files:
        raise PreparationError("prepared-files-integrity-mismatch")
    passwords = [files[name] for name in FILE_MODES if name.startswith("secrets/")]
    if any(re.fullmatch(rb"[0-9a-f]{64}\n", password) is None for password in passwords) or len(set(passwords)) != 3:
        raise PreparationError("prepared-secrets-invalid")
    _verify_site_tls(site, files, identity["tls"], host, now)
    return {"status": "verified", "project": project, "instance_id": instance,
            "app_version": APP_VERSION, "schema_revision": SCHEMA_REVISION}


def verify_prepared(site: Path, project: str) -> dict[str, str]:
    """Public read-only contract for initialization and repeat verification."""
    _require_platform_root()
    host = _project_host(project)
    _no_links_in_path(site)
    _assert_no_extended_acl(site)
    return _verify_completed(site, project, host, datetime.now(timezone.utc))


def prepare(site: Path, project: str) -> dict[str, str]:
    """Internal entry for a container bind or controlled local test fixture."""
    _require_platform_root()
    host = _project_host(project)
    _no_links_in_path(site)
    _assert_no_extended_acl(site)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if any(site.iterdir()):
        # Read-only verification is the sole allowed behavior for a completed
        # instance. Failed/partial journals or unknown existing content remain.
        return _verify_completed(site, project, host, now)

    _secure_directory(site, create=False)
    if any(site.iterdir()):
        raise PreparationError("site-no-longer-empty")
    instance = str(uuid.uuid4())
    started = {"format": PREPARE_FORMAT, "project": project, "instance_id": instance,
               "status": "started", "started_at": _utc(now)}
    control = site / "control"
    started_written = False
    try:
        _secure_directory(control, create=True)
        _new_record(control / "prepare-started.json", started)
        started_written = True
        for name in ("secrets", "certs"):
            _secure_directory(site / name, create=True)
        generated: set[str] = set()
        values: dict[str, bytes] = {}
        for name in FILE_MODES:
            if name.startswith("secrets/"):
                password = secrets.token_hex(32)
                if re.fullmatch(r"[0-9a-f]{64}", password) is None or password in generated:
                    raise PreparationError("new-secret-generator-invalid-or-repeated")
                generated.add(password)
                values[name] = (password + "\n").encode("ascii")
        if os.environ.get("GIFTLEDGER_INSTALL_KIND") == "single-yaml":
            import site_ca
            files, tls, issuer_key = site_ca.issue(host, instance, now)
            key_dir = site.parent / "maintenance/site-ca"
            _secure_directory(key_dir, create=True)
            _new_file(key_dir / "issuing-key.pem", issuer_key, 0o600)
            values.update(files)
        else:
            certificate, private, tls = _new_tls(host, now)
            values.update({"certs/fullchain.pem": certificate, "certs/privkey.pem": private,
                           "certs/ca-chain.pem": certificate})
        _verify_site_tls(site, values, tls, host, now)
        for name, mode in FILE_MODES.items():
            _new_file(site / name, values[name], mode)
        identity = {"format": IDENTITY_FORMAT, "project": project, "instance_id": instance,
                    "app_version": APP_VERSION, "schema_revision": SCHEMA_REVISION,
                    "created_at": _utc(now), "tls": tls,
                    "files": {name: {"sha256": hashlib.sha256(values[name]).hexdigest(),
                                      "mode": f"{mode:04o}", "uid": 0, "gid": 0}
                              for name, mode in FILE_MODES.items()}}
        _new_record(control / "identity.json", identity)
        completed = {**started, "status": "completed",
                     "completed_at": _utc(datetime.now(timezone.utc)),
                     "identity_sha256": hashlib.sha256(_read_safe(control / "identity.json", 0o600)).hexdigest(),
                     "started_sha256": hashlib.sha256(_read_safe(control / "prepare-started.json", 0o600)).hexdigest()}
        _new_record(control / "prepare-completed.json", completed)
        result = _verify_completed(site, project, host, datetime.now(timezone.utc))
        result["status"] = "prepared"
        return result
    except Exception as exc:
        if started_written:
            try:
                _new_record(control / "prepare-failed.json",
                            {**started, "status": "failed", "failed_at": _utc(datetime.now(timezone.utc)),
                             "error_code": exc.code if isinstance(exc, PreparationError) else "preparation-failed"})
            except Exception:  # noqa: BLE001, S110 -- keep started lock; never log a secret-bearing exception
                pass  # The durable started marker still forbids another write run.
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError("preparation-failed") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="独立新站准备；已有站点与失败现场禁止重跑。")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        result = prepare(SITE, args.project)
    except PreparationError as exc:
        print("新站准备停止：" + exc.code + "；保留现场，勿清空、覆盖或重跑。", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 -- fail closed without printing private values
        print("新站准备停止：inspection-failed；保留现场。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
