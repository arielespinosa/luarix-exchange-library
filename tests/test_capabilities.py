# -*- coding: utf-8 -*-
"""Contrato por capacidades (Fase 3): declaración por herencia, consulta sin
instanciar, matriz de los brokers actuales y `ExchangeFactory`."""

import asyncio
import subprocess
import sys
import unittest
import warnings
from decimal import Decimal as D

from luarix_exchange.capabilities import (
    CAPABILITY_ABCS,
    Capability,
    CandleStream,
    Exchange,
    MarketData,
    SymbolRulesSource,
)
from luarix_exchange.factory import ExchangeFactory
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.rules import SymbolRules


class _SpotDataExchange(Exchange, MarketData, SymbolRulesSource):
    """La forma que tendrá el primer paso de Kraken: solo spot, solo datos
    públicos + reglas. Si escribirlo es cómodo, el diseño vale."""

    name = "fake-spot"
    label = "Fake Spot"

    def get_candles(self, symbol, interval="1m", limit=100, market="spot"):
        return []

    def get_historical_candles(self, symbol, interval="1m", start=None, end=None, limit=None, market="spot"):
        return []

    def get_ticker(self, symbol, market="spot"):
        return 1.0

    def get_order_book(self, symbol, limit=20, market="spot"):
        return OrderBook.from_levels(symbol, market, [(1, 1)], [(2, 1)])

    def list_symbols(self, market="spot"):
        return ["XBTUSD"]

    def get_symbol_rules(self, market="spot"):
        return {"XBTUSD": SymbolRules("XBTUSD", "XBT", "USD", D("0.0001"), D("0.1"), D("0.0001"), D("5"))}


class CapabilityDeclarationTests(unittest.TestCase):
    def test_cada_capacidad_tiene_una_abc_registrada(self):
        self.assertEqual(set(CAPABILITY_ABCS), set(Capability))

    def test_las_capacidades_salen_de_la_herencia_sin_instanciar(self):
        self.assertEqual(_SpotDataExchange.capabilities(), {Capability.MARKET_DATA, Capability.SYMBOL_RULES})
        self.assertTrue(_SpotDataExchange.supports(Capability.MARKET_DATA))
        self.assertFalse(_SpotDataExchange.supports(Capability.TRADING))

    def test_el_mercado_tambien_cuenta(self):
        self.assertTrue(_SpotDataExchange.supports(Capability.MARKET_DATA, market="spot"))
        self.assertFalse(_SpotDataExchange.supports(Capability.MARKET_DATA, market=Market.FUTURES))

    def test_unsupported_quita_lo_heredado(self):
        class SinReglas(_SpotDataExchange):
            unsupported = frozenset({Capability.SYMBOL_RULES})

        self.assertEqual(SinReglas.capabilities(), {Capability.MARKET_DATA})

    def test_require_explica_lo_que_falta(self):
        ex = _SpotDataExchange()
        ex.require(Capability.MARKET_DATA)
        with self.assertRaisesRegex(NotImplementedError, "Fake Spot no soporta trading"):
            ex.require(Capability.TRADING)
        with self.assertRaisesRegex(NotImplementedError, "no tiene mercado futures"):
            ex.require(Capability.MARKET_DATA, market="futures")

    def test_get_symbol_rule_levanta_si_el_par_no_existe(self):
        ex = _SpotDataExchange()
        self.assertEqual(ex.get_symbol_rule("XBTUSD").tick_size, D("0.1"))
        with self.assertRaisesRegex(ValueError, "no existe"):
            ex.get_symbol_rule("NOPE")

    def test_una_capacidad_a_medias_no_se_puede_instanciar(self):
        class StreamSinImplementar(Exchange, CandleStream):
            pass

        with self.assertRaises(TypeError):
            StreamSinImplementar()

    def test_las_capacidades_de_stream_son_asincronas(self):
        self.assertTrue(asyncio.iscoroutinefunction(CandleStream.stream_candles))

    def test_no_se_puede_registrar_dos_veces_la_misma_capacidad(self):
        with self.assertRaises(TypeError):
            class OtraMarketData(MarketData):  # noqa: F841
                capability = Capability.MARKET_DATA


class BrokerMatrixTests(unittest.TestCase):
    """La matriz de `docs/referencia/capacidades.md`, fijada."""

    TRADING = {Capability.ACCOUNT, Capability.TRADING}
    STREAMS = {Capability.CANDLE_STREAM, Capability.ORDER_BOOK_STREAM, Capability.ORDER_UPDATE_STREAM}

    def test_binance(self):
        from luarix_exchange.brokers.binance import BinanceBroker

        self.assertEqual(BinanceBroker.capabilities(), self.TRADING | self.STREAMS | {
            Capability.MARKET_DATA, Capability.SYMBOL_RULES, Capability.DERIVATIVES, Capability.NATIVE_OCO,
            Capability.SERVER_CLOCK, Capability.FEES, Capability.ORDER_ROUNDTRIP,
            Capability.ACCOUNT_SUMMARY, Capability.FUNDING_HISTORY, Capability.INCOME_HISTORY})
        self.assertTrue(BinanceBroker.has_native(Capability.ORDER_UPDATE_STREAM, "spot"))
        self.assertEqual(BinanceBroker.markets, {Market.SPOT, Market.FUTURES})

    def test_bitunix(self):
        from luarix_exchange.brokers.bitunix import BitunixBroker

        self.assertEqual(BitunixBroker.capabilities(), self.TRADING | self.STREAMS | {
            Capability.MARKET_DATA, Capability.SYMBOL_RULES, Capability.DERIVATIVES, Capability.ACCOUNT_SUMMARY})
        # WebSocket solo en futuros; spot va por polling.
        self.assertTrue(BitunixBroker.has_native(Capability.CANDLE_STREAM, "futures"))
        self.assertFalse(BitunixBroker.has_native(Capability.CANDLE_STREAM, "spot"))
        self.assertEqual(BitunixBroker.markets, {Market.SPOT, Market.FUTURES})

    def test_paper(self):
        from luarix_exchange.brokers.paper import PaperBroker

        self.assertEqual(PaperBroker.capabilities(), self.TRADING | {Capability.NATIVE_OCO})
        self.assertEqual(PaperBroker.markets, {Market.SPOT})

    def test_list_active_sigue_funcionando_pero_avisa(self):
        from luarix_exchange.brokers.paper import PaperBroker

        broker = PaperBroker()
        broker._candles = {"BTCUSDT": {}}
        with warnings.catch_warnings(record=True) as avisos:
            warnings.simplefilter("always")
            self.assertEqual(broker.list_active(), ["BTCUSDT"])
        self.assertTrue(any(issubclass(a.category, DeprecationWarning) for a in avisos))
        self.assertEqual(broker.list_symbols(), ["BTCUSDT"])


class ExchangeFactoryTests(unittest.TestCase):
    def test_available(self):
        self.assertEqual(ExchangeFactory.available(), ["binance", "bitfinex", "bitunix", "bybit", "gate", "htx", "kraken",
                                                       "kucoin", "mexc", "mt5", "okx", "paper"])

    def test_nombre_desconocido_levanta_y_nombra_los_disponibles(self):
        with self.assertRaisesRegex(ValueError, "binance, bitfinex, bitunix, bybit"):
            ExchangeFactory.create("exmo")

    def test_capacidades_sin_instanciar_ni_credenciales(self):
        self.assertNotIn(Capability.NATIVE_OCO, ExchangeFactory.capabilities("bitunix"))
        self.assertIn(Capability.NATIVE_OCO, ExchangeFactory.capabilities("BINANCE"))

    def test_find_por_capacidad_y_mercado(self):
        con_oco = ExchangeFactory.find(Capability.NATIVE_OCO)
        self.assertIn("binance", con_oco)
        self.assertIn("paper", con_oco)
        self.assertNotIn("bitunix", con_oco)
        self.assertEqual(ExchangeFactory.find(Capability.DERIVATIVES, market="futures"), ["binance", "bitunix"])

    def test_create_paper_sin_credenciales(self):
        from luarix_exchange.brokers.paper import PaperBroker

        self.assertIsInstance(ExchangeFactory.create("paper"), PaperBroker)

    def test_importar_la_factory_no_importa_ningun_sdk(self):
        # En un proceso limpio: así no influye lo que otros tests ya importaron.
        codigo = (
            "import sys, luarix_exchange.factory, luarix_exchange.brokers.factory\n"
            "cargados = [m for m in ('binance', 'bitunix', 'MetaTrader5', 'luarix_exchange.brokers.binance',"
            " 'luarix_exchange.brokers.bitunix') if m in sys.modules]\n"
            "print(cargados)\n"
        )
        salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, check=True)
        self.assertEqual(salida.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
