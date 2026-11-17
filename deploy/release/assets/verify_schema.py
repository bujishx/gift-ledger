"""Read-only release contract verifier; never stamps, creates or repairs a database.

SQL assets are frozen versioned inputs, not SQL to execute against an existing site.
Checks use MySQL metadata and a parsed expression tree, not cosmetic SHOW CREATE text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent
DICTIONARIES = ("relationship_types", "kinship_types", "event_types")
BUSINESS = (
    "users",
    "contacts",
    "event_records",
    "gift_records",
    "legacy_import_batches",
)
SUPPORTED = tuple(f"schema_v2_s{number:03d}" for number in range(4, 12))


class VerificationError(RuntimeError):
    """A safe message containing only contract names, never row data or credentials."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def sql_parts(value: str) -> list[str]:
    """Split commas outside quoted SQL strings/identifiers and balanced parentheses."""
    result, start, depth, quote, index = [], 0, 0, "", 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
            elif char == "\\":
                index += 1
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(value[start:index].strip())
            start = index + 1
        require(depth >= 0, "invalid SQL parentheses")
        index += 1
    require(not quote and depth == 0, "invalid SQL quoting or parentheses")
    return result + [value[start:].strip()]


def parenthesized(value: str, start: int) -> tuple[str, int]:
    require(value[start] == "(", "expected SQL parenthesis")
    quote, depth, index = "", 0, start
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = ""
            elif char == "\\":
                index += 1
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return value[start + 1 : index], index + 1
        index += 1
    raise VerificationError("unclosed SQL parentheses")


TOKEN = re.compile(
    r"\s+|`(?:``|[^`])+`|'(?:''|\\.|[^'])*'|(?:>=|<=|<>|!=)|[(),=<>+*/-]|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?"
)


def expression(value: str):
    """Canonical expression AST preserving precedence and every literal/operator.

    MySQL adds parentheses and charset introducers when reflecting expressions.
    Case, backticks and charset prefixes are presentation; grouping is semantic.
    This deliberately supports only the operators in the frozen schema contracts.
    """
    # MySQL 8.4 CHECK_CLAUSE may escape charset-introduced quote delimiters.
    # Frozen contracts have no apostrophe/backslash inside these literals. Decode
    # only that exact transport form; never strip arbitrary backslashes or quotes.
    value = re.sub(
        r"(?:_utf8mb4|_utf8mb3|_ascii|_latin1)\\'([^'\\]*)\\'",
        lambda match: "'" + match.group(1) + "'",
        value,
        flags=re.IGNORECASE,
    )
    tokens, end = [], 0
    for match in TOKEN.finditer(value):
        require(match.start() == end, "unknown token in schema expression")
        raw = match.group()
        end = match.end()
        if raw.isspace():
            continue
        if raw.startswith("`"):
            tokens.append(raw[1:-1].replace("``", "`").lower())
        elif raw.startswith("'"):
            tokens.append(
                (
                    "literal",
                    raw[1:-1]
                    .replace("''", "'")
                    .replace("\\'", "'")
                    .replace("\\\\", "\\"),
                )
            )
        elif raw.lower() not in {"_utf8mb4", "_utf8mb3", "_ascii", "_latin1"}:
            tokens.append(raw.lower())
    require(end == len(value), "unknown trailing schema expression")
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take(expected=None):
        nonlocal position
        require(position < len(tokens), "incomplete schema expression")
        token = tokens[position]
        if expected is not None:
            require(token == expected, "unexpected schema expression token")
        position += 1
        return token

    def parse(minimum=0):
        token = take()
        if token == "(":
            left = parse()
            take(")")
        elif token == "not":
            left = ("not", parse(25))
        elif token == "case":
            take("when")
            condition = parse()
            take("then")
            when_true = parse()
            take("else")
            when_false = parse()
            take("end")
            left = ("case", condition, when_true, when_false)
        elif peek() == "(":
            take("(")
            arguments = []
            if peek() != ")":
                arguments.append(parse())
                while peek() == ",":
                    take(",")
                    arguments.append(parse())
            take(")")
            left = ("function", token, tuple(arguments))
        else:
            left = token
        while True:
            operator = peek()
            precedence = (
                {
                    "or": 10,
                    "and": 20,
                    "=": 30,
                    "!=": 30,
                    "<>": 30,
                    "<": 30,
                    ">": 30,
                    "<=": 30,
                    ">=": 30,
                    "is": 30,
                    "in": 30,
                    "between": 30,
                    "+": 40,
                    "-": 40,
                    "*": 50,
                    "/": 50,
                }.get(operator, -1)
                if isinstance(operator, str)
                else -1
            )
            if precedence < minimum:
                return left
            take()
            if operator == "is":
                negated = peek() == "not"
                if negated:
                    take("not")
                right = take("null")
                left = ("is-not" if negated else "is", left, right)
            elif operator == "between":
                lower = parse(precedence + 1)
                take("and")
                upper = parse(precedence + 1)
                left = ("between", left, lower, upper)
            elif operator == "in":
                take("(")
                members = [parse()]
                while peek() == ",":
                    take(",")
                    members.append(parse())
                take(")")
                left = ("in", left, tuple(members))
            else:
                right = parse(precedence + 1)
                # Associative AND/OR trees can be regrouped by MySQL without changing meaning.
                if operator in {"and", "or"}:

                    def operands(item, current_operator=operator):
                        return (
                            item[1]
                            if isinstance(item, tuple) and item[0] == current_operator
                            else (item,)
                        )

                    left = (operator, operands(left) + operands(right))
                else:
                    left = ("<>" if operator == "!=" else operator, left, right)

    result = parse()
    require(position == len(tokens), "unparsed schema expression tokens")
    return result


def column_names(value):
    return [part.strip().strip("`") for part in sql_parts(value)]


def normalize_column_default(value, column_type):
    """Accept only MySQL's two spellings of a zero-precision TIMESTAMP default."""
    if (
        column_type.lower() == "timestamp"
        and isinstance(value, str)
        and value.upper() in {"CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"}
    ):
        return "current_timestamp()"
    return value


def schema_contract(sql: str) -> dict:
    """Build an expected contract only from reviewed, digest-locked SQL assets."""
    sql = re.sub(r"(?m)^\s*--[^\n]*", "", sql)
    tables = {}
    for match in re.finditer(r"CREATE TABLE `([a-z_]+)`\s*\(", sql):
        name = match.group(1)
        body, end = parenthesized(sql, match.end() - 1)
        suffix = sql[end : sql.index(";", end)]
        require(
            "ENGINE=InnoDB" in suffix and "COLLATE=utf8mb4_0900_bin" in suffix,
            "unexpected table options",
        )
        table = {"columns": {}, "indexes": {}, "foreign_keys": {}, "checks": {}}
        for item in sql_parts(body):
            if item.startswith("`"):
                col = re.match(
                    r"`([a-z_]+)`\s+([A-Z]+(?:\(\d+(?:,\s*\d+)?\))?)(\s+UNSIGNED)?",
                    item,
                )
                require(col is not None, "unknown column definition")
                col_name, data_type, unsigned = col.groups()
                data_type = data_type.lower().replace(" ", "") + (
                    " unsigned" if unsigned else ""
                )
                default_match = re.search(
                    r"\bDEFAULT\s+(NULL|CURRENT_TIMESTAMP|'(?:''|[^'])*'|\d+(?:\.\d+)?)",
                    item,
                )
                default = default_match.group(1) if default_match else None
                if default == "NULL":
                    default = None
                elif default == "CURRENT_TIMESTAMP":
                    default = "current_timestamp()"
                elif default is not None:
                    default = default.strip("'")
                generated = ""
                if "GENERATED ALWAYS AS" in item:
                    generated_start = item.index("(", item.index("GENERATED ALWAYS AS"))
                    generated, _ = parenthesized(item, generated_start)
                charset_match = re.search(r"CHARACTER SET\s+(\w+)", item)
                collation_match = re.search(r"COLLATE\s+(\w+)", item)
                textual = data_type.startswith(("varchar", "text"))
                charset = (
                    charset_match.group(1)
                    if charset_match
                    else ("utf8mb4" if textual else None)
                )
                collation = (
                    collation_match.group(1)
                    if collation_match
                    else ("utf8mb4_0900_bin" if textual else None)
                )
                table["columns"][col_name] = {
                    "type": data_type,
                    "nullable": "NOT NULL" not in item,
                    "default": default,
                    "auto_increment": "AUTO_INCREMENT" in item,
                    "charset": charset,
                    "collation": collation,
                    "generated": expression(generated) if generated else None,
                    "stored": False,
                }
            elif item.startswith(("PRIMARY KEY", "UNIQUE KEY", "KEY")):
                index = re.match(
                    r"(PRIMARY KEY|UNIQUE KEY|KEY)(?:\s+`([a-z_]+)`)?\s*\((.*)\)$",
                    item,
                    re.DOTALL,
                )
                require(index is not None, "unknown index definition")
                kind, index_name, members = index.groups()
                table["indexes"][index_name or "PRIMARY"] = {
                    "unique": kind != "KEY",
                    "columns": column_names(members),
                    "type": "BTREE",
                }
            elif " FOREIGN KEY " in item:
                foreign = re.match(
                    r"CONSTRAINT `([a-z_]+)` FOREIGN KEY\s*\((.*?)\)\s*REFERENCES `([a-z_]+)`\s*\((.*?)\) ON DELETE RESTRICT ON UPDATE RESTRICT$",
                    item,
                    re.DOTALL,
                )
                require(foreign is not None, "unknown foreign key definition")
                key, members, target, target_members = foreign.groups()
                table["foreign_keys"][key] = {
                    "columns": column_names(members),
                    "target": target,
                    "target_columns": column_names(target_members),
                    "delete": "RESTRICT",
                    "update": "RESTRICT",
                }
            elif " CHECK " in item:
                check = re.match(r"CONSTRAINT `([a-z_]+)` CHECK\s*\(", item)
                require(check is not None, "unknown CHECK definition")
                check_text, check_end = parenthesized(item, check.end() - 1)
                require(not item[check_end:].strip(), "unexpected CHECK suffix")
                table["checks"][check.group(1)] = {
                    "expression": expression(check_text),
                    "enforced": "YES",
                }
            else:
                raise VerificationError("unknown table contract member")
        tables[name] = table
    require(len(tables) in {7, 8}, "unexpected number of contract tables")
    dictionary_rows = {}
    for match in re.finditer(
        r"INSERT INTO `([a-z_]+)`\s*\((.*?)\)\s*VALUES\s*(.*?);", sql, re.DOTALL
    ):
        name, columns, values = match.groups()
        require(name in DICTIONARIES, "contract contains non-dictionary insert")
        keys = column_names(columns)
        rows = []
        for raw in sql_parts(values):
            inside, end = parenthesized(raw, 0)
            require(end == len(raw), "unknown seed value suffix")
            row = []
            for value in sql_parts(inside):
                row.append(
                    None
                    if value == "NULL"
                    else value[1:-1].replace("''", "'")
                    if value.startswith("'")
                    else int(value)
                )
            require(len(keys) == len(row), "seed columns mismatch")
            rows.append(dict(zip(keys, row, strict=True)))
        dictionary_rows[name] = rows
    require(
        set(dictionary_rows) == set(DICTIONARIES), "incomplete dictionary seed contract"
    )
    return {"tables": tables, "dictionary_rows": dictionary_rows}


def read_snapshot(connection) -> dict:
    from sqlalchemy import text

    current_database = connection.scalar(text("SELECT DATABASE()"))

    def rows(sql):
        return [dict(row) for row in connection.execute(text(sql)).mappings()]

    tables = {}
    for row in rows(
        "SELECT TABLE_NAME AS `name`, ENGINE AS `engine`, TABLE_COLLATION AS `collation`, TABLE_TYPE AS `type` FROM information_schema.tables WHERE table_schema=DATABASE()"
    ):
        require(row["type"] == "BASE TABLE", "unexpected view in release database")
        tables[row["name"]] = {
            "engine": row["engine"],
            "collation": row["collation"],
            "columns": {},
            "indexes": {},
            "foreign_keys": {},
            "checks": {},
        }
    for row in rows(
        "SELECT TABLE_NAME AS `table_name`,COLUMN_NAME AS `name`,COLUMN_TYPE AS `type`,IS_NULLABLE AS `nullable`,COLUMN_DEFAULT AS `default_value`,EXTRA AS `extra`,CHARACTER_SET_NAME AS `charset`,COLLATION_NAME AS `collation`,GENERATION_EXPRESSION AS `generated` FROM information_schema.columns WHERE table_schema=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION"
    ):
        tables[row["table_name"]]["columns"][row["name"]] = {
            "type": row["type"].lower(),
            "nullable": row["nullable"] == "YES",
            "default": normalize_column_default(row["default_value"], row["type"]),
            "auto_increment": "auto_increment" in row["extra"].lower(),
            "charset": row["charset"],
            "collation": row["collation"],
            "generated": expression(row["generated"]) if row["generated"] else None,
            "stored": "stored generated" in row["extra"].lower(),
        }
        extras = (
            row["extra"]
            .lower()
            .replace("auto_increment", "")
            .replace("default_generated", "")
            .replace("virtual generated", "")
            .replace("stored generated", "")
            .strip()
        )
        require(
            not extras,
            f"unexpected column attribute: {row['table_name']}.{row['name']}",
        )
    for row in rows(
        "SELECT TABLE_NAME AS `table_name`,INDEX_NAME AS `name`,NON_UNIQUE AS `non_unique`,COLUMN_NAME AS `column_name`,SEQ_IN_INDEX AS `seq`,INDEX_TYPE AS `type`,SUB_PART AS `subpart`,COLLATION AS `ordering`,IS_VISIBLE AS `visible`,EXPRESSION AS `expr` FROM information_schema.statistics WHERE table_schema=DATABASE() ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX"
    ):
        require(
            row["subpart"] is None
            and row["ordering"] == "A"
            and row["visible"] == "YES"
            and row["expr"] is None,
            f"unsupported index attribute: {row['table_name']}.{row['name']}",
        )
        index = tables[row["table_name"]]["indexes"].setdefault(
            row["name"],
            {"unique": row["non_unique"] == 0, "columns": [], "type": row["type"]},
        )
        index["columns"].append(row["column_name"])
    for row in rows(
        "SELECT k.TABLE_NAME AS `table_name`,k.CONSTRAINT_NAME AS `name`,k.COLUMN_NAME AS `column_name`,k.REFERENCED_TABLE_NAME AS `target`,k.REFERENCED_COLUMN_NAME AS `target_column`,r.DELETE_RULE AS `delete_rule`,r.UPDATE_RULE AS `update_rule`,k.REFERENCED_TABLE_SCHEMA AS `target_schema` FROM information_schema.key_column_usage k JOIN information_schema.referential_constraints r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA=DATABASE() ORDER BY k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION"
    ):
        require(row["target_schema"] == current_database, "cross-database foreign key")
        foreign = tables[row["table_name"]]["foreign_keys"].setdefault(
            row["name"],
            {
                "columns": [],
                "target": row["target"],
                "target_columns": [],
                "delete": row["delete_rule"],
                "update": row["update_rule"],
            },
        )
        foreign["columns"].append(row["column_name"])
        foreign["target_columns"].append(row["target_column"])
    for row in rows(
        "SELECT t.TABLE_NAME AS `table_name`,t.CONSTRAINT_NAME AS `name`,c.CHECK_CLAUSE AS `expr`,t.ENFORCED AS `enforced` FROM information_schema.table_constraints t JOIN information_schema.check_constraints c ON c.CONSTRAINT_SCHEMA=t.CONSTRAINT_SCHEMA AND c.CONSTRAINT_NAME=t.CONSTRAINT_NAME WHERE t.CONSTRAINT_SCHEMA=DATABASE() AND t.CONSTRAINT_TYPE='CHECK'"
    ):
        tables[row["table_name"]]["checks"][row["name"]] = {
            "expression": expression(row["expr"]),
            "enforced": row["enforced"],
        }
    require(set(DICTIONARIES) <= set(tables), "required dictionary tables missing")
    dictionary_rows = {
        name: rows(f"SELECT * FROM `{name}` WHERE user_id IS NULL ORDER BY id")
        for name in DICTIONARIES
    }
    counts = {
        name: connection.scalar(text(f"SELECT COUNT(*) FROM `{name}`"))
        for name in BUSINESS
        if name in tables
    }
    custom_counts = {
        name: connection.scalar(
            text(f"SELECT COUNT(*) FROM `{name}` WHERE user_id IS NOT NULL")
        )
        for name in DICTIONARIES
    }
    versions = (
        connection.execute(text("SELECT version_num FROM alembic_version"))
        .scalars()
        .all()
        if "alembic_version" in tables
        else []
    )
    identity = rows(
        "SELECT DATABASE() AS `database_name`,@@server_uuid AS `server_uuid`,VERSION() AS `mysql_version`,@@character_set_database AS `charset`,@@collation_database AS `collation`"
    )[0]
    return {
        "tables": tables,
        "dictionary_rows": dictionary_rows,
        "business_counts": counts,
        "custom_counts": custom_counts,
        "versions": versions,
        "identity": identity,
    }


def verify_snapshot(
    snapshot,
    contract,
    *,
    revision,
    unstamped=False,
    expect_empty=False,
    expected_server_uuid=None,
    expected_database="gift_ledger_v2",
    version_only=False,
):
    """Pure verification entry for meaningful offline contract tamper tests."""
    require(revision in SUPPORTED, "unsupported target revision")
    require(
        not unstamped or revision == "schema_v2_s004",
        "only S004 can be verified before stamping",
    )
    require(
        not version_only
        or revision not in {"schema_v2_s004", "schema_v2_s010", "schema_v2_s011"},
        "full verification required for contract revisions",
    )
    identity = snapshot["identity"]
    require(
        expected_database == "gift_ledger_v2"
        or re.fullmatch(r"gift_ledger_v2_restore_[0-9a-f]{16}", expected_database)
        is not None,
        "unsupported protected database target",
    )
    require(
        identity["database_name"] == expected_database, "unexpected database target"
    )
    require(
        re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity["server_uuid"]
        )
        is not None,
        "invalid database server UUID",
    )
    require(identity["mysql_version"].startswith("8.4."), "release requires MySQL 8.4")
    require(
        identity["charset"] == "utf8mb4"
        and identity["collation"] == "utf8mb4_0900_bin",
        "database charset/collation mismatch",
    )
    if expected_server_uuid is not None:
        require(
            identity["server_uuid"] == expected_server_uuid,
            "database server identity changed",
        )
    require(
        snapshot["versions"] == ([] if unstamped else [revision]),
        "missing, duplicate or unexpected database revision",
    )
    if not version_only:
        expected_tables = set(contract["tables"]) | (
            set() if unstamped else {"alembic_version"}
        )
        require(
            set(snapshot["tables"]) == expected_tables,
            "unexpected or missing database tables",
        )
        if not unstamped:
            version_table = snapshot["tables"]["alembic_version"]
            require(
                version_table["engine"] == "InnoDB"
                and version_table["collation"] == "utf8mb4_0900_bin",
                "version table options mismatch",
            )
            require(
                version_table["columns"]
                == {
                    "version_num": {
                        "type": "varchar(32)",
                        "nullable": False,
                        "default": None,
                        "auto_increment": False,
                        "charset": "utf8mb4",
                        "collation": "utf8mb4_0900_bin",
                        "generated": None,
                        "stored": False,
                    }
                },
                "version table columns mismatch",
            )
            require(
                version_table["indexes"]
                == {
                    "PRIMARY": {
                        "unique": True,
                        "columns": ["version_num"],
                        "type": "BTREE",
                    }
                }
                and not version_table["foreign_keys"]
                and not version_table["checks"],
                "version table constraints mismatch",
            )
        for name, expected in contract["tables"].items():
            actual = snapshot["tables"][name]
            require(
                actual["engine"] == "InnoDB"
                and actual["collation"] == "utf8mb4_0900_bin",
                f"table options mismatch: {name}",
            )
            for kind in ("columns", "indexes", "foreign_keys", "checks"):
                require(
                    set(actual[kind]) == set(expected[kind]),
                    f"{kind} names mismatch: {name}",
                )
                for member, value in expected[kind].items():
                    require(
                        actual[kind][member] == value,
                        f"{kind} contract mismatch: {name}.{member}",
                    )
        for table, expected_rows in contract["dictionary_rows"].items():
            # Legacy S004 dictionary IDs are fixed; later contracts use stable codes.
            keys = (
                ("id", "user_id", "type_name", "sort_order", "is_active")
                if revision == "schema_v2_s004"
                else ("system_code", "user_id", "type_name", "sort_order", "is_active")
            )

            def projected(rows, current_keys=keys):
                return sorted(tuple(row[key] for key in current_keys) for row in rows)

            require(
                projected(snapshot["dictionary_rows"][table])
                == projected(expected_rows),
                f"system dictionary mismatch: {table}",
            )
        self_rows = [
            row
            for row in snapshot["dictionary_rows"]["relationship_types"]
            if row.get("system_code") == "relationship.self"
            or revision == "schema_v2_s004"
            and row["id"] == 1
        ]
        require(
            len(self_rows) == 1 and self_rows[0]["id"] == 1,
            "self relationship ID must be 1",
        )
    if expect_empty:
        expected_business = set(
            BUSINESS[:4] if revision < "schema_v2_s010" else BUSINESS
        )
        require(
            set(snapshot["business_counts"]) == expected_business
            and all(value == 0 for value in snapshot["business_counts"].values()),
            "fresh deployment contains business records",
        )
        require(
            all(value == 0 for value in snapshot["custom_counts"].values()),
            "fresh deployment contains user dictionaries",
        )


def verify_settings(settings):
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url.get_secret_value())
    require(
        url.drivername == "mysql+pymysql"
        and url.host == "db"
        and url.port == 3306
        and url.database == "gift_ledger_v2"
        and url.username == "gift_ledger_app",
        "runtime database connection mismatch",
    )
    require(
        settings.environment == "production"
        and settings.log_level == "INFO"
        and settings.auth_cookie_secure
        and settings.app_version == "1.5.0",
        "production runtime settings mismatch",
    )
    require(
        all(origin.startswith("https://") for origin in settings.trusted_origin_list),
        "production trusted origin mismatch",
    )
    require(
        settings.admin_backup_offsite_dir is None
        and not settings.admin_backup_age_recipients,
        "offsite backup must remain unconfigured",
    )
    for field in (
        "import_temp_dir",
        "personal_restore_temp_dir",
        "personal_safety_backup_dir",
        "admin_backup_dir",
    ):
        path = getattr(settings, field)
        require(
            path.is_absolute() and Path("/var/lib/gift-ledger") in path.parents,
            f"persistent runtime path mismatch: {field}",
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="只读核对固定数据库版本、结构、系统字典和正式配置"
    )
    parser.add_argument("--revision", choices=SUPPORTED, required=True)
    parser.add_argument("--unstamped", action="store_true")
    parser.add_argument("--expect-empty", action="store_true")
    parser.add_argument("--version-only", action="store_true")
    parser.add_argument("--expected-server-uuid")
    args = parser.parse_args(argv)
    try:
        # An absolute external script sets sys.path to its asset directory, not
        # the image's WORKDIR. Only the fixed, reviewed image application tree is
        # eligible for imports; never derive this from the caller's current path.
        backend_path = Path("/app/backend")
        require(
            backend_path.is_dir() and not backend_path.is_symlink(),
            "missing trusted image application tree",
        )
        sys.path.insert(0, str(backend_path))
        from app.core.config import Settings
        from app.core.database import get_engine

        settings = Settings()
        verify_settings(settings)
        contract_revision = args.revision if not args.version_only else "schema_v2_s011"
        sql_path = ASSET_DIR / f"{contract_revision}.sql"
        require(
            sql_path.is_file() and not sql_path.is_symlink(),
            "missing immutable SQL contract",
        )
        contract = schema_contract(sql_path.read_text(encoding="utf-8"))
        engine = get_engine(
            settings.database_url.get_secret_value(),
            settings.database_connect_timeout_seconds,
        )
        with engine.connect() as connection:
            # The verifier has no data-changing statement, even with the app user's privileges.
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            snapshot = read_snapshot(connection)
            verify_snapshot(
                snapshot,
                contract,
                revision=args.revision,
                unstamped=args.unstamped,
                expect_empty=args.expect_empty,
                expected_server_uuid=args.expected_server_uuid,
                version_only=args.version_only,
            )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "revision": args.revision,
                    "unstamped": args.unstamped,
                    "expect_empty": args.expect_empty,
                    "verification": "version-and-empty"
                    if args.version_only
                    else "full-schema-contract",
                    "server_uuid": snapshot["identity"]["server_uuid"],
                    "mysql_version": snapshot["identity"]["mysql_version"],
                    "table_count": len(snapshot["tables"]),
                    "business_counts": snapshot["business_counts"],
                    "custom_dictionary_counts": snapshot["custom_counts"],
                    "system_dictionary_counts": {
                        name: len(rows)
                        for name, rows in snapshot["dictionary_rows"].items()
                    },
                    "contract_sha256": hashlib.sha256(
                        sql_path.read_bytes()
                    ).hexdigest(),
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed without exposing credential-bearing exceptions
        # SQLAlchemy/Pydantic exceptions may embed URLs/secrets, so print only safe custom errors.
        message = (
            str(exc)
            if isinstance(exc, VerificationError)
            else "数据库连接或核验失败；详细异常不输出，以保护凭据"
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": message,
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
