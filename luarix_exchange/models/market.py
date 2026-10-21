"""Vela OHLCV, el modelo de datos de mercado que devuelven todos los brokers.

`Trend`, `Fractal`, `MarketSnapshot` y `Market`, que compartian fichero con
`Candle` en luarix-trading-framework, son modelos de estrategia y se quedaron
alla.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pandas solo hace falta en `from_dataframe_row`
    import pandas as pd


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: datetime | None = None
    is_closed: bool = True

    @staticmethod
    def from_binance(msg: dict) -> Candle:
        k = msg["k"]

        return Candle(
            symbol=k["s"],
            timeframe=k["i"],
            open_time=datetime.fromtimestamp(k["t"] / 1000, tz=UTC),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            close_time=datetime.fromtimestamp(k["T"] / 1000, tz=UTC),
            is_closed=k["x"],
        )
    
    @classmethod
    def from_close(
        cls,
        close: float,
        symbol: str = "BTCUSDT",
        timeframe: str = "1m",
    ) -> "Candle":
        now = datetime.now(timezone.utc)
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open_time=now,
            close_time=now,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=0.0,
        )

    @classmethod
    def from_dataframe_row(cls, symbol:str, timeframe:str, row: pd.Series) -> Candle:
        import pandas as pd

        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open_time=pd.to_datetime(row["date"]),
            close_time=pd.to_datetime(row["close_time"]) if "close_time" in row else None,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            is_closed=row.get("is_closed", True),
        )


@dataclass(frozen=True)
class Ticker24h:
    """Lo que hizo un par en las últimas 24 horas, normalizado entre
    exchanges: precios en la moneda cotizada, `change_pct` en puntos
    porcentuales (2.5 = +2.5 %) y el volumen en base y en quote.

    `volume_quote` es el que permite comparar pares entre sí (cuánto dinero
    se movió); cuando el exchange no lo publica se estima con el precio."""

    symbol: str       # identificador del par en el exchange
    base: str         # activo base común ("BTC", nunca "XBT")
    quote: str        # moneda cotizada común ("USDT")
    market: str       # "spot" | "futures"
    last: float
    open: float
    high: float
    low: float
    change_pct: float
    volume_base: float
    volume_quote: float
    time: datetime | None = None


@dataclass(frozen=True)
class FundingRate:
    """El funding actual de un perpetuo. `rate` es una FRACCIÓN por
    intervalo (0.0001 = 0.01 %), sea cual sea la unidad del exchange."""

    symbol: str
    base: str
    quote: str
    rate: float
    mark_price: float | None = None
    interval_hours: float = 8.0
    next_funding_time: datetime | None = None


def pct_change(open_: float, last: float) -> float:
    """Cambio porcentual entre apertura y último precio; 0 sin apertura."""
    return (last - open_) / open_ * 100 if open_ else 0.0


def ms_to_datetime(value: object) -> datetime | None:
    """Milisegundos epoch (número o texto) → `datetime` UTC; `None` si no hay."""
    try:
        ms = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms > 0 else None
