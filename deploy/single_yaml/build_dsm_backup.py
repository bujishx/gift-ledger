"""Build the deterministic, standard-library-only DSM host backup zip application."""
import argparse
from pathlib import Path
import zipfile

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / 'deploy/single_yaml'
CORE = REPO / 'deploy/release/backup_ops'
SITE_FILES = ('dsm_backup.py', 'dsm_certificate.py', 'certificate_auto.py', 'certificate_policy.py', 'certificate_site.py',
              'backup_site.py', 'single_yaml_site.py', 'owner_guide.py', 'service_images.json')
CORE_FILES = ('backup_ops.py', 'backup_export.py', 'backup_store.py', 'windows_path_budget.py')
EVENT_FILES = ('__init__.py', 'target.py', 'catalog.py', 'io.py', 'adapter.py', 'provider.py', 'observer.py')


def build(output):
    members = {name: (SITE / name).read_bytes() for name in SITE_FILES}
    members.update({name: (CORE / name).read_bytes() for name in CORE_FILES})
    for name in EVENT_FILES:
        members['dsm_events/' + name] = (CORE / 'dsm_events' / name).read_bytes()
    members['__main__.py'] = b'from dsm_backup import main\n\nraise SystemExit(main())\n'
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    build(parser.parse_args().output)
