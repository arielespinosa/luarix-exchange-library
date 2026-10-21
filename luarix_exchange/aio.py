"""Fachada ASÍNCRONA de un exchange, con la forma del contrato de
luarix-arbitrage-web-server (`engine/exchanges/base.py::Exchange`).

La librería es híbrida (REST síncrono, streams asíncronos). Arbitraje vive en
`asyncio` y habla con sus exchanges así:

    rules = await ex.fetch_rules()
    offset = await ex.clock_offset_ms()
    await ex.stream_books(symbols, on_book, stop, latency_symbol=..., on_event_time=...)
    rates = await ex.fetch_commissions(symbols, credentials)
    assets = await ex.fetch_assets()

`AsyncExchange` da exactamente eso sobre cualquier exchange de la librería
(Binance, Bitunix, Kraken, Bitfinex...), con los MISMOS modelos que arbitraje
(`OrderBook`, `SymbolRules`, `CommissionRates`, `AssetListing` tienen sus
campos, más algunos). El REST va en `asyncio.to_thread` para no bloquear el
bucle; los streams son nativos (WebSocket) o polling según el exchange.

Ver `docs/integracion/arbitraje.md` para el adaptador de la Fase 7.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from luarix_exchange.capabilities import Capability, assets_from_rules
from luarix_exchange.models.assets import AssetListing
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming import order_book_stream
from luarix_exchange.streaming._util import deliver

OnBook = Callable[[OrderBook], Awaitable[None] | None]
OnEventTime = Callable[[int], None]


class AsyncExchange:
    """Envuelve un exchange de la librería con la interfaz async de arbitraje."""

    def __init__(self, exchange: Any, depth_levels: int | None = None, market: str = "spot"):
        self.exchange = exchange
        self.market = market
        self.depth_levels = depth_levels or getattr(exchange, "depth_levels", None) or 20

    @property
    def name(self) -> str:
        return type(self.exchange).name

    @property
    def label(self) -> str:
        return type(self.exchange).display_name()

    def supports(self, capability: Capability | str) -> bool:
        return type(self.exchange).supports(capability, self.market)

    async def fetch_rules(self) -> dict[str, SymbolRules]:
        return await asyncio.to_thread(self.exchange.get_symbol_rules, self.market)

    async def clock_offset_ms(self) -> int:
        """hora_exchange ≈ hora_local + offset. 0 si el exchange no da hora."""
        if not self.supports(Capability.SERVER_CLOCK):
            return 0
        return await asyncio.to_thread(self.exchange.clock_offset_ms)

    async def fetch_commissions(self, symbols: list[str], credentials: Any = None) -> dict[str, CommissionRates]:
        """`credentials` (opcional, `api_key`/`api_secret`): si se pasan, se usan
        para esta consulta sin tocar las del exchange construido."""
        if not self.supports(Capability.FEES):
            raise NotImplementedError(f"{self.label} no permite consultar la comisión de la cuenta")
        ex = self.exchange
        if credentials is not None:
            ex = await asyncio.to_thread(self._with_credentials, credentials)
        return await asyncio.to_thread(ex.get_commissions, list(symbols), self.market)

    def _with_credentials(self, credentials: Any) -> Any:
        """Otra instancia del mismo exchange con esas claves. Acepta cualquier
        objeto con `api_key`/`api_secret`(/`passphrase`), como el `Credentials`
        de arbitraje, y conserva la red del original (Binance testnet/mainnet)."""
        from luarix_exchange.brokers.credentials import BrokerCredentials

        claves = BrokerCredentials(api_key=credentials.api_key, api_secret=credentials.api_secret,
                                   passphrase=getattr(credentials, "passphrase", None) or "")
        cls = type(self.exchange)
        opciones = {}
        if "testnet" in inspect.signature(cls.create).parameters:
            opciones["testnet"] = bool(getattr(self.exchange, "testnet", False))
        return cls.create(credentials=claves, **opciones)

    async def fetch_assets(self) -> dict[str, AssetListing]:
        if self.supports(Capability.ASSET_INFO):
            return await asyncio.to_thread(self.exchange.list_assets)
        return assets_from_rules(await self.fetch_rules())

    @property
    def has_sandbox(self) -> bool:
        """¿Tiene entorno de pruebas para medir órdenes? (el `sandbox_env_prefix`
        de arbitraje: Binance testnet, Bybit testnet, OKX demo)."""
        return self.supports(Capability.ORDER_ROUNDTRIP)

    async def measure_order_roundtrip_ms(self, credentials: Any, samples: int = 8) -> list[int]:
        """Ida y vuelta de `samples` órdenes de prueba en el entorno de pruebas.
        `credentials`: claves de ESE entorno."""
        if not self.has_sandbox:
            raise NotImplementedError(f"{self.label} no tiene entorno de pruebas para medir órdenes")
        return await asyncio.to_thread(self.exchange.measure_order_roundtrip_ms, credentials, samples)

    def latency_symbol(self, symbols: Sequence[str]) -> str | None:
        propio = getattr(self.exchange, "latency_symbol", None)
        if callable(propio):
            return propio(symbols)
        return symbols[0] if symbols else None

    async def stream_books(
        self,
        symbols: list[str],
        on_book: OnBook,
        stop: asyncio.Event,
        latency_symbol: str | None = None,
        on_event_time: OnEventTime | None = None,
    ) -> None:
        """Libros de `symbols` hasta `stop`. Con `latency_symbol`, pasa a
        `on_event_time` la hora del exchange (ms) de cada mensaje de ese par:
        de `OrderBook.event_ms`, o del flujo propio que el exchange use (Binance
        spot: miniTicker)."""
        stream = order_book_stream(self.exchange, market=self.market)
        acepta_latencia = "on_event_time" in inspect.signature(stream.stream_order_books).parameters

        async def entregar(libro: OrderBook) -> None:
            if (not acepta_latencia and on_event_time is not None and libro.symbol == latency_symbol
                    and libro.event_ms):
                on_event_time(libro.event_ms)
            await deliver(on_book, libro)

        if acepta_latencia:
            await stream.stream_order_books(symbols, self.depth_levels, entregar, stop, self.market,
                                            latency_symbol=latency_symbol, on_event_time=on_event_time)
        else:
            await stream.stream_order_books(symbols, self.depth_levels, entregar, stop, self.market)
