# Balance y posiciones

Todo lo de esta página va **firmado**. Si falla con `10004 Invalid IP`
(Bitunix) o `-2015` (Binance) mientras las velas funcionan, el problema es la
key o la whitelist de IP — ver [Errores](errores.md).

---

## Balance

```python
broker.get_balance(market="spot", asset=None) -> dict[str, float]
```

### Qué significa exactamente "saldo"

**El disponible, no el total.** Lo bloqueado en órdenes abiertas o retenido
como margen de una posición **no cuenta**.

Es la respuesta a *"¿con cuánto puedo abrir ahora mismo?"*, que es la pregunta
que hace una estrategia antes de entrar. Y significa lo mismo en los cuatro
casos, que es lo que permite compararlos:

| Broker / mercado | Campo que se lee | Qué queda fuera |
|---|---|---|
| Binance spot | `free` | `locked` |
| Binance futuros | `availableBalance` | margen de posiciones abiertas |
| Bitunix spot | `balance` | `balanceLocked` |
| Bitunix futuros | `available` | `frozen`, `margin`, `bonus`, `crossUnrealizedPNL` |

### Toda la cuenta

```python
saldo = broker.get_balance(market="futures")
# {'USDT': 1500.0}
```

En Binance spot esto devuelve **~400 activos**, casi todos a cero. Fíltralos:

```python
{k: v for k, v in saldo.items() if v > 0}
```

### Filtrado por activo

```python
broker.get_balance(market="futures", asset="USDT")
# {'USDT': 1500.0}
```

!!! important "Un activo pedido y ausente vale `0.0`, no desaparece"
    ```python
    broker.get_balance("spot", asset="DOGE")
    # {'DOGE': 0.0}    ← NO {}
    ```

    *"No tienes DOGE"* y *"no pregunté por DOGE"* son respuestas distintas, y
    quien llama no debería tener que distinguirlas con un `if 'DOGE' in saldo`.

!!! note "En los futuros de Bitunix el filtro **es** la consulta"
    Su cuenta se pregunta por **moneda de margen**, de una en una. Pedir
    `asset="USDC"` hace una llamada distinta, no recorta el resultado de otra.

    ```python
    broker.get_balance("futures")                 # USDT (la de por defecto)
    broker.get_balance("futures", asset="USDC")   # otra llamada
    ```

### Las dos bolsas son independientes

```python
for m in ("spot", "futures"):
    print(m, broker.get_balance(market=m, asset="USDT"))
```

Tener 1000 USDT en spot no te deja abrir nada en futuros hasta que los
transfieras. El paquete **no** hace transferencias entre cuentas.

---

## Posiciones

```python
broker.get_positions(market="spot", symbol=None) -> list[BrokerPosition]
```

```python
for p in broker.get_positions(market="futures"):
    print(f"{p.symbol:12} {p.side:5} qty={p.quantity:<10} "
          f"entrada={p.entry_price:<10} marca={p.mark_price:<10} "
          f"pnl={p.unrealized_pnl:<8} x{p.leverage}")
```

### `BrokerPosition`

| Campo | Notas |
|---|---|
| `symbol` | |
| `side` | `"LONG"` o `"SHORT"` |
| `quantity` | **Siempre positiva** |
| `entry_price` | Precio medio de entrada |
| `mark_price` | Precio de marca |
| `unrealized_pnl` | No realizado |
| `leverage` | `float \| None` |
| `margin_mode` | `"cross"` / `"isolated"` |
| `position_id` | Solo Bitunix y MT5 |
| `market` | |
| `raw` | El dict original del exchange |
| `notional` | *(propiedad)* `quantity × mark_price` |

!!! danger "Por qué `quantity` es siempre positiva"
    Binance **firma** la cantidad (`positionAmt` negativo = corto). Bitunix
    manda `qty` sin signo y la dirección aparte en `side`.

    Sin normalizar esto, `qty > 0` significaría cosas distintas según el
    exchange. **Un corto leído como largo** es el tipo de error que solo se
    descubre operando en real.

    Los dos describen el mismo corto de 2 BTC así:

    ```python
    # Binance
    {"symbol": "BTCUSDT", "positionAmt": "-2.0", "positionSide": "BOTH", ...}
    # Bitunix
    {"symbol": "BTCUSDT", "qty": "2.0", "side": "SELL", ...}
    ```

    Los dos salen como `side="SHORT", quantity=2.0`.

### Filtrado por par

```python
broker.get_positions(market="futures", symbol="BTCUSDT")
```

### Solo las abiertas

Los dos exchanges devuelven también los pares con `qty=0` (todos aquellos con
los que la cuenta ha operado alguna vez). El paquete los descarta: listarlos
sería ruido.

### En spot no hay posiciones

Hay **saldos de activos**. Se representan igual (`side="LONG"`,
`entry_price=0.0`) para que listar "lo que tengo abierto" no obligue a
ramificar por mercado:

```python
broker.get_positions(market="spot")
# [BrokerPosition(symbol='BTCUSDT', side='LONG', quantity=0.5, market='spot', ...)]
```

El `symbol` se construye pegando la moneda de cotización al activo
(`BTC` → `BTCUSDT`) para que el campo signifique lo mismo que en futuros y se
pueda pedir su precio sin más traducción.

!!! note "Las monedas de cotización se excluyen"
    `USDT, USDC, BUSD, FDUSD, TUSD, DAI`. Tenerlas es tener el **efectivo**, no
    estar dentro de una posición. Si quieres el efectivo, usa `get_balance`.

### Acceder a campos que no expone el modelo

`raw` conserva el diccionario original entero:

```python
p = broker.get_positions(market="futures")[0]
p.raw            # {'symbol': 'BTCUSDT', 'positionAmt': '-2.0', 'liquidationPrice': ..., ...}
p.raw.get("liquidationPrice")
```

!!! tip "Primer sitio donde mirar si algo sale a cero"
    Si una posición sale con `entry_price=0.0` o `leverage=None`, mira `.raw`.
    Los nombres de campo de `pending_positions` de Bitunix salen de su
    documentación y **no se han verificado contra la cuenta real** — ver
    [Estado](../estado.md). Si alguno no coincide, el normalizador devuelve
    ceros en vez de reventar, y `.raw` te dice cuál es el nombre bueno.

---

## Apalancamiento

```python
broker.set_leverage("BTCUSDT", 5)                 # market="futures" por defecto
```

!!! warning "Es una operación de CUENTA, no de orden"
    Afecta a **todo** lo que se coloque después en ese par, incluido lo que
    tenga abierto otra sesión o el propio usuario desde la app del exchange. No
    es un parámetro de la orden que vayas a colocar a continuación.

| Broker | Endpoint |
|---|---|
| Binance | `futures_change_leverage` |
| Bitunix | `POST /api/v1/futures/account/change_leverage` |

En spot levanta:

```pycon
>>> broker.set_leverage("BTCUSDT", 5, market="spot")
NotImplementedError: El apalancamiento solo existe en la cuenta de futuros.
```

Y en un broker sin futuros:

```pycon
>>> PaperBroker().set_leverage("BTCUSDT", 5)
NotImplementedError: PaperBroker no expone datos de cuenta.
```

---

## Equity de una cuenta simulada

El `PaperBroker` tiene un método propio para el capital **real** (efectivo más
el valor de mercado de lo abierto):

```python
broker.mark_to_market_equity("BTCUSDT", precio_actual)
```

!!! danger "`get_balance()['USDT']` NO es el capital, es la caja"
    El simulador opera al contado: **vender acredita el importe en USDT en el
    acto** y comprar lo debita. Una posición corta todavía abierta deja la caja
    inflada con el producto de una venta que aún no se ha recomprado.

    Caso real: una corrida terminó con un corto abierto de 0,045 BTC @ 66 699 e
    informaba `final_capital = 12 998` sobre 10 000 iniciales — un +30% y un APR
    del 198% con **cero operaciones cerradas**. El equity real era 9 997.

    Ver [PaperBroker](../brokers/paper.md).
