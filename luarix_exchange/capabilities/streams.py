"""Capacidades de streaming: velas cerradas, libros y cambios de órdenes.

ASÍNCRONAS (decisión híbrida, 2026-10-16): arbitraje las consume con
`async`; los consumidores síncronos (web-server, orchestator) las usarán a
través de un puente que las corre en un hilo y entrega por callback (Fase 4).

En esta fase solo se DEFINEN. La reconexión con espera creciente, el puente
síncrono y el respaldo por polling (construido sobre `MarketData`/`Account`,
para que cualquier exchange con REST tenga stream aunque no tenga WebSocket)
son la Fase 4.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from luarix_exchange.capabilities.base import Capability, CapabilityInterface
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.market import Candle
from luarix_exchange.models.updates import OrderUpdate

OnCandle = Callable[[Candle], Awaitable[None] | None]
OnBook = Callable[[OrderBook], Awaitable[None] | None]
OnOrderUpdate = Callable[[OrderUpdate], Awaitable[None] | None]


@dataclass(frozen=True)
class CandleSubscription:
    symbol: str
    timeframe: str
    market: str = "spot"


class CandleStream(CapabilityInterface):
    capability = Capability.CANDLE_STREAM

    @abstractmethod
    async def stream_candles(
        self, subscriptions: Sequence[CandleSubscription], on_candle: OnCandle, stop: asyncio.Event,
    ) -> None:
        """Entrega SOLO velas cerradas (`is_closed=True`) hasta que `stop` se
        active, reconectando sola si se cae la conexión. Una vela parcial
        tomada por cerrada cambia de valor entre dos lecturas: ya pasó una vez."""


class OrderBookStream(CapabilityInterface):
    capability = Capability.ORDER_BOOK_STREAM

    @abstractmethod
    async def stream_order_books(
        self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event, market: str = "spot",
    ) -> None:
        """Entrega un `OrderBook` por cada cambio de los libros de `symbols`."""


class OrderUpdateStream(CapabilityInterface):
    capability = Capability.ORDER_UPDATE_STREAM

    @abstractmethod
    async def stream_order_updates(self, on_update: OnOrderUpdate, stop: asyncio.Event, market: str = "spot") -> None:
        """Entrega un `OrderUpdate` por cada cambio de estado o llenado de las
        órdenes de la cuenta."""
