import base64
import json
from uuid import uuid4

import pytest
from test_owner_maintenance_api import BASE, ORIGIN, SIBLING, headers
from test_owner_maintenance_api import site as site

from app.services import maintenance_enrollment_protocol as wire
from app.services import maintenance_restore_protocol as protocol

URL = BASE + "/restore"


@pytest.fixture
def restore_site(site):
    project, instance, _ = site.store.binding
    record = dict(project=project, instance_id=instance, operation_id=None, state="idle")
    for name in ("restore", "enrollment"):
        (site.root / "status" / (name + ".json")).write_text(json.dumps(record))
    return site, record


def test_upload_replay_and_seal_have_one_immutable_scoped_queue_entry(restore_site):
    site, idle = restore_site
    op = uuid4().hex
    auth = headers(site.verified())
    binary = {**auth, "Content-Type": "application/octet-stream"}
    files = [{"name": "input.zip", "size": protocol.CHUNK + 3}]
    body = {"operation_id": op, "files": files}
    job = URL + "/" + op
    d = site.root / "requests"
    with site.client() as c:
        assert c.get(URL).status_code == 401
        assert c.get(URL, headers=auth).json() == idle
        assert c.post(URL, json=body, headers={**auth, "Origin": SIBLING}).status_code == 403
        assert c.post(URL, json=body, headers=headers(site.legacy)).status_code in (401, 403)
        assert c.post(URL, json=body, headers=auth).json()["state"] == "uploading"
        competing = {"operation_id": uuid4().hex}
        assert c.post(URL, json={**competing, "files": files}, headers=auth).status_code == 409
        assert c.post(BASE + "/key-enrollment", json=competing, headers=auth).status_code == 409
        assert (
            c.post(
                BASE + "/backup", json={**competing, "after_operation_id": None}, headers=auth
            ).status_code
            == 409
        )
        first = "z" * protocol.CHUNK
        block = job + "/files/0/0"
        assert (
            c.post(block, content=first, headers={**binary, "Origin": SIBLING}).status_code == 403
        )
        assert c.post(block, content=first, headers=binary).json() == {"received": protocol.CHUNK}
        assert c.post(block, content=first, headers=binary).status_code == 200
        assert c.post(block, content="x" * len(first), headers=binary).status_code == 409
        assert c.post(job + "/seal", json={}, headers=auth).status_code == 409
        target = d / ("restore-upload-" + op) / "input.zip"
        with target.open("ab") as f:
            f.write(b"1")  # A short previous write is recovered only when its prefix matches.
        assert (
            c.post(job + f"/files/0/{protocol.CHUNK}", content=b"123", headers=binary).status_code
            == 200
        )
        assert (
            target.read_bytes() == first.encode() + b"123"
            and target.stat().st_mode & 0o777 == 0o600
        )
        assert c.post(job + "/seal", json={}, headers=auth).json()["state"] == "queued"
        assert c.post(job + "/seal", json={}, headers=auth).json()["state"] == "queued"
        assert (d / "restore.request").read_text() == op + "\n" and not (
            d / "restore.upload"
        ).exists()
        assert c.post(block, content=first, headers=binary).status_code == 409


def test_five_member_selection_cancel_and_incompatible_active_backup(restore_site):
    site, _ = restore_site
    auth = headers(site.verified())
    op = uuid4().hex
    files = [
        {"name": name, "size": 1}
        for name in sorted(protocol.META | {f"backup-{uuid4().hex}.glgui.age"})
    ]
    d = site.root / "requests"
    body = {"operation_id": op, "files": files}
    with site.client() as c:
        assert c.post(URL, json={**body, "files": files[:4]}, headers=auth).status_code == 400
        assert (
            c.post(
                URL, json={**body, "files": [{"name": "identity.txt", "size": 2}]}, headers=auth
            ).status_code
            == 400
        )
        (d / "backup.request").write_text(uuid4().hex + "\n")
        assert c.post(URL, json=body, headers=auth).status_code == 409
        (d / "backup.request").unlink()
        assert c.post(URL, json=body, headers=auth).status_code == 202
        job = URL + "/" + op
        for i in range(5):
            assert (
                c.post(
                    job + f"/files/{i}/0",
                    content=b"A",
                    headers={**auth, "Content-Type": "application/octet-stream"},
                ).status_code
                == 200
            )
        assert c.post(job + "/cancel", json={}, headers=auth).json()["state"] == "cancelled"
        assert c.get(URL, headers=auth).json()["state"] == "idle" and list(d.iterdir()) == []


def test_only_host_checked_preview_and_matching_confirmation_can_reach_executor(restore_site):
    site, idle = restore_site
    auth = headers(site.verified())
    op = uuid4().hex
    current = {**idle, "state": "awaiting_packet", "operation_id": op}
    d = site.root / "requests"
    target_base = {
        "format": "gift-ledger-gui-key-target-v1",
        "project": site.project,
        "instance_id": current["instance_id"],
        "operation_id": uuid4().hex,
        "started_at": "2026-11-19T08:31:00+00:00",
    }
    target = {
        **target_base,
        "success": True,
        "recipient": "age1" + "q" * 58,
        "started_sha256": wire.digest(wire.encode(target_base)),
    }
    current["target"] = target
    status = site.root / "status/restore.json"
    status.write_bytes(wire.encode(current))
    cipher = b"age-encryption.org/v1\nfixture"
    addr = {k: v for k, v in target_base.items() if k in {"project", "instance_id"}}
    envelope = {
        "request": {
            **addr,
            "format": "gift-ledger-gui-key-import-request-v1",
            "operation_id": op,
            "ciphertext": "key-transfer-" + op + ".age",
            "ciphertext_sha256": wire.digest(cipher),
            "backup_recipient": "age1" + "p" * 58,
            "target_recipient": target["recipient"],
        },
        "ciphertext_b64": base64.b64encode(cipher).decode(),
    }
    preview = {
        "backup_id": uuid4().hex,
        "manifest_sha256": "a" * 64,
        "source_project": "old-site",
        "source_instance_id": str(uuid4()),
        "gift_count": 1,
        "target_url": ORIGIN + "/",
        "target_project": site.project,
        "target_instance_id": current["instance_id"],
        "engine_operation_id": uuid4().hex,
    }
    confirmation = {"operation_id": op, "preview": preview, "confirmation": "恢复"}
    job = URL + "/" + op
    with site.client() as c:
        assert c.post(job + "/packet", json=envelope, headers=auth).status_code == 202
        assert json.loads((d / (op + ".restore-packet")).read_bytes()) == envelope
        assert c.post(job + "/confirm", json=confirmation, headers=auth).status_code == 409
        current.update(state="eligible", preview=preview)
        current.pop("target")
        status.write_bytes(wire.encode(current))
        for bad in [
            {**confirmation, "confirmation": "yes"},
            {**confirmation, "preview": {**preview, "target_project": "sibling"}},
        ]:
            assert c.post(job + "/confirm", json=bad, headers=auth).status_code in (400, 409)
        assert not (d / (op + ".restore-confirm")).exists()
        assert c.post(job + "/confirm", json=confirmation, headers=auth).status_code == 202
        assert json.loads((d / (op + ".restore-confirm")).read_bytes()) == {
            "operation_id": op,
            "preview": preview,
        }
        assert c.post(job + "/cancel", json={}, headers=auth).status_code == 409
        current.update(
            state="succeeded",
            result={
                "gift_count": 1,
                "cash_amount_all_gift_records": "16.88",
                "temporary_key_removed": True,
                "full_database_and_app_files_verified": True,
            },
        )
        status.write_bytes(wire.encode(current))
        assert c.get(job, headers=headers(site.verified())).json() == current
        current["result"]["temporary_key_removed"] = False
        status.write_bytes(wire.encode(current))
        assert c.get(job, headers=auth).status_code == 503
