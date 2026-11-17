# ruff: noqa: RUF002
"""同一用户账本写操作的数据库级串行锁。

个人整账本恢复会先生成安全备份，再删除并重建该用户的全部业务数据。所有可能改动
联系人或自定义字典的普通写路径也必须先锁定同一条 ``users`` 记录，才能避免它们在
安全备份生成后插入数据、随后又被恢复事务无提示删除。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User


def lock_user_ledger_write(session: Session, user_id: int) -> None:
    """锁定用户行直到当前事务提交或回滚。"""

    session.execute(select(User.id).where(User.id == user_id).with_for_update())


__all__ = ["lock_user_ledger_write"]
