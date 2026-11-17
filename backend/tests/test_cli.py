import warnings
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app import cli
from app.db.models import User
from app.services.user_management import UserNotFoundError


def user(*, is_admin: int = 0, is_active: int = 1) -> User:
    return User(
        id=8,
        username="managed_user",
        password_hash="hidden-hash",
        is_admin=is_admin,
        is_active=is_active,
    )


def test_cli_does_not_accept_password_as_command_argument() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["create-admin", "--username", "admin_user", "--password", "must-not-work"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["reset-password", "--username", "managed_user", "--password", "test"])


@pytest.mark.parametrize("rows", [[], [("first_user", 1, 0), ("second_user", 0, 1)]])
def test_list_users_reads_only_public_account_metadata_without_a_prompt(rows) -> None:
    session = MagicMock(spec=Session)
    session.execute.return_value.all.return_value = rows
    prompt = MagicMock(side_effect=AssertionError("unexpected prompt"))
    output = []
    assert (
        cli.execute_command(
            cli.build_parser().parse_args(["list-users"]),
            session,
            input_function=prompt,
            password_function=prompt,
            output_function=output.append,
        )
        == cli.EXIT_SUCCESS
    )
    query = session.execute.call_args.args[0]
    assert list(query.selected_columns.keys()) == ["username", "is_active", "is_admin"]
    assert "ORDER BY users.id" in str(query)
    session.commit.assert_not_called()
    assert output == (
        ["登录名\t状态\t角色", "first_user\t启用\t普通用户", "second_user\t停用\t管理员"]
        if rows
        else ["本站尚未注册账号。"]
    )


def test_reset_aborts_before_password_input_if_terminal_cannot_hide_it(monkeypatch) -> None:
    def no_terminal(_prompt):
        warnings.warn("would fall back to echoed input", cli.getpass.GetPassWarning, stacklevel=2)
        pytest.fail("echoed input reached")

    monkeypatch.setattr(cli.getpass, "getpass", no_terminal)
    change = MagicMock()
    monkeypatch.setattr(cli, "reset_user_password", change)
    lines = []
    result = cli.execute_command(
        cli.build_parser().parse_args(["reset-password", "--username", "managed_user"]),
        MagicMock(spec=Session),
        output_function=lines.append,
    )
    assert result == cli.EXIT_INPUT_ERROR
    assert "隐藏输入" in lines[0]
    change.assert_not_called()


def test_create_admin_uses_hidden_confirmed_password_and_safe_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["create-admin"])
    session = MagicMock(spec=Session)
    create_mock = MagicMock(return_value=user(is_admin=1))
    monkeypatch.setattr(cli, "create_admin_user", create_mock)
    prompts = iter(["strong secret", "strong secret"])
    outputs: list[str] = []

    result = cli.execute_command(
        args,
        session,
        input_function=lambda _prompt: "Managed_User",
        password_function=lambda _prompt: next(prompts),
        output_function=outputs.append,
    )

    assert result == cli.EXIT_SUCCESS
    create_mock.assert_called_once_with(session, "Managed_User", "strong secret")
    assert "strong secret" not in "".join(outputs)
    assert "首次登录后请完善本人信息" in outputs[0]


def test_password_confirmation_mismatch_stops_before_database_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["reset-password", "--username", "managed_user"])
    session = MagicMock(spec=Session)
    reset_mock = MagicMock()
    monkeypatch.setattr(cli, "reset_user_password", reset_mock)
    prompts = iter(["first password", "different password"])
    outputs: list[str] = []

    result = cli.execute_command(
        args,
        session,
        password_function=lambda _prompt: next(prompts),
        output_function=outputs.append,
    )

    assert result == cli.EXIT_INPUT_ERROR
    reset_mock.assert_not_called()
    assert "不一致" in outputs[0]
    assert "first password" not in outputs[0]


def test_reset_password_reports_old_login_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["reset-password", "--username", "managed_user"])
    session = MagicMock(spec=Session)
    reset_mock = MagicMock(return_value=user())
    monkeypatch.setattr(cli, "reset_user_password", reset_mock)
    outputs: list[str] = []

    result = cli.execute_command(
        args,
        session,
        password_function=lambda _prompt: "new secure password",
        output_function=outputs.append,
    )

    assert result == cli.EXIT_SUCCESS
    assert "原有登录凭证已失效" in outputs[0]


@pytest.mark.parametrize(
    ("flag", "expected_state", "expected_text"),
    [("--active", True, "已启用"), ("--inactive", False, "已停用")],
)
def test_set_user_active_requires_explicit_state_and_no_password(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    expected_state: bool,
    expected_text: str,
) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["set-user-active", "--username", "managed_user", flag])
    session = MagicMock(spec=Session)
    managed_user = user(is_active=int(expected_state))
    active_mock = MagicMock(return_value=managed_user)
    monkeypatch.setattr(cli, "set_user_active", active_mock)
    password_mock = MagicMock(side_effect=AssertionError("不应读取密码"))
    outputs: list[str] = []

    result = cli.execute_command(
        args,
        session,
        password_function=password_mock,
        output_function=outputs.append,
    )

    assert result == cli.EXIT_SUCCESS
    active_mock.assert_called_once_with(session, "managed_user", is_active=expected_state)
    password_mock.assert_not_called()
    assert expected_text in outputs[0]


def test_cli_missing_user_returns_safe_message(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["set-user-active", "--username", "missing_user", "--inactive"])
    monkeypatch.setattr(cli, "set_user_active", MagicMock(side_effect=UserNotFoundError))
    outputs: list[str] = []

    result = cli.execute_command(args, MagicMock(spec=Session), output_function=outputs.append)

    assert result == cli.EXIT_INPUT_ERROR
    assert outputs == ["操作未执行：找不到该账号。"]


def test_cli_cancelled_input_does_not_call_management_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(["create-admin"])
    create_mock = MagicMock()
    monkeypatch.setattr(cli, "create_admin_user", create_mock)
    outputs: list[str] = []

    result = cli.execute_command(
        args,
        MagicMock(spec=Session),
        input_function=MagicMock(side_effect=KeyboardInterrupt),
        output_function=outputs.append,
    )

    assert result == cli.EXIT_INPUT_ERROR
    create_mock.assert_not_called()
    assert outputs == ["操作已取消，数据库未修改。"]


def test_cli_configuration_error_does_not_print_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "Settings", MagicMock(side_effect=RuntimeError("hidden detail")))

    result = cli.main(["set-user-active", "--username", "user_name", "--inactive"])

    captured = capsys.readouterr()
    assert result == cli.EXIT_SYSTEM_ERROR
    assert "hidden detail" not in captured.err
    assert "数据库未修改" in captured.err
