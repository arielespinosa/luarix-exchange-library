"""Comisión real de la cuenta en un par."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CommissionRates:
    """Comisión maker/taker de la cuenta en un par, como fracción
    (`Decimal("0.001")` = 0,1 %).

    `discount` es el multiplicador de un descuento activo (0.75 si Binance
    cobra en BNB con un 25 % menos), `None` si no hay.
    """

    symbol: str
    maker: Decimal
    taker: Decimal
    discount: Decimal | None = None

    @property
    def effective_taker(self) -> Decimal:
        return self.taker * self.discount if self.discount is not None else self.taker

    @property
    def effective_maker(self) -> Decimal:
        return self.maker * self.discount if self.discount is not None else self.maker
