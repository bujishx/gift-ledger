"""Stage release assets in a draft and publish only after byte-for-byte readback."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from images import has_windows_tools

EXPECTED = {
    "docker-compose.yml",
    "mac-site-tools.zip",
    "windows-site-tools.zip",
    "Install.en.md",
    "Install.zh.md",
}


class GitHub:
    def __init__(self, repository):
        self.repository = repository
        self.rest = "repos/" + repository + "/releases"

    def api(self, method, path, body=None):
        cmd = ["gh", "api", "-X", method, path]
        raw = None
        if body is not None:
            cmd += ["--input", "-"]
            raw = json.dumps(body).encode()
        result = subprocess.check_output(cmd, input=raw)
        return json.loads(result) if result else None

    def releases(self, tag):
        pages = subprocess.check_output(
            ["gh", "api", "--paginate", "--slurp", self.rest + "?per_page=100"]
        )
        return [
            item
            for page in json.loads(pages)
            for item in page
            if item["tag_name"] == tag
        ]

    def create(self, metadata):
        return self.api("POST", self.rest, dict(metadata, draft=True))

    def get(self, rid):
        return self.api("GET", self.rest + "/" + str(rid))

    def read_asset(self, aid):
        return subprocess.check_output(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/octet-stream",
                self.rest + "/assets/" + str(aid),
            ]
        )

    def clear_incomplete(self, aid):
        path = self.rest + "/assets/" + str(aid)
        item = self.api("GET", path)
        if (
            item["state"] != "starter"
            or item["size"] != 0
            or item["uploader"]["login"] != "github-actions[bot]"
        ):
            raise ValueError("A draft asset changed; preserve it")
        self.api("DELETE", path)

    def upload(self, tag, path):
        subprocess.run(
            ["gh", "release", "upload", tag, str(path), "--repo", self.repository],
            check=True,
        )

    def publish(self, rid):
        self.api("PATCH", self.rest + "/" + str(rid), {"draft": False})


def deliver(service, version, commit, folder):
    if not re.fullmatch(
        r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
    ) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid release identity")
    files = {p.name: p for p in folder.iterdir() if p.is_file() and not p.is_symlink()}
    expected = EXPECTED if has_windows_tools() else EXPECTED - {"windows-site-tools.zip"}
    if set(files) != expected or len(list(folder.iterdir())) != len(expected):
        raise ValueError("Unexpected release assets")
    base = f"https://github.com/{service.repository}/releases/download/{version}/"
    metadata = {
        "tag_name": version,
        "target_commitish": commit,
        "name": version,
        "body": f"Linux/amd64 · Docker Compose / Synology Container Manager. [安装]({base}Install.zh.md) · [Install]({base}Install.en.md)",
    }
    matches = service.releases(version)
    if len(matches) > 1:
        raise ValueError("Ambiguous existing release")
    release = matches[0] if matches else service.create(metadata)

    def inspect(item):
        if any(item.get(k) != value for k, value in metadata.items()):
            raise ValueError("This release belongs to a different candidate")
        checked = set()
        incomplete = []
        for asset in item["assets"]:
            name = asset["name"]
            if name not in files or name in checked:
                raise ValueError("Foreign or duplicate asset in release")
            if (
                asset["state"] == "starter"
                and asset["size"] == 0
                and item["draft"]
                and asset["uploader"]["login"] == "github-actions[bot]"
            ):
                incomplete.append(asset["id"])
                continue
            if (
                asset["state"] != "uploaded"
                or asset["size"] != files[name].stat().st_size
                or hashlib.sha256(service.read_asset(asset["id"])).digest()
                != hashlib.sha256(files[name].read_bytes()).digest()
            ):
                raise ValueError("Existing release asset has different bytes: " + name)
            checked.add(name)
        return checked, incomplete

    current, stubs = inspect(release)
    if not release["draft"]:
        if current != expected:
            raise ValueError("Published release has missing assets")
        return
    for asset in stubs:
        service.clear_incomplete(asset)
    for missing in sorted(expected - current):
        service.upload(version, files[missing])
    reread = service.get(release["id"])
    if not reread["draft"] or inspect(reread) != (expected, []):
        raise ValueError("Draft readback incomplete")
    service.publish(release["id"])
    final = service.get(release["id"])
    if final["draft"] or inspect(final) != (expected, []):
        raise ValueError("Published release readback incomplete")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("assets", type=Path)
    p.add_argument("version")
    p.add_argument("commit")
    args = p.parse_args()
    deliver(
        GitHub(os.environ["GITHUB_REPOSITORY"]), args.version, args.commit, args.assets
    )
