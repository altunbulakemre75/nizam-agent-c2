"""JWT authentication — paylaşılan yardımcılar.

HS256 basit JWT; NIZAM_JWT_SECRET env'den okunur. Üretimde asimetrik
(RS256) + public key dağıtım + token süresi 15dk önerilir.

Kullanım:
    token = issue_token(subject="opr-01", role="operator", ttl_s=3600)
    payload = verify_token(token)   # dict veya raise AuthError
"""
from __future__ import annotations

import os
import time
from typing import Any

import jwt   # PyJWT (requirements.txt)

JWT_ALG = "HS256"
DEFAULT_TTL_S = 3600


class AuthError(Exception):
    """Token geçersiz, süresi dolmuş veya imza hatalı."""


def _secret() -> str:
    secret = os.getenv("NIZAM_JWT_SECRET")
    if not secret:
        raise AuthError("NIZAM_JWT_SECRET env'i set edilmeli (üretimde >=32 karakter)")
    if len(secret) < 16:
        raise AuthError("NIZAM_JWT_SECRET çok kısa (min 16 karakter)")
    return secret


def issue_token(subject: str, role: str = "operator", ttl_s: int = DEFAULT_TTL_S, **extra: Any) -> str:
    """Token üret. sub = kullanıcı ID, role = operator/admin/service."""
    now = int(time.time())
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + ttl_s,
        **extra,
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALG)


def verify_token(token: str) -> dict:
    """Token'ı doğrula ve payload döndür. Raise AuthError yanlışsa."""
    try:
        return jwt.decode(token, _secret(), algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {exc}") from exc
