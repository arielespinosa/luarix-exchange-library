# Contrato `Broker`

`luarix_exchange.brokers.base.Broker` — la interfaz que implementan todos.

Referencia completa de firmas. Para el *cómo* y el *por qué*, ver la
[Guía de uso](../guia/credenciales.md).

---

## Atributos de clase

`Broker` es, desde la Fase 3, la unión de las capacidades de trading
(`MarketData`, `Account`, `Trading`, `Derivatives`, `NativeOco`). Ver
[Capacidades](capacidades.md).

```python
name, label                                 # "binance", "Binance"
markets: frozenset[Market]                  # {SPOT} o {SPOT, FUTURES}
unsupported: frozenset[Capability]          # lo heredado que NO soporta
supports_futures                            # DERIVADO: Market.FUTURES in markets
supports_oco                                # DERIVADO: NATIVE_OCO in capabilities()
supports_independent_orders: bool = False   # ¿Entry+TP+SL como 3 órdenes propias?
```

| Broker | `futures` | `oco` | `independent` |
|---|---|---|---|
| `BinanceBroker` | ✅ | ✅ | ✅ |
| `BitunixBroker` | ✅ | ❌ | ✅ |
| `PaperBroker` | ❌ | ✅ | ✅ |
| `MT5Broker` | ❌ | ✅ | ❌ |

## Atributos de instancia

```python
market_data_provider: MarketDataProvider    # velas, precio, libro
account_data_provider: AccountDataProvider  # saldo, posiciones, órdenes
on_kline: Callable
on_execution_report: Callable
```

`PaperBroker` y `MT5Broker` **no tienen proveedores**: los métodos que delegan
en ellos levantan `NotImplementedError`.

---

## Conexión

### `connect()`

```python
def connect(self) -> None
```

**Abstracto.** Establece o verifica la conexión. Binance hace `ping()`; Bitunix
y Paper no hacen nada.

### `list_symbols()`

```python
def list_symbols(self, market: str = "spot") -> list[str]
```

**Abstracto.** Los pares operables de ese mercado. `list_active()` sigue
existiendo como alias **deprecado** de `list_symbols("spot")` (avisa con
`DeprecationWarning`).

| Broker | Fuente |
|---|---|
| Binance | `exchangeInfo` de spot o de futuros, solo `status == "TRADING"` |
| Bitunix | lista pública de pares de spot (`isOpen == 1`) o de futuros (`symbolStatus == "OPEN"`), en MAYÚSCULAS |
| Paper | Los símbolos cargados con `load_csv` |
| MT5 | `mt5.symbols_get()` |

---

## Datos de mercado

### `get_candles()`

```python
def get_candles(
    self,
    symbol: str,
    interval: str = "1m",
    limit: int = 100,
    market: str = "spot",
) -> list[Candle]
```

Concreto. Las `limit` velas **cerradas** más recientes, de la más antigua a la
más nueva. Delega en `market_data_provider` con `format=DataFormat.CANDLE`.

→ [Velas](../guia/velas.md)

### `get_last_candle()`

```python
def get_last_candle(
    self, symbol: str, interval: str = "1m", market: str = "spot",
) -> Candle | None
```

Concreto. Atajo de `get_candles(..., limit=1)`. `None` si no hay ninguna.

### `get_historical_candles()`

```python
def get_historical_candles(
    self,
    symbol: str,
    interval: str = "1m",
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    market: str = "spot",
) -> list[Candle]
```

Concreto. Velas de un rango, paginando. Sin `start`, equivale a `get_candles`.

### `get_ticker()`

```python
def get_ticker(self, symbol: str, market: str = "spot") -> float
```

**Abstracto.** El último precio negociado.

### `get_order_book()`

```python
def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> OrderBook
```

**Abstracto.** El libro **normalizado** (desde la Fase 3): `Decimal`, bids y
asks del mejor al peor, crudo en `.raw`. Paper y MT5 levantan
`NotImplementedError` (no tienen libro).

---

## Cuenta

### `get_balance()`

```python
def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]
```

**Abstracto.** El saldo **disponible** por activo. Lo bloqueado en órdenes o
retenido como margen queda fuera.

Un activo pedido y ausente devuelve `{asset: 0.0}`, no `{}`.

→ [Balance y posiciones](../guia/balance-y-posiciones.md)

### `get_positions()`

```python
def get_positions(
    self, market: str = "spot", symbol: str | None = None,
) -> list[BrokerPosition]
```

**Abstracto.** Las posiciones **abiertas** (cantidad distinta de cero).

En spot no hay posiciones: se devuelven los saldos de activos representados
igual, excluyendo las monedas de cotización.

### `set_leverage()`

```python
def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict
```

Concreto. Delega en `account_data_provider`.

⚠ Operación de **cuenta**: afecta a todo lo que se coloque después en ese par.

---

## Órdenes

### `place_order()`

```python
def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]
```

**Abstracto.** Coloca una orden.

En futuros, `fills` viene vacío: se consultan después.

→ [Órdenes](../guia/ordenes.md) · [`OrderRequest`](../conceptos/modelos.md#orderrequest)

### `get_open_orders()`

```python
def get_open_orders(
    self, market: str = "spot", symbol: str | None = None,
) -> list[OrderInfo]
```

Concreto. Las órdenes pendientes, normalizadas.

⚠ El spot de Bitunix **exige** símbolo: sin él levanta.

### `get_order()`

```python
def get_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderInfo
```

Concreto. El detalle completo, **incluso de órdenes ya cerradas**.

### `get_order_status()`

```python
def get_order_status(
    self, symbol: str, order_id: str, market: str = "spot",
) -> OrderStatus
```

Concreto. Deriva de `get_order()`. Un estado no reconocido → `NEW`, nunca
`FILLED`.

### `update_order()`

```python
def update_order(
    self,
    symbol: str,
    order_id: str,
    *,
    price: float | None = None,
    quantity: float | None = None,
    stop_price: float | None = None,
    tp_price: float | None = None,
    sl_price: float | None = None,
    market: str = "spot",
) -> OrderAck
```

**Abstracto.** Modifica una orden pendiente. Lo que no se pasa se conserva.

⚠ **En spot el `order_id` cambia** (cancelar + recolocar) y **no es atómico**.

### `cancel_order()`

```python
def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck
```

**Abstracto.** Cancela una orden.

⚠ `market` importa: en Bitunix, cancelar una orden de futuros por la vía de spot
devuelve un **OK falso**.

### `cancel_open_orders()`

```python
def cancel_open_orders(
    self, market: str = "spot", symbol: str | None = None,
) -> list[str]
```

Concreto. Devuelve los **ids cancelados**.

---

## OCO

### `place_oco_order()`

```python
def place_oco_order(
    self,
    symbol: str,
    side: Side,                  # ← el lado OPUESTO al de la entrada
    quantity: float,
    take_profit_price: float,
    stop_price: float,
    stop_limit_price: float,
) -> tuple[str, OrderAck, OrderAck]
```

**Abstracto.** Devuelve `(oco_list_id, tp_ack, sl_ack)`.

⚠ Comprueba `supports_oco` **antes**. Bitunix levanta.

→ [Take profit y stop loss](../guia/tp-sl.md)

### `cancel_oco()` / `update_oco()`

```python
def cancel_oco(self, symbol: str, oco_list_id: int) -> OrderAck
def update_oco(
    self, entry: Entry, new_tp: float, new_sl: float, new_sl_limit: float,
) -> tuple[str, OrderAck, OrderAck]
```

**Abstractos.**

---

## Callbacks

```python
def on_order_status_change(self, report) -> None   # no-op por defecto
def on_candle(self, tick: Candle) -> None          # no-op por defecto
def load_csv(symbol, timeframe, filepath)          # no-op por defecto
```

`PaperBroker` los usa de verdad: `on_candle` es lo que hace avanzar la
simulación.

---

## Implementar un broker nuevo

Lo mínimo:

```python
from luarix_exchange.brokers.base import Broker
from luarix_exchange.models.orders import OrderInfo

class MiBroker(Broker):
    name = "mibroker"
    label = "Mi Broker"
    markets = frozenset({Market.SPOT, Market.FUTURES})   # -> supports_futures
    unsupported = frozenset({Capability.NATIVE_OCO})      # -> supports_oco = False
    supports_independent_orders = True

    def __init__(self, api_key, api_secret, **kw):
        self.market_data_provider = MiMarketData(...)     # hereda las velas
        self.account_data_provider = MiAccountData(...)   # hereda las consultas

    # --- lo que NADIE puede heredar ---
    def connect(self): ...
    def list_symbols(self, market="spot"): ...
    def get_ticker(self, symbol, market="spot"): ...
    def get_order_book(self, symbol, limit=20, market="spot"): ...
    def get_balance(self, market="spot", asset=None): ...
    def get_positions(self, market="spot", symbol=None): ...
    def place_order(self, order): ...
    def cancel_order(self, symbol, order_id, market="spot"): ...
    def update_order(self, symbol, order_id, *, price=None, quantity=None,
                     stop_price=None, tp_price=None, sl_price=None, market="spot"): ...
    def place_oco_order(self, *a, **kw):
        raise NotImplementedError("Mi exchange no tiene OCO nativo.")
    def cancel_oco(self, *a, **kw): raise NotImplementedError(...)
    def update_oco(self, *a, **kw): raise NotImplementedError(...)

    # --- para que get_order/get_open_orders funcionen ---
    def _to_order_info(self, raw: dict, market: str) -> OrderInfo:
        return OrderInfo(...)      # traduce TU forma
```

!!! important "Las tres reglas"
    1. **Fallar en voz alta.** Lo que no sepas hacer, levanta. Nunca `[]`, nunca
       `{}`, nunca `None`.
    2. **`quantity` positiva.** La dirección va en `side`.
    3. **Orden ascendente.** Las velas, de la más antigua a la más nueva.

Si no es un broker de trading completo (solo datos, solo spot...), no heredes
de `Broker`: hereda de `Exchange` y de las capacidades que tenga. Ver
[Añadir un exchange](../guia/nuevo-exchange.md).

Después, regístralo en `_REGISTRY` de `luarix_exchange/factory.py` (por RUTA,
para que su SDK no sea obligatorio), y escribe un doble de su API
en `tests/test_broker_contract.py` con las respuestas **reales** de tu exchange,
nombres de campo incluidos. Ahí es donde estaban todos los bugs.
