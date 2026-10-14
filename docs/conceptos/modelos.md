# Modelos de dominio

Todos viven en `luarix_exchange.models`. Son `dataclass` de stdlib: sin
dependencias, serializables, comparables e imprimibles en un cuaderno.

## Mapa rápido

| Modelo | Qué es | Dónde aparece |
|---|---|---|
| `OrderRequest` | La instrucción que se **manda** | entrada de `place_order` |
| `OrderAck` | Lo que el exchange respondió **al colocar** | salida de `place_order`, `update_order`, `cancel_order` |
| `OrderInfo` | Lo que el exchange dice de la orden **después** | salida de `get_order`, `get_open_orders` |
| `Fill` | Una **ejecución** real | salida de `place_order` |
| `BrokerPosition` | Una posición abierta | salida de `get_positions` |
| `Candle` | Una vela | salida de `get_candles` |
| `Side` | `BUY` / `SELL` | en todas partes |
| `OrderType` | `MARKET` / `LIMIT` / `STOP_LOSS_LIMIT` | en `OrderRequest` |
| `OrderStatus` | `NEW` / `FILLED` / `CANCELED`... | en `OrderAck`, `OrderInfo` |

---

## `OrderRequest`

`luarix_exchange.models.orders.OrderRequest` — inmutable (`frozen=True`).

La **instrucción ejecutable**: los datos exactos que se envían al exchange.

```python
from luarix_exchange.models.orders import OrderRequest, OrderType, TimeInForce
from luarix_exchange.models.orders import Side

orden = OrderRequest(
    symbol="BTCUSDT",
    side=Side.BUY,
    quantity=0.01,
    order_type=OrderType.LIMIT,
    market="futures",
    price=60000.0,
    tp_price=63000.0,
    sl_price=58000.0,
)
```

| Campo | Tipo | Por defecto | Qué es |
|---|---|---|---|
| `symbol` | `str` | — | Par ("BTCUSDT") |
| `side` | `Side` | — | `BUY` o `SELL` |
| `quantity` | `float` | — | Se redondeará al `step_size` del símbolo |
| `order_type` | `OrderType` | `LIMIT` | Ver abajo |
| `market` | `str` | `"spot"` | `"spot"` o `"futures"` |
| `price` | `float \| None` | `None` | Obligatorio para `LIMIT` y `STOP_LOSS_LIMIT` |
| `stop_price` | `float \| None` | `None` | Precio de activación. Solo `STOP_LOSS_LIMIT` |
| `time_in_force` | `TimeInForce \| None` | `None` | `GTC` si no se pasa |
| `tp_price` | `float \| None` | `None` | Take-profit **adjunto** a esta orden |
| `sl_price` | `float \| None` | `None` | Stop-loss **adjunto** a esta orden |
| `reduce_only` | `bool` | `False` | ¿Esta orden **cierra** una posición? |
| `client_order_id` | `str` | auto `tb-<uuid>` | Permite rastrearla si se pierde el `orderId` |
| `strategy_name` | `str \| None` | `None` | Trazabilidad |
| `metadata` | `dict` | `{}` | Libre |

!!! warning "`market` por defecto es `\"spot\"`"
    Antes este campo no existía y **todo** se colocaba en spot, incluso con el
    grafo configurado en futuros: el selector solo llegaba a las consultas de
    cuenta, no a la colocación. El default conserva el comportamiento anterior
    para llamadores viejos, no porque spot sea lo más probable.

!!! danger "`reduce_only` no es cosmético"
    En un exchange en modo cobertura (HEDGE), una venta marcada como apertura
    **no reduce el largo abierto**: abre un corto independiente que convive con
    él. Ver [Spot y futuros](mercados.md#modo-cobertura-hedge-vs-neteo-one_way).

### `OrderType`

| Valor | Qué hace | Requiere |
|---|---|---|
| `MARKET` | Se ejecuta al precio actual | — |
| `LIMIT` | Se ejecuta solo al llegar al precio | `price`, `time_in_force` |
| `STOP_LOSS_LIMIT` | Al cruzar `stop_price`, coloca una LIMIT a `price` | `price` + `stop_price` |

!!! warning "Bitunix futuros no acepta `STOP_LOSS_LIMIT`"
    Sus únicos tipos colocables son `LIMIT` y `MARKET` — verificado contra la
    API (`code=10008 ... alternative value ["ALL","LIMIT","MARKET"]`). El stop
    va **adjunto** a la orden con `sl_price`, nunca como orden propia. Mandar
    `STOP_LOSS_LIMIT` allí dejaba la entrada abierta y **sin stop**, y el
    traceback no lo decía en ningún sitio. Ahora se rechaza en local con el
    motivo explícito.

### `TimeInForce`

| Valor | Qué hace |
|---|---|
| `GTC` | Good-Till-Canceled. Queda activa hasta ejecutarse o cancelarse. El más común |
| `IOC` | Immediate-Or-Cancel. Ejecuta lo que pueda ya, cancela el resto |
| `FOK` | Fill-Or-Kill. Completa e inmediata, o nada |

---

## `OrderAck`

Confirmación de que el exchange **recibió** (o rechazó) una orden. **No
garantiza que se ejecutó** — para eso hay que mirar `status`.

```python
ack, fills = broker.place_order(orden)
ack.exchange_order_id   # el id del exchange -- guárdalo
ack.status              # OrderStatus.NEW normalmente
ack.raw                 # respuesta completa, para depurar
```

| Campo | Tipo | Qué es |
|---|---|---|
| `exchange_order_id` | `str \| None` | El id del exchange. `None` en test orders |
| `client_order_id` | `str \| None` | El que asignamos nosotros (prefijo `tb-`) |
| `symbol` | `str \| None` | Par |
| `side` | `Side \| None` | |
| `status` | `OrderStatus \| None` | Estado en el momento de la respuesta |
| `oco_list_id` | `str \| None` | Id del OCO al que pertenece, si aplica |
| `raw` | `dict` | Respuesta completa de la API |

---

## `OrderInfo`

El estado **completo** de una orden que ya vive (o vivió) en el exchange.
Salida de `get_order()` y `get_open_orders()`.

Existe porque los dos brokers describen lo mismo con nombres distintos:

| Concepto | Binance | Bitunix futuros | Bitunix spot |
|---|---|---|---|
| id | `orderId` | `orderId` | `id` |
| cantidad | `origQty` | `qty` | `volume` |
| ya ejecutado | `executedQty` | `tradeQty` | `dealVolume` |
| lado | `"BUY"`/`"SELL"` | `"BUY"`/`"SELL"` | `1` / `2` |
| parcial | `PARTIALLY_FILLED` | `PART_FILLED` | `PART_FILLED` |

```python
info = broker.get_order("BTCUSDT", order_id, market="futures")

info.status               # OrderStatus.PARTIALLY_FILLED
info.is_open              # True (NEW o PARTIALLY_FILLED)
info.quantity             # 2.2
info.filled_quantity      # 1.1
info.remaining_quantity   # 1.1
info.average_price        # precio medio REAL de lo ejecutado
info.tp_price             # protección adjunta, si la lleva
info.raw                  # el dict original del exchange
```

| Campo | Tipo | Notas |
|---|---|---|
| `order_id` | `str` | |
| `symbol` | `str` | |
| `status` | `OrderStatus` | Ya traducido |
| `side` | `Side \| None` | Acepta `"BUY"` y el `1`/`2` de Bitunix spot |
| `order_type` | `str \| None` | Tal cual lo dice el exchange |
| `quantity` | `float` | Cantidad pedida |
| `filled_quantity` | `float` | Cantidad ejecutada |
| `price` | `float \| None` | Precio límite |
| `average_price` | `float \| None` | Precio medio real |
| `stop_price` | `float \| None` | |
| `tp_price` / `sl_price` | `float \| None` | Protección adjunta (Bitunix) |
| `client_order_id` | `str \| None` | |
| `reduce_only` | `bool` | `True` si `tradeSide == "CLOSE"` |
| `market` | `str` | |
| `created_at` | `datetime \| None` | UTC |
| `raw` | `dict` | El original, entero |

**Propiedades**: `is_open` (`NEW` o `PARTIALLY_FILLED`), `remaining_quantity`.

!!! info "Un `0` en un precio significa \"no aplica\", no \"cero\""
    Los dos exchanges usan el cero para campos que no aplican: una `MARKET` de
    Binance responde `price: "0.00000000"`, y una orden de Bitunix sin stop
    responde `slPrice: "0"`. `OrderInfo` los normaliza a **`None`**.

    Devolver `0.0` haría creer que hay un stop puesto en cero, que es un nivel
    real y catastrófico.

### `OrderStatus`

| Valor | Qué significa |
|---|---|
| `NEW` | Aceptada, sin ejecutar |
| `PARTIALLY_FILLED` | Ejecutada en parte |
| `FILLED` | Ejecutada del todo |
| `CANCELED` | Cancelada (por el usuario o por TIF) |
| `REJECTED` | El exchange la rechazó |
| `TAKE_PROFIT_ACTIVATED` | Legado del motor de OCO |
| `STOP_LOSE_ACTIVATED` | Legado del motor de OCO |

Un estado que no esté en la tabla se traduce a **`NEW`**, no a `FILLED`. Es
conservador a propósito: dar por cerrada una orden que sigue viva llevaría a
colocar un TP/SL contra una posición inexistente.

---

## `BrokerPosition`

`luarix_exchange.models.brokers.BrokerPosition` — una posición abierta,
con la misma forma venga de donde venga.

```python
for p in broker.get_positions(market="futures"):
    print(p.symbol, p.side, p.quantity, p.entry_price, p.unrealized_pnl)
```

| Campo | Tipo | Notas |
|---|---|---|
| `symbol` | `str` | |
| `side` | `str` | `"LONG"` o `"SHORT"` |
| `quantity` | `float` | **Siempre positiva** |
| `entry_price` | `float` | Precio medio de entrada |
| `mark_price` | `float` | Precio de marca |
| `unrealized_pnl` | `float` | No realizado |
| `leverage` | `float \| None` | |
| `margin_mode` | `str \| None` | `"cross"` / `"isolated"` |
| `position_id` | `str \| None` | Solo Bitunix y MT5 |
| `market` | `str` | |
| `raw` | `dict` | El original |

**Propiedad**: `notional` = `quantity × mark_price` (o `entry_price` si no hay
marca).

!!! danger "Por qué `quantity` es siempre positiva"
    Binance **firma** la cantidad (`positionAmt` negativo = corto). Bitunix no:
    manda `qty` sin signo y la dirección aparte en `side`.

    Sin normalizar esto, `qty > 0` significaría cosas distintas según el
    exchange, y un corto leído como largo es exactamente el tipo de error que
    solo se descubre operando en real.

    Los tres constructores (`from_binance_futures`, `from_bitunix_futures`,
    `from_spot_balance`) están cubiertos por
    `tests/test_broker_contract.py::BrokerPositionTests`.

!!! note "En spot no hay posiciones"
    Hay **saldos**. Se representan igual (`side="LONG"`, `entry_price=0.0`)
    para que listar "lo que tengo abierto" no obligue a ramificar por mercado,
    pero `market="spot"` lo deja claro. Las monedas de cotización (USDT, USDC,
    BUSD, FDUSD, TUSD, DAI) se excluyen: tenerlas es tener el efectivo, no
    estar dentro de una posición.

---

## `Fill`

Una **ejecución real**. Una orden puede generar varios: una `MARKET` de 1 BTC
puede ejecutarse a 50 000 y el resto a 50 001.

| Campo | Tipo |
|---|---|
| `symbol`, `side`, `quantity`, `price` | obligatorios |
| `fee` | `float` (0.0) |
| `fee_asset` | `str \| None` — moneda de la comisión ("BNB", "USDT") |
| `client_order_id`, `exchange_order_id`, `fill_id` | `str \| None` |
| `timestamp` | `datetime` (UTC, auto) |

**Propiedad**: `notional` = `quantity × price`.

!!! info "Las órdenes de futuros no devuelven fills al colocar"
    `place_order` en futuros devuelve `(ack, [])`. Los fills llegan después,
    consultando la orden (`get_order().filled_quantity`) o por el stream de
    ejecución. En spot con `MARKET`, sí vienen en la respuesta.

---

## `Candle`

`luarix_exchange.models.market.Candle` — inmutable.

| Campo | Tipo |
|---|---|
| `symbol`, `timeframe` | `str` |
| `open_time` | `datetime` (UTC) |
| `open`, `high`, `low`, `close`, `volume` | `float` |
| `close_time` | `datetime \| None` |
| `is_closed` | `bool` (`True`) |

**Constructores**: `Candle.from_binance(msg)` (mensaje de websocket),
`Candle.from_close(...)`.

!!! warning "Las velas EN CURSO se descartan"
    Los dos exchanges incluyen la vela en formación, con valores parciales.
    El paquete la quita: una vela parcial marcada `is_closed=True` es de las
    peores cosas que puede tragarse un indicador, porque su OHLC **cambia entre
    dos consultas del mismo instante**.

---

## `Side`

```python
from luarix_exchange.models.orders import Side
Side.BUY    # "BUY"
Side.SELL   # "SELL"
```

En futuros, `BUY` abre un largo (o cierra un corto) y `SELL` abre un corto (o
cierra un largo). Cuál de las dos cosas depende de `reduce_only` y del modo de
la cuenta — ver [Spot y futuros](mercados.md#modo-cobertura-hedge-vs-neteo-one_way).

---

## Modelos de apoyo

| Modelo | Módulo | Para qué |
|---|---|---|
| `TradeIntent` | `orders` | La "idea" de un trade, con riesgo. **No** es una instrucción al exchange |
| `OrderPlan` | `orders` | Entradas escalonadas (DCA / pirámide) |
| `Entry` | `orders` | Una entrada viva con su TP/SL y OCO asociados |
| `BrokerType`, `Market` | `brokers` | Enums auxiliares |

!!! note "Lo que NO está en esta librería"
    `Signal`, `Portfolio`/`Position`, `MarketSnapshot`, `Trend`, `Fractal`,
    `RiskResult`, `PipelineConfig` y `BacktestStats` son modelos de
    **estrategia**, no de exchange: viven en `luarix-trading-framework`.

!!! note "`Position` y `BrokerPosition` no son lo mismo"
    `Position` (de `luarix-trading-framework`) es contabilidad **interna** del `Portfolio`:
    cantidad y precio medio para calcular P&L. `BrokerPosition` es lo que el
    **exchange** dice que tienes abierto. Nombres parecidos, papeles distintos.
