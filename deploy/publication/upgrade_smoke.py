"""Exercise an in-place upgrade and an optional HTTPS reverse proxy on a disposable Linux runner."""

import argparse
from contextlib import ExitStack
from decimal import Decimal
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from uuid import UUID

from images import ROLES, local, render
from smoke import wait_ready


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


class Browser:
    """An HTTPS origin with its own cookie jar; test DNS resolves only to loopback."""

    def __init__(self, host, port, ca, cookies):
        self.origin = f"https://{host}:{port}"
        self.cookies = cookies
        cookies.touch(mode=0o600)
        self.curl = [
            "curl", "--silent", "--show-error", "--max-time", "10", "--noproxy", "*",
            "--cacert", str(ca), "--resolve", f"{host}:{port}:127.0.0.1",
            "--cookie", str(cookies), "--cookie-jar", str(cookies),
            "--write-out", "\\n%{http_code}",
        ]

    def request(self, path, body=None, status=200):
        args = [*self.curl, self.origin + path]
        payload = None
        if body is not None:
            args.extend(["-H", "Origin: " + self.origin, "-H", "Content-Type: application/json",
                         "-H", "Sec-Fetch-Site: same-origin", "--data-binary", "@-"])
            for line in self.cookies.read_text().splitlines():
                fields = line.split("\t")
                if len(fields) == 7 and fields[5] == "gift_ledger_csrf":
                    args.extend(["-H", "X-CSRF-Token: " + fields[6]])
                    break
            payload = json.dumps(body).encode()
        result = subprocess.run(args, input=payload, capture_output=True, timeout=15)
        check(result.returncode == 0, f"HTTPS request {path} failed with curl status {result.returncode}")
        data, code = result.stdout.rsplit(b"\n", 1)
        check(int(code) == status, f"Expected HTTP {status} for {path}, got {code.decode()}")
        return data

    def api(self, path, body=None, status=200):
        return json.loads(self.request("/api/v1/" + path, body, status))


def add_gift(browser, contact, day, amount, remark):
    return browser.api("ledger/entries", {
        "host_contact_id": contact, "kinship_type_id": 1, "event_type_id": 1,
        "event_date": day, "event_remark": remark,
        "amount": amount, "gift_remark": remark,
    }, 201)["gift"]["id"]


def verify_ledger(browser, expected):
    ledger = browser.api("ledger/query/gifts")
    actual = {row["id"]: (Decimal(row["amount"]), row["remark"]) for row in ledger["items"]}
    totals = ledger["totals"]
    check(actual == expected and ledger["page"]["total"] == len(expected), "Ledger rows changed")
    check(totals["gift_count"] == len(expected) and Decimal(totals["income_amount"]) == 0
          and Decimal(totals["expense_amount"]) == sum(value[0] for value in expected.values()),
          "Ledger totals changed")


def start_proxy(work, name, host, public_port, lan_port, proxy_key):
    folder = work / "proxy"
    folder.mkdir(mode=0o700)
    cert = folder / "cert.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=" + host, "-addext", "subjectAltName=DNS:" + host,
        "-keyout", str(folder / "key.pem"), "-out", str(cert),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # A real, independently certified reverse proxy forwards the configured public
    # authority to the NAS HTTPS listener, including when the public entry is disabled.
    (folder / "nginx.conf").write_text(f"""
events {{}}
http {{
    server {{
        listen 127.0.0.1:{public_port} ssl;
        ssl_certificate /proxy/cert.pem;
        ssl_certificate_key /proxy/key.pem;
        location / {{
            proxy_pass https://127.0.0.1:{lan_port};
            proxy_set_header Host {host}:{public_port};
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-GiftLedger-Proxy-Key {proxy_key};
        }}
        location = /misconfigured-upstream {{
            proxy_pass https://127.0.0.1:{lan_port}/maintenance;
            proxy_set_header Host 127.0.0.1:{lan_port};
            proxy_set_header X-GiftLedger-Proxy-Key {proxy_key};
        }}
        location = /unverified-login {{
            proxy_pass https://127.0.0.1:{lan_port}/api/v1/auth/login;
            proxy_set_header Host {host}:{public_port};
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-GiftLedger-Proxy-Key "";
        }}
    }}
}}
""")
    container = subprocess.check_output([
        "docker", "create", "--name", name + "-proxy", "--network", "host", "--pull=never",
        "--mount", f"type=bind,src={folder},dst=/proxy,readonly", "--entrypoint", "nginx",
        local("web"), "-c", "/proxy/nginx.conf", "-g", "daemon off;",
    ], text=True).strip()
    return container, Browser(host, public_port, cert, work / "external-cookies")


def upgrade(old_yaml):
    check(sys.platform == "linux" and os.geteuid() == 0, "Run as root on a disposable Linux runner")
    with tempfile.TemporaryDirectory(prefix="giftledger-upgrade-") as temp, ExitStack() as cleanup:
        work = Path(temp)
        station = work / "site"
        station.mkdir()
        config = station / "docker-compose.yml"
        config.write_bytes(old_yaml.read_bytes())
        name = "giftledger-upgrade-" + secrets.token_hex(5)
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            lan_port, public_port = first.getsockname()[1], second.getsockname()[1]
        env = {**os.environ, "COMPOSE_PROJECT_NAME": name, "GIFTLEDGER_SITE_DIR": ".",
               "GIFTLEDGER_SITE_IP": "127.0.0.1", "GIFTLEDGER_HTTPS_PORT": str(lan_port),
               "GIFTLEDGER_REVERSE_PROXY_ORIGIN": ""}
        compose = ["docker", "compose", "--project-directory", str(station), "-f", str(config), "-p", name]
        cleanup.callback(subprocess.run, [*compose, "--profile", "maintenance", "down",
                                          "--volumes", "--remove-orphans"], env=env, check=True)

        def start():
            subprocess.run([*compose, "up", "-d"], env=env, check=True)
            roots = list((station / "data").glob(name + "-????????-本站根证书.cer"))
            check(len(roots) == 1, "Expected exactly one station root certificate")
            wait_ready(compose, env, f"https://127.0.0.1:{lan_port}", roots[0])
            return roots[0].read_bytes()

        original_root = start()
        ca = work / "site-ca.pem"
        ca.write_text(ssl.DER_cert_to_PEM_cert(original_root))
        lan = Browser("127.0.0.1", lan_port, ca, work / "lan-cookies")
        credentials = {"username": "upgrade_user", "password": secrets.token_urlsafe(24)}
        account = lan.api("auth/register", credentials, 201)["id"]
        lan.api("auth/login", credentials)
        lan.api("me/profile", {"name": "升级验收"}, 201)
        contact = lan.api("contacts", {"name": "验收联系人", "relationship_type_id": 3}, 201)["id"]
        old_gift = add_gift(lan, contact, "2025-01-20", "12.34", "升级前")
        expected = {old_gift: (Decimal("12.34"), "升级前")}
        verify_ledger(lan, expected)
        site = lan.api("site/about")
        check(site["project"] == name and site["origin"] == lan.origin, "Unexpected station binding")
        instance = str(UUID(site["instance_id"]))

        config.write_text(render({role: local(role) for role in ROLES}))
        check(start() == original_root, "Upgrade changed the station root certificate")
        check(lan.api("auth/login", credentials)["id"] == account, "Upgrade changed the account")
        verify_ledger(lan, expected)
        check(json.loads(lan.request("/site-access.json")) == {"maintenance": True}, "Missing LAN entry")
        host = "ledger-upgrade.test"
        proxy_key = secrets.token_hex(32)
        container, public = start_proxy(work, name, host, public_port, lan_port, proxy_key)
        cleanup.callback(subprocess.run, ["docker", "rm", "-f", container], check=True)
        subprocess.run(["docker", "start", container], check=True)
        for attempt in range(20):
            try:
                public.request("/", status=404)
                break
            except RuntimeError:
                if attempt == 19:
                    raise
                time.sleep(0.5)
        print("v1.8.1 upgraded, original account and ledger verified; public entry closed", flush=True)

        env["GIFTLEDGER_REVERSE_PROXY_ORIGIN"] = public.origin
        env["GIFTLEDGER_REVERSE_PROXY_KEY"] = proxy_key
        start()
        public.request("/")
        check(json.loads(public.request("/site-access.json")) == {"maintenance": False}, "Unexpected public tools")
        public.request("/api/v1/auth/me", status=401)
        rejected = json.loads(public.request("/unverified-login", credentials, status=403))
        check(rejected["error"]["code"] == "LOGIN_PROXY_UNVERIFIED", "Unverified proxy accepted")
        check(public.api("auth/login", credentials)["id"] == account, "External login returned a different account")
        verify_ledger(public, expected)
        external_gift = add_gift(public, contact, "2025-02-20", "56.78", "域名记账")
        expected[external_gift] = (Decimal("56.78"), "域名记账")
        for path in ("/maintenance", "/misconfigured-upstream", "/api/v1/maintenance/page",
                     "/api/v1/maintenance/access-methods", "/api/v1/maintenance/restore",
                     "/api/v1/admin/backups/database", "/api/v1/backups/personal"):
            public.request(path, status=404)
        verify_ledger(lan, expected)
        check(lan.api("site/about")["instance_id"] == instance, "The station instance changed")
        check("本站维护授权码" in lan.request("/maintenance").decode(), "Missing LAN maintenance page")
        lan.api("maintenance/access-methods")
        print("Independent proxy login and write verified, maintenance restricted to LAN", flush=True)

        env["GIFTLEDGER_REVERSE_PROXY_ORIGIN"] = ""
        start()
        public.request("/", status=404)
        public.request("/api/v1/auth/me", status=404)
        verify_ledger(lan, expected)
        check(lan.api("site/about")["instance_id"] == instance, "Disabling the entry changed the instance")
        print("Public entry closed again; both ledger rows and totals verified on LAN", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-yaml", required=True, type=Path)
    upgrade(parser.parse_args().from_yaml)
