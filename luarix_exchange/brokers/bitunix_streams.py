"""WebSockets de Bitunix (Fase 5): velas, libros y órdenes de FUTUROS.

Documentación oficial: https://www.bitunix.com/api-docs/futures/websocket/prepare/WebSocket.html

- Público `wss://fapi.bitunix.com/public/`, privado `wss://fapi.bitunix.com/private/`.
- Máximo 5 mensajes/s por conexión (pings incluidos): todas las suscripciones
  van en UN mensaje. Pasarse corta la conexión y repetirlo puede bloquear la IP.
- Ping de aplicación `{"op": "ping", "ping": <segundos>}`.
- Login: `sign = sha256(sha256(nonce + timestamp + apiKey) + secretKey)`, en hex,
  con `timestamp` en SEGUNDOS.

El SPOT de Bitunix tiene otra API WebSocket (otro host) que no se implementa:
spot va por polling (`native_stream_markets` lo declara así).

⚠ El kline por WebSocket NO dice cuándo cierra una vela ni trae su hora de
apertura: manda instantáneas cada 500 ms con un `ts`. Una vela "cerrada" sacada
de la última instantánea podría perder los trades de los últimos 500 ms. Por eso
el WebSocket se usa como DISPARADOR (el periodo de `ts` cambió -> la vela
anterior cerró) y la vela exacta se lee por REST.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from luarix_exchange.capabilities import CandleStream, CandleSubscription, OrderBookStream, OrderUpdateStream
from luarix_exchange.capabilities.streams import OnBook, OnCandle, OnOrderUpdate
from luarix_exchange.models.book import OrderBook, to_decimal
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.orders import OrderInfo, Side
from luarix_exchange.models.updates import OrderUpdate
from luarix_exchange.streaming._util import deliver, iso_to_ms
from luarix_exchange.streaming.candles import ClosedCandleEmitter
from luarix_exchange.streaming.polling import PollingCandleStream, PollingOrderBookStream
from luarix_exchange.streaming.reconnect import StreamAuthError, run_with_reconnect
from luarix_exchange.streaming.websocket import websocket_session

log = logging.getLogger(__name__)

PUBLIC_WS = "wss://fapi.bitunix.com/public/"
PRIVATE_WS = "wss://fapi.bitunix.com/private/"

# Intervalo estándar -> sufijo del canal `market_kline_<x>`, y su duración en
# ms. 3d, 1w y 1M no tienen duración fija alineable con `ts`: van por polling.
KLINE_CHANNELS = {
    "1m": ("1min", 60_000), "3m": ("3min", 180_000), "5m": ("5min", 300_000),
    "15m": ("15min", 900_000), "30m": ("30min", 1_800_000), "1h": ("60min", 3_600_000),
    "2h": ("2h", 7_200_000), "4h": ("4h", 14_400_000), "6h": ("6h", 21_600_000),
    "8h": ("8h", 28_800_000), "12h": ("12h", 43_200_000), "1d": ("1day", 86_400_000),
}
DEPTH_CHANNELS = ((1, "depth_book1"), (5, "depth_book5"), (15, "depth_book15"))

# Reintentos al leer por REST la vela que el WebSocket dio por cerrada: el
# endpoint puede tardar un instante en tenerla.
REST_CANDLE_RETRIES = 4
REST_CANDLE_RETRY_S = 1.0


def _ping() -> dict:
    return {"op": "ping", "ping": int(time.time())}


def login_message(api_key: str, api_secret: str, *, nonce: str | None = None, timestamp: int | None = None) -> dict:
    """Mensaje de login del WebSocket privado, firmado."""
    nonce = nonce or uuid.uuid4().hex
    timestamp = int(time.time()) if timestamp is None else timestamp
    primera = hashlib.sha256(f"{nonce}{timestamp}{api_key}".encode()).hexdigest()
    firma = hashlib.sha256(f"{primera}{api_secret}".encode()).hexdigest()
    return {"op": "login", "args": [{"apiKey": api_key, "timestamp": timestamp, "nonce": nonce, "sign": firma}]}


def login_error(respuesta: dict) -> str | None:
    """El motivo si la respuesta al login es un rechazo, `None` si es un éxito.

    La documentación no muestra la respuesta del login: se considera rechazo
    cualquier `code` distinto de 0, `success: false` o un campo `error`/`msg`
    de error."""
    data = respuesta.get("data") if isinstance(respuesta.get("data"), dict) else {}
    codigo = respuesta.get("code", data.get("code"))
    if codigo not in (None, 0, "0"):
        return f"code={codigo} {respuesta.get('msg') or data.get('msg') or respuesta}"
    if respuesta.get("success") is False or data.get("result") is False:
        return str(respuesta)
    if respuesta.get("error"):
        return str(respuesta["error"])
    return None


async def _esperar_login(ws: Any, timeout_s: float = 15.0) -> dict:
    """La respuesta al login. Bitunix manda ANTES un saludo
    `{"op": "connect", "data": {"result": true}}` que NO es esa respuesta:
    tomarlo por ella daba por buena cualquier clave (verificado con claves
    falsas el 2026-10-18; el rechazo real es
    `{"op": "login", "data": {"msg": "...Token invalid", "result": false}}`)."""
    limite = time.monotonic() + timeout_s
    while True:
        restante = limite - time.monotonic()
        if restante <= 0:
            raise ConnectionError("bitunix: no llegó la respuesta al login")
        msg = await asyncio.wait_for(ws.receive(), restante)
        if not isinstance(msg.data, str):
            raise ConnectionError(f"bitunix: la conexión se cerró esperando el login ({msg.type})")
        data = json.loads(msg.data)
        if data.get("op") == "login":
            return data


_iso_to_ms = iso_to_ms


def order_push_to_update(data: dict, ts_ms: int | None, previo: Decimal | None) -> OrderUpdate:
    """Un push del canal `order` -> `OrderUpdate`.

    ⚠ SIN VERIFICAR contra una cuenta real: `dealAmount` se documenta como
    "Deal amount" y se toma como cantidad EJECUTADA (en base), que es lo que
    significa en REST como `tradeQty`. Si resultara ser el importe en USDT,
    `filled_qty` saldría multiplicado por el precio: es lo primero que hay que
    mirar en la prueba real (el crudo va en `.raw`)."""
    ejecutado = to_decimal(data.get("dealAmount") or 0)
    delta = ejecutado - (previo or Decimal(0))
    medio = data.get("averagePrice")
    lado = str(data.get("side") or "").upper()
    return OrderUpdate(
        exchange="bitunix",
        market=Market.FUTURES,
        symbol=data.get("symbol", ""),
        order_id=str(data.get("orderId", "")),
        status=OrderInfo._status_of(data.get("orderStatus")),
        side=Side(lado) if lado in ("BUY", "SELL") else None,
        filled_qty=ejecutado,
        event_time_ms=_iso_to_ms(data.get("mtime")) or int(ts_ms or time.time() * 1000),
        client_order_id=data.get("clientId") or None,
        last_fill_qty=delta if delta > 0 else None,
        average_price=to_decimal(medio) if medio not in (None, "", "0") else None,
        fee=to_decimal(data["fee"]) if data.get("fee") not in (None, "") else None,
        raw=data,
    )


class BitunixStreamsMixin(CandleStream, OrderBookStream, OrderUpdateStream):
    """Streams nativos de Bitunix futuros; spot por polling. Lo hereda
    `BitunixBroker` (usa su `api_key`, `api_secret` y `get_candles`)."""

    async def stream_candles(self, subscriptions: Sequence[CandleSubscription], on_candle: OnCandle,
                             stop: asyncio.Event) -> None:
        nativas = [s for s in subscriptions if Market(s.market) is Market.FUTURES and s.timeframe in KLINE_CHANNELS]
        resto = [s for s in subscriptions if s not in nativas]
        tareas = []
        if nativas:
            tareas.append(self._futures_candles(nativas, on_candle, stop))
        if resto:
            log.info("bitunix: %d suscripciones de velas van por polling (spot o intervalo sin canal)", len(resto))
            tareas.append(PollingCandleStream(self).stream_candles(resto, on_candle, stop))
        await asyncio.gather(*tareas)

    async def _futures_candles(self, subs: list[CandleSubscription], on_candle: OnCandle, stop: asyncio.Event) -> None:
        por_canal = {(s.symbol, f"market_kline_{KLINE_CHANNELS[s.timeframe][0]}"): s for s in subs}
        etiqueta = "bitunix velas futuros"
        emisor = ClosedCandleEmitter(
            lambda sub: self.get_candles(sub.symbol, sub.timeframe, 3, "futures"),
            on_candle,
            {s: KLINE_CHANNELS[s.timeframe][1] for s in subs},
            retries=REST_CANDLE_RETRIES, retry_s=REST_CANDLE_RETRY_S, label=etiqueta,
        )

        async def on_message(msg: dict) -> None:
            sub = por_canal.get((msg.get("symbol"), msg.get("ch")))
            if sub is not None and msg.get("ts"):
                await emisor.period_seen(sub, int(msg["ts"]))

        suscripcion = {"op": "subscribe", "args": [{"symbol": s, "ch": ch} for s, ch in por_canal]}

        async def sesion() -> None:
            await websocket_session(PUBLIC_WS, on_message, stop, subscribe=[suscripcion], keepalive=_ping,
                                    label=etiqueta)

        await asyncio.gather(run_with_reconnect(sesion, stop, label=etiqueta), emisor.run_boundary_timer(stop))

    async def stream_order_books(self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event,
                                 market: str = "spot") -> None:
        canal = next((ch for n, ch in DEPTH_CHANNELS if n >= depth), None)
        if Market(market) is not Market.FUTURES or canal is None:
            await PollingOrderBookStream(self).stream_order_books(symbols, depth, on_book, stop, market)
            return
        pedidos = set(symbols)
        etiqueta = "bitunix libros futuros"

        async def on_message(msg: dict) -> None:
            if msg.get("ch") != canal or msg.get("symbol") not in pedidos:
                return
            data = msg.get("data") or {}
            await deliver(on_book, OrderBook.from_levels(msg["symbol"], "futures", data.get("b") or [],
                                                         data.get("a") or [], depth=depth, raw=msg))

        suscripcion = {"op": "subscribe", "args": [{"symbol": s, "ch": canal} for s in symbols]}

        async def sesion() -> None:
            await websocket_session(PUBLIC_WS, on_message, stop, subscribe=[suscripcion], keepalive=_ping,
                                    label=etiqueta)

        await run_with_reconnect(sesion, stop, label=etiqueta)

    async def stream_order_updates(self, on_update: OnOrderUpdate, stop: asyncio.Event, market: str = "spot") -> None:
        if Market(market) is not Market.FUTURES:
            raise NotImplementedError(
                "Bitunix spot no tiene stream de órdenes en esta librería y su API exige símbolo para listar "
                "pendientes: usa PollingOrderUpdateStream(broker, symbols=[...])."
            )
        ejecutado: dict[str, Decimal] = {}
        etiqueta = "bitunix órdenes futuros"

        async def on_open(ws: Any) -> None:
            await ws.send_json(login_message(self.api_key, self.api_secret))
            respuesta = await _esperar_login(ws)
            motivo = login_error(respuesta)
            if motivo:
                raise StreamAuthError(f"{etiqueta}: Bitunix rechazó el login: {motivo}")
            log.info("%s: login aceptado (%s)", etiqueta, str(respuesta)[:200])
            await ws.send_json({"op": "subscribe", "args": [{"ch": "order"}]})

        async def on_message(msg: dict) -> None:
            if msg.get("ch") != "order" or not isinstance(msg.get("data"), dict):
                return
            data = msg["data"]
            oid = str(data.get("orderId", ""))
            update = order_push_to_update(data, msg.get("ts"), ejecutado.get(oid))
            ejecutado[oid] = update.filled_qty
            if not data.get("orderStatus") or update.status.value in ("FILLED", "CANCELED", "REJECTED"):
                ejecutado.pop(oid, None)
            await deliver(on_update, update)

        async def sesion() -> None:
            await websocket_session(PRIVATE_WS, on_message, stop, on_open=on_open, keepalive=_ping, label=etiqueta)

        await run_with_reconnect(sesion, stop, label=etiqueta)
