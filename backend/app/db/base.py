from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型共享的声明式基类。"""


def mysql_table_options(comment: str) -> dict[str, str]:
    """返回与 S004 完全一致的 MySQL 表级选项。"""
    return {
        "mysql_engine": "InnoDB",
        "mysql_default_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_0900_bin",
        "comment": comment,
    }
