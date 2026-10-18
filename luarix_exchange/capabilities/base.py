"""Qué sabe hacer un exchange, declarado por herencia y consultable sin
instanciarlo.

Cada capacidad es una ABC pequeña (`MarketData`, `Account`, `Trading`...)
que declara su `capability`. Un exchange hereda solo las que implementa, y
`Exchange.capabilities()` las deduce de su jerarquía de clases:

    class KrakenExchange(Exchange, MarketData, SymbolRulesSource): ...
    KrakenExchange.capabilities()   # {MARKET_DATA, SYMBOL_RULES}

Es de CLASE a propósito: la factory responde "¿Bitunix tiene OCO?" sin
credenciales ni conexión. Un método no soportado sigue LEVANTANDO con un
mensaje claro cuando se llama (regla del repo: nunca devolver vacío);
`supports()` sirve para decidir antes, no para sustituir eso.
"""

from __future__ import annotations

from abc import ABC
from enum import Enum
from typing import Any, ClassVar

from luarix_exchange.models.brokers import Market


class Capability(str, Enum):
    MARKET_DATA = "market_data"
    SYMBOL_RULES = "symbol_rules"
    ACCOUNT = "account"
    TRADING = "trading"
    DERIVATIVES = "derivatives"
    NATIVE_OCO = "native_oco"
    FEES = "fees"
    SERVER_CLOCK = "server_clock"
    CANDLE_STREAM = "candle_stream"
    ORDER_BOOK_STREAM = "order_book_stream"
    ORDER_UPDATE_STREAM = "order_update_stream"


# Capacidad -> la ABC que la define. Se rellena solo, al declararse cada ABC.
CAPABILITY_ABCS: dict[Capability, type] = {}


class CapabilityInterface(ABC):
    """Base de las ABCs de capacidad. Una subclase que declara
    `capability = Capability.X` EN SU PROPIO CUERPO se registra como la ABC
    de esa capacidad; las que solo la heredan (los exchanges concretos) no."""

    capability: ClassVar[Capability]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        declarada = cls.__dict__.get("capability")
        if declarada is not None:
            previa = CAPABILITY_ABCS.get(declarada)
            if previa is not None and previa is not cls:
                raise TypeError(f"{declarada} ya la define {previa.__name__}; no puede definirla {cls.__name__}.")
            CAPABILITY_ABCS[declarada] = cls


class Exchange(ABC):
    """Identidad de un exchange y consulta de sus capacidades.

    - `name`: clave en la factory y en el entorno (`<NAME>_API_KEY`), en
      minúsculas.
    - `markets`: qué cuentas tiene (`Market.SPOT`, `Market.FUTURES`).
    - `unsupported`: capacidades que la clase HEREDA pero no soporta de
      verdad. Pasa con `Broker`, que agrupa las capacidades de trading por
      compatibilidad: Bitunix hereda `NativeOco` a través de él y levanta,
      así que lo declara aquí.
    """

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    markets: ClassVar[frozenset[Market]] = frozenset({Market.SPOT})
    unsupported: ClassVar[frozenset[Capability]] = frozenset()

    @classmethod
    def capabilities(cls) -> frozenset[Capability]:
        return frozenset(cap for cap, abc in CAPABILITY_ABCS.items() if issubclass(cls, abc)) - cls.unsupported

    @classmethod
    def supports_market(cls, market: Market | str) -> bool:
        return Market(market) in cls.markets

    @classmethod
    def supports(cls, capability: Capability | str, market: Market | str | None = None) -> bool:
        """¿Tiene la capacidad (y, si se indica, en ese mercado)?"""
        if market is not None and not cls.supports_market(market):
            return False
        return Capability(capability) in cls.capabilities()

    @classmethod
    def display_name(cls) -> str:
        return cls.label or cls.name or cls.__name__

    def require(self, capability: Capability | str, market: Market | str | None = None) -> None:
        """Levanta `NotImplementedError` con un motivo legible si no la tiene."""
        if market is not None and not self.supports_market(market):
            raise NotImplementedError(f"{self.display_name()} no tiene mercado {Market(market).value}.")
        if Capability(capability) not in self.capabilities():
            raise NotImplementedError(f"{self.display_name()} no soporta {Capability(capability).value}.")

    @classmethod
    def create(cls, credentials: Any = None, **options: Any) -> Exchange:
        """Cómo la construye la factory. Por defecto, el constructor con las
        opciones (y `credentials` si se pasan); cada exchange que necesite
        resolver claves o modos lo redefine."""
        if credentials is None:
            return cls(**options)
        return cls(credentials=credentials, **options)
