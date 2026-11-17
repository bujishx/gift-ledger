"""Build a Linux/amd64 candidate once, then publish the tested OCI manifests."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("GIFTLEDGER_PUBLIC_SOURCE", HERE.parents[1])).resolve()
ROLES = ("api", "web", "db", "installer", "backup")
REF = re.compile(
    r"^(\s*image: )giftledger-candidate/(api|web|db|installer|backup):[a-zA-Z0-9_.-]+$",
    re.MULTILINE,
)
PIN = re.compile(r"sha256:[a-f0-9]{64}")
BASES = {
    "mysql": "mysql:8.4.11-oraclelinux9",
    "node": "node:22-alpine",
    "nginx": "nginx:stable-alpine",
}


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def output(*args):
    return subprocess.check_output(list(map(str, args)))


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def selected_file(path):
    path = ROOT / path
    if not path.is_file() or path.resolve() != path or not path.is_relative_to(ROOT):
        raise ValueError(f"Expected a regular reviewed source file: {path.name}")
    return path.read_bytes()


def local(role):
    return "giftledger-ci/" + role + ":candidate"


def render(refs):
    original = selected_file("deploy/single_yaml/docker-compose.yml").decode()
    if set(refs) != set(ROLES) or {m[2] for m in REF.finditer(original)} != set(ROLES):
        raise ValueError("Compose image roles differ from the build")
    result = REF.sub(lambda m: m[1] + refs[m[2]], original)
    if "giftledger-candidate/" in result:
        raise ValueError("Unresolved Compose reference")
    return result


def verify_layout(layout, role):
    index = json.loads((layout / "index.json").read_bytes())
    matches = [
        entry
        for entry in index["manifests"]
        if entry.get("annotations", {}).get("org.opencontainers.image.ref.name") == role
    ]
    if len(matches) != 1 or not PIN.fullmatch(matches[0]["digest"]):
        raise ValueError("Missing or ambiguous OCI image reference")
    entry = matches[0]
    manifest = (layout / "blobs/sha256" / entry["digest"][7:]).read_bytes()
    if digest(manifest) != entry["digest"] or len(manifest) != entry["size"]:
        raise ValueError("Invalid OCI manifest descriptor")
    record = json.loads(manifest)
    if record.get("mediaType") != "application/vnd.oci.image.manifest.v1+json":
        raise ValueError("Expected a single OCI image")
    for entry in [record["config"], *record["layers"]]:
        if not PIN.fullmatch(entry["digest"]):
            raise ValueError("Invalid OCI descriptor")
        blob = layout / "blobs/sha256" / entry["digest"].split(":")[1]
        if blob.stat().st_size != entry["size"]:
            raise ValueError("OCI blob size mismatch")
        with blob.open("rb") as f:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
            if "sha256:" + hasher.hexdigest() != entry["digest"]:
                raise ValueError("OCI blob mismatch")
    config = json.loads(
        (layout / "blobs/sha256" / record["config"]["digest"][7:]).read_bytes()
    )
    if (config["os"], config["architecture"]) != ("linux", "amd64"):
        raise ValueError("Unexpected platform")
    return {"manifest": digest(manifest), "config": record["config"]["digest"]}


MANIFEST = "SOURCE-FILES.json"
DOT_PATHS = {
    ".dockerignore",
    ".gitignore",
    ".github/workflows/candidate.yml",
    ".github/workflows/release.yml",
}


def reviewed_manifest(source):
    manifest = source / MANIFEST
    if manifest.resolve() != manifest or not manifest.is_file():
        raise ValueError("Expected the reviewed source manifest.")

    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate reviewed path")
        return result

    contents = manifest.read_bytes()
    files = json.loads(contents, object_pairs_hook=unique)
    if not isinstance(files, dict) or not DOT_PATHS.issubset(files):
        raise ValueError("Incomplete source manifest")
    for name, checksum in files.items():
        path = Path(name)
        if (
            not path.parts
            or path.is_absolute()
            or path.as_posix() != name
            or name == MANIFEST
            or ".." in path.parts
            or "\\" in name
            or not name.isprintable()
            or (
                any(part.startswith(".") for part in path.parts)
                and name not in DOT_PATHS
            )
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise ValueError(f"Invalid reviewed path or checksum: {name!r}")
    return contents, files


def reviewed_bytes(source, name, checksum):
    path = source / name
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"Missing or linked reviewed file: {name}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError(f"Reviewed file changed: {name}")
    return data


def stage_source(source, destination):
    """Copy only the selected commit's reviewed bytes, leaving its checkout untouched."""
    source = source.resolve(strict=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    if os.path.lexists(destination) or destination.is_relative_to(source):
        raise ValueError("Stage into a new directory outside the checkout.")
    contents, files = reviewed_manifest(source)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        tree = Path(scratch) / "source"
        tree.mkdir()
        for name, checksum in files.items():
            data = reviewed_bytes(source, name, checksum)
            out = tree / name
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("xb") as handle:
                handle.write(data)
        (tree / MANIFEST).write_bytes(contents)
        tree.rename(destination)


def assert_export():
    _, files = reviewed_manifest(ROOT)
    expected = set(files) | {MANIFEST}
    for path in ROOT.rglob("*"):
        if path.is_symlink() or (
            not path.is_dir() and path.relative_to(ROOT).as_posix() not in expected
        ):
            raise ValueError("Build from the isolated reviewed source directory.")
    for name, checksum in files.items():
        reviewed_bytes(ROOT, name, checksum)


def build_candidate(out, commit):
    assert_export()
    if not re.fullmatch("[0-9a-f]{40}", commit) or out.exists():
        raise ValueError("Expected a new output and a commit SHA")
    out.mkdir(parents=True)
    layout = out / "images"

    def build(role, filename, **args):
        flags = [v for k, a in args.items() for v in ("--build-arg", k + "=" + a)]
        run(
            "docker",
            "build",
            "--platform=linux/amd64",
            "-t",
            local(role),
            "-f",
            filename,
            *flags,
            ROOT,
        )

    resolved = {}
    for key, image in BASES.items():
        upstream = "docker.io/library/" + image
        run("docker", "pull", "--platform=linux/amd64", upstream)
        ref = (
            output(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{index .RepoDigests 0}}",
                upstream,
            )
            .decode()
            .strip()
        )
        if not PIN.fullmatch(ref.split("@")[-1]):
            raise ValueError("Unresolved base")
        resolved[key] = upstream.split(":")[0] + "@" + ref.split("@")[1]

    def pinned_dockerfile(src, pairs, name):
        data = src.read_text()
        for old, new in pairs:
            if data.count("FROM " + old) != 1:
                raise ValueError("Expected base declaration changed")
            data = data.replace("FROM " + old, "FROM " + new)
        if name == "web.build":
            expected = "RUN pnpm run build"
            if data.count(expected) != 1:
                raise ValueError("Web build step changed")
            data = data.replace(
                expected, "RUN node --test tests/*.test.mjs && pnpm run build"
            )
        dest = out / name
        dest.write_text(data)
        return dest

    api_file = pinned_dockerfile(
        ROOT / "deploy/api.Dockerfile",
        [(BASES["mysql"], resolved["mysql"])],
        "api.build",
    )
    web_file = pinned_dockerfile(
        ROOT / "deploy/web.Dockerfile",
        [(BASES["node"], resolved["node"]), (BASES["nginx"], resolved["nginx"])],
        "web.build",
    )
    try:
        build("api", api_file)
        build("web", web_file)
    finally:
        api_file.unlink(missing_ok=True)
        web_file.unlink(missing_ok=True)
    run("docker", "tag", resolved["mysql"], local("db"))
    records = {}

    def freeze(role):
        run("skopeo", "copy", "docker-daemon:" + local(role), f"oci:{layout}:{role}")
        records[role] = verify_layout(layout, role)

    for role in ("api", "web", "db"):
        freeze(role)
    guard = ROOT / "deploy/single_yaml/service_images.json"
    old = guard.read_bytes()
    try:
        guard.write_text(
            json.dumps(
                {
                    role: sorted(set(records[role].values()))
                    for role in ("api", "web", "db")
                },
                indent=2,
            )
            + "\n"
        )
        build(
            "maintenance-base",
            ROOT / "deploy/maintenance/Dockerfile",
            API_IMAGE=local("api"),
        )
        build("clients", HERE / "docker-client.Dockerfile", BASE=local("api"))
        build(
            "installer",
            ROOT / "deploy/single_yaml/installer.Dockerfile",
            BASE=local("maintenance-base"),
        )
        build(
            "backup",
            ROOT / "deploy/single_yaml/backup.Dockerfile",
            CLIENTS=local("clients"),
            BASE=local("installer"),
        )
    finally:
        guard.write_bytes(old)
    for role in ("installer", "backup"):
        freeze(role)
    for role in ROLES:
        # Run the release-format OCI content, including regenerated transport metadata.
        run("skopeo", "copy", f"oci:{layout}:{role}", "docker-daemon:" + local(role))
        actual = (
            output("docker", "image", "inspect", "--format", "{{.Id}}", local(role))
            .decode()
            .strip()
        )
        if actual not in records[role].values():
            raise ValueError("Loaded image does not match OCI image")
    (out / "CANDIDATE.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "source_manifest_sha256": digest(selected_file(MANIFEST)),
                "platform": "linux/amd64",
                "images": records,
                "resolved_base_images": resolved,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def verify(out, commit):
    candidate = json.loads((out / "CANDIDATE.json").read_bytes())
    if (
        not re.fullmatch("[0-9a-f]{40}", commit)
        or candidate["commit"] != commit
        or candidate.get("source_manifest_sha256") != digest(selected_file(MANIFEST))
        or set(candidate["images"]) != set(ROLES)
    ):
        raise ValueError("Candidate commit, reviewed source or image set mismatch")
    for role in ROLES:
        if verify_layout(out / "images", role) != candidate["images"][role]:
            raise ValueError("Candidate changed")
    return candidate["images"]


def release_files(out, refs, repository, version):
    out.mkdir(parents=True, exist_ok=False)
    yaml = f"# {version} · Linux/amd64\n" + render(refs).replace(
        "单文件安装候选版", "单文件安装", 1
    )
    (out / "docker-compose.yml").write_text(yaml)
    with zipfile.ZipFile(
        out / "mac-site-tools.zip", "x", compression=zipfile.ZIP_DEFLATED
    ) as z:

        def member(name, data):
            entry = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            z.writestr(entry, data)

        for name in ("mac_backup_key.py", "mac_restore.py", "mac_restore_bundle.py"):
            member(name, selected_file(Path("deploy/single_yaml") / name))
        for name in ("backup_store.py", "backup_export.py"):
            member(name, selected_file(Path("deploy/release/backup_ops") / name))
        for guide in ("从备份恢复.md", "站主找回账号.md", "站主升级.md"):
            member(guide, selected_file(Path("docs") / guide))
        member("docker-compose.yml", yaml.encode())
    for lang in ("zh", "en"):
        page = (
            selected_file(f"deploy/publication/README.{lang}.md")
            .decode()
            .replace("{GITHUB_REPOSITORY}", repository)
        )
        page = page.replace(
            "/releases/latest/download/", "/releases/download/" + version + "/"
        )
        assets = f"https://github.com/{repository}/releases/download/{version}/"
        page = page.replace("](README.en.md)", f"]({assets}Install.en.md)").replace(
            "](README.md)", f"]({assets}Install.zh.md)"
        )
        page = page.replace(
            "](docs/", f"](https://github.com/{repository}/blob/{version}/docs/"
        )
        (out / f"Install.{lang}.md").write_text(page)


def publish(out, commit, namespace, version):
    records = verify(out, commit)
    if not re.fullmatch("[a-z0-9][a-z0-9_-]{1,29}", namespace) or not re.fullmatch(
        r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
    ):
        raise ValueError("Expected Docker Hub namespace and vMAJOR.MINOR.PATCH")
    refs = {role: f"docker.io/{namespace}/giftledger-{role}" for role in ROLES}
    # Refuse all collisions before writing the first repository.
    existing = set()
    for role, repo in refs.items():
        tag = repo + ":" + version
        p = subprocess.run(
            ["skopeo", "inspect", "--raw", "docker://" + tag], capture_output=True
        )
        if p.returncode == 0:
            if digest(p.stdout) != records[role]["manifest"]:
                raise ValueError(
                    "Version already exists with different content: " + role
                )
            existing.add(role)
        elif not any(
            s in p.stderr.lower()
            for s in (b"manifest unknown", b"name unknown", b"manifest_unknown")
        ):
            raise RuntimeError("Could not check the registry for " + role)
    for role, repo in refs.items():
        if role not in existing:
            run(
                "skopeo",
                "copy",
                "--preserve-digests",
                f"oci:{out / 'images'}:{role}",
                "docker://" + repo + ":" + version,
            )
        if (
            digest(
                output("skopeo", "inspect", "--raw", "docker://" + repo + ":" + version)
            )
            != records[role]["manifest"]
        ):
            raise ValueError("Published manifest changed: " + role)
    return {r: repo + "@" + records[r]["manifest"] for r, repo in refs.items()}


if __name__ == "__main__":
    if sys.argv[1:2] == ["stage-source"]:
        stage = argparse.ArgumentParser(description=stage_source.__doc__)
        stage.add_argument("checkout", type=Path)
        stage.add_argument("destination", type=Path)
        selection = stage.parse_args(sys.argv[2:])
        stage_source(selection.checkout, selection.destination)
        sys.exit(0)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["build", "verify", "publish"])
    parser.add_argument("out", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version")
    parser.add_argument("--namespace")
    parser.add_argument("--assets", type=Path)
    args = parser.parse_args()
    if args.action == "build":
        build_candidate(args.out.absolute(), args.commit)
    elif args.action == "verify":
        verify(args.out, args.commit)
    else:
        refs = publish(args.out, args.commit, args.namespace, args.version)
        release_files(args.assets, refs, os.environ["GITHUB_REPOSITORY"], args.version)
