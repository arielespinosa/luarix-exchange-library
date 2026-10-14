# -*- coding: utf-8 -*-
"""Salidas `reduce_only` en `PaperBroker` con `position_mode="single"`: el
`strategy.exit(stop=..., limit=...)` de TradingView como dos órdenes que solo
cierran (una STOP_LOSS y una LIMIT).

- Cierran TODA la posición; sin posición (o en la misma dirección) se
  rechazan, así que la segunda pata se anula sola cuando la primera ya cerró.
- La LIMIT se llena a la apertura si la vela abre ya más allá del límite
  (mejor precio), como TradingView. En `hedge` sigue llenándose al límite.
"""

import unittest

import pandas as pd

from luarix_exchange.brokers.paper import ExecutionReportFactory, PaperBroker
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

SYMBOL = "BTCUSDT"


def _candle(hour: int, o: float, h: float, l: float, c: float) -> pd.Series:
    return pd.Series({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
                      "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _broker(**options):
    reports: list[dict] = []
    broker = PaperBroker(initial_balance=100_000, commission=0.0, on_execution_report=reports.append, **options)
    broker.on_kline = lambda tick: None
    broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
    return broker, reports


def _fills(reports):
    parsed = (ExecutionReportFactory.parse(r) for r in reports if r["x"] == "TRADE")
    return [(p.side.upper(), p.fill_price) for p in parsed]


def _open_long(broker, hour=1):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=2, order_type=OrderType.MARKET))
    broker.on_candle(SYMBOL, "1h", _candle(hour, 100, 101, 99, 100))  # se llena a 100 (next_open)


def _exits(broker, stop: float, limit: float):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=2, order_type=OrderType.STOP_LOSS,
                                    stop_price=stop, reduce_only=True, metadata={"valid_candles": 1}))
    broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=2, order_type=OrderType.LIMIT,
                                    price=limit, reduce_only=True, metadata={"valid_candles": 1}))


class ReduceOnlyExitsTests(unittest.TestCase):
    def test_take_profit_limit_closes_the_long_and_the_stop_is_rejected(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        _open_long(broker)
        _exits(broker, stop=95, limit=105)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 106, 94, 98))  # máximo más cerca: 105 antes que 95

        self.assertEqual(_fills(reports), [("BUY", 100.0), ("SELL", 105.0)])
        self.assertEqual(broker.get_positions(), [])
        self.assertIn("REJECTED", [r["x"] for r in reports])

    def test_limit_fills_at_the_open_when_the_candle_gaps_beyond_it(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        _open_long(broker)
        _exits(broker, stop=95, limit=105)
        broker.on_candle(SYMBOL, "1h", _candle(2, 108, 110, 107, 109))

        self.assertEqual(_fills(reports)[-1], ("SELL", 108.0))

    def test_stop_closes_the_long_at_its_level(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        _open_long(broker)
        _exits(broker, stop=95, limit=105)
        broker.on_candle(SYMBOL, "1h", _candle(2, 99, 100, 94, 96))

        self.assertEqual(_fills(reports)[-1], ("SELL", 95.0))
        self.assertEqual(broker.get_positions(), [])

    def test_reduce_only_without_a_position_is_rejected(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        _exits(broker, stop=95, limit=105)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 106, 94, 98))

        self.assertEqual(_fills(reports), [])
        self.assertEqual(broker.get_positions(), [])

    def test_hedge_limit_keeps_filling_at_the_limit_price(self):
        broker, reports = _broker()
        broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=1, order_type=OrderType.LIMIT,
                                        price=105))
        broker.on_candle(SYMBOL, "1h", _candle(1, 108, 110, 107, 109))

        self.assertEqual(_fills(reports), [("SELL", 105.0)])



class CloseOppositeTests(unittest.TestCase):
    """`close_opposite` (`strategy.order` de TradingView, "Rob Booker — ADX
    breakout"): sin posición abre; contra una contraria solo la cierra."""

    def _market(self, broker, side):
        broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=2, order_type=OrderType.MARKET,
                                        metadata={"close_opposite": True}))

    def test_opens_when_flat(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        self._market(broker, Side.BUY)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        self.assertEqual([p.side for p in broker.get_positions()], ["LONG"])

    def test_long_then_short_in_the_same_candle_ends_flat(self):
        """Las dos órdenes de la misma vela: la larga abre, la corta la cierra
        al mismo precio y no abre el corto."""
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        self._market(broker, Side.BUY)
        self._market(broker, Side.SELL)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))

        self.assertEqual(_fills(reports), [("BUY", 100.0), ("SELL", 100.0)])
        self.assertEqual(broker.get_positions(), [])

    def test_without_close_opposite_the_same_pair_reverses(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        for side in (Side.BUY, Side.SELL):
            broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=2, order_type=OrderType.MARKET))
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        self.assertEqual([p.side for p in broker.get_positions()], ["SHORT"])


if __name__ == "__main__":
    unittest.main()
