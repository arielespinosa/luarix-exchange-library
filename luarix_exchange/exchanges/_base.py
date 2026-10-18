"""Base de los conectores spot portados de luarix-arbitrage-web-server (Bybit,
Gate, HTX, KuCoin, MEXC, OKX): lo que todos hacen igual.

Cada conector concreto implementa: `_fetch_rules()`, `server_time_ms()`,
`_book_session(...)` (UNA conexión del libro por WebSocket) y
`get_commissions(...)`; opcionalmente `list_assets()` y
`measure_order_roundtrip_ms(...)`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any, ClassVar

from luarix_exchange.capabilities import (
    AssetInfo,
    Exchange,
    Fees,
    OrderBookStream,
    ServerClock,
    SymbolRulesSource,
    assets_from_rules,
)
from luarix_exchange.capabilities.streams import OnBook
from luarix_exchange.exchanges._common import RestClient
from luarix_exchange.models.assets import AssetListing
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming.reconnect import run_with_reconnect


class SpotConnector(Exchange, SymbolRulesSource, ServerClock, OrderBookStream, Fees, AssetInfo):
    markets = frozenset({Market.SPOT})
    #: profundidades de libro que admite el canal del exchange
    DEPTHS: ClassVar[tuple[int, ...]] = ()
    DEFAULT_DEPTH: ClassVar[int] = 20
    #: pares preferidos para medir latencia (los más activos)
    LATENCY_PREFERRED: ClassVar[tuple[str, ...]] = ()

    def __init__(self, api_key: str = "", api_secret: str = "", passphrase: str = "",
                 depth_levels: int | None = None, timeout: float = 20.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.depth_levels = int(depth_levels if depth_levels is not None else self.DEFAULT_DEPTH)
        self._validate_depth(self.depth_levels)
        self.rest = RestClient(timeout)
        self._rules: dict[str, SymbolRules] | None = None
        self._lock = threading.Lock()

    def _validate_depth(self, depth: int) -> None:
        if self.DEPTHS and depth not in self.DEPTHS:
            raise ValueError(f"{self.name}.depth_levels debe ser uno de {self.DEPTHS}")

    @classmethod
    def create(cls, credentials: Any = None, **options: Any):
        """Sin `credentials` intenta las del entorno (`<NOMBRE>_API_KEY`...); los
        datos públicos no las necesitan."""
        if credentials is None:
            from luarix_exchange.brokers.credentials import BrokerCredentials

            credentials = BrokerCredentials.from_env(cls.name.upper())
        return cls(api_key=getattr(credentials, "api_key", "") or "",
                   api_secret=getattr(credentials, "api_secret", "") or "",
                   passphrase=getattr(credentials, "passphrase", "") or "", **options)

    # --- comunes ----------------------------------------------------------

    @staticmethod
    def _spot(market: str) -> None:
        if Market(market) is not Market.SPOT:
            raise ValueError("Este conector solo cubre spot.")

    def _require_keys(self, what: str, passphrase: bool = False) -> None:
        if not (self.api_key and self.api_secret) or (passphrase and not self.passphrase):
            extra = " y la passphrase" if passphrase else ""
            raise ValueError(f"{self.display_name()}: {what} necesita claves{extra} "
                             f"({self.name.upper()}_API_KEY / _API_SECRET).")

    def _fetch_rules(self) -> dict[str, SymbolRules]:
        raise NotImplementedError

    def get_symbol_rules(self, market: str = "spot") -> dict[str, SymbolRules]:
        """Reglas de todos los pares operables (se leen una vez; `refresh_rules`
        para releerlas)."""
        self._spot(market)
        with self._lock:
            if self._rules is None:
                self._rules = self._fetch_rules()
            return dict(self._rules)

    def refresh_rules(self) -> dict[str, SymbolRules]:
        with self._lock:
            self._rules = None
        return self.get_symbol_rules()

    def list_assets(self) -> dict[str, AssetListing]:
        """Por defecto, los activos de sus pares (estado desconocido)."""
        return assets_from_rules(self.get_symbol_rules())

    def latency_symbol(self, symbols: Sequence[str]) -> str | None:
        return next((s for s in self.LATENCY_PREFERRED if s in symbols), symbols[0] if symbols else None)

    # --- libro por WebSocket ---------------------------------------------

    async def stream_order_books(self, symbols: Sequence[str], depth: int, on_book: OnBook, stop: asyncio.Event,
                                 market: str = "spot") -> None:
        """El canal del exchange se suscribe con `self.depth_levels` (lo que
        admita), y cada libro se entrega recortado a `depth`."""
        self._spot(market)
        if depth > self.depth_levels:
            raise ValueError(f"{self.display_name()}: se pidieron {depth} niveles y el conector está configurado con "
                             f"{self.depth_levels} (depth_levels).")
        simbolos = list(symbols)

        async def sesion() -> None:
            await self._book_session(simbolos, on_book, stop, depth)

        await run_with_reconnect(sesion, stop, label=f"{self.name} libros")

    async def _book_session(self, symbols: list[str], on_book: OnBook, stop: asyncio.Event, depth: int) -> None:
        raise NotImplementedError
