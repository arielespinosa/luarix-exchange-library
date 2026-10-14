# Órdenes

Crear, listar, consultar por id, editar y cancelar. El TP/SL tiene su propia
página: [Take profit y stop loss](tp-sl.md).

## El ciclo completo

```python
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

precio = broker.get_ticker("BTCUSDT", market="futures")

# 1. Crear
ack, fills = broker.place_order(OrderRequest(
    symbol="BTCUSDT", side=Side.BUY, quantity=0.001,
    order_type=OrderType.LIMIT, price=round(precio * 0.98, 2),
    market="futures",
))
order_id = ack.exchange_order_id

# 2. Consultar
info = broker.get_order("BTCUSDT", order_id, market="futures")

# 3. Editar   ⚠ en spot el id CAMBIA
ack = broker.update_order("BTCUSDT", order_id, price=nuevo, market="futures")
order_id = ack.exchange_order_id

# 4. Cancelar
broker.cancel_order("BTCUSDT", order_id, market="futures")
```

---

## Crear

```python
broker.place_order(order: OrderRequest) -> tuple[OrderAck, list[Fill]]
```

### Orden a mercado

```python
OrderRequest(
    symbol="BTCUSDT", side=Side.BUY, quantity=0.001,
    order_type=OrderType.MARKET, market="futures",
)
```

### Orden límite

```python
OrderRequest(
    symbol="BTCUSDT", side=Side.BUY, quantity=0.001,
    order_type=OrderType.LIMIT, price=60000.0,
    time_in_force=TimeInForce.GTC,      # GTC si no se pasa
    market="futures",
)
```

### Orden de cierre

```python
OrderRequest(
    symbol="BTCUSDT", side=Side.SELL, quantity=0.001,
    order_type=OrderType.MARKET, market="futures",
    reduce_only=True,       # ← imprescindible en modo HEDGE
)
```

!!! danger "Sin `reduce_only`, en HEDGE abres un corto"
    Una venta marcada como apertura **no reduce** el largo abierto: abre una
    posición corta independiente que convive con él. Ya causó un incidente real
    — ver [Spot y futuros](../conceptos/mercados.md#modo-cobertura-hedge-vs-neteo-one_way).

### Qué devuelve

```python
ack, fills = broker.place_order(orden)

ack.exchange_order_id   # guárdalo: es con lo que se consulta, edita y cancela
ack.status              # OrderStatus.NEW normalmente
ack.raw                 # respuesta completa del exchange
```

!!! info "`OrderAck` no garantiza ejecución"
    Solo dice que el exchange **recibió** la orden. Para saber si se ejecutó,
    `get_order()` o comprobar `status == OrderStatus.FILLED`.

!!! note "En futuros, `fills` viene vacío"
    Los dos brokers devuelven `(ack, [])` en futuros. Los fills se consultan
    después (`get_order().filled_quantity`). En spot con `MARKET`, Binance sí
    los devuelve en la respuesta.

### Precisión: se redondea solo

Los exchanges rechazan órdenes con más decimales de los permitidos. El paquete
consulta las reglas del símbolo y redondea **hacia abajo**:

| Regla | Qué limita |
|---|---|
| `LOT_SIZE` → `step_size` | Incremento mínimo de cantidad (0.001 BTC) |
| `PRICE_FILTER` → `tick_size` | Incremento mínimo de precio (0.01 USDT) |
| `MIN_NOTIONAL` | Valor mínimo de la orden (10 USDT) |

Si la cantidad redondeada da 0, levanta:

```pycon
ValueError: Rounded quantity is 0 for BTCUSDT (step_size=0.001)
```

!!! warning "Spot y futuros tienen filtros DISTINTOS"
    El mismo par tiene `stepSize` y `tickSize` diferentes en cada mercado.
    Usar los de spot en una orden de futuros da un `-1111` de precisión
    imposible de diagnosticar desde el log. Por eso hay dos caminos separados.

### Errores al colocar

| Broker | Qué pasa si el exchange rechaza |
|---|---|
| **Binance** | Devuelve un `OrderAck` con `status=REJECTED` y el motivo en `raw["error"]` |
| **Bitunix** | **Levanta `BitunixApiError`** con el `code` y el `msg` |

La asimetría es deliberada. Bitunix nunca responde con un HTTP de error:
devuelve `200` con `{"code": "100011", "data": null}`. Antes eso producía un
`OrderAck` con `order_id=""` y `status=NEW` — **una orden fantasma** que el
sistema daba por colocada y a la que luego colgaba su SL/TP.

```python
from luarix_exchange.brokers.bitunix_response import BitunixApiError

try:
    ack, fills = broker.place_order(orden)
except BitunixApiError as e:
    print("Bitunix rechazó la orden:", e)
```

---

## Listar las abiertas

```python
broker.get_open_orders(market="spot", symbol=None) -> list[OrderInfo]
```

```python
for o in broker.get_open_orders(market="futures"):
    print(f"{o.order_id:22} {o.symbol:10} {o.side.value:5} {o.order_type:8} "
          f"qty={o.quantity:<8} @ {o.price} -> {o.status.value}")
```

Devuelve `OrderInfo` **normalizado**, no el dict crudo: los dos brokers llaman
distinto a los mismos campos. El original sigue en `.raw`.

!!! warning "El spot de Bitunix exige un símbolo"
    Su endpoint no tiene un "dame todas las de la cuenta".

    ```python
    broker.get_open_orders(market="spot")                     # ❌ BitunixApiError
    broker.get_open_orders(market="spot", symbol="BTCUSDT")   # ✅
    broker.get_open_orders(market="futures")                  # ✅ todos los pares
    ```

    Levanta en vez de devolver `[]`: una lista vacía se leería como "no tienes
    órdenes", que es justo la conclusión equivocada.

---

## Detalle de una `order_id`

```python
broker.get_order(symbol, order_id, market="spot") -> OrderInfo
```

```python
info = broker.get_order("BTCUSDT", order_id, market="futures")

print(f"{info.symbol} {info.side.value} {info.order_type}")
print(f"  estado    : {info.status.value}   (viva={info.is_open})")
print(f"  cantidad  : {info.filled_quantity} de {info.quantity} "
      f"(queda {info.remaining_quantity})")
print(f"  precio    : límite={info.price}  medio real={info.average_price}")
print(f"  protección: TP={info.tp_price}  SL={info.sl_price}")
```

Distinto de `get_order_status`, que solo da el enum: aquí vienen además la
cantidad ejecutada, el precio medio real y el TP/SL adjunto.

!!! important "Responde también por órdenes ya CERRADAS"
    Y esa es la parte importante. Derivar el estado de la lista de pendientes
    **no vale**: una orden ausente de ahí puede haber llenado **o** haber sido
    cancelada a mano.

!!! danger "El incidente que motivó esto"
    `get_order_status` daba por **llenada** toda orden ausente de los
    pendientes. Tres cortos de LTCUSDT que el usuario canceló a mano en la app
    de Bitunix quedaron marcados `FILLED` en la base de datos, con `Trade` en
    `ACTIVE` — posiciones que en el exchange **nunca existieron**.

    Peor: al pasar la orden a un estado no-vivo, ninguna reconciliación
    posterior volvía a mirarla. El estado falso quedaba congelado.

    Ids reales: `2082878949264547840`, `2082879103115870208`,
    `2082880194939265024`. Reconstruido en `docs/plans/bugs.md`.

### Cómo lo resuelve cada broker

| Broker / mercado | Endpoint |
|---|---|
| Binance spot | `get_order` |
| Binance futuros | `futures_get_order` |
| Bitunix futuros | `get_order_detail` |
| Bitunix **spot** | Combinación (ver abajo) |

El spot de Bitunix **no tiene** un endpoint de detalle. Se combinan las dos
fuentes que sí existen:

1. `query_matching_orders` devuelve las **ejecuciones**. Si trae algo, llenó.
2. Si no, se mira la lista de **pendientes**. Si aparece, sigue viva.
3. Si no aparece en ninguno de los dos sitios, **levanta**. No se inventa un
   estado: "no la encuentro" no es "llenó".

```pycon
>>> broker.get_order("BTCUSDT", "id-inexistente", market="spot")
BitunixApiError: El spot de Bitunix no reconoce la orden id-inexistente de
BTCUSDT: no tiene ejecuciones ni está entre las pendientes. Puede haberse
cancelado, o el id/símbolo no ser correctos. No se asume un estado.
```

### Solo el estado

```python
broker.get_order_status(symbol, order_id, market="spot") -> OrderStatus
```

Deriva de `get_order()`. Un estado que el exchange no sepa dar se traduce a
**`NEW`**, no a `FILLED`: la ignorancia no es un fill. El coste de equivocarse
hacia `NEW` es otra consulta; el de equivocarse hacia `FILLED` es colocar un
TP/SL real contra una posición inexistente.

---

## Editar

```python
broker.update_order(
    symbol, order_id, *,
    price=None, quantity=None, stop_price=None,
    tp_price=None, sl_price=None,
    market="spot",
) -> OrderAck
```

Todos los valores son **por nombre** y opcionales. Lo que no se pasa **se
conserva** de la orden original.

```python
broker.update_order("BTCUSDT", oid, price=61000.0, market="futures")
broker.update_order("BTCUSDT", oid, quantity=0.002, market="futures")
broker.update_order("BTCUSDT", oid, price=61000.0, quantity=0.002, market="futures")
```

!!! danger "El `order_id` puede cambiar"
    Solo hay **enmienda nativa en futuros**:

    | | Enmienda nativa | Qué pasa |
    |---|---|---|
    | Binance futuros | `futures_modify_order` | Conserva el id |
    | Bitunix futuros | `modify_order` | Conserva el id |
    | Binance **spot** | no existe | **Cancela y recoloca** → id nuevo |
    | Bitunix **spot** | no existe | **Cancela y recoloca** → id nuevo |

    **Relee siempre el id del `OrderAck`**:

    ```python
    ack = broker.update_order("BTCUSDT", oid, price=nuevo, market="spot")
    oid = ack.exchange_order_id      # ← puede ser distinto
    ```

!!! danger "Cancelar y recolocar NO es atómico"
    Entre las dos llamadas **la orden no existe**. Para mover un stop-loss en un
    mercado que se está moviendo, eso importa: si la red falla en medio, la
    posición se queda sin protección y nada lo dice.

    En futuros no ocurre: la enmienda nativa nunca deja a la orden de existir.

### Solo se editan órdenes vivas

```pycon
>>> broker.update_order("BTCUSDT", oid_cancelado, price=1.0)
ValueError: La orden 28 de BTCUSDT está en CANCELED: solo se puede modificar
una orden viva.
```

### `tp_price` / `sl_price` al editar

Solo en **Bitunix futuros**, donde el TP/SL son campos **adjuntos** a la orden:

```python
broker.update_order("LTCUSDT", oid, sl_price=44.5, market="futures")
```

En Binance son **órdenes propias**, así que se rechaza en vez de no hacer nada:

```pycon
>>> broker.update_order("BTCUSDT", oid, sl_price=1.0, market="futures")
ValueError: En Binance el TP/SL son órdenes independientes, no campos de la
orden de entrada. Modifícalos llamando a update_order() con el order_id de la
propia orden de TAKE_PROFIT_MARKET/STOP_MARKET y `stop_price=`.
```

Ver [Take profit y stop loss](tp-sl.md).

!!! info "Este método estaba roto en Binance"
    Su cuerpo referenciaba `new_quantity`, `new_price`, `new_stop_price` y
    `new_stop_limit_price`: **cuatro variables que no existían en ningún sitio**
    (la firma era `(symbol, order_id)` a secas). Y remataba con
    `OrderStatus.from_binance(...)`, un método que tampoco existe.

    La secuencia real habría sido: `cancel_order()` → `NameError`. O sea, **la
    orden cancelada y sin sustituta**, con un traceback que no menciona en
    ningún sitio que el stop ya no está. Nadie lo notó porque nada lo llamaba.

---

## Cancelar

### Una

```python
broker.cancel_order(symbol, order_id, market="spot") -> OrderAck
```

!!! warning "`market` importa de verdad aquí"
    En Bitunix, las dos cuentas viven en APIs distintas. Cancelar el TP de una
    posición de **futuros** por la vía de spot **no encuentra nada y devuelve un
    OK falso**.

### Todas las de un par

```python
broker.cancel_open_orders(market="spot", symbol=None) -> list[str]
```

Devuelve los ids que canceló, no un booleano — así el llamador puede registrar
exactamente qué se tocó.

```python
cancelados = broker.cancel_open_orders(market="futures", symbol="BTCUSDT")
print(f"cancelé {len(cancelados)}: {cancelados}")
```

Ninguno de los dos exchanges ofrece un "cancel all" de cuenta completa, así que
por dentro se listan primero y se cancelan por id. Ese listado previo es
obligatorio de todos modos y además deja el registro exacto.

---

## Órdenes OCO

Un OCO coloca TP y SL a la vez, y el exchange cancela la pierna que no se
ejecutó.

```python
if broker.supports_oco:
    oco_id, tp_ack, sl_ack = broker.place_oco_order(
        symbol="BTCUSDT",
        side=Side.SELL,             # ← el lado OPUESTO a la entrada
        quantity=0.01,
        take_profit_price=52000,
        stop_price=49000,
        stop_limit_price=48900,     # ligeramente peor que stop_price
    )
```

| Broker | `supports_oco` |
|---|---|
| Binance | ✅ nativo |
| Bitunix | ❌ **levanta** |
| PaperBroker | ✅ simulado |

!!! warning "Comprueba `supports_oco` ANTES de colocar la entrada"
    Antes de que existiera este flag, un motor en modo OCO contra Bitunix
    reventaba con `NotImplementedError` **en el instante del fill** — con la
    posición ya abierta y sin protección. El peor momento posible.

En Bitunix la alternativa es el TP/SL **adjunto** a la entrada, que además es
atómico. Ver [Take profit y stop loss](tp-sl.md).

Otros métodos: `cancel_oco(symbol, oco_list_id)`,
`update_oco(entry, new_tp, new_sl, new_sl_limit)`.

---

## Referencia rápida

| Operación | Método |
|---|---|
| Crear | `place_order(OrderRequest)` |
| Listar abiertas | `get_open_orders(market, symbol)` |
| Detalle por id | `get_order(symbol, order_id, market)` |
| Solo el estado | `get_order_status(symbol, order_id, market)` |
| Editar | `update_order(symbol, order_id, *, price, quantity, ...)` |
| Cancelar una | `cancel_order(symbol, order_id, market)` |
| Cancelar todas | `cancel_open_orders(market, symbol)` |
| OCO | `place_oco_order(...)`, `cancel_oco(...)`, `update_oco(...)` |
