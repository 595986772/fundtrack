"""FundScope 本地账号、密码哈希和服务端 Session。

浏览器 Cookie 中只有随机 Token；数据库保存 Token 的 SHA-256，用户密码则使用带随机
salt 的 PBKDF2。业务 Router 不再自行信任请求中的 user_id，而是统一通过
``CurrentPrincipal.resolve_user_id`` 获取当前用户。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import Database, runtime_database
from app.models import AppUserModel, AuthSessionModel
from app.services.ai_governance_context import bind_ai_user


PASSWORD_ITERATIONS = 310_000


class AuthConflictError(ValueError):
    """注册账号已存在。"""


class InvalidCredentialsError(ValueError):
    """账号或密码不正确。"""


@dataclass(frozen=True)
class CurrentPrincipal:
    """Router 使用的可信身份；legacy=True 只存在于本地迁移模式。"""

    user_id: str | None
    display_name: str
    role: str
    authenticated: bool
    legacy: bool = False

    def resolve_user_id(self, requested_user_id: str | None = None) -> str:
        """登录态永远覆盖浏览器参数；迁移模式才允许旧 user_id。"""

        if self.authenticated and self.user_id:
            bind_ai_user(self.user_id)
            return self.user_id
        candidate = str(requested_user_id or "guest").strip()
        resolved = candidate[:50] or "guest"
        bind_ai_user(resolved)
        return resolved


class AuthService:
    """用 SQLAlchemy 持久化账号和可撤销登录会话。"""

    def __init__(self, database: Database = runtime_database) -> None:
        self.database = database
        self.database.create_schema()

    def register(
        self,
        *,
        user_id: str,
        display_name: str,
        password: str,
    ) -> tuple[CurrentPrincipal, str]:
        """创建账号并直接签发登录 Session；第一个账号成为本地管理员。"""

        normalized_id = user_id.strip().lower()
        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            user_count = int(
                session.scalar(select(func.count()).select_from(AppUserModel)) or 0
            )
            user = AppUserModel(
                id=normalized_id,
                display_name=display_name.strip(),
                password_hash=self._hash_password(password),
                role="admin" if user_count == 0 else "user",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            try:
                session.flush()
            except IntegrityError as exc:
                raise AuthConflictError(f"账号已存在：{normalized_id}") from exc

        token = self._create_session(user_id=normalized_id)
        return self._principal(user), token

    def login(self, *, user_id: str, password: str) -> tuple[CurrentPrincipal, str]:
        """校验密码并轮换一枚新的随机 Session Token。"""

        normalized_id = user_id.strip().lower()
        with self.database.session() as session:
            user = session.get(AppUserModel, normalized_id)
        if user is None or not self._verify_password(password, user.password_hash):
            # 不区分“账号不存在”和“密码错误”，避免接口泄露账号枚举信息。
            raise InvalidCredentialsError("账号或密码不正确")

        token = self._create_session(user_id=user.id)
        return self._principal(user), token

    def authenticate(self, token: str | None) -> CurrentPrincipal | None:
        """校验 Cookie 中的原始 Token，并返回对应账号。"""

        if not token:
            return None
        token_hash = self._token_hash(token)
        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            auth_session = session.get(AuthSessionModel, token_hash)
            if auth_session is None:
                return None
            expires_at = self._aware(auth_session.expires_at)
            if expires_at <= now:
                session.delete(auth_session)
                return None
            user = session.get(AppUserModel, auth_session.user_id)
            if user is None:
                session.delete(auth_session)
                return None
            auth_session.last_seen_at = now
            return self._principal(user)

    def logout(self, token: str | None) -> None:
        """撤销当前浏览器的 Session；其他设备保持登录。"""

        if not token:
            return
        with self.database.session() as session, session.begin():
            auth_session = session.get(AuthSessionModel, self._token_hash(token))
            if auth_session is not None:
                session.delete(auth_session)

    def _create_session(self, *, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self.database.session() as session, session.begin():
            session.add(
                AuthSessionModel(
                    token_hash=self._token_hash(token),
                    user_id=user_id,
                    created_at=now,
                    expires_at=now + timedelta(days=settings.auth_session_days),
                    last_seen_at=now,
                )
            )
        return token

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PASSWORD_ITERATIONS,
        )
        return (
            f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"
            f"{salt.hex()}${digest.hex()}"
        )

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, iterations, salt_hex, expected_hex = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(actual.hex(), expected_hex)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _principal(user: AppUserModel) -> CurrentPrincipal:
        return CurrentPrincipal(
            user_id=user.id,
            display_name=user.display_name,
            role=user.role,
            authenticated=True,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


auth_service = AuthService()


def request_session_token(request: Request) -> str | None:
    """小程序显式携带 Bearer，网页继续使用 Cookie；两端共用可撤销的 Session。

    显式传了 Authorization 就不能退回 Cookie，否则错误账号的令牌可能意外使用
    浏览器中的另一个身份。这里不接收 URL 中的令牌，避免落进访问日志。
    """

    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip() or " " in token.strip():
            raise HTTPException(status_code=401, detail="登录凭证无效，请重新登录")
        return token.strip()
    return request.cookies.get(settings.auth_cookie_name)


async def get_current_principal(request: Request) -> CurrentPrincipal:
    """FastAPI 依赖：严格模式要求登录，本地迁移模式返回 legacy principal。"""

    token = request_session_token(request)
    principal = auth_service.authenticate(token)
    if principal is not None:
        bind_ai_user(principal.user_id)
        return principal
    # 即使本地模式允许匿名，也不能把过期的显式令牌降级成任意 user_id。
    if settings.auth_required or "authorization" in request.headers:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录 FundScope",
        )
    return CurrentPrincipal(
        user_id=None,
        display_name="本地工作区",
        role="admin",
        authenticated=False,
        legacy=True,
    )


async def require_admin(request: Request) -> CurrentPrincipal:
    """开发者和评测入口使用的管理员依赖。"""

    principal = await get_current_principal(request)
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return principal
