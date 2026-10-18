"""Reglas de un par tal y como el exchange las aplica a una orden."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

from luarix_exchange.models.book import to_decimal
from luarix_exchange.models.brokers import Market

# En qué activo cobra el exchange la comisión de una orden:
# - "received": en el activo que recibes (Binance spot).
# - "quote": siempre en el activo quote del par (Kraken por defecto).
FeeOn = Literal["received", "quote"]


def _quantize_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


@dataclass(frozen=True)
class SymbolRules:
    """Precisión y mínimos de un par en un mercado concreto.

    Un mismo par tiene reglas DISTINTAS en spot y en futuros (Binance:
    `stepSize`/`tickSize` diferentes), por eso `market` forma parte del
    modelo. Una orden que no las respeta la rechaza el exchange con un error
    de precisión que, visto en un log, no dice nada útil.
    """

    symbol: str          # identificador del par en el exchange
    base: str
    quote: str
    step_size: Decimal   # la cantidad (en base) va en múltiplos de esto
    tick_size: Decimal   # el precio va en múltiplos de esto
    min_qty: Decimal     # cantidad mínima, en base
    min_notional: Decimal  # importe mínimo (precio × cantidad), en quote
    market: Market = Market.SPOT
    fee_on: FeeOn = "received"
    raw: Any = field(default=None, repr=False, compare=False)

    def floor_quantity(self, quantity: Any) -> Decimal:
        """La cantidad redondeada HACIA ABAJO al `step_size`: nunca más de lo
        pedido (redondear hacia arriba puede superar el saldo)."""
        return _quantize_to_step(to_decimal(quantity), self.step_size, ROUND_DOWN)

    def round_price(self, price: Any) -> Decimal:
        """El precio al `tick_size` más cercano."""
        return _quantize_to_step(to_decimal(price), self.tick_size, ROUND_HALF_UP)

    def violations(self, quantity: Any, price: Any) -> list[str]:
        """Por qué el exchange rechazaría esta orden, o `[]` si no hay motivo.

        Comprueba los valores TAL CUAL llegan (no los redondea): sirve para
        validar antes de mandar y dar un motivo legible."""
        qty, px = to_decimal(quantity), to_decimal(price)
        motivos = []
        if qty < self.min_qty:
            motivos.append(f"cantidad {qty} < mínimo {self.min_qty} {self.base}")
        if self.step_size > 0 and qty % self.step_size != 0:
            motivos.append(f"cantidad {qty} no es múltiplo de {self.step_size}")
        if self.tick_size > 0 and px % self.tick_size != 0:
            motivos.append(f"precio {px} no es múltiplo de {self.tick_size}")
        if qty * px < self.min_notional:
            motivos.append(f"importe {qty * px} < mínimo {self.min_notional} {self.quote}")
        return motivos
