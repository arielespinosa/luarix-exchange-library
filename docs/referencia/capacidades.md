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
| `ASSET_INFO` | `AssetInfo` | `list_assets()` → `dict[str, AssetListing]` |
| `ORDER_ROUNDTRIP` | `OrderRoundtripProbe` | `measure_order_roundtrip_ms(credentials, samples)` en testnet/demo |
| `SERVER_CLOCK` | `ServerClock` | `server_time_ms()`, `clock_offset_ms()` |
| `CANDLE_STREAM` | `CandleStream` | `async stream_candles(...)` |
| `ORDER_BOOK_STREAM` | `OrderBookStream` | `async stream_order_books(...)` |
| `ORDER_UPDATE_STREAM` | `OrderUpdateStream` | `async stream_order_updates(...)` |
| `ACCOUNT_SUMMARY` | `AccountSummarySource` | `get_account_summary(markets=None)` → `AccountSummary`: spot + futuros, libre + bloqueado, cada activo con su valor en USDT y los totales |
| `FUNDING_HISTORY` | `FundingHistory` | `get_deposits(start, end)`, `get_withdrawals(start, end)` → `list[FundingMovement]` |
| `INCOME_HISTORY` | `IncomeHistory` | `get_income(start, end)` → `list[IncomeEntry]` (PnL realizado, funding, comisiones de futuros) |

REST **síncrono**; streams **asíncronos** (decisión híbrida, 2026-10-16).
Cualquier exchange con `MARKET_DATA`/`ACCOUNT` tiene streams por polling
desde la Fase 4; ver [Streaming en vivo](../guia/streaming.md).

## Matriz actual

Fijada en `tests/test_capabilities.py::BrokerMatrixTests`.

| | `MARKET_DATA` | `SYMBOL_RULES` | `ACCOUNT` | `TRADING` | `DERIVATIVES` | `NATIVE_OCO` | `SERVER_CLOCK` | streams nativos | `markets` |
|---|---|---|---|---|---|---|---|---|---|
| Binance | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | velas, libro, órdenes (spot y futuros) | spot, futures |
| Kraken | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | velas, libro | spot |
| Bitfinex | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | velas, libro | spot |
| Bybit, OKX¹ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | libro | spot |
| Gate, HTX, KuCoin, MEXC | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | libro | spot |
| Bitunix | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | velas, libro, órdenes (solo futuros) | spot, futures |
| Paper | ❌¹ | ❌ | ✅ | ✅ | ❌ | ✅ | ❌ | — | spot |
| MT5 | ❌² | ❌ | ✅ | ✅ | ❌ | ✅³ | ❌ | — | spot |

¹ Bybit y OKX además `ORDER_ROUNDTRIP` (testnet / demo), como Binance. Los seis tienen `FEES` y `ASSET_INFO`.

¹ (Paper) Las velas le entran por `on_candle`, no por red. ² Tiene `get_ticker` y
`list_symbols`, pero no velas ni libro. ³ SL/TP sobre la posición.

`FEES`: Binance, Kraken y Bitfinex. `ASSET_INFO`: Kraken (con estado de
depósitos/retiradas) y Bitfinex (deducido de sus pares). En qué mercado es nativo cada stream lo dice
`native_stream_markets` (`Exchange.has_native(cap, market)`); donde no lo es,
la selección usa polling sobre `MARKET_DATA`/`ACCOUNT`.

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

## Saldo, depósitos e income (Fase 9, 2026-10-19)

Para el portafolio de Luarix. Ver `docs/plans/historial-de-cuenta.md`.

| | `ACCOUNT_SUMMARY` | `FUNDING_HISTORY` | `INCOME_HISTORY` |
|---|---|---|---|
| Binance | ✅ spot (`get_account`) + futuros (`futures_account`), precios con `get_all_tickers` | ✅ (no en testnet: `/sapi` no existe ahí) | ✅ futuros, `futures_income_history` |
| Bitunix | ✅ spot (`/user/account`) + futuros (`account()`), precios con `/common/rate/list` | ❌ sin endpoint conocido | ❌ |
| MT5, Paper, resto | ❌ | ❌ | ❌ |

```python
resumen = broker.get_account_summary()       # spot + futuros
resumen.equity_usdt, resumen.available_usdt  # totales en USDT
for activo in resumen.assets:                # AssetBalance
    activo.asset, activo.market, activo.free, activo.locked, activo.usdt_value
resumen.errors                               # {"futures": "..."} si un mercado falló
```

`get_balance` **no cambia**: sigue siendo el saldo DISPONIBLE por activo (lo
que pregunta un grafo antes de entrar). El total valorado es `get_account_summary`.
