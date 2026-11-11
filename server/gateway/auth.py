"""认证与鉴权（gateway/auth.py）。

JWT（HS256）无状态令牌：登录签发，受保护路由用依赖 ``require_admin`` 校验。
密钥与有效期来自 config.server。密码校验委托 UserRepo（PBKDF2 加盐哈希）。
"""
from __future__ import annotations

import logging
from uuid import uuid4
from typing import Optional

import jwt
from fastapi import Depends, Header, Cookie, HTTPException, status

from ..core.config import AppConfig, get_config
from ..core.errors import AuthError
from .context import Container, get_container

log = logging.getLogger("gateway.auth")

_ALGO = "HS256"


def create_token(username: str, role: str = "admin",
                 cfg: Optional[AppConfig] = None) -> str:
    from datetime import datetime, timedelta, timezone
    cfg = cfg or get_config()
    # 统一用 aware UTC，避免 Windows/UTC+8 下 naive datetime.timestamp() 被当本地时间。
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username, "role": role,
        "jti": uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=cfg.server.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, cfg.server.jwt_secret, algorithm=_ALGO)


def decode_token(token: str, cfg: Optional[AppConfig] = None) -> dict:
    cfg = cfg or get_config()
    try:
        return jwt.decode(token, cfg.server.jwt_secret, algorithms=[_ALGO],
                          options={"require": ["exp", "iat", "sub", "jti", "role"]})
    except jwt.PyJWTError as exc:
        raise AuthError(f"令牌无效或已过期: {exc}") from exc


def login(username: str, password: str, c: Container) -> dict:
    user = c.user_repo.authenticate(username, password)
    if user is None:
        raise AuthError("用户名或密码错误")
    token = create_token(user.username, user.role, c.cfg)
    return {"token": token, "username": user.username, "role": user.role,
            "expires_in": c.cfg.server.jwt_expire_minutes * 60}


def _extract(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Authorization 头",
                            headers={"WWW-Authenticate": "Bearer"})
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authorization 格式应为 Bearer <token>")
    return parts[1]


async def require_admin(authorization: Optional[str] = Header(None),
                        parking_session: Optional[str] = Cookie(None),
                        container: Container = Depends(get_container)) -> dict:
    """受保护路由依赖：校验 JWT，返回 {sub, role}。"""
    token = _extract(authorization or (f"Bearer {parking_session}" if parking_session else None))
    try:
        payload = validate_token(token, container)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.message) from exc
    if payload.get("role") not in ("admin", "operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
    return payload


def validate_token(token, container):
    from ..repository.models import RevokedToken
    payload = decode_token(token, container.cfg)
    try:
        if container.db.session.get(RevokedToken, payload['jti']) is not None:
            raise AuthError("令牌已登出")
    finally:
        # Authentication must not reserve one pool connection for every queued request.
        if not container.db.in_scope:
            container.db.remove()
    return payload
