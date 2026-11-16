from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .db import Database, utcnow
from .models import BillingInvoice, Order

logger = logging.getLogger(__name__)

TRANSIENT_STATUS = {429, 500, 502, 503, 504}
LEASE_SECONDS = 60
MAX_AUTOMATIC_ATTEMPTS = 2


class BillingError(Exception):
    def __init__(self, code: str, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def build_invoice_payload(order: Order) -> dict[str, Any]:
    """Arma el contrato exacto desde el snapshot persistido, nunca desde el navegador."""
    snapshot = order.snapshot
    return {
        "order_id": str(order.id),
        "currency": "PEN",
        "items": [
            {
                "product_id": item["product_id"],
                "name": item["name"],
                "quantity": item["quantity"],
                "unit_price_cents": item["unit_price_cents"],
            }
            for item in snapshot["items"]
        ],
        "total_cents": snapshot["total_cents"],
    }


async def _post_invoice(
    client: httpx.AsyncClient,
    settings: Settings,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{settings.billing_base_url}/invoices",
            json=payload,
            headers={
                "X-Billing-Token": settings.billing_token.get_secret_value(),
                "Idempotency-Key": payload["order_id"],
            },
        )
    except httpx.TimeoutException:
        raise BillingError("TIMEOUT", True) from None
    except httpx.HTTPError:
        raise BillingError("NETWORK_ERROR", True) from None

    if response.status_code in (200, 201):
        try:
            body = response.json()
        except ValueError:
            raise BillingError("INVALID_RESPONSE", False) from None
        if (
            not isinstance(body, dict)
            or body.get("order_id") != payload["order_id"]
            or body.get("status") != "issued"
            or not _is_uuid(body.get("id"))
        ):
            raise BillingError("INVALID_RESPONSE", False)
        return body

    code = f"HTTP_{response.status_code}"
    try:
        api_code = response.json()["error"]["code"]
        if isinstance(api_code, str) and re.fullmatch(r"[A-Z_]{1,40}", api_code):
            code = f"{code}_{api_code}"
    except (ValueError, KeyError, TypeError):
        pass
    raise BillingError(code, response.status_code in TRANSIENT_STATUS)


async def _claim(session_factory: async_sessionmaker[AsyncSession], order_id: uuid.UUID) -> bool:
    now = utcnow()
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(BillingInvoice)
                .where(
                    BillingInvoice.order_id == order_id,
                    BillingInvoice.status == "pending",
                    or_(BillingInvoice.lease_until.is_(None), BillingInvoice.lease_until < now),
                )
                .values(lease_until=now + timedelta(seconds=LEASE_SECONDS), updated_at=now)
            )
            return result.rowcount == 1


async def issue_invoice(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    order_id: uuid.UUID,
) -> str:
    if not await _claim(session_factory, order_id):
        return "skipped"

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        payload = build_invoice_payload(order) if order is not None else None
    if payload is None:
        return "skipped"

    invoice: dict[str, Any] | None = None
    last_error = "UNKNOWN"
    attempts = 0
    async with httpx.AsyncClient(timeout=settings.billing_timeout_seconds) as client:
        for attempt in range(1, MAX_AUTOMATIC_ATTEMPTS + 1):
            attempts = attempt
            try:
                invoice = await _post_invoice(client, settings, payload)
                break
            except BillingError as exc:
                last_error = exc.code
                if not exc.transient:
                    break
                if attempt < MAX_AUTOMATIC_ATTEMPTS:
                    await asyncio.sleep(1.0)

    if invoice is not None:
        values: dict[str, Any] = {
            "status": "issued",
            "invoice_id": uuid.UUID(invoice["id"]),
            "invoice_number": str(invoice.get("number", ""))[:64] or None,
            "last_error": None,
        }
    else:
        values = {"status": "failed", "last_error": last_error[:200]}

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(BillingInvoice)
                .where(BillingInvoice.order_id == order_id)
                .values(
                    attempts=BillingInvoice.attempts + attempts,
                    lease_until=None,
                    updated_at=utcnow(),
                    **values,
                )
            )
    return values["status"]


async def billing_loop(database: Database, settings: Settings) -> None:
    warned = False
    while True:
        try:
            if not settings.billing_enabled():
                if not warned:
                    logger.warning("billing disabled: BILLING_BASE_URL or BILLING_TOKEN missing")
                    warned = True
                await asyncio.sleep(10.0)
                continue
            warned = False
            now = utcnow()
            async with database.session_factory() as session:
                order_ids = (
                    await session.execute(
                        select(BillingInvoice.order_id)
                        .where(
                            BillingInvoice.status == "pending",
                            or_(BillingInvoice.lease_until.is_(None), BillingInvoice.lease_until < now),
                        )
                        .order_by(BillingInvoice.created_at)
                        .limit(10)
                    )
                ).scalars().all()
            for order_id in order_ids:
                status = await issue_invoice(database.session_factory, settings, order_id)
                logger.info("billing order=%s status=%s", order_id, status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("billing error type=%s", type(exc).__name__)
            await asyncio.sleep(5.0)
            continue
        await asyncio.sleep(settings.billing_poll_seconds)
