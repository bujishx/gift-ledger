"""Install the pinned upstream age release into a Linux/amd64 tool image."""
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "v1.3.2"
SHA256 = "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"
URL = f"https://github.com/FiloSottile/age/releases/download/{VERSION}/age-{VERSION}-linux-amd64.tar.gz"


def main():
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise SystemExit("This maintenance release supports Linux/amd64 only.")
    with urllib.request.urlopen(URL, timeout=120) as response:
        content = response.read(32 * 1024 * 1024 + 1)
    if len(content) > 32 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != SHA256:
        raise SystemExit("The pinned age release failed its integrity check.")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for name in ("age", "age-keygen"):
            member = archive.getmember(f"age/{name}")
            if not member.isfile() or member.size > 32 * 1024 * 1024:
                raise SystemExit("Unexpected executable in the age release.")
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit("Missing executable in the age release.")
            path = Path("/usr/local/bin") / name
            with path.open("xb") as output:
                output.write(source.read())
            os.chmod(path, 0o755)


if __name__ == "__main__":
    main()
