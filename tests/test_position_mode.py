# -*- coding: utf-8 -*-
"""`Broker.get_position_mode(market)`: el modo de posición de la CUENTA en el exchange.

`"single"` = one-way (una posición neta por símbolo; una orden contraria la reduce o la invierte).
`"hedge"` = cobertura (largo y corto conviven; una orden contraria abre otra posición).
Mismo vocabulario que `PaperBroker.POSITION_MODES`, para que el simulador pueda imitar la cuenta real.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from luarix_exchange.brokers.binance import BinanceBroker
from luarix_exchange.brokers.bitunix import BitunixBroker
from luarix_exchange.brokers.paper import PaperBroker


class BitunixPositionModeTests(unittest.TestCase):

    def _broker(self, raw_mode: str) -> BitunixBroker:
        broker = BitunixBroker.__new__(BitunixBroker)
        futures = SimpleNamespace(position_mode=mock.Mock(return_value=raw_mode))
        broker.account_data_provider = SimpleNamespace(futures=futures)
        return broker

    def test_hedge(self):
        self.assertEqual(self._broker("HEDGE").get_position_mode("futures"), "hedge")

    def test_one_way(self):
        self.assertEqual(self._broker("ONE_WAY").get_position_mode("futures"), "single")

    def test_spot_is_always_single_without_asking(self):
        broker = self._broker("HEDGE")
        self.assertEqual(broker.get_position_mode("spot"), "single")
        broker.account_data_provider.futures.position_mode.assert_not_called()


class BinancePositionModeTests(unittest.TestCase):

    def _broker(self, dual: bool) -> BinanceBroker:
        broker = BinanceBroker.__new__(BinanceBroker)
        client = SimpleNamespace(futures_get_position_mode=mock.Mock(return_value={"dualSidePosition": dual}))
        broker._client = lambda: client  # noqa: SLF001 -- doble del cliente de python-binance
        return broker

    def test_dual_side_is_hedge(self):
        self.assertEqual(self._broker(True).get_position_mode("futures"), "hedge")

    def test_one_way_is_single(self):
        self.assertEqual(self._broker(False).get_position_mode("futures"), "single")

    def test_spot_is_single(self):
        self.assertEqual(self._broker(True).get_position_mode("spot"), "single")


class PaperPositionModeTests(unittest.TestCase):

    def test_reports_its_own_mode(self):
        self.assertEqual(PaperBroker(position_mode="single").get_position_mode("futures"), "single")
        self.assertEqual(PaperBroker(position_mode="hedge").get_position_mode("futures"), "hedge")


if __name__ == "__main__":
    unittest.main()
