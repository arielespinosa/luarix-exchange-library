# Fase 3 — Contrato por capacidades (diseño)

Creado el 2026-10-16. Estado: **aprobado por el usuario el 2026-10-16, en implementación**
(rama `feat/fase3-capacidades`).
Roadmap general: `docs/plans/roadmap.md`.

## Objetivo

Que añadir un exchange sea **implementar solo lo que ese exchange sabe hacer**,
y que el consumidor pueda preguntar qué sabe hacer antes de usarlo. Hoy no se
puede: `Broker` es una sola ABC con OCO, futuros y trading abstractos, y
Kraken o Bitfinex tendrían que rellenar media interfaz con stubs que levantan.

## Restricciones (lo que NO se puede romper)

Medido con `grep` sobre los consumidores (2026-10-16):

| Usado por orchestator / web-server | Se queda tal cual |
|---|---|
| `place_oco_order` (16 usos), `supports_oco`, `supports_independent_orders` | sí |
| `market_data_provider` (8), `account_data_provider` (1) | sí |
| `on_execution_report` (11), `on_kline` (9) | sí |
| `BrokerFactory.get_broker`, `AvailableBrokers`, `BrokerCredentials` | sí |
| todos los modelos actuales (`OrderRequest`, `OrderAck`, `Candle`... en `float`) | sí |

**Nadie** usa `get_order_book`, `update_oco`, `cancel_oco`, `supports_futures`
ni `list_active` → hay margen para cambiarlos (ver decisiones abiertas).

Criterio de aceptación de la fase: los 163 tests actuales pasan sin tocarlos,
y las suites de orchestator y web-server dan lo mismo que antes.

## Decisión fijada: librería híbrida (usuario, 2026-10-16)

- **REST síncrono** (consultas y órdenes). Es lo que usan web-server y
  orchestator; arbitraje lo llama con `asyncio.to_thread(...)` en las pocas
  llamadas REST que hace (reglas, comisiones, reloj).
- **Streams asíncronos** (velas, libros, fills). Arbitraje los consume con
  `async`; web-server/orchestator con callbacks en un hilo, mediante un puente
  (Fase 4). En esta fase solo se **definen** sus interfaces.

---

## 1. Capacidades

Cada capacidad es una ABC pequeña en `luarix_exchange/capabilities/`. Un
exchange hereda solo las que implementa.

| Capacidad (`Capability.X`) | ABC | Métodos | Tienen hoy |
|---|---|---|---|
| `MARKET_DATA` | `MarketData` | `get_candles`, `get_last_candle`, `get_historical_candles`, `get_ticker`, `get_order_book`, `list_symbols` | Binance, Bitunix, Paper (velas propias) |
| `SYMBOL_RULES` | `SymbolRulesSource` | `get_symbol_rules(market) -> dict[str, SymbolRules]` | Binance (ya lo calcula por dentro: `SymbolFilters`) |
| `ACCOUNT` | `Account` | `get_balance`, `get_positions`, `get_open_orders`, `get_order`, `get_order_status` | Binance, Bitunix, Paper, MT5 |
| `TRADING` | `Trading` | `place_order`, `cancel_order`, `update_order`, `cancel_open_orders` | Binance, Bitunix, Paper, MT5 |
| `DERIVATIVES` | `Derivatives` | `set_leverage` (+ modo de posición, más adelante) | Binance, Bitunix |
| `NATIVE_OCO` | `NativeOco` | `place_oco_order`, `cancel_oco`, `update_oco` | Binance, MT5, Paper |
| `FEES` | `Fees` | `get_commissions(symbols, market) -> dict[str, CommissionRates]` | — (arbitraje lo tiene para Binance/Kraken/Bitfinex) |
| `SERVER_CLOCK` | `ServerClock` | `server_time_ms()`, `clock_offset_ms()` | — (arbitraje) |
| `CANDLE_STREAM` | `CandleStream` | `async stream_candles(...)` | Binance (WS hoy vía python-binance) |
| `ORDER_BOOK_STREAM` | `OrderBookStream` | `async stream_order_books(...)` | — (arbitraje: Binance/Kraken/Bitfinex) |
| `ORDER_UPDATE_STREAM` | `OrderUpdateStream` | `async stream_order_updates(...)` | — (Fase 5) |

### Cómo se declara y se consulta

```python
class Exchange(ABC):
    name: ClassVar[str]                      # "binance", "kraken"... clave de la factory
    markets: ClassVar[frozenset[Market]]     # {Market.SPOT, Market.FUTURES}
    # Lo que la clase HEREDA pero no soporta de verdad (p. ej. Bitunix hereda
    # NativeOco a través de Broker y levanta). Vacío por defecto.
    unsupported: ClassVar[frozenset[Capability]] = frozenset()

    @classmethod
    def capabilities(cls) -> frozenset[Capability]:
        return frozenset(c for c, abc in CAPABILITY_ABCS.items()
                         if issubclass(cls, abc)) - cls.unsupported

    @classmethod
    def supports(cls, capability: Capability, market: Market | None = None) -> bool: ...
```

- Es **de clase**: la factory responde "¿Kraken tiene futuros? ¿Bitunix tiene
  OCO?" sin instanciar ni necesitar credenciales.
- Un método no soportado sigue **levantando** con un mensaje claro (regla del
  repo: nunca devolver vacío). `supports()` es para decidir ANTES, no un
  sustituto de eso.

### Compatibilidad: `Broker` = unión de capacidades de trading

```
Exchange (identidad + capabilities())
  ├── MarketData, SymbolRulesSource, Account, Trading, Derivatives, NativeOco,
  │   Fees, ServerClock, CandleStream, OrderBookStream, OrderUpdateStream
  │
  └── Broker(Exchange, MarketData, Account, Trading, Derivatives, NativeOco)
        ← MISMA API pública que hoy; Binance/Bitunix/Paper/MT5 siguen
          heredando de aquí sin cambiar de firma.
```

Los flags actuales se **derivan** de las capacidades y se conservan como
propiedades de clase de solo lectura:

| Flag de hoy | Pasa a ser |
|---|---|
| `supports_oco` | `NATIVE_OCO in capabilities()` |
| `supports_futures` | `Market.FUTURES in markets` |
| `supports_independent_orders` | se queda como flag propio: no es una capacidad del exchange sino una garantía de comportamiento de `cancel_order` que web-server espeja en su catálogo |

Resultado esperado (los valores no cambian respecto a hoy):

```
              MARKET SYMBOL ACCOUNT TRADING DERIV NATIVE_OCO  markets
              _DATA  _RULES                       
Binance        sí     sí      sí      sí     sí      sí       spot, futures
Bitunix        sí     no*     sí      sí     sí      no       spot, futures
PaperBroker    sí**   no      sí      sí     no      sí       spot
MT5            no     no      sí      sí     no      sí       spot
Kraken (F6)    sí     sí      sí      sí     no      no       spot
Bitfinex (F6)  sí     sí      sí      sí     sí***   no       spot, futures
```
`*` Bitunix expone precisión por par en `get_trading_pairs`; se añade si se
verifica. `**` velas alimentadas por `on_candle`, no por red. `***` perpetuos
`tXXXF0:USTF0`, sin verificar.

---

## 2. Modelos nuevos (`luarix_exchange/models/`)

En **`Decimal`**: son los que necesita arbitraje y los que ya usa. Los modelos
actuales siguen en `float` (cambiarlos rompería a orchestator y web-server).

```python
@dataclass(frozen=True)
class SymbolRules:            # = el de arbitraje, + market
    symbol: str; base: str; quote: str; market: Market
    step_size: Decimal; tick_size: Decimal
    min_qty: Decimal; min_notional: Decimal
    fee_on: Literal["received", "quote"] = "received"
    raw: dict = field(default_factory=dict, compare=False)

@dataclass
class OrderBook:              # = el de arbitraje, + market y raw
    symbol: str; market: Market
    bids: list[tuple[Decimal, Decimal]]   # mejor primero
    asks: list[tuple[Decimal, Decimal]]
    update_id: int | None; received_ms: int
    raw: Any = None

@dataclass(frozen=True)
class CommissionRates:        # = el de arbitraje
    symbol: str; maker: Decimal; taker: Decimal; discount: Decimal | None = None

@dataclass(frozen=True)
class OrderUpdate:            # NUEVO: un cambio de estado o llenado (Fase 4/5)
    exchange: str; market: Market; symbol: str
    order_id: str; client_order_id: str | None
    status: OrderStatus; side: Side
    filled_qty: Decimal; last_fill_qty: Decimal | None; last_fill_price: Decimal | None
    fee: Decimal | None; fee_asset: str | None
    event_time_ms: int; raw: dict
```

`Market` pasa a ser el enum ya existente en `models/brokers.py`
(`Market.SPOT`/`FUTURES`, que es `str`): los métodos siguen aceptando
`market="spot"`.

---

## 3. Factory

```python
ExchangeFactory.create("kraken", credentials=None, **options) -> Exchange
ExchangeFactory.available() -> list[str]
ExchangeFactory.capabilities("bitunix") -> frozenset[Capability]   # sin instanciar
ExchangeFactory.find(Capability.ORDER_BOOK_STREAM, market="spot") -> list[str]
```

- **Registro** con decorador (`@register_exchange`) en vez del `match` actual:
  un exchange nuevo se registra en su propio módulo.
- Importación **perezosa** igual que hoy: registrar no importa el SDK; solo
  `create()` lo hace. Se mantiene que una instalación base (solo pandas) pueda
  crear un `PaperBroker`.
- `BrokerFactory.get_broker(...)` y `AvailableBrokers` siguen existiendo y
  delegan en `ExchangeFactory` (mismos parámetros, mismos errores).

---

## 4. Interfaces de streaming (solo definición en esta fase)

```python
class CandleStream(ABC):
    @abstractmethod
    async def stream_candles(self, subscriptions: Sequence[CandleSubscription],
                             on_candle: Callable[[Candle], Awaitable[None] | None],
                             stop: asyncio.Event) -> None:
        """Solo velas CERRADAS. Reconecta sola hasta `stop`."""

class OrderBookStream(ABC):
    async def stream_order_books(self, symbols, depth, on_book, stop, market="spot") -> None

class OrderUpdateStream(ABC):
    async def stream_order_updates(self, on_update, stop, market="spot") -> None
```

La reconexión con espera exponencial (el *template method* de
`engine/exchanges/base.py` de arbitraje) y el puente síncrono a hilo son la
Fase 4. El respaldo por polling se construye sobre `MarketData`/`Account`, así
que cualquier exchange con REST tendrá streams aunque no tenga WebSocket.

---

## 5. Pasos y verificación

Cada paso deja los 163 tests en verde antes de seguir.

1. `models/`: `SymbolRules`, `OrderBook`, `CommissionRates`, `OrderUpdate`.
   Tests de los modelos.
2. `capabilities/`: enum `Capability`, las ABCs, `Exchange` con
   `capabilities()`/`supports()`. Tests con exchanges falsos mínimos (uno solo
   `MarketData`, otro `MarketData+Account`...).
3. `Broker` hereda de las capacidades sin cambiar firmas; flags derivados.
   Test: la matriz de capacidades de arriba, fijada.
4. `get_symbol_rules` en Binance (spot y futuros) reutilizando `SymbolFilters`.
5. Decisión 1 (abajo): `get_order_book` normalizado en Binance/Bitunix.
6. `ExchangeFactory` con registro; `BrokerFactory` delega. Tests de la factory
   actuales intactos + nuevos de `capabilities()`/`find()` sin instanciar.
7. Docs: `docs/referencia/capacidades.md` (la matriz), `estado.md`,
   guía "cómo añadir un exchange".
8. Verificación de consumidores: suite de orchestator y la de web-server con
   la librería editable, mismos resultados que antes.

Kraken y Bitfinex **no** entran en esta fase (Fase 6), pero el paso 2 incluye
un exchange falso "solo spot, solo datos + reglas + libro" que es exactamente
la forma que tendrá el primer paso de Kraken: si ese test es cómodo de
escribir, el diseño vale.

---

## Decisiones (cerradas por el usuario, 2026-10-16)

Las tres, aceptadas tal como se proponen:

1. ✅ **`get_order_book` normalizado.** Hoy devuelve el dict crudo de cada
   exchange (formas distintas) y **nadie lo usa**. Propuesta: que devuelva
   `OrderBook` (Decimal, bids/asks ordenados, crudo en `.raw`). Rompe la firma
   pero sin consumidores afectados. Alternativa: método nuevo
   `get_book()` y dejar el viejo.
2. ✅ **`Decimal` solo en los modelos nuevos** (migrar los `float` viejos queda para más adelante). Propuesta: sí; unificar los
   viejos a `Decimal` sería una fase aparte, porque toca a todos los
   consumidores.
3. ✅ **`list_active` → `list_symbols`.** Nadie usa `list_active`. Propuesta:
   añadir `list_symbols(market)` y dejar `list_active` como alias deprecado.
