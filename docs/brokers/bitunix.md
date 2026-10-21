# Bitunix

`luarix_exchange.brokers.bitunix.BitunixBroker` — spot y futuros perpetuos
USDT-M.

```bash
pip install "luarix-exchange-library[bitunix]"
```

!!! danger "Bitunix NO tiene testnet"
    Lo que se coloque con esas claves es **dinero real**. No hay red de pruebas.
    Para practicar: Binance testnet o el [`PaperBroker`](paper.md).

## Capacidades

| Flag | Valor |
|---|---|
| `supports_futures` | ✅ |
| `supports_oco` | ❌ **no existe en su API** (`unsupported = {NATIVE_OCO}`) |
| `supports_independent_orders` | ✅ |

## Construcción

```python
broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    credentials=BrokerCredentials(api_key="...", api_secret="..."),
    trading_mode="live",
    allow_live_trading=True,
)
```

El parámetro `testnet` **se ignora**: la factory llama a `resolve(False)` para
este broker. Antes usaba `resolve(bool(testnet))` con `testnet=True` por
defecto, así que devolvía `("", "")` y la primera llamada fallaba con un
`10003 API key inexistente` que no dice nada de la causa real.

---

## Dos APIs, dos hosts

El paquete `bitunix` de PyPI es **solo spot**. Los futuros van por
`BitunixFuturesClient`, escrito en este repo.

| | Spot | Futuros |
|---|---|---|
| Host | `openapi.bitunix.com` | `fapi.bitunix.com` |
| Cliente | `bitunix.BitunixClient` (PyPI) | `BitunixFuturesClient` (este repo) |
| Prefijo | `/api/spot/v1/...` | `/api/v1/futures/...` |

De ahí que casi todo empiece con un `if market == "futures"`: no hay un endpoint
común al que llamar.

### La firma de futuros

Verificada contra la cuenta real. Dos SHA-256 encadenados:

1. `sha256(nonce + timestamp + api_key + payload)`
2. `sha256(digest + api_secret)`

El `payload` cambia según el verbo:

- **GET**: las claves y valores **ordenados y concatenados sin separadores** —
  `marginCoinUSDT`, **no** `marginCoin=USDT`. Con el formato de `urlencode` que
  usa `BitunixClient.sign_request`, la firma **no valida**.
- **POST**: el cuerpo JSON **tal cual viaja**. Se serializa una vez y se manda
  ese mismo texto como `data`, sin dejar que `requests` lo regenere con otro
  espaciado.

---

## `unwrap()`: por qué existe

!!! danger "Bitunix NUNCA responde con un HTTP de error"
    Cuando rechaza una llamada devuelve **200** con
    `{"code": "100011", "msg": "...", "data": null, "success": false}`.

    Todos los wrappers hacían `raw.get("data", <default>)`, que **no cubre ese
    caso**: el default de `.get()` solo aplica cuando la clave *falta*, y aquí
    está presente con valor `null`.

    Resultado: `data` quedaba en `None` y el llamador reventaba más adelante con
    un error sin relación con la causa — o peor, seguía adelante con datos
    vacíos. `place_order` construía un `OrderAck` con `order_id=""` en lugar de
    fallar: **una orden fantasma** que el sistema daba por colocada y a la que
    luego colgaba su SL/TP.

```python
from luarix_exchange.brokers.bitunix_response import unwrap, BitunixApiError

data = unwrap(raw, "la consulta de balance")                 # rechazo -> levanta
data = unwrap(raw, "la consulta de velas", on_null=[])       # "sin datos" -> []
```

`on_null` es qué devolver cuando la llamada fue **bien** pero `data` vino en
`null` — típicamente una lista vacía para una consulta que no encontró nada. Sin
`on_null`, ese caso también levanta.

!!! info "Criterio: no se levanta por un `code` desconocido si hay datos"
    No hay documentación fiable de todos los códigos de éxito de Bitunix, y
    tratar uno legítimo como error rompería llamadas que hoy funcionan. El corte
    se hace donde sí es inequívoco: **no hay datos con los que seguir**.

---

## Endpoints usados

### Spot (paquete `bitunix`)

| Operación | Método |
|---|---|
| Saldo | `get_account_balance` → `balance` |
| Velas | `get_kline_data` |
| Precio | `get_latest_price` |
| Libro | `get_depth_data` |
| Pares | `get_trading_pairs` |
| Órdenes abiertas | `query_current_orders` (**exige símbolo**) |
| Ejecuciones | `query_matching_orders` |
| Colocar | `place_order` |
| Cancelar | `cancel_orders` |

### Futuros (`BitunixFuturesClient`)

| Operación | Endpoint |
|---|---|
| Cuenta / saldo | `/api/v1/futures/account` |
| Modo de posición | (del mismo, campo `positionMode`) |
| Posiciones | `/api/v1/futures/position/get_pending_positions` |
| Órdenes abiertas | `/api/v1/futures/trade/get_pending_orders` |
| Detalle de orden | `/api/v1/futures/trade/get_order_detail` |
| Colocar | `/api/v1/futures/trade/place_order` |
| Modificar | `/api/v1/futures/trade/modify_order` |
| Cancelar | `/api/v1/futures/trade/cancel_orders` |
| Apalancamiento | `/api/v1/futures/account/change_leverage` |
| Velas (**público**) | `/api/v1/futures/market/kline` |

---

## Órdenes

### Tipos colocables

**Solo `LIMIT` y `MARKET`.** Verificado contra la API: cualquier otro valor
devuelve `code=10008 ... alternative value ["ALL","LIMIT","MARKET"]` (`"ALL"` es
un filtro de consulta, no algo colocable).

!!! danger "NO existe ningún tipo de stop en Bitunix futuros"
    El stop-loss va **adjunto** a la orden (`slPrice`), nunca como orden propia.

    Mandar `STOP_LOSS_LIMIT` (un tipo de Binance) dejaba la entrada abierta y
    **sin stop**, y el traceback no lo decía en ninguna parte. Ahora se rechaza
    en local con el motivo explícito, en vez de dejar que la API devuelva un
    `10008` opaco.

### TP/SL adjuntos (nativo)

```python
OrderRequest(
    symbol="LTCUSDT", side=Side.BUY, quantity=2.2,
    order_type=OrderType.LIMIT, price=44.92,
    market="futures",
    tp_price=45.37,
    sl_price=44.50,
)
```

Viajan como `tpPrice`/`slPrice` en el mismo cuerpo. Es la forma nativa y **la
única atómica**: la posición nunca llega a existir sin protección.

El disparo usa `LAST_PRICE` (el último precio negociado, que es lo que ve el
usuario en el gráfico) y no `MARK_PRICE`, que dispararía con el precio de marca
y sorprendería a quien puso el nivel mirando la vela.

### Modo cobertura (HEDGE)

`BitunixFuturesClient.place_order` consulta el modo real de la cuenta
(`position_mode()`, cacheado por instancia) y manda `tradeSide` solo cuando hace
falta:

| Modo | `tradeSide` | Qué hace un `SELL` contra un largo |
|---|---|---|
| ONE_WAY | no se manda | Lo **reduce** (el exchange netea) |
| HEDGE | `"OPEN"` / `"CLOSE"` | `OPEN` → **abre un corto**; `CLOSE` → cierra |

`reduce_only=True` se mapea a `tradeSide="CLOSE"`.

!!! danger "El incidente"
    Toda orden llevaba `tradeSide="OPEN"` **fijo**. El take-profit de un largo
    (un `SELL`) **abrió un corto real** que convivía con él. En ONE_WAY el neteo
    lo tapaba, por eso nunca se vio. Ver
    [Take profit y stop loss](../guia/tp-sl.md#el-incidente-el-take-profit-abria-un-corto-real).

### Modificar

**Enmienda nativa** en futuros: la orden nunca deja de existir, así que mover un
stop no abre una ventana de desprotección.

Bitunix exige mandar `qty` **y** `price` siempre, aunque solo se toque uno: los
campos ausentes no se "conservan", se interpretan como falta de parámetro. Por
eso el broker lee antes la orden con `order_detail()` y rellena lo que falta.

En **spot** no hay enmienda: cancelar y recolocar, con `order_id` nuevo y sin
atomicidad.

### Detalle de una orden

=== "Futuros"

    `get_order_detail` responde por la orden concreta, **viva o cerrada**.
    Devuelve `{}` cuando el exchange no la conoce — que **no** es lo mismo que
    "se cerró".

    Estados: `INIT`, `NEW`, `PART_FILLED`, `CANCELED`, `FILLED`. Ojo:
    `PART_FILLED`, no `PARTIALLY_FILLED`.

=== "Spot"

    **No tiene** endpoint de detalle. Se combinan las dos fuentes que sí existen:

    1. `query_matching_orders` → las **ejecuciones**. Si trae algo, llenó.
    2. Si no, la lista de **pendientes**. Si aparece, sigue viva.
    3. Si no aparece en ninguno, **levanta**. No se inventa un estado.

!!! danger "El incidente de las posiciones fantasma"
    `get_order_status` miraba la lista de PENDIENTES y daba por **llenada** toda
    orden ausente de ella. Una orden cancelada a mano también desaparece de ahí.

    Tres cortos de LTCUSDT (`2082878949264547840`, `2082879103115870208`,
    `2082880194939265024`) que el usuario canceló a mano quedaron marcados
    `FILLED`, con `Trade` en `ACTIVE` — posiciones que nunca existieron. Y al
    pasar a un estado no-vivo, ninguna reconciliación posterior volvía a
    mirarlas: el estado falso quedaba **congelado**.

    Evidencia: los tres "llenados" en 22 segundos, o sea una única pasada de
    polling, con `fill_price` igual exacto al precio límite de cada uno.

    Una orden que el exchange dice no conocer devuelve **`NEW`, no `FILLED`**:
    la ignorancia no es un fill.

---

## Velas

!!! warning "El kline de SPOT ignora todos los parámetros"
    Comprobado contra la API real:

    | Endpoint | `limit` | `startTime` | `endTime` |
    |---|---|---|---|
    | `/api/spot/v1/market/kline` | ignorado | **ignorado** | **ignorado** |
    | `/api/v1/futures/market/kline` | respeta | ignorado | **respeta** |

    El de spot devuelve **siempre** las mismas ~201 velas más recientes. Ese era
    el error que veía el usuario al pedir un backtest largo: *"hay 201 de 923
    esperadas"*.

Por eso `get_historical_candles` usa **siempre** el kline de futuros, incluso
pidiendo spot. Se pagina **hacia atrás** anclando en `endTime`: cada vuelta pide
"las 200 anteriores a la más antigua que ya tengo".

Verificado: 6 páginas → 1200 velas de 4h **sin un solo hueco**.

```
LTCUSDT 4h: 1080 velas  2026-02-02 .. 2026-07-31  huecos=0
LTCUSDT 1d:  360 velas  2025-08-06 .. 2026-07-31  huecos=0
BTCUSDT 4h: 1080 velas  2026-02-02 .. 2026-07-31  huecos=0
```

!!! note "Son precios de PERPETUO"
    Para el mismo par se mueven casi igual que el spot, pero no son idénticos
    (base, funding). Para una estrategia que opera futuros en Bitunix es además
    la fuente correcta. Si algún día su spot admite rangos, el sitio al que
    volver está marcado en `FUTURES_KLINE_URL`.

### Dos formas de respuesta

| | Formato |
|---|---|
| spot | `{"ts": "2026-08-01T16:00:00Z", "open": "44.11", ..., "volume": "137.149"}` (ISO) |
| futuros | `{"time": "1785585600000", "open": "44.31", ..., "baseVol": "232675.4"}` (ms **como cadena**) |

`_parse_ts` reconoce las tres formas posibles: ms numérico, ms **como cadena de
dígitos**, e ISO-8601. La segunda es la que rompía: `"1785585600000"` caía en la
rama ISO y reventaba con `month must be in 1..12`.

### Intervalos: los dos klines usan códigos distintos

| | Códigos |
|---|---|
| spot | `1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M` |
| futuros | `1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M` |

El paquete traduce desde los nombres estándar. Un intervalo no soportado en
futuros **levanta**; en spot cae a 1h **en silencio** (comportamiento heredado —
usa siempre un intervalo de la tabla).

---

## Limitaciones conocidas

| | |
|---|---|
| **Sin testnet** | Dinero real siempre |
| **Sin OCO** | Usa TP/SL adjuntos |
| **Sin tipos de stop en futuros** | El stop va adjunto |
| **Spot no lista todas las órdenes** | Su endpoint exige símbolo |
| **Kline de spot sin rango** | El histórico sale del de futuros |
| **Libro de futuros: `limit` solo 1, 5, 15 o 50** | Se pide el menor que cubre lo pedido y se recorta (verificado 2026-10-16) |
| **API keys restringidas por IP** | `10004 Invalid IP` |
| **`modify_order`, `pending_positions`, `change_leverage` sin verificar** | Documentados, no ejercitados contra la cuenta real |

### Whitelist de IP

```bash
curl https://fapi.bitunix.com/cdn-cgi/trace     # campo ip=
```

**No** uses un "what is my IP" genérico: pueden no coincidir.

Los klines son públicos y siguen funcionando aunque la IP esté mal. Esa
asimetría es la mejor herramienta de diagnóstico: si el precio va y el saldo no,
es la key o la IP.
