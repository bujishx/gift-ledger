"""Site-scoped maintenance requests. The DSM host worker owns the status and backup action."""

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from app.api.maintenance_access import HEADERS, require_owner
from app.services import maintenance_restore_protocol as restore_wire

router = APIRouter(prefix="/api/v1/maintenance", include_in_schema=False)
RESOURCES = Path(__file__).resolve().parents[2] / "resources"
OPERATION_ID = re.compile(r"[0-9a-f]{32}")
TOKEN = re.compile(r"[0-9a-f]{64}")


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern="^[0-9a-f]{32}$", min_length=32, max_length=32)
    after_operation_id: str | None = Field(default=None, pattern="^[0-9a-f]{32}$", max_length=32)


def unavailable() -> HTTPException:
    return HTTPException(
        503, "本站维护任务记录不可用，请按本站维护说明在服务器查看。", headers=HEADERS
    )


def authorized_root(request: Request) -> Path:
    if request.query_params:
        raise HTTPException(400, "请从维护页访问。", headers=HEADERS)
    root = request.app.state.maintenance_root
    if hasattr(request.app.state, "maintenance_owner_dir"):
        require_owner(request)
        return root
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    try:
        expected = (root / "owner.sha256").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise unavailable() from exc
    if not TOKEN.fullmatch(expected):
        raise unavailable()
    if not (
        scheme.lower() == "bearer"
        and TOKEN.fullmatch(token)
        and secrets.compare_digest(hashlib.sha256(token.encode("ascii")).hexdigest(), expected)
    ):
        raise HTTPException(403, "请核对本站维护授权码。", headers=HEADERS)
    return root


def read_status(root: Path) -> dict:
    try:
        record = json.loads((root / "status" / "result.json").read_bytes())
        state = record["state"]
        operation = record["operation_id"]
        project = os.environ["GIFTLEDGER_PROJECT"]
        if (
            record["format"] != "giftledger-maintenance-status-v1"
            or record["project"] != project
            or state not in {"idle", "running", "succeeded", "failed", "needs_review"}
            or (state == "idle" and operation is not None)
            or (operation is None and state not in {"idle", "needs_review"})
            or (operation is not None and not OPERATION_ID.fullmatch(operation))
        ):
            raise ValueError("Invalid maintenance status")
        # Only these public fields belong in the website. Worker diagnostics stay on the host.
        result = {"state": state, "operation_id": operation, "project": project}
        if state == "succeeded":
            if record["decryption_verified"] is not True or not OPERATION_ID.fullmatch(
                record["backup_id"]
            ):
                raise ValueError("Missing backup receipt")
            result.update(backup_id=record["backup_id"], decryption_verified=True)
            size = record.get("download_bytes")
            if type(size) is int and size > 0:
                result["download_bytes"] = size
            elif record.get("download_unavailable") is True:
                result["download_unavailable"] = True
        if state in {"failed", "needs_review"}:
            code = record.get("code", "")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code):
                result["code"] = code
        return result
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise unavailable() from exc


def queue_status(root: Path) -> dict:
    status = read_status(root)
    if status["state"] == "needs_review":
        return status
    # Never follow or wait on an invalid marker in this writable directory.
    try:
        marker = root / "requests" / "backup.request"
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return read_status(root)
    except OSError as exc:
        raise unavailable() from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 33:
            raise unavailable()
        line = os.read(fd, 34)
    except OSError as exc:
        raise unavailable() from exc
    finally:
        os.close(fd)
    status = read_status(root)
    if status["state"] == "needs_review":
        return status
    if not re.fullmatch(rb"[0-9a-f]{32}\n", line):
        raise unavailable()
    pending_id = line[:-1].decode("ascii")
    if pending_id != status["operation_id"]:
        return {"state": "queued", "operation_id": pending_id, "project": status["project"]}
    return status


@contextmanager
def submission_lock(directory: Path):
    # Only opted-in Linux server deployments enter this route. Lock across all API processes.
    import fcntl

    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def publish_request(root: Path, operation_id: str, directory_fd: int) -> None:
    directory = root / "requests"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".request-", delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(operation_id.encode("ascii") + b"\n")
            temp.flush()
            os.fsync(temp.fileno())
        try:
            os.link(temp_path, directory / "backup.request")
        except FileExistsError:
            # Re-read the winning operation below to distinguish a retry from a competing click.
            pass
        os.fsync(directory_fd)
    except OSError as exc:
        raise unavailable() from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@router.get("/page")
def maintenance_page():
    return FileResponse(
        RESOURCES / "site_maintenance.html", media_type="text/html", headers=HEADERS
    )


@router.get("/page.js")
def maintenance_script():
    return FileResponse(
        RESOURCES / "site_maintenance.js", media_type="text/javascript", headers=HEADERS
    )


@router.get("/page.css")
def maintenance_style():
    return FileResponse(RESOURCES / "site_maintenance.css", media_type="text/css", headers=HEADERS)


@router.get("/status")
def maintenance_status(request: Request):
    return JSONResponse(queue_status(authorized_root(request)), headers=HEADERS)


@router.post("/backup")
def request_backup(body: BackupRequest, request: Request):
    root = authorized_root(request)
    origin = request.headers.get("origin", "")
    if (
        not origin.startswith("https://")
        or origin not in request.app.state.settings.trusted_origin_list
        or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
        or request.headers.get("content-type", "").partition(";")[0].lower() != "application/json"
    ):
        raise HTTPException(403, "请从本站 HTTPS 维护页操作。", headers=HEADERS)
    try:
        with submission_lock(root / "requests") as directory_fd:
            status = queue_status(root)
            if status["state"] == "needs_review":
                return JSONResponse(status, status_code=409, headers=HEADERS)
            if status["operation_id"] != body.operation_id:
                if restore_wire.busy(root):
                    raise HTTPException(
                        409, "本站已有恢复任务，请先完成或取消恢复。", headers=HEADERS
                    )
                if status["operation_id"] != body.after_operation_id or status["state"] in {
                    "queued",
                    "running",
                }:
                    return JSONResponse(status, status_code=409, headers=HEADERS)
                publish_request(root, body.operation_id, directory_fd)
                status = queue_status(root)
    except OSError as exc:
        raise unavailable() from exc
    if status["operation_id"] != body.operation_id:
        return JSONResponse(status, status_code=409, headers=HEADERS)
    return JSONResponse(
        status,
        status_code=202 if status["state"] in {"queued", "running"} else 200,
        headers=HEADERS,
    )


@router.get("/backups/{backup_id}/download")
def download_backup(backup_id: str, request: Request):
    if not hasattr(request.app.state, "maintenance_owner_dir"):
        raise HTTPException(404, headers=HEADERS)
    require_owner(request)
    root = request.app.state.maintenance_root
    current = read_status(root)
    if (
        not OPERATION_ID.fullmatch(backup_id)
        or current.get("backup_id") != backup_id
        or not current.get("download_bytes")
    ):
        raise HTTPException(404, "请刷新备份结果，选择已核验的备份。", headers=HEADERS)
    source = None
    try:
        fd = os.open(root / "status" / "download.zip", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        source = os.fdopen(fd, "rb")
        meta = os.fstat(fd)
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_size != current["download_bytes"]
            or meta.st_uid != (root / "status").stat().st_uid
            or meta.st_mode & 0o022
        ):
            raise ValueError()
        with zipfile.ZipFile(source) as archive:
            expected = {
                "BUNDLE.json",
                "SHA256SUMS",
                "verify.json",
                "backup.json",
                f"backup-{backup_id}.glgui.age",
            }
            if len(archive.namelist()) != 5 or set(archive.namelist()) != expected:
                raise ValueError()
        source.seek(0)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if source is not None:
            source.close()
        raise HTTPException(409, "备份下载文件已变化，请刷新备份结果。", headers=HEADERS) from exc

    def chunks():
        with source:
            yield from iter(lambda: source.read(256 * 1024), b"")

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={
            **HEADERS,
            "Content-Disposition": f'attachment; filename="{backup_id}.zip"',
            "Content-Length": str(meta.st_size),
        },
        background=BackgroundTask(source.close),
    )
