# ruff: noqa: RUF002
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request, status
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.security import (
    AUTH_COOKIE_NAME,
    csrf_token_is_valid,
    request_origin_is_trusted,
)
from app.core.config import Settings
from app.core.database import probe_database
from app.core.security import (
    SecurityConfigurationError,
    TokenError,
    access_token_matches_password,
    decode_access_token,
)
from app.db.models import Contact, User
from app.db.session import open_session
from app.services.auth import get_active_user
from app.services.gift_change_auth import GiftChangeReauthStore
from app.services.profile import get_self_contact
from app.services.sensitive_actions import SensitiveActionError, SensitiveActionStore


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDependency = Annotated[Settings, Depends(get_app_settings)]


def database_is_ready(settings: SettingsDependency) -> bool:
    return probe_database(settings)


def get_database_session(settings: SettingsDependency) -> Iterator[Session]:
    yield from open_session(settings)


DatabaseSessionDependency = Annotated[Session, Depends(get_database_session)]


# 鉴权与本人建档门禁使用独立只读 Session。这样它们不会在业务 Session 中提前建立
# MySQL REPEATABLE READ 快照; 后续写服务拿到用户行锁后读取的一定是锁生效时的账本状态。
def get_authentication_session(settings: SettingsDependency) -> Iterator[Session]:
    yield from open_session(settings)


AuthenticationSessionDependency = Annotated[
    Session,
    Depends(get_authentication_session),
]


def require_trusted_origin(request: Request, settings: SettingsDependency) -> None:
    if not request_origin_is_trusted(request, settings):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "ORIGIN_NOT_ALLOWED",
            "请求来源不受信任",
        )


TrustedOriginDependency = Annotated[None, Depends(require_trusted_origin)]


def require_csrf_token(
    request: Request,
    _trusted_origin: TrustedOriginDependency,
) -> None:
    if not csrf_token_is_valid(request):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "CSRF_VALIDATION_FAILED",
            "安全校验失败，请刷新页面后重试",
        )


CsrfProtectionDependency = Annotated[None, Depends(require_csrf_token)]


def get_current_user(
    request: Request,
    settings: SettingsDependency,
    session: AuthenticationSessionDependency,
) -> User:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "AUTHENTICATION_REQUIRED",
            "请先登录",
            {"WWW-Authenticate": "Cookie"},
        )

    try:
        claims = decode_access_token(token, settings.jwt_secret)
    except SecurityConfigurationError as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_NOT_CONFIGURED",
            "登录功能尚未完成安全配置",
        ) from exc
    except TokenError as exc:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_AUTHENTICATION",
            "登录状态无效或已过期，请重新登录",
            {"WWW-Authenticate": "Cookie"},
        ) from exc

    user = get_active_user(session, claims.user_id)
    if user is None or not access_token_matches_password(
        claims,
        user.password_hash,
        settings.jwt_secret,
    ):
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_AUTHENTICATION",
            "登录状态无效或账号已停用，请重新登录",
            {"WWW-Authenticate": "Cookie"},
        )
    return user


CurrentUserDependency = Annotated[User, Depends(get_current_user)]


def get_session_id(request: Request, settings: SettingsDependency) -> str:
    """从登录令牌中取出每次登录随机生成的会话标识（``jti``）。"""
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "AUTHENTICATION_REQUIRED",
            "请先登录",
            {"WWW-Authenticate": "Cookie"},
        )
    try:
        claims = decode_access_token(token, settings.jwt_secret)
    except SecurityConfigurationError as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_NOT_CONFIGURED",
            "登录功能尚未完成安全配置",
        ) from exc
    except TokenError as exc:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_AUTHENTICATION",
            "登录状态无效或已过期，请重新登录",
            {"WWW-Authenticate": "Cookie"},
        ) from exc
    return claims.session_id


SessionIdDependency = Annotated[str, Depends(get_session_id)]


def get_gift_change_reauth_store(request: Request) -> GiftChangeReauthStore:
    return request.app.state.gift_change_reauth_store


GiftChangeReauthStoreDependency = Annotated[
    GiftChangeReauthStore,
    Depends(get_gift_change_reauth_store),
]


def require_gift_change_reauth(
    session_id: SessionIdDependency,
    store: GiftChangeReauthStoreDependency,
) -> None:
    if not store.is_verified(session_id):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "GIFT_CHANGE_REAUTH_REQUIRED",
            "请先验证密码后再修改或删除流水",
        )


GiftChangeReauthDependency = Annotated[None, Depends(require_gift_change_reauth)]


def get_sensitive_action_store(request: Request) -> SensitiveActionStore:
    return request.app.state.sensitive_action_store


SensitiveActionStoreDependency = Annotated[
    SensitiveActionStore, Depends(get_sensitive_action_store)
]


def require_sensitive_action_csrf(request: Request, settings: SettingsDependency) -> None:
    """GET downloads need CSRF too; browsers omit Origin on same-origin GETs."""
    if request.method != "GET" or request.headers.get("origin") is not None:
        require_trusted_origin(request, settings)
    else:
        fetch_site = request.headers.get("sec-fetch-site")
        referer = request.headers.get("referer")
        referer_origin = ""
        if referer:
            try:
                source = urlsplit(referer)
                referer_origin = f"{source.scheme}://{source.netloc}"
            except ValueError:
                pass
        if fetch_site not in {None, "same-origin"} or not (
            fetch_site == "same-origin" or referer_origin in settings.trusted_origin_list
        ):
            raise ApiError(status.HTTP_403_FORBIDDEN, "ORIGIN_NOT_ALLOWED", "请求来源不受信任")
    if not csrf_token_is_valid(request):
        raise ApiError(
            status.HTTP_403_FORBIDDEN, "CSRF_VALIDATION_FAILED", "安全校验失败，请刷新页面后重试"
        )


SensitiveActionCsrfDependency = Annotated[None, Depends(require_sensitive_action_csrf)]


def sensitive_action_api_error(exc: SensitiveActionError) -> ApiError:
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return ApiError(
        status.HTTP_429_TOO_MANY_REQUESTS if exc.retry_after else status.HTTP_403_FORBIDDEN,
        exc.code,
        exc.message,
        headers,
    )


class SensitiveActionGate:
    def __init__(
        self, request: Request, user_id: int, session_id: str, store: SensitiveActionStore
    ) -> None:
        self.request = request
        self.user_id = user_id
        self.session_id = session_id
        self.store = store

    def authorize(self, purpose: str, context: str = "") -> None:
        try:
            self.store.consume(
                self.request.headers.get("X-Operation-Grant"),
                user_id=self.user_id,
                session_id=self.session_id,
                purpose=purpose,
                context=context,
            )
        except SensitiveActionError as exc:
            raise sensitive_action_api_error(exc) from exc


def get_sensitive_action_gate(
    request: Request,
    current_user: CurrentUserDependency,
    session_id: SessionIdDependency,
    store: SensitiveActionStoreDependency,
    _csrf: SensitiveActionCsrfDependency,
) -> SensitiveActionGate:
    return SensitiveActionGate(request, current_user.id, session_id, store)


SensitiveActionGateDependency = Annotated[SensitiveActionGate, Depends(get_sensitive_action_gate)]


def require_admin(current_user: CurrentUserDependency) -> User:
    """统一管理员门禁：普通用户一律返回 403，不因目标编号不同泄漏资源是否存在。"""
    if not current_user.is_admin:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "ADMIN_REQUIRED",
            "该操作仅限管理员",
        )
    return current_user


AdminUserDependency = Annotated[User, Depends(require_admin)]


def get_completed_profile(
    current_user: CurrentUserDependency,
    session: AuthenticationSessionDependency,
) -> Contact:
    profile = get_self_contact(session, current_user.id)
    if profile is None:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "PROFILE_REQUIRED",
            "请先完善本人信息后再进入账本",
        )
    return profile


CompletedProfileDependency = Annotated[Contact, Depends(get_completed_profile)]
