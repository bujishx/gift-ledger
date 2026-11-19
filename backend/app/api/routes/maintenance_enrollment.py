"""Owner-only site key enrollment. The host verifies the ciphertext and signs off on the result."""

import os
import re
import stat
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.maintenance_access import HEADERS, require_owner
from app.api.routes.site_maintenance import submission_lock, unavailable
from app.services import maintenance_enrollment_protocol as wire
from app.services import maintenance_restore_protocol as restore_wire

router = APIRouter(prefix="/api/v1/maintenance/key-enrollment", include_in_schema=False)


def reject(message="本站钥匙登记请求无效。", status=400):
    return HTTPException(status, message, headers=HEADERS)


def safe_read(path: Path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise unavailable() from exc
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise unavailable()
        raw = source.read(limit + 1)
        if len(raw) != meta.st_size:
            raise unavailable()
        return raw


def public(raw, project, instance):
    try:
        value = wire.unpack(raw)
        current = value["state"]
        operation = value["operation_id"]
        wire.check(
            value.keys()
            <= {
                "project",
                "instance_id",
                "state",
                "operation_id",
                "code",
                "registered",
                "target",
                "receipt",
            }
            and (value["project"], value["instance_id"]) == (project, instance)
            and (
                (current in wire.ACTIVE | wire.TERMINAL and wire.matches(wire.OP, operation))
                or (current == "idle" and operation is None)
            )
        )
        if "code" in value:
            wire.check(
                isinstance(value["code"], str)
                and re.fullmatch("[a-zA-Z0-9_-]{1,80}", value["code"])
            )
        if "target" in value:
            wire.check_target(value["target"], project, instance)
            wire.check(type(value["registered"]) is bool)
        if "receipt" in value or current == "succeeded":
            wire.check(current == "succeeded")
            wire.check_receipt(value["receipt"], value["target"])
            wire.check(value["registered"] or value["receipt"]["operation_id"] == operation)
        return value
    except (KeyError, ValueError, TypeError) as exc:
        raise unavailable() from exc


def get_state(root, binding, operation=None):
    def status_file(name):
        raw = safe_read(root / "status" / name, wire.MAX_PACKET)
        return public(raw, *binding) if raw is not None else None

    if operation is not None:
        if not wire.matches(wire.OP, operation):
            raise reject()
        previous = status_file("enrollment-" + operation + ".json")
        if previous is not None:
            if previous["operation_id"] != operation:
                raise unavailable()
            return previous
    pending = safe_read(root / "requests" / "enroll.request", 33)
    current = status_file("enrollment.json")
    if current is None:
        raise unavailable()
    if pending is not None:
        if not re.fullmatch(rb"[0-9a-f]{32}\n", pending):
            raise unavailable()
        uid = pending[:-1].decode("ascii")
        if current["operation_id"] != uid and current["state"] != "needs_review":
            if current["state"] in wire.ACTIVE:
                raise unavailable()
            current = dict(
                project=binding[0], instance_id=binding[1], operation_id=uid, state="queued"
            )
    if operation is not None and operation != current["operation_id"]:
        raise reject("本站没有这次登记记录。", 404)
    return current


def publish(directory, fd, name, data):
    path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".enroll-", delete=False) as temp:
            path = Path(temp.name)
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        try:
            os.link(path, directory / name)
            os.fsync(fd)
        except FileExistsError:
            if safe_read(directory / name, wire.MAX_PACKET) != data:
                raise reject("已有不同的登记请求。", 409) from None
    except OSError as exc:
        raise unavailable() from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def context(request):
    owner = require_owner(request)
    return request.app.state.maintenance_root, owner.binding[:2]


async def body(request, limit):
    raw = bytearray()
    async for piece in request.stream():
        if len(piece) > limit - len(raw):
            raise reject("本站登记数据过大。", 413)
        raw.extend(piece)
    try:
        return wire.unpack(raw)
    except ValueError as exc:
        raise reject() from exc


def response(value, *, conflict=False):
    code = 409 if conflict else (202 if value["state"] in wire.ACTIVE else 200)
    return JSONResponse(value, status_code=code, headers=HEADERS)


@router.get("")
@router.get("/{operation}")
def status(request: Request, operation: str | None = None):
    root, site = context(request)
    return response(get_state(root, site, operation))


@router.post("")
async def start(request: Request):
    root, site = context(request)
    data = await body(request, 256)
    operation = data.get("operation_id")
    if set(data) != {"operation_id"} or not wire.matches(wire.OP, operation):
        raise reject()
    with submission_lock(root / "requests") as fd:
        try:
            previous = get_state(root, site, operation)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            return response(previous)
        current = get_state(root, site)
        if restore_wire.busy(root):
            raise reject("本站已有恢复任务，请先完成或取消恢复。", 409)
        if (
            current["state"] in wire.ACTIVE | {"needs_review"}
            or (root / "requests" / "enroll.request").exists()
        ):
            return response(current, conflict=True)
        publish(root / "requests", fd, "enroll.request", (operation + "\n").encode())
        return response(get_state(root, site, operation))


@router.post("/{operation}/packet")
async def packet(request: Request, operation: str):
    root, site = context(request)
    packet = await body(request, wire.MAX_PACKET)
    with submission_lock(root / "requests") as fd:
        current = get_state(root, site, operation)
        if not current.get("target") or current.get("registered"):
            return response(current, conflict=True)
        try:
            req = wire.check_packet(packet, operation, current["target"])
            if current["state"] == "succeeded":
                wire.check_receipt(current["receipt"], current["target"], req)
                return response(current)
        except (KeyError, TypeError, ValueError) as exc:
            raise reject() from exc
        if (
            current["state"] not in {"awaiting_packet", "verifying"}
            or (root / "requests" / (operation + ".cancel")).exists()
        ):
            return response(current, conflict=True)
        publish(root / "requests", fd, operation + ".packet", wire.encode(packet))
        return response(current)


@router.post("/{operation}/cancel")
async def cancel(request: Request, operation: str):
    root, site = context(request)
    if await body(request, 256) != {}:
        raise reject()
    with submission_lock(root / "requests") as fd:
        current = get_state(root, site, operation)
        if current["state"] in wire.TERMINAL:
            return response(current, conflict=current["state"] != "cancelled")
        if (
            current["state"] == "verifying"
            or (root / "requests" / (operation + ".packet")).exists()
        ):
            return response(current, conflict=True)
        publish(root / "requests", fd, operation + ".cancel", (operation + "\n").encode())
        return response(current)
