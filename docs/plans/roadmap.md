# Roadmap: una sola librería de exchanges para todo Luarix

Creado el 2026-10-14. Estado vivo: marcar cada paso al cerrarlo, con fecha.

## Objetivo

Centralizar en `luarix-exchange-library` **todo** lo que habla con un exchange,
para que lo usen como librería:

- `luarix-trading-framework` → y a través de él `luarix-orchestator` y
  `luarix-web-server` (estrategias sobre velas, órdenes con TP/SL, backtest con
  `PaperBroker`).
- `luarix-arbitrage-web-server` (arbitraje triangular y entre exchanges, sobre
  libros de órdenes).

Requisitos fijados por el usuario:

1. Factory + clases abstractas + polimorfismo: el consumidor pide un exchange
   por nombre y trabaja contra la interfaz, nunca contra la clase concreta.
2. `PaperBroker` vive aquí, como **un broker más** (el simulado).
3. Cubrir los exchanges principales con sus **APIs reales**: Binance y Bitunix
   (ya hechos), y Kraken y Bitfinex (hoy solo existen, parciales, en arbitraje).
4. **WebSocket como opción 1** para velas nuevas, llenado/estado de órdenes y
   libros. **Polling solo como respaldo** cuando el exchange no ofrece canal
   (o mientras no esté implementado), y siempre detrás de la misma interfaz.

## Punto de partida (2026-10-14)

Lo que hay en cada lado y por qué no encaja tal cual:

| | luarix-trading-framework (origen de este repo) | luarix-arbitrage-web-server (`engine/exchanges/`) |
|---|---|---|
| Modelo de ejecución | Síncrono (`python-binance`, SDK `bitunix`, `requests`) | Asíncrono (`aiohttp`, websockets) |
| Números | `float` | `Decimal` |
| Qué necesita | Velas, órdenes, posiciones, futuros, TP/SL | Libros en streaming, reglas por par (step/tick/minNotional), comisión de la cuenta, desfase de reloj, latencia de orden en testnet |
| Exchanges | Binance, Bitunix, MT5 (+ Yahoo datos) | Binance, Bitfinex, Kraken (solo spot, solo datos públicos + comisiones) |
| Interfaz | `Broker` — una sola ABC grande, con OCO y futuros abstractos | `Exchange` — ABC pequeña, con reconexión como *template method* |

Consecuencia de diseño: **no** se puede meter Kraken/Bitfinex en `Broker` tal
como está (tendrían que implementar OCO, `update_oco`, apalancamiento... con
stubs). El contrato tiene que partirse por capacidades (Fase 3).

---

## Fase 0 — Repo y estructura ✅ 2026-10-14

- Repo clonado en `D:\Trabajo\luarix-exchange-library` (estaba vacío).
- Paquete `luarix_exchange` (distribución `luarix-exchange-library`), extras
  por broker idénticos a los del framework.

## Fase 1 — Sacar los brokers de luarix-trading-framework ✅ 2026-10-14

Movimiento **sin cambios de comportamiento**, solo rutas de import.

- [x] `luarix_trading/brokers/**` → `luarix_exchange/brokers/**` (incluye
      `PaperBroker`, MT5 y Yahoo).
- [x] `core/models/{orders,brokers}.py` → `luarix_exchange/models/`.
      `Candle` → `models/market.py`. `Side` → `models/orders.py`.
- [x] Se quedan en el framework (son estrategia, no exchange): `core.stats`,
      `core.sizing`, `Signal`, `RiskResult`, `Portfolio`, `Pipeline*`,
      `Trend`, `Fractal`, `MarketSnapshot`, `Market`.
- [x] Tests: 148 aquí; 1 (`BacktestStats`) se quedó allá. Los 149 tests
      originales, ejecutados por las rutas VIEJAS contra los alias, pasan.
- [x] Docs MkDocs y cuadernos movidos; guía de migración reescrita.
- [x] El framework depende de esta librería y conserva
      `luarix_trading.brokers.*` y `core.models.{orders,brokers}` como alias
      (`luarix_trading/_alias.py`): mismo objeto módulo, no copia.
- [x] Instalada en editable en los venv de `luarix-trading-framework` y de
      `luarix-orchestator`. Suite de orchestator: 1026 ok, 1 fallo
      preexistente y ajeno (`_advance_broker_clock` no existe en
      `rpc/manual_execution_servicer.py`).

**Pendiente del usuario:** commit + push de los dos repos. Hasta el push, un
`pip install` limpio del framework falla, porque su dependencia apunta a
`git+https://github.com/arielespinosa/luarix-exchange-library.git`.

## Fase 2 — Consumidores importan `luarix_exchange` directamente

- [ ] `luarix-orchestator`: cambiar sus imports de `luarix_trading.brokers.*`,
      `core.models.{orders,signals.Side,market.Candle}` a `luarix_exchange.*`
      (~40 sitios, ver `grep -rn "luarix_trading" workflow rpc tests`).
      Añadir `luarix-exchange-library` a su extra `adapters`.
- [ ] `luarix-web-server`: igual (~30 ficheros). Ojo: su `requirements.txt`
      fija el framework a un commit (`@1ff977b`), así que hoy NO le afecta
      nada; al subir el pin, añadir también la librería.
      ⚠ Reemplazar la **ruta completa**, nunca la subcadena: ya hubo un
      find-and-replace que convirtió `append` en `luarix_trading_frameworkend`.
- [ ] Cuando ninguno importe ya `luarix_trading.brokers`: borrar los alias del
      framework y `tests/test_exchange_aliases.py`.

Se puede hacer en paralelo con las fases siguientes; no las bloquea.

## Fase 3 — Contrato por capacidades

Partir `Broker` en interfaces pequeñas (ABC, para que `isinstance` funcione y
la factory pueda decir qué sabe hacer cada exchange):

| Capacidad | Métodos | Quién la necesita |
|---|---|---|
| `MarketData` | `get_candles`, `get_historical_candles`, `get_ticker`, `get_order_book` | todos |
| `SymbolRules` | `get_symbol_rules` → step, tick, min_qty, min_notional | arbitraje, y Binance spot ya lo usa por dentro |
| `Account` | `get_balance`, `get_positions`, `get_open_orders`, `get_order` | trading |
| `Trading` | `place_order`, `cancel_order`, `update_order` | trading |
| `Derivatives` | `set_leverage`, modo de posición | trading en futuros |
| `NativeOco` | `place_oco_order`, `cancel_oco`, `update_oco` | Binance spot, MT5, Paper |
| `Fees` | `get_commissions` | arbitraje |
| `CandleStream` / `OrderBookStream` / `OrderUpdateStream` | ver Fase 4 | todos |

- `Broker` se mantiene como la unión de las capacidades de trading, para no
  romper a nadie. Los flags actuales (`supports_oco`, `supports_futures`,
  `supports_independent_orders`) pasan a derivarse de las capacidades.
- La factory crece: `ExchangeFactory.create(name, credentials, market=...)` y
  `ExchangeFactory.capabilities(name)`. `BrokerFactory.get_broker` sigue
  funcionando igual.
- Modelos nuevos en `Decimal` (lo que arbitraje necesita y ya usa):
  `OrderBook`, `SymbolRules`, `CommissionRates`, `OrderUpdate`. Los modelos
  actuales (`float`) no se tocan en esta fase: cambiarles el tipo rompería a
  orchestator y web-server. Decisión a revisar al final.

**Decisión abierta D1 — síncrono vs asíncrono.** Propuesta: los **streams** son
`asyncio` (`aiohttp`, que arbitraje ya usa para todo), con un puente que los
corre en un hilo para los consumidores síncronos (orchestator, web-server). El
REST existente sigue síncrono. El REST nuevo (reglas, comisiones, Kraken,
Bitfinex) se escribe async con una fachada síncrona generada. Confirmar con el
usuario antes de empezar la Fase 4.

## Fase 4 — Infraestructura de streaming

Una sola implementación de reconexión para todos: se generaliza el
`Exchange.stream_books` de arbitraje (*template method*: el bucle común
reconecta con espera exponencial y cada exchange implementa solo una sesión).

- `CandleStream.subscribe(symbol, timeframe, on_candle)` — entrega **solo velas
  cerradas** (`is_closed=True`), igual que hoy `get_candles` descarta la vela
  en curso. Ya pasó una vez que una vela parcial se tomara por cerrada: no
  repetirlo.
- `OrderUpdateStream.subscribe(on_update)` — un `OrderUpdate` normalizado por
  cada cambio de estado o llenado (parcial o total), con `.raw`.
- `OrderBookStream` — lo que arbitraje ya tiene.
- **Respaldo por polling**: `PollingCandleStream` (ya existe:
  `PollingMarketDataFeed`), `PollingOrderUpdateStream` (compara
  `get_open_orders` + `get_order` entre dos lecturas), `PollingOrderBookStream`.
  Se construyen **sobre las capacidades REST**, así que cualquier exchange que
  tenga REST tiene streaming gratis.
- La factory elige: WebSocket si el exchange lo declara; polling si no. El
  consumidor no se entera.
- `PaperBroker` emite los mismos `OrderUpdate` (ya genera execution reports con
  forma de Binance: mapearlos).

## Fase 5 — Binance y Bitunix por WebSocket

| | Velas | Libro | Órdenes/fills (privado) |
|---|---|---|---|
| Binance spot | `<sym>@kline_<tf>` | `<sym>@depth<N>` (ya en arbitraje) | user data stream ⚠ |
| Binance USDT-M | `fstream`: `<sym>@kline_<tf>` | `fstream`: `<sym>@depth<N>` | user data stream, evento `ORDER_TRADE_UPDATE` |
| Bitunix futuros | WS público ⚠ | WS público ⚠ | WS privado, canal de órdenes ⚠ |
| Bitunix spot | ⚠ por confirmar si existe | ⚠ | ⚠ → si no hay, polling |

⚠ = **sin verificar**; sale de la documentación de cada exchange o de memoria,
no de una respuesta real. Antes de implementar cada celda, confirmarla contra
la documentación actual y una conexión real.

- Binance spot: comprobar si el *listenKey* sigue vigente o si ya hay que usar
  `userDataStream.subscribe` de la WebSocket API (Binance lo estaba
  cambiando).
- Bitunix: `bugs.md` dice "Bitunix no tiene stream de usuario"; eso se
  escribió mirando el SDK de PyPI (solo spot, solo REST). La API de futuros
  documenta un WS privado: **verificarlo** es la primera tarea de esta fase,
  porque quitaría el polling de `order_tracker` en web-server.
- El `start_user_socket` comentado de `BinanceBroker` y el
  `WebSocketMarketDataFeed` (sobre `ThreadedWebsocketManager` de
  python-binance) se sustituyen por la infraestructura de la Fase 4.

## Fase 6 — Kraken y Bitfinex completos

Punto de partida: `engine/exchanges/kraken.py` y `bitfinex.py` de arbitraje
(reglas de pares, libros por WS, comisiones). Se mueven aquí y se completan.

| | Kraken spot | Bitfinex |
|---|---|---|
| Velas REST | `/0/public/OHLC` (máx. 720 velas por llamada) ⚠ | `/v2/candles/trade:<tf>:<sym>/hist` ⚠ |
| Velas WS | WS v2, canal `ohlc` ⚠ | WS v2, canal `candles` ⚠ |
| Libro WS | ya en arbitraje | ya en arbitraje |
| Cuenta / órdenes REST | `/0/private/Balance`, `AddOrder`, `CancelOrder`, `EditOrder` ⚠ | `/v2/auth/r/wallets`, `/v2/auth/w/order/submit`, `.../cancel`, `.../update` ⚠ |
| Órdenes/fills WS | WS v2 autenticado, canal `executions` (token vía `GetWebSocketsToken`) ⚠ | WS autenticado: eventos `on`/`ou`/`oc` y `te`/`tu` ⚠ |
| Derivados | Kraken Futures es OTRA API (`futures.kraken.com`) → fuera de esta fase | perpetuos (`tBTCF0:USTF0`) en la misma API ⚠ |

Orden: (1) datos públicos + reglas + velas, (2) cuenta y órdenes spot,
(3) streams privados, (4) derivados. Cada paso se marca en `docs/estado.md`
como verificado contra la API real o no, igual que se hizo con Binance y
Bitunix.

## Fase 7 — luarix-arbitrage-web-server usa la librería

- Sustituir `engine/exchanges/*` por la librería. Primero con un adaptador
  fino que implemente su `Exchange` actual sobre las capacidades nuevas (cero
  cambios en `engine/`), y después quitar el adaptador.
- Sus tests de `tests/engine/test_{exchanges,kraken,bitfinex}.py` se vienen
  aquí como tests de contrato.

## Fase 8 — Más exchanges (por decidir)

Candidatos por volumen: OKX, Bybit, KuCoin (estuvo en el enum de la factory
sin implementación; se quitó). Decidir con el usuario cuáles y en qué orden.
Con las Fases 3 y 4 hechas, añadir uno es implementar las capacidades que
soporte; los streams por polling salen solos.

---

## Reglas para todo el roadmap

- Lo que un exchange no sabe hacer **levanta**; nunca devuelve vacío (una
  lista vacía se lee como "no tienes posiciones").
- Nada se da por bueno contra la API real sin haberlo ejercitado: cada
  endpoint nuevo entra en `docs/estado.md` como verificado o no.
- Bitunix no tiene testnet. Binance sí, con claves distintas. Kraken y
  Bitfinex: comprobar antes de probar órdenes.
- Logs con datos, nunca "ok/colocada" (regla permanente, ver `bugs.md`).
