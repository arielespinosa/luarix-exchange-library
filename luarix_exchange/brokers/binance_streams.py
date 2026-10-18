"""WebSockets de Binance (Fase 5): velas, libros y órdenes, en spot y USDT-M.

Rutas verificadas contra los servidores reales el 2026-10-18 (ver
`docs/plans/bugs.md`). Binance futuros reparte sus streams por ruta y una ruta
equivocada NO da error: se queda muda.

| qué                  | spot                                   | USDT-M futuros                                |
|----------------------|----------------------------------------|-----------------------------------------------|
| velas (`kline`)      | `stream.binance.com:9443/stream`       | `fstream.binance.com/market/stream`           |
| libro (`depthN`)     | `stream.binance.com:9443/stream`       | `fstream.binance.com/public/stream`           |
| órdenes (privado)    | WebSocket API: `userDataStream.subscribe.signature` | `fstream.binance.com/private/ws/<listenKey>` |

Spot ya no tiene `listenKey`: Binance retiró esos endpoints el 2026-02-20; el
flujo de usuario va por la WebSocket API firmando la suscripción (HMAC).
Futuros sigue con `listenKey` (válido 60 min; se renueva cada 30).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from luarix_exchange.capabilities import CandleStream, CandleSubscription, OrderBookStream, OrderUpdateStream
from luarix_exchange.capabilities.streams import OnBook, OnCandle, OnOrderUpdate
from luarix_exchange.models.book import OrderBook, to_decimal
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.market import Candle
from luarix_exchange.models.orders import OrderInfo, Side
from luarix_exchange.models.updates import OrderUpdate
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.polling import PollingOrderBookStream
from luarix_exchange.streaming.reconnect import StreamAuthError, run_with_reconnect
from luarix_exchange.streaming.websocket import websocket_session

log = logging.getLogger(__name__)

SPOT_STREAM = {"main": "wss://stream.binance.com:9443/stream", "test": "wss://stream.testnet.binance.vision/stream"}
# ⚠ Las URLs de TESTNET de futuros no están verificadas (no hay claves de testnet de futuros).
FUT_MARKET = {"main": "wss://fstream.binance.com/market/stream", "test": "wss://fstream.binancefuture.com/stream"}
FUT_PUBLIC = {"main": "wss://fstream.binance.com/public/stream", "test": "wss://fstream.binancefuture.com/stream"}
FUT_PRIVATE = {"main": "wss://fstream.binance.com/private/ws/", "test": "wss://fstream.binancefuture.com/ws/"}
WS_API = {"main": "wss://ws-api.binance.com:443/ws-api/v3", "test": "wss://ws-api.testnet.binance.vision:443/ws-api/v3"}

# Niveles que admite el stream de libro parcial; más profundo va por polling.
DEPTH_LEVELS = (5, 10, 20)
LISTEN_KEY_KEEPALIVE_S = 30 * 60


# ── Traducción de mensajes (funciones puras, testeables sin red) ──────────

def kline_to_candle(data: dict) -> Candle | None:
    """La vela de un mensaje `kline`, SOLO si está cerrada (`k.x`)."""
    if data.get("e") != "kline" or not data.get("k", {}).get("x"):
        return None
    return Candle.from_binance(data)


def depth_to_book(data: dict, symbol: str, market: str, depth: int) -> OrderBook:
    """Libro parcial. Spot manda `{lastUpdateId, bids, asks}` (sin símbolo);
    futuros `{e: depthUpdate, s, u, b, a}`."""
    return OrderBook.from_levels(
        symbol, market,
        data.get("bids") or data.get("b") or [],
        data.get("asks") or data.get("a") or [],
        depth=depth,
        update_id=data.get("lastUpdateId", data.get("u")),
        raw=data,
    )


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return to_decimal(value)


def _order_update(exchange: str, market: str, o: dict, event_time: int, average: Decimal | None) -> OrderUpdate:
    llenado = _dec(o.get("l")) or Decimal(0)
    es_llenado = o.get("x") == "TRADE" and llenado > 0
    return OrderUpdate(
        exchange=exchange,
        market=Market(market),
        symbol=o["s"],
        order_id=str(o["i"]),
        status=OrderInfo._status_of(o.get("X")),
        side=Side(o["S"]) if o.get("S") in ("BUY", "SELL") else None,
        filled_qty=_dec(o.get("z")) or Decimal(0),
        event_time_ms=int(event_time or time.time() * 1000),
        # En una cancelación de spot, `c` es el id de la PETICIÓN de cancelar y
        # el de la orden original va en `C` (verificado en testnet 2026-10-18).
        client_order_id=o.get("C") or o.get("c") or None,
        last_fill_qty=llenado if es_llenado else None,
        last_fill_price=_dec(o.get("L")) if es_llenado else None,
        average_price=average,
        fee=_dec(o.get("n")) if es_llenado else None,
        fee_asset=o.get("N") if es_llenado else None,
        raw=o,
    )


def execution_report_to_update(event: dict) -> OrderUpdate | None:
    """Spot: evento `executionReport`. El precio medio no viene: se calcula
    con el importe acumulado (`Z`) entre la cantidad acumulada (`z`)."""
    if event.get("e") != "executionReport":
        return None
    z, cuota = _dec(event.get("z")), _dec(event.get("Z"))
    medio = cuota / z if z and cuota is not None else None
    return _order_update("binance", "spot", event, event.get("E"), medio)


def order_trade_update_to_update(event: dict) -> OrderUpdate | None:
    """Futuros: evento `ORDER_TRADE_UPDATE` (la orden va en `o`, con `ap`
    como precio medio)."""
    if event.get("e") != "ORDER_TRADE_UPDATE":
        return None
    o = event["o"]
    medio = _dec(o.get("ap"))
    return _order_update("binance", "futures", o, event.get("E"), medio if medio else None)


def sign_ws_api(params: dict, secret: str) -> str:
    """Firma HMAC-SHA256 de la WebSocket API de spot: todos los parámetros
    (salvo `signature`) ordenados alfabéticamente como `k=v&k=v`."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "signature")
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ── Las capacidades ──────────────────────────────────────────────────────

class BinanceStreamsMixin(CandleStream, OrderBookStream, OrderUpdateStream):
    """Streams nativos de Binance. Lo hereda `BinanceBroker`; usa su
    `testnet`, `api_key`, `api_secret` y `client` (python-binance)."""

    def _net(self) -> str:
        return "test" if getattr(self, "testnet", False) else "main"

    async def stream_candles(self, subscriptions: Sequence[CandleSubscription], on_candle: OnCandle,
                             stop: asyncio.Event) -> None:
        por_mercado: dict[Market, list[CandleSubscription]] = defaultdict(list)
        for sub in subscriptions:
            por_mercado[Market(sub.market)].append(sub)

        async def un_mercado(mercado: Market, subs: list[CandleSubscription]) -> None:
            base = (FUT_MARKET if mercado is Market.FUTURES else SPOT_STREAM)[self._net()]
            streams = "/".join(f"{s.symbol.lower()}@kline_{s.timeframe}" for s in subs)

            async def on_message(msg: dict) -> None:
                vela = kline_to_candle(msg.get("data", msg))
                if vela is not None:
                    await deliver(on_candle, vela)

            async def sesion() -> None:
                await websocket_session(f"{base}?streams={streams}", on_message, stop,
                                        label=f"binance velas {mercado.value}")

            await run_with_reconnect(sesion, stop, label=f"binance velas {mercado.value}")

        await asyncio.gather(*(un_mercado(m, s) for m, s in por_mercado.items()))

    async def stream_order_books(self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event,
                                 market: str = "spot") -> None:
        if depth > DEPTH_LEVELS[-1]:
            log.info("binance: libro de %d niveles > %d del stream parcial; va por polling", depth, DEPTH_LEVELS[-1])
            await PollingOrderBookStream(self).stream_order_books(symbols, depth, on_book, stop, market)
            return
        nivel = next(n for n in DEPTH_LEVELS if n >= depth)
        mercado = Market(market)
        base = (FUT_PUBLIC if mercado is Market.FUTURES else SPOT_STREAM)[self._net()]
        por_nombre = {s.lower(): s for s in symbols}
        streams = "/".join(f"{s.lower()}@depth{nivel}@100ms" for s in symbols)

        async def on_message(msg: dict) -> None:
            simbolo = por_nombre.get(str(msg.get("stream", "")).split("@")[0])
            if simbolo is None:
                return
            await deliver(on_book, depth_to_book(msg["data"], simbolo, mercado.value, depth))

        async def sesion() -> None:
            await websocket_session(f"{base}?streams={streams}", on_message, stop,
                                    label=f"binance libros {mercado.value}")

        await run_with_reconnect(sesion, stop, label=f"binance libros {mercado.value}")

    async def stream_order_updates(self, on_update: OnOrderUpdate, stop: asyncio.Event, market: str = "spot") -> None:
        if Market(market) is Market.FUTURES:
            await self._futures_order_updates(on_update, stop)
        else:
            await self._spot_order_updates(on_update, stop)

    async def _spot_order_updates(self, on_update: OnOrderUpdate, stop: asyncio.Event) -> None:
        etiqueta = "binance órdenes spot"

        async def on_open(ws: Any) -> None:
            # Firma NUEVA en cada conexión, con la hora de BINANCE (el reloj
            # local puede ir adelantado y Binance rechaza con -1021).
            await asyncio.to_thread(self.sync_clock)
            desfase = getattr(self.client, "timestamp_offset", 0) or 0
            params = {"apiKey": self.api_key, "recvWindow": 10000, "timestamp": int(time.time() * 1000 + desfase)}
            params["signature"] = sign_ws_api(params, self.api_secret)
            await ws.send_json({"id": str(uuid.uuid4()), "method": "userDataStream.subscribe.signature",
                                "params": params})
            respuesta = json.loads((await asyncio.wait_for(ws.receive(), 15)).data)
            if respuesta.get("status") != 200:
                raise StreamAuthError(f"{etiqueta}: Binance rechazó la suscripción: {respuesta.get('error') or respuesta}")
            log.info("%s: suscrito (subscriptionId=%s)", etiqueta, respuesta.get("result", {}).get("subscriptionId"))

        async def on_message(msg: dict) -> None:
            # Los eventos llegan envueltos: {"subscriptionId": 0, "event": {...}}.
            update = execution_report_to_update(msg.get("event", msg))
            if update is not None:
                await deliver(on_update, update)

        async def sesion() -> None:
            await websocket_session(WS_API[self._net()], on_message, stop, on_open=on_open, label=etiqueta)

        await run_with_reconnect(sesion, stop, label=etiqueta)

    async def _futures_order_updates(self, on_update: OnOrderUpdate, stop: asyncio.Event) -> None:
        etiqueta = "binance órdenes futuros"

        async def renovar(clave: str) -> None:
            while True:
                await asyncio.sleep(LISTEN_KEY_KEEPALIVE_S)
                await asyncio.to_thread(self.client.futures_stream_keepalive, clave)

        async def on_message(msg: dict) -> None:
            if msg.get("e") == "listenKeyExpired":
                raise ConnectionError(f"{etiqueta}: el listenKey caducó; se pide otro")
            update = order_trade_update_to_update(msg)
            if update is not None:
                await deliver(on_update, update)

        async def sesion() -> None:
            clave = await asyncio.to_thread(self.client.futures_stream_get_listen_key)
            renovador = asyncio.ensure_future(renovar(clave))
            try:
                await websocket_session(FUT_PRIVATE[self._net()] + clave, on_message, stop, label=etiqueta)
            finally:
                renovador.cancel()

        await run_with_reconnect(sesion, stop, label=etiqueta)
