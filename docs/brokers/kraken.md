# Kraken

```bash
pip install "luarix-exchange-library[kraken]"
```

Solo **spot** (Kraken Futures es otra API). Portado de luarix-arbitrage-web-server,
donde está en producción, y completado con velas para Luarix.

```python
from luarix_exchange.factory import ExchangeFactory

kraken = ExchangeFactory.create("kraken")          # datos públicos: sin claves
kraken.list_symbols()                              # ['1INCH/EUR', ..., 'BTC/USDT', ...]
kraken.get_candles("BTC/USDT", "1h", limit=200)    # solo velas cerradas
kraken.get_order_book("BTC/USDT", 10)              # OrderBook en Decimal
kraken.get_symbol_rule("BTC/USDT")                 # step, tick, mínimos, fee_on="quote"
kraken.clock_offset_ms()                           # desfase con su reloj
```

| Capacidad | |
|---|---|
| `MARKET_DATA` | velas (`OHLC`), precio (`Ticker`), libro (`Depth`), pares |
| `SYMBOL_RULES` | desde `AssetPairs`; comisión en el QUOTE |
| `SERVER_CLOCK` | estimado con las marcas del WebSocket (`Time` solo da segundos) |
| `FEES` | `TradeVolume` (clave con permiso "Query Funds") |
| `ASSET_INFO` | `Assets`: código de Kraken y estado de depósitos/retiradas |
| `ORDER_BOOK_STREAM` | canal `book` v2, con checksum CRC-32 en cada mensaje |
| `CANDLE_STREAM` | canal `ohlc` como disparador + REST + temporizador |

## Particularidades

- **Símbolos**: los del WebSocket v2 (`BTC/USDT`), con nombres comunes
  (`XBT` → `BTC`, `XDG` → `DOGE`). En REST se llaman distinto (`XBTUSDT`): la
  traducción es interna.
- **Solo pares `online`**: los `cancel_only`/`post_only` no admiten mercado.
- **Velas REST**: como mucho las **720 más recientes** de cada intervalo; no hay
  histórico más antiguo por este endpoint. `get_historical_candles` levanta si
  se pide un `start` anterior, en vez de devolver un rango incompleto.
- **Profundidad de libro**: 10, 25, 100, 500 o 1000 niveles.
- **Sin cuenta ni órdenes** todavía (necesita claves para probarlo).
