# ruff: noqa: RUF002, RUF003
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "往来有礼 API"
    app_version: str = "1.5.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    registration_enabled: bool = True
    database_url: SecretStr | None = None
    database_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    jwt_secret: SecretStr | None = None
    auth_cookie_secure: bool = True
    import_temp_dir: Path = BACKEND_DIR / ".runtime" / "imports"
    import_preview_ttl_minutes: int = Field(default=30, ge=5, le=120)
    import_max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    import_max_rows: int = Field(default=5000, ge=1, le=20000)
    import_max_uncompressed_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1024,
        le=200 * 1024 * 1024,
    )
    personal_restore_temp_dir: Path = BACKEND_DIR / ".runtime" / "personal-restores"
    personal_restore_preview_ttl_minutes: int = Field(default=30, ge=5, le=120)
    personal_restore_max_file_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    personal_safety_backup_dir: Path = BACKEND_DIR / ".runtime" / "personal-safety-backups"
    personal_safety_backup_ttl_hours: int = Field(default=24, ge=1, le=168)
    admin_backup_dir: Path = BACKEND_DIR / ".runtime" / "admin-backups"
    admin_backup_offsite_dir: Path | None = None
    admin_backup_age_recipients: str = ""
    admin_backup_age_path: str = "age"
    admin_backup_mysqldump_path: str = "mysqldump"
    admin_backup_mysql_path: str = "mysql"
    admin_backup_keep_daily: int = Field(default=14, ge=1, le=60)
    admin_backup_keep_weekly: int = Field(default=8, ge=1, le=52)
    admin_backup_keep_monthly: int = Field(default=12, ge=1, le=120)
    admin_backup_command_timeout_seconds: int = Field(default=3600, ge=10, le=86400)
    admin_backup_stale_overdue_hours: int = Field(default=24, ge=1, le=168)
    admin_backup_max_sql_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024 * 1024,
    )
    trusted_origins: str = (
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5173,http://localhost:5173"
    )
    reverse_proxy_origin: str = ""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="GIFT_LEDGER_",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("reverse_proxy_origin")
    @classmethod
    def validate_reverse_proxy_origin(cls, value: str) -> str:
        if not value:
            return ""
        match = re.fullmatch(r"https://([a-zA-Z0-9.-]{1,253})(?::([1-9][0-9]{0,4}))?/?", value)
        if match:
            host, port = match.groups()
            labels = host.lower().split(".")
            if (
                len(labels) >= 2
                and re.search("[a-z]", labels[-1])
                and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s) for s in labels)
                and (port is None or int(port) <= 65535)
            ):
                authority = host.lower() if port in {None, "443"} else f"{host.lower()}:{port}"
                return f"https://{authority}"
        raise ValueError("反向代理地址应为 HTTPS 域名及可选端口，例如 https://ledger.example:8443")

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        self._validate_database_target()
        self._validate_admin_backup_paths()
        origins = self.trusted_origin_list
        if not origins:
            raise ValueError("GIFT_LEDGER_TRUSTED_ORIGINS 至少需要配置一个来源")
        if self.reverse_proxy_origin:
            if len(origins) != 1 or urlsplit(origins[0]).scheme != "https":
                raise ValueError("启用反向代理时必须保留一个内网 HTTPS 站点地址")
            if urlsplit(origins[0]).hostname == urlsplit(self.reverse_proxy_origin).hostname:
                raise ValueError("反向代理域名须与内网站点主机不同")
            if not self.auth_cookie_secure:
                raise ValueError("反向代理入口必须启用安全 Cookie")

        if not self.auth_cookie_secure:
            if self.environment not in {"development", "test"}:
                raise ValueError("只有本机开发环境可以关闭安全 Cookie")
            if any(
                urlsplit(origin).hostname not in {"127.0.0.1", "localhost", "::1"}
                for origin in origins
            ):
                raise ValueError("关闭安全 Cookie 时只允许使用本机回环地址")

        if self.environment == "production":
            if self.database_url is None:
                raise ValueError("正式环境必须配置 GIFT_LEDGER_DATABASE_URL")
            if self.jwt_secret is None or len(self.jwt_secret.get_secret_value().encode()) < 32:
                raise ValueError("正式环境的 GIFT_LEDGER_JWT_SECRET 至少需要 32 字节")
            if not self.auth_cookie_secure:
                raise ValueError("正式环境必须启用安全 Cookie")
            if any(urlsplit(origin).scheme != "https" for origin in origins):
                raise ValueError("正式环境的可信来源必须全部使用 HTTPS")
        return self

    def _validate_database_target(self) -> None:
        if self.database_url is None:
            return
        try:
            url = make_url(self.database_url.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("GIFT_LEDGER_DATABASE_URL 格式无效") from exc
        if url.drivername != "mysql+pymysql":
            raise ValueError("数据库连接必须使用 mysql+pymysql")

        database_name = url.database or ""
        allowed = database_name == "gift_ledger_v2"
        if self.environment == "test":
            allowed = allowed or database_name.startswith("gift_ledger_v2_test")
        if not allowed:
            raise ValueError(
                "数据库名称不受允许；开发和正式环境只能使用 gift_ledger_v2，"
                "测试环境另允许 gift_ledger_v2_test 前缀"
            )

    def _validate_admin_backup_paths(self) -> None:
        """管理员整库备份目录必须安全：拒绝相对路径与仓库／源码目录。"""
        for field_name, path in (("GIFT_LEDGER_ADMIN_BACKUP_DIR", self.admin_backup_dir),):
            if not path.is_absolute():
                raise ValueError(f"{field_name} 必须使用绝对路径")
            resolved = path.resolve()
            forbidden_roots = (
                (BACKEND_DIR / "app").resolve(),
                (BACKEND_DIR / "migrations").resolve(),
                (BACKEND_DIR / "tests").resolve(),
                (BACKEND_DIR / ".venv").resolve(),
            )
            for forbidden in forbidden_roots:
                if resolved == forbidden or forbidden in resolved.parents:
                    raise ValueError(f"{field_name} 不能位于后端源码或依赖目录内")
            if resolved in {BACKEND_DIR.resolve(), BACKEND_DIR.parent.resolve()}:
                raise ValueError(f"{field_name} 不能直接使用仓库或后端根目录")

        if (
            self.admin_backup_offsite_dir is not None
            and not self.admin_backup_offsite_dir.is_absolute()
        ):
            raise ValueError("GIFT_LEDGER_ADMIN_BACKUP_OFFSITE_DIR 必须使用绝对路径")

        if self.admin_backup_offsite_dir is not None:
            backup_resolved = self.admin_backup_dir.resolve()
            offsite_resolved = self.admin_backup_offsite_dir.resolve()
            if (
                backup_resolved == offsite_resolved
                or backup_resolved in offsite_resolved.parents
                or offsite_resolved in backup_resolved.parents
            ):
                raise ValueError("本机备份目录与异地备份目录不能重叠")
            backend_resolved = BACKEND_DIR.resolve()
            if offsite_resolved == backend_resolved or backend_resolved in offsite_resolved.parents:
                raise ValueError("异地备份目录不能位于项目目录内")

        # 异地目录与 age 接收者必须同时配置或同时留空，避免半配置的静默失效。
        offsite_dir_set = self.admin_backup_offsite_dir is not None
        recipients_set = bool(self.admin_backup_age_recipients.strip())
        if offsite_dir_set != recipients_set:
            raise ValueError("异地备份目录与 age 接收者必须同时配置或同时留空")

        for field_name, value in (
            ("GIFT_LEDGER_ADMIN_BACKUP_AGE_PATH", self.admin_backup_age_path),
            ("GIFT_LEDGER_ADMIN_BACKUP_MYSQLDUMP_PATH", self.admin_backup_mysqldump_path),
            ("GIFT_LEDGER_ADMIN_BACKUP_MYSQL_PATH", self.admin_backup_mysql_path),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} 不能为空")

    @property
    def admin_backup_age_recipient_list(self) -> tuple[str, ...]:
        recipients: list[str] = []
        for raw in self.admin_backup_age_recipients.replace(",", "\n").splitlines():
            recipient = raw.strip()
            if recipient:
                recipients.append(recipient)
        return tuple(dict.fromkeys(recipients))

    @property
    def trusted_origin_list(self) -> tuple[str, ...]:
        origins: list[str] = []
        for raw_origin in self.trusted_origins.split(","):
            origin = raw_origin.strip().rstrip("/")
            if not origin:
                continue
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"可信来源格式无效：{origin}")
            origins.append(origin)
        return tuple(dict.fromkeys(origins))

    @property
    def site_origin(self) -> str | None:
        """Single-site deployments bind the private entry to this HTTPS origin."""
        origins = self.trusted_origin_list
        if len(origins) == 1 and (self.environment == "production" or self.reverse_proxy_origin):
            return origins[0]
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
