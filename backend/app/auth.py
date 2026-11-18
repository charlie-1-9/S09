from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .db import utcnow
from .models import AuthSession, User
from .security import ApiError

router = APIRouter()

SESSION_HOURS = 12
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 600

_failures: dict[str, list[float]] = {}


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: StrictStr = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: StrictStr = Field(min_length=8, max_length=128)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


_DUMMY_HASH = _hash_password("contrasena-de-relleno")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_throttle(username: str) -> None:
    now = time.monotonic()
    recent = [t for t in _failures.get(username, []) if now - t < FAILURE_WINDOW_SECONDS]
    if recent:
        _failures[username] = recent
    else:
        _failures.pop(username, None)
    if len(recent) >= MAX_FAILURES:
        raise ApiError(429, "RATE_LIMITED", "Demasiados intentos. Espera unos minutos.")


def _record_failure(username: str) -> None:
    if len(_failures) > 10000:
        _failures.clear()
    _failures.setdefault(username, []).append(time.monotonic())


def _bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value or len(value) > 200:
        return None
    return value


async def get_optional_user(request: Request, strict: bool = True) -> User | None:
    """Devuelve el usuario de la sesion, o None si no se envio token.

    Con strict=True, un token enviado pero invalido o vencido responde 401.
    """
    token = _bearer_token(request)
    if token is None:
        return None
    async with request.app.state.db.session_factory() as session:
        user = (
            await session.execute(
                select(User)
                .join(AuthSession, AuthSession.user_id == User.id)
                .where(
                    AuthSession.token_hash == _hash_token(token),
                    AuthSession.expires_at > utcnow(),
                )
            )
        ).scalar_one_or_none()
    if user is None and strict:
        raise ApiError(401, "UNAUTHORIZED", "Sesi\u00f3n inv\u00e1lida o vencida.")
    return user


def _session_payload(
    token: str, user_id: uuid.UUID, username: str, name: str | None = None
) -> dict[str, object]:
    return {
        "token": token,
        "user": {"id": str(user_id), "username": username, "name": name or username},
    }


async def open_session(session_factory, user_id: uuid.UUID) -> str:
    """Crea una sesion nueva para el usuario y devuelve el token en claro."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AuthSession).where(AuthSession.expires_at < now))
            session.add(
                AuthSession(
                    token_hash=_hash_token(token),
                    user_id=user_id,
                    created_at=now,
                    expires_at=now + timedelta(hours=SESSION_HOURS),
                )
            )
    return token


@router.post("/api/v1/auth/register")
async def register(payload: Credentials, request: Request) -> JSONResponse:
    # El registro es solo con Google (correo verificado): ver google_auth.py.
    raise ApiError(
        403,
        "REGISTRATION_DISABLED",
        "El registro con usuario y contrase\u00f1a est\u00e1 desactivado. Usa Google.",
    )
    username = payload.username.lower()
    password_hash = await asyncio.to_thread(_hash_password, payload.password)
    token = secrets.token_urlsafe(32)
    now = utcnow()
    user_id = uuid.uuid4()
    try:
        async with request.app.state.db.session_factory() as session:
            async with session.begin():
                session.add(
                    User(id=user_id, username=username, password_hash=password_hash, created_at=now)
                )
                await session.flush()
                session.add(
                    AuthSession(
                        token_hash=_hash_token(token),
                        user_id=user_id,
                        created_at=now,
                        expires_at=now + timedelta(hours=SESSION_HOURS),
                    )
                )
    except IntegrityError:
        raise ApiError(409, "USERNAME_TAKEN", "Ese nombre de usuario ya existe.") from None
    return JSONResponse(status_code=201, content=_session_payload(token, user_id, username))


@router.post("/api/v1/auth/login")
async def login(payload: Credentials, request: Request) -> JSONResponse:
    username = payload.username.lower()
    _check_throttle(username)
    session_factory = request.app.state.db.session_factory
    async with session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
    stored = user.password_hash if user is not None else _DUMMY_HASH
    valid = await asyncio.to_thread(_verify_password, payload.password, stored)
    if user is None or not valid:
        _record_failure(username)
        raise ApiError(401, "INVALID_CREDENTIALS", "Usuario o contrase\u00f1a incorrectos.")
    _failures.pop(username, None)

    token = secrets.token_urlsafe(32)
    now = utcnow()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(AuthSession).where(AuthSession.expires_at < now))
            session.add(
                AuthSession(
                    token_hash=_hash_token(token),
                    user_id=user.id,
                    created_at=now,
                    expires_at=now + timedelta(hours=SESSION_HOURS),
                )
            )
    return JSONResponse(
        content=_session_payload(token, user.id, user.username, user.display_name)
    )


@router.get("/api/v1/auth/me")
async def me(request: Request) -> JSONResponse:
    user = await get_optional_user(request)
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "Inicia sesi\u00f3n para continuar.")
    return JSONResponse(
        content={
            "id": str(user.id),
            "username": user.username,
            "name": user.display_name or user.username,
        }
    )


@router.post("/api/v1/auth/logout")
async def logout(request: Request) -> JSONResponse:
    token = _bearer_token(request)
    if token is not None:
        async with request.app.state.db.session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(AuthSession).where(AuthSession.token_hash == _hash_token(token))
                )
    return JSONResponse(content={"ok": True})
