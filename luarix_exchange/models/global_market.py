"""Datos de mercado que no son de un exchange: capitalización total,
dominancia, ranking por capitalización y el índice Fear & Greed. Los
publican agregadores (CoinGecko, alternative.me), no los exchanges."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GlobalMarket:
    """El mercado cripto entero, en USD."""

    total_market_cap: float
    total_volume: float
    market_cap_change_pct_24h: float
    # Cuota de la capitalización total por activo, en %: {"BTC": 57.1, "ETH": 11.8, ...}
    dominance: dict[str, float] = field(default_factory=dict)
    active_assets: int | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class AssetMarketCap:
    """Un activo del ranking por capitalización."""

    id: str
    symbol: str          # en mayúsculas ("BTC")
    name: str
    rank: int | None
    price: float
    change_pct_24h: float | None
    change_pct_7d: float | None
    market_cap: float
    volume: float
    image: str | None = None
    # Precios horarios de los últimos 7 días, del más antiguo al más nuevo.
    sparkline_7d: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class FearGreed:
    """Una lectura del índice Fear & Greed (0 = miedo extremo, 100 = codicia extrema)."""

    value: int
    classification: str
    time: datetime
