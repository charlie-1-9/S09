from __future__ import annotations

import asyncio
import logging
import re
import secrets
import smtplib
import uuid
from email.message import EmailMessage
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask

from .auth import _session_payload, open_session
from .config import Settings
from .db import utcnow
from .models import User
from .security import ApiError

logger = logging.getLogger(__name__)

router = APIRouter()

# Valor que nunca coincide con un hash scrypt valido: _verify_password lo rechaza,
# asi que una cuenta de Google no puede entrar con usuario y contrasena.
UNUSABLE_PASSWORD = "oauth$google"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: StrictStr


def verify_google_credential(credential: str, client_id: str) -> dict[str, Any]:
    """Valida el JWT de Google (firma, audiencia y vencimiento). Bloquea: usar en un hilo."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    info = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    if info.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("wrong issuer")
    return info


def _username_from_email(email: str) -> str:
    local = re.sub(r"[^a-z0-9_]", "", email.split("@", 1)[0].lower())[:18] or "user"
    return f"{local}_{secrets.token_hex(3)}"


def send_welcome_email(settings: Settings, to: str, name: str) -> None:
    message = EmailMessage()
    message["Subject"] = "Bienvenido a FLASH"
    message["From"] = settings.smtp_from
    message["To"] = to
    message.set_content(
        f"Hola {name},\n\n"
        f"Tu cuenta de FLASH quedó registrada con el correo {to}.\n"
        "Ya puedes comprar en el drop y ver tus comprobantes.\n\n"
        "Si no fuiste tú, ignora este mensaje.\n"
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
            smtp.send_message(message)
    except Exception as exc:  # el correo nunca debe romper el login
        logger.warning("welcome email failed type=%s", type(exc).__name__)


@router.get("/api/v1/auth/config")
async def auth_config(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    return JSONResponse(
        content={"google_client_id": settings.google_client_id.strip() or None}
    )


@router.post("/api/v1/auth/google")
async def google_login(payload: GoogleLogin, request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if not settings.google_enabled():
        raise ApiError(503, "GOOGLE_DISABLED", "El inicio de sesión con Google no está configurado.")

    try:
        info = await asyncio.to_thread(
            verify_google_credential, payload.credential, settings.google_client_id.strip()
        )
    except Exception:
        raise ApiError(401, "INVALID_GOOGLE_TOKEN", "No se pudo validar tu cuenta de Google.") from None

    subject = info.get("sub")
    email = info.get("email")
    if not isinstance(subject, str) or not isinstance(email, str) or not info.get("email_verified"):
        raise ApiError(401, "INVALID_GOOGLE_TOKEN", "Tu correo de Google no está verificado.")
    email = email.strip().lower()
    raw_name = info.get("name")
    name = (raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else email.split("@")[0])[:120]

    session_factory = request.app.state.db.session_factory
    is_new = False
    user: User | None = None
    for _ in range(3):
        try:
            async with session_factory() as session:
                async with session.begin():
                    user = (
                        await session.execute(select(User).where(User.google_sub == subject))
                    ).scalar_one_or_none()
                    if user is None:
                        user = User(
                            id=uuid.uuid4(),
                            username=_username_from_email(email),
                            password_hash=UNUSABLE_PASSWORD,
                            email=email,
                            google_sub=subject,
                            display_name=name,
                            created_at=utcnow(),
                        )
                        session.add(user)
                        is_new = True
                    else:
                        user.email = email
                        user.display_name = name
            break
        except IntegrityError:
            user = None  # carrera por username/email/sub: reintenta
    if user is None:
        raise ApiError(409, "ACCOUNT_CONFLICT", "No se pudo crear la cuenta. Intenta de nuevo.")

    token = await open_session(session_factory, user.id)
    background = (
        BackgroundTask(send_welcome_email, settings, email, name)
        if is_new and settings.smtp_enabled()
        else None
    )
    return JSONResponse(
        status_code=201 if is_new else 200,
        content=_session_payload(token, user.id, user.username, user.display_name),
        background=background,
    )
