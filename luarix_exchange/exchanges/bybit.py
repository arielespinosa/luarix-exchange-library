"""Bybit spot, portado de luarix-arbitrage-web-server (`engine/exchanges/bybit.py`).

- Pares `BTCUSDT`; base y quote ya vienen con su nombre común.
- Libro: FOTO y después DELTAS por nivel (cantidad 0 = quitar), mantenido con
  `LevelBook`. Una foto nueva (`type=snapshot`, o `u == 1` tras un reinicio del
  servicio de Bybit) sustituye al libro. Sin checksum en spot: un delta de un par
  sin foto reabre la conexión.
- Bybit cierra la conexión sin un `{"op": "ping"}` propio cada 20 s.
- Cada mensaje trae la hora de Bybit (`ts`, ms) → `OrderBook.event_ms`.
- Comisión en el activo que se recibe (`fee_on="received"`); la de la cuenta,
  `GET /v5/account/fee-rate` (todos los pares), HMAC-SHA256.
- Testnet de spot (`api-testnet.bybit.com`): mide la ida y vuelta de una orden.

Profundidad (`depth_levels`): 1, 50 o 200 (50 por defecto).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from luarix_exchange.capabilities import OrderRoundtripProbe
from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._base import SpotConnector
from luarix_exchange.exchanges._common import LevelBook, levels, now_ms
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.websocket import websocket_session

log = logging.getLogger(__name__)

REST_BASE = "https://api.bybit.com"
TESTNET_REST_BASE = "https://api-testnet.bybit.com"
WS_URL = "wss://stream.bybit.com/v5/public/spot"
DEPTHS = (1, 50, 200)
SUBSCRIBE_BATCH = 10  # temas por mensaje de suscripción (límite de Bybit en spot)
PING_S = 20
RECV_WINDOW = "5000"


class BookGap(ConnectionError):
    """Llegó un cambio de un libro sin su foto: se reabre la conexión."""


# --- parseo puro (sin red) -----------------------------------------------------

def rules_from_instrument(info: dict) -> SymbolRules:
    lot, price = info["lotSizeFilter"], info["priceFilter"]
    return SymbolRules(
        symbol=info["symbol"], base=info["baseCoin"].upper(), quote=info["quoteCoin"].upper(),
        step_size=Decimal(str(lot["basePrecision"])), tick_size=Decimal(str(price["tickSize"])),
        min_qty=Decimal(str(lot["minOrderQty"])), min_notional=Decimal(str(lot.get("minOrderAmt") or "0")),
        market=Market.SPOT, fee_on="received", raw=info,
    )


def tradable(info: dict) -> bool:
    return info.get("status") == "Trading"


def book_topic(symbol: str, depth: int) -> str:
    return f"orderbook.{depth}.{symbol}"


def apply_message(books: dict[str, LevelBook], message: dict) -> LevelBook:
    """Aplica un mensaje `orderbook.*` al libro de su par y lo devuelve."""
    data = message["data"]
    symbol = data["s"]
    bids, asks, update_id = levels(data.get("b", [])), levels(data.get("a", [])), int(data["u"])
    if message.get("type") == "snapshot" or update_id == 1:
        book = books.setdefault(symbol, LevelBook(symbol))
        book.reset(bids, asks, update_id)
        return book
    existing = books.get(symbol)
    if existing is None:
        raise BookGap(f"Bybit: cambio de {symbol} sin foto previa")
    existing.apply(bids, asks, update_id)
    return existing


def sign(secret: str, timestamp: str, api_key: str, payload: str) -> str:
    """Firma v5: HMAC-SHA256 (hex) de hora + clave + ventana + (consulta o cuerpo)."""
    return hmac.new(secret.encode(), f"{timestamp}{api_key}{RECV_WINDOW}{payload}".encode(), hashlib.sha256).hexdigest()


def auth_headers(api_key: str, api_secret: str, payload: str, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(now_ms())
    return {"X-BAPI-API-KEY": api_key, "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign(api_secret, timestamp, api_key, payload)}


def rates_from_fee(row: dict) -> CommissionRates:
    return CommissionRates(symbol=row["symbol"], maker=Decimal(str(row["makerFeeRate"])),
                           taker=Decimal(str(row["takerFeeRate"])))


def far_limit_order(price: Decimal, rules: SymbolRules, max_deviation: Decimal) -> tuple[Decimal, Decimal]:
    """(precio, cantidad) de una compra límite que no se ejecuta: por debajo del
    precio, dentro de la desviación que Bybit admite, con el importe mínimo."""
    limit = (price * (1 - max_deviation * Decimal("0.8")) / rules.tick_size).to_integral_value(ROUND_DOWN) * rules.tick_size
    qty = ((rules.min_notional * Decimal("1.2") / limit) / rules.step_size).to_integral_value(ROUND_UP) * rules.step_size
    return limit, max(qty, rules.min_qty)


# --- el exchange ---------------------------------------------------------------

class BybitExchange(SpotConnector, OrderRoundtripProbe):
    name = "bybit"
    label = "Bybit"
    DEPTHS = DEPTHS
    DEFAULT_DEPTH = 50
    LATENCY_PREFERRED = ("BTCUSDT", "ETHUSDT")

    def _instruments(self, base: str = REST_BASE, symbol: str | None = None) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        while True:
            params = {"category": "spot", "limit": "1000"}
            if symbol:
                params["symbol"] = symbol
            if cursor:
                params["cursor"] = cursor
            data = self.rest.get(f"{base}/v5/market/instruments-info", params=params)
            out += data["result"]["list"]
            cursor = data["result"].get("nextPageCursor") or ""
            if not cursor:
                return out

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        return {i["symbol"]: rules_from_instrument(i) for i in self._instruments() if tradable(i)}

    def server_time_ms(self) -> int:
        return int(self.rest.get(f"{REST_BASE}/v5/market/time")["time"])

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        books: dict[str, LevelBook] = {}
        lotes = [{"op": "subscribe", "args": [book_topic(s, self.depth_levels) for s in symbols[i:i + SUBSCRIBE_BATCH]]}
                 for i in range(0, len(symbols), SUBSCRIBE_BATCH)]

        async def on_message(message: dict) -> None:
            if message.get("op") == "subscribe" and not message.get("success", True):
                raise RuntimeError(f"Bybit rechazó la suscripción: {message.get('ret_msg', message)}")
            if not str(message.get("topic", "")).startswith("orderbook."):
                return
            book = apply_message(books, message)
            ts = int(message["ts"]) if message.get("ts") else None
            await deliver(on_book, book.snapshot(now_ms(), depth, ts))

        await websocket_session(WS_URL, on_message, stop, subscribe=lotes, keepalive=lambda: {"op": "ping"},
                                keepalive_s=PING_S, heartbeat=None, max_msg_size=8 * 1024 * 1024, label="bybit libros")

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        """Todos los pares spot de una vez; se devuelven los pedidos."""
        self._spot(market)
        self._require_keys("la comisión de la cuenta")
        query = "category=spot"
        data = self.rest.get(f"{REST_BASE}/v5/account/fee-rate?{query}",
                             headers=auth_headers(self.api_key, self.api_secret, query))
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit al consultar comisiones: {data.get('retMsg', data)}")
        wanted = set(symbols)
        return {r["symbol"]: rates_from_fee(r) for r in data["result"]["list"] if r["symbol"] in wanted}

    def measure_order_roundtrip_ms(self, credentials: Any, samples: int = 8, symbol: str = "BTCUSDT") -> list[int]:
        """En la TESTNET: coloca `samples` compras límite que no se ejecutan y las
        cancela, midiendo cada ida y vuelta. `credentials`: claves de testnet."""
        info = self._instruments(TESTNET_REST_BASE, symbol)[0]
        rules = rules_from_instrument(info)
        ticker = self.rest.get(f"{TESTNET_REST_BASE}/v5/market/tickers", params={"category": "spot", "symbol": symbol})
        price = Decimal(str(ticker["result"]["list"][0]["lastPrice"]))
        desviacion = Decimal(str((info.get("riskParameters") or {}).get("priceLimitRatioY") or "0.05"))
        limit, qty = far_limit_order(price, rules, desviacion)

        def post(path: str, body: dict) -> dict:
            text = json.dumps(body, separators=(",", ":"))
            headers = {**auth_headers(credentials.api_key, credentials.api_secret, text), "Content-Type": "application/json"}
            data = self.rest.post(f"{TESTNET_REST_BASE}{path}", data=text, headers=headers)
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit testnet: {data.get('retMsg', data)}")
            return data["result"]

        self.rest.get(f"{TESTNET_REST_BASE}/v5/market/time")  # abre la conexión: la 1ª medida no paga el TLS
        tiempos: list[int] = []
        for _ in range(samples):
            orden = {"category": "spot", "symbol": symbol, "side": "Buy", "orderType": "Limit", "qty": str(qty),
                     "price": str(limit)}
            inicio = now_ms()
            colocada = post("/v5/order/create", orden)
            tiempos.append(now_ms() - inicio)
            post("/v5/order/cancel", {"category": "spot", "symbol": symbol, "orderId": colocada["orderId"]})
            time.sleep(0.2)
        log.info("Bybit testnet: %d órdenes de prueba, ida y vuelta %s ms", samples, tiempos)
        return tiempos
