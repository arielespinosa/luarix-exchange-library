# Bybit, OKX, Gate, HTX, KuCoin y MEXC

```bash
pip install "luarix-exchange-library[bybit,okx,gate,htx,kucoin,mexc]"
```

Conectores **spot** portados de luarix-arbitrage-web-server (2026-10-18) con su
lógica intacta. Están pensados para arbitraje: reglas, reloj, libros en tiempo
real, comisiones y activos. Todavía **no** tienen velas ni órdenes (ver abajo).

```python
from luarix_exchange.factory import ExchangeFactory
from luarix_exchange.aio import AsyncExchange

okx = ExchangeFactory.create("okx")                 # datos públicos: sin claves
okx.get_symbol_rules()                              # {'BTC-USDT': SymbolRules(...), ...}
okx.clock_offset_ms()

ex = AsyncExchange(okx)                             # interfaz async de arbitraje
await ex.stream_books(["BTC-USDT"], on_book, stop, latency_symbol="BTC-USDT", on_event_time=...)
```

| | Símbolos | Libro (WebSocket) | Profundidad | Activos con redes | Órdenes de prueba |
|---|---|---|---|---|---|
| **Bybit** | `BTCUSDT` | foto + deltas, detecta huecos | 1, 50, 200 | — | testnet |
| **OKX** | `BTC-USDT` | foto + deltas, `seqId` + checksum | 1-400 (5 = `books5`) | — | modo demo |
| **Gate** | `BTC_USDT` | fotos cada 100 ms | 5, 10, 20, 50, 100 | ✅ | — |
| **HTX** | `btcusdt` | fotos (gzip) | 5, 10, 20 | ✅ (comisión de retirada) | — |
| **KuCoin** | `BTC-USDT` | fotos (token previo) | 5, 50 | ✅ (comisión de retirada) | — |
| **MEXC** | `BTCUSDT` | fotos (protobuf) | 5, 10, 20 | — | — |

Capacidades de los seis: `SYMBOL_RULES`, `SERVER_CLOCK`, `ORDER_BOOK_STREAM`,
`FEES`, `ASSET_INFO` (+ `ORDER_ROUNDTRIP` en Bybit y OKX).

## Credenciales

Para comisiones (claves de solo lectura) y, en Bybit/OKX, para medir órdenes en
su entorno de pruebas:

| | Variables |
|---|---|
| Bybit | `BYBIT_API_KEY`, `BYBIT_API_SECRET` (testnet: `BYBIT_TESTNET_*`) |
| OKX | `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE` (demo: `OKX_DEMO_*`) |
| KuCoin | `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_API_PASSPHRASE` |
| Gate / HTX / MEXC | `<NOMBRE>_API_KEY`, `<NOMBRE>_API_SECRET` |

## Pendiente

- **Velas, precio y libro por REST** (capacidad `MARKET_DATA`): necesarias para
  usarlos desde Luarix (estrategias sobre velas).
- **Cuenta y órdenes reales**: necesitan claves para probarlas.
- **Reloj de HTX**: su endpoint REST da una estimación ~200 ms peor que la de
  los demás (latencias medidas de ~320 ms en vez de ~100); mejorable con las
  marcas del WebSocket, como Kraken y Bitfinex.
