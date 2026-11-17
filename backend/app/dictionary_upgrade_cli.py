"""S006 字典升级冲突的服务器本机维护命令。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_session_factory
from app.services.system_dictionary_upgrade import (
    DictionaryUpgradeError,
    rename_dictionary_upgrade_conflict,
    scan_dictionary_upgrade_conflicts,
)

EXIT_SUCCESS = 0
EXIT_SYSTEM_ERROR = 1
EXIT_INPUT_ERROR = 2
EXIT_CONFLICTS_FOUND = 3

OutputFunction = Callable[[str], None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="S006 字典升级冲突维护")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="只读扫描与 S006 系统名称冲突的自定义项")
    scan_parser.add_argument("--json-output", type=Path, help="把完整报告写入指定 JSON 文件")

    rename_parser = subparsers.add_parser("rename", help="安全改名一个已确认的冲突自定义项")
    rename_parser.add_argument(
        "--category",
        choices=("relationship", "kinship", "event"),
        required=True,
    )
    rename_parser.add_argument("--user-id", type=int, required=True)
    rename_parser.add_argument("--item-id", type=int, required=True)
    rename_parser.add_argument("--expected-name", required=True)
    rename_parser.add_argument("--new-name", required=True)
    return parser


def _write_json_report(path: Path, payload: list[dict]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def execute_command(
    args: argparse.Namespace,
    session: Session,
    *,
    output_function: OutputFunction = print,
) -> int:
    try:
        if args.command == "scan":
            conflicts = scan_dictionary_upgrade_conflicts(session)
            payload = [item.as_dict() for item in conflicts]
            if args.json_output is not None:
                _write_json_report(args.json_output, payload)
                output_function(f"冲突报告已写入：{args.json_output.resolve()}")
            if not conflicts:
                output_function("S006 字典升级预检通过：未发现自定义项名称冲突。")
                return EXIT_SUCCESS
            output_function(f"发现 {len(conflicts)} 个冲突；数据库未修改：")
            for item in conflicts:
                output_function(
                    f"- 用户 {item.user_id}（{item.username}）／{item.category}／"
                    f"项目 {item.item_id}「{item.name}」／引用 {item.reference_count}／"
                    f"建议「{item.suggested_name}」"
                )
            return EXIT_CONFLICTS_FOUND

        if args.command == "rename":
            if args.user_id <= 0 or args.item_id <= 0:
                raise DictionaryUpgradeError("IDENTIFIER_INVALID", "用户编号和项目编号必须为正整数")
            renamed = rename_dictionary_upgrade_conflict(
                session,
                category=args.category,
                user_id=args.user_id,
                item_id=args.item_id,
                expected_name=args.expected_name,
                new_name=args.new_name,
            )
            output_function(f"自定义字典项已安全改名为「{renamed}」；编号和全部历史引用保持不变。")
            return EXIT_SUCCESS

        raise RuntimeError("未知维护命令")
    except DictionaryUpgradeError as exc:
        output_function(f"操作未执行：{exc.message}（{exc.code}）")
        return EXIT_INPUT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
        factory = get_session_factory(settings)
        with factory() as session:
            return execute_command(args, session)
    except (OSError, RuntimeError, ValidationError):
        print("操作失败，数据库未修改；请检查本机数据库配置。", file=sys.stderr)
        return EXIT_SYSTEM_ERROR
    except SQLAlchemyError:
        print("操作失败，数据库未修改；数据库连接或操作发生异常。", file=sys.stderr)
        return EXIT_SYSTEM_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
