import argparse
import getpass
import sys
import warnings
from collections.abc import Callable, Sequence

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import PasswordPolicyError, UsernamePolicyError
from app.db.session import get_session_factory
from app.services.auth import UsernameAlreadyExistsError
from app.services.registration_limits import LoginBusy
from app.services.user_management import (
    UserNotFoundError,
    create_admin_user,
    list_business_accounts,
    reset_user_password,
    set_user_active,
)

EXIT_SUCCESS = 0
EXIT_SYSTEM_ERROR = 1
EXIT_INPUT_ERROR = 2

InputFunction = Callable[[str], str]
OutputFunction = Callable[[str], None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="往来有礼本机账号管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-users", help="只读查询本站登录名、启停状态和角色")

    create_parser = subparsers.add_parser("create-admin", help="创建管理员账号")
    create_parser.add_argument("--username", help="登录用户名；省略时交互输入")

    reset_parser = subparsers.add_parser("reset-password", help="重置指定账号密码")
    reset_parser.add_argument("--username", help="登录用户名；省略时交互输入")

    active_parser = subparsers.add_parser("set-user-active", help="启用或停用指定账号")
    active_parser.add_argument("--username", help="登录用户名；省略时交互输入")
    state_group = active_parser.add_mutually_exclusive_group(required=True)
    state_group.add_argument("--active", dest="is_active", action="store_true", help="启用账号")
    state_group.add_argument(
        "--inactive",
        dest="is_active",
        action="store_false",
        help="停用账号",
    )
    return parser


def _username(args: argparse.Namespace, input_function: InputFunction) -> str:
    supplied = args.username
    return supplied if supplied is not None else input_function("请输入登录用户名：")


def _confirmed_password(password_function: InputFunction) -> str:
    password = password_function("请输入新密码：")
    confirmation = password_function("请再次输入新密码：")
    if password != confirmation:
        raise PasswordPolicyError("PASSWORD_CONFIRMATION_MISMATCH", "两次输入的密码不一致")
    return password


def _hidden_password(prompt: str) -> str:
    # getpass otherwise falls back to an echoed read on some terminals.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning as exc:
            raise PasswordPolicyError(
                "INTERACTIVE_TERMINAL_REQUIRED", "请在支持隐藏输入的交互终端中运行此命令。"
            ) from exc


def execute_command(
    args: argparse.Namespace,
    session: Session,
    *,
    input_function: InputFunction = input,
    password_function: InputFunction = _hidden_password,
    output_function: OutputFunction = print,
) -> int:
    try:
        if args.command == "list-users":
            accounts = list_business_accounts(session)
            if accounts:
                output_function("登录名\t状态\t角色")
                for account in accounts:
                    state = "启用" if account.is_active else "停用"
                    role = "管理员" if account.is_admin else "普通用户"
                    output_function(f"{account.username}\t{state}\t{role}")
            else:
                output_function("本站尚未注册账号。")
            return EXIT_SUCCESS
        username = _username(args, input_function)
        if args.command == "create-admin":
            user = create_admin_user(session, username, _confirmed_password(password_function))
            output_function(f"管理员账号 {user.username} 已创建；首次登录后请完善本人信息。")
        elif args.command == "reset-password":
            user = reset_user_password(
                session,
                username,
                _confirmed_password(password_function),
            )
            output_function(f"账号 {user.username} 的密码已重置，原有登录凭证已失效。")
        elif args.command == "set-user-active":
            user = set_user_active(session, username, is_active=args.is_active)
            state = "启用" if user.is_active else "停用"
            output_function(f"账号 {user.username} 已{state}。")
        else:
            raise RuntimeError("未知管理命令")
        return EXIT_SUCCESS
    except UsernamePolicyError as exc:
        output_function(f"操作未执行：{exc}")
    except PasswordPolicyError as exc:
        output_function(f"操作未执行：{exc.message}")
    except UsernameAlreadyExistsError:
        output_function("操作未执行：该用户名已存在。")
    except UserNotFoundError:
        output_function("操作未执行：找不到该账号。")
    except LoginBusy:
        output_function("密码验证繁忙，请稍后重试。")
        return EXIT_SYSTEM_ERROR
    except (EOFError, KeyboardInterrupt):
        output_function("操作已取消，数据库未修改。")
    return EXIT_INPUT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        factory = get_session_factory(settings)
        with factory() as session:
            return execute_command(args, session)
    except (RuntimeError, ValidationError):
        print("操作失败，数据库未修改；请检查本机数据库配置。", file=sys.stderr)
        return EXIT_SYSTEM_ERROR
    except SQLAlchemyError:
        print("操作失败，数据库未修改；数据库连接或操作发生异常。", file=sys.stderr)
        return EXIT_SYSTEM_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
