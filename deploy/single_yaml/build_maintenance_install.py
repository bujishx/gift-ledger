"""Bundle the site-owner's host installer with the identical release backup engine."""
from pathlib import Path
import argparse
import zipfile


def build(backup, output):
    root = Path(__file__).resolve().parent
    files = {name: (root / name).read_bytes() for name in
             ("maintenance_install.py", "maintenance_host.py", "owner_login_code.py", "ubuntu_certificate_timer.py")}
    files["dsm-backup.pyz"] = Path(backup).read_bytes()
    files["__main__.py"] = b"from maintenance_install import main\nraise SystemExit(main())\n"
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.backup, args.output)
