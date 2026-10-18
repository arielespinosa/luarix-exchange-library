# Errores y diagnóstico

## El primer paso, siempre: pública contra firmada

Haz **dos llamadas** antes de teorizar:

```python
broker.get_ticker("BTCUSDT", market="futures")     # PÚBLICA
broker.get_balance(market="futures")               # FIRMADA
```

| Resultado | Qué significa |
|---|---|
| Fallan las dos | No hay conexión, o el símbolo no existe |
| Va la pública, falla la firmada | **Credenciales o whitelist de IP** |
| Van las dos | El problema está en otro sitio |

Esa asimetría es la herramienta de diagnóstico más útil que hay con Bitunix,
porque sus klines son públicos y todo lo demás va firmado.

---

## Excepciones del paquete

### `BitunixApiError`

`luarix_exchange.brokers.bitunix_response.BitunixApiError` — Bitunix rechazó la
llamada. El mensaje lleva su `code` y su `msg` tal cual, más una pista cuando
el código es conocido.

!!! info "Bitunix NUNCA responde con un HTTP de error"
    Cuando rechaza una llamada devuelve **200** con:

    ```json
    {"code": "100011", "msg": "Trigger IP risk control rules",
     "data": null, "success": false}
    ```

    El envoltorio es idéntico salga bien o mal; solo cambian `code` y que `data`
    viene en `null`.

    Todos los wrappers hacían `raw.get("data", <default>)`, que **no cubre ese
    caso**: el default de `.get()` solo aplica cuando la clave *falta*, y aquí
    está presente con valor `null`. Resultado: `data` quedaba en `None` y cada
    llamador reventaba más adelante con un error sin relación con la causa
    (`'NoneType' object is not iterable` al pedir el balance) o —peor— seguía
    adelante con datos vacíos como si nada.

    `unwrap()` centraliza el criterio y hace visible el motivo real.

#### Códigos de Bitunix

| Código | Qué es | Qué hacer |
|---|---|---|
| `10004` | **IP no autorizada.** La key SÍ es válida | Añadir la IP a la whitelist (ver abajo) |
| `10003` | API key inexistente o mal copiada | Revisar la clave. ¿Pediste testnet? Bitunix no tiene |
| `100011` | IP no autorizada o credenciales incorrectas | Igual que `10004` |
| `100012` | Credenciales inválidas o key desactivada | Regenerar la key |
| `10002` | Parameter error | Falta un campo, o la precondición de negocio no se cumple |
| `10008` | Valor no permitido en un campo | El mensaje nombra el campo y los valores válidos |
| `30006` | Se pidió un TP/SL sin ninguno de los dos | |
| `300105` | System error | A menudo, símbolo inválido |
| `1` | Error interno del servidor | Reintentar |

#### `10004 Invalid IP`: la IP correcta

```bash
curl https://fapi.bitunix.com/cdn-cgi/trace     # campo ip=
```

**No** uses un "what is my IP" genérico: pueden no coincidir.

!!! danger "En un servidor esto rompe la reconciliación entera"
    Si la IP del backend no está en la whitelist, **ninguna** consulta de estado
    funciona. Los errores de red se tragan correctamente (un fallo de red no es
    un fill), pero entonces nada se reconcilia nunca y el sistema **parece estar
    al día cuando no lo está**.

#### `10002 Parameter error` colocando un TP/SL

Casi siempre significa: **estás intentando cerrar una posición que no existe**.
Un `tradeSide="CLOSE"` sin posición que cerrar da exactamente esto.

Causa típica: el sistema dio por llenada una orden que en realidad se canceló, y
ahora coloca la protección de una posición fantasma. Ver
[Órdenes](ordenes.md#detalle-de-una-order_id).

---

### `BinanceAPIException` / `BinanceOrderException`

De `python-binance`. El paquete las **captura en `place_order`** y devuelve un
`OrderAck` con `status=REJECTED` y el motivo en `raw["error"]`, en vez de
propagarlas:

```python
ack, fills = broker.place_order(orden)
if ack.status == OrderStatus.REJECTED:
    print(ack.raw["error"])
```

En el resto de métodos se propagan.

#### Códigos de Binance

| Código | Qué es |
|---|---|
| `-1013` | Filtro de precisión o mínimo notional |
| `-1021` | Timestamp fuera de la ventana (reloj desincronizado) |
| `-1022` | Firma inválida (secreto mal) |
| `-1111` | **Precisión.** Demasiados decimales para ese símbolo *y ese mercado* |
| `-1121` | Símbolo inválido |
| `-2010` | Orden rechazada (fondos insuficientes, etc.) |
| `-2011` | Orden desconocida al cancelar |
| `-2015` | **API key inválida, sin permisos, o IP no autorizada** |

!!! warning "`-1111` con un símbolo que sí existe"
    Casi siempre: se usaron los filtros de **spot** para una orden de
    **futuros**. El mismo par tiene `stepSize` y `tickSize` distintos en cada
    mercado.

---

### `NotImplementedError`

Un broker que no sabe hacer algo. Nunca devuelve vacío — ver
[Arquitectura](../conceptos/arquitectura.md#la-regla-que-gobierna-todo-fallar-en-voz-alta).

| Mensaje | Causa |
|---|---|
| `PaperBroker no expone datos de cuenta.` | Le pediste al simulador algo que solo tiene un broker con cuenta remota |
| `PaperBroker no expone datos de mercado.` | Ídem, con velas |
| `Bitunix no tiene OCO nativo...` | Usa `tp_price`/`sl_price` adjuntos |
| `MT5Broker.update_order todavía no sabe modificar...` | Sin argumentos, cancela |
| `El apalancamiento solo existe en la cuenta de futuros.` | `market="spot"` |
| `El Paper Broker no tiene libro de órdenes...` | El simulador llena contra el rango de cada vela; no hay libro |

---

### `ValueError`

| Mensaje | Causa |
|---|---|
| `No existe el broker 'X'. Disponibles: ...` | Nombre mal escrito |
| `No hay credenciales para BITUNIX...` | Ni `credentials=` ni variables de entorno |
| `Rounded quantity is 0 for BTCUSDT (step_size=0.001)` | La cantidad es menor que el mínimo |
| `Limit orders require price` | Falta `price` en una `LIMIT` |
| `Stop-loss-limit orders require both price and stop_price` | Falta uno de los dos |
| `La orden X está en CANCELED: solo se puede modificar una orden viva` | |
| `En Binance el TP/SL son órdenes independientes...` | Pasaste `tp_price`/`sl_price` a `update_order` de Binance |
| `'X' no existe en el mercado de futuros de Binance.` | Par que no cotiza como perpetuo |

---

### `RuntimeError`

```
Live trading is blocked. Set ALLOW_LIVE_TRADING=true to enable.
```

El candado doble. Para operar en real hacen falta `trading_mode="live"` **y**
`allow_live_trading=True`. Ver [BrokerFactory](factory.md).

---

## Síntomas sin excepción

Los peores: no hay traceback, solo un resultado equivocado.

### "Coloqué la orden pero no aparece en el exchange"

**`test_order=True`**, que es el valor por **defecto** de la factory. Valida
contra la API y devuelve `status=NEW` sin colocar nada.

```python
BrokerFactory.get_broker(..., test_order=False)
```

### "Las velas de spot y futuros son idénticas"

El selector `market` no está llegando al endpoint. Deberían diferir: son
mercados distintos. Ver [Spot y futuros](../conceptos/mercados.md).

### "Los indicadores cambian solos entre dos consultas"

Se está colando la vela **en curso**, con OHLC parcial. El paquete la descarta;
si construyes velas por tu cuenta desde el proveedor crudo, no.

### "Una posición sale con `entry_price=0.0`"

Mira `.raw`. Los nombres de campo de `pending_positions` de Bitunix salen de su
documentación y no están verificados contra la cuenta real: si alguno no
coincide, el normalizador devuelve ceros en vez de reventar.

### "Cancelé el TP y sigue ahí"

Cancelaste con `market="spot"` una orden de **futuros**. En Bitunix eso no
encuentra nada y **devuelve un OK falso**.

### "El backtest gana dinero con 0 operaciones cerradas"

`get_balance()["USDT"]` del `PaperBroker` es la **caja**, no el capital. Usa
`mark_to_market_equity(symbol, price)`. Ver [PaperBroker](../brokers/paper.md).

### "El TP está en pérdida garantizada"

TP y SL invertidos: pasó de verdad en la rama SHORT de un grafo (entrada `SELL`
a 45.30, TP en 45.53). Comprueba que para un `SELL` se cumple
`TP < entrada < SL`.

### "El estado de la orden se quedó congelado en NEW"

Los brokers reales **no empujan** cambios de estado: Bitunix no tiene stream de
usuario y el `start_user_socket` de Binance está comentado. Hay que **preguntar**
con `get_order_status()` periódicamente.

---

## Depurar: `raw` lo conserva todo

Todos los modelos normalizados guardan la respuesta original:

```python
info = broker.get_order("BTCUSDT", oid, market="futures")
info.raw          # el dict exacto del exchange

p = broker.get_positions(market="futures")[0]
p.raw

ack, _ = broker.place_order(orden)
ack.raw
```

Es lo primero que hay que mirar cuando un campo normalizado sale a cero o a
`None`.

### Logs

El paquete usa `logging` estándar:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("luarix_exchange").setLevel(logging.DEBUG)
```

`BitunixBroker` emite `warning` cuando el exchange devuelve un estado que no
reconoce, incluyendo el cuerpo completo.

### Sondear la API sin colocar nada

Truco verificado con Bitunix: un **símbolo inválido** se rechaza con `300105`
**después** de validar el cuerpo. Sirve para descubrir qué campos acepta un
endpoint sin arriesgar dinero — un campo mal puesto devuelve `10008`
nombrándolo.

!!! warning "Valida la FORMA, no las precondiciones"
    Esa técnica **no** detectó que un `tradeSide="CLOSE"` necesita `positionId`:
    el rechazo por símbolo ocurre antes. Tenlo en cuenta al usarla.

---

## Tabla de decisión rápida

| Síntoma | Mira primero |
|---|---|
| Todo firmado falla, lo público va | Whitelist de IP / credenciales |
| `10003` en Bitunix | ¿Pediste testnet? No la tiene |
| La orden no aparece en el exchange | `test_order=True` |
| `-1111` en Binance futuros | Filtros de spot usados en futuros |
| `10002` colocando protección | Posición fantasma: la entrada no llenó de verdad |
| Un corto donde esperabas un cierre | Falta `reduce_only=True` en modo HEDGE |
| Campo normalizado a cero | `.raw` |
| Nada se reconcilia, sin errores | La IP del servidor no está en la whitelist |
