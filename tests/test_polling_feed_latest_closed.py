# -*- coding: utf-8 -*-
"""`PollingMarketDataFeed` emite la ÚLTIMA vela cerrada, no la penúltima.

Regresión (2026-10-18): emitía `candles[-2]` suponiendo que la última vela
seguía formándose, pero los proveedores ya descartan la vela en curso. Cada
vela llegaba un periodo tarde a las estrategias en vivo sobre polling
(Bitunix en web-server y orchestator). Comprobado contra la API real: a las
05:30:52 las velas cerradas eran 05:27, 05:28, 05:29 y el feed emitía 05:28."""

import unittest
from datetime import datetime, timedelta, timezone

from luarix_exchange.brokers.market_data.live_feed import PollingMarketDataFeed
from luarix_exchange.models.market import Candle

T0 = datetime(2026, 10, 18, 5, 27, tzinfo=timezone.utc)


def _vela(minuto: int, cerrada: bool = True) -> Candle:
    return Candle(symbol="BTCUSDT", timeframe="1m", open_time=T0 + timedelta(minutes=minuto),
                  open=1, high=1, low=1, close=1, volume=1, is_closed=cerrada)


class _Proveedor:
    def __init__(self, velas):
        self.velas = velas

    def get_last_candles(self, symbol, interval="1m", limit=3):
        return self.velas


class PollingLatestClosedTests(unittest.TestCase):
    def test_emite_la_ultima_vela_cerrada(self):
        emitidas = []
        PollingMarketDataFeed(_Proveedor([_vela(0), _vela(1), _vela(2)]))._poll_one("BTCUSDT", "1m", emitidas.append)
        self.assertEqual(emitidas[0].open_time, T0 + timedelta(minutes=2))

    def test_si_el_proveedor_cuela_la_vela_en_curso_no_la_emite(self):
        emitidas = []
        PollingMarketDataFeed(_Proveedor([_vela(0), _vela(1), _vela(2, cerrada=False)]))._poll_one(
            "BTCUSDT", "1m", emitidas.append)
        self.assertEqual(emitidas[0].open_time, T0 + timedelta(minutes=1))

    def test_no_repite_la_misma_vela(self):
        emitidas = []
        feed = PollingMarketDataFeed(_Proveedor([_vela(0), _vela(1)]))
        feed._poll_one("BTCUSDT", "1m", emitidas.append)
        feed._poll_one("BTCUSDT", "1m", emitidas.append)
        self.assertEqual(len(emitidas), 1)


if __name__ == "__main__":
    unittest.main()
