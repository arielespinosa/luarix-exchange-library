"""Kraken spot (Fase 6), portado de luarix-arbitrage-web-server
(`engine/exchanges/kraken.py`) y completado con lo que necesita Luarix.

Capacidades: datos de mercado (velas, precio, libro, pares), reglas de pares,
reloj del servidor, comisiones de la cuenta, activos, libros en streaming
(con checksum) y velas cerradas en streaming. Sin cuenta ni órdenes todavía.

Particularidades que se traducen al modelo común (heredadas de arbitraje,
donde están probadas en producción):
- **Símbolos**: los del WebSocket v2 (`BTC/USDT`), que ya usan nombres comunes.
  En REST el mismo par se llama `XBTUSDT`; la traducción se hace por dentro.
- **Activos**: `XXBT`/`XBT` → `BTC`, `XDG` → `DOGE`, `ZUSD` → `USD`...
- **Comisión** en el activo QUOTE (`fee_on="quote"`).
- **Solo pares `online`**: los `cancel_only`/`post_only` no admiten mercado.
- **Libro**: foto + actualizaciones por nivel (`qty` 0 = quitar), recortado a la
  profundidad suscrita (Kraken lo exige para el checksum). Cada mensaje trae un
  CRC-32 de los 10 mejores niveles; si no cuadra (mensaje perdido) se reabre la
  conexión. Las cifras se leen como TEXTO para el CRC.
- **Reloj**: `Time` solo da segundos; el desfase se estima con las marcas del
  WebSocket: `desfase ≈ min(recibido − marca) − rtt/2`.
- **Velas REST** (`OHLC`): máximo 720 por llamada y SOLO las 720 más recientes
  (no hay histórico más antiguo por este endpoint). La última fila es la vela
  en curso; las cerradas son las de `time <= last`.
- **Velas por WebSocket** (`ohlc`): vela en curso sin marca de cierre → se usa
  `ClosedCandleEmitter` (WebSocket como disparador + REST + temporizador).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from luarix_exchange.capabilities import (
    AssetInfo,
    CandleStream,
    CandleSubscription,
    Exchange,
    Fees,
    MarketData,
    OrderBookStream,
    ServerClock,
    SymbolRulesSource,
)
from luarix_exchange.capabilities.streams import OnBook, OnCandle
from luarix_exchange.models.assets import AssetListing
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.market import Candle
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.exchanges._common import run_async
from luarix_exchange.streaming._util import deliver, iso_to_ms
from luarix_exchange.streaming.candles import ClosedCandleEmitter
from luarix_exchange.streaming.polling import PollingCandleStream
from luarix_exchange.streaming.reconnect import run_with_reconnect
from luarix_exchange.streaming.websocket import websocket_session

log = logging.getLogger(__name__)

REST_BASE = "https://api.kraken.com"
WS_PUBLIC = "wss://ws.kraken.com/v2"
DEPTHS = (10, 25, 100, 500, 1000)
CHECKSUM_LEVELS = 10
HUNDRED = Decimal(100)
ASSET_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
# Intervalo estándar -> minutos de Kraken (REST `OHLC` y WebSocket `ohlc`).
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080, "15d": 21600}
# Los que tienen frontera alineada con epoch (streaming con ClosedCandleEmitter);
# 1w y 15d van por polling.
STREAM_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
MAX_OHLC = 720
# `status` de /0/public/Assets -> (depósitos, retiradas).
ASSET_STATUS = {
    "enabled": (True, True),
    "deposit_only": (True, False),
    "withdrawal_only": (False, True),
    "funding_temporarily_disabled": (False, False),
}


# ── Parseo puro (sin red) ────────────────────────────────────────────────

def canonical_asset(code: str, assets: Mapping[str, dict] | None = None) -> str:
    """'XXBT' → 'BTC', 'ZEUR' → 'EUR', 'XBT' → 'BTC'. `assets` = /0/public/Assets."""
    altname = (assets or {}).get(code, {}).get("altname", code)
    return ASSET_ALIASES.get(altname, altname).upper()


def ws_symbol(wsname: str) -> str:
    """'XBT/USDT' (nombre en REST) → 'BTC/USDT' (símbolo del WebSocket v2)."""
    base, quote = wsname.split("/", 1)
    return f"{canonical_asset(base)}/{canonical_asset(quote)}"


def rules_from_pair(info: dict, assets: Mapping[str, dict] | None = None) -> SymbolRules:
    """Una fila de /0/public/AssetPairs → reglas del par."""
    return SymbolRules(
        symbol=ws_symbol(info["wsname"]),
        base=canonical_asset(info["base"], assets),
        quote=canonical_asset(info["quote"], assets),
        step_size=Decimal(1).scaleb(-int(info["lot_decimals"])),
        tick_size=Decimal(str(info["tick_size"])),
        min_qty=Decimal(str(info["ordermin"])),
        min_notional=Decimal(str(info.get("costmin") or 0)),
        market=Market.SPOT,
        fee_on="quote",
        raw=info,
    )


def tradable(info: dict) -> bool:
    """Par de contado operable a mercado y con nombre en el WebSocket."""
    return info.get("status") == "online" and info.get("aclass_base", "currency") == "currency" and bool(info.get("wsname"))


def _checksum_part(text: str, decimals: int) -> str:
    """Una cifra para el CRC desde su texto: `decimals` decimales, sin punto y
    sin ceros a la izquierda."""
    if "e" in text or "E" in text:
        text = f"{Decimal(text):.{decimals}f}"
    whole, _, frac = text.partition(".")
    return (whole + frac[:decimals].ljust(decimals, "0")).lstrip("0")


def _crc(parts: Iterable[str]) -> int:
    return zlib.crc32("".join(parts).encode())


def book_checksum(asks, bids, price_decimals: int, qty_decimals: int) -> int:
    """CRC-32 de Kraken: 10 mejores asks y luego 10 mejores bids."""
    return _crc(
        _checksum_part(format(price, "f"), price_decimals) + _checksum_part(format(qty, "f"), qty_decimals)
        for price, qty in [*asks[:CHECKSUM_LEVELS], *bids[:CHECKSUM_LEVELS]]
    )


def parse_timestamp(value: str) -> int:
    """'2026-10-07T13:33:24.426310Z' → ms (admite nanosegundos)."""
    ms = iso_to_ms(value)
    if ms is None:
        raise ValueError(f"marca de tiempo de Kraken ilegible: {value!r}")
    return ms


def sign_request(path: str, nonce: str, postdata: str, secret: str) -> str:
    """Firma privada: HMAC-SHA512(secreto, ruta + SHA256(nonce + datos)), base64."""
    message = path.encode() + hashlib.sha256((nonce + postdata).encode()).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret), message, hashlib.sha512).digest()).decode()


def rates_from_trade_volume(result: dict, pair_key: str, symbol: str) -> CommissionRates:
    """/0/private/TradeVolume: `fees[par].fee` (taker) y `fees_maker[par].fee`, en %."""
    taker = Decimal(str(result["fees"][pair_key]["fee"])) / HUNDRED
    maker_row = (result.get("fees_maker") or {}).get(pair_key)
    maker = Decimal(str(maker_row["fee"])) / HUNDRED if maker_row else taker
    return CommissionRates(symbol=symbol, maker=maker, taker=taker)


def candle_from_ohlc(symbol: str, interval: str, row: list) -> Candle:
    """Fila de /0/public/OHLC: [time(s), open, high, low, close, vwap, volume, count]."""
    apertura = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
    return Candle(
        symbol=symbol, timeframe=interval, open_time=apertura,
        open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]), volume=float(row[6]),
        close_time=apertura + timedelta(minutes=INTERVALS[interval]), is_closed=True,
    )


class BookState:
    """Libro de un par mantenido con el canal `book`, recortado a `depth`."""

    def __init__(self, symbol: str, depth: int) -> None:
        self.symbol = symbol
        self.depth = depth
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self._text: dict[Decimal, tuple[str, str]] = {}
        self._bids_sorted: list[tuple[Decimal, Decimal]] = []
        self._asks_sorted: list[tuple[Decimal, Decimal]] = []
        self.version = 0

    def apply(self, data: dict, snapshot: bool = False) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
            self._text.clear()
        for side, levels in ((self.bids, data.get("bids") or []), (self.asks, data.get("asks") or [])):
            for level in levels:
                price_text, qty_text = str(level["price"]), str(level["qty"])
                price, qty = Decimal(price_text), Decimal(qty_text)
                if qty == 0:
                    side.pop(price, None)
                    self._text.pop(price, None)
                else:
                    side[price] = qty
                    self._text[price] = (price_text, qty_text)
        self._bids_sorted = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)
        self._asks_sorted = sorted(self.asks.items(), key=lambda x: x[0])
        self._truncate()
        self.version += 1

    def _truncate(self) -> None:
        # Kraken no avisa de los niveles que salen por abajo: hay que recortar.
        for side, ordered in ((self.bids, self._bids_sorted), (self.asks, self._asks_sorted)):
            if len(ordered) > self.depth:
                for price, _ in ordered[self.depth:]:
                    del side[price]
                    self._text.pop(price, None)
                del ordered[self.depth:]

    def snapshot(self, received_ms: int, depth: int | None = None, event_ms: int | None = None) -> OrderBook:
        n = depth or self.depth
        return OrderBook(symbol=self.symbol, market=Market.SPOT, bids=list(self._bids_sorted[:n]),
                         asks=list(self._asks_sorted[:n]), update_id=self.version, received_ms=received_ms,
                         event_ms=event_ms)

    def checksum(self, price_decimals: int, qty_decimals: int) -> int:
        text = self._text
        return _crc(
            _checksum_part(text[price][0], price_decimals) + _checksum_part(text[price][1], qty_decimals)
            for price, _ in [*self._asks_sorted[:CHECKSUM_LEVELS], *self._bids_sorted[:CHECKSUM_LEVELS]]
        )


_run_async = run_async


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── El exchange ──────────────────────────────────────────────────────────

class KrakenExchange(Exchange, MarketData, SymbolRulesSource, ServerClock, Fees, AssetInfo, OrderBookStream,
                     CandleStream):
    name = "kraken"
    label = "Kraken"
    markets = frozenset({Market.SPOT})

    def __init__(self, api_key: str = "", api_secret: str = "", depth_levels: int = 25, timeout: float = 20.0):
        if depth_levels not in DEPTHS:
            raise ValueError("kraken.depth_levels debe ser 10, 25, 100, 500 o 1000 (canal book)")
        self.api_key = api_key
        self.api_secret = api_secret
        self.depth_levels = depth_levels
        self.timeout = timeout
        self.clock_offset = 0  # último desfase medido (ms)
        self._http = None
        self._lock = threading.Lock()
        self._rules: dict[str, SymbolRules] | None = None
        self._pair_keys: dict[str, str] = {}
        self._precision: dict[str, tuple[int, int]] = {}

    @classmethod
    def create(cls, credentials: Any = None, **options: Any) -> KrakenExchange:
        """Sin `credentials` intenta las del entorno (`KRAKEN_API_KEY`); los datos
        públicos no las necesitan."""
        if credentials is None:
            from luarix_exchange.brokers.credentials import BrokerCredentials

            credentials = BrokerCredentials.from_env("KRAKEN")
        return cls(api_key=getattr(credentials, "api_key", "") or "",
                   api_secret=getattr(credentials, "api_secret", "") or "", **options)

    # --- transporte REST (síncrono) -----------------------------------

    def _session(self):
        if self._http is None:
            import requests

            self._http = requests.Session()
        return self._http

    def _public(self, method: str, **params: Any) -> Any:
        resp = self._session().get(f"{REST_BASE}/0/public/{method}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken {method}: {', '.join(data['error'])}")
        return data["result"]

    def _private(self, method: str, params: dict | None = None) -> Any:
        if not (self.api_key and self.api_secret):
            raise ValueError(f"Kraken {method} necesita claves (KRAKEN_API_KEY / KRAKEN_API_SECRET).")
        path = f"/0/private/{method}"
        nonce = str(time.time_ns() // 1000)
        postdata = urllib.parse.urlencode({"nonce": nonce, **(params or {})})
        headers = {"API-Key": self.api_key, "API-Sign": sign_request(path, nonce, postdata, self.api_secret),
                   "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        resp = self._session().post(f"{REST_BASE}{path}", data=postdata, headers=headers, timeout=self.timeout)
        data = resp.json()
        if resp.status_code != 200 or data.get("error"):
            raise RuntimeError(f"Kraken {resp.status_code} en {method}: {', '.join(data.get('error') or []) or data}")
        return data["result"]

    def _load_pairs(self, force: bool = False) -> dict[str, SymbolRules]:
        with self._lock:
            if self._rules is None or force:
                assets = self._public("Assets")
                pairs = self._public("AssetPairs")
                rules, keys, precision = {}, {}, {}
                for key, info in pairs.items():
                    if not tradable(info):
                        continue
                    r = rules_from_pair(info, assets)
                    rules[r.symbol] = r
                    keys[r.symbol] = key
                    precision[r.symbol] = (int(info["pair_decimals"]), int(info["lot_decimals"]))
                self._rules, self._pair_keys, self._precision = rules, keys, precision
            return self._rules

    def refresh_rules(self) -> dict[str, SymbolRules]:
        """Vuelve a leer pares y reglas (por defecto se leen una vez)."""
        return dict(self._load_pairs(force=True))

    def _key(self, symbol: str) -> str:
        self._load_pairs()
        try:
            return self._pair_keys[symbol]
        except KeyError:
            raise ValueError(f"{symbol!r} no es un par de spot operable de Kraken. Los símbolos son los del "
                             f"WebSocket v2, p. ej. 'BTC/USDT' (ver list_symbols()).") from None

    @staticmethod
    def _spot(market: str) -> None:
        if Market(market) is not Market.SPOT:
            raise ValueError("Kraken: esta librería solo cubre spot (Kraken Futures es otra API).")

    # --- MarketData / SymbolRulesSource --------------------------------

    def list_symbols(self, market: str = "spot") -> list[str]:
        self._spot(market)
        return sorted(self._load_pairs())

    def get_symbol_rules(self, market: str = "spot") -> dict[str, SymbolRules]:
        self._spot(market)
        return dict(self._load_pairs())

    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        self._spot(market)
        result = self._public("Ticker", pair=self._key(symbol))
        return float(next(iter(result.values()))["c"][0])

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> OrderBook:
        self._spot(market)
        result = self._public("Depth", pair=self._key(symbol), count=limit)
        data = next(iter(result.values()))
        return OrderBook.from_levels(symbol, market, [(p, q) for p, q, *_ in data.get("bids", [])],
                                     [(p, q) for p, q, *_ in data.get("asks", [])], depth=limit, raw=data)

    def _ohlc(self, symbol: str, interval: str, since: int | None = None) -> list[Candle]:
        if interval not in INTERVALS:
            raise ValueError(f"Kraken no tiene velas de {interval!r}. Disponibles: {', '.join(INTERVALS)}.")
        params: dict[str, Any] = {"pair": self._key(symbol), "interval": INTERVALS[interval]}
        if since is not None:
            params["since"] = since
        result = self._public("OHLC", **params)
        ultimo_cerrado = int(result["last"])
        filas = next(v for k, v in result.items() if k != "last")
        return [candle_from_ohlc(symbol, interval, f) for f in filas if int(f[0]) <= ultimo_cerrado]

    def get_candles(self, symbol: str, interval: str = "1m", limit: int = 100, market: str = "spot") -> list[Candle]:
        self._spot(market)
        if limit > MAX_OHLC:
            raise ValueError(f"Kraken sirve como mucho {MAX_OHLC} velas por intervalo.")
        return self._ohlc(symbol, interval)[-limit:]

    def get_historical_candles(self, symbol: str, interval: str = "1m", start: datetime | None = None,
                               end: datetime | None = None, limit: int | None = None,
                               market: str = "spot") -> list[Candle]:
        """⚠ Kraken solo sirve las 720 velas MÁS RECIENTES de cada intervalo:
        si `start` es anterior a la más antigua disponible, levanta en vez de
        devolver un rango incompleto."""
        self._spot(market)
        velas = self._ohlc(symbol, interval, since=int(start.timestamp()) if start else None)
        if start is not None and velas and velas[0].open_time > start + timedelta(minutes=INTERVALS[interval]):
            raise ValueError(f"Kraken solo sirve las últimas {MAX_OHLC} velas de {interval}: la más antigua es de "
                             f"{velas[0].open_time:%Y-%m-%d %H:%M} UTC y se pidió desde {start:%Y-%m-%d %H:%M}.")
        velas = [v for v in velas if (start is None or v.open_time >= start) and (end is None or v.open_time <= end)]
        return velas[:limit] if limit else velas

    def latency_symbol(self, symbols: Sequence[str]) -> str | None:
        """Par con el que medir latencia: el más activo de los escuchados."""
        if not symbols:
            return None
        for preferido in ("BTC/USD", "BTC/USDT", "BTC/EUR", "ETH/USD", "ETH/USDT"):
            if preferido in symbols:
                return preferido
        return symbols[0]

    # --- ServerClock ----------------------------------------------------

    def server_time_ms(self) -> int:
        return _now_ms() + self.clock_offset_ms()

    def clock_offset_ms(self, sample_s: float = 3.0, symbol: str = "BTC/USD") -> int:
        """hora_kraken ≈ hora_local + offset. `Time` solo da segundos: se estima
        con las marcas del WebSocket (`min(recibido − marca) − rtt/2`). 0 si en
        `sample_s` no llega ninguna marca. Síncrono: desde código asíncrono,
        `await asyncio.to_thread(ex.clock_offset_ms)`."""
        medido = _run_async(self._measure_offset(sample_s, symbol))
        if medido is None:
            # Sin muestras en la ventana (exchange callado, límite de conexiones...):
            # devolver 0 falsearía todas las latencias. Se conserva el último.
            log.warning("kraken: sin marcas de tiempo en %.1f s para medir el reloj; se conserva el desfase "
                        "anterior (%+d ms)", sample_s, self.clock_offset)
            return self.clock_offset
        self.clock_offset = medido
        return self.clock_offset

    async def _measure_offset(self, sample_s: float, symbol: str) -> int | None:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            inicio = _now_ms()
            async with session.get(f"{REST_BASE}/0/public/Time", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                await resp.read()
            rtt = _now_ms() - inicio
            diffs: list[int] = []
            limite = time.monotonic() + sample_s
            async with session.ws_connect(WS_PUBLIC) as ws:
                await ws.send_json({"method": "subscribe", "params": {"channel": "book", "symbol": [symbol],
                                                                      "depth": 10, "snapshot": False}})
                while time.monotonic() < limite:
                    try:
                        msg = await ws.receive(timeout=max(0.05, limite - time.monotonic()))
                    except TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    recibido = _now_ms()
                    data = json.loads(msg.data)
                    if data.get("channel") == "book":
                        diffs += [recibido - parse_timestamp(d["timestamp"]) for d in data.get("data", [])
                                  if d.get("timestamp")]
        if not diffs:
            return None
        return -(min(diffs) - rtt // 2)

    # --- Fees / AssetInfo ------------------------------------------------

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        """/0/private/TradeVolume (clave con permiso "Query Funds")."""
        self._spot(market)
        self._load_pairs()
        claves = {s: self._pair_keys[s] for s in symbols if s in self._pair_keys}
        if not claves:
            return {}
        result = self._private("TradeVolume", {"pair": ",".join(claves.values())})
        return {s: rates_from_trade_volume(result, k, s) for s, k in claves.items() if k in result.get("fees", {})}

    def list_assets(self) -> dict[str, AssetListing]:
        """/0/public/Assets: nombre común, código de Kraken y estado de
        depósitos/retiradas (`status`). Las redes necesitan claves: no van."""
        assets = self._public("Assets")
        listado = {}
        for code, info in assets.items():
            if info.get("aclass", "currency") != "currency":
                continue
            deposito, retirada = ASSET_STATUS.get(info.get("status", ""), (None, None))
            comun = canonical_asset(code, assets)
            listado[comun] = AssetListing(asset=comun, symbol=code, deposit_enabled=deposito,
                                          withdraw_enabled=retirada, name=info.get("altname", ""))
        return listado

    # --- Streams --------------------------------------------------------

    async def stream_order_books(self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event,
                                 market: str = "spot") -> None:
        """Libros por el canal `book`, con checksum CRC-32 en cada mensaje."""
        self._spot(market)
        nivel = next((d for d in DEPTHS if d >= depth), None)
        if nivel is None:
            raise ValueError(f"Kraken: como mucho {DEPTHS[-1]} niveles por lado.")
        await asyncio.to_thread(self._load_pairs)
        desconocidos = [s for s in symbols if s not in self._precision]
        if desconocidos:
            raise ValueError(f"Kraken: pares desconocidos {desconocidos} (ver list_symbols()).")
        etiqueta = "kraken libros"
        libros: dict[str, BookState] = {}

        async def on_message(data: dict) -> None:
            if data.get("method") == "subscribe":
                if not data.get("success"):
                    raise ValueError(f"Kraken rechazó la suscripción al libro: {data.get('error')}")
                return
            if data.get("channel") != "book":
                return  # heartbeat, status
            recibido = _now_ms()
            es_foto = data.get("type") == "snapshot"
            for item in data.get("data", []):
                simbolo = item.get("symbol")
                if simbolo not in symbols:
                    continue
                libro = libros.setdefault(simbolo, BookState(simbolo, nivel))
                libro.apply(item, snapshot=es_foto)
                esperado = item.get("checksum")
                if esperado is not None and libro.checksum(*self._precision[simbolo]) != int(esperado):
                    raise ConnectionError(f"libro de {simbolo} desincronizado (checksum): se reabre la conexión")
                ts = item.get("timestamp")
                await deliver(on_book, libro.snapshot(recibido, depth, parse_timestamp(ts) if ts else None))

        suscripcion = {"method": "subscribe", "params": {"channel": "book", "symbol": list(symbols),
                                                         "depth": nivel, "snapshot": True}}

        async def sesion() -> None:
            libros.clear()  # cada conexión empieza de una foto limpia
            await websocket_session(WS_PUBLIC, on_message, stop, subscribe=[suscripcion],
                                    loads=lambda s: json.loads(s, parse_float=str), label=etiqueta)

        await run_with_reconnect(sesion, stop, label=etiqueta)

    async def stream_candles(self, subscriptions: Sequence[CandleSubscription], on_candle: OnCandle,
                             stop: asyncio.Event) -> None:
        for sub in subscriptions:
            self._spot(sub.market)
        nativas = [s for s in subscriptions if s.timeframe in STREAM_INTERVALS]
        resto = [s for s in subscriptions if s not in nativas]
        tareas = []
        if resto:
            tareas.append(PollingCandleStream(self).stream_candles(resto, on_candle, stop))
        if nativas:
            tareas.append(self._ws_candles(nativas, on_candle, stop))
        await asyncio.gather(*tareas)

    async def _ws_candles(self, subs: list[CandleSubscription], on_candle: OnCandle, stop: asyncio.Event) -> None:
        etiqueta = "kraken velas"
        por_clave = {(s.symbol, INTERVALS[s.timeframe]): s for s in subs}
        emisor = ClosedCandleEmitter(lambda sub: self.get_candles(sub.symbol, sub.timeframe, 3), on_candle,
                                     {s: INTERVALS[s.timeframe] * 60_000 for s in subs}, label=etiqueta)
        por_intervalo: dict[int, list[str]] = defaultdict(list)
        for simbolo, minutos in por_clave:
            por_intervalo[minutos].append(simbolo)
        suscripciones = [{"method": "subscribe", "params": {"channel": "ohlc", "symbol": simbolos,
                                                            "interval": minutos, "snapshot": False}}
                         for minutos, simbolos in por_intervalo.items()]

        async def on_message(data: dict) -> None:
            if data.get("method") == "subscribe" and not data.get("success"):
                raise ValueError(f"Kraken rechazó la suscripción a velas: {data.get('error')}")
            if data.get("channel") != "ohlc":
                return
            for item in data.get("data", []):
                sub = por_clave.get((item.get("symbol"), item.get("interval")))
                inicio = iso_to_ms(item.get("interval_begin"))
                if sub is not None and inicio is not None:
                    await emisor.period_seen(sub, inicio)

        async def sesion() -> None:
            await websocket_session(WS_PUBLIC, on_message, stop, subscribe=suscripciones, label=etiqueta)

        await asyncio.gather(run_with_reconnect(sesion, stop, label=etiqueta), emisor.run_boundary_timer(stop))
