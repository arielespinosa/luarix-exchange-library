"""Prueba del adaptador de la Fase 7: el motor de luarix-arbitrage-web-server
(`generate_cycles`, `resolve_cycle`, `evaluate_cycle`) sobre libros servidos por
luarix-exchange-library, para Kraken, Bitfinex y Binance. NO toca el repo de
arbitraje: importa su código por ruta. Solo datos públicos, no opera.

    python scripts/probar_adaptador_arbitraje.py ../luarix-arbitrage-web-server .

`LibraryExchange` (abajo) es el adaptador de referencia que copiar a
`engine/exchanges/` de arbitraje en la Fase 7 (ver `docs/integracion/arbitraje.md`).
Verificado el 2026-10-18: ciclos USDT/BTC/ETH evaluados en los tres exchanges,
latencias medianas 109-134 ms.
"""

import asyncio
import sys
import time
from decimal import Decimal

sys.path[:0] = [sys.argv[1], sys.argv[2]]

# Solo para poder importar los conectores de arbitraje en este entorno (sin orjson).
import json, types  # noqa: E401,E402
if "orjson" not in sys.modules:
    try:
        import orjson  # noqa: F401
    except ImportError:
        sys.modules["orjson"] = types.SimpleNamespace(loads=json.loads, dumps=lambda o, **k: json.dumps(o).encode())

from engine.exchanges.base import AssetListing as EAsset  # noqa: E402
from engine.exchanges.base import CommissionRates as ERates  # noqa: E402
from engine.exchanges.base import Exchange  # noqa: E402
from engine.models import OrderBook as EBook  # noqa: E402
from engine.models import SymbolRules as ERules  # noqa: E402
from engine.triangular import evaluate_cycle, generate_cycles, resolve_cycle  # noqa: E402

from luarix_exchange.aio import AsyncExchange  # noqa: E402
from luarix_exchange.factory import ExchangeFactory  # noqa: E402


class LibraryExchange(Exchange):
    """Contrato de arbitraje sobre la librería. Solo traduce modelos: la lógica
    (WebSocket, checksum, reconexión, reloj) es la de la librería."""

    #: opciones para `ExchangeFactory.create` además de las de la BD
    library_options: dict = {}

    def __init__(self, options=None, session=None):
        super().__init__(options, session)
        opciones = dict(self.library_options)
        if "depth_levels" in self.options and self.name in ("kraken", "bitfinex"):
            opciones["depth_levels"] = int(self.options["depth_levels"])
        self.lib = AsyncExchange(ExchangeFactory.create(self.name, **opciones),
                                 depth_levels=self.options.get("depth_levels"))

    async def fetch_rules(self):
        return {s: ERules(symbol=r.symbol, base=r.base, quote=r.quote, step_size=r.step_size, tick_size=r.tick_size,
                          min_qty=r.min_qty, min_notional=r.min_notional, fee_on=r.fee_on)
                for s, r in (await self.lib.fetch_rules()).items()}

    async def clock_offset_ms(self):
        return await self.lib.clock_offset_ms()

    async def stream_books(self, symbols, on_book, stop, latency_symbol=None, on_event_time=None):
        def traducir(b):
            return on_book(EBook(symbol=b.symbol, bids=b.bids, asks=b.asks, update_id=b.update_id or 0,
                                 received_ms=b.received_ms))
        await self.lib.stream_books(symbols, traducir, stop, latency_symbol=latency_symbol, on_event_time=on_event_time)

    async def _stream_session(self, *a, **kw):  # la reconexión la hace la librería
        raise NotImplementedError

    def latency_symbol(self, symbols):
        return self.lib.latency_symbol(symbols)

    async def fetch_commissions(self, symbols, credentials):
        return {s: ERates(symbol=r.symbol, maker=r.maker, taker=r.taker, discount=r.discount)
                for s, r in (await self.lib.fetch_commissions(symbols, credentials)).items()}

    async def measure_order_roundtrip_ms(self, credentials, samples=8):
        return await self.lib.measure_order_roundtrip_ms(credentials, samples)

    async def fetch_assets(self):
        return {k: EAsset(asset=a.asset, symbol=a.symbol, deposit_enabled=a.deposit_enabled,
                          withdraw_enabled=a.withdraw_enabled, name=a.name)
                for k, a in (await self.lib.fetch_assets()).items()}


class KrakenLib(LibraryExchange):
    name, label = "kraken", "Kraken"


class BitfinexLib(LibraryExchange):
    name, label = "bitfinex", "Bitfinex"


class BinanceLib(LibraryExchange):
    name, label = "binance", "Binance"
    sandbox_env_prefix = "BINANCE_TEST"
    library_options = {"testnet": False, "public_only": True}


def _clase(nombre, etiqueta):
    return type(f"{etiqueta}Lib", (LibraryExchange,), {"name": nombre, "label": etiqueta})


# Los seis portados (2026-10-18): mismos nombres y símbolos que en arbitraje.
BybitLib, OkxLib, GateLib = _clase("bybit", "Bybit"), _clase("okx", "OKX"), _clase("gate", "Gate")
HtxLib, KucoinLib, MexcLib = _clase("htx", "HTX"), _clase("kucoin", "KuCoin"), _clase("mexc", "MEXC")


async def probar(cls, start, via):
    profundidad = {KrakenLib: 10, BitfinexLib: 25, BinanceLib: 10}.get(cls)
    ex = cls({"depth_levels": profundidad} if profundidad else {})
    rules = await ex.fetch_rules()
    offset = await ex.clock_offset_ms()
    ciclos = generate_cycles([start], via, rules)
    specs = [resolve_cycle(c, rules) for c in ciclos]
    simbolos = sorted({s for spec in specs for s in spec.symbols})
    libros, latencias, stop = {}, [], asyncio.Event()
    tarea = asyncio.ensure_future(ex.stream_books(
        simbolos, lambda b: libros.__setitem__(b.symbol, b), stop, latency_symbol=ex.latency_symbol(simbolos),
        on_event_time=lambda ev: latencias.append(int(time.time() * 1000) + offset - ev)))
    await asyncio.sleep(8)
    ahora = int(time.time() * 1000)
    resultados = [evaluate_cycle(spec, libros, rules, Decimal("1000"), Decimal("0.001"), ahora) for spec in specs]
    stop.set()
    await asyncio.wait_for(tarea, 10)
    print(f"\n{ex.label}: {len(rules)} reglas, reloj {offset:+d} ms, {len(ciclos)} ciclos sobre {simbolos}, "
          f"latencia mediana {sorted(latencias)[len(latencias) // 2] if latencias else '-'} ms")
    for r in resultados:
        print(f"  {' -> '.join(r.spec.assets)}: neto {r.net_pct:+.4f}% bruto {r.gross_pct:+.4f}% "
              f"ejecutable={r.executable} {r.reason or ''}")


async def main():
    for cls in (KrakenLib, BitfinexLib, BinanceLib, BybitLib, OkxLib, GateLib, HtxLib, KucoinLib, MexcLib):
        await probar(cls, "USDT", ["BTC", "ETH"])


asyncio.run(main())
