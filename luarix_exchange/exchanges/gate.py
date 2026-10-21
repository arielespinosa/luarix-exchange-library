"""Gate spot, portado de luarix-arbitrage-web-server (`engine/exchanges/gate.py`).

- Pares `BTC_USDT`; base y quote ya vienen con su nombre común.
- Las reglas dan decimales, no pasos (`amount_precision` 6 → 0.000001).
- Libro: FOTO completa de los N mejores niveles cada 100 ms (sin libro que
  mantener ni checksum), con la hora de Gate (`t`, ms) → `OrderBook.event_ms`.
- Activos (`/spot/currencies`, público): estado de depósitos/retiradas y redes
  con su contrato.
- Comisión de la cuenta: `GET /wallet/fee` por par, HMAC-SHA512.

Profundidad (`depth_levels`): 5, 10, 20, 50 o 100 (20); `update_speed_ms` 100 o 1000.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any

from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._base import SpotConnector
from luarix_exchange.exchanges._common import levels, now_ms, snapshot_book, step_from_decimals
from luarix_exchange.models.assets import AssetListing, AssetNetworkInfo
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.websocket import websocket_session

REST_BASE = "https://api.gateio.ws/api/v4"
WS_URL = "wss://api.gateio.ws/ws/v4/"
DEPTHS = (5, 10, 20, 50, 100)


def rules_from_pair(info: dict) -> SymbolRules:
    return SymbolRules(
        symbol=info["id"], base=info["base"].upper(), quote=info["quote"].upper(),
        step_size=step_from_decimals(info["amount_precision"]), tick_size=step_from_decimals(info["precision"]),
        min_qty=Decimal(str(info.get("min_base_amount") or "0")),
        min_notional=Decimal(str(info.get("min_quote_amount") or "0")),
        market=Market.SPOT, fee_on="received", raw=info,
    )


def tradable(info: dict) -> bool:
    return info.get("trade_status") == "tradable" and info.get("type", "normal") == "normal"


def listing_from_currency(info: dict) -> AssetListing | None:
    """Una moneda de /spot/currencies. Las entradas con `_` (USDT_EOS…) son redes
    antiguas de otra moneda: se ignoran."""
    code = str(info["currency"])
    if "_" in code:
        return None
    if info.get("delisted"):
        return AssetListing(code.upper(), code, False, False, str(info.get("name") or ""))
    chains = info.get("chains") or []
    if chains:
        deposit = any(not c.get("deposit_disabled") for c in chains)
        withdraw = any(not c.get("withdraw_disabled") for c in chains)
    else:
        deposit, withdraw = not info.get("deposit_disabled"), not info.get("withdraw_disabled")
    networks = tuple(
        AssetNetworkInfo(network=str(c["name"]), contract_address=str(c.get("addr") or ""),
                         deposit_enabled=not c.get("deposit_disabled"), withdraw_enabled=not c.get("withdraw_disabled"))
        for c in chains if c.get("name")
    )
    return AssetListing(code.upper(), code, deposit, withdraw, str(info.get("name") or ""), networks)


def parse_book(data: dict, received_ms: int, depth: int) -> OrderBook:
    """`result` de un mensaje `spot.order_book` (foto completa)."""
    return snapshot_book(data["s"], levels(data["bids"]), levels(data["asks"]), int(data["lastUpdateId"]), received_ms,
                         depth, int(data["t"]) if data.get("t") else None)


def sign(secret: str, method: str, path: str, query: str, body: str, timestamp: str) -> str:
    """Firma APIv4: HMAC-SHA512 de método, ruta, consulta, SHA-512 del cuerpo y hora (s)."""
    payload = "\n".join((method, path, query, hashlib.sha512(body.encode()).hexdigest(), timestamp))
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest()


def rates_from_fee(symbol: str, data: dict) -> CommissionRates:
    return CommissionRates(symbol=symbol, maker=Decimal(str(data["maker_fee"])), taker=Decimal(str(data["taker_fee"])))


class GateExchange(SpotConnector):
    name = "gate"
    label = "Gate"
    DEPTHS = DEPTHS
    DEFAULT_DEPTH = 20
    LATENCY_PREFERRED = ("BTC_USDT", "ETH_USDT")

    def __init__(self, *args: Any, update_speed_ms: int = 100, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if update_speed_ms not in (100, 1000):
            raise ValueError("gate.update_speed_ms debe ser 100 o 1000")
        self.update_speed_ms = update_speed_ms

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        return {i["id"]: rules_from_pair(i) for i in self.rest.get(f"{REST_BASE}/spot/currency_pairs") if tradable(i)}

    def list_assets(self) -> dict[str, AssetListing]:
        out = {}
        for info in self.rest.get(f"{REST_BASE}/spot/currencies"):
            listing = listing_from_currency(info)
            if listing is not None:
                out[listing.asset] = listing
        return out

    def server_time_ms(self) -> int:
        return int(self.rest.get(f"{REST_BASE}/spot/time")["server_time"])

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        mensajes = [{"time": int(time.time()), "channel": "spot.order_book", "event": "subscribe",
                     "payload": [s, str(self.depth_levels), f"{self.update_speed_ms}ms"]} for s in symbols]

        async def on_message(payload: dict) -> None:
            if payload.get("channel") != "spot.order_book" or payload.get("event") != "update":
                if payload.get("error"):
                    raise RuntimeError(f"Gate rechazó la suscripción: {payload['error']}")
                return
            await deliver(on_book, parse_book(payload["result"], now_ms(), depth))

        await websocket_session(WS_URL, on_message, stop, subscribe=mensajes, heartbeat=20,
                                max_msg_size=8 * 1024 * 1024, label="gate libros")

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        """`GET /wallet/fee` por par, con clave de solo lectura."""
        self._spot(market)
        self._require_keys("la comisión de la cuenta")
        return {s: rates_from_fee(s, self._private("GET", "/wallet/fee", f"currency_pair={s}")) for s in symbols}

    def _private(self, method: str, path: str, query: str) -> dict:
        timestamp = str(int(time.time()))
        headers = {"KEY": self.api_key, "Timestamp": timestamp, "Accept": "application/json",
                   "SIGN": sign(self.api_secret, method, f"/api/v4{path}", query, "", timestamp)}
        url = f"{REST_BASE}{path}?{query}" if query else f"{REST_BASE}{path}"
        return self.rest.request(method, url, headers=headers)
