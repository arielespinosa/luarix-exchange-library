# PaperBroker

`luarix_exchange.brokers.paper.PaperBroker` — el simulador. Implementa el mismo
contrato `Broker`, **sin red y sin dinero**.

Está en la instalación **base**: no necesita ningún extra.

```python
from luarix_exchange.brokers.paper import PaperBroker

broker = PaperBroker(initial_balance=10_000)
```

O por la factory, que no le pide credenciales:

```python
broker = BrokerFactory.get_broker(AvailableBrokers.PAPER)
```

## Capacidades

| Flag | Valor |
|---|---|
| `supports_futures` | ❌ |
| `supports_oco` | ✅ **simulado de verdad** |
| `supports_independent_orders` | ✅ |

Implementa OCO completo (`place_oco_order` más la cancelación de la pierna
hermana), así que se comporta como Binance y no como Bitunix.

## Constructor

```python
PaperBroker(
    initial_balance=10_000.0,
    commission=0.001,            # 0.1% por operación
    on_kline=None,               # callback por vela
    on_execution_report=None,    # callback por evento de orden
    speed=0.2,
)
```

---

## Cómo funciona

**No consulta datos de mercado.** El precio actual se actualiza llamando a
`on_candle()` en cada iteración:

```python
for vela in velas:
    broker.on_candle(vela)      # avanza el reloj y resuelve las órdenes
```

Dentro de `on_candle`:

1. Se actualiza `_last_close`.
2. Se recorre la cola de órdenes pendientes.
3. Cada una se empareja contra el **rango high/low** de esa vela.
4. Las que llenan generan un `Fill`, mueven el balance y emiten un execution
   report.

### Cuándo llena una orden

| Tipo | Condición |
|---|---|
| `BUY` LIMIT | `candle.low <= price` |
| `SELL` LIMIT | `candle.high >= price` |
| `MARKET` | Al `close` de la vela en curso |
| `STOP_LOSS_LIMIT` | Contra `stop_price` |

!!! info "Una MARKET reventaba en la primera vela"
    `place_order` calculaba `price = Decimal(str(order.price)) if order.price
    else None`. Para una `MARKET`, `order.price` es `None` — y `_filled()` hacía
    `float(order.price)` sin resguardo:

    ```python
    return candle.low <= float(order.price)   # float(None) -> TypeError
    ```

    Cualquier orden de mercado reventaba con `TypeError` en la primerísima vela
    que intentaba emparejarla. Nunca se había ejercitado esa ruta con un broker
    real de punta a punta (los tests usaban gateways falsos que no pasan por
    `_filled()`).

    Ahora se rellena con `_last_close` — el cierre de la vela que `on_candle()`
    está procesando, que es exactamente lo que "ejecutar a mercado" significa.

---

## El equity: la trampa importante

!!! danger "`get_balance()['USDT']` NO es el capital, es la CAJA"
    El simulador opera **al contado**: vender acredita el importe en USDT en el
    acto y comprar lo debita. Una posición corta todavía abierta deja la caja
    **inflada** con el producto de una venta que aún no se ha recomprado.

    Caso real, una corrida que terminó con **un corto abierto** de 0,045 BTC @
    66 699,07:

    | | |
    |---|---|
    | caja (lo que se informaba como capital) | **12 998,46** |
    | equity real (posición valorada a mercado) | **9 997,00** |
    | P&L real | **−3,00** |
    | comisión realmente pagada | **3,00** |

    Los 2 998,46 de "beneficio" eran, al céntimo, `0,045 × 66 699,07`: el
    importe de la venta pendiente de recomprar. **Dinero que no existía.** El
    informe decía +30% y un APR del 198,82% con **cero operaciones cerradas**.

Usa siempre:

```python
equity = broker.mark_to_market_equity("BTCUSDT", precio_actual)
```

Efectivo **más** el valor de mercado de lo que sigue abierto para ese símbolo:
un largo **suma** `qty × precio`, un corto lo **resta** (la caja ya lleva la
pata contraria de la entrada, así que se compensan y queda el no realizado como
única diferencia).

Verificado en ambos sentidos: abrir deja el equity en `inicial − comisión`, y un
movimiento de +3000 puntos da `+300` en largo y `−300` en corto sobre 0,1 BTC.

!!! warning "Solo valora el par que se le pase"
    Una corrida multi-par necesitaría un precio por símbolo.

Test de regresión con esos números:
`tests/test_paper_broker_mark_to_market_equity.py`.

---

## Qué implementa y qué no

### ✅ Implementa

| Operación | Notas |
|---|---|
| `place_order` | MARKET, LIMIT, STOP_LOSS_LIMIT |
| `cancel_order` | |
| `update_order` | **In situ**: conserva el `order_id` |
| `get_order_status` | La verdad está en memoria |
| `get_balance(market, asset)` | Una sola bolsa; `market` se ignora |
| `get_positions(market, symbol)` | `list[BrokerPosition]` |
| `get_ticker` | El último `close` visto |
| `list_symbols` | Los símbolos cargados |
| `place_oco_order` / `cancel_oco` / `update_oco` | Completo |
| `load_csv`, `get_historical_data`, `prepare_data` | Datos desde disco |
| `mark_to_market_equity` | El capital de verdad |

`update_order` modifica **in situ** — aquí no hay ninguna razón para cancelar y
recolocar (que es lo único que permite el spot de un exchange real), y hacerlo
cambiaría el `order_id` y rompería el seguimiento del backtest.

Sin ningún valor nuevo, cancela — que es lo que hacía **siempre** antes,
ignorando por completo que se le pidiera una modificación.

### ❌ No implementa

| Operación | Qué pasa |
|---|---|
| `get_open_orders` | `NotImplementedError: PaperBroker no expone datos de cuenta.` |
| `cancel_open_orders` | Ídem |
| `get_order` | Ídem |
| `get_candles` y compañía | `NotImplementedError: PaperBroker no expone datos de mercado.` |
| `set_leverage` | Ídem |

No tiene `account_data_provider` ni `market_data_provider`: no es un broker con
cuenta remota.

!!! tip "Cómo listar sus pendientes"
    Directamente desde el estado interno:

    ```python
    for orden in broker.state.pending_orders:
        if orden.is_oco_leg:
            continue        # esas no son entradas esperando, son la salida
        print(orden.order_id, orden.symbol, orden.side, orden.quantity, orden.price)
    ```

    Es lo que hace `TradingFrameworkBrokerAdapter` en `luarix-orchestator`.

---

## Callbacks

```python
def al_reporte(report: dict):
    print(report["x"], report["s"], report["i"])   # tipo, símbolo, id

broker = PaperBroker(on_execution_report=al_reporte)
```

Los reports tienen el **formato de execution report de Binance** (`x`, `s`, `i`,
`S`, `o`, `L`, `z`, `n`, `g`...), a propósito: así el mismo código que procesa
eventos de Binance vale para el simulador.

`ExecutionReportFactory.parse(report)` los traduce de vuelta a un
`ParsedExecutionReport` tipado:

```python
from luarix_exchange.brokers.paper import ExecutionReportFactory

p = ExecutionReportFactory.parse(report)
p.execution_type     # "NEW" | "TRADE" | "CANCELED" | "REJECTED" | "EXPIRED"
p.fill_price
p.quantity
p.commission_paid    # n (comisión en base) × L (precio) -- lo que salió de la caja
p.direction          # "LONG" | "SHORT"
```

---

## Cargar datos históricos

```python
broker.load_csv("BTCUSDT", "4h", "datos/BTCUSDT/4h.csv")
broker.prepare_data("BTCUSDT")         # carga 4h y 1d desde DATA_PATH
df = broker.get_historical_data("BTCUSDT", "4h", start=..., limit=1000)
```

El CSV debe tener una columna `date` parseable.

---

## Un backtest mínimo

```python
from luarix_exchange.brokers.paper import PaperBroker
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

broker = PaperBroker(initial_balance=10_000, commission=0.001)

for vela in velas:
    broker.on_candle(vela)

    if hay_señal(vela):
        broker.place_order(OrderRequest(
            symbol="BTCUSDT", side=Side.BUY, quantity=0.01,
            order_type=OrderType.LIMIT, price=vela.close * 0.999,
        ))

print("equity final:", broker.mark_to_market_equity("BTCUSDT", velas[-1].close))
print("posiciones abiertas:", broker.get_positions())
```

!!! tip "Cuenta también lo que quedó ABIERTO"
    `n_trades` cuenta solo operaciones **cerradas**, lo cual es correcto, pero
    un log que no mencione las abiertas hace que "0 trades" sea indistinguible
    de "no pasó nada". Di siempre cuántas quedaron abiertas y cuánta comisión se
    pagó.

---

## Diferencias con un broker real

| | PaperBroker | Real |
|---|---|---|
| Latencia | 0 | Sí |
| Slippage | Ninguno (llena al precio pedido) | Sí |
| Rechazos | Nunca | Fondos, precisión, límites |
| Liquidez | Infinita | Del libro |
| Precisión | Sin filtros de símbolo | `step_size`, `tick_size`, `min_notional` |
| Fills parciales | No | Sí |
| Futuros | No | Sí |
| Funding | No | Sí |

Un backtest que gana en el simulador puede perder en real por cualquiera de
estas filas.
