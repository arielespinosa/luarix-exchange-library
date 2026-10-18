from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

import pandas as pd

from luarix_exchange.brokers.market_data.binance import WebSocketBinanceMarketDataProvider
from luarix_exchange.brokers.market_data.provider import MarketDataProvider
from luarix_exchange.models.market import Candle

logger = logging.getLogger(__name__)

OnNewCandle = Callable[[Candle], None]


class LiveMarketDataFeed(ABC):
    """
    Uniform live-candle subscription surface, independent of whether the
    underlying broker pushes candles over a websocket (real-time) or has to
    be polled for the latest closed candle (a plain REST-only broker).
    Resolved per `BrokerAccount` by `marketdata.services.get_live_feed_for`
    — a new broker without a dedicated real-time feed registered there gets
    `PollingMarketDataFeed` automatically, built from whatever
    `MarketDataProvider` it already has for historical data. Nothing that
    consumes a feed (the live-trading manager) needs to know which kind it
    got.
    """

    @abstractmethod
    def subscribe(self, symbol: str, timeframe: str, on_new_candle: OnNewCandle) -> None:
        """Registers `on_new_candle` to be called once per newly-closed
        candle observed for (symbol, timeframe). Calling again for the same
        pair replaces the callback rather than adding a second one."""

    @abstractmethod
    def unsubscribe(self, symbol: str, timeframe: str) -> None:
        """Stops watching (symbol, timeframe) — a no-op if not subscribed."""

    @abstractmethod
    def stop(self) -> None:
        """Releases every subscription and any background thread/connection
        this feed opened. Idempotent."""


def _dataframe_to_candles(symbol: str, timeframe: str, df: pd.DataFrame) -> list[Candle]:
    """`MarketDataProvider.get_last_candles` is documented to return either
    `list[Candle]` or a `pd.DataFrame` depending on the concrete provider
    (Binance defaults to a DataFrame unless `format=DataFormat.CANDLE` is
    passed explicitly, but not every provider accepts that kwarg at all —
    `BitunixMarketDataProvider.get_last_candles` has no `format` parameter).
    Normalizing the DataFrame shape here — rather than requiring every
    provider to accept the same kwarg — keeps `PollingMarketDataFeed`
    usable against any provider that satisfies the (loosely-typed) ABC as
    it exists today, present broker or future one alike."""
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=row["date"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            close_time=row.get("close_time"),
            is_closed=True,
        )
        for _, row in df.iterrows()
    ]


class PollingMarketDataFeed(LiveMarketDataFeed):
    """
    Generic fallback feed for any broker with no native real-time push —
    just a plain `MarketDataProvider` (REST). Polls `get_last_candles` on a
    background thread and fires `on_new_candle` the first time the LATEST
    CLOSED candle's `open_time` changes. (The providers already drop the
    still-forming candle; until 2026-10-18 this took the second-to-last one
    and every candle arrived one period late -- see `_poll_one`.) The
    asyncio equivalent is `luarix_exchange.streaming.PollingCandleStream`.
    This is what any broker
    without a dedicated entry in `get_live_feed_for` gets automatically —
    the seam a new broker needs zero extra code to plug into.
    """

    def __init__(self, market_data_provider: MarketDataProvider, poll_seconds: float = 20):
        self._provider = market_data_provider
        self._poll_seconds = poll_seconds
        self._subscriptions: dict[tuple[str, str], OnNewCandle] = {}
        self._last_open_time: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    def subscribe(self, symbol: str, timeframe: str, on_new_candle: OnNewCandle) -> None:
        with self._lock:
            self._subscriptions[(symbol, timeframe)] = on_new_candle
        self._ensure_started()

    def unsubscribe(self, symbol: str, timeframe: str) -> None:
        with self._lock:
            self._subscriptions.pop((symbol, timeframe), None)
            self._last_open_time.pop((symbol, timeframe), None)

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._subscriptions.clear()
            self._last_open_time.clear()

    def _ensure_started(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            with self._lock:
                pairs = list(self._subscriptions.items())
            for (symbol, timeframe), callback in pairs:
                try:
                    self._poll_one(symbol, timeframe, callback)
                except Exception:
                    logger.exception("PollingMarketDataFeed: fallo consultando %s/%s", symbol, timeframe)
            time.sleep(self._poll_seconds)

    def _poll_one(self, symbol: str, timeframe: str, callback: OnNewCandle) -> None:
        raw = self._provider.get_last_candles(symbol, interval=timeframe, limit=3)
        candles = _dataframe_to_candles(symbol, timeframe, raw) if isinstance(raw, pd.DataFrame) else list(raw)
        # La ÚLTIMA vela cerrada. Antes se tomaba `candles[-2]` dando por
        # hecho que la última seguía formándose -- pero los proveedores ya
        # descartan la vela en curso (bugs.md, "limit=N devolvía N-1 velas"),
        # así que `[-2]` era la ANTERIOR a la última cerrada: cada vela
        # llegaba con un periodo entero de retraso a toda estrategia en vivo
        # sobre polling (Bitunix). Verificado contra la API el 2026-10-18.
        closed = [c for c in candles if c.is_closed]
        if not closed:
            return
        latest_closed = closed[-1]
        key = (symbol, timeframe)
        if self._last_open_time.get(key) == latest_closed.open_time:
            return
        self._last_open_time[key] = latest_closed.open_time
        callback(latest_closed)


class WebSocketMarketDataFeed(LiveMarketDataFeed):
    """
    Real-time push via Binance's kline websocket
    (`WebSocketBinanceMarketDataProvider` — already written, previously
    unwired: see `app/brokers/market_data/binance.py`). One underlying
    provider instance per (symbol, timeframe) subscribed, since that's how
    `python-binance`'s `ThreadedWebsocketManager` socket is scoped.
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._providers: dict[tuple[str, str], WebSocketBinanceMarketDataProvider] = {}

    def subscribe(self, symbol: str, timeframe: str, on_new_candle: OnNewCandle) -> None:
        key = (symbol, timeframe)
        if key in self._providers:
            return
        provider = WebSocketBinanceMarketDataProvider(
            api_key=self._api_key,
            api_secret=self._api_secret,
            symbol=symbol,
            interval=timeframe,
            testnet=self._testnet,
        )
        provider.on_kline = _kline_handler(symbol, timeframe, on_new_candle)
        self._providers[key] = provider
        provider.start()

    def unsubscribe(self, symbol: str, timeframe: str) -> None:
        provider = self._providers.pop((symbol, timeframe), None)
        if provider:
            provider.stop()

    def stop(self) -> None:
        for provider in self._providers.values():
            provider.stop()
        self._providers.clear()


def _kline_handler(symbol: str, timeframe: str, on_new_candle: OnNewCandle) -> Callable[[dict], None]:
    def handle(msg: dict) -> None:
        candle = Candle.from_binance(msg)
        if candle.is_closed:  # ignore the still-forming candle each kline tick also carries
            on_new_candle(candle)

    return handle
