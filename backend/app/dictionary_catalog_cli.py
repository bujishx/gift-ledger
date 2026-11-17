"""Read-only preflight for the approved S009 system dictionary refresh."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.db.session import get_session_factory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S009 字典更新只读预检")
    parser.add_argument("command", choices=("preflight",))
    parser.parse_args(argv)
    migration = importlib.import_module("migrations.versions.schema_v2_s009_dictionary_labels")
    try:
        settings = Settings()
        factory = get_session_factory(settings)
        with factory() as session:
            bind = session.connection()
            revision = bind.scalar(text("SELECT version_num FROM alembic_version"))
            if revision not in {"schema_v2_s008", "schema_v2_s009"}:
                print(json.dumps({"ok": False, "code": "SCHEMA_REVISION_MISMATCH"}))
                return 3
            migration._preflight_system_rows(bind)
            conflicts = migration.custom_conflicts(bind)
            print(
                json.dumps(
                    {
                        "ok": not conflicts,
                        "schema_revision": revision,
                        "conflict_count": len(conflicts),
                        "conflicts": conflicts,
                    },
                    ensure_ascii=False,
                )
            )
            return 3 if conflicts else 0
    except RuntimeError as exc:
        # The migration emits only controlled errors and numeric IDs, never credentials.
        print(json.dumps({"ok": False, "code": str(exc).split("：", 1)[0]}, ensure_ascii=False))
        return 3
    except (OSError, ValidationError, SQLAlchemyError):
        print("S009 只读预检失败，请检查本机数据库配置；数据库未修改。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
