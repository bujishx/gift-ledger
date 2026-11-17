import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pydantic import SecretStr

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "gift-ledger"
JWT_AUDIENCE = "gift-ledger-web"
JWT_LIFETIME = timedelta(days=7)
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{3,29}$")

WEAK_PASSWORDS = frozenset(
    {
        "00000000",
        "11111111",
        "12345678",
        "123456789",
        "1234567890",
        "abc12345",
        "admin123",
        "iloveyou",
        "password",
        "password123",
        "qwerty123",
        "qwertyuiop",
    }
)

password_hasher = PasswordHash.recommended()
dummy_password_hash = password_hasher.hash("dummy-password-for-timing-check-only")


class UsernamePolicyError(ValueError):
    pass


class PasswordPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TokenError(ValueError):
    pass


class SecurityConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: int
    password_version: str
    session_id: str


def normalize_username(username: str) -> str:
    normalized = username.lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise UsernamePolicyError(
            "用户名须为 4—30 位，并以英文字母开头，只能包含小写字母、数字和下划线"
        )
    return normalized


def validate_new_password(password: str) -> None:
    if len(password) < 8:
        raise PasswordPolicyError("PASSWORD_TOO_SHORT", "密码至少需要 8 个字符")
    if len(password) > 128:
        raise PasswordPolicyError("PASSWORD_TOO_LONG", "密码最多允许 128 个字符")
    if password.isspace():
        raise PasswordPolicyError("PASSWORD_ONLY_WHITESPACE", "密码不能全部是空格")
    if password.casefold() in WEAK_PASSWORDS:
        raise PasswordPolicyError("PASSWORD_TOO_WEAK", "该密码过于常见，请换一个不容易猜到的密码")


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password, password_hash)
    except (PwdlibError, ValueError):
        return False


def verify_unknown_user_password(password: str) -> None:
    verify_password(password, dummy_password_hash)


def require_jwt_secret(secret: SecretStr | None) -> str:
    if secret is None or len(secret.get_secret_value().encode()) < 32:
        raise SecurityConfigurationError("登录签名密钥尚未配置或长度不足 32 字节")
    return secret.get_secret_value()


def password_version(password_hash: str, secret: SecretStr | None) -> str:
    return hmac.new(
        require_jwt_secret(secret).encode(),
        password_hash.encode(),
        hashlib.sha256,
    ).hexdigest()


def create_access_token(
    user_id: int,
    secret: SecretStr | None,
    password_hash: str,
    *,
    now: datetime | None = None,
) -> str:
    if user_id <= 0:
        raise ValueError("user_id 必须是正整数")
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME,
        "jti": secrets.token_urlsafe(16),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "typ": "access",
        "pwdv": password_version(password_hash, secret),
    }
    return jwt.encode(payload, require_jwt_secret(secret), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: SecretStr | None) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            require_jwt_secret(secret),
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["sub", "iat", "exp", "jti", "iss", "aud", "typ", "pwdv"]},
        )
        if payload["typ"] != "access":
            raise TokenError("令牌类型无效")
        user_id = int(payload["sub"])
        if user_id <= 0 or str(user_id) != payload["sub"]:
            raise TokenError("令牌账号编号无效")
        password_state = payload["pwdv"]
        if (
            not isinstance(password_state, str)
            or len(password_state) != 64
            or any(character not in "0123456789abcdef" for character in password_state)
        ):
            raise TokenError("令牌密码版本无效")
        session_id = payload["jti"]
        if not isinstance(session_id, str) or not session_id:
            raise TokenError("令牌会话标识无效")
        return AccessTokenClaims(
            user_id=user_id,
            password_version=password_state,
            session_id=session_id,
        )
    except SecurityConfigurationError:
        raise
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise TokenError("登录凭证无效或已过期") from exc


def access_token_matches_password(
    claims: AccessTokenClaims,
    password_hash: str,
    secret: SecretStr | None,
) -> bool:
    return hmac.compare_digest(
        claims.password_version,
        password_version(password_hash, secret),
    )
