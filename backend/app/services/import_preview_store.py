from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _discard_stored_files(manifest_path: Path, workbook_path: Path) -> None:
    """Best-effort cleanup that never replaces the import result with an I/O error.

    Delete the workbook first. If another process temporarily locks it, keep the
    manifest so ``cleanup_expired`` can find the pair and retry later.
    """

    try:
        workbook_path.unlink(missing_ok=True)
    except OSError:
        return
    try:
        manifest_path.unlink(missing_ok=True)
    except OSError:
        pass


class ImportPreviewStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StoredImportPreview:
    token: str
    user_id: int
    content: bytes
    expires_at: datetime
    manifest_path: Path
    workbook_path: Path
    decisions: dict[str, str] | None = None
    plan_fingerprint: str | None = None
    issues: tuple[dict[str, Any], ...] = ()

    def discard(self) -> None:
        _discard_stored_files(self.manifest_path, self.workbook_path)

    def release(self) -> None:
        """Release only this atomic claim after a rolled-back confirmation."""
        target = self.manifest_path.with_suffix(".json")
        if self.manifest_path.suffix == ".processing" and self.manifest_path.exists():
            self.manifest_path.replace(target)


class ImportPreviewStore:
    def __init__(self, directory: Path, *, ttl_minutes: int) -> None:
        self.directory = directory
        self.ttl = timedelta(minutes=ttl_minutes)

    def _prepare_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def _paths(self, token: str) -> tuple[Path, Path, Path]:
        if not _TOKEN_PATTERN.fullmatch(token):
            raise ImportPreviewStoreError("IMPORT_PREVIEW_INVALID", "导入预检凭证无效")
        return (
            self.directory / f"{token}.json",
            self.directory / f"{token}.processing",
            self.directory / f"{token}.xlsx",
        )

    def cleanup_expired(
        self, *, now: datetime | None = None, exclude_token: str | None = None
    ) -> None:
        self._prepare_directory()
        current = now or datetime.now(UTC)
        for manifest_path in (*self.directory.glob("*.json"), *self.directory.glob("*.processing")):
            if manifest_path.stem == exclude_token:
                continue
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(payload["expires_at"])
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                expires_at = current - timedelta(seconds=1)
            if expires_at <= current:
                token = manifest_path.stem
                _discard_stored_files(manifest_path, self.directory / f"{token}.xlsx")

    def create(
        self,
        *,
        user_id: int,
        content: bytes,
        now: datetime | None = None,
        decisions: dict[str, str] | None = None,
        plan_fingerprint: str | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> tuple[str, datetime]:
        current = now or datetime.now(UTC)
        self.cleanup_expired(now=current)
        expires_at = current + self.ttl
        for _attempt in range(5):
            token = secrets.token_urlsafe(32)
            manifest_path, _processing_path, workbook_path = self._paths(token)
            payload = {
                "version": 1,
                "user_id": user_id,
                "expires_at": expires_at.isoformat(),
                "decisions": decisions or {},
                "plan_fingerprint": plan_fingerprint,
                "issues": issues or [],
            }
            try:
                with workbook_path.open("xb") as workbook_file:
                    workbook_file.write(content)
                try:
                    with manifest_path.open("x", encoding="utf-8") as manifest_file:
                        json.dump(payload, manifest_file, ensure_ascii=True, separators=(",", ":"))
                except Exception:
                    _discard_stored_files(manifest_path, workbook_path)
                    raise
            except FileExistsError:
                continue
            return token, expires_at
        raise RuntimeError("无法生成不重复的导入预检凭证")

    def claim(
        self,
        token: str,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> StoredImportPreview:
        current = now or datetime.now(UTC)
        manifest_path, processing_path, workbook_path = self._paths(token)
        self.cleanup_expired(now=current, exclude_token=token)
        try:
            payload: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(payload["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            manifest_user_id = int(payload["user_id"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ImportPreviewStoreError(
                "IMPORT_PREVIEW_INVALID",
                "导入预检凭证不存在、已过期或已经使用",
            ) from exc
        if manifest_user_id != user_id:
            raise ImportPreviewStoreError(
                "IMPORT_PREVIEW_INVALID",
                "导入预检凭证不存在、已过期或已经使用",
            )
        if expires_at <= current:
            _discard_stored_files(manifest_path, workbook_path)
            raise ImportPreviewStoreError("IMPORT_PREVIEW_EXPIRED", "导入预检已过期，请重新上传")
        owns_claim = False
        try:
            # Exclusive creation is the cross-process claim. On Windows two
            # concurrent replacements of the same source can both succeed.
            with processing_path.open("x", encoding="utf-8") as processing_file:
                owns_claim = True
                json.dump(payload, processing_file, ensure_ascii=True, separators=(",", ":"))
        except OSError as exc:
            # Never remove another request's files. A failure after our own
            # exclusive creation can release only our claim for a safe retry.
            if owns_claim:
                processing_path.unlink(missing_ok=True)
            raise ImportPreviewStoreError(
                "IMPORT_PREVIEW_INVALID",
                "导入预检凭证不存在、已过期或已经使用",
            ) from exc
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError:
            # The exclusive claim still prevents a second claimant. read()
            # prefers it, and release() replaces any leftover pending manifest.
            pass
        try:
            content = workbook_path.read_bytes()
        except OSError as exc:
            _discard_stored_files(processing_path, workbook_path)
            raise ImportPreviewStoreError(
                "IMPORT_PREVIEW_INVALID",
                "导入预检凭证不存在、已过期或已经使用",
            ) from exc
        return StoredImportPreview(
            token=token,
            user_id=user_id,
            content=content,
            expires_at=expires_at,
            manifest_path=processing_path,
            workbook_path=workbook_path,
            decisions=payload.get("decisions", {}),
            plan_fingerprint=payload.get("plan_fingerprint"),
            issues=tuple(payload.get("issues", [])),
        )

    def read(self, token: str, *, user_id: int, now: datetime | None = None):
        """Read a preview/report without claiming it; owner and expiry are checked."""
        manifest_path, processing_path, workbook_path = self._paths(token)
        current = now or datetime.now(UTC)
        path = processing_path if processing_path.exists() else manifest_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(payload["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if int(payload["user_id"]) != user_id:
                raise ValueError("owner mismatch")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ImportPreviewStoreError(
                "IMPORT_PREVIEW_INVALID", "导入预检凭证不存在或不可用"
            ) from exc
        if expires_at <= current:
            # A read now precedes claim() when authorizing a reviewed import.
            # Keep its previous pending-file expiry cleanup, but never delete a
            # processing batch which another request may still be committing.
            if path == manifest_path:
                _discard_stored_files(manifest_path, workbook_path)
            raise ImportPreviewStoreError("IMPORT_PREVIEW_EXPIRED", "导入预检已过期，请重新上传")
        return payload, "processing" if path == processing_path else "pending"


__all__ = [
    "ImportPreviewStore",
    "ImportPreviewStoreError",
    "StoredImportPreview",
]
