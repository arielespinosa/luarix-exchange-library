# Take profit y stop loss

La parte más delicada del paquete. Es donde han ocurrido los dos incidentes más
graves de su historia, y los dos exchanges lo resuelven de forma
**incompatible**.

## Las tres formas de proteger una posición

| Forma | Cómo | Atómico | Dónde |
|---|---|---|---|
| **Adjunta** | El TP/SL viajan **dentro** de la orden de entrada | ✅ | Bitunix futuros (nativo), Binance futuros (emulado) |
| **OCO** | Una llamada coloca las dos patas; el exchange cancela la sobrante | ⚠️ tras el fill | Binance spot |
| **Independiente** | Tres órdenes propias, gestionadas por el motor | ❌ | Cualquiera |

**Si puedes usar la adjunta, úsala.** Es la única en la que la posición nunca
llega a existir sin protección.

---

## Protección adjunta (recomendada)

```python
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

precio = broker.get_ticker("BTCUSDT", market="futures")

orden = OrderRequest(
    symbol="BTCUSDT",
    side=Side.BUY,
    quantity=0.01,
    order_type=OrderType.LIMIT,
    price=round(precio * 0.98, 2),
    market="futures",
    tp_price=round(precio * 1.05, 2),   # objetivo
    sl_price=round(precio * 0.95, 2),   # stop
)

ack, _ = broker.place_order(orden)
```

Una sola llamada. La orden llega al exchange **ya con su protección**.

### Cómo lo resuelve cada broker

=== "Bitunix futuros"

    **Nativo.** `tp_price`/`sl_price` viajan como `tpPrice`/`slPrice` en el
    mismo cuerpo de `place_order`. El disparo usa `LAST_PRICE` (el último
    precio negociado, que es lo que ve el usuario en el gráfico) y no
    `MARK_PRICE`, que dispararía con el precio de marca y sorprendería a
    cualquiera que haya puesto el nivel mirando la vela.

    Verificado contra la API: `tpPrice`/`tpStopType` y `slPrice`/`slStopType`
    se aceptan **dentro** de `place_order`.

=== "Binance futuros"

    **Emulado, pero seguro.** Binance no admite adjuntarlos, así que se colocan
    acto seguido como órdenes propias:

    ```
    LIMIT               BUY  qty=0.01 price=58800  timeInForce=GTC
    TAKE_PROFIT_MARKET  SELL stopPrice=63000  closePosition=true
    STOP_MARKET         SELL stopPrice=57000  closePosition=true
    ```

    La clave es `closePosition=true`: cierra la posición **entera** al
    dispararse, no hace falta repetir la cantidad y —esto es lo importante—
    **no puede abrir nada por sí misma**. Esa es la garantía que faltaba en el
    equivalente de Bitunix y que causó el incidente de abajo.

    !!! warning "Si el TP/SL fallan, la entrada YA está colocada"
        Se propaga el error (quien llama tiene que enterarse de que la posición
        quedó sin proteger), pero el `OrderAck` de la entrada no se pierde: va
        en el mensaje de la excepción.

=== "Bitunix spot / Binance spot"

    **No soportado.** Un broker que no soporte `tp_price`/`sl_price` debe
    **ignorarlos**, no inventarse una orden con ellos. En spot hay que usar OCO
    (Binance) o gestionar las dos patas a mano.

---

## El incidente: el take-profit abría un corto real

!!! danger "Caso real, cuenta Bitunix futuros en modo HEDGE"
    Estrategia LONG, nodo de orden LONG, y en el exchange apareció **un corto de
    LTC**:

    ```
    entry        BUY  LIMIT 44.92    exch_id=2082852993787334656   ← largo correcto
    take_profit  SELL LIMIT 45.3692  exch_id=2082854548489949184   ← esto abrió el corto
    ```

    **Causa.** El motor colocaba el TP/SL como órdenes normales del lado
    opuesto, y `BitunixFuturesClient.place_order` ponía `tradeSide="OPEN"`
    **fijo** a toda orden en modo HEDGE. Un `SELL` + `OPEN` en cobertura no
    cierra el largo: **abre un corto independiente**. En ONE_WAY el neteo del
    exchange lo tapaba, por eso nunca se había visto.

    **Segundo bug del mismo camino.** El SL se mandaba con
    `orderType="STOP_LOSS_LIMIT"` (un tipo de Binance). Futuros de Bitunix **no
    tiene tipos de stop**: `code=10008 ... alternative value
    ["ALL","LIMIT","MARKET"]`. O sea que el stop-loss **nunca se colocaba** — la
    posición quedaba abierta y sin protección, y el traceback no lo decía en
    ninguna parte.

**Las tres defensas que quedaron**, en capas:

1. **TP/SL adjuntos a la entrada** (`tp_price`/`sl_price`). Elimina las dos
   órdenes sueltas del lado contrario, que eran la fuente del corto.
2. **`reduce_only=True`** en cualquier salida. Se mapea a `tradeSide="CLOSE"`.
   Defensa en profundidad: aunque la ruta de órdenes independientes se ejecute
   contra un broker real, cierra en vez de abrir.
3. **Rechazo local de tipos no colocables.** `BitunixFuturesClient.place_order`
   valida `orderType` en local, con el motivo explícito, en vez de dejar que la
   API devuelva un `10008` opaco:

   ```pycon
   BitunixApiError: Bitunix futuros no puede colocar una orden de tipo
   'STOP_LOSS_LIMIT' (solo acepta LIMIT, MARKET). Un stop-loss se adjunta a la
   orden con `sl_price`, no se coloca por separado.
   ```

---

## OCO (Binance spot)

Coloca las dos patas de una vez; el exchange cancela la que no se ejecutó.

```python
oco_id, tp_ack, sl_ack = broker.place_oco_order(
    symbol="BTCUSDT",
    side=Side.SELL,             # ← OPUESTO al de la entrada
    quantity=0.01,
    take_profit_price=52000,    # LIMIT
    stop_price=49000,           # activación del stop
    stop_limit_price=48900,     # límite de la stop ya activada
)
```

!!! important "El `side` es el OPUESTO al de la entrada"
    - Entraste con `BUY` → `side=Side.SELL` para cerrar.
    - Entraste con `SELL` → `side=Side.BUY` para cerrar.

### `stop_price` vs `stop_limit_price`

| | Qué es |
|---|---|
| `stop_price` | Precio de **activación**. Cuando el mercado llega aquí, se activa la orden STOP |
| `stop_limit_price` | Precio **límite** de la orden STOP ya activada |

`stop_limit_price` debe ser **ligeramente peor** que `stop_price` para asegurar
ejecución:

- Para `SELL`: `stop_limit_price < stop_price` (SL en 49000, limit en 48900).
- Para `BUY`: `stop_limit_price > stop_price`.

Si son iguales o el límite es mejor, el stop puede activarse y **no llenar**,
dejando la posición abierta con el mercado corriendo en contra.

### Cuándo llamarlo

**Después** de que la entrada se haya ejecutado:

```python
ack, fills = broker.place_order(entrada)
if ack.status == OrderStatus.FILLED:
    oco_id, tp_ack, sl_ack = broker.place_oco_order(...)
```

Ahí está la ventana: entre el fill y el OCO, la posición está desprotegida. Es
lo que la protección adjunta elimina.

### Comprueba `supports_oco` antes

```python
if not broker.supports_oco:
    ...   # usa tp_price/sl_price adjuntos, o gestiona las dos patas
```

Bitunix **levanta**:

```pycon
>>> bitunix.place_oco_order(...)
NotImplementedError: Bitunix no tiene OCO nativo. En futuros el TP/SL van
ADJUNTOS a la orden de entrada (`OrderRequest.tp_price`/`sl_price`), que además
es atómico; en spot hay que gestionarlos como dos órdenes y cancelar la hermana
al llenar una.
```

### Modificar y cancelar un OCO

```python
broker.cancel_oco("BTCUSDT", oco_list_id)
oco_id, tp, sl = broker.update_oco(entry, new_tp, new_sl, new_sl_limit)
```

`update_oco` cancela el OCO actual y crea uno nuevo con los valores nuevos.

---

## Órdenes independientes

Tres órdenes propias: entrada, TP y SL, con el motor cancelando la hermana
cuando una llena. Es el camino cuando no hay ni adjunta ni OCO.

```python
if broker.supports_independent_orders:
    ...
```

!!! danger "Marca SIEMPRE las salidas con `reduce_only=True`"
    ```python
    tp = OrderRequest(
        symbol="LTCUSDT", side=Side.SELL, quantity=2.2,
        order_type=OrderType.LIMIT, price=45.37,
        market="futures",
        reduce_only=True,       # ← sin esto, en HEDGE abres un corto
    )
    ```

Es exactamente el camino que causó el incidente de arriba. Funciona, pero es el
menos seguro de los tres: entre el fill de la entrada y la colocación del stop
hay una ventana real.

---

## Modificar un TP/SL ya colocado

=== "Bitunix futuros (adjunto)"

    Son campos de la propia orden:

    ```python
    broker.update_order("LTCUSDT", entry_id, sl_price=44.50, market="futures")
    ```

    Enmienda **nativa**: la orden nunca deja de existir, así que mover el stop
    no abre una ventana de desprotección.

=== "Binance futuros (órdenes propias)"

    El TP y el SL tienen su **propio** `order_id`. Se modifican como cualquier
    otra orden:

    ```python
    broker.update_order("BTCUSDT", sl_order_id, stop_price=57000, market="futures")
    ```

    Pasar `sl_price=` a la orden de **entrada** levanta `ValueError`, en vez de
    no hacer nada y dejarte creer que moviste el stop.

=== "OCO"

    ```python
    broker.update_oco(entry, new_tp, new_sl, new_sl_limit)
    ```

    Cancela y recrea. **No es atómico.**

---

## Errores que ya han pasado, para no repetirlos

| Error | Consecuencia real |
|---|---|
| TP/SL como órdenes del lado opuesto sin `reduce_only` en HEDGE | **Abrió un corto real** en vez de cerrar el largo |
| `orderType="STOP_LOSS_LIMIT"` en Bitunix futuros | El stop **nunca se colocó**; posición abierta y sin protección |
| Colocar TP/SL sin verificar `supports_oco` | `NotImplementedError` **en el instante del fill**, con la posición ya abierta |
| Cancelar un TP de futuros con `market="spot"` en Bitunix | **OK falso**: no encuentra nada y devuelve éxito |
| Confiar en que "ausente de pendientes" = llenada | TP/SL colocado contra una posición **inexistente** (`10002 Parameter error`) |
| TP y SL invertidos en la rama SHORT de un grafo | Entrada `SELL` a 45.30 con TP en **45.53** (pérdida garantizada) y SL en 44.85 |

Todos están reconstruidos con datos reales en `docs/plans/bugs.md`.

---

## Checklist antes de operar con protección

- [ ] ¿`market="futures"` está puesto en el `OrderRequest`?
- [ ] Si es una **salida**: ¿`reduce_only=True`?
- [ ] ¿La cuenta está en HEDGE o en ONE_WAY? (`futures.position_mode()`)
- [ ] Si usas OCO: ¿comprobaste `broker.supports_oco`?
- [ ] Si usas OCO: ¿`stop_limit_price` es **peor** que `stop_price` en la
      dirección correcta?
- [ ] ¿El TP está en la dirección buena? (`SELL` → TP **por debajo** de la
      entrada, SL por encima)
- [ ] Tras colocar: ¿`get_order()` confirma `tp_price`/`sl_price` en el
      exchange?
