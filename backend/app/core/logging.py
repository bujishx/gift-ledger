import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
request_id_pattern = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_token_path_patterns = (
    re.compile(r"^(/api/v1/imports/)[^/]+(/(?:confirm|result|issues\.xlsx)/?)$"),
    re.compile(r"^(/api/v1/restores/personal/)[^/]+(/confirm/?)$"),
    re.compile(r"^(/api/v1/restores/personal/safety-backups/)[^/]+(/?)$"),
    re.compile(r"^(/api/v1/admin/backups/database/)[^/]+(/download/?)$"),
)


def redact_request_path(path: str) -> str:
    """访问日志只保留路径。含临时凭据的业务路由只保留操作名。"""

    path = path.partition("?")[0]
    for pattern in _token_path_patterns:
        match = pattern.fullmatch(path)
        if match is not None:
            return f"{match.group(1)}{{token}}{match.group(2)}"
    return path


class _AccessPathFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's HTTP access record contains client, method, target, version, status.
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5 or not isinstance(args[2], str):
            return False
        client, method, target, version, status = args
        record.args = (client, method, redact_request_path(target), version, status)
        return True


_access_path_filter = _AccessPathFilter()


def _is_sensitive_backup_or_restore_path(path: str) -> bool:
    return (
        path == "/api/v1/backups/personal"
        or path.startswith("/api/v1/restores/personal")
        or path.startswith("/api/v1/admin/backups/database")
    )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_context.get()),
        }
        for field in ("method", "path", "status_code", "duration_ms", "error_type"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
    logging.getLogger("uvicorn.access").addFilter(_access_path_filter)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = logging.getLogger("gift_ledger.request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        supplied_request_id = headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if request_id_pattern.fullmatch(supplied_request_id)
            else uuid.uuid4().hex
        )
        context_token = request_id_context.set(request_id)
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                # 请求体验证错误会在路由函数执行前返回。敏感备份/恢复接口仍须
                # 禁止浏览器和中间代理缓存这些错误响应。
                if _is_sensitive_backup_or_restore_path(scope["path"]):
                    response_headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            self.logger.info(
                "请求完成",
                extra={
                    "method": scope["method"],
                    "path": redact_request_path(scope["path"]),
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_context.reset(context_token)
