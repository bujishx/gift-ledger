import json

from fastapi.testclient import TestClient

from app.api.dependencies import get_authentication_session, get_current_user
from app.api.maintenance_access import require_owner
from app.api.routes import site_about
from app.core.config import Settings
from app.main import create_app

ORIGIN = "https://192.0.2.10:8443"
INSTANCE = "1d1a53b9-1ca8-4c41-9625-c7a21897a42d"


def test_about_routes_are_separate_and_expose_only_this_targets_verified_data(tmp_path, monkeypatch):
    for name, value in {"GIFTLEDGER_PROJECT": "test-family", "GIFTLEDGER_MAINTENANCE_DIR": str(tmp_path),
                        "GIFTLEDGER_OWNER_STATE_DIR": str(tmp_path), "GIFTLEDGER_BUILD_COMMIT": "ab"*20,
                        "GIFTLEDGER_BUILD_DATE": "2026-10-03", "GIFTLEDGER_BUILD_VERSION": "1.8.0-rc"}.items():
        monkeypatch.setenv(name, value)
    site_file = tmp_path / "site.json"
    monkeypatch.setattr(site_about, "SITE_FILE", site_file)
    settings = Settings(trusted_origins=ORIGIN)
    assert settings.app_version == "1.5.0"  # Frozen schema and backup compatibility contract.
    app = create_app(settings)
    app.dependency_overrides[get_authentication_session] = lambda: object()
    client = TestClient(app, base_url=ORIGIN)
    business, owner = "/api/v1/site/about", "/api/v1/maintenance/about"
    assert client.get(business).status_code != 200
    assert client.get(owner).status_code != 200
    app.dependency_overrides[get_current_user] = lambda: 1
    assert client.get(owner).status_code != 200
    response = client.get(business)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["restore_state"] == "unknown" and response.json()["instance_id"] is None
    site = dict(project="test-family", instance_id=INSTANCE, origin=ORIGIN, restore_state="verified",
                restored_at="2026-10-01T12:00:00+00:00", private="hidden")
    site_file.write_text(json.dumps(site))
    verified = client.get(business).json()
    assert verified["instance_id"] == INSTANCE and verified["git_commit"] == "ab"*20
    assert verified["version"] == "1.8.0-rc" and verified["updated_at"] == "2026-10-03"
    assert verified["restore_state"] == "verified" and verified["restored_at"].startswith("2026-10-01T12:00:00")
    assert "private" not in verified
    app.dependency_overrides.pop(get_current_user)
    app.dependency_overrides[require_owner] = lambda: object()
    assert client.get(business).status_code != 200
    assert client.get(owner).json() == verified
    site_file.write_text(json.dumps(dict(site, restored_at="unavailable")))
    partial = client.get(owner).json()
    assert partial["instance_id"] == INSTANCE and partial["restored_at"] is None
    site_file.write_text(json.dumps(dict(site, project="other-site")))
    assert client.get(owner).json()["instance_id"] is None
