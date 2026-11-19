"""Receive an owner-selected backup and publish commands for this site's host worker."""

import os
import stat
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.maintenance_access import HEADERS, require_owner_for_content
from app.api.routes import maintenance_enrollment as enrollment
from app.api.routes.site_maintenance import queue_status, submission_lock, unavailable
from app.services import maintenance_enrollment_protocol as wire
from app.services import maintenance_restore_protocol as restore

router = APIRouter(prefix="/api/v1/maintenance/restore", include_in_schema=False)
RESERVE = 1024**2


def reject(message="恢复请求无效，请重新核对所选备份。", code=400):
    return HTTPException(code, message, headers=HEADERS)


def conflict():
    return reject("本站任务或已上传的内容与本次请求不符，请查看恢复状态。", 409)


@contextmanager
def locked(request, content="application/json"):
    sessions = require_owner_for_content(request, content)
    root = request.app.state.maintenance_root
    try:
        with submission_lock(root / "requests") as fd:
            yield root, sessions.binding, fd
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise unavailable() from exc


def reply(data):
    code = 202 if data["state"] in restore.ACTIVE | {"uploading"} else 200
    return JSONResponse(data, status_code=code, headers=HEADERS)


def check_id(value):
    if not wire.matches(wire.OP, value):
        raise reject()


def upload(root, marker="restore.upload"):
    raw = restore.read(root / "requests" / marker)
    if raw is None:
        return None
    data = wire.unpack(raw)
    wire.check(
        data.keys() == {"operation_id", "files"} and wire.matches(wire.OP, data["operation_id"])
    )
    restore.files(data["files"])
    return data


def folder(root, op):
    return root / "requests" / ("restore-upload-" + op)


@contextmanager
def upload_dir(root, op):
    fd = os.open(folder(root, op), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        wire.check(meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o700)
        yield fd
    finally:
        os.close(fd)


def member_size(fd, item):
    try:
        meta = os.stat(item["name"], dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    wire.check(
        stat.S_ISREG(meta.st_mode)
        and stat.S_IMODE(meta.st_mode) == 0o600
        and meta.st_nlink == 1
        and meta.st_uid == os.geteuid()
        and meta.st_size <= item["size"]
    )
    return meta.st_size


def finish_discard(root):
    metadata = upload(root, "restore.discard")
    if not metadata:
        return
    op = metadata["operation_id"]
    path = folder(root, op)
    try:
        with upload_dir(root, op) as directory:
            permitted = {i["name"] for i in metadata["files"]}
            found = os.listdir(directory)
            wire.check(set(found) <= permitted)
            for name in found:
                os.unlink(name, dir_fd=directory)
        path.rmdir()
    except FileNotFoundError:
        pass
    (root / "requests/restore.discard").unlink()


def state(root, binding, op=None):
    project, instance = binding[:2]
    finish_discard(root)
    if op:
        check_id(op)
        previous = restore.read(root / "status" / ("restore-" + op + ".json"))
        if previous:
            record = restore.public(previous, binding)
            wire.check(record["operation_id"] == op)
            return record
    raw = restore.read(root / "status/restore.json")
    if raw is None:
        raise reject("本站尚未启用网页恢复。", 404)
    current = restore.public(raw, binding)
    pending = restore.read(root / "requests/restore.request", 33)
    pending_id = None
    if pending:
        pending_id = pending.decode("ascii").removesuffix("\n")
        wire.check(wire.matches(wire.OP, pending_id) and len(pending) == 33)
    metadata = upload(root)
    if metadata:
        uploading_id = metadata["operation_id"]
        if pending_id == uploading_id or current["operation_id"] == uploading_id:
            # Recover a seal published before its staging marker was retired.
            (root / "requests/restore.upload").unlink()
        else:
            wire.check(pending_id is None and current["state"] not in restore.BLOCKED)
            with upload_dir(root, uploading_id) as directory:
                current = dict(
                    project=project,
                    instance_id=instance,
                    operation_id=uploading_id,
                    state="uploading",
                    files=[{**i, "received": member_size(directory, i)} for i in metadata["files"]],
                )
    if pending_id and current["operation_id"] != pending_id:
        wire.check(current["state"] not in restore.BLOCKED)
        current = dict(
            project=project, instance_id=instance, operation_id=pending_id, state="queued"
        )
    if op and current["operation_id"] != op:
        raise reject("本站没有这次恢复任务。", 404)
    return current


@router.get("")
@router.get("/{operation}")
def status(request: Request, operation: str | None = None):
    if operation is not None:
        check_id(operation)
    with locked(request) as (root, binding, _):
        return reply(state(root, binding, operation))


def free_space(fd, wanted):
    fs = os.fstatvfs(fd)
    if fs.f_bavail * fs.f_frsize < wanted + RESERVE:
        raise reject("目标站可用空间不足，请先腾出空间再上传备份。", 507)


@router.post("")
async def start(request: Request):
    require_owner_for_content(request, "application/json")
    data = await enrollment.body(request, 2048)
    op = data.get("operation_id")
    check_id(op)
    if data.keys() != {"operation_id", "files"}:
        raise reject()
    try:
        files = restore.files(data["files"])
    except (TypeError, ValueError) as exc:
        raise reject() from exc
    with locked(request) as (root, binding, fd):
        current = state(root, binding)
        if current["operation_id"] == op:
            if current["state"] == "uploading" and upload(root)["files"] != files:
                raise conflict()
            return reply(current)
        # Previous operations are immutable, even after a newer job starts.
        if (root / "status" / ("restore-" + op + ".json")).exists():
            return reply(state(root, binding, op))
        if (
            current["state"] in restore.BLOCKED | {"uploading"}
            or (root / "requests/restore.request").exists()
        ):
            raise conflict()
        if (
            queue_status(root)["state"] in {"queued", "running", "needs_review"}
            or (root / "requests/backup.request").exists()
        ):
            raise reject("本站已有备份任务，请先完成备份。", 409)
        if (
            enrollment.get_state(root, binding[:2])["state"] in wire.ACTIVE | {"needs_review"}
            or (root / "requests/enroll.request").exists()
        ):
            raise reject("本站正在登记备份钥匙，请先完成登记。", 409)
        free_space(fd, sum(i["size"] for i in files))
        # An interrupted start may have left an empty directory before publishing its descriptor.
        path = folder(root, op)
        path.mkdir(mode=0o700, exist_ok=True)
        with upload_dir(root, op) as directory:
            wire.check(not os.listdir(directory))
        enrollment.publish(root / "requests", fd, "restore.upload", wire.encode(data))
        return reply(state(root, binding, op))


@router.post("/{operation}/files/{index}/{offset}")
async def chunk(request: Request, operation: str, index: int, offset: int):
    check_id(operation)
    content = "application/octet-stream"
    require_owner_for_content(request, content)
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > restore.CHUNK:
            raise reject("本次上传块过大。", 413)
        data.extend(part)
    with locked(request, content) as (root, binding, _):
        if state(root, binding, operation)["state"] != "uploading":
            raise conflict()
        files = upload(root)["files"]
        if not 0 <= index < len(files):
            raise reject()
        item = files[index]
        if (
            not 0 <= offset < item["size"]
            or offset % restore.CHUNK
            or len(data) != min(restore.CHUNK, item["size"] - offset)
        ):
            raise reject()
        with upload_dir(root, operation) as directory:
            size = member_size(directory, item)
            if offset > size:
                raise conflict()
            fd = os.open(
                item["name"],
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "r+b") as out:
                meta = os.fstat(out.fileno())
                wire.check(
                    stat.S_ISREG(meta.st_mode)
                    and meta.st_uid == os.geteuid()
                    and meta.st_nlink == 1
                    and stat.S_IMODE(meta.st_mode) == 0o600
                    and meta.st_size == size
                )
                previous = min(size - offset, len(data))
                out.seek(offset)
                if out.read(previous) != data[:previous]:
                    raise conflict()
                if previous < len(data):
                    free_space(directory, len(data) - previous)
                    out.write(data[previous:])
                    out.flush()
                    os.fsync(out.fileno())
        return JSONResponse({"received": offset + len(data)}, headers=HEADERS)


@router.post("/{operation}/seal")
async def seal(request: Request, operation: str):
    check_id(operation)
    require_owner_for_content(request, "application/json")
    if await enrollment.body(request, 64) != {}:
        raise reject()
    with locked(request) as (root, binding, fd):
        current = state(root, binding, operation)
        if current["state"] != "uploading":
            return reply(current)
        if any(i["size"] != i["received"] for i in current["files"]):
            raise conflict()
        with upload_dir(root, operation) as directory:
            wire.check(set(os.listdir(directory)) == {i["name"] for i in current["files"]})
            os.fsync(directory)
        enrollment.publish(root / "requests", fd, "restore.request", (operation + "\n").encode())
        (root / "requests/restore.upload").unlink()
        os.fsync(fd)
        return reply(state(root, binding, operation))


@router.post("/{operation}/packet")
async def packet(request: Request, operation: str):
    check_id(operation)
    require_owner_for_content(request, "application/json")
    content = await enrollment.body(request, wire.MAX_PACKET)
    with locked(request) as (root, binding, fd):
        current = state(root, binding, operation)
        if current["state"] != "awaiting_packet":
            raise conflict()
        try:
            wire.check_packet(content, operation, current["target"])
        except (KeyError, TypeError, ValueError) as exc:
            raise reject() from exc
        enrollment.publish(
            root / "requests", fd, operation + ".restore-packet", wire.encode(content)
        )
        return reply(current)


@router.post("/{operation}/confirm")
async def confirm(request: Request, operation: str):
    check_id(operation)
    require_owner_for_content(request, "application/json")
    content = await enrollment.body(request, 4096)
    if (
        content.keys() != {"operation_id", "preview", "confirmation"}
        or content["operation_id"] != operation
        or content["confirmation"] != "恢复"
    ):
        raise reject()
    with locked(request) as (root, binding, fd):
        current = state(root, binding, operation)
        try:
            restore.preview(content["preview"], binding)
        except (KeyError, TypeError, ValueError) as exc:
            raise reject() from exc
        if current.get("preview") != content["preview"] or current["state"] not in {
            "eligible",
            "applying",
            "succeeded",
        }:
            raise conflict()
        if current["state"] == "eligible":
            if (root / "requests" / (operation + ".restore-cancel")).exists():
                raise conflict()
            enrollment.publish(
                root / "requests",
                fd,
                operation + ".restore-confirm",
                wire.encode({"operation_id": operation, "preview": current["preview"]}),
            )
        return reply(current)


@router.post("/{operation}/cancel")
async def cancel(request: Request, operation: str):
    check_id(operation)
    require_owner_for_content(request, "application/json")
    if await enrollment.body(request, 64) != {}:
        raise reject()
    with locked(request) as (root, binding, fd):
        current = state(root, binding, operation)
        if current["state"] == "uploading":
            os.rename("restore.upload", "restore.discard", src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            finish_discard(root)
            os.fsync(fd)
            return reply(
                dict(
                    project=binding[0],
                    instance_id=binding[1],
                    operation_id=operation,
                    state="cancelled",
                )
            )
        if current["state"] in restore.TERMINAL:
            return reply(current)
        if (
            current["state"] == "applying"
            or (root / "requests" / (operation + ".restore-confirm")).exists()
        ):
            raise conflict()
        enrollment.publish(
            root / "requests", fd, operation + ".restore-cancel", (operation + "\n").encode()
        )
        return reply(current)
