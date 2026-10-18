"""HTX (antes Huobi) spot, portado de luarix-arbitrage-web-server (`engine/exchanges/htx.py`).

- Pares y activos en minúsculas (`btcusdt`, `btc`): base/quote se pasan a
  mayúsculas; el símbolo se queda como lo usa HTX.
- Reglas en decimales (`ap` 6 → 0.000001) y mínimos en base (`minoa`) y en quote
  (`minov`).
- Precios y cantidades llegan como NÚMEROS JSON: se leen como `Decimal`.
- Cada mensaje del WebSocket va comprimido con gzip, y HTX manda su propio
  `{"ping": …}` que hay que contestar con `{"pong": …}` o corta la conexión.
- Libro: FOTO de 5, 10 o 20 niveles cada 100 ms, con la hora de HTX (`ts`).
- Activos (público): estado y redes con comisión y mínimo de retirada.
- Comisión de la cuenta: `GET /v2/reference/transact-fee-rate` (hasta 10 pares),
  firma v2 (HMAC-SHA256 en base64).

Profundidad (`depth_levels`): 5, 10 o 20 (20).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._base import SpotConnector
from luarix_exchange.exchanges._common import levels, loads, now_ms, snapshot_book, step_from_decimals
from luarix_exchange.models.assets import AssetListing, AssetNetworkInfo
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.websocket import websocket_session

HOST = "api.huobi.pro"
REST_BASE = f"https://{HOST}"
WS_URL = f"wss://{HOST}/ws"
DEPTHS = (5, 10, 20)
FEES_BATCH = 10


def rules_from_symbol(info: dict) -> SymbolRules:
    return SymbolRules(
        symbol=info["symbol"], base=info["bc"].upper(), quote=info["qc"].upper(),
        step_size=step_from_decimals(info["ap"]), tick_size=step_from_decimals(info["pp"]),
        min_qty=Decimal(str(info.get("minoa") or "0")), min_notional=Decimal(str(info.get("minov") or "0")),
        market=Market.SPOT, fee_on="received", raw=info,
    )


def tradable(info: dict) -> bool:
    return info.get("state") == "online" and info.get("at", "enabled") == "enabled"


def _decimal(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def listing_from_currency(info: dict) -> AssetListing:
    code = str(info["currency"])
    chains = info.get("chains") or []
    deposit = any(c.get("depositStatus") == "allowed" for c in chains) if chains else None
    withdraw = any(c.get("withdrawStatus") == "allowed" for c in chains) if chains else None
    if info.get("instStatus") == "delisted":
        deposit = withdraw = False
    networks = tuple(
        AssetNetworkInfo(network=str(c.get("displayName") or c["chain"]), contract_address=str(c.get("contractAddress") or ""),
                         deposit_enabled=c.get("depositStatus") == "allowed",
                         withdraw_enabled=c.get("withdrawStatus") == "allowed",
                         withdraw_fee=_decimal(c.get("transactFeeWithdraw")), min_withdraw=_decimal(c.get("minWithdrawAmt")))
        for c in chains if c.get("chain")
    )
    return AssetListing(code.upper(), code, deposit, withdraw, networks=networks)


def book_channel(symbol: str, depth: int) -> str:
    return f"market.{symbol}.mbp.refresh.{depth}"


def decode(raw: bytes | str) -> dict:
    return loads(gzip.decompress(raw) if isinstance(raw, bytes) else raw)


def parse_book(message: dict, received_ms: int, depth: int) -> OrderBook:
    """Mensaje `market.<par>.mbp.refresh.<N>` (foto completa)."""
    symbol = message["ch"].split(".")[1]
    tick = message["tick"]
    return snapshot_book(symbol, levels(tick["bids"]), levels(tick["asks"]), int(tick["seqNum"]), received_ms, depth,
                         int(message["ts"]) if message.get("ts") else None)


def signed_query(api_key: str, api_secret: str, method: str, path: str, params: dict[str, str],
                 now: datetime | None = None) -> str:
    """Consulta firmada (v2): parámetros de la clave + los propios, en orden, y la
    firma HMAC-SHA256 en base64 de método, host, ruta y consulta."""
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S")
    query_params = {"AccessKeyId": api_key, "SignatureMethod": "HmacSHA256", "SignatureVersion": "2",
                    "Timestamp": stamp, **params}
    query = urllib.parse.urlencode(sorted(query_params.items()))
    payload = "\n".join((method, HOST, path, query))
    firma = base64.b64encode(hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).digest()).decode()
    return f"{query}&{urllib.parse.urlencode({'Signature': firma})}"


def rates_from_fee(row: dict) -> CommissionRates:
    maker = row.get("actualMakerRate", row["makerFeeRate"])
    taker = row.get("actualTakerRate", row["takerFeeRate"])
    return CommissionRates(symbol=row["symbol"], maker=Decimal(str(maker)), taker=Decimal(str(taker)))


class HtxExchange(SpotConnector):
    name = "htx"
    label = "HTX"
    DEPTHS = DEPTHS
    DEFAULT_DEPTH = 20
    LATENCY_PREFERRED = ("btcusdt", "ethusdt")

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        data = self.rest.get(f"{REST_BASE}/v1/settings/common/market-symbols")
        return {i["symbol"]: rules_from_symbol(i) for i in data["data"] if tradable(i)}

    def list_assets(self) -> dict[str, AssetListing]:
        data = self.rest.get(f"{REST_BASE}/v2/reference/currencies")
        return {listing.asset: listing for listing in map(listing_from_currency, data["data"])}

    def server_time_ms(self) -> int:
        return int(self.rest.get(f"{REST_BASE}/v1/common/timestamp")["data"])

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        socket: dict[str, Any] = {}
        mensajes = [{"sub": book_channel(s, self.depth_levels), "id": s} for s in symbols]

        async def on_open(ws: Any) -> None:
            socket["ws"] = ws

        async def on_message(message: dict) -> None:
            if "ping" in message:
                await socket["ws"].send_str(json.dumps({"pong": int(message["ping"])}))
                return
            if message.get("status") == "error":
                raise RuntimeError(f"HTX rechazó la suscripción: {message.get('err-msg', message)}")
            if "tick" in message:
                await deliver(on_book, parse_book(message, now_ms(), depth))

        await websocket_session(WS_URL, on_message, stop, subscribe=mensajes, on_open=on_open, loads=decode,
                                heartbeat=None, max_msg_size=8 * 1024 * 1024, label="htx libros")

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        self._spot(market)
        self._require_keys("la comisión de la cuenta")
        path = "/v2/reference/transact-fee-rate"
        out: dict[str, CommissionRates] = {}
        for i in range(0, len(symbols), FEES_BATCH):
            query = signed_query(self.api_key, self.api_secret, "GET", path, {"symbols": ",".join(symbols[i:i + FEES_BATCH])})
            data = self.rest.get(f"{REST_BASE}{path}?{query}")
            if data.get("code") != 200:
                raise RuntimeError(f"HTX al consultar comisiones: {data.get('message', data)}")
            for row in data["data"]:
                out[row["symbol"]] = rates_from_fee(row)
        return out
