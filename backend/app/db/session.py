from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import get_engine


@lru_cache
def _session_factory(database_url: str, connect_timeout_seconds: int) -> sessionmaker[Session]:
    engine = get_engine(database_url, connect_timeout_seconds)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session_factory(settings: Settings) -> sessionmaker[Session]:
    """构造惰性会话工厂; 本函数本身不会连接数据库。"""
    if settings.database_url is None:
        raise RuntimeError("尚未配置 GIFT_LEDGER_DATABASE_URL")
    return _session_factory(
        settings.database_url.get_secret_value(),
        settings.database_connect_timeout_seconds,
    )


def open_session(settings: Settings) -> Iterator[Session]:
    """为一次操作提供会话; 提交由业务事务显式负责。"""
    factory = get_session_factory(settings)
    with factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
