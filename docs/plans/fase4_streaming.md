# Fase 4 — Infraestructura de streaming

Creado el 2026-10-18. Rama `feat/fase4-streaming`. Roadmap: `docs/plans/roadmap.md`.
Base: las interfaces de streams de la Fase 3 (`CandleStream`,
`OrderBookStream`, `OrderUpdateStream`, asíncronas) y la decisión híbrida.

## Objetivo

Que cualquier exchange tenga streams de velas, libros y órdenes **hoy**, por
polling sobre su REST, y que en la Fase 5 enchufar un WebSocket sea
implementar una sola sesión de conexión, sin reescribir reconexión, puente
síncrono ni selección.

## Piezas (`luarix_exchange/streaming/`)

| Módulo | Qué |
|---|---|
| `reconnect.py` | `ReconnectPolicy` + `run_with_reconnect(session, stop, policy)`: reabre una sesión que cae por error transitorio con espera exponencial; un error no transitorio (un bug, un símbolo inválido) se propaga. Generaliza el *template method* de `engine/exchanges/base.py` de arbitraje. |
| `websocket.py` | `websocket_session(url, on_message, stop, subscribe=...)`: UNA conexión aiohttp (suscribe, decodifica JSON, entrega). La reconexión la pone `run_with_reconnect`. `aiohttp` es el extra `[streams]`. |
| `polling.py` | `PollingCandleStream`, `PollingOrderBookStream`, `PollingOrderUpdateStream`: implementan las interfaces sobre `MarketData`/`Account`, con `asyncio.to_thread` para no bloquear el bucle. |
| `bridge.py` | `StreamRunner`: un hilo con su event loop, para consumidores síncronos (web-server, orchestator). `runner.start(lambda stop: stream.stream_candles(...))` → `StreamHandle` con `stop()`, `exception()`. |
| `__init__.py` | `candle_stream(ex)`, `order_book_stream(ex)`, `order_update_stream(ex)`: devuelven el stream nativo si el exchange declara la capacidad, si no el de polling, si no levantan. |

## Reglas que el polling respeta

- **Velas**: solo CERRADAS, cada una una vez, en orden. La primera lectura
  fija la marca de agua y no emite historia (igual que un WebSocket).
- **Libros**: se emite cuando cambian los mejores `depth` niveles (`same_top`).
- **Órdenes**: nunca se infiere `FILLED` porque una orden desaparezca de las
  pendientes (ya causó posiciones fantasma, ver `bugs.md`): se pregunta
  `get_order()`. Si falla, se reintenta en la siguiente vuelta, no se inventa.
  Una orden que se coloca y se llena entera entre dos lecturas no se ve en
  pendientes: `track(symbol, order_id)` la sigue explícitamente.
- Errores de red: se registran CON detalle y se reintenta con espera; los
  demás se propagan.

## Fuera de esta fase

- Los WebSockets concretos de Binance y Bitunix (Fase 5).
- Reescribir `brokers/market_data/live_feed.py` (lo usan web-server y
  orchestator) sobre esta infraestructura: Fase 5, cuando haya WebSocket de
  Bitunix que justifique el cambio.

## Hallazgo previo: `PollingMarketDataFeed` iba una vela tarde — ARREGLADO

Ver `bugs.md` (2026-10-18). Afecta a las estrategias en vivo sobre polling
(Bitunix) de web-server y orchestator en cuanto suban el commit fijado de la
librería.
