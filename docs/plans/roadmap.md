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
→ **Hecho 2026-10-14**: los dos repos subidos. Comprobado en venvs limpios:
la librería se instala desde GitHub (163 tests en verde, también con pandas
3.0.6) y el framework desde GitHub se trae la librería solo.

## Fase 2 — Consumidores importan `luarix_exchange` directamente

- [x] `luarix-orchestator` (2026-10-14, rama `feat/luarix-exchange-library`,
      sin commitear): 21 ficheros, imports por ruta completa, extra
      `adapters` con la librería, README. Solo sigue usando del framework
      `luarix_trading.core.stats`. Verificado con un plugin de pytest que
      BLOQUEA las rutas viejas (los alias taparían un olvido): 1026 passed,
      mismo fallo preexistente ajeno. Detalle en su `docs/plans/bugs.md`.
      Rama subida por el usuario; se mezcla a `develop` cuando lo coordine.
- [x] `luarix-web-server` (2026-10-14, rama `feat/luarix-exchange-library`
      desde `saas`, sin commitear): 66 imports en 30 ficheros,
      `requirements.txt` con la librería fijada, verificación de
      `deploy-linux.sh`. Del framework solo usa `core.stats` y `core.sizing`.
      Verificado con las rutas viejas bloqueadas: mismos resultados que antes
      del cambio. Detalle en su `docs/plans/bugs.md`.
- [x] Framework: `pyproject.toml` fija la librería a `e6888ca` (commit
      `62f12db` del framework, 2026-10-14). `requirements.txt` de web-server
      Y de orchestator fijan librería `e6888ca` + framework `62f12db`;
      instalados los dos en venvs limpios desde GitHub, sin conflicto.
      READMEs de los cuatro repos explican la instalación (git y editable).
- [ ] luarix-arbitrage-web-server: NO se añade todavía a su `requirements`
      (no usa la librería hasta la Fase 7; arrastraría pandas/numpy a su
      despliegue sin motivo). Se añade al empezar la Fase 7.
- [ ] Mezclar orchestator → `develop` y web-server → `saas` a la vez
      (planificado por el usuario para el 2026-10-15), avisando al equipo de
      que instale la librería.
- [ ] Alias del framework: NO borrar hasta que todo el equipo haya
      instalado la librería (decisión del usuario, 2026-10-14).
- [ ] Cuando ninguno importe ya `luarix_trading.brokers`: borrar los alias del
      framework y `tests/test_exchange_aliases.py`.

**Regla de versiones (aprendida por las malas con pip):** la librería va
fijada por commit con la MISMA URL exacta en el `pyproject.toml` del framework
y en el `requirements.txt` de web-server. Si difieren en algo (aunque sea el
`@sha`), pip aborta con `ResolutionImpossible`. Subir la librería = cambiar el
sha en los dos repos a la vez.

Se puede hacer en paralelo con las fases siguientes; no las bloquea.

### Pendientes ajenos detectados durante la Fase 2 (no son de esta librería)

- ~~orchestator no soporta `PositionExit`~~ → **resuelto 2026-10-16**: Ramon
  mezcló `TrailingStopAction` a `develop` (desde
  `feat/tradingview-strategy-templates-batch-a`). Esa rama traía un import
  viejo (`luarix_trading.brokers.paper`), corregido en la rama
  `fix/position-exit-luarix-exchange` de orchestator. Con ella, los tests de
  paridad de trailing de web-server (`TechnicalRatingsParityTests`,
  `test_paper_runner_tv_broker_modes`) pasan.
- orchestator: `test_broker_state_snapshot_reflects_filled_position_and_pending_limit_order`
  importa `_advance_broker_clock`, que no existe.
- En Windows, la suite de web-server agota los puertos efímeros hacia Redis
  (`Errno 10048`); los tests sueltos pasan.

## Fase 3 — Contrato por capacidades

**Diseño detallado: `docs/plans/fase3_contrato_capacidades.md`.**
**Implementada el 2026-10-16/17** en la rama `feat/fase3-capacidades` (sin
commitear): capacidades, `Exchange`, `Broker` como unión, modelos `Decimal`
(`OrderBook`, `SymbolRules`, `CommissionRates`, `OrderUpdate`), `get_order_book`
normalizado, `list_symbols`, `get_symbol_rules` en Binance y Bitunix,
`ExchangeFactory`. Verificación en `docs/plans/bugs.md` (entrada del
2026-10-16). Los exchanges que no sean un `Broker` completo (Kraken, Bitfinex)
irán en `luarix_exchange/exchanges/` (ver `docs/guia/nuevo-exchange.md`).

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

**D1 — síncrono vs asíncrono: DECIDIDO HÍBRIDO (usuario, 2026-10-16).**
REST **síncrono** para todo, también el nuevo (reglas, comisiones, Kraken,
Bitfinex): es lo que usan web-server y orchestator sin cambios, y arbitraje
llama sus pocas consultas REST con `asyncio.to_thread`. **Streams
asíncronos** (`asyncio` + `aiohttp`) con un puente a callbacks en hilo para
los consumidores síncronos. Se descartó todo-async (reescribir web-server y
orchestator) y todo-sync (reescribir el motor de arbitraje).

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
