# Estado y limitaciones

**Léelo antes de operar con dinero.**

Esta página declara qué está verificado contra una API real y qué no. El
registro completo, con ids de orden y reconstrucción de cada incidente, está en
`docs/plans/bugs.md` del repo.

---

## Verificado contra la API real

Comprobado en vivo el 2026-08-11, sin credenciales (endpoints públicos):

- **Velas y precio**, los cuatro caminos: Binance spot, Binance futuros,
  Bitunix spot, Bitunix futuros.
- **Paginación del histórico de Bitunix**: 59 velas de 4h en 10 días, sin
  huecos. Antes, 1200 velas de 4h en 6 páginas, también sin huecos.
- **Orden ascendente y descarte de la vela en curso** en los cuatro:

  ```
  ahora UTC: 2026-08-11 15:57
  binance spot     -> 5 velas, ultima 14:00 close=63770.03
  binance futures  -> 5 velas, ultima 14:00 close=63747.0
  bitunix spot     -> 5 velas, ultima 14:00 close=63770.02
  bitunix futures  -> 5 velas, ultima 14:00 close=63747.0
  ```

Comprobado en vivo el 2026-10-16 (Fase 3), sin credenciales:

- **`get_order_book` normalizado**, los cuatro caminos (Binance spot/futuros,
  Bitunix spot/futuros): libros ordenados y con niveles.
- **`get_symbol_rules`** en los cuatro: los precios reales del libro son
  múltiplos exactos del `tick_size` calculado (lo que valida la lectura de
  `basePrecision`/`quotePrecision` de Bitunix como número de decimales).
- **`list_symbols`** en los cuatro (Binance 1380 spot / 793 futuros; Bitunix
  545 / 781 ese día).
- El libro de **spot de Bitunix salía VACÍO**: se pedía `precision=2` y la API
  respondía `success` con listas vacías. Arreglado (ver `docs/plans/bugs.md`).

Verificado en sesiones anteriores contra la cuenta real de Bitunix:

- La **firma** de `BitunixFuturesClient` (GET y POST).
- `place_order` de futuros: los campos `qty`, `side`, `orderType`, `effect`,
  `tradeSide`, `tpPrice`, `slPrice`.
- Los **valores aceptados**: `tradeSide` ∈ `["OPEN","CLOSE"]` (en minúsculas lo
  rechaza), `orderType` ∈ `["ALL","LIMIT","MARKET"]`.
- `get_pending_orders` y `get_order_detail`.
- Que el kline de spot **ignora** `limit`, `startTime` y `endTime`.

---

## NO verificado

!!! danger "Todo el camino de Binance FUTUROS"
    La cuenta disponible tiene **restricción geográfica** sobre los endpoints de
    futuros de Binance. La forma de las llamadas sale de la documentación de
    USDT-M Futures y de la firma de `python-binance`, **no de una respuesta
    real**.

    Afecta a: `futures_create_order`, `futures_position_information`,
    `futures_modify_order`, `futures_change_leverage`, `futures_get_order`,
    `futures_klines`, `futures_account_balance`.

    Probado solo con un cliente falso. Una entrada `SELL LIMIT 45.30` produce:

    ```
    LIMIT               SELL qty=0.083 price=45.30 timeInForce=GTC
    TAKE_PROFIT_MARKET  BUY  stopPrice=45.07 closePosition=true
    STOP_MARKET         BUY  stopPrice=45.75 closePosition=true
    ```

!!! warning "Tres endpoints nuevos de Bitunix"
    `modify_order`, `pending_positions` y `change_leverage` están documentados
    por Bitunix pero **no se han ejercitado contra la cuenta real** — hacen
    falta las API keys.

!!! warning "Los nombres de campo de `pending_positions`"
    `qty`, `side`, `avgOpenPrice`, `markPrice`, `unrealizedPNL`, `positionId`,
    `marginMode` salen de la documentación.

    Si alguno no coincide, `BrokerPosition.from_bitunix_futures` devuelve
    **ceros en vez de reventar**. Si ves una posición con `entry_price=0.0`,
    mira `.raw`: ahí está el nombre bueno.

---

## Limitaciones por diseño

### Bitunix

| | |
|---|---|
| **Sin testnet** | Dinero real siempre |
| **Sin OCO** | `place_oco_order` levanta. Usa `tp_price`/`sl_price` adjuntos |
| **Sin tipos de stop en futuros** | Solo `LIMIT` y `MARKET`. El stop va adjunto |
| **Spot no lista todas las órdenes** | Su endpoint exige símbolo. `get_open_orders(market="spot")` levanta |
| **Kline de spot sin rango** | Devuelve siempre ~201 velas. El histórico sale del kline de **futuros** (precios de perpetuo) |
| **Libro de futuros: `limit` solo 1, 5, 15 o 50** | Se pide el menor que cubre lo pedido y se recorta (verificado 2026-10-16) |
| **Saldo de futuros por moneda de margen** | Una llamada por moneda |
| **Intervalos desconocidos en spot caen a 1h** | En silencio. Comportamiento heredado |

### Binance

| | |
|---|---|
| **Modo cobertura sin soporte** | En hedge exige `positionSide` en vez de `reduceOnly`, y el broker no lee el modo de la cuenta |
| **`_futures_symbol_filters` sin caché** | Una llamada a `futures_exchange_info()` por orden |
| **`start_user_socket` comentado** | Sin push de estado de órdenes |
| **`test_order=True` no aplica a futuros** | |
| **`update_order` en spot cancela y recoloca** | El `order_id` cambia y no es atómico |

### MT5

| | |
|---|---|
| **Solo Windows** | |
| **Sin proveedores** | Ni velas ni órdenes abiertas ni detalle de orden |
| **`update_order` no modifica** | Con argumentos, levanta |
| **`get_balance` devuelve la clave `"USDT"`** | Aunque la cuenta esté en euros |

### PaperBroker

| | |
|---|---|
| **Sin latencia, slippage, rechazos ni liquidez** | |
| **Sin filtros de precisión** | Acepta cantidades que un exchange real rechazaría |
| **Sin fills parciales** | |
| **Sin futuros ni funding** | |
| **`mark_to_market_equity` valora un solo par** | Una corrida multi-par necesita un precio por símbolo |

---

## Ningún broker empuja estado de órdenes

!!! danger "Hay que PREGUNTAR"
    - Bitunix **no tiene** stream de datos de usuario.
    - El `start_user_socket` de Binance está **comentado**.

    Consecuencia: una orden colocada y luego cancelada a mano en la app del
    exchange se queda como `NEW` para siempre en cualquier sistema que espere un
    evento. Hay que sondear con `get_order_status()` / `get_order()`.

    Los feeds de `live_feed.py` solo traen **velas**.

---

## Notas de operación

### Whitelist de IP en Bitunix

Toda llamada firmada falla con `10004 Invalid IP` si la IP no está autorizada.
Los klines siguen funcionando (son públicos) — esa asimetría es la mejor
herramienta de diagnóstico.

```bash
curl https://fapi.bitunix.com/cdn-cgi/trace     # campo ip=
```

**No** uses un "what is my IP" genérico: pueden no coincidir.

!!! warning "En un servidor rompe la reconciliación entera"
    Si la IP del backend no está en la whitelist, ninguna consulta de estado
    funciona. Los errores de red se tragan correctamente, pero entonces **nada
    se reconcilia nunca** y el sistema parece estar al día cuando no lo está.

### `test_order=True` es el default de la factory

Y en Binance spot **no coloca nada**. Si esperas ver una orden y no aparece,
mira esto primero.

### Editar en spot cambia el `order_id`

No hay enmienda nativa en spot en ninguno de los dos exchanges. Y entre la
cancelación y la recolocación **la protección no existe**.

---

## Incidentes ya corregidos

Contexto de por qué el código es como es. Todos con datos reales en
`docs/plans/bugs.md`.

| Incidente | Qué pasó |
|---|---|
| **El TP abrió un corto real** | `tradeSide="OPEN"` fijo en modo HEDGE. Un `SELL` de take-profit no cerró el largo: abrió un corto independiente |
| **El stop nunca se colocaba** | `STOP_LOSS_LIMIT` (tipo de Binance) contra Bitunix futuros → `10008`. Posición abierta y sin protección, sin que el traceback lo dijera |
| **Tres posiciones fantasma** | "Ausente de pendientes" se leía como llenada. Tres cortos cancelados a mano quedaron `FILLED` y `ACTIVE`, con el estado falso congelado |
| **`update_order` de Binance** | Referenciaba 4 variables inexistentes: cancelaba la orden y reventaba con `NameError`, dejándola sin sustituta |
| **`get_positions()` de Bitunix** | Devolvía `{}` **fijo** |
| **`get_positions()` de Binance** | Devolvía saldos de **spot** aunque pidieras futuros |
| **Bitunix sin candado de trading real** | El doble candado existía solo en Binance |
| **Las velas al revés** | `klines[:limit]` sobre respuesta descendente: `limit=1` daba la vela **más antigua** |
| **La vela en curso** | Se colaba marcada `is_closed=True`, con OHLC que cambiaba entre consultas |
| **`market` no llegaba a los datos** | Un grafo en futuros calculaba indicadores sobre precios de **contado** |
| **El backtest "ganaba" con 0 trades** | `get_balance()["USDT"]` es la caja, no el capital: +30% y APR 198% con un corto abierto |
| **TP y SL invertidos en SHORT** | Entrada `SELL` a 45.30 con TP en 45.53 (pérdida garantizada) |
| **Una MARKET reventaba en la 1ª vela** | `float(None)` en el emparejamiento del `PaperBroker` |
| **El paquete no importaba** | `app/` era bytecode huérfano; los tests del consumidor se **salteaban** en silencio |

---

## Cómo verificar sin arriesgar

### 1. Endpoints públicos

```python
from binance import Client
from luarix_exchange.brokers.market_data.binance import BinanceMarketDataProvider

md = BinanceMarketDataProvider(Client())
print(md.get_candles("BTCUSDT", "1h", limit=3, market="futures"))
```

### 2. Sondear con un símbolo inválido

Verificado con Bitunix: un símbolo inválido se rechaza con `300105` **después**
de validar el cuerpo. Sirve para descubrir qué campos acepta un endpoint sin
colocar nada — un campo mal puesto devuelve `10008` nombrándolo.

!!! warning "Valida la FORMA, no las precondiciones"
    No detectó que un `tradeSide="CLOSE"` necesita `positionId`: el rechazo por
    símbolo ocurre antes.

### 3. Órdenes límite muy lejos del precio

Son órdenes **reales**, pero no llegan a ejecutarse y se pueden cancelar. Es lo
que hace `notebooks/03_ordenes.ipynb`.

### 4. `allow_live_trading=False`

Permite consultar toda la cuenta sin poder colocar por accidente.

---

## Tests

```bash
pytest        # 90 tests
```

| Fichero | Qué fija |
|---|---|
| `test_broker_contract.py` | Que Binance y Bitunix responden **lo mismo** a las mismas preguntas, en spot y futuros. Los dobles usan las respuestas reales de cada API, nombres de campo incluidos |
| `test_credentials_and_factory.py` | Precedencia de credenciales, `.env`, y los fallos que antes eran silenciosos |
| `test_bitunix_order_status.py` | El bug de las posiciones fantasma |
| `test_bitunix_response.py` | `unwrap()` |
| `test_execution_report_factory_parse.py` | Los execution reports del simulador |
| `test_paper_broker_mark_to_market_equity.py` | El equity con los números del incidente real |
