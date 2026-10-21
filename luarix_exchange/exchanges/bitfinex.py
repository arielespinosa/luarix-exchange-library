"""Bitfinex spot (Fase 6), portado de luarix-arbitrage-web-server
(`engine/exchanges/bitfinex.py`) y completado con lo que necesita Luarix.

Capacidades: datos de mercado (velas, precio, libro, pares), reglas de pares,
reloj del servidor, comisiones de la cuenta, activos, libros en streaming (con
checksum) y velas cerradas en streaming. Sin cuenta ni órdenes todavía.

Particularidades (heredadas de arbitraje, probadas en producción):
- **Símbolos** nativos: `tBTCUST` (3+3) o `tAVAX:BTC` (con `:` si una moneda
  tiene más de 3 letras). USDT se llama `UST`, USDC `UDC`: las reglas traducen
  base/quote a nombres comunes con el mapa oficial.
- **Libro**: foto + actualizaciones por nivel (`[precio, nº órdenes, cantidad]`,
  cantidad > 0 compra, < 0 venta, nº órdenes 0 = quitar). Con el flag CHECKSUM
  llega cada pocos mensajes un CRC-32 de 25 niveles; si no cuadra se reabre.
  Con el flag TIMESTAMP cada mensaje trae la hora del exchange (`event_ms`).
- **Precios**: 5 cifras significativas, sin tick fijo (`tick_size = 0`).
- **Sin endpoint de hora**: desfase estimado con las marcas del WebSocket.
- **Velas REST**: columnas `[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]` -- el CIERRE
  va ANTES que el máximo. Leerlas como O/H/L/C cambiaría los precios en silencio.
- **Velas por WebSocket**: vela en curso sin marca de cierre →
  `ClosedCandleEmitter` (WebSocket como disparador + REST + temporizador).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
import zlib
from collections.abc import Sequence
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
    assets_from_rules,
)
from luarix_exchange.capabilities.streams import OnBook, OnCandle
from luarix_exchange.exchanges._common import run_async as _run_async
from luarix_exchange.models.assets import AssetListing
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.market import Candle
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver
from luarix_exchange.streaming.candles import ClosedCandleEmitter
from luarix_exchange.streaming.polling import PollingCandleStream
from luarix_exchange.streaming.reconnect import run_with_reconnect
from luarix_exchange.streaming.websocket import websocket_session

log = logging.getLogger(__name__)

REST_PUBLIC = "https://api-pub.bitfinex.com/v2"
REST_AUTH = "https://api.bitfinex.com"
WS_PUBLIC = "wss://api-pub.bitfinex.com/ws/2"
FLAG_TIMESTAMP = 32768
FLAG_CHECKSUM = 131072
CHECKSUM_LEVELS = 25
BOOK_LENGTHS = (1, 25, 100)
AMOUNT_STEP = Decimal("0.00000001")
FIAT = {"USD", "EUR", "GBP", "JPY"}
STABLE = {"USDT", "USDC", "TUSD", "DAI", "EURT", "XAUT"}
# Intervalo estándar -> timeframe de Bitfinex y su duración en ms (0 = sin
# frontera alineada: va por polling en streaming).
TIMEFRAMES = {
    "1m": ("1m", 60_000), "5m": ("5m", 300_000), "15m": ("15m", 900_000), "30m": ("30m", 1_800_000),
    "1h": ("1h", 3_600_000), "3h": ("3h", 10_800_000), "6h": ("6h", 21_600_000), "12h": ("12h", 43_200_000),
    "1d": ("1D", 86_400_000), "1w": ("1W", 0), "14d": ("14D", 0), "1M": ("1M", 0),
}
MAX_CANDLES = 10_000
# Bitfinex pide reconectar con estos `info`.
RECONNECT_CODES = (20051, 20061)


# ── Parseo puro (sin red) ────────────────────────────────────────────────

def split_pair(pair: str) -> tuple[str, str]:
    """'BTCUST' → ('BTC', 'UST'); 'AVAX:BTC' → ('AVAX', 'BTC')."""
    if ":" in pair:
        base, quote = pair.split(":", 1)
        return base, quote
    return pair[:3], pair[3:]


def canonical_currency(code: str, currency_map: dict[str, str]) -> str:
    """UST → USDT, UDC → USDC... (mapa oficial, en mayúsculas)."""
    return currency_map.get(code, code).upper()


def rules_from_pair(pair: str, info: list, currency_map: dict[str, str]) -> SymbolRules:
    """`info` = fila de pub:info:pair: [_, _, _, min_order_size, max_order_size, ...]."""
    base, quote = split_pair(pair)
    return SymbolRules(
        symbol=f"t{pair}",
        base=canonical_currency(base, currency_map),
        quote=canonical_currency(quote, currency_map),
        step_size=AMOUNT_STEP,
        tick_size=Decimal(0),  # 5 cifras significativas: no hay tick fijo
        min_qty=Decimal(str(info[3])),
        min_notional=Decimal(0),
        market=Market.SPOT,
        fee_on="received",
        raw=info,
    )


def taker_fee_for(symbol_quote: str, fees: list) -> Decimal:
    """`fees` = [cripto, stable, fiat] de auth/r/summary; según el quote del par."""
    if symbol_quote in FIAT:
        return Decimal(str(fees[2]))
    if symbol_quote in STABLE:
        return Decimal(str(fees[1]))
    return Decimal(str(fees[0]))


def candle_from_row(symbol: str, interval: str, row: list) -> Candle:
    """[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME] -- ojo: CLOSE antes que HIGH."""
    apertura = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
    duracion = TIMEFRAMES[interval][1]
    return Candle(
        symbol=symbol, timeframe=interval, open_time=apertura,
        open=float(row[1]), close=float(row[2]), high=float(row[3]), low=float(row[4]), volume=float(row[5]),
        close_time=apertura + timedelta(milliseconds=duracion) if duracion else None, is_closed=True,
    )


class BookState:
    """Libro de un par con las actualizaciones del canal `book`. Guarda los
    números como texto exacto (checksum) y como Decimal (cálculo)."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[str, tuple[Decimal, Decimal, str]] = {}
        self.asks: dict[str, tuple[Decimal, Decimal, str]] = {}
        self.version = 0

    def apply_snapshot(self, levels: list[list]) -> None:
        self.bids.clear()
        self.asks.clear()
        for level in levels:
            self.apply_update(level)

    def apply_update(self, level: list) -> None:
        price_s, count, amount_s = str(level[0]), int(level[1]), str(level[2])
        amount = Decimal(amount_s)
        side = self.bids if amount > 0 else self.asks
        if count == 0:
            side.pop(price_s, None)
        else:
            side[price_s] = (Decimal(price_s), abs(amount), amount_s)
        self.version += 1

    def _sorted(self):
        bids = sorted(((p, q, ps, a) for ps, (p, q, a) in self.bids.items()), key=lambda x: x[0], reverse=True)
        asks = sorted(((p, q, ps, a) for ps, (p, q, a) in self.asks.items()), key=lambda x: x[0])
        return bids, asks

    def snapshot(self, depth: int, received_ms: int, event_ms: int | None = None) -> OrderBook:
        bids, asks = self._sorted()
        return OrderBook(symbol=self.symbol, market=Market.SPOT, bids=[(p, q) for p, q, _, _ in bids[:depth]],
                         asks=[(p, q) for p, q, _, _ in asks[:depth]], update_id=self.version,
                         received_ms=received_ms, event_ms=event_ms)

    def checksum(self) -> int:
        """CRC-32 con signo de 25 niveles intercalados 'pbid:abid:pask:aask:…'."""
        bids, asks = self._sorted()
        parts: list[str] = []
        for i in range(CHECKSUM_LEVELS):
            if i < len(bids):
                parts += [bids[i][2], bids[i][3]]
            if i < len(asks):
                parts += [asks[i][2], asks[i][3]]
        crc = zlib.crc32(":".join(parts).encode())
        return crc - (1 << 32) if crc >= (1 << 31) else crc


def _is_timestamp(value: object) -> bool:
    try:
        return int(str(value)) > 1_000_000_000_000
    except ValueError:
        return False


def parse_message(data: list) -> tuple[str, int, list | None, int | None]:
    """[chanId, payload, (checksum), ts] → (tipo, chanId, payload, ts). Tipos:
    snapshot, update, hb, cs (payload = [crc])."""
    chan_id = int(data[0])
    payload = data[1]
    ts = int(data[-1]) if len(data) > 2 and _is_timestamp(data[-1]) else None
    if payload == "hb":
        return "hb", chan_id, None, ts
    if payload == "cs":
        return "cs", chan_id, [int(data[2])], ts
    if payload and isinstance(payload[0], list):
        return "snapshot", chan_id, payload, ts
    return "update", chan_id, payload, ts


def _now_ms() -> int:
    return int(time.time() * 1000)


def _control(data: dict, etiqueta: str) -> None:
    """Mensajes de control (dict) de Bitfinex."""
    evento = data.get("event")
    if evento == "error":
        # Un error de suscripción (símbolo/canal inválido) no se arregla reintentando.
        raise ValueError(f"{etiqueta}: Bitfinex: {data.get('msg')} (código {data.get('code')})")
    if evento == "info" and data.get("code") in RECONNECT_CODES:
        raise ConnectionError(f"{etiqueta}: Bitfinex pide reconectar (código {data.get('code')})")


# ── El exchange ──────────────────────────────────────────────────────────

class BitfinexExchange(Exchange, MarketData, SymbolRulesSource, ServerClock, Fees, AssetInfo, OrderBookStream,
                       CandleStream):
    name = "bitfinex"
    label = "Bitfinex"
    markets = frozenset({Market.SPOT})

    def __init__(self, api_key: str = "", api_secret: str = "", depth_levels: int = 25, timeout: float = 20.0):
        if depth_levels not in BOOK_LENGTHS:
            raise ValueError("bitfinex.depth_levels debe ser 1, 25 o 100 (canal book)")
        self.api_key = api_key
        self.api_secret = api_secret
        self.depth_levels = depth_levels
        self.timeout = timeout
        self.clock_offset = 0
        self._http = None
        self._lock = threading.Lock()
        self._rules: dict[str, SymbolRules] | None = None
        self._currency_map: dict[str, str] = {}

    @classmethod
    def create(cls, credentials: Any = None, **options: Any) -> BitfinexExchange:
        if credentials is None:
            from luarix_exchange.brokers.credentials import BrokerCredentials

            credentials = BrokerCredentials.from_env("BITFINEX")
        return cls(api_key=getattr(credentials, "api_key", "") or "",
                   api_secret=getattr(credentials, "api_secret", "") or "", **options)

    # --- transporte REST (síncrono) -----------------------------------

    def _session(self):
        if self._http is None:
            import requests

            self._http = requests.Session()
        return self._http

    def _public(self, path: str, **params: Any) -> Any:
        resp = self._session().get(f"{REST_PUBLIC}/{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data and data[0] == "error":
            raise RuntimeError(f"Bitfinex {path}: {data}")
        return data

    def _auth(self, path: str, body: dict | None = None) -> Any:
        if not (self.api_key and self.api_secret):
            raise ValueError(f"Bitfinex {path} necesita claves (BITFINEX_API_KEY / BITFINEX_API_SECRET).")
        raw = json.dumps(body or {})
        nonce = str(time.time_ns() // 1000)
        firma = hmac.new(self.api_secret.encode(), f"/api/{path}{nonce}{raw}".encode(), hashlib.sha384).hexdigest()
        headers = {"bfx-nonce": nonce, "bfx-apikey": self.api_key, "bfx-signature": firma,
                   "content-type": "application/json"}
        resp = self._session().post(f"{REST_AUTH}/{path}", data=raw, headers=headers, timeout=self.timeout)
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"Bitfinex {resp.status_code} en {path}: {data}")
        return data

    def _load_pairs(self, force: bool = False) -> dict[str, SymbolRules]:
        with self._lock:
            if self._rules is None or force:
                pares: list[str] = self._public("conf/pub:list:pair:exchange")[0]
                info = {fila[0]: fila[1] for fila in self._public("conf/pub:info:pair")[0]}
                self._currency_map = {code: nombre for code, nombre in self._public("conf/pub:map:currency:sym")[0]}
                reglas = {}
                for par in pares:
                    if par not in info or par.startswith("TEST"):
                        continue
                    r = rules_from_pair(par, info[par], self._currency_map)
                    reglas[r.symbol] = r
                self._rules = reglas
            return self._rules

    def refresh_rules(self) -> dict[str, SymbolRules]:
        return dict(self._load_pairs(force=True))

    def _check(self, symbol: str) -> str:
        if symbol not in self._load_pairs():
            raise ValueError(f"{symbol!r} no es un par de spot de Bitfinex. Los símbolos son los nativos, "
                             f"p. ej. 'tBTCUST' (ver list_symbols()).")
        return symbol

    @staticmethod
    def _spot(market: str) -> None:
        if Market(market) is not Market.SPOT:
            raise ValueError("Bitfinex: esta librería solo cubre spot por ahora.")

    # --- MarketData / SymbolRulesSource --------------------------------

    def list_symbols(self, market: str = "spot") -> list[str]:
        self._spot(market)
        return sorted(self._load_pairs())

    def get_symbol_rules(self, market: str = "spot") -> dict[str, SymbolRules]:
        self._spot(market)
        return dict(self._load_pairs())

    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        """`/ticker`: [BID, BID_SIZE, ASK, ASK_SIZE, DAILY_CHANGE, DAILY_CHANGE_REL, LAST_PRICE, ...]."""
        self._spot(market)
        return float(self._public(f"ticker/{self._check(symbol)}")[6])

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> OrderBook:
        self._spot(market)
        largo = next((n for n in BOOK_LENGTHS if n >= limit), BOOK_LENGTHS[-1])
        filas = self._public(f"book/{self._check(symbol)}/P0", len=largo)
        bids = [(f[0], f[2]) for f in filas if float(f[2]) > 0]
        asks = [(f[0], -float(f[2])) for f in filas if float(f[2]) < 0]
        return OrderBook.from_levels(symbol, market, bids, asks, depth=limit, raw=filas)

    def _candles(self, symbol: str, interval: str, *, limit: int, start_ms: int | None = None,
                 end_ms: int | None = None) -> list[Candle]:
        """Velas CERRADAS, de la más antigua a la más nueva."""
        if interval not in TIMEFRAMES:
            raise ValueError(f"Bitfinex no tiene velas de {interval!r}. Disponibles: {', '.join(TIMEFRAMES)}.")
        tf, duracion = TIMEFRAMES[interval]
        params: dict[str, Any] = {"limit": min(limit + 1, MAX_CANDLES), "sort": -1}
        if start_ms is not None:
            params["start"] = start_ms
        if end_ms is not None:
            params["end"] = end_ms
        filas = self._public(f"candles/trade:{tf}:{self._check(symbol)}/hist", **params)
        velas = sorted((candle_from_row(symbol, interval, f) for f in filas), key=lambda v: v.open_time)
        if duracion:
            ahora = _now_ms() + self.clock_offset
            velas = [v for v in velas if v.open_time.timestamp() * 1000 + duracion <= ahora]
        return velas

    def get_candles(self, symbol: str, interval: str = "1m", limit: int = 100, market: str = "spot") -> list[Candle]:
        self._spot(market)
        return self._candles(symbol, interval, limit=limit)[-limit:]

    def get_historical_candles(self, symbol: str, interval: str = "1m", start: datetime | None = None,
                               end: datetime | None = None, limit: int | None = None,
                               market: str = "spot") -> list[Candle]:
        """Pagina hacia atrás desde `end` (máx. 10.000 velas por llamada)."""
        self._spot(market)
        inicio_ms = int(start.timestamp() * 1000) if start else None
        fin_ms = int(end.timestamp() * 1000) if end else None
        todas: dict[datetime, Candle] = {}
        while True:
            lote = self._candles(symbol, interval, limit=MAX_CANDLES - 1, start_ms=inicio_ms, end_ms=fin_ms)
            nuevas = [v for v in lote if v.open_time not in todas]
            for v in nuevas:
                todas[v.open_time] = v
            if not nuevas or len(lote) < MAX_CANDLES - 1 or inicio_ms is None:
                break
            fin_ms = int(min(v.open_time for v in nuevas).timestamp() * 1000) - 1
            if fin_ms < inicio_ms or (limit and len(todas) >= limit):
                break
        velas = sorted(todas.values(), key=lambda v: v.open_time)
        return velas[:limit] if limit else velas

    def latency_symbol(self, symbols: Sequence[str]) -> str | None:
        if not symbols:
            return None
        for preferido in ("tBTCUST", "tBTCUSD", "tETHUST", "tETHUSD"):
            if preferido in symbols:
                return preferido
        return symbols[0]

    # --- ServerClock ----------------------------------------------------

    def server_time_ms(self) -> int:
        return _now_ms() + self.clock_offset_ms()

    def clock_offset_ms(self, sample_s: float = 3.0, symbol: str = "tBTCUST") -> int:
        """Sin endpoint de hora: se estima con las marcas del WebSocket. 0 si no
        llega ninguna en `sample_s`. Síncrono (ver `KrakenExchange.clock_offset_ms`)."""
        medido = _run_async(self._measure_offset(sample_s, symbol))
        if medido is None:
            # Sin muestras en la ventana (exchange callado, límite de conexiones...):
            # devolver 0 falsearía todas las latencias. Se conserva el último.
            log.warning("bitfinex: sin marcas de tiempo en %.1f s para medir el reloj; se conserva el desfase "
                        "anterior (%+d ms)", sample_s, self.clock_offset)
            return self.clock_offset
        self.clock_offset = medido
        return self.clock_offset

    async def _measure_offset(self, sample_s: float, symbol: str) -> int | None:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            inicio = _now_ms()
            async with session.get(f"{REST_PUBLIC}/platform/status", timeout=aiohttp.ClientTimeout(total=10)) as r:
                await r.read()
            rtt = _now_ms() - inicio
            diffs: list[int] = []
            limite = time.monotonic() + sample_s
            async with session.ws_connect(WS_PUBLIC) as ws:
                await ws.send_json({"event": "conf", "flags": FLAG_TIMESTAMP})
                await ws.send_json({"event": "subscribe", "channel": "book", "symbol": symbol, "prec": "P0",
                                    "freq": "F0", "len": "25"})
                while time.monotonic() < limite:
                    try:
                        msg = await ws.receive(timeout=max(0.05, limite - time.monotonic()))
                    except TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    recibido = _now_ms()
                    data = json.loads(msg.data)
                    if isinstance(data, list) and len(data) > 2 and data[1] != "hb" and _is_timestamp(data[-1]):
                        diffs.append(recibido - int(data[-1]))
        if not diffs:
            return None
        return -(min(diffs) - rtt // 2)

    # --- Fees / AssetInfo ------------------------------------------------

    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        """auth/r/summary: maker/taker según el quote sea cripto, stable o fiat."""
        self._spot(market)
        self._load_pairs()
        resumen = self._auth("v2/auth/r/summary")
        maker_row, taker_row = resumen[4][0], resumen[4][1]
        tasas = {}
        for s in symbols:
            _, quote = split_pair(s[1:])
            quote = canonical_currency(quote, self._currency_map)
            tasas[s] = CommissionRates(symbol=s, maker=taker_fee_for(quote, maker_row),
                                       taker=taker_fee_for(quote, taker_row))
        return tasas

    def list_assets(self) -> dict[str, AssetListing]:
        """Los activos de sus pares (Bitfinex no publica sin claves el estado de
        depósitos/retiradas)."""
        return assets_from_rules(self._load_pairs())

    # --- Streams --------------------------------------------------------

    async def stream_order_books(self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event,
                                 market: str = "spot") -> None:
        self._spot(market)
        largo = next((n for n in BOOK_LENGTHS if n >= depth), None)
        if largo is None:
            raise ValueError(f"Bitfinex: como mucho {BOOK_LENGTHS[-1]} niveles por lado.")
        etiqueta = "bitfinex libros"
        canales: dict[int, str] = {}
        libros: dict[str, BookState] = {}

        async def on_message(data: Any) -> None:
            if isinstance(data, dict):
                _control(data, etiqueta)
                if data.get("event") == "subscribed" and data.get("channel") == "book":
                    canales[int(data["chanId"])] = data["symbol"]
                return
            tipo, canal, payload, ts = parse_message(data)
            simbolo = canales.get(canal)
            if simbolo is None or tipo == "hb":
                return
            libro = libros.setdefault(simbolo, BookState(simbolo))
            if tipo == "cs":
                if libro.checksum() != (payload or [0])[0]:
                    raise ConnectionError(f"libro de {simbolo} desincronizado (checksum): se reabre la conexión")
                return
            if tipo == "snapshot":
                libro.apply_snapshot(payload or [])
            else:
                libro.apply_update(payload or [])
            await deliver(on_book, libro.snapshot(depth, _now_ms(), ts))

        mensajes = [{"event": "conf", "flags": FLAG_TIMESTAMP | FLAG_CHECKSUM}] + [
            {"event": "subscribe", "channel": "book", "symbol": s, "prec": "P0", "freq": "F0", "len": str(largo)}
            for s in symbols]

        async def sesion() -> None:
            canales.clear()
            libros.clear()
            await websocket_session(WS_PUBLIC, on_message, stop, subscribe=mensajes,
                                    loads=lambda s: json.loads(s, parse_float=str, parse_int=str), label=etiqueta)

        await run_with_reconnect(sesion, stop, label=etiqueta)

    async def stream_candles(self, subscriptions: Sequence[CandleSubscription], on_candle: OnCandle,
                             stop: asyncio.Event) -> None:
        for sub in subscriptions:
            self._spot(sub.market)
        nativas = [s for s in subscriptions if TIMEFRAMES.get(s.timeframe, ("", 0))[1]]
        resto = [s for s in subscriptions if s not in nativas]
        tareas = []
        if resto:
            tareas.append(PollingCandleStream(self).stream_candles(resto, on_candle, stop))
        if nativas:
            tareas.append(self._ws_candles(nativas, on_candle, stop))
        await asyncio.gather(*tareas)

    async def _ws_candles(self, subs: list[CandleSubscription], on_candle: OnCandle, stop: asyncio.Event) -> None:
        etiqueta = "bitfinex velas"
        por_clave = {f"trade:{TIMEFRAMES[s.timeframe][0]}:{s.symbol}": s for s in subs}
        canales: dict[int, CandleSubscription] = {}
        emisor = ClosedCandleEmitter(lambda sub: self.get_candles(sub.symbol, sub.timeframe, 3), on_candle,
                                     {s: TIMEFRAMES[s.timeframe][1] for s in subs},
                                     clock_offset_ms=self.clock_offset, label=etiqueta)

        async def on_message(data: Any) -> None:
            if isinstance(data, dict):
                _control(data, etiqueta)
                if data.get("event") == "subscribed" and data.get("channel") == "candles":
                    sub = por_clave.get(data.get("key"))
                    if sub is not None:
                        canales[int(data["chanId"])] = sub
                return
            sub = canales.get(int(data[0]))
            payload = data[1] if len(data) > 1 else None
            if sub is None or payload == "hb" or not payload:
                return
            ultima = payload[0] if isinstance(payload[0], list) else payload  # foto: la más reciente primero
            await emisor.period_seen(sub, int(ultima[0]))

        mensajes = [{"event": "subscribe", "channel": "candles", "key": k} for k in por_clave]

        async def sesion() -> None:
            canales.clear()
            await websocket_session(WS_PUBLIC, on_message, stop, subscribe=mensajes, label=etiqueta)

        await asyncio.gather(run_with_reconnect(sesion, stop, label=etiqueta), emisor.run_boundary_timer(stop))
