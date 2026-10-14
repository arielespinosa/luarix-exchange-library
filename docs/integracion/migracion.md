# Guía de migración

Cambios que rompen código existente, con el antes y el después.

---

## De `luarix-trading-framework` a `luarix-exchange-library` (2026-10-14)

Todo lo que habla con un exchange salió de `luarix-trading-framework` a esta
librería. El código es **el mismo**; cambian las rutas de importación.

| Antes (`luarix_trading`) | Ahora (`luarix_exchange`) |
|---|---|
| `luarix_trading.brokers.*` (todo: `base`, `factory`, `credentials`, `paper`, `binance`, `bitunix`, `mt5`, `account_data.*`, `market_data.*`) | `luarix_exchange.brokers.*` (misma estructura) |
| `luarix_trading.core.models.orders` | `luarix_exchange.models.orders` |
| `luarix_trading.core.models.brokers` | `luarix_exchange.models.brokers` |
| `luarix_trading.core.models.market.Candle` | `luarix_exchange.models.market.Candle` |
| `luarix_trading.core.models.signals.Side` | `luarix_exchange.models.orders.Side` |

Lo que **se queda** en `luarix-trading-framework`, porque es estrategia y no
exchange: `core.stats` (`BacktestStats`), `core.sizing` (`calc_quantity`),
`core.models.signals.Signal`, `core.models.risks`, `core.models.portfolio`,
`core.models.pipeline`, y `Trend`/`Fractal`/`MarketSnapshot`/`Market` de
`core.models.market`.

!!! info "Las rutas viejas siguen funcionando, por ahora"
    `luarix-trading-framework` depende de esta librería y conserva sus módulos
    `luarix_trading.brokers.*` y `luarix_trading.core.models.{orders,brokers}`
    como **alias**: importarlos devuelve el MISMO módulo de aquí (no una copia),
    así que `isinstance`, los enums y los `mock.patch` siguen funcionando. Es un
    puente para que `luarix-orchestator` y `luarix-web-server` migren sin prisa;
    se quitará cuando los dos importen `luarix_exchange` directamente.

!!! danger "El nombre de distribución NO es el de importación"
    Se instala como `luarix-exchange-library` y se importa como
    `luarix_exchange`. En `luarix-web-server` un *find-and-replace* que confundió
    los dos (con el paquete anterior) corrompió palabras dentro del código:
    `ranges.append(...)` acabó como `ranges.luarix_trading_frameworkend(...)`.
    Reemplaza la ruta completa (`luarix_trading.brokers` →
    `luarix_exchange.brokers`), nunca una subcadena suelta.

---

## Revisión del contrato (2026-08-11)

### `get_positions()` — **cambia el tipo de retorno**

```python
# ANTES -- y cada broker devolvía una cosa distinta
broker.get_positions()
# Binance:  {'BTC': 0.5}          ← ¡saldos de SPOT, aunque pidieras futuros!
# Bitunix:  {}                    ← FIJO, siempre vacío
# Paper:    {'BTCUSDT': 0.5}

# AHORA
broker.get_positions(market="futures", symbol=None) -> list[BrokerPosition]
```

```python
# Migración: si tu código espera {símbolo: cantidad}
posiciones = {
    p.symbol: (p.quantity if p.side == "LONG" else -p.quantity)
    for p in broker.get_positions(market="futures")
}
```

!!! warning "`quantity` es siempre positiva"
    La dirección va en `side` (`"LONG"` / `"SHORT"`). Binance firmaba la
    cantidad y Bitunix no; normalizarlo era imprescindible. Si tu código
    esperaba cantidades firmadas, aplica el signo tú (fragmento de arriba).

### `get_open_orders()` — **cambia el tipo de retorno**

```python
# ANTES: list[dict] crudo, forma distinta por broker
for o in broker.get_open_orders():
    oid = o.get("orderId") or o.get("id")
    qty = float(o.get("origQty") or o.get("qty") or o.get("volume") or 0)

# AHORA: list[OrderInfo] normalizado
for o in broker.get_open_orders(market="futures"):
    o.order_id, o.quantity, o.filled_quantity, o.side, o.status
    o.raw          # el dict original, entero
```

Si necesitas el dict crudo, está en `.raw`. Y el proveedor sigue devolviéndolo
sin normalizar:

```python
broker.account_data_provider.get_open_orders(market, symbol)   # list[dict]
```

### `update_order()` — firma nueva

```python
# ANTES
broker.update_order(symbol, order_id)          # no aceptaba valores nuevos

# AHORA
broker.update_order(
    symbol, order_id, *,
    price=None, quantity=None, stop_price=None,
    tp_price=None, sl_price=None, market="spot",
) -> OrderAck
```

Sin argumentos, el comportamiento es el de antes. Con argumentos, **modifica de
verdad**.

!!! danger "En spot, el `order_id` CAMBIA"
    No hay enmienda nativa: es cancelar y recolocar. Relee el id del `OrderAck`.

!!! info "En Binance estaba ROTO"
    Referenciaba cuatro variables inexistentes y un método inexistente:
    cancelaba la orden y **acto seguido reventaba con `NameError`**, dejando la
    orden cancelada y sin sustituta. Nadie lo notó porque nada lo llamaba.

### `get_balance()` — parámetro nuevo

```python
broker.get_balance(market="spot", asset=None)
```

Compatible: las llamadas existentes siguen funcionando. Un activo pedido y
ausente devuelve `{asset: 0.0}`, no `{}`.

### `get_ticker()` y `get_order_book()` — parámetro nuevo

```python
broker.get_ticker(symbol, market="spot")
broker.get_order_book(symbol, limit=20, market="spot")
```

Compatibles.

### `AvailableBrokers` — se quitaron dos

```python
# ANTES: PAPER, BINANCE, BITUNIX, KUCOIN, MT5
# AHORA: PAPER, BINANCE, BITUNIX
```

`KUCOIN` y `MT5` no tenían implementación (`case ...: pass`). `MT5Broker` existe
pero no cabe en esa firma: se construye directamente con
`MT5Broker(login=..., password=..., server=...)`.

### `BrokerFactory.get_broker()` — ahora levanta

```python
# ANTES: devolvía None en silencio ante un nombre desconocido
# AHORA: ValueError nombrando los disponibles
```

Y también levanta si no hay credenciales ni en el argumento ni en el entorno.

### `MarketDataProvider` — firmas nuevas

Si tienes un provider propio que hereda de la clase abstracta, **hay que
actualizarlo**:

```python
get_last_candles(symbol, interval="1m", limit=100,
                 market="spot", format=DataFormat.OHLCV)

get_historical_candles(symbol, interval="1m", start=None, end=None,
                       limit=None, format=DataFormat.OHLCV, market="spot")

get_last_price(symbol, market="spot")
get_order_book(symbol, limit=20, market="spot")
```

`async_get_candles` dejó de ser `@abstractmethod` (ninguna implementación lo
cumplía: las tres lo tenían como `pass`, devolviendo `None` en silencio). Ahora
es concreto y levanta.

### `AccountDataProvider` — métodos nuevos

```python
get_balance(market="spot", asset=None)     # ← `asset` nuevo
get_positions(market="spot", symbol=None)  # ← nuevo
get_order(symbol, order_id, market="spot") # ← nuevo
set_leverage(symbol, leverage, market="futures")  # ← nuevo
```

Los tres nuevos tienen default que **levanta**, así que un provider propio sigue
funcionando: solo pierde esas capacidades.

---

## Cambios de comportamiento (sin cambio de firma)

Estos no rompen la compilación, pero **cambian resultados**. Revísalos.

### `get_candles` devuelve exactamente `limit` velas

Antes, Binance devolvía `limit - 1` porque descartaba la vela en curso después
de pedir `limit`. Ahora pide una de más.

### Las velas de Bitunix vienen en el orden correcto

Antes, `klines[:limit]` sobre una respuesta descendente devolvía las `limit`
**más antiguas**, en orden inverso. Con `limit=1`, "la última vela" era la más
vieja disponible.

!!! warning "Si tu código compensaba esto, quítalo"
    Un `reversed(...)` o un `[::-1]` puesto para arreglar el orden ahora lo
    rompe.

### La vela EN CURSO se descarta

Los dos exchanges incluyen la vela en formación. Ahora se quita.

Consecuencia: la última vela puede ser **una menos reciente** que antes, y sus
valores ya no cambian entre dos consultas del mismo instante.

### `get_order_status` en spot de Bitunix ya no infiere

```python
# ANTES: si no encontraba datos -> FILLED
# AHORA: combina ejecuciones + pendientes; si no aparece en ninguno, LEVANTA
```

!!! danger "Esto ya provocó posiciones fantasma"
    Una orden cancelada a mano también desaparece de los pendientes. Darla por
    llenada abría un `Trade` en `ACTIVE` por una posición que nunca existió.

    Si tu código asumía que "no la encuentro" = llenada, **captura
    `BitunixApiError`** y trátalo como "no se pudo determinar", no como un fill.

### `BitunixBroker` ahora respeta el candado de trading real

```python
# Con trading_mode="live" y allow_live_trading=False:
broker.place_order(orden)
# RuntimeError: Live trading is blocked. Set ALLOW_LIVE_TRADING=true to enable.
```

Antes ese candado existía **solo en Binance**. Si tu código construía un
`BitunixBroker` con `trading_mode="live"` sin `allow_live_trading=True` y
esperaba que colocara, ahora falla — correctamente.

### `place_oco_order` de Binance devuelve 3 elementos también al fallar

Antes el camino de error devolvía **dos**, así que un rechazo reventaba en el
desempaquetado del llamador con un `ValueError` que no mencionaba a Binance.

### `BitunixBroker.list_active()` ya no devuelve `[]`

Ahora llama a `get_trading_pairs()`. Si tu código trataba `[]` como "este broker
no opera nada", revísalo.

---

## Migración mínima

Si solo quieres que compile:

```python
# 1. get_positions
posiciones = {p.symbol: p.quantity for p in broker.get_positions(market=MERCADO)}

# 2. get_open_orders
ordenes = [o.raw for o in broker.get_open_orders(market=MERCADO, symbol=simbolo)]

# 3. AvailableBrokers.KUCOIN / .MT5  -> quitar
```

Y después, cuando puedas:

- Propaga `market` a **todo** (es el bug latente más caro).
- Usa `get_order()` en vez de inferir estados de la lista de pendientes.
- Usa `OrderInfo` y `BrokerPosition` en vez de `.raw`, que para eso están.

---

## Verificar la migración

```bash
pytest                    # en este repo: 148 tests
```

Y un humo mínimo en tu repo:

```python
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory

broker = BrokerFactory.get_broker(AvailableBrokers.PAPER)
assert broker.get_positions() == []
assert broker.get_balance() == {"USDT": 10000.0}
print("ok")
```
