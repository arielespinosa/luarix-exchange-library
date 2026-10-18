"""MEXC spot, portado de luarix-arbitrage-web-server (`engine/exchanges/mexc.py`).

- Pares `BTCUSDT`; base y quote ya vienen con su nombre común.
- Paso de cantidad `baseSizePrecision`; de precio, `quotePrecision` (decimales);
  importe mínimo, `quoteAmountPrecision`.
- El WebSocket público solo habla **protobuf**. Se decodifica con un lector
  mínimo (`_common.protobuf_fields`), sin depender de `protobuf`. Mensaje: canal
  (1), par (3), hora de envío (6, ms) y libro (303): `asks` (1) y `bids` (2), cada
  nivel con precio (1) y cantidad (2) como texto.
- 30 suscripciones por conexión: con más pares se abren varias a la vez; si cae
  una, se reabren todas.
- Libro: FOTO de 5, 10 o 20 niveles; hora de envío → `OrderBook.event_ms`.
- Comisión de la cuenta: `GET /api/v3/tradeFee` por par, firmada como en Binance.

Profundidad (`depth_levels`): 5, 10 o 20 (20).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
from decimal import Decimal
from typing import Any

from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._base import SpotConnector
from luarix_exchange.exchanges._common import loads, now_ms, protobuf_fields, snapshot_book, step_from_decimals
from luarix_exchange.models.book import Level, OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver, transient_errors
from luarix_exchange.streaming.websocket import websocket_session

REST_BASE = "https://api.mexc.com"
WS_URL = "wss://wbs-api.mexc.com/ws"
DEPTHS = (5, 10, 20)
SUBSCRIPTIONS_PER_CONNECTION = 30
PING_S = 20
F_CHANNEL, F_SYMBOL, F_SEND_TIME, F_LIMIT_DEPTHS = 1, 3, 6, 303
F_ASKS, F_BIDS, F_VERSION = 1, 2, 4
F_PRICE, F_QTY = 1, 2


def rules_from_symbol(info: dict) -> SymbolRules:
    paso = Decimal(str(info.get("baseSizePrecision") or "0"))
    if paso == 0:
        paso = step_from_decimals(info.get("baseAssetPrecision", 8))
    return SymbolRules(
        symbol=info["symbol"], base=info["baseAsset"].upper(), quote=info["quoteAsset"].upper(),
        step_size=paso, tick_size=step_from_decimals(info["quotePrecision"]), min_qty=paso,
        min_notional=Decimal(str(info.get("quoteAmountPrecision") or "0")),
        market=Market.SPOT, fee_on="received", raw=info,
    )


def tradable(info: dict) -> bool:
    return str(info.get("status")) == "1" and bool(info.get("isSpotTradingAllowed"))


def book_channel(symbol: str, depth: int) -> str:
    return f"spot@public.limit.depth.v3.api.pb@{symbol}@{depth}"


def _levels(items: list[bytes]) -> list[Level]:
    out: list[Level] = []
    for raw in items:
        item = protobuf_fields(raw)
        out.append((Decimal(item[F_PRICE][0].decode()), Decimal(item[F_QTY][0].decode())))
    return out


def parse_book(raw: bytes, received_ms: int, depth: int) -> tuple[OrderBook, int] | None:
    """(libro, hora de envío de MEXC) de un mensaje binario; None si no es un libro."""
    wrapper = protobuf_fields(raw)
    if F_LIMIT_DEPTHS not in wrapper or F_SYMBOL not in wrapper:
        return None
    body = protobuf_fields(wrapper[F_LIMIT_DEPTHS][0])
    version = int(body[F_VERSION][0].decode()) if F_VERSION in body else 0
    enviado = int(wrapper.get(F_SEND_TIME, [0])[0])
    book = snapshot_book(wrapper[F_SYMBOL][0].decode(), _levels(body.get(F_BIDS, [])), _levels(body.get(F_ASKS, [])),
                         version, received_ms, depth, enviado or None)
    return book, enviado


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def rates_from_fee(symbol: str, data: dict) -> CommissionRates:
    fees = data.get("data", data)
    return CommissionRates(symbol=symbol, maker=Decimal(str(fees["makerCommission"])),
                           taker=Decimal(str(fees["takerCommission"])))


def _decode(raw: bytes | str) -> Any:
    """Binario (protobuf) tal cual; texto (respuestas a la suscripción) como JSON."""
    return raw if isinstance(raw, bytes) else loads(raw)


class MexcExchange(SpotConnector):
    name = "mexc"
    label = "MEXC"
    DEPTHS = DEPTHS
    DEFAULT_DEPTH = 20
    LATENCY_PREFERRED = ("BTCUSDT", "ETHUSDT")

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        data = self.rest.get(f"{REST_BASE}/api/v3/exchangeInfo")
        return {i["symbol"]: rules_from_symbol(i) for i in data["symbols"] if tradable(i)}

    def server_time_ms(self) -> int:
        return int(self.rest.get(f"{REST_BASE}/api/v3/time")["serverTime"])

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        trozos = [symbols[i:i + SUBSCRIPTIONS_PER_CONNECTION] for i in range(0, len(symbols), SUBSCRIPTIONS_PER_CONNECTION)]
        tareas = [asyncio.ensure_future(self._connection(t, on_book, stop, depth)) for t in trozos]
        try:
            # La primera que termine (cerrada o con error) hace reabrirlas todas.
            hechas, _ = await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
            for tarea in hechas:
                tarea.result()
        finally:
            for tarea in tareas:
                tarea.cancel()
            for tarea in tareas:
                with contextlib.suppress(asyncio.CancelledError, *transient_errors()):
                    await tarea

    async def _connection(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        suscripcion = {"method": "SUBSCRIPTION", "params": [book_channel(s, self.depth_levels) for s in symbols]}

        async def on_message(message: Any) -> None:
            if isinstance(message, dict):
                if message.get("code") not in (0, None) or "Blocked" in str(message.get("msg", "")):
                    raise RuntimeError(f"MEXC rechazó la suscripción: {message.get('msg', message)}")
                return
            parsed = parse_book(message, now_ms(), depth)
            if parsed is not None:
                await deliver(on_book, parsed[0])

        await websocket_session(WS_URL, on_message, stop, subscribe=[suscripcion], keepalive=lambda: {"method": "PING"},
                                keepalive_s=PING_S, loads=_decode, heartbeat=None, max_msg_size=8 * 1024 * 1024,
                                label="mexc libros")

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        self._spot(market)
        self._require_keys("la comisión de la cuenta")
        out: dict[str, CommissionRates] = {}
        for s in symbols:
            query = f"symbol={s}&recvWindow=5000&timestamp={now_ms()}"
            data = self.rest.get(f"{REST_BASE}/api/v3/tradeFee?{query}&signature={sign(self.api_secret, query)}",
                                 headers={"X-MEXC-APIKEY": self.api_key})
            out[s] = rates_from_fee(s, data)
        return out

