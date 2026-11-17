"""Read-only HTTP acceptance of the two synthetic rehearsal accounts.

The only POST is login. No registration, reauthentication, import, recovery or
business mutation is sent to either the source or the recovered application.
Internal HTTP transport is restricted to the isolated Compose service aliases;
the normal application still sets its existing Secure browser cookies.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from hashlib import sha256
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from maintenance.database import guarded_engine, load_config, require_guard
from maintenance.rehearsal_data import (
    EXPECTED_USERS,
    USERNAMES,
    read_test_password,
    work_file,
    write_new_result,
)

FORMAT = "gift-ledger-synthetic-application-check"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
VOLATILE_KEYS = frozenset({"request_id", "trace_id", "server_time", "elapsed_ms"})
TARGETS = {
    "source": ("source-db", "http://source-api:8000", "https://localhost:18443"),
    "restore": ("restore-db", "http://candidate-api:8000", "https://localhost:18444"),
}
READ_PATHS = (
    "/api/v1/auth/me",
    "/api/v1/me/profile",
    "/api/v1/contacts?include_inactive=true&limit=100&offset=0",
    "/api/v1/events?limit=100&offset=0",
    "/api/v1/ledger/query/people?limit=100&offset=0",
    "/api/v1/ledger/query/events?limit=100&offset=0",
    "/api/v1/ledger/query/gifts?limit=100&offset=0",
    "/api/v1/dictionaries/relationship?include_inactive=true",
    "/api/v1/dictionaries/kinship?include_inactive=true",
    "/api/v1/dictionaries/event?include_inactive=true",
    "/api/v1/home/summary?year=2024",
    "/api/v1/statistics/timeline?year=2024",
    "/api/v1/statistics/event-distribution?year=2024",
    "/api/v1/statistics/relationships",
    "/api/v1/statistics/reciprocity",
    "/api/v1/statistics/dashboard?grain=year",
    "/api/v1/statistics/dashboard?date_from=2024-01-01&date_to=2024-12-31&grain=month",
)


def validate_target(config, base_url: str, origin: str) -> tuple[str, str]:
    if config.role not in TARGETS:
        raise ValueError("Unknown isolated application role")
    host, expected_url, expected_origin = TARGETS[config.role]
    if config.host != host:
        raise ValueError("Application role and guarded database host do not agree")
    base_url = base_url.rstrip("/")
    origin = origin.rstrip("/")
    if base_url != expected_url or origin != expected_origin:
        raise ValueError("Only the current isolated rehearsal service and origin are permitted")
    return base_url, origin


def normalized_payload(value):
    """Ignore request diagnostics; retain persistent business dates and IDs."""
    if isinstance(value, dict):
        return {key: normalized_payload(item) for key, item in value.items()
                if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [normalized_payload(item) for item in value]
    return value


def canonical_endpoint_payload(path: str | None, value):
    payload = normalized_payload(value)
    if path is not None and urlsplit(path).path == "/api/v1/statistics/dashboard":
        # This existing dashboard deliberately removes SQL ORDER BY, and returns
        # entity aggregates in the query's insertion order. Compare each entity's
        # complete data by its unique ID; the trend chronology stays untouched.
        for field in ("people", "events", "relationships", "event_types"):
            items = payload.get(field)
            if not isinstance(items, list):
                raise ValueError("The dashboard entity collection is missing")
            identifiers = [item.get("id") for item in items if isinstance(item, dict)]
            if (len(identifiers) != len(items)
                    or any(type(identifier) is not int for identifier in identifiers)
                    or len(set(identifiers)) != len(identifiers)):
                raise ValueError("Dashboard entities require unique integer IDs")
            payload[field] = sorted(items, key=lambda item: item["id"])
    return payload


def payload_digest(value, *, path: str | None = None) -> str:
    data = json.dumps(canonical_endpoint_payload(path, value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(data).hexdigest()


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Rehearsal requests must never follow redirects")


class RehearsalClient:
    """Small cookie client restricted to login plus same-service GET requests."""

    def __init__(self, base_url: str, origin: str) -> None:
        self.base_url = base_url
        self.origin = origin
        self._cookie_header = ""
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    def request(self, path: str, *, login: dict | None = None) -> tuple[int, object]:
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or not path.startswith("/api/v1/")
                or parsed.fragment):
            raise ValueError("Only relative application API paths are permitted")
        if login is not None and path != "/api/v1/auth/login":
            raise ValueError("The only permitted POST is synthetic login")
        headers = {"Accept": "application/json", "Origin": self.origin}
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        data = None
        if login is not None:
            data = json.dumps(login, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers,
                          method="POST" if login is not None else "GET")
        try:
            response = self._opener.open(request, timeout=20)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.code
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("Application check response exceeds its size limit")
            if login is not None and status == 200:
                cookie = SimpleCookie()
                for raw in response.headers.get_all("Set-Cookie", []):
                    cookie.load(raw)
                if set(cookie) != {"gift_ledger_session", "gift_ledger_csrf"}:
                    raise ValueError("The application did not issue the expected login cookies")
                if (not cookie["gift_ledger_session"]["secure"]
                        or not cookie["gift_ledger_session"]["httponly"]
                        or not cookie["gift_ledger_csrf"]["secure"]):
                    raise ValueError("The rehearsal must keep the existing Secure cookie settings")
                # This transport is an internal maintenance probe, not a browser.
                # It sends cookies explicitly only to the prevalidated same alias.
                self._cookie_header = "; ".join(
                    f"{name}={cookie[name].value}" for name in sorted(cookie)
                )
            payload = json.loads(content)
            return status, payload

    def get(self, path: str):
        status, payload = self.request(path)
        if status != 200:
            raise ValueError("An application read failed its expected HTTP status")
        return payload


def _check_account_payloads(username: str, data: dict) -> dict:
    expected = EXPECTED_USERS[username]
    account = data[READ_PATHS[0]]
    if (account["username"] != username or account["is_admin"]
            or not account["profile_completed"]
            or account["display_name"] != expected["display_name"]):
        raise ValueError("The restored synthetic account is incorrect")
    contacts = data[READ_PATHS[2]]
    events = data[READ_PATHS[3]]
    gifts = data[READ_PATHS[6]]
    if (contacts["total"] != expected["contact_count"]
            or events["total"] != expected["event_count"]
            or gifts["page"]["total"] != expected["gift_count"]
            or len(gifts["items"]) != expected["gift_count"]):
        raise ValueError("Synthetic per-user row counts are incorrect")
    if (Decimal(gifts["totals"]["income_amount"]) != Decimal(expected["income_amount"])
            or Decimal(gifts["totals"]["expense_amount"]) != Decimal(expected["expense_amount"])):
        raise ValueError("Synthetic income or expense totals are incorrect")
    summary = data[READ_PATHS[15]]["summary"]
    if (summary["gift_count"] != expected["gift_count"]
            or summary["pure_goods_count"] != expected["pure_goods_count"]
            or Decimal(summary["income_amount"]) != Decimal(expected["income_amount"])
            or Decimal(summary["expense_amount"]) != Decimal(expected["expense_amount"])):
        raise ValueError("Dashboard and ledger totals disagree")
    for category, path in zip(("relationship", "kinship", "event"), READ_PATHS[7:10], strict=True):
        custom_names = sorted(item["name"] for item in data[path]["items"] if not item["is_system"])
        if custom_names != expected["custom_dictionaries"][category]:
            raise ValueError("Custom dictionaries are missing or visible to another user")
    if username == USERNAMES[0]:
        precisions = {item["event"]["date_precision"] for item in gifts["items"]}
        if precisions != {"day", "year", "month", "unknown"}:
            raise ValueError("Legacy date precision did not survive recovery")
        if sum(not item["is_active"] for item in contacts["items"]) != 1:
            raise ValueError("The synthetic inactive contact was not preserved")
        annual = data[READ_PATHS[10]]["annual"]
        if Decimal(annual["income_amount"]) != 600 or Decimal(annual["expense_amount"]) != 200:
            raise ValueError("The fixed 2024 annual statistics are incorrect")
    return {
        "contact_id": contacts["items"][0]["id"],
        "event_id": events["items"][0]["event"]["id"],
        "gift_id": gifts["items"][0]["id"],
    }


def collect_snapshot(config, base_url: str, origin: str, password_file: Path) -> dict:
    base_url, origin = validate_target(config, base_url, origin)
    password = read_test_password(password_file)
    with guarded_engine(config, "gift_ledger_v2") as engine:
        health = RehearsalClient(base_url, origin).get("/api/v1/health/ready")
        if health.get("status") != "ok" or health.get("version") != "1.5.0":
            raise ValueError("The rehearsal application is not the approved healthy version")
        clients = {}
        account_data = {}
        representatives = {}
        for username in USERNAMES:
            client = RehearsalClient(base_url, origin)
            status, _payload = client.request("/api/v1/auth/login", login={
                "username": username, "password": password,
            })
            if status != 200:
                raise ValueError("A synthetic user could not log in after recovery")
            data = {path: client.get(path) for path in READ_PATHS}
            representatives[username] = _check_account_payloads(username, data)
            clients[username] = client
            account_data[username] = data
        owner_id = account_data[USERNAMES[0]][READ_PATHS[0]]["id"]
        other_id = account_data[USERNAMES[1]][READ_PATHS[0]]["id"]
        if owner_id == other_id:
            raise ValueError("Synthetic users were not isolated")
        isolation = {}
        for username in USERNAMES:
            other = next(item for item in USERNAMES if item != username)
            isolation[username] = {}
            for kind, prefix in (("contact", "/contacts"), ("event", "/events"),
                                 ("gift", "/gifts")):
                identifier = representatives[other][kind + "_id"]
                status, payload = clients[username].request(f"/api/v1{prefix}/{identifier}")
                if status != 404 or not isinstance(payload, dict):
                    raise ValueError("Another user's business detail was exposed")
                isolation[username][kind] = {
                    "status": status, "code": payload.get("error", {}).get("code"),
                }
        require_guard(engine, config)
    return {
        "format": FORMAT, "version": 1, "run_id": config.run_id,
        "application_version": "1.5.0", "synthetic_only": True,
        "read_only_business": True,
        "accounts": {
            username: {"endpoint_hashes": {path: payload_digest(data[path], path=path)
                                           for path in READ_PATHS},
                       "expected_counts_and_totals_checked": True}
            for username, data in account_data.items()
        },
        "cross_user_reads": isolation,
        "login_verified": True,
    }


def compare_reference(snapshot: dict, reference: dict) -> None:
    if (reference.get("format") != FORMAT or reference.get("version") != 1
            or reference.get("run_id") != snapshot["run_id"]
            or reference.get("synthetic_only") is not True
            or reference.get("read_only_business") is not True
            or reference.get("application_version") != "1.5.0"):
        raise ValueError("The reference must be from this synthetic source and application version")
    for key in ("accounts", "cross_user_reads", "login_verified"):
        if reference.get(key) != snapshot[key]:
            raise ValueError("Recovered application responses differ from the source reference")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only synthetic application recovery checks")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if config.role == "restore" and args.reference is None:
            raise ValueError("Recovered applications require the original source reference")
        result_path = work_file(args.result, Path(config.work_root))
        if result_path.exists():
            raise ValueError("An existing acceptance report cannot be overwritten")
        reference = None
        if args.reference is not None:
            reference_path = work_file(args.reference, Path(config.work_root))
            if not reference_path.is_file() or reference_path.stat().st_size > MAX_RESPONSE_BYTES:
                raise ValueError("The source acceptance reference is missing or too large")
            reference = json.loads(reference_path.read_text(encoding="utf-8"))
        snapshot = collect_snapshot(config, args.base_url, args.origin, args.password_file)
        if reference is not None:
            compare_reference(snapshot, reference)
        snapshot["matched_source_reference"] = reference is not None
        write_new_result(result_path, Path(config.work_root), snapshot)
    except Exception as exc:
        print(json.dumps({"success": False, "operation": "application-check",
                          "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"success": True, "operation": "application-check",
                      "run_id": config.run_id, "users_checked": len(USERNAMES),
                      "endpoints_per_user": len(READ_PATHS), "cross_user_denials": 6,
                      "matched_source_reference": reference is not None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
