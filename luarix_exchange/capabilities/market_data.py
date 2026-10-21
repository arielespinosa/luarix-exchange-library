"""Capacidades de datos públicos de mercado: velas, precio, libro, pares,
reglas de cada par y hora del servidor."""

from __future__ import annotations

import time
from abc import abstractmethod
from datetime import datetime

from luarix_exchange.capabilities.base import Capability, CapabilityInterface
from luarix_exchange.models.assets import AssetListing
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.market import Candle, FundingRate, Ticker24h
from luarix_exchange.models.rules import SymbolRules


class MarketData(CapabilityInterface):
    """Velas, último precio, libro de órdenes y lista de pares (REST,
    síncrono). Todo acepta `market="spot"|"futures"`."""

    capability = Capability.MARKET_DATA

    @abstractmethod
    def get_candles(self, symbol: str, interval: str = "1m", limit: int = 100, market: str = "spot") -> list[Candle]:
        """Las `limit` velas CERRADAS más recientes, de la más antigua a la más nueva."""

    def get_last_candle(self, symbol: str, interval: str = "1m", market: str = "spot") -> Candle | None:
        """La última vela cerrada, o `None` si el exchange no devolvió ninguna."""
        candles = self.get_candles(symbol, interval, limit=1, market=market)
        return candles[-1] if candles else None

    @abstractmethod
    def get_historical_candles(
        self,
        symbol: str,
        interval: str = "1m",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        market: str = "spot",
    ) -> list[Candle]:
        """Velas de un rango de fechas, paginando si hace falta."""

    @abstractmethod
    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        """El último precio negociado del par."""

    @abstractmethod
    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> OrderBook:
        """El libro normalizado: `Decimal`, mejores niveles primero, crudo en `.raw`."""

    @abstractmethod
    def list_symbols(self, market: str = "spot") -> list[str]:
        """Los pares operables en ese mercado, con el identificador que espera el exchange."""


class TickerStats(CapabilityInterface):
    """Las estadísticas de 24 horas de TODOS los pares de un mercado en una
    sola llamada: lo que necesita un resumen de mercado o un screener, sin
    pedir par a par."""

    capability = Capability.TICKER_STATS

    @abstractmethod
    def get_tickers_24h(self, market: str = "spot") -> list[Ticker24h]:
        """Un `Ticker24h` por par operable del mercado (los que el exchange
        tenga sin estadística se omiten)."""


class FundingRates(CapabilityInterface):
    """El funding actual de todos los perpetuos."""

    capability = Capability.FUNDING_RATES

    @abstractmethod
    def get_funding_rates(self) -> list[FundingRate]:
        """Un `FundingRate` por perpetuo, con `rate` como fracción."""


class SymbolRulesSource(CapabilityInterface):
    """Precisión y mínimos de cada par (step, tick, cantidad e importe mínimos)."""

    capability = Capability.SYMBOL_RULES

    @abstractmethod
    def get_symbol_rules(self, market: str = "spot") -> dict[str, SymbolRules]:
        """Reglas de TODOS los pares operables del mercado, por símbolo."""

    def get_symbol_rule(self, symbol: str, market: str = "spot") -> SymbolRules:
        """Las reglas de un par. Levanta `ValueError` si el par no existe en
        ese mercado (no devuelve reglas inventadas)."""
        reglas = self.get_symbol_rules(market)
        try:
            return reglas[symbol]
        except KeyError:
            raise ValueError(f"{symbol!r} no existe (o no se opera) en el mercado {market}.") from None


class ServerClock(CapabilityInterface):
    """Hora del exchange, para medir latencias con sus marcas de tiempo y
    firmar peticiones que exigen un reloj sincronizado."""

    capability = Capability.SERVER_CLOCK

    @abstractmethod
    def server_time_ms(self) -> int:
        """La hora del exchange, en milisegundos desde epoch."""

    def clock_offset_ms(self) -> int:
        """hora_exchange ≈ hora_local + offset. Se toma el punto medio de la
        petición para descontar la mitad del viaje de ida y vuelta."""
        antes = time.time() * 1000
        servidor = self.server_time_ms()
        despues = time.time() * 1000
        return int(servidor - (antes + despues) / 2)


class AssetInfo(CapabilityInterface):
    """Los activos del exchange: nombre común, nombre en su API, si se puede
    depositar/retirar y por qué redes (lo que el exchange publique sin claves)."""

    capability = Capability.ASSET_INFO

    @abstractmethod
    def list_assets(self) -> dict[str, AssetListing]:
        """Activos por nombre COMÚN."""


def assets_from_rules(rules: dict[str, SymbolRules]) -> dict[str, AssetListing]:
    """Activos deducidos de los pares (estado de depósitos/retiradas
    desconocido), para un exchange que no publica más."""
    nombres = {r.base for r in rules.values()} | {r.quote for r in rules.values()}
    return {n: AssetListing(n, n) for n in sorted(nombres)}
