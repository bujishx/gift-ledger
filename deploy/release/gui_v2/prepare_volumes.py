"""Claim ONLY empty private Linux volumes for a GUI v2 site.

No host secrets, network or Docker API. The old site contract remains unchanged
inside ``root_config/site``. Completed preparation is strictly read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path("/work/site")
APP_SECRETS = Path("/work/app-secrets")
TLS = Path("/work/tls")
DB_SECRETS = Path("/work/db-secrets")
LEGACY_PREPARE = Path("/opt/gift-ledger-gui/prepare.py")
FORMAT = "gift-ledger-gui-private-volumes-v2"
MARKER_FORMAT = "gift-ledger-gui-volume-identity-v2"
MARKER = ".volume-identity.json"
ROLES = ("root_config", "app_secrets", "tls_config", "db_secrets")
DATA_ROLES = ("mysql_data", "app_data", "mysql_socket")
ALIASES = {
    "mysql_root_password": ("secrets/mysql-root-password.txt", 0o400),
    "mysql_app_password": ("secrets/mysql-app-password.txt", 0o440),
    "jwt_secret": ("secrets/jwt-secret.txt", 0o440),
}
TLS_FILES = {"fullchain.pem": 0o644, "privkey.pem": 0o600, "ca-chain.pem": 0o644}


class VolumePreparationError(RuntimeError):
    """A fixed nonsecret error code."""


def require(condition, code):
    if not condition:
        raise VolumePreparationError(code)


def legacy():
    spec = importlib.util.spec_from_file_location(
        "gui_v2_legacy_prepare", LEGACY_PREPARE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat()


def roots(root, app_secrets, tls, db_secrets):
    return dict(zip(ROLES, (root, app_secrets, tls, db_secrets), strict=True))


def directory(module, path, mode, *, create=False):
    if mode == 0o700:
        module._secure_directory(path, create=create)
        return
    require(not create, "unsupported_directory_creation")
    module._no_links_in_path(path)
    module._assert_no_extended_acl(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    module._metadata(path, mode, directory=True)


def marker(project, instance, role):
    return {
        "format": MARKER_FORMAT,
        "project": project,
        "instance_id": instance,
        "role": role,
        "volume_name": project + "_" + role,
        "role_id": str(uuid.uuid4()),
    }


def check_marker(module, path, expected):
    actual, raw = module._record(path / MARKER)
    require(actual == expected, "volume_identity_mismatch")
    require(
        str(uuid.UUID(actual["role_id"])) == actual["role_id"],
        "volume_role_uuid_invalid",
    )
    return digest(raw)


def verify_layout(
    project, root=ROOT, app_secrets=APP_SECRETS, tls=TLS, db_secrets=DB_SECRETS
):
    """Verify all private roles without changing a byte or a permission."""
    module = legacy()
    module._require_platform_root()
    module._project_host(project)
    paths = roots(root, app_secrets, tls, db_secrets)
    require(
        len({(p.stat().st_dev, p.stat().st_ino) for p in paths.values()}) == 4,
        "configuration_volumes_are_aliased",
    )
    for role, path in paths.items():
        module._no_links_in_path(path)
        module._metadata(
            path, 0o750 if role == "app_secrets" else 0o700, directory=True
        )
    require(
        {p.name for p in root.iterdir()}
        == {MARKER, "site", "bindings", "layout", "maintenance"},
        "root_configuration_members_invalid",
    )
    for name in ("bindings", "layout", "maintenance"):
        module._metadata(root / name, 0o700, directory=True)
    layout_dir = root / "layout"
    require(
        {p.name for p in layout_dir.iterdir()}
        == {"layout.json", "prepare-started.json", "prepare-completed.json"},
        "volume_preparation_incomplete_failed_or_unknown",
    )
    layout, raw = module._record(layout_dir / "layout.json")
    require(
        set(layout)
        == {
            "format",
            "project",
            "instance_id",
            "canonical_identity_sha256",
            "roles",
            "data_volumes",
        }
        and layout["format"] == FORMAT
        and layout["project"] == project,
        "volume_layout_contract_invalid",
    )
    canonical = module.verify_prepared(root / "site", project)
    instance = canonical["instance_id"]
    require(layout["instance_id"] == instance, "volume_instance_mismatch")
    identity_raw = module._read_safe(root / "site/control/identity.json", 0o600)
    require(
        layout["canonical_identity_sha256"] == digest(identity_raw),
        "canonical_identity_mismatch",
    )
    require(
        layout["data_volumes"] == {role: project + "_" + role for role in DATA_ROLES},
        "persistent_volume_names_invalid",
    )
    require(set(layout["roles"]) == set(ROLES), "volume_roles_invalid")
    seen = set()
    for role, path in paths.items():
        role_data = layout["roles"][role]
        require(
            set(role_data) == {"identity", "marker_sha256", "files"},
            "volume_role_contract_invalid",
        )
        expected_marker = role_data["identity"]
        require(
            set(expected_marker)
            == {"format", "project", "instance_id", "role", "volume_name", "role_id"}
            and expected_marker["format"] == MARKER_FORMAT
            and expected_marker["project"] == project
            and expected_marker["instance_id"] == instance
            and expected_marker["role"] == role
            and expected_marker["volume_name"] == project + "_" + role,
            "volume_identity_contract_invalid",
        )
        require(
            expected_marker["role_id"] not in seen, "duplicate_volume_role_identity"
        )
        seen.add(expected_marker["role_id"])
        require(
            check_marker(module, path, expected_marker) == role_data["marker_sha256"],
            "volume_marker_digest_mismatch",
        )
        if role == "root_config":
            expected_files = {
                "bindings/" + name: (relative, mode)
                for name, (relative, mode) in ALIASES.items()
            }
            require(
                {p.name for p in (root / "bindings").iterdir()} == set(ALIASES),
                "secret_binding_members_invalid",
            )
        elif role == "app_secrets":
            expected_files = {
                name: ALIASES[name] for name in ("mysql_app_password", "jwt_secret")
            }
        elif role == "db_secrets":
            expected_files = {"mysql_root_password": ALIASES["mysql_root_password"]}
        else:
            expected_files = {
                name: ("certs/" + name, mode) for name, mode in TLS_FILES.items()
            }
        if role != "root_config":
            require(
                {p.name for p in path.iterdir()} == set(expected_files) | {MARKER},
                "volume_members_invalid",
            )
        require(
            set(role_data["files"]) == set(expected_files),
            "volume_file_contract_invalid",
        )
        for name, (source, mode) in expected_files.items():
            value = module._read_safe(path / name, mode)
            source_value = module._read_safe(root / "site" / source, mode)
            require(value == source_value, "volume_secret_or_tls_alias_mismatch")
            require(
                role_data["files"][name]
                == {"sha256": digest(value), "uid": 0, "gid": 0, "mode": f"{mode:04o}"},
                "volume_file_metadata_mismatch",
            )
    started, started_raw = module._record(layout_dir / "prepare-started.json")
    complete, _ = module._record(layout_dir / "prepare-completed.json")
    require(
        set(started) == {"format", "project", "status", "started_at"}
        and started["format"] == FORMAT
        and started["project"] == project
        and started["status"] == "started",
        "volume_started_record_invalid",
    )
    require(
        complete
        == {
            **started,
            "status": "completed",
            "instance_id": instance,
            "completed_at": complete.get("completed_at"),
            "layout_sha256": digest(raw),
            "started_sha256": digest(started_raw),
        },
        "volume_completed_record_invalid",
    )
    require(
        datetime.fromisoformat(complete["completed_at"])
        >= datetime.fromisoformat(started["started_at"]),
        "volume_completion_time_invalid",
    )
    return {
        "status": "verified",
        "project": project,
        "instance_id": instance,
        "layout_sha256": digest(raw),
        "canonical_identity_sha256": digest(identity_raw),
        "site_path": str(root / "site"),
        "secret_bindings_path": str(root / "bindings"),
    }


def prepare_volumes(
    project,
    root=ROOT,
    app_secrets=APP_SECRETS,
    tls=TLS,
    db_secrets=DB_SECRETS,
    data_roots=None,
):
    module = legacy()
    module._require_platform_root()
    module._project_host(project)
    paths = roots(root, app_secrets, tls, db_secrets)
    for path in paths.values():
        module._no_links_in_path(path)
        module._assert_no_extended_acl(path)
    if any(any(path.iterdir()) for path in paths.values()):
        return verify_layout(project, root, app_secrets, tls, db_secrets)
    fresh = {**paths, **(data_roots or {})}
    require(
        len({(p.stat().st_dev, p.stat().st_ino) for p in fresh.values()}) == len(fresh),
        "new_volumes_are_aliased",
    )
    for path in fresh.values():
        module._no_links_in_path(path)
        module._assert_no_extended_acl(path)
        require(not any(path.iterdir()), "new_volume_not_empty")
    for role, path in paths.items():
        directory(module, path, 0o750 if role == "app_secrets" else 0o700)
    directory(module, root / "layout", 0o700, create=True)
    started = {
        "format": FORMAT,
        "project": project,
        "status": "started",
        "started_at": utc_now(),
    }
    module._new_record(root / "layout/prepare-started.json", started)
    try:
        directory(module, root / "site", 0o700, create=True)
        directory(module, root / "maintenance", 0o700, create=True)
        result = module.prepare(root / "site", project)
        instance = result["instance_id"]
        directory(module, root / "bindings", 0o700, create=True)
        identities = {role: marker(project, instance, role) for role in ROLES}
        role_records = {}
        for role, path in paths.items():
            module._new_record(path / MARKER, identities[role])
            entries = (
                {
                    "bindings/" + name: (relative, mode)
                    for name, (relative, mode) in ALIASES.items()
                }
                if role == "root_config"
                else (
                    {
                        name: ALIASES[name]
                        for name in ("mysql_app_password", "jwt_secret")
                    }
                    if role == "app_secrets"
                    else (
                        {"mysql_root_password": ALIASES["mysql_root_password"]}
                        if role == "db_secrets"
                        else {
                            name: ("certs/" + name, mode)
                            for name, mode in TLS_FILES.items()
                        }
                    )
                )
            )
            for name, (relative, mode) in entries.items():
                module._new_file(
                    path / name, module._read_safe(root / "site" / relative, mode), mode
                )
            role_records[role] = {
                "identity": identities[role],
                "marker_sha256": digest(module._read_safe(path / MARKER, 0o600)),
                "files": {
                    name: {
                        "sha256": digest(module._read_safe(path / name, mode)),
                        "uid": 0,
                        "gid": 0,
                        "mode": f"{mode:04o}",
                    }
                    for name, (_, mode) in entries.items()
                },
            }
        layout = {
            "format": FORMAT,
            "project": project,
            "instance_id": instance,
            "canonical_identity_sha256": digest(
                module._read_safe(root / "site/control/identity.json", 0o600)
            ),
            "roles": role_records,
            "data_volumes": {role: project + "_" + role for role in DATA_ROLES},
        }
        module._new_record(root / "layout/layout.json", layout)
        module._new_record(
            root / "layout/prepare-completed.json",
            {
                **started,
                "status": "completed",
                "instance_id": instance,
                "completed_at": utc_now(),
                "layout_sha256": digest(
                    module._read_safe(root / "layout/layout.json", 0o600)
                ),
                "started_sha256": digest(
                    module._read_safe(root / "layout/prepare-started.json", 0o600)
                ),
            },
        )
        final = verify_layout(project, root, app_secrets, tls, db_secrets)
        final["status"] = "prepared"
        return final
    except BaseException as exc:
        try:
            module._new_record(
                root / "layout/prepare-failed.json",
                {
                    **started,
                    "status": "failed",
                    "failed_at": utc_now(),
                    "error_code": "preparation_stopped_preserve_evidence",
                },
            )
        except Exception:  # noqa: BLE001, S110 -- keep permanent started marker; never print private failures
            pass
        raise VolumePreparationError("preparation_stopped_preserve_evidence") from exc


def main():
    parser = argparse.ArgumentParser(
        description="仅准备独立全新私有卷；失败禁止覆盖和重跑。"
    )
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        result = prepare_volumes(
            args.project,
            data_roots={
                "mysql_data": Path("/work/new-mysql-data"),
                "app_data": Path("/work/app-data"),
                "mysql_socket": Path("/work/mysql"),
            },
        )
    except BaseException as exc:  # noqa: BLE001 -- fixed reason codes only
        code = (
            str(exc)
            if isinstance(exc, VolumePreparationError)
            else "preparation_stopped_preserve_evidence"
        )
        print(
            json.dumps({"status": "failed", "error_code": code}),
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
