from logging.config import fileConfig

from alembic import context

from app.core.config import Settings
from app.core.database import get_engine
from app.db import models as _models  # noqa: F401
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("运行 Alembic 前必须配置 GIFT_LEDGER_DATABASE_URL")
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("运行 Alembic 前必须配置 GIFT_LEDGER_DATABASE_URL")

    engine = get_engine(
        settings.database_url.get_secret_value(),
        settings.database_connect_timeout_seconds,
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
