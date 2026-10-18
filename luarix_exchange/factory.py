"""`ExchangeFactory`: construir un exchange por nombre y preguntar qué sabe
hacer, sin conocer su clase.

```python
ExchangeFactory.available()                        # ["binance", "bitunix", "mt5", "paper"]
ExchangeFactory.capabilities("bitunix")            # sin instanciar ni credenciales
ExchangeFactory.find(Capability.NATIVE_OCO)        # ["binance", "mt5", "paper"]
broker = ExchangeFactory.create("bitunix", credentials=creds, trading_mode="live")
```

El registro guarda RUTAS ("modulo:Clase"), no clases: importar esta factory
no importa ningún SDK. Solo se importa el módulo del exchange que se pide, así
que una instalación base (solo pandas) sigue pudiendo crear un `PaperBroker`.
Consultar las capacidades de un exchange sí importa su módulo, y por tanto
necesita su extra instalado (`pip install luarix-exchange-library[binance]`).

Añadir un exchange = una línea en `_REGISTRY` + su clase heredando de
`Exchange` y de las capacidades que implemente.
"""

from __future__ import annotations

import importlib
from typing import Any

from luarix_exchange.capabilities import Capability, Exchange
from luarix_exchange.models.brokers import Market

_REGISTRY: dict[str, str] = {
    "binance": "luarix_exchange.brokers.binance:BinanceBroker",
    "bitunix": "luarix_exchange.brokers.bitunix:BitunixBroker",
    "mt5": "luarix_exchange.brokers.mt5:MT5Broker",
    "paper": "luarix_exchange.brokers.paper:PaperBroker",
}


class ExchangeFactory:

    @staticmethod
    def available() -> list[str]:
        return sorted(_REGISTRY)

    @staticmethod
    def exchange_class(name: str) -> type[Exchange]:
        """La clase registrada con ese nombre (importa su módulo).

        Raises:
            ValueError: si el nombre no está registrado, nombrando los que sí.
            ModuleNotFoundError: si falta el SDK del exchange (su extra).
        """
        clave = str(name).strip().lower()
        ruta = _REGISTRY.get(clave)
        if ruta is None:
            raise ValueError(f"No existe el exchange {name!r}. Disponibles: {', '.join(ExchangeFactory.available())}.")
        modulo, clase = ruta.split(":")
        return getattr(importlib.import_module(modulo), clase)

    @staticmethod
    def create(name: str, credentials: Any = None, **options: Any) -> Exchange:
        """Construye el exchange. Las opciones son las de su `create()`
        (p. ej. `testnet`, `trading_mode`, `allow_live_trading`)."""
        return ExchangeFactory.exchange_class(name).create(credentials=credentials, **options)

    @staticmethod
    def capabilities(name: str) -> frozenset[Capability]:
        return ExchangeFactory.exchange_class(name).capabilities()

    @staticmethod
    def markets(name: str) -> frozenset[Market]:
        return ExchangeFactory.exchange_class(name).markets

    @staticmethod
    def find(capability: Capability | str, market: Market | str | None = None) -> list[str]:
        """Los exchanges registrados con esa capacidad (y mercado). Omite en
        silencio los que no se pueden importar porque falta su SDK: no
        tenerlo instalado equivale, en esta máquina, a no tenerlo."""
        encontrados = []
        for nombre in ExchangeFactory.available():
            try:
                cls = ExchangeFactory.exchange_class(nombre)
            except ImportError:
                continue
            if cls.supports(capability, market):
                encontrados.append(nombre)
        return encontrados
