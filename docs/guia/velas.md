# Velas

Todo lo de esta página es **público**: funciona aunque las credenciales estén
mal o la IP no esté en la whitelist. Úsalo para comprobar que hay conexión
antes de sospechar de las claves.

## Los tres métodos

```python
broker.get_last_candle(symbol, interval="1m", market="spot")             -> Candle | None
broker.get_candles(symbol, interval="1m", limit=100, market="spot")      -> list[Candle]
broker.get_historical_candles(symbol, interval="1m", start=None,
                              end=None, limit=None, market="spot")       -> list[Candle]
```

| | Cuándo usarlo | Coste |
|---|---|---|
| `get_last_candle` | "¿cómo cerró la última vela?" | 1 llamada |
| `get_candles` | "dame las N últimas" (indicadores en vivo) | 1 llamada |
| `get_historical_candles` | "dame de enero a marzo" (backtest) | **varias** llamadas |

---

## La última vela cerrada

```python
vela = broker.get_last_candle("BTCUSDT", "1h", market="futures")

print(f"{vela.symbol} {vela.timeframe}  {vela.open_time:%Y-%m-%d %H:%M} UTC")
print(f"  O={vela.open}  H={vela.high}  L={vela.low}  C={vela.close}")
print(f"  volumen={vela.volume}")
```

Devuelve `None` si el exchange no dio ninguna. Es un atajo de
`get_candles(..., limit=1)`.

---

## Un listado

```python
velas = broker.get_candles("BTCUSDT", "1h", limit=500, market="futures")

velas[0]     # la MÁS ANTIGUA
velas[-1]    # la MÁS RECIENTE
```

!!! important "El orden ascendente es parte del contrato"
    De la más antigua a la más nueva, **siempre**. Los indicadores y cualquier
    cálculo de estructura lo dan por hecho, y los dos exchanges devuelven sus
    klines al revés.

    Esto ya fue un bug: `get_last_candles` de Bitunix hacía `klines[:limit]`
    sobre una respuesta descendente, así que se quedaba con las `limit` **más
    viejas** del bloque y las devolvía en orden inverso. Con `limit=1`, "la
    última vela" era la vela más antigua disponible.

### Máximos por llamada

| Broker | Máximo |
|---|---|
| Binance | 1000 |
| Bitunix futuros | ~200 |
| Bitunix **spot** | ~201 **fijas**, ignora `limit` |

Para más, usa `get_historical_candles`, que pagina.

### La vela en curso se descarta

Los dos exchanges incluyen la vela **en formación**, con valores parciales. El
paquete pide una de más y la quita.

!!! danger "Por qué importa"
    Una vela parcial marcada `is_closed=True` es de las peores cosas que puede
    tragarse un indicador: su OHLC **cambia entre dos consultas del mismo
    instante**, así que los valores calculados "cambian solos" sin que nada en
    el código lo explique.

Verificado en vivo (2026-08-11 15:57 UTC, BTCUSDT 1h, `limit=5`):

```
binance spot     -> 5 velas, ultima 14:00 close=63770.03
binance futures  -> 5 velas, ultima 14:00 close=63747.0
bitunix spot     -> 5 velas, ultima 14:00 close=63770.02
bitunix futures  -> 5 velas, ultima 14:00 close=63747.0
```

Las cuatro devuelven exactamente 5 y terminan en la misma vela cerrada.

Cómo se detecta en cada uno:

- **Binance** compara `close_time` contra el reloj.
- **Bitunix** **deduce** la duración del intervalo del espaciado entre velas.
  No la saca de una tabla porque sus dos klines usan códigos distintos para el
  mismo intervalo (`"240"` vs `"4h"`) y una tabla duplicada se desincroniza.

---

## Un rango de fechas

```python
from datetime import datetime, timedelta, timezone

fin = datetime.now(timezone.utc)
inicio = fin - timedelta(days=30)

velas = broker.get_historical_candles(
    "BTCUSDT", "4h", start=inicio, end=fin, market="futures",
)
```

Sin `start`, equivale a `get_candles`. Con `start`, **pagina de verdad**: puede
costar varias llamadas y tardar.

Lo que devuelve está **siempre dentro de `[start, end]`**. Filtrar es lo que
convierte un endpoint que quizá ignore el rango en un fallo honesto y
diagnosticable ("faltan velas") en vez de un backtest corriendo sobre datos de
otra época.

### Fechas sin zona horaria

Se asumen **UTC**. Mejor pasarlas explícitas:

```python
inicio = datetime(2026, 1, 1, tzinfo=timezone.utc)
```

---

## Intervalos

Cada broker acepta los suyos. **No coinciden.**

### Binance

Los estándar: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`.

### Bitunix

| Uso | Códigos |
|---|---|
| `get_candles(market="spot")` | `1m 3m 5m 15m 30m 1h 2h 4h 6h 12h 1d 1w 1M` |
| `get_candles(market="futures")` y todo el histórico | `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M` |

Un intervalo no soportado en el camino de futuros **levanta**:

```pycon
>>> broker.get_candles("BTCUSDT", "7m", market="futures")
BitunixApiError: El kline de futuros de Bitunix no acepta el intervalo '7m'.
Disponibles: 12h, 15m, 1M, 1d, 1h, 1m, 1w, 2h, 30m, 3d, 3m, 4h, 5m, 6h, 8h.
```

!!! warning "El camino de SPOT cae a 1h en silencio"
    `BitunixMarketDataProvider.INTERVAL_MAP.get(interval, "60")`: un intervalo
    desconocido en spot se traduce a `"60"` (1 hora) **sin avisar**. Es un
    comportamiento heredado; si pides `"7m"` en spot recibes velas de 1h como
    si nada. Usa siempre un intervalo de la tabla.

---

## Spot y futuros

Son **series de precios distintas** para el mismo par. Ver
[Spot y futuros](../conceptos/mercados.md).

```python
spot = broker.get_last_candle("BTCUSDT", "1h", market="spot")
fut  = broker.get_last_candle("BTCUSDT", "1h", market="futures")

print(spot.close, fut.close, abs(spot.close - fut.close))
```

Si dan exactamente lo mismo, sospecha: significa que el selector no está
llegando al endpoint.

!!! note "El histórico de Bitunix siempre es de futuros"
    Aunque pidas `market="spot"`. Su kline de spot ignora `startTime` y
    `endTime`, así que es la única fuente paginable que tiene. Son precios de
    perpetuo: casi iguales, no idénticos.

---

## Pasar a DataFrame

```python
import pandas as pd

velas = broker.get_candles("BTCUSDT", "1h", limit=500, market="futures")
df = pd.DataFrame([{
    "date": c.open_time, "open": c.open, "high": c.high,
    "low": c.low, "close": c.close, "volume": c.volume,
} for c in velas])
```

O directamente desde el proveedor, que ya lo hace:

```python
from luarix_exchange.brokers.market_data.provider import DataFormat

df = broker.market_data_provider.get_last_candles(
    "BTCUSDT", "1h", limit=500, market="futures", format=DataFormat.OHLCV,
)
```

`DataFormat.OHLCV` devuelve un `DataFrame` con columnas
`date, open, high, low, close, volume, close_time`. `DataFormat.CANDLE`
devuelve `list[Candle]`.

!!! info "Por qué `Broker.get_candles` fija el formato"
    `get_last_candles` tiene defaults distintos según el proveedor: Binance
    devolvía un `DataFrame` y Bitunix una lista. `Broker.get_candles` pasa
    `format=DataFormat.CANDLE` siempre, para que el tipo de retorno sea
    predecible sin importar el broker.

---

## Comprobar huecos

Un hueco **en medio** invalida cualquier cálculo de indicadores. Que la serie
**termine** antes de lo pedido, en cambio, solo significa que el exchange no
tiene más — no es un error.

```python
df = pd.DataFrame([{"date": c.open_time} for c in velas])
pasos = df["date"].diff().value_counts()
print(pasos)                                  # debería haber un solo valor
print("huecos:", max(0, len(pasos) - 1))
```

Motivos legítimos por los que una serie termina antes:

- el exchange acaba de cerrar la vela y tarda en publicarla;
- su histórico no llega tan atrás para ese par;
- la descarga trajo hasta donde pudo.

---

## Último precio y libro

```python
precio = broker.get_ticker("BTCUSDT", market="futures")     # float
libro  = broker.get_order_book("BTCUSDT", limit=20, market="spot")   # dict crudo
```

El libro se devuelve **sin normalizar**: cada exchange usa su forma y quien lee
un libro suele querer los datos exactos.

!!! warning "El libro de futuros de Bitunix levanta"
    `NotImplementedError` a propósito: su endpoint no está verificado contra la
    API real y devolver el de spot sería mentir sobre qué mercado se mira.

---

## Velas en vivo

`luarix_exchange.brokers.market_data.live_feed` da una superficie única de
suscripción, independientemente de si el broker empuja por websocket o hay que
sondearlo:

```python
from luarix_exchange.brokers.market_data.live_feed import PollingMarketDataFeed

feed = PollingMarketDataFeed(broker.market_data_provider)
feed.subscribe("BTCUSDT", "1m", on_new_candle=lambda c: print(c.close))
...
feed.stop()
```

| Feed | Cuándo |
|---|---|
| `WebSocketMarketDataFeed` | Binance (push real) |
| `PollingMarketDataFeed` | Cualquier broker solo-REST. Fallback genérico |

`subscribe` llama al callback **una vez por vela recién cerrada**. Volver a
llamarlo para el mismo `(símbolo, timeframe)` **reemplaza** el callback, no
añade un segundo. `stop()` es idempotente.
