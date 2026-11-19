"""The web restore dispatcher accepts only the selected site's original operation and receipt."""
from pathlib import Path
import os
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX server IPC")


@pytest.fixture
def queue(monkeypatch):
    for folder in ("backend/app/services", "deploy/single_yaml", "deploy/release/backup_ops"):
        monkeypatch.syspath_prepend(str(ROOT / folder))
    import maintenance_restore_queue
    return maintenance_restore_queue


def test_snapshot_preserves_original_bytes_and_rejects_incomplete_file_set(queue, tmp_path):
    src = tmp_path / "upload"
    src.mkdir(mode=0o700)
    dst = tmp_path / "private"
    dst.mkdir(mode=0o700)
    (src / "input.zip").write_bytes(b"example")
    (src / "input.zip").chmod(0o600)
    fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert queue.snapshot(fd, dst, os.getuid()).read_bytes() == b"example"
        (src / "input.zip").rename(src / "BUNDLE.json")
        with pytest.raises(ValueError, match="restore_upload_files_invalid"):
            queue.snapshot(fd, dst, os.getuid())
    finally:
        os.close(fd)


@pytest.fixture
def job(queue, tmp_path):
    record = {"public": {"operation_id": "a"*32, "state": "eligible", "preview": {"target_project": "target"}},
              "deadline": 9999999999, "bundle": {"id": "b"*32, "digest": "c"*64}}
    return queue.Task(Mock(), tmp_path, {"requests": 33, "status": 34}, {"station": str(tmp_path)}, record, Mock(), "image")


def test_wrong_confirmation_never_runs_engine(queue, job):
    forged = {"operation_id": job.op, "preview": {"target_project": "source"}}
    with patch.object(queue.enroll, "sealed_packet", return_value=forged), patch.object(job, "stopped", return_value=False), \
            patch.object(job, "apply") as apply, pytest.raises(ValueError, match="restore_confirmation_target_changed"):
        job.confirm()
    apply.assert_not_called()
    assert "confirmed" not in job.record


def test_pending_confirmation_is_idle_until_owner_confirms(queue, job):
    with patch.object(queue.enroll, "sealed_packet", return_value=None), patch.object(job, "stopped", return_value=False), \
            patch.object(job, "apply") as apply:
        assert job.confirm() is False
        apply.assert_not_called()
        original = {"operation_id": job.op, "preview": dict(job.public["preview"])}
        with patch.object(queue.enroll, "sealed_packet", return_value=original):
            assert job.confirm() is True
            assert job.record["confirmed"] and job.public["state"] == "applying"
            apply.assert_called_once()


def test_host_restart_during_apply_requires_original_job_review(queue, job):
    job.public["state"] = "applying"
    with patch.object(queue.enroll, "marker", return_value=job.op), patch.object(queue.Task, "apply") as apply:
        assert queue.tick(job.host, job.state, job.handles, job.cfg, job.record, job.archive, job.image)
        assert job.public["state"] == "needs_review"
        apply.assert_not_called()
