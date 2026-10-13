"""FundScope 注册、登录、当前账号和退出接口。"""

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import settings
from app.schemas import (
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthStateResponse,
    AuthTokenResponse,
    AuthUserInfo,
)
from app.services.auth import (
    AuthConflictError,
    InvalidCredentialsError,
    auth_service,
    request_session_token,
)


router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


def _state(principal) -> AuthStateResponse:
    return AuthStateResponse(
        authenticated=True,
        auth_required=settings.auth_required,
        user=AuthUserInfo(
            user_id=principal.user_id,
            display_name=principal.display_name,
            role=principal.role,
        ),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    """Cookie 不暴露给 JavaScript；SameSite=Lax 保护普通跨站 POST。"""

    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/register", response_model=AuthStateResponse, status_code=201)
async def register(request: AuthRegisterRequest, response: Response):
    try:
        principal, token = auth_service.register(
            user_id=request.user_id,
            display_name=request.display_name,
            password=request.password,
        )
    except AuthConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _set_session_cookie(response, token)
    return _state(principal)


@router.post("/login", response_model=AuthStateResponse)
async def login(request: AuthLoginRequest, response: Response):
    try:
        principal, token = auth_service.login(
            user_id=request.user_id,
            password=request.password,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    _set_session_cookie(response, token)
    return _state(principal)


@router.get("/me", response_model=AuthStateResponse)
async def current_auth_state(request: Request):
    principal = auth_service.authenticate(
        request_session_token(request)
    )
    if principal is None:
        return AuthStateResponse(
            authenticated=False,
            auth_required=settings.auth_required,
        )
    return _state(principal)


@router.post("/logout", response_model=AuthStateResponse)
async def logout(request: Request, response: Response):
    auth_service.logout(request_session_token(request))
    response.delete_cookie(settings.auth_cookie_name, path="/")
    return AuthStateResponse(
        authenticated=False,
        auth_required=settings.auth_required,
    )


@router.post("/token", response_model=AuthTokenResponse)
async def token_login(request: AuthLoginRequest, response: Response):
    """小程序账号密码登录，不假冒微信一键登录，也不影响网页的 HttpOnly Cookie。"""

    try:
        principal, token = auth_service.login(
            user_id=request.user_id, password=request.password,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return AuthTokenResponse(
        **_state(principal).model_dump(),
        access_token=token,
        expires_in=settings.auth_session_days * 24 * 60 * 60,
    )
