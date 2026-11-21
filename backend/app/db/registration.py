"""NAS-local registration state, in the same MySQL transaction as business accounts.

The portable ledger dump has a frozen S011 schema. Admission settings and unused
invitations belong to the destination site, not to a ledger restored into it.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    inspect,
    select,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert

DATABASE = "gift_ledger_registration"
metadata = MetaData(schema=DATABASE)

policy = Table(
    "policy",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("lan_enabled", Boolean, nullable=False),
    Column("public_mode", String(6), nullable=False),
    CheckConstraint("id = 1"),
    CheckConstraint("public_mode IN ('open', 'closed', 'invite')"),
    mysql_engine="InnoDB",
)

invitations = Table(
    "invitations",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("code_hash", String(64), nullable=False, unique=True),
    Column("created_at", BigInteger, nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("used_at", BigInteger),
    Column("revoked_at", BigInteger),
    mysql_engine="InnoDB",
)


def seed_policy(connection, legacy_open: bool) -> None:
    values = {
        "id": 1,
        "lan_enabled": legacy_open,
        "public_mode": "invite" if legacy_open else "closed",
    }
    if connection.dialect.name == "mysql":
        # A repeated initialize must never overwrite the owner's saved choices.
        statement = mysql_insert(policy).values(**values).on_duplicate_key_update(id=1)
    else:
        if connection.scalar(select(policy.c.id).where(policy.c.id == 1)) is not None:
            return
        statement = insert(policy).values(**values)
    connection.execute(statement)


def provision(root_engine, legacy_open: bool) -> None:
    """Called after the existing installer has verified the site and its local root socket."""
    with root_engine.begin() as connection:
        if DATABASE in inspect(connection).get_schema_names():
            tables = set(inspect(connection).get_table_names(schema=DATABASE))
            if tables - {"policy", "invitations"}:
                raise ValueError("unexpected_registration_tables")
            if tables == {"policy", "invitations"} and connection.scalar(select(policy.c.id)) == 1:
                return
        connection.exec_driver_sql(
            "CREATE DATABASE IF NOT EXISTS gift_ledger_registration "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
        )
        metadata.create_all(connection)
        connection.exec_driver_sql(
            "GRANT SELECT, UPDATE ON gift_ledger_registration.policy TO 'gift_ledger_app'@%s",
            ("%",),
        )
        connection.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE ON gift_ledger_registration.invitations "
            "TO 'gift_ledger_app'@%s",
            ("%",),
        )
        # MySQL DDL auto-commits; this last marker makes an interrupted setup retryable.
        seed_policy(connection, legacy_open)
