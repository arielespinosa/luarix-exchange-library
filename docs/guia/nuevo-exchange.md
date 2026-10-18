# Añadir un exchange

Un exchange implementa **solo lo que sabe hacer**. Nada de métodos que
levantan por obligación de la interfaz.

## 1. Elige la base

- **Trading completo** (cuenta, órdenes, datos): hereda de `Broker`, como
  Binance y Bitunix. Declara en `unsupported` lo que `Broker` te obliga a
  heredar y no tienes (p. ej. `NATIVE_OCO`).
- **Cualquier otra cosa**: hereda de `Exchange` y de las capacidades que
  tengas. Es lo que harán Kraken y Bitfinex en su primer paso:

```python
from luarix_exchange.capabilities import Exchange, MarketData, SymbolRulesSource
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market


class KrakenExchange(Exchange, MarketData, SymbolRulesSource):
    name = "kraken"
    label = "Kraken"
    markets = frozenset({Market.SPOT})

    def get_order_book(self, symbol, limit=20, market="spot") -> OrderBook:
        data = self._public("Depth", pair=symbol, count=limit)
        return OrderBook.from_levels(symbol, market, data["bids"], data["asks"], depth=limit, raw=data)

    def get_symbol_rules(self, market="spot"): ...
    def list_symbols(self, market="spot"): ...
    def get_candles(...): ...
    def get_historical_candles(...): ...
    def get_ticker(...): ...
```

`KrakenExchange.capabilities()` da `{MARKET_DATA, SYMBOL_RULES}` sin hacer
nada más. Si te dejas un método abstracto, la clase no se puede instanciar
(`TypeError`): una capacidad a medias no pasa desapercibida.

## 2. Regístralo

Una línea en `_REGISTRY` de `luarix_exchange/factory.py`, por **ruta**
(para no importar su SDK hasta que alguien lo pida):

```python
"kraken": "luarix_exchange.exchanges.kraken:KrakenExchange",
```

Si su constructor necesita resolver credenciales o modos, redefine el
`create()` de clase (ver `BinanceBroker.create`). Su SDK, si lo tiene, va en
un extra de `pyproject.toml`.

## 3. Reglas

1. **REST síncrono, streams asíncronos.** Las capacidades de stream son
   `async def`; todo lo demás es síncrono.
2. **Fallar en voz alta.** Lo que no sepas hacer, levanta. Nunca `[]`, `{}` ni
   `None` como respuesta "neutra": un libro vacío se lee como "no hay
   liquidez", una lista vacía como "no tienes posiciones".
3. **Normaliza.** Libros en `OrderBook`, reglas en `SymbolRules`, cantidades
   positivas con la dirección en `side`, velas en orden ascendente y solo
   cerradas.
4. **WebSocket primero, polling de respaldo** para velas, libros y órdenes
   (Fases 4-5).

## 4. Tests y verificación

- Dobles de su API con respuestas **reales** (copiadas de los endpoints, nombres
  de campo incluidos): ahí es donde aparecen los bugs. Ver
  `tests/test_book_and_rules_normalization.py`.
- Añade su fila a `tests/test_capabilities.py::BrokerMatrixTests` y a la
  [matriz](../referencia/capacidades.md).
- Pruébalo contra la API real (los endpoints públicos no necesitan claves) y
  anota en [Estado y limitaciones](../estado.md) qué está verificado y qué no.
