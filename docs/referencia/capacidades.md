# Capacidades

Qué sabe hacer cada exchange, declarado por herencia y consultable **sin
instanciarlo ni tener credenciales**. Diseño completo en
`docs/plans/fase3_contrato_capacidades.md` del repo.

```python
from luarix_exchange.capabilities import Capability
from luarix_exchange.factory import ExchangeFactory

ExchangeFactory.available()                            # ['binance', 'bitunix', 'mt5', 'paper']
ExchangeFactory.capabilities("bitunix")                # frozenset({...})
ExchangeFactory.find(Capability.NATIVE_OCO)            # ['binance', 'mt5', 'paper']
ExchangeFactory.find(Capability.DERIVATIVES, market="futures")   # ['binance', 'bitunix']

broker = ExchangeFactory.create("bitunix", credentials=creds, trading_mode="live",
                                allow_live_trading=True)
broker.supports(Capability.SYMBOL_RULES, market="futures")       # True
broker.require(Capability.NATIVE_OCO)    # NotImplementedError: Bitunix no soporta native_oco.
```

!!! note "Consultar capacidades importa el módulo del exchange"
    La factory guarda rutas, no clases: importarla no carga ningún SDK.
    Pero `capabilities("binance")` sí importa `luarix_exchange.brokers.binance`,
    así que necesita el extra `[binance]`. `find()` omite los exchanges cuyo
    SDK no está instalado.

## Las capacidades

| `Capability` | Interfaz | Métodos |
|---|---|---|
| `MARKET_DATA` | `MarketData` | `get_candles`, `get_last_candle`, `get_historical_candles`, `get_ticker`, `get_order_book` → `OrderBook`, `list_symbols` |
| `SYMBOL_RULES` | `SymbolRulesSource` | `get_symbol_rules(market)` → `dict[str, SymbolRules]`, `get_symbol_rule(symbol, market)` |
| `ACCOUNT` | `Account` | `get_balance`, `get_positions`, `get_open_orders`, `get_order`, `get_order_status` |
| `TRADING` | `Trading` | `place_order`, `cancel_order`, `update_order`, `cancel_open_orders` |
| `DERIVATIVES` | `Derivatives` | `set_leverage` |
| `NATIVE_OCO` | `NativeOco` | `place_oco_order`, `cancel_oco`, `update_oco` |
| `FEES` | `Fees` | `get_commissions(symbols, market)` → `dict[str, CommissionRates]` |
| `SERVER_CLOCK` | `ServerClock` | `server_time_ms()`, `clock_offset_ms()` |
| `CANDLE_STREAM` | `CandleStream` | `async stream_candles(...)` |
| `ORDER_BOOK_STREAM` | `OrderBookStream` | `async stream_order_books(...)` |
| `ORDER_UPDATE_STREAM` | `OrderUpdateStream` | `async stream_order_updates(...)` |

REST **síncrono**; streams **asíncronos** (decisión híbrida, 2026-10-16). Los
streams solo están definidos: su implementación es la Fase 4.

## Matriz actual

Fijada en `tests/test_capabilities.py::BrokerMatrixTests`.

| | `MARKET_DATA` | `SYMBOL_RULES` | `ACCOUNT` | `TRADING` | `DERIVATIVES` | `NATIVE_OCO` | `markets` |
|---|---|---|---|---|---|---|---|
| Binance | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | spot, futures |
| Bitunix | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | spot, futures |
| Paper | ❌¹ | ❌ | ✅ | ✅ | ❌ | ✅ | spot |
| MT5 | ❌² | ❌ | ✅ | ✅ | ❌ | ✅³ | spot |

¹ Las velas le entran por `on_candle`, no por red. ² Tiene `get_ticker` y
`list_symbols`, pero no velas ni libro. ³ SL/TP sobre la posición.

Ninguno tiene todavía `FEES`, `SERVER_CLOCK` ni streams (Fases 4-6).

## Modelos nuevos (en `Decimal`)

| Modelo | Módulo | Qué |
|---|---|---|
| `OrderBook` | `models.book` | Libro normalizado: `bids`/`asks` del mejor al peor, `best_bid`, `best_ask`, `spread`, `mid`, `raw` |
| `SymbolRules` | `models.rules` | `step_size`, `tick_size`, `min_qty`, `min_notional`, `market`; `floor_quantity()`, `round_price()`, `violations()` |
| `CommissionRates` | `models.fees` | `maker`, `taker`, `discount`, `effective_taker` |
| `OrderUpdate` | `models.updates` | Un cambio de estado o llenado (para los streams de órdenes) |

Los modelos anteriores (`OrderRequest`, `OrderAck`, `Candle`...) siguen en
`float`; pasarlos a `Decimal` es una tarea aparte.

## Cómo se declaran

```python
class Exchange:
    name: ClassVar[str]                          # clave de la factory
    markets: ClassVar[frozenset[Market]]         # {SPOT} por defecto
    unsupported: ClassVar[frozenset[Capability]] # heredado pero NO soportado

    @classmethod
    def capabilities(cls) -> frozenset[Capability]   # herencia - unsupported
```

`Broker` hereda todas las de trading por compatibilidad; por eso Bitunix
declara `unsupported = {NATIVE_OCO}`, y Paper y MT5
`{MARKET_DATA, DERIVATIVES}`. Un método no soportado sigue **levantando**
cuando se llama: `supports()` sirve para decidir antes, no para sustituir eso.
