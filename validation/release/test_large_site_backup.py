"""Check the production streaming path and its stop-before-offline space decisions."""
from contextlib import nullcontext
import gzip
import hashlib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/release/gui_v2"))
import maintenance as core


def test_large_sql_uses_compressed_scratch_and_streams_a_checked_import(monkeypatch, tmp_path):
    mib = 1024 ** 2
    block = (b"INSERT INTO ledger VALUES (123);\n" * 32768)[:mib]
    count = 260
    class Dump:
        remaining = count

        def read(self, limit):
            assert limit == mib
            if self.remaining:
                self.remaining -= 1
                return block
            return b""

    monkeypatch.setattr(core, "mysql_stream", lambda *args, **kwargs: nullcontext(Dump()))
    monkeypatch.setattr(core, "mysql_defaults", lambda *args: tmp_path / "fixture.cnf")
    compressed = tmp_path / "dump.sql.gz"
    with compressed.open("xb") as output:
        size, digest, stored = core.compressed_dump(tmp_path, {"passwords": {"mysql_root_password": ""}}, output)
    assert size == len(block) * count > 256 * mib
    assert stored == compressed.stat().st_size < mib
    package = tmp_path / "sample.gladminbackup"
    with ZipFile(package, "w") as archive:
        archive.write(compressed, "dump.sql.gz")
    class Digest:
        def __init__(self):
            self.hash = hashlib.sha256()

        def write(self, data):
            assert len(data) <= 64 * 1024
            self.hash.update(data)

    restored = Digest()
    core.stream_sql(package, SimpleNamespace(sql_size=size, sql_sha256=digest), restored)
    assert restored.hash.hexdigest() == digest
    assert {p.name for p in tmp_path.iterdir()} == {"dump.sql.gz", "sample.gladminbackup"}


@pytest.mark.parametrize("prefix", [b"", b" " * 65533, b"\t" * 131072])
def test_sql_directive_rejected_even_across_chunk_boundaries(tmp_path, prefix):
    content = prefix + b"sourCE example\n"
    archive = tmp_path / "fixture"
    with ZipFile(archive, "w") as z:
        z.writestr("dump.sql.gz", gzip.compress(content))
    metadata = SimpleNamespace(sql_size=len(content), sql_sha256=hashlib.sha256(content).hexdigest())
    with pytest.raises(core.GuiMaintenanceError, match="sql_client_directive_forbidden"):
        core.stream_sql(archive, metadata)


def test_early_capacity_check_preserves_source_and_uses_compressed_size(tmp_path, monkeypatch):
    app, assets, store = (tmp_path / name for name in ("app", "assets", "out"))
    for d in (app, assets, store):
        d.mkdir()
    (assets / "SHA256SUMS").write_bytes(b"fixture")
    monkeypatch.setattr(core, "APP_DATA", app)
    monkeypatch.setattr(core, "ASSETS", assets)
    monkeypatch.setattr(core, "EXCHANGE", store)
    monkeypatch.setattr(core, "compressed_dump", lambda *args: (300 * 1024**2, "unused", 8 * 1024**2))
    free = {tmp_path: 128 * 1024**2, store: 128 * 1024**2}
    monkeypatch.setattr(core.shutil, "disk_usage", lambda path: SimpleNamespace(free=free[path]))
    context = {"legacy": SimpleNamespace(ASSET_DIGESTS={})}
    assert 12 * 1024**2 < core.backup_capacity(tmp_path, context) < 13 * 1024**2
    free[tmp_path] = 20 * 1024**2
    with pytest.raises(core.GuiMaintenanceError, match="backup_private_workspace_too_small"):
        core.backup_capacity(tmp_path, context)
    free[tmp_path], free[store] = 128 * 1024**2, 2 * 1024**2
    with pytest.raises(core.GuiMaintenanceError, match="backup_destination_space_low"):
        core.backup_capacity(tmp_path, context)
