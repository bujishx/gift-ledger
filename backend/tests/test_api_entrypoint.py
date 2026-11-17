import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.core.config import Settings

source = Path(__file__).resolve().parents[2] / "deploy/api_entrypoint.py"
spec = importlib.util.spec_from_file_location("tested_api_entrypoint", source)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


@pytest.mark.parametrize(
    ("port", "command", "origin"),
    [
        ("443", ["list-users"], "https://192.168.1.24"),
        ("28444", ["reset-password", "--username", "member_1"], "https://192.168.1.24:28444"),
    ],
)
def test_owner_commands_recreate_production_origin_in_a_fresh_exec(
    monkeypatch, port, command, origin
):
    secrets = {"mysql_app_password": "db pass", "jwt_secret": "a" * 40}
    monkeypatch.setattr(entrypoint, "read_secret", secrets.__getitem__)
    monkeypatch.setattr(sys, "argv", ["api_entrypoint.py", *command])
    launch = Mock()
    monkeypatch.setattr(os, "execvp", launch)
    with patch.dict(
        os.environ,
        {
            "GIFTLEDGER_SITE_IP": "192.168.1.24",
            "GIFTLEDGER_HTTPS_PORT": port,
            "GIFT_LEDGER_ENVIRONMENT": "production",
        },
        clear=True,
    ):
        entrypoint.main()
        assert Settings().trusted_origins == origin
    launch.assert_called_once_with(sys.executable, [sys.executable, "-m", "app.cli", *command])


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {
            "GIFT_LEDGER_TRUSTED_ORIGINS": "https://ledger.example",
            "GIFTLEDGER_SITE_IP": "192.168.1.24",
            "GIFTLEDGER_HTTPS_PORT": "443",
        },
    ],
)
def test_legacy_launch_preserves_explicit_origins_and_command(monkeypatch, environment):
    monkeypatch.setattr(entrypoint, "read_secret", lambda _: "a" * 40)
    command = ["python3.12", "-m", "uvicorn", "app.main:app"]
    monkeypatch.setattr(sys, "argv", ["api_entrypoint.py", *command])
    launch = Mock()
    monkeypatch.setattr(os, "execvp", launch)
    with patch.dict(os.environ, environment, clear=True):
        entrypoint.main()
        assert os.environ.get("GIFT_LEDGER_TRUSTED_ORIGINS") == environment.get(
            "GIFT_LEDGER_TRUSTED_ORIGINS"
        )
    launch.assert_called_once_with(command[0], command)
