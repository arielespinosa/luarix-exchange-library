# Integración con luarix-arbitrage-web-server

La librería está preparada para que el motor de arbitraje la use **sin cambiar
su motor**: un adaptador fino que cumple su `engine.exchanges.base.Exchange` y
delega en la librería. Esta página es la guía de la **Fase 7** del roadmap.

!!! success "Verificado el 2026-10-18: los 9 conectores de arbitraje"
    El propio motor de arbitraje (`generate_cycles`, `resolve_cycle`,
    `evaluate_cycle`) evaluó ciclos USDT→BTC→ETH→USDT con libros servidos por la
    librería en **los 9 exchanges que tiene arbitraje**: Binance, Kraken,
    Bitfinex, Bybit, OKX, Gate, HTX, KuCoin y MEXC. Latencias medianas
    ~100-170 ms (HTX ~320 ms, por su estimación de reloj). Script:
    `scripts/probar_adaptador_arbitraje.py`.

## Qué pide el contrato de arbitraje y qué lo cubre

| Contrato de arbitraje | Librería |
|---|---|
| `await fetch_rules()` | `SymbolRulesSource.get_symbol_rules()` |
| `await clock_offset_ms()` | `ServerClock.clock_offset_ms()` (Kraken/Bitfinex: estimado con el WebSocket, como en arbitraje) |
| `await stream_books(symbols, on_book, stop, latency_symbol, on_event_time)` | `OrderBookStream` + `OrderBook.event_ms` (Binance spot: `miniTicker` en la misma conexión, como en arbitraje) |
| `latency_symbol(symbols)` | método de cada exchange (mismas preferencias que en arbitraje) |
| `await fetch_commissions(symbols, credentials)` | `Fees.get_commissions()` |
| `await fetch_assets()` | `AssetInfo.list_assets()` (o deducido de las reglas) |
| `await measure_order_roundtrip_ms(credentials)` | `OrderRoundtripProbe` (Binance testnet, Bybit testnet, OKX demo) |
| `sandbox_env_prefix is not None` | `AsyncExchange.has_sandbox` |
| reconexión con espera creciente | `run_with_reconnect` (la misma idea, generalizada) |
| checksum de libro (Kraken, Bitfinex) | portado tal cual, con los mismos tests y mensajes reales |

`luarix_exchange.aio.AsyncExchange` ya da esa forma exacta (`fetch_rules`,
`clock_offset_ms`, `stream_books`, `fetch_commissions`, `fetch_assets`): el
adaptador solo traduce modelos.

### Modelos: mismos campos

| Arbitraje (`engine.models` / `engine.exchanges.base`) | Librería |
|---|---|
| `OrderBook(symbol, bids, asks, update_id, received_ms)` | igual + `market`, `raw`, `event_ms` |
| `SymbolRules(symbol, base, quote, step_size, tick_size, min_qty, min_notional, fee_on)` | igual + `market`, `raw` |
| `CommissionRates(symbol, maker, taker, discount)` | idéntico |
| `AssetListing`, `AssetNetworkInfo` | idénticos |

Los símbolos son los mismos que usa arbitraje: Kraken `BTC/USDT` (WebSocket v2),
Bitfinex `tBTCUST`, Binance/Bybit/MEXC `BTCUSDT`, OKX/KuCoin `BTC-USDT`, Gate
`BTC_USDT`, HTX `btcusdt`.

## El adaptador (copiar a `engine/exchanges/`)

```python
from luarix_exchange.aio import AsyncExchange
from luarix_exchange.factory import ExchangeFactory

from engine.exchanges.base import AssetListing, AssetNetworkInfo, CommissionRates, Exchange
from engine.models import OrderBook, SymbolRules


class LibraryExchange(Exchange):
    library_options: dict = {}

    def __init__(self, options=None, session=None):
        super().__init__(options, session)
        opciones = dict(self.library_options)
        if "depth_levels" in self.options and self.name in ("kraken", "bitfinex"):
            opciones["depth_levels"] = int(self.options["depth_levels"])
        self.lib = AsyncExchange(ExchangeFactory.create(self.name, **opciones),
                                 depth_levels=self.options.get("depth_levels"))

    async def fetch_rules(self):
        return {s: SymbolRules(symbol=r.symbol, base=r.base, quote=r.quote, step_size=r.step_size,
                               tick_size=r.tick_size, min_qty=r.min_qty, min_notional=r.min_notional,
                               fee_on=r.fee_on)
                for s, r in (await self.lib.fetch_rules()).items()}

    async def clock_offset_ms(self):
        return await self.lib.clock_offset_ms()

    async def stream_books(self, symbols, on_book, stop, latency_symbol=None, on_event_time=None):
        def traducir(b):
            return on_book(OrderBook(symbol=b.symbol, bids=b.bids, asks=b.asks,
                                     update_id=b.update_id or 0, received_ms=b.received_ms))
        await self.lib.stream_books(symbols, traducir, stop, latency_symbol=latency_symbol,
                                    on_event_time=on_event_time)

    async def _stream_session(self, *a, **kw):   # la reconexión la hace la librería
        raise NotImplementedError

    def latency_symbol(self, symbols):
        return self.lib.latency_symbol(symbols)

    async def fetch_commissions(self, symbols, credentials):
        return {s: CommissionRates(symbol=r.symbol, maker=r.maker, taker=r.taker, discount=r.discount)
                for s, r in (await self.lib.fetch_commissions(symbols, credentials)).items()}

    async def measure_order_roundtrip_ms(self, credentials, samples=8):
        return await self.lib.measure_order_roundtrip_ms(credentials, samples)

    async def fetch_assets(self):
        return {k: AssetListing(asset=a.asset, symbol=a.symbol, deposit_enabled=a.deposit_enabled,
                                withdraw_enabled=a.withdraw_enabled, name=a.name,
                                networks=tuple(AssetNetworkInfo(**vars(n)) for n in a.networks))
                for k, a in (await self.lib.fetch_assets()).items()}


class KrakenExchange(LibraryExchange):
    name, label = "kraken", "Kraken"
    fee_presets = (...)          # los de hoy: son metadatos del panel de arbitraje


class BinanceExchange(LibraryExchange):
    name, label = "binance", "Binance"
    sandbox_env_prefix = "BINANCE_TEST"
    library_options = {"testnet": False, "public_only": True}
```

Lo que **se queda en arbitraje** (no es del exchange, es de su panel/motor):
`fee_presets`, `sandbox_env_prefix`, la validación de sus `options` en la BD.

## Plan de migración recomendado

1. Instalar la librería en arbitraje (`luarix-exchange-library[kraken,bitfinex,binance,streams]`,
   fijada por commit como en los demás repos).
2. Sustituir **un exchange cada vez** por su `LibraryExchange` (empezar por
   Kraken), con sus tests de `engine/tests/` en verde y comparando contra el
   conector viejo en paralelo (mismas reglas, mismos libros).
3. Cuando los tres estén migrados, borrar sus conectores viejos.

## Pendiente antes de migrar todo

- ~~Bybit, Gate, HTX, KuCoin, MEXC y OKX~~ → **portados el 2026-10-18**: los
  9 conectores de arbitraje están en la librería.
- ~~`measure_order_roundtrip_ms`~~ → `OrderRoundtripProbe` en Binance (testnet,
  firmando con la hora de Binance), Bybit (testnet) y OKX (demo).
- **Redes de activos**: Gate, HTX y KuCoin las publican (con comisión de
  retirada en HTX y KuCoin); el resto no sin claves.
- **Reloj de HTX**: estimación ~200 ms peor que la del resto (mismo método REST
  que arbitraje, mismo problema).
- **Dependencias**: la librería requiere `pandas` (lo usa el `PaperBroker`),
  aunque importar Kraken/Bitfinex/`aio` no lo carga. Si arbitraje no lo quiere
  instalado, habría que mover `pandas` a un extra (decisión pendiente).
