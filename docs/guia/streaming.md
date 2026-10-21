# Streaming: velas, libros y órdenes en vivo

Regla del proyecto: **WebSocket primero, polling de respaldo**. No hace falta
saber cuál le toca a cada exchange: la selección es automática.

```python
from luarix_exchange.streaming import candle_stream, order_book_stream, order_update_stream

candle_stream(broker)        # el nativo si el exchange declara CANDLE_STREAM; si no, polling sobre su REST
order_book_stream(broker)    # ídem con ORDER_BOOK_STREAM / MARKET_DATA
order_update_stream(broker, symbols=["BTCUSDT"])   # ídem con ORDER_UPDATE_STREAM / ACCOUNT
```

Qué va por WebSocket desde la Fase 5 (`market=` decide):

| | velas | libro | órdenes |
|---|---|---|---|
| Binance spot | WebSocket | WebSocket (≤20 niveles) | WebSocket API firmada |
| Binance futuros | WebSocket (`/market/`) | WebSocket (`/public/`, ≤20) | `listenKey` (`/private/`) |
| Bitunix futuros | WebSocket como disparador + REST | WebSocket (≤15 niveles) | canal privado `order` |
| Bitunix spot | polling | polling | polling (necesita `symbols=`) |

Más niveles de libro de los que da el stream (20 Binance, 15 Bitunix) → polling.

```python
candle_stream(broker, market="futures")     # pasa market para elegir bien en Bitunix
```

### Probarlo contra un exchange real

```bash
python scripts/probar_streams.py --exchange bitunix --market futures
python scripts/probar_streams.py --exchange bitunix --market futures --privado --env ruta/.env
python scripts/probar_streams.py --exchange binance --market spot --privado --testnet
```

No coloca órdenes: con `--privado`, coloca o cancela una a mano en la web del
exchange y mírala llegar.

### Para web-server y orchestator: `StreamingMarketDataFeed`

Misma interfaz que `PollingMarketDataFeed` / `WebSocketMarketDataFeed`
(`subscribe`/`unsubscribe`/`stop`), pero WebSocket donde lo hay:

```python
from luarix_exchange.brokers.market_data.live_feed import StreamingMarketDataFeed
feed = StreamingMarketDataFeed(broker, market="futures")
feed.subscribe("BTCUSDT", "1h", on_new_candle)
```

## Desde código asíncrono (arbitraje)

Los streams son `async`. Corren hasta que se activa `stop`:

```python
import asyncio
from luarix_exchange.capabilities import CandleSubscription

async def main():
    stop = asyncio.Event()
    async def on_candle(vela):           # puede ser síncrono o asíncrono
        print(vela.symbol, vela.open_time, vela.close)
    await candle_stream(broker).stream_candles(
        [CandleSubscription("BTCUSDT", "1m", market="futures")], on_candle, stop)
```

## Desde código síncrono (web-server, orchestator)

`StreamRunner` corre los streams en un hilo con su propio event loop:

```python
from luarix_exchange.streaming import StreamRunner

runner = StreamRunner()
handle = runner.start(
    lambda stop: candle_stream(broker).stream_candles([CandleSubscription("BTCUSDT", "1m")], on_candle, stop),
    label="velas BTCUSDT",
)
...
handle.stop()            # para ese stream
handle.exception()       # el error con el que terminó, si terminó mal
runner.close()           # para todos
```

!!! warning "Los callbacks corren en el hilo del runner"
    Tienen que ser rápidos: uno que bloquee frena a todos los streams de ese
    runner. El trabajo pesado (base de datos, ejecutar un grafo), a otra cola.

## Qué garantiza cada stream

| Stream | Garantía |
|---|---|
| Velas | Solo **cerradas**, cada una una vez, en orden. No emite historia al arrancar. |
| Libros | Un `OrderBook` cuando cambian los mejores `depth` niveles. |
| Órdenes | Un `OrderUpdate` por cambio de estado o de cantidad ejecutada. Una orden que desaparece de las pendientes **no** se da por llenada: se pregunta `get_order()`. |

### Limitaciones del polling (las resuelve el WebSocket)

- **Latencia**: hasta `poll_s` segundos (por defecto 10 s velas, 1 s libros,
  5 s órdenes).
- **Órdenes rápidas**: una orden que se coloca y se llena entera entre dos
  lecturas no aparece en pendientes. Síguela explícitamente:
  `stream.track(symbol, order_id)`.
- **Precio de cada llenado**: por polling solo se conoce el precio medio
  (`OrderUpdate.average_price`); `last_fill_price` queda en `None`.
- **Bitunix spot** exige símbolo para listar pendientes: pasa `symbols=[...]`.

## Reconexión

Todos los streams se reconectan solos ante errores de red, con espera
creciente (`ReconnectPolicy`: 1 s, doblando hasta 60 s). Un error que no es
de red (un símbolo inválido, un cambio de formato) **termina** el stream y
queda en `handle.exception()`: reintentarlo solo lo escondería.

## WebSocket (para implementar exchanges)

`websocket_session(url, on_message, stop, subscribe=[...])` abre UNA conexión
(aiohttp, extra `[streams]`), suscribe, decodifica JSON y entrega cada mensaje.
Combinada con `run_with_reconnect`, es todo lo que un stream nativo necesita:

```python
async def stream_candles(self, subscriptions, on_candle, stop):
    async def sesion():
        await websocket_session(URL, lambda msg: self._on_kline(msg, on_candle), stop,
                                subscribe=[{"method": "SUBSCRIBE", "params": [...]}])
    await run_with_reconnect(sesion, stop, label="binance klines")
```
