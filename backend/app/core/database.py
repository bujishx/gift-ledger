import logging
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine(database_url: str, connect_timeout_seconds: int) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"connect_timeout": connect_timeout_seconds},
    )


def probe_database(settings: Settings) -> bool:
    if settings.database_url is None:
        logger.warning("数据库就绪检查失败：尚未配置数据库地址")
        return False

    try:
        engine = get_engine(
            settings.database_url.get_secret_value(),
            settings.database_connect_timeout_seconds,
        )
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1
    except SQLAlchemyError as exc:
        logger.warning(
            "数据库就绪检查失败",
            extra={"error_type": type(exc).__name__},
        )
        return False
