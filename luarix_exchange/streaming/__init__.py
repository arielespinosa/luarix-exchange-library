"""Streaming (Fase 4): reconexión común, sesión WebSocket, respaldo por
polling, puente síncrono y selección automática.

La selección aplica la regla del proyecto -- **WebSocket primero, polling de
respaldo** -- sin que el consumidor tenga que saber cuál le toca:

```python
from luarix_exchange.streaming import candle_stream, StreamRunner

stream = candle_stream(broker)     # nativo si lo tiene, si no polling sobre su REST
```

Ver `docs/plans/fase4_streaming.md`.
"""

from __future__ import annotations

from collections.abc import Sequence

from luarix_exchange.capabilities import (
    Account,
    Capability,
    CandleStream,
    CandleSubscription,
    MarketData,
    OrderBookStream,
    OrderUpdateStream,
)
from luarix_exchange.streaming.bridge import StreamHandle, StreamRunner
from luarix_exchange.streaming.polling import PollingCandleStream, PollingOrderBookStream, PollingOrderUpdateStream
from luarix_exchange.streaming.reconnect import ReconnectPolicy, run_with_reconnect
from luarix_exchange.streaming.websocket import websocket_session


def _soporta(fuente: object, capacidad: Capability, abc: type) -> bool:
    """¿Tiene la capacidad de verdad? (`isinstance` y no está en `unsupported`)."""
    if not isinstance(fuente, abc):
        return False
    capacidades = getattr(type(fuente), "capabilities", None)
    return capacidades is None or capacidad in capacidades()


def candle_stream(exchange: object, poll_s: float = 10.0) -> CandleStream:
    """El stream de velas del exchange: el nativo (WebSocket) si declara
    `CANDLE_STREAM`, si no polling sobre `MarketData`."""
    if _soporta(exchange, Capability.CANDLE_STREAM, CandleStream):
        return exchange  # type: ignore[return-value]
    if _soporta(exchange, Capability.MARKET_DATA, MarketData):
        return PollingCandleStream(exchange, poll_s=poll_s)  # type: ignore[arg-type]
    raise NotImplementedError(f"{type(exchange).__name__} no tiene stream de velas ni datos de mercado para sondear.")


def order_book_stream(exchange: object, poll_s: float = 1.0) -> OrderBookStream:
    if _soporta(exchange, Capability.ORDER_BOOK_STREAM, OrderBookStream):
        return exchange  # type: ignore[return-value]
    if _soporta(exchange, Capability.MARKET_DATA, MarketData):
        return PollingOrderBookStream(exchange, poll_s=poll_s)  # type: ignore[arg-type]
    raise NotImplementedError(f"{type(exchange).__name__} no tiene stream de libros ni datos de mercado para sondear.")


def order_update_stream(exchange: object, symbols: Sequence[str] | None = None, poll_s: float = 5.0) -> OrderUpdateStream:
    """`symbols` solo lo usa el polling (Bitunix spot no lista pendientes sin símbolo)."""
    if _soporta(exchange, Capability.ORDER_UPDATE_STREAM, OrderUpdateStream):
        return exchange  # type: ignore[return-value]
    if _soporta(exchange, Capability.ACCOUNT, Account):
        return PollingOrderUpdateStream(exchange, symbols=symbols, poll_s=poll_s)  # type: ignore[arg-type]
    raise NotImplementedError(f"{type(exchange).__name__} no tiene stream de órdenes ni cuenta para sondear.")


__all__ = [
    "CandleSubscription",
    "PollingCandleStream",
    "PollingOrderBookStream",
    "PollingOrderUpdateStream",
    "ReconnectPolicy",
    "StreamHandle",
    "StreamRunner",
    "candle_stream",
    "order_book_stream",
    "order_update_stream",
    "run_with_reconnect",
    "websocket_session",
]
