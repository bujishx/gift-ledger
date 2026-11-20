import pytest
from pydantic import ValidationError

from app.core.config import Settings

LOCAL = "https://192.0.2.8:16888"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("", ""),
        ("https://Ledger.Example:443/", "https://ledger.example"),
        ("https://ledger.example:51888", "https://ledger.example:51888"),
    ],
)
def test_proxy_origin_never_changes_the_site_binding(value, canonical):
    settings = Settings(trusted_origins=LOCAL, reverse_proxy_origin=value, _env_file=None)
    assert settings.reverse_proxy_origin == canonical
    assert settings.trusted_origin_list == (LOCAL,)


@pytest.mark.parametrize(
    "invalid",
    [
        "http://ledger.example",
        "https://*.example",
        "https://a..example",
        "https://ledger.example/#",
        "https://ledger.example?x",
        "https://ledger.example/path",
        "https://someone@ledger.example",
        "https://ledger.example:0",
        "https://ledger.example:65536",
        "https://ledger.example:0443",
        "https://a.example,https://b.example",
        "https://192.0.2.1",
        "https://ledger.example//",
        "https://ledger.example\n",
    ],
)
def test_invalid_proxy_origin_is_rejected(invalid):
    with pytest.raises(ValidationError):
        Settings(trusted_origins=LOCAL, reverse_proxy_origin=invalid, _env_file=None)


def test_proxy_and_private_cookies_must_use_different_hosts():
    with pytest.raises(ValidationError):
        Settings(
            trusted_origins="https://ledger.example:16888",
            reverse_proxy_origin="https://ledger.example:51888",
            _env_file=None,
        )
