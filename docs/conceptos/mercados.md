# Spot y futuros

Casi todos los métodos del contrato aceptan `market="spot"` o
`market="futures"`. Esta página explica qué cambia de verdad al pasar uno u
otro, porque **no es un filtro**: son cuentas distintas, precios distintos y,
en Bitunix, APIs distintas en hosts distintos.

## Qué es cada cosa

| | Spot | Futuros (USDT-M perpetuos) |
|---|---|---|
| Qué se compra | El activo (tienes BTC) | Un contrato sobre su precio |
| Saldo | Uno por activo (BTC, ETH, USDT...) | Uno por **moneda de margen** (USDT) |
| "Posición" | No existe: hay **saldos** | Sí, con dirección, entrada y apalancamiento |
| Apalancamiento | No | Sí |
| Vender sin tener | No | Sí (abrir corto) |
| Precio | Contado | Perpetuo (difiere por base y funding) |

## Los precios NO son los mismos

Comprobado en vivo, BTCUSDT 1h, 2026-08-11 15:57 UTC:

```
binance spot     close=63770.03
binance futures  close=63747.0     ← ~23 puntos de diferencia
bitunix spot     close=63770.02
bitunix futures  close=63747.0
```

Los dos brokers coinciden **dentro** de cada mercado, y difieren **entre**
mercados. Eso es lo correcto: son instrumentos distintos.

!!! warning "Consecuencia práctica"
    Pedir velas de spot para operar el perpetuo no es una aproximación
    inofensiva: **mueve los niveles que calcule cualquier estrategia**. Un
    stop-loss puesto sobre un mínimo de spot puede quedar dentro del ruido
    normal del perpetuo, o al revés.

    Si spot y futuros te devuelven exactamente lo mismo, sospecha: significa
    que el selector no está llegando al endpoint. Es un bug que ya ocurrió —
    `BinanceMarketDataProvider` llamaba siempre a `get_klines` (spot) y un
    grafo configurado en futuros calculaba sus indicadores sobre precios de
    contado sin que nada lo dijera.

## Cómo se implementa en cada broker

### Binance

Los dos mercados son endpoints distintos del **mismo** `Client` de
`python-binance`:

| Operación | Spot | Futuros |
|---|---|---|
| Velas | `get_klines` | `futures_klines` |
| Precio | `get_symbol_ticker` | `futures_symbol_ticker` |
| Libro | `get_order_book` | `futures_order_book` |
| Saldo | `get_account` | `futures_account_balance` |
| Posiciones | — | `futures_position_information` |
| Órdenes abiertas | `get_open_orders` | `futures_get_open_orders` |
| Detalle de orden | `get_order` | `futures_get_order` |
| Colocar | `create_order` | `futures_create_order` |
| Modificar | (no existe) | `futures_modify_order` |
| Filtros de precisión | `get_symbol_info` | `futures_exchange_info` |

!!! danger "Los filtros de precisión NO son intercambiables"
    El mismo par tiene `stepSize` y `tickSize` **distintos** en cada mercado.
    Usar los de spot para una orden de futuros da un `-1111` de precisión que
    es imposible de diagnosticar desde el log. Por eso existen dos métodos
    separados, `_get_symbol_filters` y `_futures_symbol_filters`.

### Bitunix

Son **dos APIs distintas en dos hosts distintos**, y el paquete `bitunix` de
PyPI solo cubre una:

| | Spot | Futuros |
|---|---|---|
| Host | `openapi.bitunix.com` | `fapi.bitunix.com` |
| Cliente | `bitunix.BitunixClient` (PyPI) | `BitunixFuturesClient` (**escrito en este repo**) |
| Prefijo | `/api/spot/v1/...` | `/api/v1/futures/...` |

De ahí que casi todo en `account_data/bitunix.py` empiece con un
`if market == "futures"`: no es estilo, es que no hay un endpoint común al que
llamar.

`BitunixFuturesClient` implementa la firma verificada contra la cuenta real:
dos SHA-256 encadenados sobre `nonce + timestamp + api_key + payload`, y el
resultado otra vez con el secreto concatenado. En GET el payload son las claves
y valores **ordenados y concatenados sin separadores** (`marginCoinUSDT`, no
`marginCoin=USDT`); en POST es el cuerpo JSON tal cual viaja.

## Limitaciones asimétricas que hay que conocer

### Bitunix spot no puede listar las órdenes de toda la cuenta

Su endpoint `/api/spot/v1/order/pending/list` **exige** un símbolo. No existe
un "dame todas mis pendientes".

```python
broker.get_open_orders(market="spot")                    # ❌ levanta
broker.get_open_orders(market="spot", symbol="BTCUSDT")  # ✅
broker.get_open_orders(market="futures")                 # ✅ todos los pares
```

Levanta a propósito en vez de devolver `[]`: una lista vacía se leería como
"no tienes órdenes".

### El kline de spot de Bitunix ignora todo

Comprobado contra la API real:

| Endpoint | `limit` | `startTime` | `endTime` |
|---|---|---|---|
| `/api/spot/v1/market/kline` | ignorado | **ignorado** | **ignorado** |
| `/api/v1/futures/market/kline` | respeta | ignorado | **respeta** |

El de spot devuelve **siempre** las mismas ~201 velas más recientes, se le
mande lo que se le mande. Por eso `get_historical_candles` usa el kline de
**futuros** incluso pidiendo spot: es la única fuente de Bitunix con histórico
paginable.

!!! note "Consecuencia"
    El histórico que devuelve Bitunix son precios de **perpetuo**, aunque pidas
    `market="spot"`. Para el mismo par se mueven casi igual pero no son
    idénticos. Está marcado en el código (`FUTURES_KLINE_URL`) como el sitio al
    que volver si algún día su spot admite rangos.

### El saldo de futuros de Bitunix se pide de una en una

Su cuenta se consulta **por moneda de margen**. Ahí el filtro por activo no
recorta un resultado: *es* la consulta.

```python
broker.get_balance("futures")                 # {'USDT': 1500.0}  (la de por defecto)
broker.get_balance("futures", asset="USDC")   # una llamada DISTINTA
```

### El libro de futuros de Bitunix no está implementado

Levanta `NotImplementedError` a propósito: su endpoint no se ha verificado
contra la API real y devolver el de spot sería mentir sobre qué mercado se está
mirando.

## Modo cobertura (HEDGE) vs neteo (ONE_WAY)

Solo aplica a futuros, y es lo más peligroso de esta página.

| Modo | Qué hace una venta contra un largo abierto |
|---|---|
| **ONE_WAY** | Lo **reduce**. El exchange netea solo. |
| **HEDGE** | **Abre un corto independiente** que convive con el largo. |

!!! danger "Esto ya causó un incidente real"
    Una estrategia LONG con un take-profit `SELL` en una cuenta Bitunix en modo
    HEDGE: el TP **no cerró el largo, abrió un corto real**.

    ```
    entry        BUY  LIMIT 44.92    exch_id=2082852993787334656   ← largo correcto
    take_profit  SELL LIMIT 45.3692  exch_id=2082854548489949184   ← abrió un corto
    ```

    La causa: toda orden llevaba `tradeSide="OPEN"` fijo. En ONE_WAY el neteo
    del exchange lo tapaba, por eso nunca se había visto.

La defensa es `OrderRequest.reduce_only=True`, que marca la orden como
**cierre**. `BitunixFuturesClient.place_order` consulta el modo real de la
cuenta y manda `tradeSide="CLOSE"` solo cuando hace falta.

```python
salida = OrderRequest(
    symbol="LTCUSDT", side=Side.SELL, quantity=2.2,
    order_type=OrderType.MARKET, market="futures",
    reduce_only=True,          # ← sin esto, en HEDGE abres un corto
)
```

!!! warning "Binance en modo cobertura, pendiente"
    En hedge, Binance exige `positionSide` en vez de `reduceOnly`, y
    `BinanceBroker` todavía **no lee el modo de la cuenta**. Hay que resolverlo
    antes de operar hedge en Binance. Ver [Estado](../estado.md).

## Regla general

Cuando dudes, **pasa `market` explícito**. El valor por defecto es `"spot"` en
todas partes por compatibilidad con el código que existía antes de que el
parámetro existiera, no porque sea el caso más probable.
