"""Libro de órdenes normalizado, el mismo venga del exchange que venga.

Cada exchange devuelve su libro con una forma distinta -- Binance listas
`[precio, cantidad]` de texto, Bitunix spot diccionarios
`{"price", "volume"}`, Kraken/Bitfinex mensajes por nivel -- y en un orden
que no siempre es el que se espera. Aquí siempre:

- precios y cantidades en `Decimal` (sin el error binario de `float`: un
  libro se usa para calcular importes);
- `bids` del mejor (más caro) al peor, `asks` del mejor (más barato) al peor;
- el crudo del exchange entero en `.raw`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from luarix_exchange.models.brokers import Market

# Un nivel del libro: (precio, cantidad en activo base).
Level = tuple[Decimal, Decimal]


def to_decimal(value: Any) -> Decimal:
    """`Decimal` desde texto, entero, float o Decimal. El float pasa por
    `str()` para no heredar su representación binaria (`Decimal(0.1)` no es
    `Decimal("0.1")`)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass
class OrderBook:
    """Foto del libro de un par: los mejores niveles de cada lado.

    `received_ms` es la hora LOCAL de recepción. `event_ms` es la hora DEL
    EXCHANGE del mensaje, cuando la manda (Kraken, Bitfinex, Bitunix, Binance
    futuros; no el libro parcial de Binance spot): con ella se mide la latencia
    (`received_ms + desfase_de_reloj - event_ms`). `update_id` es el número de
    secuencia del exchange cuando lo hay (Binance `lastUpdateId`), `None` si no.
    """

    symbol: str
    market: Market
    bids: list[Level]
    asks: list[Level]
    update_id: int | None = None
    received_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    raw: Any = field(default=None, repr=False, compare=False)
    event_ms: int | None = None

    @classmethod
    def from_levels(
        cls,
        symbol: str,
        market: Market | str,
        bids: Iterable[tuple[Any, Any]],
        asks: Iterable[tuple[Any, Any]],
        *,
        depth: int | None = None,
        update_id: int | None = None,
        received_ms: int | None = None,
        raw: Any = None,
        event_ms: int | None = None,
    ) -> OrderBook:
        """Construye el libro desde pares (precio, cantidad) en cualquier
        formato numérico, ordenándolo y recortándolo a `depth` niveles por
        lado. Descarta niveles con cantidad cero: algunos exchanges los mandan
        para indicar "este nivel desapareció"."""
        bids_dec = sorted(((to_decimal(p), to_decimal(q)) for p, q in bids), key=lambda lvl: lvl[0], reverse=True)
        asks_dec = sorted(((to_decimal(p), to_decimal(q)) for p, q in asks), key=lambda lvl: lvl[0])
        bids_dec = [lvl for lvl in bids_dec if lvl[1] > 0]
        asks_dec = [lvl for lvl in asks_dec if lvl[1] > 0]
        if depth is not None:
            bids_dec, asks_dec = bids_dec[:depth], asks_dec[:depth]
        return cls(
            symbol=symbol,
            market=Market(market),
            bids=bids_dec,
            asks=asks_dec,
            update_id=update_id,
            received_ms=received_ms if received_ms is not None else int(time.time() * 1000),
            raw=raw,
            event_ms=event_ms,
        )

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        """`best_ask - best_bid`, o `None` si falta algún lado."""
        if not self.bids or not self.asks:
            return None
        return self.asks[0][0] - self.bids[0][0]

    @property
    def mid(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.asks[0][0] + self.bids[0][0]) / 2

    def same_top(self, other: OrderBook | None, levels: int = 10) -> bool:
        """¿Los `levels` mejores niveles de cada lado son los mismos que en
        `other`? Sirve para no recalcular cuando solo cambian niveles
        profundos (Kraken y Bitfinex mandan un mensaje por cada nivel que
        cambia, la mayoría lejos del precio)."""
        return (
            other is not None
            and self.bids[:levels] == other.bids[:levels]
            and self.asks[:levels] == other.asks[:levels]
        )
