# -*- coding: utf-8 -*-
"""`PaperBroker.set_position_exit`: salida de la posición con stop loss y
trailing stop, como el `strategy.exit(loss=, trail_points=, trail_offset=)`
de TradingView re-emitido en cada vela (R12, estrategia "Technical ratings").

Reglas medidas contra TradingView (178/178 operaciones, 28 al centavo):
1. Vale una vela y el trailing no se arrastra: solo salta si se activa y se
   toca dentro de la vela.
2. Pedida con una posición abierta, cubre ESA posición (tras una reversión,
   la nueva no tiene salida en su primera vela); pedida en plano, cubre la
   entrada que llene a continuación.
3. Si la vela abre más allá de la activación, el trailing se activa en la
   apertura.
"""

import unittest

import pandas as pd

from luarix_exchange.brokers.paper import ExecutionReportFactory, PaperBroker, PositionExit
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

SYMBOL = "BTCUSDT"


def _candle(hour: int, o: float, h: float, l: float, c: float) -> pd.Series:
    return pd.Series({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
                      "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _broker():
    reports: list[dict] = []
    broker = PaperBroker(initial_balance=100_000, commission=0.0, on_execution_report=reports.append,
                         position_mode="single", fill_model="next_open")
    broker.on_kline = lambda tick: None
    broker.on_candle(SYMBOL, "1h", _candle(0, 100, 100, 100, 100))
    return broker, reports


def _market(broker, side: Side):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=1, order_type=OrderType.MARKET))


def _closes(reports):
    parsed = [ExecutionReportFactory.parse(r) for r in reports if r["x"] == "TRADE"]
    return [(p.fill_price, p.close_reason) for p in parsed if p.is_position_close]


def _long_at_100(broker):
    """Largo llenado a 100 en la vela 1 (sin salida)."""
    _market(broker, Side.BUY)
    broker.on_candle(SYMBOL, "1h", _candle(1, 100, 100.5, 99.5, 100))


EXIT = PositionExit(loss=5, trail_points=4, trail_offset=2)


class PositionExitTests(unittest.TestCase):
    def test_stop_loss_closes_at_the_stop_and_reports_the_reason(self):
        broker, reports = _broker()
        _long_at_100(broker)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 90, 92))

        self.assertEqual(_closes(reports), [(95.0, "SL")])
        self.assertEqual(broker.get_positions(), [])

    def test_trailing_activated_and_touched_within_the_bar(self):
        broker, reports = _broker()
        _long_at_100(broker)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 106, 93, 97))  # open->high 106 (activa)->low: stop 104

        self.assertEqual(_closes(reports), [(104.0, "SL")])

    def test_trailing_does_not_carry_over_to_the_next_bar(self):
        broker, reports = _broker()
        _long_at_100(broker)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 106, 99.5, 105))  # activa en 106, cierra sin tocar 104
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(3, 103.5, 103.8, 101, 102))  # bajo la activación: no hay trailing

        self.assertEqual(_closes(reports), [])

    def test_bar_opening_past_activation_activates_at_the_open(self):
        broker, reports = _broker()
        _long_at_100(broker)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 110, 111, 105, 106))  # best 111 -> stop 109

        self.assertEqual(_closes(reports), [(109.0, "SL")])

    def test_bar_opening_past_the_stop_closes_at_the_open(self):
        broker, reports = _broker()
        _long_at_100(broker)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 90, 91, 88, 89))

        self.assertEqual(_closes(reports), [(90.0, "SL")])

    def test_requested_while_flat_covers_the_entry_filled_on_the_next_bar(self):
        broker, reports = _broker()
        _market(broker, Side.BUY)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 100.5, 90, 92))  # entra a 100 y toca 95 en la misma vela

        self.assertEqual(_closes(reports), [(95.0, "SL")])

    def test_requested_with_an_open_position_does_not_cover_the_reversed_one(self):
        broker, reports = _broker()
        _long_at_100(broker)
        _market(broker, Side.SELL)  # revierte a corto en la apertura de la vela 2
        broker.set_position_exit(SYMBOL, EXIT)  # cubre el LARGO que hay ahora
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 110, 99.5, 108))  # el corto nuevo no tiene stop (105)

        self.assertEqual(_closes(reports), [(100.0, None)])  # solo el cierre del largo por la reversión
        self.assertEqual([p.side for p in broker.get_positions()], ["SHORT"])

    def test_it_lasts_one_bar_unless_requested_again(self):
        broker, reports = _broker()
        _long_at_100(broker)
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 99, 100))
        broker.on_candle(SYMBOL, "1h", _candle(3, 100, 101, 90, 92))  # no se volvió a pedir

        self.assertEqual(_closes(reports), [])

    def test_short_positions_mirror_the_rules(self):
        broker, reports = _broker()
        _market(broker, Side.SELL)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 100.5, 99.5, 100))
        broker.set_position_exit(SYMBOL, EXIT)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 107, 94, 103))  # open->low 94 (activa)->high: stop 96

        self.assertEqual(_closes(reports), [(96.0, "SL")])

    def test_only_single_position_mode(self):
        broker = PaperBroker(initial_balance=100_000, commission=0.0)
        with self.assertRaises(ValueError):
            broker.set_position_exit(SYMBOL, EXIT)

    def test_invalid_exits_are_rejected(self):
        for kwargs in ({}, {"trail_points": 4}, {"loss": 0}, {"loss": -1}, {"trail_points": 4, "trail_offset": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PositionExit(**kwargs)


if __name__ == "__main__":
    unittest.main()
