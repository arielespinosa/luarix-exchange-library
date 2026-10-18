"""KuCoin spot, portado de luarix-arbitrage-web-server (`engine/exchanges/kucoin.py`).

- Pares `BTC-USDT`; base y quote ya vienen con su nombre común.
- El WebSocket necesita antes un token (`POST /bullet-public`), que da también
  el servidor y cada cuánto mandar un ping PROPIO (sin él, KuCoin cierra).
- Libro: FOTO de 5 o 50 niveles cada 100 ms, con la hora de KuCoin
  (`timestamp`, ms) → `OrderBook.event_ms` (y `update_id`, como en arbitraje).
- Comisión en el activo de `feeCurrency`: si es el quote, `fee_on="quote"`.
- Activos (público): estado y redes con comisión y mínimo de retirada.
- Comisión de la cuenta: `GET /api/v1/trade-fees` (10 pares por petición), con
  clave, secreto y passphrase.

Profundidad (`depth_levels`): 5 o 50 (50).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal

from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._base import SpotConnector
from luarix_exchange.exchanges._common import levels, now_ms, snapshot_book
from luarix_exchange.models.assets import AssetListing, AssetNetworkInfo
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.websocket import websocket_session

REST_BASE = "https://api.kucoin.com"
DEPTHS = (5, 50)
SUBSCRIBE_BATCH = 50
FEES_BATCH = 10


def rules_from_symbol(info: dict) -> SymbolRules:
    quote = info["quoteCurrency"].upper()
    return SymbolRules(
        symbol=info["symbol"], base=info["baseCurrency"].upper(), quote=quote,
        step_size=Decimal(str(info["baseIncrement"])), tick_size=Decimal(str(info["priceIncrement"])),
        min_qty=Decimal(str(info["baseMinSize"])),
        min_notional=Decimal(str(info.get("minFunds") or info.get("quoteMinSize") or "0")),
        market=Market.SPOT, fee_on="quote" if str(info.get("feeCurrency", "")).upper() == quote else "received", raw=info,
    )


def _decimal(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def listing_from_currency(info: dict) -> AssetListing:
    code = str(info["currency"])
    chains = info.get("chains") or []
    deposit = any(c.get("isDepositEnabled") for c in chains) if chains else None
    withdraw = any(c.get("isWithdrawEnabled") for c in chains) if chains else None
    networks = tuple(
        AssetNetworkInfo(network=str(c.get("chainName") or c.get("chainId")), contract_address=str(c.get("contractAddress") or ""),
                         deposit_enabled=c.get("isDepositEnabled"), withdraw_enabled=c.get("isWithdrawEnabled"),
                         withdraw_fee=_decimal(c.get("withdrawalMinFee")), min_withdraw=_decimal(c.get("withdrawalMinSize")))
        for c in chains if c.get("chainName") or c.get("chainId")
    )
    return AssetListing(code.upper(), code, deposit, withdraw, str(info.get("fullName") or ""), networks)


def book_topic(depth: int, symbols: list[str]) -> str:
    return f"/spotMarket/level2Depth{depth}:{','.join(symbols)}"


def parse_book(topic: str, data: dict, received_ms: int, depth: int) -> OrderBook:
    """Mensaje del tema `/spotMarket/level2Depth<N>:<par>` (foto completa)."""
    symbol = topic.rsplit(":", 1)[1]
    ts = int(data.get("timestamp", 0))
    return snapshot_book(symbol, levels(data["bids"]), levels(data["asks"]), ts, received_ms, depth, ts or None)


def sign(secret: str, text: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha256).digest()).decode()


def auth_headers(api_key: str, api_secret: str, passphrase: str, method: str, endpoint: str, body: str = "",
                 timestamp: str | None = None) -> dict[str, str]:
    """Cabeceras v2: firma de hora+método+ruta(+consulta)+cuerpo y la passphrase
    firmada también con el secreto."""
    if not passphrase:
        raise LookupError("KuCoin necesita la passphrase de la clave (KUCOIN_API_PASSPHRASE)")
    timestamp = timestamp or str(int(time.time() * 1000))
    return {"KC-API-KEY": api_key, "KC-API-SIGN": sign(api_secret, f"{timestamp}{method}{endpoint}{body}"),
            "KC-API-TIMESTAMP": timestamp, "KC-API-PASSPHRASE": sign(api_secret, passphrase), "KC-API-KEY-VERSION": "2"}


def rates_from_fee(row: dict) -> CommissionRates:
    return CommissionRates(symbol=row["symbol"], maker=Decimal(str(row["makerFeeRate"])),
                           taker=Decimal(str(row["takerFeeRate"])))


class KucoinExchange(SpotConnector):
    name = "kucoin"
    label = "KuCoin"
    DEPTHS = DEPTHS
    DEFAULT_DEPTH = 50
    LATENCY_PREFERRED = ("BTC-USDT", "ETH-USDT")

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        data = self.rest.get(f"{REST_BASE}/api/v2/symbols")
        return {i["symbol"]: rules_from_symbol(i) for i in data["data"] if i.get("enableTrading")}

    def list_assets(self) -> dict[str, AssetListing]:
        data = self.rest.get(f"{REST_BASE}/api/v3/currencies")
        return {listing.asset: listing for listing in map(listing_from_currency, data["data"])}

    def server_time_ms(self) -> int:
        return int(self.rest.get(f"{REST_BASE}/api/v1/timestamp")["data"])

    def _ws_endpoint(self) -> tuple[str, float]:
        """(URL con token, segundos entre pings) de `POST /api/v1/bullet-public`."""
        data = self.rest.post(f"{REST_BASE}/api/v1/bullet-public")["data"]
        servidor = data["instanceServers"][0]
        return f"{servidor['endpoint']}?token={data['token']}", int(servidor["pingInterval"]) / 1000

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        url, ping_s = await asyncio.to_thread(self._ws_endpoint)
        lotes = [{"id": str(i), "type": "subscribe", "topic": book_topic(self.depth_levels, symbols[i:i + SUBSCRIBE_BATCH])}
                 for i in range(0, len(symbols), SUBSCRIBE_BATCH)]

        async def on_message(payload: dict) -> None:
            tipo = payload.get("type")
            if tipo == "error":
                raise RuntimeError(f"KuCoin: {payload.get('data', payload)}")
            if tipo == "message":
                await deliver(on_book, parse_book(payload["topic"], payload["data"], now_ms(), depth))

        await websocket_session(url, on_message, stop, subscribe=lotes,
                                keepalive=lambda: {"id": str(now_ms()), "type": "ping"}, keepalive_s=ping_s,
                                heartbeat=None, max_msg_size=8 * 1024 * 1024, label="kucoin libros")

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        """`GET /api/v1/trade-fees`, de 10 en 10 pares."""
        self._spot(market)
        self._require_keys("la comisión de la cuenta", passphrase=True)
        out: dict[str, CommissionRates] = {}
        for i in range(0, len(symbols), FEES_BATCH):
            endpoint = f"/api/v1/trade-fees?{urllib.parse.urlencode({'symbols': ','.join(symbols[i:i + FEES_BATCH])})}"
            data = self.rest.get(f"{REST_BASE}{endpoint}",
                                 headers=auth_headers(self.api_key, self.api_secret, self.passphrase, "GET", endpoint))
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin al consultar comisiones: {data.get('msg', data)}")
            for row in data["data"]:
                out[row["symbol"]] = rates_from_fee(row)
        return out
