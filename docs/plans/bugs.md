# Bugs / puntos importantes ya analizados

Registro para no tener que re-analizar lo mismo en cada sesión.

Las secciones marcadas **[heredado]** vienen de `docs/plans/bugs.md` de
`luarix-trading-framework` (2026-07-30 a 2026-08-13), donde vivía este código
antes de la extracción del 2026-10-14. Se copiaron solo las que tratan de
brokers, órdenes, velas o `PaperBroker`; las rutas que citan
(`luarix_trading/...`, `app/...`) son las de entonces — hoy el código está en
`luarix_exchange/`. Las referencias a `position_manager`, `order_tracker`,
`nodes/execution.py`, etc. son de `luarix-web-server`.

---

## [2026-10-14] Extracción desde luarix-trading-framework

- El código se copió **sin cambios de comportamiento**: solo rutas de import.
  Línea base antes de mover: 149 tests en luarix-trading-framework. Después:
  148 aquí + 1 (`StatsOnCloseTests`, de `BacktestStats`) que se quedó allá.
- `Side` se movió de `core/models/signals.py` a `models/orders.py`. `Signal`
  (su vecino) es de estrategia y se quedó en luarix-trading-framework, que
  reexporta `Side` desde aquí: es el MISMO objeto enum en los dos paquetes. Si
  alguien redefine `Side` en el framework, `Side.BUY == Side.BUY` dejaría de
  ser cierto entre paquetes — no hacerlo.
- `models/market.py` solo tiene `Candle`. `Trend`, `Fractal`, `MarketSnapshot`
  y `Market` son de estrategia y se quedaron allá.

---

## REGLA PERMANENTE — los logs llevan DETALLES, nunca "ok / ejecutado / colocada"

Pedido explícito y repetido del usuario (2026-07-30), heredado del framework.
Un log que dice "orden colocada", "ok" o "ejecutado" no sirve y cuenta como
regresión. Si hay datos (símbolo, lado, cantidad, precio, mercado, id de
orden), el log los imprime; si algo falla, imprime el motivo REAL. Antes de
escribir una plantilla de log fija, mirar qué campos hay: si el mensaje se
puede escribir sin leerlos, está mal.

---

## [heredado] GRAVE — el take-profit abría un CORTO real en Bitunix futuros (2026-07-30) — ARREGLADO

Estrategia LONG, nodo `limit-order` LONG, y en el exchange apareció un corto de LTC.
Evidencia (Trade 1972, run 231, cuenta 16 en modo HEDGE):

```
entry        BUY  LIMIT 44.92    exch_id=2082852993787334656   <- long correcto
take_profit  SELL LIMIT 45.3692  exch_id=2082854548489949184   <- esto abrió el corto
```

**Causa raíz.** `position_manager.open_independent_trade` coloca el TP/SL como órdenes
normales del lado opuesto, y `BitunixFuturesClient.place_order` ponía `tradeSide="OPEN"`
FIJO a toda orden en modo HEDGE. Un `SELL` + `OPEN` en hedge **no cierra el largo: abre un
corto independiente**. El comentario del código asumía que las salidas iban por otra vía;
no era cierto, pasan por el mismo `place_order`. En ONE_WAY el neteo del exchange lo tapaba,
por eso nunca se vio antes.

**Segundo bug del mismo camino.** El SL se mandaba con `orderType="STOP_LOSS_LIMIT"` (un tipo
de Binance). Futuros de Bitunix **no tiene tipos de stop**: `code=10008 ... alternative value
["ALL","LIMIT","MARKET"]`. O sea que el stop-loss **nunca se colocaba** — la posición quedaba
abierta y sin protección, y el traceback no lo decía en ninguna parte.

### Contrato real de la API (verificado con símbolo inválido, sin colocar nada)

Un símbolo inválido se rechaza con `300105 System error` DESPUÉS de validar el cuerpo, así que
sirve para descubrir campos sin riesgo. Un campo mal puesto devuelve `10008` nombrándolo.

| Campo | Valores aceptados |
|---|---|
| `tradeSide` | exactamente `["OPEN","CLOSE"]` (en minúsculas lo rechaza) |
| `orderType` | `["ALL","LIMIT","MARKET"]` — `ALL` es filtro de consulta, no colocable |
| `tpPrice`/`tpStopType`, `slPrice`/`slStopType` | aceptados DENTRO de `place_order` |
| `/api/v1/futures/tpsl/place_order` | existe (`30006` pide al menos TP o SL) |
| `/api/v1/futures/tpsl/position/place_order` | existe, requiere `positionId` (`30004`) |

### Solución aplicada — TP/SL adjuntos a la entrada

Decisión del usuario: la protección viaja DENTRO de la orden de entrada. Es atómico —
la posición nunca llega a existir sin stop — y elimina las dos órdenes sueltas del lado
contrario, que eran la fuente del corto.

- `app/core/models/orders.py` — `OrderRequest` gana `tp_price`, `sl_price`, `reduce_only`.
- `app/brokers/bitunix_futures.py::place_order` — manda `tpPrice`/`slPrice`, mapea
  `reduce_only` a `tradeSide="CLOSE"`, y **rechaza en local** cualquier `orderType` no
  colocable con el motivo explícito, en vez de dejar que la API devuelva un `10008` opaco.
- `nodes/execution.py` — `_attached_exit_levels()` resuelve los niveles ANTES de colocar.
  Clave: `_build_exit_plan` es **pura** (`nodo`, `state`), así que el nodo de orden puede
  construir los planes de sus nodos `stop-loss`/`take-profit` aguas abajo sin que hayan
  corrido todavía y sin reordenar el motor. El nivel se ancla al precio LÍMITE de la entrada
  (que es a lo que va a llenar), no al cierre de la vela.
- `position_manager.create_pending_trade` — persiste los niveles y marca
  `metadata["exits_attached"]`.
- `execution_report_bridge._handle_entry_fill` — si `exits_attached`, NO coloca nada al llenar.
- `position_manager.open_independent_trade` — defensa en profundidad: sus dos patas ahora van
  con `reduce_only=True`, así que aunque esa ruta se ejecute contra un broker real, cierra.

Solo afecta al camino de broker REAL en futuros. PaperBroker, backtest y spot, intactos.

### Auditoría de Binance futuros (pedida en la misma sesión)

`BinanceBroker` **no tiene camino de futuros en absoluto**: `place_order` llama a
`create_order` (el endpoint SPOT de python-binance), no existe ninguna llamada
`futures_*`, y `_build_order_payload` **nunca mira `order.market`**. Consecuencia: un grafo
con un sub-nodo Binance en "futures" coloca la orden en **SPOT**, en silencio. No es el mismo
bug que el de Bitunix, pero es de la misma familia (el selector `market` no llega a la
colocación) y hay que arreglarlo antes de operar futuros en Binance. **Pendiente.**

### Secuela — `10002 Parameter error`: el tracker reconciliaba órdenes de PAPEL contra el exchange

Tras el arreglo anterior apareció un error distinto en `run_live_market_feeds`. No era una
regresión: era un **segundo bug independiente** que el primero tapaba.

Cadena completa, reconstruida sobre datos reales (Trade 1974, run 233, grafo 40):

1. `dispatch_manual` (el botón "Run" del builder) **reutiliza el `active_run` del grafo LIVE**
   (`run = graph.active_run or engine.start_run(MANUAL)`) y llama a `engine.execute(...)`
   **sin `allow_real_broker=True`** — a diferencia de `dispatch_candle`, que sí lo pasa.
   Resultado: la orden va al `PaperBroker`, pero la fila `Trade`/`Order` queda dentro del run
   EN VIVO, con `exchange_order_id="1"` (el contador del simulador).
2. `order_tracker.poll_graph` reconcilia contra el broker REAL **todas** las órdenes vivas del
   run, sin distinguir de dónde salieron.
3. Bitunix responde "no encuentro la orden 1". El tracker interpreta eso como **LLENADA** —
   correcto para una orden real (los endpoints de pendientes solo listan vivas), catastrófico
   para una de papel.
4. `handle_execution_report` intenta colocar el TP/SL **reales** de una posición que no existe.
   Con `reduce_only=True` eso es un `tradeSide="CLOSE"` sin posición que cerrar → `10002`.

Verificado además que la ruta en vivo SÍ es real: para `node_11`, `_execution_broker` con
`allow_real_broker=True` devuelve `BitunixBroker/futures`. O sea que un grafo en vivo mezcla
los dos mundos en el mismo `WorkflowRun` según qué lo dispare.

**Nota sobre el sondeo de la API**: la sonda con símbolo inválido NO detectó que un
`tradeSide="CLOSE"` necesita `positionId` — el rechazo por símbolo (`300105`) ocurre antes.
Esa técnica valida la FORMA del cuerpo, no las precondiciones de negocio. Tenerlo en cuenta
al usarla.

**Arreglo:** `create_pending_trade` ahora graba `metadata["venue"]` (`"real"`/`"paper"`),
resuelto por `nodes/execution.py::_venue_of()` a partir del TIPO del broker que recibió la
orden (lista blanca explícita `_REAL_BROKER_TYPES`; lo desconocido cae a `"paper"`, que es el
lado seguro). `order_tracker.poll_graph` filtra con `venue == "real"` — los Trades anteriores
a esta marca no lo tienen y quedan excluidos, que es justo lo que se quiere.

**Pendiente (no arreglado):** `dispatch_manual` y `dispatch_on_start` siguen colocando contra
el simulador aunque el grafo esté LIVE. Ya no rompe nada, pero es confuso: darle a "Run" en un
grafo en vivo no opera de verdad. Decidir si deben heredar `allow_real_broker` del estado del
grafo o si deben crear su propio run separado en vez de reutilizar el `active_run`.

---

## GRAVE — TP y SL invertidos en la rama SHORT de un grafo (2026-07-30) — ARREGLADO

Un grafo con dos ramas long/short hechas con nodos `if` (el patrón normal) calculaba el
TP/SL de la rama CORTA como si fuera larga. Caso real: entrada `SELL` a 45.30 →
take-profit en **45.53** (por encima de la entrada, o sea en pérdida garantizada) y
stop-loss en **44.85** (por debajo, donde debería estar el objetivo). Los dos al revés.

**Causa raíz.** `_build_exit_plan` decidía la dirección con
`is_buy = resolve_trade_side(state) == "BUY"`. `resolve_trade_side` responde a nivel de
PASADA, no de rama: solo mira nodos `entry-condition`, y sin ninguno (el caso de este grafo,
que usa `if`) cae a `strategy.side`, que es estático. Las dos ramas recibían el mismo `BUY`.

**Arreglo.** `_upstream_order_side(node, state)` recorre las aristas de ENTRADA en anchura
hasta el `limit-order`/`market-order` que este TP/SL protege y lee el `side` REAL de su
output en esta pasada. `_build_exit_plan` lo prefiere sobre `resolve_trade_side`, que queda
solo como fallback. Se recorre en anchura porque el cableado normal encadena
`limit-order -> take-profit -> stop-loss`: el SL llega a su orden a dos saltos.

`_build_exit_plan` acepta además `is_buy` explícito, que le pasa `_attached_exit_levels`:
esa ruta construye los planes DESDE el nodo de orden, antes de que la orden haya guardado su
`output`, así que ahí `_upstream_order_side` no encontraría nada y caería al lado estático.

Verificado sobre el grafo 40: `node_21`/`node_22` detectan `SELL` desde `node_20`, y una
entrada short a 45.30 da **TP 45.0735 / SL 45.7530** (`TP < entrada < SL`).

---

## Binance USDT-M Futures — camino implementado, SIN PROBAR contra la API

`BinanceBroker` no tenía camino de futuros: `place_order` llamaba siempre a `create_order`
(endpoint de SPOT de python-binance) y `_build_order_payload` no miraba `order.market`. Un
grafo con el sub-nodo Binance en "futures" colocaba en **spot**, en silencio.

Implementado:

- `place_order` enruta a `_place_futures_order` cuando `order.market == "futures"`.
- `_futures_symbol_filters` lee `futures_exchange_info()` — **no** se puede reusar el de spot:
  el mismo par tiene `stepSize`/`tickSize` distintos en cada mercado, y usar los de spot da un
  `-1111` de precisión imposible de diagnosticar desde el log. Se guardan como STRING, igual
  que en spot, porque `_round_to_step` usa `Decimal`.
- TP/SL: Binance **no** admite adjuntarlos a la entrada (a diferencia de Bitunix). Se colocan
  acto seguido como `TAKE_PROFIT_MARKET` y `STOP_MARKET` del lado contrario, con
  `closePosition="true"` — cierran la posición entera al dispararse y, por construcción, no
  pueden abrir nada. `reduce_only` se mapea a `reduceOnly`.
- `_supports_attached_exits` incluye ahora `BinanceBroker`: para el motor los dos brokers son
  "esta orden sale protegida", aunque por dentro lo resuelvan distinto.

Probado con un cliente falso — entrada SELL LIMIT 45.30 produce:
```
LIMIT               SELL qty=0.083 price=45.30 timeInForce=GTC
TAKE_PROFIT_MARKET  BUY  stopPrice=45.07 closePosition=true
STOP_MARKET         BUY  stopPrice=45.75 closePosition=true
```

⚠ **PENDIENTE DE VALIDAR CONTRA LA API REAL** (restricción geográfica en la cuenta
disponible). La forma de las llamadas sale de la documentación de USDT-M Futures y de la
firma de `python-binance`, no de una respuesta real. Además, en modo COBERTURA Binance exige
`positionSide` en vez de `reduceOnly`, y este broker todavía no lee el modo de la cuenta —
hay que resolverlo antes de operar en hedge.

---


## [heredado] Bitunix futuros daba por LLENADA toda orden cancelada a mano — ARREGLADO 2026-07-31

Síntoma: la estrategia `test-oss` mostraba tres cortos de LTCUSDT como posición abierta
(`Trade.status = ACTIVE`) cuando en la app de Bitunix constaban CANCELADOS y nunca se
ejecutaron. El botón "Re-sync broker" contestaba "el broker dice lo mismo que la base de
datos", que era falso.

### Causa

`BitunixBroker.get_order_status`, en el camino de futuros, no preguntaba por la orden:

```python
pendientes = self.account_data_provider.futures.pending_orders(symbol)
sigue_viva = any(str(o.get("orderId")) == str(order_id) for o in pendientes)
return OrderStatus.NEW if sigue_viva else OrderStatus.FILLED
```

`get_pending_orders` lista solo las órdenes VIVAS. Una orden cancelada a mano también
desaparece de ahí, así que la inferencia "ausente ⇒ llenada" es sencillamente falsa.

La cadena completa del daño:

1. El worker `run_live_market_feeds` pregunta, recibe FILLED.
2. `_reconcile_order` sintetiza un execution report → `handle_execution_report` →
   `position_manager.mark_entry_filled` → `Order.status = FILLED`, `Trade.status = ACTIVE`.
3. A partir de ahí la orden ya no está en `_LIVE_STATUSES`, así que **ninguna reconciliación
   posterior vuelve a mirarla**. El estado falso queda congelado.

Evidencia en BD (trades 1976/1977/1978, run 234): creados 17:20:16, 17:20:52 y 17:25:11, y
"llenados" los tres entre 17:27:05 y 17:27:27 — 22 segundos, o sea una única pasada de
polling, no tres fills independientes. `fill_price` coincide exacto con el precio límite de
cada uno (45.42 / 45.39 / 45.38), que es lo que devuelve `_expected_fill_price`, no un precio
de ejecución real. Ids de Bitunix: 2082878949264547840, 2082879103115870208,
2082880194939265024.

Descartado como causa: un error de API. `unwrap` levanta ante `code != "0"` con `data: null`,
así que un `10004 Invalid IP` habría hecho fallar la consulta, no devolver lista vacía.

### Arreglo

1. `BitunixFuturesClient.order_detail(order_id)` → `GET /api/v1/futures/trade/get_order_detail`,
   que responde por la orden concreta esté viva o cerrada. Estados: `INIT`, `NEW`,
   `PART_FILLED`, `CANCELED`, `FILLED` (ojo: `PART_FILLED`, no `PARTIALLY_FILLED` — la
   traducción está en `_FUTURES_STATUS`).
2. `get_order_status` en futuros usa ese endpoint. Una orden que el exchange dice no conocer
   devuelve **NEW, no FILLED**: la ignorancia no es un fill. Equivocarse hacia NEW cuesta otra
   consulta; equivocarse hacia FILLED coloca un TP/SL real contra una posición inexistente.
3. `position_manager.revert_phantom_entry_fill` — repara lo ya escrito: deshace el fill
   (entrada a CANCELED sin `fill_price`/`filled_at`, `Trade` a CANCELLED) y cancela cualquier
   TP/SL que se hubiera colocado apoyándose en él.
4. `order_tracker._reverify_active_entry` — el re-sync ahora re-verifica también la entrada de
   los trades ACTIVE, que es la única forma de que un estado falso ya congelado se pueda
   reparar. Cuesta una llamada por trade abierto.
5. `resync_strategy` devuelve `{"changes", "errors", "checked"}` en vez de una lista.
   "Pregunté y no había cambios" y "no pude preguntar" ya no se ven iguales en la UI.

Tests: `app/test_bitunix_order_status.py` (9, pasan).

### Pendiente: el camino de SPOT tiene la misma inferencia

`get_order_status` en spot sigue haciendo `if not data: return OrderStatus.FILLED`. Se dejó
sin tocar A PROPÓSITO: no hay un endpoint de detalle de orden de spot comprobado contra la API
real con el que sustituirla, y cambiarlo a ciegas dejaría los fills de spot sin detectar (y
por tanto sin TP/SL). Antes de tocarlo hay que confirmar qué devuelve
`query_matching_orders` para una orden NEW y para una CANCELED. Queda un `logger.warning` en
ese camino para que se vea cuando ocurre.

### Nota de operación: whitelist de IP

Las API keys de Bitunix están restringidas por IP (`code=10004 Invalid IP` desde una máquina
no autorizada). Si la IP del servidor que corre el backend no está en la whitelist, TODAS las
consultas de estado fallan; `_reconcile_order` se las traga correctamente (un error de red no
es un fill), pero entonces nada se reconcilia nunca. Con el cambio 5 de arriba eso ya se ve en
la UI en vez de reportarse como "todo al día".

---


## [heredado] Bitunix: el kline de SPOT ignora el rango — ARREGLADO 2026-08-01

Síntoma al pedir cobertura de un backtest largo:

> `Faltan velas de LTCUSDT 4h para el rango solicitado: hay 201 de 923 esperadas, cubriendo de
> 2026-06-29 08:00 a 2026-08-01 16:00 UTC.`

### Lo que pasa de verdad

Comprobado contra la API real (el kline es público, no le afecta la whitelist de IP):

| endpoint | `limit` | `startTime` | `endTime` | resultado |
|---|---|---|---|---|
| `openapi.bitunix.com/api/spot/v1/market/kline` | ignorado | **ignorado** | **ignorado** | SIEMPRE las mismas 201 velas más recientes |
| `fapi.bitunix.com/api/v1/futures/market/kline` | respetado | ignorado | **respetado** | 200 velas que TERMINAN en `endTime`, más reciente primero |

Las 201 velas del error son, literalmente, la única respuesta que el kline de spot sabe dar.
Da igual lo que se le mande.

El código ya tenía paginación (`PAGE_SIZE=200`, `MAX_PAGES=500`) pero avanzaba **hacia
adelante** moviendo `startTime`, y `startTime` no lo respeta ningún kline de Bitunix: la
primera página devolvía la ventana reciente, el cursor saltaba a su último timestamp —
pasándose del final pedido — y el bucle terminaba con una sola página.

### Arreglo

1. **Se pagina hacia ATRÁS** contra el kline de **futuros**, anclando en `endTime`: cada vuelta
   pide "las 200 anteriores a la más antigua que ya tengo". Verificado: 6 páginas → 1.200 velas
   de 4h **sin un solo hueco**.
2. `_to_candle` acepta las dos formas, que son distintas:
   - spot: `{"ts": "2026-08-01T16:00:00Z", ..., "volume": "137.149"}` (ISO)
   - futuros: `{"time": "1785585600000", ..., "baseVol": "232675.4"}` (ms **como cadena**)
   Sin esto toda vela de futuros se descartaba en silencio (`ts_val` salía `None`).
3. `_parse_ts` reconoce milisegundos en **cadena de dígitos**. Antes solo int/float o ISO, así
   que `"1785585600000"` caía en la rama ISO y reventaba con `month must be in 1..12`.
4. Eliminado `_kline_page` (la paginación de spot hacia adelante): ya no lo llamaba nadie y su
   docstring afirmaba que `startTime` funcionaba.

Resultado, con el provider real del repo:

```
LTCUSDT 4h: 1080 velas  2026-02-02 .. 2026-07-31  huecos=0
LTCUSDT 1d:  360 velas  2025-08-06 .. 2026-07-31  huecos=0
BTCUSDT 4h: 1080 velas  2026-02-02 .. 2026-07-31  huecos=0
```

### ⚠ Son precios de FUTUROS perpetuos

Es la única fuente de Bitunix con histórico paginable. Para el mismo par se mueven casi igual
que el spot, pero no son idénticos (base, funding). Para una estrategia que opera futuros en
Bitunix —el caso de este usuario— es además la fuente correcta. Si algún día su spot admite
rangos, el sitio donde volver está marcado en `FUTURES_KLINE_URL`.

### Aparte: la cuenta de datos del backtest está fijada a PAPER

`hooks/useBacktestRun.ts` manda siempre `data_source_account: ensurePaperBrokerAccount().id`.
No hay selector en el formulario. Con PAPER, `compile_strategy` no puede descargar nada
("no hay un broker real conectado que pueda completarlas") y solo sirve lo ya cacheado. Hoy la
única vía para que descargue es poner un sub-nodo Broker real en el grafo (On Candle y/o
Get Candles). Un selector de fuente de datos en el formulario de lanzamiento sería lo natural
— pendiente, no hecho.

---


## [heredado] `PaperBroker.place_order` reventaba CUALQUIER orden MARKET al primer `on_candle` (2026-08-07)

Descubierto verificando, con un `PaperBroker` real (no un doble de test), el trabajo de OCO/
stop-loss/take-profit de `luarix-orchestator` (`workflow/backtesting/position_tracking.py`) --
pero el bug es previo a esa sesión y afecta a CUALQUIER orden de mercado, no solo a las nuevas.

`place_order()` (linea ~507) calculaba `price = Decimal(str(order.price)) if order.price else
None`. Para una orden MARKET, `order.price` es `None` (nunca se pasa un precio) -- y el
comentario ya existente en el propio método decía explícitamente que MARKET "también terminaba
como LIMIT interno" (comportamiento pre-existente). El problema: `_filled()` (el matching de
`on_candle()`) hace `float(order.price)` SIN resguardo para esa rama --

```python
if order.side == OrderSide.BUY:
    return candle.low <= float(order.price)   # float(None) -> TypeError
return candle.high >= float(order.price)
```

Reproducido con un `PaperBroker` real: `place_order` con una orden MARKET, seguido de un
`on_candle()` -- revienta con `TypeError: float() argument must be a string or a real number,
not 'NoneType'` en la primerísima vela que intenta emparejarla. Nunca se había ejercitado esta
ruta con un broker real de punta a punta (los tests anteriores de `PlaceMarketOrderAction`
usaban gateways falsos que no pasan por `_filled()`).

**Fix** (`brokers/paper.py::place_order`, rama `else` que arma `order_type = OrderType.LIMIT`):
si `price` sigue siendo `None` ahí, se rellena con `self._last_close` -- el cierre de la vela
que `on_candle()` está procesando AHORA MISMO (se setea al principio de `on_candle`, antes de la
cola de matching), que es exactamente lo que "ejecutar a mercado" significa. No toca la rama
`STOP_LOSS_LIMIT`: esa usa `order.stop_price` para matching y fill (`_filled`/`_fill`), nunca
`order.price`, así que dejarlo en `None` ahí sigue siendo inofensivo.

Verificado con un `PaperBroker` real (shim `app.*`, ver la nota de estado en
`luarix-orchestator/workflow/adapters/trading_framework_market_data_adapter.py`): orden MARKET
BUY seguida de `on_candle()` ya no revienta y llena al `close` de esa vela.


## [heredado] El paquete no importaba en absoluto: `app/` era bytecode huérfano -- ARREGLADO 2026-08-10

Encontrado al empezar la limpieza de la rama `ossuan_cleaning`. Los 53
imports internos de `brokers/` y `core/` seguían apuntando a `app.*`
(`from app.brokers.base import Broker`), pero `app/` **ya no contenía
ningún `.py`**: 47 archivos, todos `.pyc` dentro de `__pycache__`, sobras
de un checkout anterior al aplanado del árbol. Lo mismo pasaba con
`backtesting/`, `pandas_luarix/` y `luarix_rest_api/` -- cero fuentes,
solo bytecode.

Por eso `git status` daba limpio y el problema era invisible:
`__pycache__` está en `.gitignore`, y una carpeta que solo contiene
archivos ignorados no aparece como untracked. Ninguna de esas 4 carpetas
estaba en `git ls-tree HEAD`.

**Consecuencia**: `import brokers.paper` reventaba con
`ModuleNotFoundError: No module named 'app.brokers.base'`. El paquete
estaba roto en HEAD y nadie lo notaba porque el único consumidor
(`luarix-orchestator`) protege esos imports con
`pytest.importorskip("brokers.paper")` -- sus tests contra el `PaperBroker`
real se estaban SALTEANDO en silencio, no pasando.

**Arreglo**: borradas las 4 carpetas fantasma; los 53 `from app.` →
`from luarix_trading.`; verificado importando los 28 módulos del paquete
uno por uno (cero rotos) y los 39 tests en verde.

## Por qué el paquete es `luarix_trading.*` y no `brokers`/`core` planos (2026-08-10)

Instalar `brokers` y `core` como paquetes top-level en `site-packages` es
pedir una colisión de nombres: cualquier otra librería que se llame igual
gana o pierde según el orden de `sys.path`. Es exactamente el bug que ya
mordió a `luarix-orchestator`, donde una carpeta `grpc/` en la raíz del
repo tapaba a la librería `grpcio` instalada (ver `docs/plans/bugs.md` de
ese repo). Decidido con el usuario: un único paquete raíz
`luarix_trading/`, con `brokers/` y `core/` como subpaquetes.

**Los consumidores tienen que actualizar sus imports** --
`luarix-orchestator` usa 6 (`brokers.paper`, `core.models.orders`,
`core.models.signals`, y `app.rendering.stats`).

## `Trend` se define en `core/models/market.py`, ya no se importa (2026-08-10)

`core/models/market.py` importaba `Trend` de `tradingview/types.py`, el
port del indicador SMC de LuxAlgo. Era la ÚNICA dependencia de `brokers/`+
`core/` hacia cualquier otra carpeta del repo: arrastraba 362 líneas de
estructuras SMC por un enum de dos valores. Como `tradingview/` es lógica
de indicadores (territorio de `pandas-luarix`, no de un paquete de
brokers), se eliminó del repo y `Trend` se define local, con los mismos
valores (-1/1) para no romper nada ya serializado.

## `rendering/stats.py` → `core/stats.py` (2026-08-10)

462 líneas de stdlib puro (`TradeRecord`, `BacktestStats`) que no
renderizan nada: son estadísticas de trades cerrados. Estaban bajo
`rendering/` por accidente histórico. Se movieron a `core/stats.py` y el
resto de `rendering/` se borró. `luarix-orchestator` las importa en
`workflow/backtesting/backtest_runner.py` -- ese import hay que
actualizarlo (`app.rendering.stats` → `luarix_trading.core.stats`).

---

# Auditoría del contrato de broker: qué faltaba para operar de verdad (2026-08-11)

Revisión pedida por el usuario: *"este proyecto debe ser capaz de operar con un broker, sea
Binance o Bitunix, sea spot o futuros"*. Lo que sigue es lo que faltaba, lo que estaba roto y
lo que se ha hecho. Las seis operaciones que pidió (última vela / listado de velas, saldo
filtrable, posiciones activas, órdenes abiertas, crear y editar órdenes, detalle de una
`order_id`) están las seis en `brokers/base.py::Broker` y funcionan con los dos brokers en los
dos mercados.

## Resumen: qué había, qué hay

| Operación | Antes | Ahora |
|---|---|---|
| Última vela / N velas | **no existía en `Broker`**; había que alcanzar a mano `broker.market_data_provider`, que devolvía DataFrame en Binance y `list[Candle]` en Bitunix | `get_last_candle()` / `get_candles()`, `list[Candle]` siempre, con `market=` |
| Velas de un rango | solo en el provider, sin `market` | `get_historical_candles()` |
| Saldo | `get_balance(market)`, sin filtro por activo | `get_balance(market, asset)` |
| Posiciones activas | Binance devolvía **saldos de spot**; Bitunix devolvía **`{}` fijo** | `get_positions(market, symbol)` -> `list[BrokerPosition]` |
| Órdenes abiertas | `list[dict]` crudo, distinto en cada broker | `list[OrderInfo]` normalizado, con `.raw` |
| Detalle de `order_id` | **no existía** | `get_order(symbol, order_id, market)` -> `OrderInfo` |
| Crear orden | ok | ok |
| Editar orden | Binance **reventaba con `NameError`**; Bitunix no modificaba nada | `update_order(..., price=, quantity=, stop_price=, tp_price=, sl_price=)` |
| Apalancamiento | no existía | `set_leverage(symbol, leverage)` |
| Último precio | solo spot | `get_ticker(symbol, market)` |

## Bugs encontrados y corregidos

### 1. GRAVE — `BinanceBroker.update_order` cancelaba y luego reventaba

Su cuerpo referenciaba `new_quantity`, `new_price`, `new_stop_price` y `new_stop_limit_price`:
**cuatro variables que no existían en ningún sitio** (la firma era `(symbol, order_id)` a
secas). Remataba con `OrderStatus.from_binance(...)`, un método que tampoco existe.

La secuencia real habría sido: `cancel_order()` -> `NameError`. O sea, **la orden cancelada y
sin sustituta**: una posición desprotegida y un traceback que no menciona en ningún sitio que
el stop ya no está. Nadie lo notó porque nada lo llamaba.

Ahora: futuros usa `futures_modify_order` (enmienda nativa, conserva el id); spot cancela y
recoloca respetando tipo, `timeInForce` y `stopPrice` originales, y devuelve el **id nuevo**.

### 2. GRAVE — `BitunixBroker.get_positions()` devolvía `{}` fijo

Literalmente `return {}`. Este broker afirmaba no tener ninguna posición abierta pasara lo que
pasara. Y la constante del endpoint (`PENDING_POSITIONS = "/api/v1/futures/position/get_pending_positions"`)
llevaba declarada desde hacía tiempo en `bitunix_futures.py` **sin que nadie la llamara**.

Ahora: `BitunixFuturesClient.pending_positions(symbol)` y normalización a `BrokerPosition`.

### 3. GRAVE — `BinanceBroker.get_positions()` devolvía saldos de SPOT

`{asset: amount for asset, amount in self.get_balance().items() if amount > 0}` — sin mirar
`market` ni llamar a `futures_position_information` en ningún momento. Consecuencias: una
cuenta de futuros con cortos abiertos aparecía **vacía**, y una con USDT parado aparecía "en
posición". Misma familia que el bug ya documentado de `place_order` colocando en spot.

### 4. GRAVE — `BitunixBroker` no tenía el candado de trading real

`_validate_pre_trade` (el doble candado `trading_mode == "live"` + `allow_live_trading`) existía
**solo en Binance**. Bitunix — el único de los dos con el que el usuario opera de verdad, y sin
testnet donde equivocarse gratis — colocaba siempre. Añadido.

### 5. Bitunix devolvía las velas al revés, y `limit` cogía las MÁS ANTIGUAS

`get_last_candles` hacía `klines[:limit]` sobre una respuesta que llega **de la más reciente a
la más antigua**. O sea que se quedaba con las `limit` más VIEJAS del bloque y las devolvía en
orden descendente. Con `limit=1`, "la última vela" era la vela más antigua disponible.

Ahora se ordena ascendente y se recorta por el final. Cubierto por
`tests/test_broker_contract.py::VelasTests::test_la_ultima_vela_es_la_mas_reciente`.

### 6. `limit=N` devolvía N-1 velas en Binance, y Bitunix spot colaba la vela EN CURSO

Comprobado contra la API real (2026-08-11 15:57 UTC, BTCUSDT 1h): Binance devolvía 4 velas
pidiendo 5 (porque la última era la vela en formación y se descartaba), y el kline de spot de
Bitunix devolvía la de las 15:00 — sin cerrar, con OHLC parcial, marcada `is_closed=True`.

Una vela parcial marcada como cerrada es de las peores cosas que puede tragarse un indicador:
su OHLC **cambia entre dos consultas del mismo instante**. Ya hay un incidente de esta familia
más arriba en este fichero (`last_closed_candle_open`).

Ahora se pide una de más y se descarta la no cerrada. Binance compara `close_time` contra el
reloj; Bitunix **deduce** la duración del intervalo del espaciado entre velas — no de una tabla,
porque sus dos klines usan códigos distintos para el mismo intervalo (`"240"` vs `"4h"`) y una
tabla duplicada se desincroniza.

Verificado en vivo, los cuatro caminos coinciden ahora:

```
ahora UTC: 2026-08-11 15:57
binance spot     -> 5 velas, ultima 14:00 close=63770.03
binance futures  -> 5 velas, ultima 14:00 close=63747.0
bitunix spot     -> 5 velas, ultima 14:00 close=63770.02
bitunix futures  -> 5 velas, ultima 14:00 close=63747.0
```

(Que spot y futuros difieran ~23 puntos es lo correcto: son mercados distintos. Que los dos
brokers coincidan **dentro** de cada mercado es la señal de que el selector llega al endpoint.)

### 7. El `market` no llegaba a los datos de mercado

`BinanceMarketDataProvider` llamaba siempre a `get_klines`/`get_symbol_ticker` (spot). Un grafo
configurado en futuros calculaba sus indicadores sobre precios de **contado** sin que nada lo
dijera. Es el mismo bug que ya está documentado más arriba para `place_order`, pero en el lado
de los datos — se arregló aquél y no éste. Ahora hay `futures_klines`, `futures_symbol_ticker`
y `futures_order_book`.

### 8. `place_oco_order` devolvía 2 elementos en el camino de error

La firma dice `tuple[str, OrderAck, OrderAck]` y el camino de éxito devuelve tres, pero el
`except` devolvía `return rejected, rejected`. Un rechazo de Binance reventaba en el
desempaquetado del llamador (`oco_id, tp, sl = ...`) con un `ValueError` que no menciona a
Binance por ningún lado — justo cuando lo que hacía falta era leer el motivo del rechazo.

### 9. `BrokerFactory` devolvía `None` en silencio

`AvailableBrokers` incluía `KUCOIN` y `MT5` sin implementación detrás (`case ...: pass`), y el
`case _: pass` final tragaba cualquier nombre desconocido. `get_broker("KUCOIN")` devolvía
`None` y quien lo recibiera reventaba mucho después con un `AttributeError` que no mencionaba
KuCoin. Ahora levanta `ValueError` nombrando los disponibles.

`MT5` se ha quitado del enum porque además **no cabe en esa firma**: `MT5Broker` no se conecta
con api_key/secret sino con login/password/servidor de un terminal instalado. Se construye
directamente.

### 10. La factory pedía credenciales de TESTNET a Bitunix, que no tiene testnet

`__create_bitunix__` hacía `credentials.resolve(bool(testnet))` con `testnet=True` **por
defecto**. Bitunix no tiene testnet, así que devolvía `("", "")` y la primera llamada fallaba
con un `10003 API key inexistente` que no dice nada de la causa real. Y ocurría con el default,
o sea siempre que alguien no lo pensara.

Ahora la factory llama a `resolve(False)` para Bitunix, y `BrokerCredentials.resolve()` cae a
las claves de producción cuando no hay de testnet.

### 11. `MarketDataProvider.async_get_candles` era `@abstractmethod` y nadie lo cumplía

Las tres implementaciones (Binance, Bitunix, Yahoo) lo tenían como `pass`: devolvían `None` en
silencio. Ahora es concreto y levanta `NotImplementedError`.

### 12. Bitunix `list_active()` devolvía `[]`

Se lee como "este broker no opera nada". El paquete de PyPI tiene `get_trading_pairs()`,
simplemente no se llamaba.

## Lo que se ha CERRADO del pendiente anterior

**"El camino de SPOT tiene la misma inferencia"** (§ *Bitunix futuros daba por LLENADA toda
orden cancelada a mano*): `get_order_status` en spot hacía `if not data: return FILLED`, o sea
que una orden cancelada a mano se marcaba como llenada — el mismo bug que se arregló en futuros.

Se dejó sin tocar entonces porque no había endpoint de detalle de spot con el que sustituirla.
La solución ahora **no necesita uno**: se combinan las dos fuentes que sí existen.

- `query_matching_orders` devuelve las **ejecuciones** de la orden. Si trae algo, la orden llenó.
- Si no trae nada, se mira la lista de **pendientes**. Si aparece ahí, sigue viva.
- Si no aparece en **ninguno de los dos sitios**, se levanta `BitunixApiError`. No se inventa un
  estado: "no la encuentro" no es "llenó".

Vive en `account_data/bitunix.py::get_order`. Cubierto por
`test_broker_contract.py::OrdenesTests::test_spot_de_bitunix_levanta_si_no_conoce_la_orden`.

## Cambios que ROMPEN a los consumidores

`luarix-orchestator` y `luarix-web-server` consumen este paquete. Estos cambios de firma hay que
propagarlos:

| Qué | Antes | Ahora |
|---|---|---|
| `Broker.get_positions()` | `dict` (`{símbolo: cantidad}` o `{activo: saldo}` según broker) | `list[BrokerPosition]`, con `market=` y `symbol=` |
| `Broker.get_open_orders()` | `list[dict]` crudo del exchange | `list[OrderInfo]` (el crudo sigue en `.raw`) |
| `Broker.update_order()` | `(symbol, order_id)` | `(symbol, order_id, *, price, quantity, stop_price, tp_price, sl_price, market)` |
| `Broker.get_balance()` | `(market)` | `(market, asset=None)` |
| `Broker.get_ticker()` | `(symbol)` | `(symbol, market="spot")` |
| `AvailableBrokers` | tenía `KUCOIN` y `MT5` | solo `PAPER`, `BINANCE`, `BITUNIX` |
| `BrokerFactory.get_broker()` | devolvía `None` si no lo conocía | levanta `ValueError` |

Los añadidos con default (`asset`, `market`, `symbol`) no rompen llamadas posicionales
existentes; los que cambian el **tipo de retorno** (`get_positions`, `get_open_orders`) sí.

`MarketDataProvider`: `get_last_candles` y `get_historical_candles` ganan `market=` y
`get_last_candles` gana `format=`. Cualquier provider de fuera de este repo que herede de la
clase abstracta tiene que actualizar sus firmas.

## Lo que sigue SIN verificar contra la API real

Se declara aquí para que nadie lo dé por probado:

- **Todo el camino de Binance futuros.** La cuenta disponible tiene restricción geográfica sobre
  esos endpoints (ya estaba anotado más arriba para `place_order`; aplica igual a
  `futures_position_information`, `futures_modify_order` y `futures_change_leverage`). La forma
  de las llamadas sale de la documentación y de la firma de `python-binance`.
- **Bitunix `modify_order`, `pending_positions` y `change_leverage`.** Los tres endpoints están
  documentados por Bitunix pero no se han ejercitado contra la cuenta real — hacen falta las API
  keys, que el usuario todavía no ha puesto en el `.env`.
- **Los nombres de campo de `pending_positions`.** `qty`, `side`, `avgOpenPrice`, `markPrice`,
  `unrealizedPNL`, `positionId`, `marginMode` salen de la documentación. Si alguno no coincide,
  `BrokerPosition.from_bitunix_futures` devolverá ceros en vez de reventar — mirar `.raw` es lo
  primero que hay que hacer si una posición sale con `entry_price=0.0`.
- **El libro de órdenes de futuros de Bitunix** levanta `NotImplementedError` a propósito: su
  endpoint no se ha verificado y devolver el de spot sería mentir.

Lo que **sí** está verificado contra la API real en esta sesión: los cuatro caminos de velas y
precio (públicos, sin credenciales), incluida la paginación del histórico de Bitunix
(59 velas de 4h en 10 días, sin huecos).

## `.env` limpiado

El `.env` del repo llevaba credenciales de **otros** proyectos: `TELEGRAM_API_TOKEN`,
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, y los cinco pares
`LUARIX_BACKEND_SERVER_*` / `LUARIX_BACKTESTING_SERVER_*` de Supabase. Ninguna de esas variables
la lee este paquete: son del backend Django (`luarix_rest_api`). El `.env.example`, además,
documentaba **solo** las de Django (`DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`,
`CORS_ALLOWED_ORIGINS`...) y ni una de las de este repo.

Ahora los dos ficheros contienen solo lo que este paquete lee:
`TRADING_MODE`, `ALLOW_LIVE_TRADING`, `BINANCE_API_KEY`/`_SECRET`,
`BINANCE_TEST_API_KEY`/`_SECRET`, `BITUNIX_API_KEY`/`_SECRET`.

`TEST_BINANCE_API_KEY` y `TEST_BINANCE_API_SECRET` (el nombre viejo) se siguen aceptando como
alias en `BrokerCredentials._ENV_VARS`, para que un `.env` antiguo no deje a nadie mirando un
"credenciales vacías" sin explicación.

## Notas de operación que conviene no re-descubrir

- **Bitunix restringe las API keys por IP.** Si la IP no está en la whitelist, TODA llamada
  firmada falla con `10004 Invalid IP`; los klines siguen funcionando porque son públicos. Esa
  asimetría es útil para diagnosticar: si el precio va y el saldo no, es la key o la IP. La IP
  correcta es la de `curl https://fapi.bitunix.com/cdn-cgi/trace` (campo `ip=`), que **no** tiene
  por qué coincidir con la de un "what is my IP" genérico.
- **Bitunix no tiene testnet.** Lo que se coloque con esas claves es dinero real.
- **`test_order=True` en Binance no coloca nada.** Valida contra la API y devuelve `NEW` sin
  fills. Es el default de la factory: quien espere ver una orden en el exchange y no la vea,
  que mire esto primero.
- **Editar en spot cambia el `order_id`.** No hay enmienda nativa en ninguno de los dos
  exchanges para spot: es cancelar y recolocar. Y no es atómico — entre las dos llamadas la
  protección no existe.

## Cuadernos de prueba

`notebooks/`, uno por bloque de operaciones. Escritos para ejecutarse contra la cuenta real del
usuario en cuanto ponga las claves:

- `00_setup_y_credenciales.ipynb` — factory, `.env`, y una llamada pública + una firmada para
  distinguir "no hay conexión" de "las credenciales o la IP están mal".
- `01_velas.ipynb` — última vela, listado, rango de fechas, spot vs futuros lado a lado,
  comprobación de huecos. Todo público: funciona sin claves.
- `02_saldo_y_posiciones.ipynb` — saldo total y filtrado, las dos bolsas, posiciones,
  apalancamiento.
- `03_ordenes.ipynb` — abiertas, crear, detalle por id, editar, cancelar, y un ciclo completo.
  **Las celdas que colocan están comentadas**: hay que descomentarlas a conciencia.

Se generan desde un script (`.ipynb` es JSON con el código troceado en listas de líneas y
editarlo a mano rompe el fichero); si hay que tocarlos mucho, es más cómodo regenerarlos.

---

# Documentación MkDocs y hallazgos al escribirla (2026-08-11)

Se documentó el paquete entero con MkDocs (`mkdocs.yml` + `docs/`, 21 páginas)
porque lo van a consumir `luarix-web-server` y `luarix-orchestator` y necesitan
saber usar la factory, colocar órdenes, poner TP/SL, leer balance, etc.

`docs/plans/` (este fichero incluido) queda **fuera del sitio** vía
`exclude_docs`: son notas internas de trabajo, no documentación de la librería.
Siguen en el repo.

Construye limpio en modo estricto (`mkdocs build --strict`), o sea sin enlaces
internos rotos.

## Hallazgo 1 — `MT5Broker` no declaraba ningún flag de capacidad — ARREGLADO

Al montar la tabla de capacidades para la documentación salió que `MT5Broker`
no declara `supports_oco`, `supports_futures` ni `supports_independent_orders`:
heredaba los tres en `False` de `Broker`.

Pero su `place_oco_order` **sí funciona**: pone el stop-loss y el take-profit
sobre la POSICIÓN y es el servidor de MT5 quien los gestiona. Una sola llamada
con exclusividad garantizada, que es exactamente lo que el flag promete aunque
MT5 no lo llame "OCO".

**Consecuencia**: con `supports_oco = False` Y `supports_independent_orders =
False`, un motor que consulte los dos flags para decidir cómo proteger una
posición se queda **sin ningún camino válido** contra este broker. No es un bug
que haya mordido todavía (nadie opera MT5 hoy), pero estaba armado.

Arreglado declarando los tres explícitamente, con el motivo de cada uno:

| | Valor | Por qué |
|---|---|---|
| `supports_oco` | `True` | `place_oco_order` funciona vía SL/TP de la posición |
| `supports_futures` | `False` | MT5 no separa spot de derivados; ignora `market` |
| `supports_independent_orders` | `False` | `update_order` ni siquiera modifica |

Tabla completa tras el cambio:

```
broker            futures    oco  independent
BinanceBroker        True   True         True
BitunixBroker        True  False         True
PaperBroker         False   True         True
MT5Broker           False   True        False
```

## Hallazgo 2 — `luarix-web-server` tiene los imports ROTOS (no es este repo)

Revisando cómo consumen el paquete los dos repos, para que la sección de
integración no fuera inventada, apareció que **`luarix-web-server` no puede
importar este paquete hoy**. Un find-and-replace mal hecho dejó
`marketdata/services.py` así:

```python
from luarix_trading_framework.brokers.base import Broker      # el módulo no existe
ranges.luarix_trading_frameworkend((range_start, previous))   # era `ranges.append(...)`
# ... if 2+ real providers hluarix_trading_frameworken to have ...   era `happen`
```

O sea que el reemplazo pisó la subcadena `app` **dentro de otras palabras**:
`append` → `luarix_trading_frameworkend`, `happen` → `hluarix_trading_frameworken`.
Hay al menos 4 sitios así en ese fichero.

La confusión de fondo: `luarix_trading_framework` es el nombre de
**distribución** (el de `pip install`), no el de importación. El de importación
es `luarix_trading`.

Además, otros módulos de ese repo siguen apuntando al namespace muerto `app.*`:

| Fichero | Import actual | Debe ser |
|---|---|---|
| `marketdata/services.py` | `luarix_trading_framework.brokers.*` | `luarix_trading.brokers.*` |
| `backtesting/execution/paper_runner.py` | `app.brokers.paper` | `luarix_trading.brokers.paper` |
| `marketdata/coverage.py` | `app.brokers.market_data.provider` | `luarix_trading.brokers.market_data.provider` |
| `marketdata/tests/test_bitunix_historical_candles.py` | `app.brokers.market_data.bitunix` | `luarix_trading.brokers.market_data.bitunix` |
| `backtesting/tests/test_paper_runner_independent_orders.py` | `app.brokers.paper` | `luarix_trading.brokers.paper` |

**No se ha tocado nada de ese repo** -- queda anotado acá y en
`docs/integracion/web-server.md` con la lista de ficheros.

## Hallazgo 3 — los adaptadores de `luarix-orchestator` rompen con el contrato nuevo

Verificado leyendo `workflow/adapters/trading_framework_broker_adapter.py`. Tres
puntos concretos, ya listados en la sección de cambios que rompen consumidores
pero ahora con el call site exacto:

1. `get_positions()` -> `return dict(self.broker.get_positions())` revienta con
   `TypeError`: ahora es `list[BrokerPosition]`.
2. `get_open_orders()` -> `_external_order_info(raw_order)` espera un `dict`;
   ahora llegan `OrderInfo`. **Ese método sobra entero**: era un best-effort
   sobre alias de clave (`order_id`/`orderId`/`id`, `origQty`/`qty`) cuyo propio
   docstring lo declaraba "sin verificar todavía contra un broker real". Es
   exactamente lo que hace `OrderInfo`, que sí está verificado y con tests.
3. **Ninguna** llamada de los adaptadores pasa `market`, así que un grafo
   configurado en futuros consulta posiciones, balance y velas de SPOT. Es el
   mismo bug que ya se arregló en la colocación de órdenes (`OrderRequest.market`)
   pero vivo todavía en las consultas. Los puertos (`BrokerGateway`,
   `MarketDataGateway`) no tienen el concepto de mercado -- hay que añadírselo o
   fijarlo en el constructor del adaptador.

También se anotó que `TradingFrameworkMarketDataAdapter` ya no necesita su
`isinstance(candles, DataFrame)`: existía porque el tipo de retorno variaba
según el broker, y ahora se puede pedir `format=DataFormat.OHLCV` explícito.

## `PaperBroker.get_positions()`/`mark_to_market_equity()` -- `state.positions` nunca se poblaba -- ARREGLADO 2026-08-13

Retomando el "Hallazgo 3" de arriba (punto 1: `get_positions()` rompía a
`luarix-orchestator`) durante una auditoría de qué le faltaba a la
migración de ejecución de `luarix-web-server` a ese repo. Verificado con
`grep` exhaustivo: `PaperPosition` (línea 140) se declara pero **nunca se
instancia** en código de producción de este repo -- el único
`PaperPosition(...)`/`state.positions.append(...)` de todo el repo vivía en
`tests/test_paper_broker_mark_to_market_equity.py`, construido a mano. Los
fills reales viven en `state.filled_orders`/`state.oco_orders` (poblados de
verdad en `_fill()`), un campo que `state.positions` nunca sincronizaba.

**Consecuencia real**: `get_positions()` siempre devolvía `[]`, y
`mark_to_market_equity()` siempre devolvía solo la caja (nunca el valor de
mercado de una posición abierta) -- en CUALQUIER corrida real, no solo en
el caso hipotético. `luarix-web-server` nunca lo notó porque
`workflows/execution/operations_snapshot.py::_pair_entries_with_ocos`
evita `get_positions()` a propósito, reconstruyendo desde
`filled_orders`/`oco_orders` directo -- ese código SÍ funciona. El bug solo
mordía a `luarix-orchestator` (`workflow.backtesting.BacktestRunner` llama
`mark_to_market_equity` en cada vela) y a cualquier nodo `open-positions`.

**Arreglo**: portado el mismo criterio de `_pair_entries_with_ocos` (que
YA estaba probado en producción) a un método nuevo,
`PaperBroker._open_position_entries()` -- empareja cada entrada FILLED
(no pierna de OCO) con la próxima OCO creada para el mismo símbolo, en
orden cronológico; una entrada sin OCO asociada (sin stop-loss/take-profit)
queda abierta indefinidamente. `get_positions()`/`mark_to_market_equity()`
ahora leen de ahí, no de `state.positions` (que sigue existiendo, sin
tocar, por si algún consumidor externo la usa -- pero confirmado que
ninguno lo hace hoy).

`tests/test_paper_broker_mark_to_market_equity.py` reescrito para armar el
estado vía `filled_orders`/`oco_orders` (antes armaba `PaperPosition` a
mano, probando un mecanismo que el código real nunca ejercitaba) -- mismos
5 casos de negocio que antes (corto no infla equity, largo suma su valor,
cerradas se ignoran, otros símbolos se ignoran) + `GetPositionsTests` (5
casos nuevos, incluido el emparejamiento cronológico con 2 entradas
secuenciales del mismo símbolo). Suite completa: **94 passed, 1 failed**
(el 1 que falla, `test_binance_si_respeta_testnet`, es una llamada de red
real a la API de Binance, preexistente, no relacionada).

## `TradingFrameworkBrokerAdapter` en `luarix-orchestator`: los 2 bugs restantes del "Hallazgo 3" -- 1 arreglado, 1 documentado (2026-08-13)

Misma sesión que la entrada de arriba, verificando el resto de lo que
"Hallazgo 3" ya había anotado:

- **Punto 1 (`get_positions()` revienta con `dict(...)`)**: arreglado en
  `luarix-orchestator` en la misma pasada -- ver
  `luarix-orchestator/docs/plans/bugs.md`.
- **Punto 2 (`get_open_orders()`/`_external_order_info` espera un `dict`,
  llega `OrderInfo`)**: **arreglado** -- `_external_order_info` ahora lee
  los atributos tipados de `OrderInfo` directo (`.order_id`, `.symbol`,
  `.side.value`, `.quantity`, `.price`), sin adivinar alias de clave. Test
  nuevo con un broker doble (no `PaperBroker`) que devuelve `OrderInfo`
  real, confirmando que la traducción no revienta. Ver
  `luarix-orchestator/docs/plans/bugs.md` para el detalle.
- **Punto 3 (ningún adaptador pasa `market`, así que un grafo de futuros
  consulta spot)**: **NO arreglado, documentado a propósito**. Los puertos
  de `luarix-orchestator` (`BrokerGateway`) no tienen el concepto de
  mercado todavía -- añadirlo bien (¿parámetro del constructor del
  adaptador? ¿de cada nodo?) es una decisión de diseño que se solapa
  directamente con portar trading en vivo (la primera vez que un grafo real
  necesita operar futuros de verdad) -- no vale la pena decidirlo aislado
  hoy. Queda anotado en `docs/plans/execution_migration_roadmap.md` de ese
  repo como parte de esa iniciativa.

Checklists de migración completos en `docs/integracion/orchestator.md` y
`docs/integracion/web-server.md`.


---

## [2026-10-16] Fase 3 — contrato por capacidades

Diseño: `docs/plans/fase3_contrato_capacidades.md`. Lo que conviene no
re-descubrir:

### El libro de SPOT de Bitunix salía VACÍO, siempre — ARREGLADO

`BitunixMarketDataProvider.get_order_book` llamaba
`client.get_depth_data(symbol, 2)`. `precision` NO es un número de decimales:
es el TAMAÑO del agrupamiento de precios (`"0.01"`, `"0.1"`, `"1"`...) y depende
del par (cada par anuncia sus `precisions` en `/api/spot/v1/common/coin_pair/list`).
Con `2` la API responde `{"code":"0","msg":"result.success","data":{"asks":[],"bids":[]}}`
-- éxito y libro vacío, sin ningún error. Nadie lo notó porque nadie leía el
libro. Ahora se usa la precisión más fina del par (`finest_spot_precision`).

### El libro de FUTUROS de Bitunix: solo `limit` 1, 5, 15, 50 o "max"

`/api/v1/futures/market/depth` es público y funciona (verificado
2026-10-16), pero cualquier otro `limit` devuelve
`10008 Parameter 20 does not match, alternative value ["1","5","15","50","max"]`
-- y 20 es el default de `get_order_book`. `BitunixFuturesClient.depth` pide
el menor permitido que cubre lo pedido y el broker recorta.

### Los pares de spot de Bitunix vienen en minúsculas

`coin_pair/list` da `"symbol": "btcusdt"`, aunque el resto de la API acepta y
devuelve mayúsculas. `spot_pairs()` los indexa en MAYÚSCULAS; `list_symbols`
y `get_symbol_rules` devuelven mayúsculas. (El viejo `list_active` devolvía
las minúsculas tal cual.)

### Reglas de Bitunix: precisión en número de decimales

`basePrecision=5` -> `step_size=0.00001`, `quotePrecision=2` -> `tick_size=0.01`.
Validado comparando con el libro real: todos los precios del libro son
múltiplos exactos del tick calculado, en spot y en futuros. Todavía NO
contrastado con el rechazo de una orden real. Futuros no publica importe
mínimo: `min_notional=0`.

### Mecánica de las capacidades (para no romperla)

- Una ABC se registra como "la" capacidad solo si declara
  `capability = Capability.X` EN SU PROPIO CUERPO. Redeclarar una capacidad ya
  registrada en otra clase levanta `TypeError` (test incluido).
- `Broker` hereda TODAS las capacidades de trading por compatibilidad. Un
  broker que no soporte alguna la pone en `unsupported`; si no, mentiría
  (Bitunix: `NATIVE_OCO`; Paper y MT5: `MARKET_DATA`, `DERIVATIVES`).
- `supports_oco`/`supports_futures` son descriptores DERIVADOS
  (`_DerivedClassFlag`), legibles desde la clase y la instancia. Un doble de
  test que los fije con un atributo normal sigue funcionando (lo tapa).
  `tests/test_broker_flags_compat.py` fija los valores de antes de la fase.
- `ExchangeFactory` guarda RUTAS "modulo:Clase": importarla no carga ningún
  SDK (test en subproceso limpio). Pedir las capacidades de un exchange SÍ
  importa su módulo.

### Verificación de la fase

- Librería: 206 tests (164 de antes sin tocar + 42 nuevos).
- APIs reales (públicas): libro, reglas y `list_symbols` en Binance y Bitunix,
  spot y futuros.
- orchestator (rama `fix/position-exit-luarix-exchange`), con las rutas viejas
  bloqueadas: 1037 passed, mismo fallo preexistente.
- web-server (`saas`), con orchestator parado: el MISMO conjunto de 21 fallos
  que la línea base del 2026-10-14 (paridad sin orchestator). Con orchestator
  arrancado, muestra de paridad (TechnicalRatings, broker modes, MACD,
  Supertrend): 7/7 OK.
- Ningún consumidor hereda de `Broker` (comprobado con grep): el abstracto
  nuevo `list_symbols` no les afecta.

---

## GRAVE — `PollingMarketDataFeed` entregaba cada vela UN PERIODO TARDE (2026-10-18) — ARREGLADO

`brokers/market_data/live_feed.py::PollingMarketDataFeed._poll_one` emitía
`candles[-2]` suponiendo que la última vela devuelta "seguía formándose". Eso
era verdad antes del arreglo del 2026-08-11 ("limit=N devolvía N-1 velas..."),
que hizo que los proveedores DESCARTEN la vela en curso. Desde entonces
`[-2]` era la ANTERIOR a la última cerrada.

Verificado contra la API real (Binance y Bitunix, BTCUSDT 1m): a las
05:30:52 UTC las velas devueltas eran 05:27, 05:28, 05:29 (todas cerradas) y
el feed emitía la de las 05:28.

**Quién lo sufría**: toda estrategia en vivo sobre polling -- en web-server
(`marketdata/services.get_live_feed_for`) y en orchestator
(`TradingFrameworkLiveFeedAdapter`) es TODO broker que no sea Binance (que va
por WebSocket), o sea Bitunix. Con velas de 1h, cada decisión llegaba una
hora tarde. Ninguno de los dos lo compensaba (comprobado con grep).

**Arreglo**: emite la última vela con `is_closed=True`. Test de regresión
`tests/test_polling_feed_latest_closed.py` (falla con el código viejo).

⚠ **Llega a producción solo cuando se suba el commit fijado de la librería**
en framework / web-server / orchestator. Al hacerlo, las estrategias de
Bitunix empezarán a recibir las velas un periodo ANTES que hoy (lo correcto):
avisar al equipo.

## [2026-10-18] Fase 4 — infraestructura de streaming

Diseño: `docs/plans/fase4_streaming.md`. Guía: `docs/guia/streaming.md`.

- `run_with_reconnect`: solo reintenta errores de RED (`OSError`, que cubre
  `requests.RequestException`, + `aiohttp.ClientError` + timeouts). Cualquier
  otro error termina el stream -- a propósito: un `ValueError` por símbolo
  inválido reintentado para siempre es un fallo silencioso.
- El polling guarda su estado (marcas de agua de velas, órdenes conocidas)
  FUERA de la sesión: una reconexión no reemite historia ni pierde órdenes.
- `PollingOrderUpdateStream` nunca infiere `FILLED` por desaparición: pregunta
  `get_order()` y, si falla, reintenta en la siguiente vuelta (avisando con el
  motivo real cada `LOG_EVERY` fallos).
- `websocket_session` termina cuando el servidor cierra (y
  `run_with_reconnect` la reabre); con `stop` corta aunque el servidor no
  mande nada (test con servidor aiohttp real que se queda mudo).
- En los tests con servidor aiohttp local: si el handler se queda esperando,
  `runner.cleanup()` espera a que acabe -- el handler debe terminar con un
  evento que el test active ANTES del cleanup, o el test se cuelga.

Verificación: 227 tests de la librería; en vivo, a través de `StreamRunner`,
libros y velas de 1m de Binance spot y Bitunix futuros, y `websocket_session`
contra el kline público de Binance (ver `docs/estado.md`).

---

## [2026-10-18] Fase 5 — WebSockets de Binance y Bitunix

Código: `brokers/binance_streams.py`, `brokers/bitunix_streams.py`,
`StreamingMarketDataFeed` en `live_feed.py`. Prueba real: `scripts/probar_streams.py`.

### Binance futuros reparte sus streams por RUTA — y la equivocada se queda MUDA

Comprobado contra `fstream.binance.com`: `kline` y `aggTrade` solo llegan por
`/market/ws` o `/market/stream`; `depthN` por `/public/...` (y todavía por
`/ws`). `wss://fstream.binance.com/ws/btcusdt@kline_1m` conecta bien y NO
manda nada, sin error. Datos privados: `/private/ws/<listenKey>`.
Spot sigue todo en `stream.binance.com:9443`.

### Binance spot ya no tiene listenKey

Retirado el 2026-02-20 (POST/PUT/DELETE `/api/v3/userDataStream`). El flujo de
usuario va por la WebSocket API (`wss://ws-api.binance.com:443/ws-api/v3`) con
`userDataStream.subscribe.signature`: params `apiKey`, `timestamp`,
`recvWindow` firmados HMAC-SHA256 ordenados alfabéticamente (sirve cualquier
tipo de clave). Los eventos llegan ENVUELTOS: `{"subscriptionId": 0, "event": {...}}`.
En una cancelación, `c` es el id de la PETICIÓN de cancelar; el de la orden
original va en `C`. Verificado en testnet: NEW, CANCELED, FILLED (precio de
llenado `L`, medio = `Z`/`z`, comisión `n`/`N`).

### GRAVE — reloj local desfasado: TODA orden de Binance salía REJECTED — ARREGLADO

El reloj de esta máquina iba ~4,3 s ADELANTADO. Binance rechaza cualquier
petición firmada con `timestamp` más de 1 s por delante (`-1021`). El stream
privado fallaba y, peor, `place_order` devolvía `REJECTED` (python-binance
firma con `time.time()`). Ahora `BinanceBroker` implementa `ServerClock` y en
`__init__` llama a `sync_clock()`, que fija `client.timestamp_offset` con el
desfase medido (si falla, avisa y sigue con el reloj local). El stream spot
re-sincroniza en cada conexión. Efecto secundario: construir un
`BinanceBroker` hace una petición más (`get_server_time`); la suite de
web-server pasó de ~21 s a ~39 s por los tests que construyen brokers reales.

⚠ Mirando timestamps en logs de ESTA máquina hay que restar ~4 s: las velas
de Bitunix parecían llegar 5-6 s tarde y llegan ~1,3 s después del cierre
(el `ts` del servidor lo delata).

### Bitunix futuros SÍ tiene WebSocket privado de órdenes

La nota de más arriba ("Bitunix no tiene stream de usuario") salió de mirar el
SDK de PyPI, que solo cubre spot. Futuros tiene `wss://fapi.bitunix.com/private/`
con login (`sha256(sha256(nonce+timestamp+apiKey)+secret)`, timestamp en
SEGUNDOS) y canal `order`. Límite: 5 mensajes/s por conexión (pings
incluidos) -> todas las suscripciones en UN mensaje.

- Al conectar, Bitunix manda primero `{"op":"connect","data":{"result":true}}`.
  NO es la respuesta al login: tomarla por ella daba por buena cualquier clave
  (cazado con claves falsas). La respuesta real es `{"op":"login","data":{...,"result":false|true}}`.
- ⚠ SIN VERIFICAR con cuenta real: `dealAmount` ("Deal amount") se toma como
  cantidad ejecutada en base, como `tradeQty` en REST. Si fuera el importe en
  USDT, `filled_qty` saldría multiplicado por el precio. Es lo primero que hay
  que mirar en la prueba real.

### Bitunix: el kline por WebSocket no dice cuándo cierra una vela

Manda instantáneas cada 500 ms con `o/h/l/c/b/q` y un `ts`, sin hora de
apertura ni flag de cierre. Sacar la vela cerrada de la última instantánea
podría perder los trades de los últimos 500 ms. Solución: el WebSocket hace de
DISPARADOR (cambia `ts // duración`) y la vela exacta se lee por REST (el REST
la tiene al primer intento, verificado). 3d/1w/1M no tienen duración fija ->
polling. El spot de Bitunix tiene otra API WebSocket (otro host), no
implementada: spot va por polling (`native_stream_markets`).

### `_iso_to_ms` desplazaba 4 horas — ARREGLADO antes de publicar

Al recortar los 9 decimales de `2024-05-16T08:13:09.123456789Z`, juntaba también
los dígitos de la zona (`+00:00`), perdía la zona y `datetime` lo interpretaba en
hora local (UTC-4). Lo cazó el test con el valor calculado aparte.

### Sin verificar todavía (no hay claves)

- Binance futuros privado (`listenKey`, `ORDER_TRADE_UPDATE`). Además, la cuenta
  tenía restricción geográfica en futuros.
- URLs de testnet de futuros de Binance.
- Canal `order` de Bitunix con una cuenta real (sí el login y su rechazo).

---

## [2026-10-18] Fase 6 — Kraken y Bitfinex (y lado librería de la Fase 7)

Portados de luarix-arbitrage-web-server con su lógica INTACTA (traducción de
activos, reglas, libro con checksum CRC-32, firma, comisiones, reloj por
WebSocket) y sus tests con mensajes reales. Añadido para Luarix: velas REST y
por WebSocket, precio, libro REST, pares, activos.

- **Arbitraje ya tiene 9 conectores** (2026-10: Bybit, Gate, HTX, KuCoin, MEXC,
  OKX además de Binance, Kraken, Bitfinex) y su contrato creció con
  `fetch_assets` / `AssetListing` / `AssetNetworkInfo`. Lo que se mire de su
  repo hay que releerlo: cambia rápido.
- **Bitfinex, velas**: columnas `[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]` -- el
  cierre va ANTES que el máximo. Test que comprueba `low <= open/close <= high`
  con datos reales.
- **Kraken, velas REST**: la última fila es la vela en curso; cerradas = las de
  `time <= result["last"]`. Solo sirve las 720 más recientes por intervalo:
  `get_historical_candles` levanta si `start` es anterior (no rango incompleto).
- **Kraken/Bitfinex, WebSocket de velas**: mandan la vela en curso sin marca de
  cierre. `streaming/candles.py::ClosedCandleEmitter`: WebSocket como disparador
  + REST para la vela exacta + temporizador en cada frontera (en un par sin
  operaciones el "periodo nuevo" solo llegaría con el siguiente trade). Bitunix
  pasó a usarlo también (antes no tenía temporizador).
- **Checksums**: necesitan los números como TEXTO exacto. `websocket_session`
  acepta `loads=` (Kraken `parse_float=str`; Bitfinex además `parse_int=str`).
- **Reloj de Kraken/Bitfinex**: devolvían 0 si en la ventana no llegaba ninguna
  marca (pasa con Bitfinex tras muchas conexiones seguidas: límite por minuto).
  Un 0 falsea TODAS las latencias (salía 4,4 s en vez de ~150 ms). Ahora se
  conserva el último desfase medido, con aviso. Arbitraje tiene ese mismo
  comportamiento (devuelve 0): conviene corregirlo allí también.
- **Binance para arbitraje**: `public_only=True` en `create()` (datos públicos
  sin claves). Latencia de spot por `miniTicker` en la misma conexión (el libro
  parcial no trae hora), como hace arbitraje; futuros por `E` del libro
  (`OrderBook.event_ms`). `FEES` con `/api/v3/account/commission` (descuento BNB
  solo si activo en cuenta Y par) y `/fapi/v1/commissionRate`.
- **`aio.AsyncExchange`**: la forma exacta del contrato async de arbitraje sobre
  cualquier exchange de la librería. Probado con el motor real de arbitraje
  (`scripts/probar_adaptador_arbitraje.py`): ciclos triangulares en Kraken,
  Bitfinex y Binance, latencias medianas 109-134 ms.
- **`pandas` perezoso en `Candle`**: importar capacidades, Kraken, Bitfinex o
  `aio` ya no carga pandas (sigue siendo dependencia por el `PaperBroker`).
- **Test intermitente** (1 vez de ~10): casi seguro
  `test_credentials_and_factory.py::test_binance_si_respeta_testnet`, que construye un
  `BinanceBroker` real (ping + hora del servidor por red).

---

## [2026-10-18] Los 6 conectores restantes de arbitraje (Bybit, OKX, Gate, HTX, KuCoin, MEXC)

Portados de luarix-arbitrage-web-server con su lógica INTACTA a
`luarix_exchange/exchanges/` sobre una base común (`_base.py::SpotConnector`:
reglas cacheadas, reloj, libro por WebSocket con `run_with_reconnect`,
comisiones, activos). Sus tests (`test_new_exchanges.py` de arbitraje) están en
`tests/test_spot_connectors.py`.

- **Binance `measure_order_roundtrip_ms`**: arbitraje firma con la hora LOCAL;
  en esta máquina (reloj ~4,3 s adelantado) eso da -1021. La librería firma con
  la hora de Binance (`sync_clock`). Verificado en testnet: ~210 ms.
- **HTX, reloj**: su `/v1/common/timestamp` por REST da una estimación ~200 ms
  peor que la del resto → latencias de ~320 ms en vez de ~100-170. Es el mismo
  método que usa arbitraje (mismo problema allí). Mejora: estimarlo con las
  marcas `ts` del WebSocket, como Kraken/Bitfinex.
- **HTX, WebSocket**: mensajes gzip; el `ping` lo manda el SERVIDOR y hay que
  responder `pong` con el mismo número — se usa el `ws` capturado en `on_open`.
- **OKX, keepalive**: `"ping"`/`"pong"` en TEXTO plano, no JSON
  (`websocket_session` acepta `keepalive` que devuelva `str`).
- **MEXC**: WebSocket solo protobuf (lector mínimo `_common.protobuf_fields`,
  sin depender de `protobuf`); 30 suscripciones por conexión → varias
  conexiones; si cae una, se reabren todas.
- **KuCoin**: hay que pedir un token (`bullet-public`) antes de cada conexión.
- **Credenciales con passphrase**: OKX y KuCoin (`*_API_PASSPHRASE`); entornos
  de pruebas `BYBIT_TESTNET_*` y `OKX_DEMO_*`.
- **Sin verificar** (necesitan claves): comisiones de los seis y órdenes de
  prueba de Bybit testnet / OKX demo.
