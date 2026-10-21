"""Contrato por capacidades (Fase 3). Ver `docs/plans/fase3_contrato_capacidades.md`.

Importar este paquete registra TODAS las capacidades, que es lo que necesita
`Exchange.capabilities()` para deducirlas de la jerarquía de clases.
"""

from luarix_exchange.capabilities.base import CAPABILITY_ABCS, Capability, CapabilityInterface, Exchange
from luarix_exchange.capabilities.market_data import (
    AssetInfo,
    FundingRates,
    MarketData,
    ServerClock,
    SymbolRulesSource,
    TickerStats,
    assets_from_rules,
)
from luarix_exchange.capabilities.streams import (
    CandleStream,
    CandleSubscription,
    OrderBookStream,
    OrderUpdateStream,
)
from luarix_exchange.capabilities.trading import (
    Account,
    AccountSummarySource,
    Derivatives,
    Fees,
    FundingHistory,
    IncomeHistory,
    NativeOco,
    OrderRoundtripProbe,
    Trading,
)

__all__ = [
    "CAPABILITY_ABCS",
    "Account",
    "AccountSummarySource",
    "AssetInfo",
    "CandleStream",
    "CandleSubscription",
    "Capability",
    "CapabilityInterface",
    "Derivatives",
    "Exchange",
    "Fees",
    "FundingHistory",
    "FundingRates",
    "IncomeHistory",
    "MarketData",
    "NativeOco",
    "OrderBookStream",
    "OrderRoundtripProbe",
    "OrderUpdateStream",
    "ServerClock",
    "SymbolRulesSource",
    "TickerStats",
    "Trading",
    "assets_from_rules",
]
