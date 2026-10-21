"""CoinGecko: capitalización total, dominancia y ranking por capitalización.

API pública sin clave (`api.coingecko.com/api/v3`), con un límite de unas
pocas decenas de peticiones por minuto: quien la use debe cachear (el
backend lo hace por minutos; estos datos no cambian más rápido).
"""

from __future__ import annotations

from datetime import UTC, datetime

from luarix_exchange.models.global_market import AssetMarketCap, GlobalMarket
from luarix_exchange.sources.base import GlobalMarketSource

API = "https://api.coingecko.com/api/v3"
MAX_PER_PAGE = 250


def global_from_coingecko(payload: dict) -> GlobalMarket:
    """`/global` → `GlobalMarket` (solo los valores en USD)."""
    data = payload.get("data", payload)
    updated = data.get("updated_at")
    return GlobalMarket(
        total_market_cap=float(data.get("total_market_cap", {}).get("usd") or 0),
        total_volume=float(data.get("total_volume", {}).get("usd") or 0),
        market_cap_change_pct_24h=float(data.get("market_cap_change_percentage_24h_usd") or 0),
        dominance={k.upper(): float(v) for k, v in (data.get("market_cap_percentage") or {}).items()},
        active_assets=data.get("active_cryptocurrencies"),
        updated_at=datetime.fromtimestamp(updated, tz=UTC) if updated else None,
    )


def asset_from_coingecko(row: dict) -> AssetMarketCap:
    """Una fila de `/coins/markets` (con `sparkline=true` y `price_change_percentage=24h,7d`)."""

    def opcional(key: str) -> float | None:
        value = row.get(key)
        return float(value) if value is not None else None

    return AssetMarketCap(
        id=row["id"],
        symbol=str(row.get("symbol", "")).upper(),
        name=row.get("name", ""),
        rank=row.get("market_cap_rank"),
        price=float(row.get("current_price") or 0),
        change_pct_24h=opcional("price_change_percentage_24h_in_currency") or opcional("price_change_percentage_24h"),
        change_pct_7d=opcional("price_change_percentage_7d_in_currency"),
        market_cap=float(row.get("market_cap") or 0),
        volume=float(row.get("total_volume") or 0),
        image=row.get("image"),
        sparkline_7d=[float(p) for p in (row.get("sparkline_in_7d") or {}).get("price", []) if p is not None],
    )


class CoinGeckoSource(GlobalMarketSource):
    name = "coingecko"
    label = "CoinGecko"

    def get_global(self) -> GlobalMarket:
        return global_from_coingecko(self._get(f"{API}/global"))

    def get_top_assets(self, limit: int = 100) -> list[AssetMarketCap]:
        rows = self._get(
            f"{API}/coins/markets",
            vs_currency="usd",
            order="market_cap_desc",
            per_page=min(limit, MAX_PER_PAGE),
            page=1,
            sparkline="true",
            price_change_percentage="24h,7d",
        )
        return [asset_from_coingecko(row) for row in rows][:limit]
