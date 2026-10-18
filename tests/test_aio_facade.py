# -*- coding: utf-8 -*-
"""`AsyncExchange`: la interfaz async de luarix-arbitrage-web-server sobre
cualquier exchange de la librería (Fase 7, lado librería)."""

import asyncio
import unittest
from decimal import Decimal as D

from luarix_exchange.aio import AsyncExchange
from luarix_exchange.capabilities import Exchange, Fees, OrderBookStream, SymbolRulesSource
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.streaming._util import deliver


class _Fake(Exchange, SymbolRulesSource, OrderBookStream, Fees):
    name, label = "fake", "Fake"
    depth_levels = 10

    def __init__(self, api_key="", api_secret=""):
        self.api_key, self.api_secret = api_key, api_secret
        self.profundidad_pedida = None

    @classmethod
    def create(cls, credentials=None, **options):
        return cls(getattr(credentials, "api_key", ""), getattr(credentials, "api_secret", ""))

    def get_symbol_rules(self, market="spot"):
        return {"BTC/USDT": SymbolRules("BTC/USDT", "BTC", "USDT", D("0.0001"), D("0.1"), D("0.0001"), D("5")),
                "ETH/BTC": SymbolRules("ETH/BTC", "ETH", "BTC", D("0.001"), D("0.00001"), D("0.001"), D("0"))}

    def get_commissions(self, symbols, market="spot"):
        return {s: CommissionRates(s, D("0.001"), D("0.002") if self.api_key == "otra" else D("0.001")) for s in symbols}

    async def stream_order_books(self, symbols, depth, on_book, stop, market="spot"):
        self.profundidad_pedida = depth
        for i, s in enumerate(symbols * 2):
            await deliver(on_book, OrderBook.from_levels(s, "spot", [(100, 1)], [(101, 1)], event_ms=1000 + i))
        stop.set()


class _Credenciales:  # como el `Credentials` de arbitraje
    def __init__(self, api_key, api_secret, passphrase=None):
        self.api_key, self.api_secret, self.passphrase = api_key, api_secret, passphrase


class AsyncFacadeTests(unittest.TestCase):
    def test_reglas_comisiones_y_activos_deducidos(self):
        ex = AsyncExchange(_Fake())

        async def main():
            return await ex.fetch_rules(), await ex.fetch_commissions(["BTC/USDT"]), await ex.fetch_assets()

        reglas, tasas, activos = asyncio.run(main())
        self.assertEqual(set(reglas), {"BTC/USDT", "ETH/BTC"})
        self.assertEqual(tasas["BTC/USDT"].taker, D("0.001"))
        self.assertEqual(sorted(activos), ["BTC", "ETH", "USDT"])  # sin ASSET_INFO: de las reglas

    def test_comisiones_con_credenciales_de_arbitraje(self):
        tasas = asyncio.run(AsyncExchange(_Fake()).fetch_commissions(["BTC/USDT"], _Credenciales("otra", "s")))
        self.assertEqual(tasas["BTC/USDT"].taker, D("0.002"))

    def test_latencia_desde_event_ms_solo_del_par_elegido(self):
        fake = _Fake()
        libros, horas = [], []

        async def main():
            await AsyncExchange(fake).stream_books(["BTC/USDT", "ETH/BTC"], libros.append, asyncio.Event(),
                                                   latency_symbol="ETH/BTC", on_event_time=horas.append)

        asyncio.run(main())
        self.assertEqual(len(libros), 4)
        self.assertEqual(horas, [1001, 1003])
        self.assertEqual(fake.profundidad_pedida, 10)  # la del exchange

    def test_sin_reloj_el_desfase_es_cero_y_sin_fees_levanta(self):
        class SinNada(Exchange, SymbolRulesSource):
            name = "x"

            def get_symbol_rules(self, market="spot"):
                return {}

        ex = AsyncExchange(SinNada())
        self.assertEqual(asyncio.run(ex.clock_offset_ms()), 0)
        with self.assertRaises(NotImplementedError):
            asyncio.run(ex.fetch_commissions(["A"]))

    def test_binance_pasa_la_latencia_a_su_propio_flujo(self):
        import inspect

        from luarix_exchange.brokers.binance import BinanceBroker

        self.assertIn("on_event_time", inspect.signature(BinanceBroker.stream_order_books).parameters)


if __name__ == "__main__":
    unittest.main()
