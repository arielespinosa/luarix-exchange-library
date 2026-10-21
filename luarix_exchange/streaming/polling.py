"""Streams por POLLING sobre las capacidades REST: el respaldo para cualquier
exchange sin WebSocket (o mientras su WebSocket no esté implementado).

Implementan las MISMAS interfaces que un stream nativo (`CandleStream`,
`OrderBookStream`, `OrderUpdateStream`), así que el consumidor no distingue.
El REST es síncrono (decisión híbrida): cada consulta va en
`asyncio.to_thread` para no bloquear el bucle de eventos.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from luarix_exchange.capabilities import (
    Account,
    CandleStream,
    CandleSubscription,
    MarketData,
    OrderBookStream,
    OrderUpdateStream,
)
from luarix_exchange.capabilities.streams import OnBook, OnCandle, OnOrderUpdate
from luarix_exchange.models.book import OrderBook, to_decimal
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.orders import OrderInfo
from luarix_exchange.models.updates import OrderUpdate
from luarix_exchange.streaming._util import deliver, sleep_or_stop
from luarix_exchange.streaming.reconnect import ReconnectPolicy, run_with_reconnect

log = logging.getLogger(__name__)


def _nombre(fuente: object) -> str:
    cls = type(fuente)
    return getattr(cls, "label", "") or getattr(cls, "name", "") or cls.__name__


class PollingCandleStream(CandleStream):
    """Velas CERRADAS por polling de `get_candles`.

    La primera lectura de cada suscripción fija la marca de agua y NO emite
    historia (igual que un WebSocket, que solo manda lo que cierra a partir de
    ahora). Después emite cada vela nueva una sola vez y en orden. Pide
    `history` velas por vuelta para recuperar las que cerraron entre dos
    lecturas; si llegan `history` nuevas de golpe, avisa de un posible hueco.
    """

    def __init__(self, market_data: MarketData, poll_s: float = 10.0, history: int = 5,
                 policy: ReconnectPolicy | None = None):
        self.market_data = market_data
        self.poll_s = poll_s
        self.history = history
        self.policy = policy

    async def stream_candles(self, subscriptions: Sequence[CandleSubscription], on_candle: OnCandle,
                             stop: asyncio.Event) -> None:
        marcas: dict[CandleSubscription, datetime | None] = {}
        etiqueta = f"velas por polling de {_nombre(self.market_data)}"

        async def sesion() -> None:
            while not stop.is_set():
                for sub in subscriptions:
                    velas = await asyncio.to_thread(
                        self.market_data.get_candles, sub.symbol, sub.timeframe, self.history, sub.market)
                    cerradas = sorted((v for v in velas if v.is_closed), key=lambda v: v.open_time)
                    if sub not in marcas:
                        marcas[sub] = cerradas[-1].open_time if cerradas else None
                        continue
                    nuevas = [v for v in cerradas if marcas[sub] is None or v.open_time > marcas[sub]]
                    if len(nuevas) >= self.history:
                        log.warning("%s: %d velas nuevas de %s %s %s en una vuelta (history=%d): puede faltar alguna "
                                    "entre %s y %s", etiqueta, len(nuevas), sub.symbol, sub.timeframe, sub.market,
                                    self.history, marcas[sub], nuevas[0].open_time)
                    for vela in nuevas:
                        await deliver(on_candle, vela)
                        marcas[sub] = vela.open_time
                await sleep_or_stop(stop, self.poll_s)

        await run_with_reconnect(sesion, stop, self.policy, label=etiqueta)


class PollingOrderBookStream(OrderBookStream):
    """Libros por polling de `get_order_book`. Emite cuando cambian los
    mejores `depth` niveles de algún lado; un cambio solo en niveles más
    profundos no se emite."""

    def __init__(self, market_data: MarketData, poll_s: float = 1.0, policy: ReconnectPolicy | None = None):
        self.market_data = market_data
        self.poll_s = poll_s
        self.policy = policy

    async def stream_order_books(self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event,
                                 market: str = "spot") -> None:
        ultimos: dict[str, OrderBook] = {}
        etiqueta = f"libros por polling de {_nombre(self.market_data)}"

        async def sesion() -> None:
            while not stop.is_set():
                for symbol in symbols:
                    libro = await asyncio.to_thread(self.market_data.get_order_book, symbol, depth, market)
                    if not libro.same_top(ultimos.get(symbol), levels=depth):
                        ultimos[symbol] = libro
                        await deliver(on_book, libro)
                await sleep_or_stop(stop, self.poll_s)

        await run_with_reconnect(sesion, stop, self.policy, label=etiqueta)


class PollingOrderUpdateStream(OrderUpdateStream):
    """Cambios de las órdenes de la cuenta, por polling de `get_open_orders`
    y `get_order`.

    - La primera lectura fija el estado de partida sin emitir.
    - Una orden pendiente que aparece o cambia (estado o cantidad ejecutada)
      emite un `OrderUpdate`.
    - Una orden que DESAPARECE de las pendientes NO se da por llenada: se
      pregunta `get_order()` por su estado final (llenada y cancelada a mano
      desaparecen igual, y confundirlas ya abrió posiciones fantasma). Si esa
      consulta falla, se reintenta en la siguiente vuelta; no se inventa.
    - Una orden que se coloca y se llena ENTERA entre dos lecturas nunca se ve
      en pendientes: `track(symbol, order_id)` la sigue por `get_order()`.

    `symbols`: los pares a consultar. Hace falta cuando el exchange no deja
    listar todas las pendientes de golpe (Bitunix spot exige símbolo).
    """

    #: cada cuántos fallos seguidos de `get_order` se vuelve a registrar el aviso
    LOG_EVERY = 10

    def __init__(self, account: Account, symbols: Sequence[str] | None = None, poll_s: float = 5.0,
                 policy: ReconnectPolicy | None = None):
        self.account = account
        self.symbols = list(symbols) if symbols else None
        self.poll_s = poll_s
        self.policy = policy
        self._seguidas: dict[str, str] = {}  # order_id -> symbol, añadidas con track()
        self._lock = threading.Lock()

    def track(self, symbol: str, order_id: str) -> None:
        """Sigue una orden concreta aunque nunca aparezca en pendientes.
        Seguro desde cualquier hilo."""
        with self._lock:
            self._seguidas[str(order_id)] = symbol

    async def stream_order_updates(self, on_update: OnOrderUpdate, stop: asyncio.Event, market: str = "spot") -> None:
        conocidas: dict[str, OrderInfo] = {}
        fallos: dict[str, int] = {}
        arrancado = False
        etiqueta = f"órdenes por polling de {_nombre(self.account)}"

        async def emitir(info: OrderInfo, previa: OrderInfo | None) -> None:
            await deliver(on_update, self._to_update(info, previa, market))

        async def sesion() -> None:
            nonlocal arrancado
            while not stop.is_set():
                pendientes = await asyncio.to_thread(self._open_orders, market)
                if not arrancado:
                    conocidas.update(pendientes)
                    arrancado = True
                else:
                    for oid, info in pendientes.items():
                        previa = conocidas.get(oid)
                        if previa is None or self._changed(previa, info):
                            await emitir(info, previa)
                        conocidas[oid] = info
                with self._lock:
                    seguidas = dict(self._seguidas)
                a_resolver = {oid: info.symbol for oid, info in conocidas.items() if oid not in pendientes}
                a_resolver.update({oid: s for oid, s in seguidas.items() if oid not in pendientes})
                for oid, symbol in a_resolver.items():
                    try:
                        final = await asyncio.to_thread(self.account.get_order, symbol, oid, market)
                    except Exception as exc:  # se reintenta: "no la encuentro" no es "llenó"
                        fallos[oid] = fallos.get(oid, 0) + 1
                        if fallos[oid] % self.LOG_EVERY == 1:
                            log.warning("%s: no se pudo leer el estado final de la orden %s (%s %s), intento %d: "
                                        "%s: %s -- se reintenta en la siguiente vuelta", etiqueta, oid, symbol,
                                        market, fallos[oid], type(exc).__name__, exc)
                        continue
                    fallos.pop(oid, None)
                    previa = conocidas.get(oid)
                    if previa is None or self._changed(previa, final):
                        await emitir(final, previa)
                    if final.is_open:
                        conocidas[oid] = final
                    else:
                        conocidas.pop(oid, None)
                        with self._lock:
                            self._seguidas.pop(oid, None)
                await sleep_or_stop(stop, self.poll_s)

        await run_with_reconnect(sesion, stop, self.policy, label=etiqueta)

    def _open_orders(self, market: str) -> dict[str, OrderInfo]:
        if self.symbols:
            ordenes = [o for s in self.symbols for o in self.account.get_open_orders(market, s)]
        else:
            ordenes = self.account.get_open_orders(market, None)
        return {str(o.order_id): o for o in ordenes}

    @staticmethod
    def _changed(previa: OrderInfo, actual: OrderInfo) -> bool:
        return previa.status != actual.status or previa.filled_quantity != actual.filled_quantity

    def _to_update(self, info: OrderInfo, previa: OrderInfo | None, market: str) -> OrderUpdate:
        ejecutado = to_decimal(info.filled_quantity or 0)
        antes = to_decimal(previa.filled_quantity or 0) if previa is not None else Decimal(0)
        delta = ejecutado - antes
        return OrderUpdate(
            exchange=getattr(type(self.account), "name", "") or type(self.account).__name__,
            market=Market(info.market or market),
            symbol=info.symbol,
            order_id=str(info.order_id),
            status=info.status,
            side=info.side,
            filled_qty=ejecutado,
            event_time_ms=int(time.time() * 1000),
            client_order_id=info.client_order_id,
            last_fill_qty=delta if delta > 0 else None,
            average_price=to_decimal(info.average_price) if info.average_price else None,
            raw=info.raw,
        )
