import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

ORIGIN = "https://192.0.2.9:16449"
BASE = "/api/v1/maintenance"
PROJECT = "isolated-maintenance-test"


def record(root, state, operation_id, **fields):
    value = {
        "format": "giftledger-maintenance-status-v1",
        "project": PROJECT,
        "state": state,
        "operation_id": operation_id,
        **fields,
    }
    (root / "status" / "result.json").write_bytes(json.dumps(value).encode())


@pytest.fixture
def site(monkeypatch, tmp_path):
    token = secrets.token_hex(32)
    (tmp_path / "owner.sha256").write_text(hashlib.sha256(token.encode()).hexdigest() + "\n")
    (tmp_path / "requests").mkdir()
    (tmp_path / "status").mkdir()
    record(tmp_path, "idle", None)
    monkeypatch.setenv("GIFTLEDGER_MAINTENANCE_DIR", str(tmp_path))
    monkeypatch.setenv("GIFTLEDGER_PROJECT", PROJECT)
    settings = Settings(environment="test", trusted_origins=ORIGIN, _env_file=None)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        yield client, {"Authorization": f"Bearer {token}", "Origin": ORIGIN}, tmp_path


def test_disabled_without_explicit_configuration(monkeypatch):
    monkeypatch.delenv("GIFTLEDGER_MAINTENANCE_DIR", raising=False)
    with TestClient(create_app(Settings(environment="test", _env_file=None))) as client:
        assert client.get(BASE + "/page").status_code == 404
        assert client.get(BASE + "/status").status_code == 404


def test_login_surfaces_and_static_assets(site):
    client, headers, _ = site
    page = client.get(BASE + "/page")
    assert page.status_code == 200
    assert "本站维护授权码" in page.text
    assert 'href="/maintenance-ui.css"' in page.text
    assert 'src="/brand-mark.png"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "img-src 'self'" in page.headers["content-security-policy"]
    assert client.get(BASE + "/page.js").status_code == 200
    assert client.get(BASE + "/page.css").status_code == 200
    assert client.get(BASE + "/status").status_code == 403
    denied = client.get(
        BASE + "/status", headers={"Authorization": "Bearer " + secrets.token_hex(32)}
    )
    assert denied.status_code == 403
    assert client.get(BASE + "/status?token=unused").status_code == 400
    assert client.get(BASE + "/status", headers=headers).json()["state"] == "idle"


def test_origin_and_input_are_checked_before_queuing(site):
    client, headers, root = site
    operation = {"operation_id": uuid4().hex}
    for origin in ("https://192.0.2.9:16450", "null", ""):
        denial = client.post(
            BASE + "/backup", json=operation, headers={**headers, "Origin": origin}
        )
        assert denial.status_code == 403
    assert client.post(BASE + "/backup", json=operation).status_code == 403
    assert (
        client.post(
            BASE + "/backup", json={**operation, "project": "other"}, headers=headers
        ).status_code
        == 422
    )
    assert not list((root / "requests").iterdir())


def test_concurrent_submission_and_idempotent_retry(site):
    client, headers, root = site
    attempts = [uuid4().hex for _ in range(8)]

    def send(operation):
        return client.post(BASE + "/backup", json={"operation_id": operation}, headers=headers)

    with ThreadPoolExecutor(max_workers=8) as threads:
        responses = list(threads.map(send, attempts))
    assert sum(r.status_code == 202 for r in responses) == 1
    assert sum(r.status_code == 409 for r in responses) == 7
    queued = client.get(BASE + "/status", headers=headers).json()
    marker = root / "requests" / "backup.request"
    operation = queued["operation_id"]
    assert marker.read_bytes() == operation.encode() + b"\n"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert send(operation).status_code == 202
    record(root, "running", operation)
    assert send(operation).json()["state"] == "running"
    record(root, "succeeded", operation, decryption_verified=True, backup_id=uuid4().hex)
    marker.unlink()
    assert send(operation).status_code == 200
    assert not marker.exists()
    next_op = uuid4().hex
    assert send(next_op).status_code == 409
    next_body = {"operation_id": next_op, "after_operation_id": operation}
    assert client.post(BASE + "/backup", json=next_body, headers=headers).status_code == 202
    record(root, "failed", next_op, code="station_directory_mismatch")
    marker.unlink()
    assert send(operation).status_code == 409
    assert not marker.exists()


def test_success_requires_verified_receipt_and_same_project(site):
    client, headers, root = site
    operation = uuid4().hex
    record(root, "succeeded", operation, backup_id=uuid4().hex)
    assert client.get(BASE + "/status", headers=headers).status_code == 503
    record(root, "running", operation, project="wrong-site")
    assert client.get(BASE + "/status", headers=headers).status_code == 503
    (root / "status" / "result.json").unlink()
    assert (
        client.post(BASE + "/backup", json={"operation_id": operation}, headers=headers).status_code
        == 503
    )
    assert not (root / "requests" / "backup.request").exists()


def test_needs_review_blocks_new_requests_even_without_marker(site):
    client, headers, root = site
    operation = uuid4().hex
    record(root, "needs_review", operation, code="interrupted", diagnostic="private raw log")
    result = client.post(
        BASE + "/backup",
        json={"operation_id": uuid4().hex, "after_operation_id": operation},
        headers=headers,
    )
    assert result.status_code == 409
    assert result.json()["code"] == "interrupted"
    assert "diagnostic" not in result.json()
    assert not (root / "requests" / "backup.request").exists()


def test_worker_can_report_unreadable_marker_without_job_id(site):
    client, headers, root = site
    marker = root / "requests" / "backup.request"
    marker.symlink_to(root / "owner.sha256")
    assert client.get(BASE + "/status", headers=headers).status_code == 503
    record(root, "needs_review", None, code="invalid_request_file")
    report = client.get(BASE + "/status", headers=headers)
    assert report.status_code == 200
    assert report.json()["code"] == "invalid_request_file"
    assert report.json()["operation_id"] is None
