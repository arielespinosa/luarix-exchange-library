"""Un cambio de estado o un llenado de una orden, normalizado.

Lo emiten los streams de órdenes (`OrderUpdateStream`, Fases 4-5): por
WebSocket cuando el exchange lo ofrece (user data stream de Binance, canal
privado de órdenes de Bitunix/Kraken/Bitfinex) y por polling cuando no.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from luarix_exchange.models.brokers import Market
from luarix_exchange.models.orders import OrderStatus, Side


@dataclass(frozen=True)
class OrderUpdate:
    """Qué le pasó a una orden y cuándo.

    `filled_qty` es lo ejecutado ACUMULADO; `last_fill_qty`/`last_fill_price`
    describen solo el llenado que provocó este evento (`None` si el evento
    no fue un llenado, p. ej. una cancelación).
    """

    exchange: str
    market: Market
    symbol: str
    order_id: str
    status: OrderStatus
    side: Side
    filled_qty: Decimal
    event_time_ms: int
    client_order_id: str | None = None
    last_fill_qty: Decimal | None = None
    last_fill_price: Decimal | None = None
    fee: Decimal | None = None
    fee_asset: str | None = None
    raw: Any = field(default=None, repr=False, compare=False)

    @property
    def is_fill(self) -> bool:
        return self.last_fill_qty is not None and self.last_fill_qty > 0
