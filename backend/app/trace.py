from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import get_optional_user
from .events import iso8601
from .models import (
    BillingInvoice,
    EventRecord,
    Notification,
    Order,
    OrderItem,
    OrderOwner,
    Outbox,
    Product,
)
from .security import ApiError, DatasetContext, dataset_context, get_session

router = APIRouter()


def _q(value: str) -> str:
    return value.replace("'", "''")


def _step(
    table: str,
    op: str,
    sql: str,
    note: str,
    done: bool,
    at: Any = None,
) -> dict[str, Any]:
    return {
        "table": table,
        "op": op,
        "sql": sql,
        "note": note,
        "done": done,
        "at": iso8601(at) if at is not None else None,
    }


@router.get("/api/v1/orders/{order_id}/trace")
async def get_order_trace(
    order_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: DatasetContext = Depends(dataset_context),
) -> JSONResponse:
    try:
        identifier = uuid.UUID(order_id)
    except (ValueError, AttributeError, TypeError):
        raise ApiError(404, "NOT_FOUND", "Order not found") from None

    order = await session.get(Order, identifier)
    if order is None or order.dataset_id != context.dataset_id:
        raise ApiError(404, "NOT_FOUND", "Order not found")

    settings = request.app.state.settings
    viewer = await get_optional_user(request, strict=False)

    items = (
        await session.execute(
            select(OrderItem).where(OrderItem.order_id == identifier).order_by(OrderItem.product_id)
        )
    ).scalars().all()
    products: dict[str, Product] = {}
    if items:
        rows = (
            await session.execute(
                select(Product).where(
                    Product.dataset_id == order.dataset_id,
                    Product.id.in_([item.product_id for item in items]),
                )
            )
        ).scalars().all()
        products = {product.id: product for product in rows}
    outbox = (
        await session.execute(select(Outbox).where(Outbox.order_id == identifier))
    ).scalars().first()
    event = await session.get(EventRecord, outbox.event_id) if outbox is not None else None
    notification = (
        await session.execute(select(Notification).where(Notification.order_id == identifier))
    ).scalars().first()
    billing = await session.get(BillingInvoice, identifier)
    owner = await session.get(OrderOwner, identifier)
    show_owner = viewer is not None and owner is not None and owner.user_id == viewer.id

    # 1. API: una sola transaccion
    api_steps: list[dict[str, Any]] = []
    if show_owner and viewer is not None:
        api_steps.append(
            _step(
                "users",
                "SELECT",
                "SELECT u.id, u.username FROM users u JOIN auth_sessions s ON s.user_id = u.id "
                "WHERE s.token_hash = sha256(:token) AND s.expires_at > now()",
                f"Sesi\u00f3n v\u00e1lida de {viewer.username}",
                True,
            )
        )
    for item in items:
        product = products.get(item.product_id)
        stock_note = f" Stock actual: {product.stock}." if product is not None else ""
        api_steps.append(
            _step(
                "products",
                "UPDATE",
                f"UPDATE products SET stock = stock - {item.quantity} "
                f"WHERE id = '{_q(item.product_id)}' AND stock >= {item.quantity} RETURNING stock",
                f"Descuenta {item.quantity} de {item.name}.{stock_note}",
                True,
            )
        )
    api_steps.append(
        _step(
            "orders",
            "INSERT",
            "INSERT INTO orders (id, status, notification_status, total_cents, created_at, snapshot) "
            f"VALUES ('{order.id}', 'confirmed', 'pending', {order.total_cents}, ..., ...)",
            "Pedido confirmado y guardado.",
            True,
            order.created_at,
        )
    )
    api_steps.append(
        _step(
            "order_items",
            "INSERT",
            "INSERT INTO order_items (order_id, product_id, name, quantity, unit_price_cents) "
            f"VALUES ('{order.id}', ...)  -- {len(items)} fila(s)",
            f"{len(items)} l\u00ednea(s) del pedido.",
            True,
            order.created_at,
        )
    )
    if outbox is not None:
        api_steps.append(
            _step(
                "outbox",
                "INSERT",
                "INSERT INTO outbox (event_id, order_id, payload, status) "
                f"VALUES ('{outbox.event_id}', '{order.id}', <CloudEvent JSON>, 'pending')",
                "Evento pe.unamba.flash.order.created.v1 guardado como pendiente.",
                True,
                outbox.created_at,
            )
        )
    if billing is not None:
        api_steps.append(
            _step(
                "billing_invoices",
                "INSERT",
                f"INSERT INTO billing_invoices (order_id, status) VALUES ('{order.id}', 'pending')",
                "Tarea de facturaci\u00f3n creada (pending).",
                True,
                billing.created_at,
            )
        )
    if show_owner and owner is not None:
        api_steps.append(
            _step(
                "order_owners",
                "INSERT",
                f"INSERT INTO order_owners (order_id, user_id) VALUES ('{order.id}', '{owner.user_id}')",
                "El pedido queda ligado al usuario.",
                True,
                owner.created_at,
            )
        )

    groups: list[dict[str, Any]] = [
        {
            "id": "api",
            "title": "1. API: una sola transacci\u00f3n en PostgreSQL",
            "actor": "Todo o nada: si algo falla, no se guarda ninguna de estas filas.",
            "steps": api_steps,
        }
    ]

    # 2. Relay del worker
    published = outbox is not None and outbox.status == "published"
    published_at = outbox.published_at if outbox is not None else None
    outbox_id = outbox.id if outbox is not None else "?"
    groups.append(
        {
            "id": "relay",
            "title": "2. Worker (relay): de la outbox a Redis Streams",
            "actor": "Proceso aparte: la API no espera a que esto ocurra.",
            "steps": [
                _step(
                    "outbox",
                    "UPDATE",
                    f"UPDATE outbox SET status = 'published', published_at = now() WHERE id = {outbox_id}",
                    "Evento marcado como publicado." if published else "Esperando al relay.",
                    published,
                    published_at,
                ),
                _step(
                    "redis",
                    "XADD",
                    f"XADD {settings.stream_name} * payload <CloudEvent JSON>",
                    "Evento publicado en el stream.",
                    published,
                    published_at,
                ),
                _step(
                    "events",
                    "INSERT",
                    "INSERT INTO events (event_id, order_id, payload, status) "
                    "VALUES (..., 'published') ON CONFLICT DO NOTHING",
                    "Registro del evento publicado.",
                    event is not None,
                    event.created_at if event is not None else None,
                ),
            ],
        }
    )

    # 3. Consumidor de notificaciones
    processed_at = event.processed_at if event is not None else None
    handled = event is not None and event.status in ("processed", "rejected")
    notification_status = order.notification_status
    groups.append(
        {
            "id": "consumer",
            "title": "3. Worker (consumidor): notificaci\u00f3n eventual",
            "actor": "Valida el evento contra el esquema y actualiza el estado.",
            "steps": [
                _step(
                    "redis",
                    "XREADGROUP",
                    f"XREADGROUP GROUP {settings.consumer_group} <consumidor> STREAMS {settings.stream_name} >",
                    "El consumidor lee y valida el evento." if handled else "Esperando al consumidor.",
                    handled,
                    processed_at,
                ),
                _step(
                    "notifications",
                    "INSERT",
                    f"INSERT INTO notifications (order_id, event_id) VALUES ('{order.id}', ...) ON CONFLICT DO NOTHING",
                    "Notificaci\u00f3n simulada registrada.",
                    notification is not None,
                    notification.created_at if notification is not None else None,
                ),
                _step(
                    "orders",
                    "UPDATE",
                    f"UPDATE orders SET notification_status = '{_q(notification_status)}' WHERE id = '{order.id}'",
                    f"notification_status: {notification_status}",
                    notification_status != "pending",
                    processed_at if notification_status != "pending" else None,
                ),
                _step(
                    "events",
                    "UPDATE",
                    "UPDATE events SET status = 'processed', processed_at = now() WHERE event_id = ...",
                    "Evento marcado como procesado." if handled else "Pendiente de procesar.",
                    handled,
                    processed_at if handled else None,
                ),
            ],
        }
    )

    # 4. Facturacion (S09 -> Supabase)
    billing_done = billing is not None and billing.status != "pending"
    if billing is not None:
        if billing.status == "issued":
            remote_note = f"Factura emitida: {billing.invoice_number}"
        elif billing.status == "failed":
            remote_note = f"La facturaci\u00f3n fall\u00f3 ({billing.last_error}); se puede reintentar."
        else:
            remote_note = "Esperando al worker de facturaci\u00f3n."
        groups.append(
            {
                "id": "billing",
                "title": "4. Worker (facturaci\u00f3n): S09 llama al servicio en Supabase",
                "actor": "Otra base de datos: el microservicio guarda la factura en su propio PostgreSQL.",
                "steps": [
                    _step(
                        "supabase.invoices",
                        "INSERT",
                        "POST /invoices (Idempotency-Key = id del pedido)  ->  "
                        "INSERT INTO invoices (issuer_id, order_id, items, total_cents, ...) en Supabase",
                        remote_note,
                        billing_done,
                        billing.updated_at if billing_done else None,
                    ),
                    _step(
                        "billing_invoices",
                        "UPDATE",
                        f"UPDATE billing_invoices SET status = '{_q(billing.status)}', "
                        f"attempts = attempts + n WHERE order_id = '{order.id}'",
                        f"Estado local: {billing.status} (intentos: {billing.attempts})",
                        billing_done,
                        billing.updated_at if billing_done else None,
                    ),
                ],
            }
        )

    complete = notification_status != "pending" and (billing is None or billing.status != "pending")
    return JSONResponse(
        content={"order_id": str(identifier), "complete": complete, "groups": groups},
        headers={"Cache-Control": "no-store"},
    )
