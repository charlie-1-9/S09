from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select, update

from .config import get_settings
from .db import Database, utcnow
from .models import BillingInvoice


async def run(args: argparse.Namespace) -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory() as session:
            if args.command == "status":
                query = select(BillingInvoice).order_by(BillingInvoice.created_at.desc())
                if args.order_id:
                    query = query.where(BillingInvoice.order_id == uuid.UUID(args.order_id))
                else:
                    query = query.limit(10)
                rows = (await session.execute(query)).scalars().all()
                if not rows:
                    print("Sin registros de facturacion.")
                for row in rows:
                    print(
                        f"{row.order_id} estado={row.status} intentos={row.attempts} "
                        f"factura={row.invoice_number or '-'} error={row.last_error or '-'}"
                    )
                return

            if not args.all and not args.order_id:
                raise SystemExit("Indica un order_id o usa --all.")
            query = update(BillingInvoice).where(BillingInvoice.status == "failed")
            if args.order_id:
                query = query.where(BillingInvoice.order_id == uuid.UUID(args.order_id))
            result = await session.execute(
                query.values(status="pending", lease_until=None, updated_at=utcnow())
            )
            await session.commit()
            print(f"{result.rowcount} facturacion(es) devuelta(s) a pending; el worker las reintentara.")
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.billing_tool")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="muestra el estado de facturacion")
    status.add_argument("order_id", nargs="?")
    retry = sub.add_parser("retry", help="devuelve facturas failed a pending")
    retry.add_argument("order_id", nargs="?")
    retry.add_argument("--all", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
