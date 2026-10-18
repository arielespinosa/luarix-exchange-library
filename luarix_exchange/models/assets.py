"""Activos de un exchange y sus redes de depósito/retirada.

Los usa arbitraje para saber si dos exchanges hablan de la misma moneda
(contrato), si se puede mover (depósitos/retiradas abiertos) y cuánto cuesta
reequilibrar (comisión y mínimo de retirada).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AssetNetworkInfo:
    """Una red por la que se deposita o retira un activo."""

    network: str
    contract_address: str = ""
    deposit_enabled: bool | None = None
    withdraw_enabled: bool | None = None
    withdraw_fee: Decimal | None = None
    min_withdraw: Decimal | None = None


@dataclass(frozen=True)
class AssetListing:
    """Un activo en un exchange. `asset` es el nombre COMÚN (el de las reglas
    de los pares: BTC, USDT...); `symbol`, el que usa la API del exchange
    (Kraken `XXBT`, Bitfinex `UST`). Depósitos/retiradas `None` = el exchange
    no lo publica sin claves; con varias redes, abierto si lo está en alguna."""

    asset: str
    symbol: str
    deposit_enabled: bool | None = None
    withdraw_enabled: bool | None = None
    name: str = ""
    networks: tuple[AssetNetworkInfo, ...] = ()
