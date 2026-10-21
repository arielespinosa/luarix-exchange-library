"""Fuentes de datos de mercado que no son exchanges.

Un exchange sabe de SUS pares; la capitalización de mercado (precio ×
oferta circulante), la dominancia o el sentimiento del mercado los calculan
agregadores. Se modelan aparte de `Exchange` para no fingir que son uno: no
tienen pares, órdenes ni mercados spot/futuros.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from luarix_exchange.models.global_market import AssetMarketCap, FearGreed, GlobalMarket


class MarketSource(ABC):
    """Identidad común: `name` (clave en la fábrica) y `label` (para mostrar)."""

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._http = None

    def _get(self, url: str, **params: Any) -> Any:
        if self._http is None:
            import requests

            self._http = requests.Session()
        response = self._http.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


class GlobalMarketSource(MarketSource):
    """Capitalización total, dominancia y el ranking de activos por capitalización."""

    @abstractmethod
    def get_global(self) -> GlobalMarket:
        """El mercado entero, en USD."""

    @abstractmethod
    def get_top_assets(self, limit: int = 100) -> list[AssetMarketCap]:
        """Los `limit` activos con mayor capitalización, en orden de ranking."""


class SentimentSource(MarketSource):
    """Un índice de sentimiento del mercado."""

    @abstractmethod
    def get_fear_greed(self, days: int = 30) -> list[FearGreed]:
        """Las últimas `days` lecturas diarias, de la más reciente a la más antigua."""
