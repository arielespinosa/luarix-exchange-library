"""OKX spot, portado de luarix-arbitrage-web-server (`engine/exchanges/okx.py`).

- Pares `BTC-USDT`; base y quote ya vienen con su nombre común.
- Canal `books`: FOTO de 400 niveles y después cambios por nivel (cantidad 0 =
  quitar), con `LevelBook`. Cada mensaje trae `seqId`/`prevSeqId`: si
  `prevSeqId` no es el último `seqId` aplicado se perdió un mensaje y se reabre
  la conexión. Con `checksum` distinto de 0 (OKX lo ha ido retirando) se
  comprueba el CRC-32 de los 25 mejores niveles.
- Con `depth_levels=5` se usa `books5`: una foto de 5 niveles cada 100 ms.
- OKX corta sin un mensaje en 30 s: se manda `ping` (texto plano) cada 25.
- Cada mensaje trae la hora de OKX (`ts`, ms) → `OrderBook.event_ms`.
- Comisión de la cuenta: `GET /api/v5/account/trade-fee` (por grupo de pares),
  con clave, secreto y passphrase; OKX la da en negativo y se guarda en positivo.
- Modo demo (`x-simulated-trading: 1`): mide la ida y vuelta de una orden.

Profundidad (`depth_levels`): 1-400 (25 por defecto; 5 usa `books5`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import zlib
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from luarix_exchange.capabilities import OrderRoundtripProbe
from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._base import SpotConnector
from luarix_exchange.exchanges._common import LevelBook, levels, now_ms
from luarix_exchange.models.book import Level
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.websocket import websocket_session

log = logging.getLogger(__name__)

REST_BASE = "https://www.okx.com"
WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
SUBSCRIBE_BATCH = 50
PING_S = 25
CHECKSUM_LEVELS = 25


class BookGap(ConnectionError):
    """Se perdió un mensaje del libro (o no cuadra el checksum): se reabre la conexión."""


# --- parseo puro (sin red) -----------------------------------------------------

def rules_from_instrument(info: dict) -> SymbolRules:
    return SymbolRules(
        symbol=info["instId"], base=info["baseCcy"].upper(), quote=info["quoteCcy"].upper(),
        step_size=Decimal(str(info["lotSz"])), tick_size=Decimal(str(info["tickSz"])), min_qty=Decimal(str(info["minSz"])),
        min_notional=Decimal(0),  # OKX no fija importe mínimo en spot: manda la cantidad mínima
        market=Market.SPOT, fee_on="received", raw=info,
    )


def tradable(info: dict) -> bool:
    return info.get("state") == "live"


def _text(value: Decimal) -> str:
    return format(value, "f")


def book_checksum(bids: list[Level], asks: list[Level]) -> int:
    """CRC-32 (con signo) de los 25 mejores niveles intercalados precio:cantidad."""
    parts: list[str] = []
    for i in range(CHECKSUM_LEVELS):
        if i < len(bids):
            parts += [_text(bids[i][0]), _text(bids[i][1])]
        if i < len(asks):
            parts += [_text(asks[i][0]), _text(asks[i][1])]
    crc = zlib.crc32(":".join(parts).encode())
    return crc - (1 << 32) if crc >= 1 << 31 else crc


def apply_message(books: dict[str, LevelBook], symbol: str, action: str | None, data: dict) -> LevelBook:
    """Aplica un mensaje del canal `books` (o `books5`, siempre foto) y lo devuelve."""
    bids, asks = levels(data.get("bids", [])), levels(data.get("asks", []))
    seq = int(data.get("seqId", 0))
    if action in (None, "snapshot"):
        book = books.setdefault(symbol, LevelBook(symbol))
        book.reset(bids, asks, seq)
    else:
        existing = books.get(symbol)
        if existing is None:
            raise BookGap(f"OKX: cambio de {symbol} sin foto previa")
        prev = int(data.get("prevSeqId", -1))
        if prev != existing.update_id:
            raise BookGap(f"OKX: se perdió un mensaje de {symbol} (esperaba {existing.update_id}, llegó {prev})")
        existing.apply(bids, asks, seq)
        book = existing
    checksum = int(data.get("checksum") or 0)
    if checksum and action is not None:
        top_bids, top_asks = book.sorted_levels(CHECKSUM_LEVELS)
        if book_checksum(top_bids, top_asks) != checksum:
            raise BookGap(f"OKX: el checksum de {symbol} no cuadra")
    return book


def auth_headers(api_key: str, api_secret: str, passphrase: str, method: str, path: str, body: str = "",
                 now: datetime | None = None) -> dict[str, str]:
    if not passphrase:
        raise LookupError("OKX necesita la passphrase de la clave (OKX_API_PASSPHRASE)")
    momento = now or datetime.now(UTC)
    stamp = momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"
    firma = base64.b64encode(hmac.new(api_secret.encode(), f"{stamp}{method}{path}{body}".encode(),
                                      hashlib.sha256).digest()).decode()
    return {"OK-ACCESS-KEY": api_key, "OK-ACCESS-SIGN": firma, "OK-ACCESS-TIMESTAMP": stamp,
            "OK-ACCESS-PASSPHRASE": passphrase, "Content-Type": "application/json"}


def rates_by_group(row: dict) -> tuple[CommissionRates, dict[str, CommissionRates]]:
    """(general, por grupo de pares) de `trade-fee`, en positivo."""
    def tasas(item: dict) -> CommissionRates:
        return CommissionRates(symbol="", maker=abs(Decimal(str(item["maker"]))), taker=abs(Decimal(str(item["taker"]))))

    grupos = {str(g["groupId"]): tasas(g) for g in row.get("feeGroup") or []}
    return tasas(row), grupos


def far_limit_order(price: Decimal, rules: SymbolRules, max_deviation: Decimal) -> tuple[Decimal, Decimal]:
    limit = (price * (1 - max_deviation * Decimal("0.8")) / rules.tick_size).to_integral_value(ROUND_DOWN) * rules.tick_size
    qty = ((Decimal(10) / limit) / rules.step_size).to_integral_value(ROUND_UP) * rules.step_size
    return limit, max(qty, rules.min_qty)


# --- el exchange ---------------------------------------------------------------

class OkxExchange(SpotConnector, OrderRoundtripProbe):
    name = "okx"
    label = "OKX"
    DEFAULT_DEPTH = 25
    LATENCY_PREFERRED = ("BTC-USDT", "ETH-USDT")

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.channel = "books5" if self.depth_levels == 5 else "books"
        self._groups: dict[str, str] = {}

    def _validate_depth(self, depth: int) -> None:
        if not 1 <= depth <= 400:
            raise ValueError("okx.depth_levels debe estar entre 1 y 400")

    def _instruments(self, headers: dict | None = None, inst_id: str | None = None) -> list[dict]:
        params = {"instType": "SPOT", **({"instId": inst_id} if inst_id else {})}
        return self.rest.get(f"{REST_BASE}/api/v5/public/instruments", params=params, headers=headers or {})["data"]

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        instrumentos = [i for i in self._instruments() if tradable(i)]
        self._groups = {i["instId"]: str(i.get("groupId", "")) for i in instrumentos}
        return {i["instId"]: rules_from_instrument(i) for i in instrumentos}

    def server_time_ms(self) -> int:
        return int(self.rest.get(f"{REST_BASE}/api/v5/public/time")["data"][0]["ts"])

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        books: dict[str, LevelBook] = {}
        lotes = [{"op": "subscribe", "args": [{"channel": self.channel, "instId": s} for s in symbols[i:i + SUBSCRIBE_BATCH]]}
                 for i in range(0, len(symbols), SUBSCRIBE_BATCH)]

        async def on_message(message: Any) -> None:
            if message is None:  # "pong"
                return
            if message.get("event") == "error":
                raise RuntimeError(f"OKX rechazó la suscripción: {message.get('msg', message)}")
            if "data" not in message:
                return
            symbol = message["arg"]["instId"]
            action = message.get("action")
            for data in message["data"]:
                book = apply_message(books, symbol, action, data)
                ts = int(data["ts"]) if data.get("ts") else None
                await deliver(on_book, book.snapshot(now_ms(), depth, ts))

        await websocket_session(WS_URL, on_message, stop, subscribe=lotes, keepalive=lambda: "ping", keepalive_s=PING_S,
                                loads=lambda s: None if s == "pong" else json.loads(s), heartbeat=None,
                                max_msg_size=16 * 1024 * 1024, label="okx libros")

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        self._spot(market)
        self._require_keys("la comisión de la cuenta", passphrase=True)
        self.get_symbol_rules()  # rellena los grupos de comisión
        path = "/api/v5/account/trade-fee?instType=SPOT"
        data = self.rest.get(f"{REST_BASE}{path}",
                             headers=auth_headers(self.api_key, self.api_secret, self.passphrase, "GET", path))
        if data.get("code") != "0":
            raise RuntimeError(f"OKX al consultar comisiones: {data.get('msg', data)}")
        general, grupos = rates_by_group(data["data"][0])
        out = {}
        for s in symbols:
            tasas = grupos.get(self._groups.get(s, ""), general)
            out[s] = CommissionRates(symbol=s, maker=tasas.maker, taker=tasas.taker)
        return out

    def measure_order_roundtrip_ms(self, credentials: Any, samples: int = 8, symbol: str = "BTC-USDT") -> list[int]:
        """En el MODO DEMO: `samples` compras límite que no se ejecutan, canceladas.
        `credentials`: claves del modo demo, con passphrase."""
        demo = {"x-simulated-trading": "1"}
        passphrase = getattr(credentials, "passphrase", "") or ""
        info = self._instruments(demo, symbol)[0]
        rules = rules_from_instrument(info)
        ticker = self.rest.get(f"{REST_BASE}/api/v5/market/ticker", params={"instId": symbol}, headers=demo)
        price = Decimal(str(ticker["data"][0]["last"]))
        limit, qty = far_limit_order(price, rules, Decimal(str(info.get("maxPxLmtPct") or "0.05")))

        def post(path: str, body: dict) -> dict:
            text = json.dumps(body, separators=(",", ":"))
            headers = {**auth_headers(credentials.api_key, credentials.api_secret, passphrase, "POST", path, text), **demo}
            data = self.rest.post(f"{REST_BASE}{path}", data=text, headers=headers)
            if data.get("code") != "0":
                raise RuntimeError(f"OKX demo: {data.get('msg', data)}")
            return data["data"][0]

        self.rest.get(f"{REST_BASE}/api/v5/public/time")  # abre la conexión: la 1ª medida no paga el TLS
        tiempos: list[int] = []
        for _ in range(samples):
            orden = {"instId": symbol, "tdMode": "cash", "side": "buy", "ordType": "limit", "px": str(limit), "sz": str(qty)}
            inicio = now_ms()
            colocada = post("/api/v5/trade/order", orden)
            tiempos.append(now_ms() - inicio)
            post("/api/v5/trade/cancel-order", {"instId": symbol, "ordId": colocada["ordId"]})
            time.sleep(0.2)
        log.info("OKX demo: %d órdenes de prueba, ida y vuelta %s ms", samples, tiempos)
        return tiempos
