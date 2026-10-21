"""Fuentes de datos de mercado que no son exchanges (ver `base.py`).

    from luarix_exchange.sources import global_market_source, sentiment_source
    global_market_source().get_global()          # CoinGecko
    sentiment_source().get_fear_greed(days=30)   # alternative.me
"""

from __future__ import annotations

from luarix_exchange.sources.alternative_me import AlternativeMeSource
from luarix_exchange.sources.base import GlobalMarketSource, MarketSource, SentimentSource
from luarix_exchange.sources.coingecko import CoinGeckoSource

GLOBAL_MARKET_SOURCES: dict[str, type[GlobalMarketSource]] = {CoinGeckoSource.name: CoinGeckoSource}
SENTIMENT_SOURCES: dict[str, type[SentimentSource]] = {AlternativeMeSource.name: AlternativeMeSource}


def global_market_source(name: str = CoinGeckoSource.name, **options) -> GlobalMarketSource:
    try:
        return GLOBAL_MARKET_SOURCES[name](**options)
    except KeyError:
        raise ValueError(f"Fuente de mercado desconocida: {name!r}. Hay: {sorted(GLOBAL_MARKET_SOURCES)}.") from None


def sentiment_source(name: str = AlternativeMeSource.name, **options) -> SentimentSource:
    try:
        return SENTIMENT_SOURCES[name](**options)
    except KeyError:
        raise ValueError(f"Fuente de sentimiento desconocida: {name!r}. Hay: {sorted(SENTIMENT_SOURCES)}.") from None


__all__ = [
    "AlternativeMeSource",
    "CoinGeckoSource",
    "GLOBAL_MARKET_SOURCES",
    "GlobalMarketSource",
    "MarketSource",
    "SENTIMENT_SOURCES",
    "SentimentSource",
    "global_market_source",
    "sentiment_source",
]
