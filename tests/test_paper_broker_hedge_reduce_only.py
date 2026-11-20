# -*- coding: utf-8 -*-
"""`reduce_only` en `position_mode="hedge"`: una salida cierra la posición contraria, no abre otra.

El bug (revisión de OCO de producción, 2026-11-20): en `hedge`, `reduce_only` no se miraba. El SL/TP
independiente de un largo (una venta `reduce_only`) se llenaba como una ENTRADA nueva: quedaban el largo
y un corto fantasma abiertos a la vez. En `single` ya funcionaba (`_resolve_single_position`).
"""

import unittest

import pandas as pd

from luarix_exchange.brokers.paper import ExecutionReportFactory, PaperBroker
from luarix_exchange.models.orders import OrderRequest, OrderType, Side

SYMBOL = "BTCUSDT"


def _candle(hour: int, o: float, h: float, l: float, c: float) -> pd.Series:
    return pd.Series({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
                      "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _broker():
    reports: list[dict] = []
    broker = PaperBroker(initial_balance=10_000, commission=0.0, fill_model="next_open", market="futures",
                         position_mode="hedge", on_execution_report=reports.append)
    broker.on_kline = lambda tick: None
    broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
    return broker, reports


def _buy(broker, hour, price):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=1, order_type=OrderType.MARKET))
    broker.on_candle(SYMBOL, "1h", _candle(hour, price, price + 1, price - 1, price))


def _stop_exit(broker, stop, **metadata):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=1, order_type=OrderType.STOP_LOSS_LIMIT,
                                    price=stop, stop_price=stop, reduce_only=True, metadata=metadata))


def _positions(broker):
    return [(p.side, p.quantity, p.entry_price) for p in broker.get_positions(symbol=SYMBOL)]


class HedgeReduceOnlyTests(unittest.TestCase):

    def test_stop_exit_closes_the_long_instead_of_opening_a_short(self):
        broker, reports = _broker()
        _buy(broker, 1, 100)
        _stop_exit(broker, 95)
        broker.on_candle(SYMBOL, "1h", _candle(2, 99, 99, 94, 96))

        self.assertEqual(_positions(broker), [])
        self.assertAlmostEqual(broker.get_balance()["USDT"], 9_995.0)
        entry_id = next(o.order_id for o in broker.state.filled_orders if o.side.value == "BUY")
        self.assertEqual(reports[-1]["closes"], str(entry_id))

    def test_explicit_entry_is_the_one_closed(self):
        broker, _ = _broker()
        _buy(broker, 1, 100)   # A
        _buy(broker, 2, 110)   # B (la más reciente)
        first = next(o for o in broker.state.filled_orders if float(o.fill_price) == 100.0)
        _stop_exit(broker, 95, closes_entry_order_id=first.order_id)
        broker.on_candle(SYMBOL, "1h", _candle(3, 99, 99, 94, 96))

        self.assertEqual(_positions(broker), [("LONG", 1.0, 110.0)])

    def test_without_an_explicit_entry_the_latest_opposite_is_closed(self):
        broker, _ = _broker()
        _buy(broker, 1, 100)
        _buy(broker, 2, 110)
        _stop_exit(broker, 95)
        broker.on_candle(SYMBOL, "1h", _candle(3, 99, 99, 94, 96))

        self.assertEqual(_positions(broker), [("LONG", 1.0, 100.0)])

    def test_reduce_only_with_nothing_to_close_is_rejected(self):
        broker, reports = _broker()
        _stop_exit(broker, 95)
        broker.on_candle(SYMBOL, "1h", _candle(1, 99, 99, 94, 96))

        self.assertEqual(reports[-1]["X"], "REJECTED")
        self.assertTrue(ExecutionReportFactory.parse(reports[-1]).is_rejected_entry)
        self.assertEqual(_positions(broker), [])

    def test_closing_cancels_the_positions_own_oco(self):
        broker, _ = _broker()
        _buy(broker, 1, 100)
        broker.place_oco_order(SYMBOL, Side.SELL, 1, take_profit_price=130, stop_price=80, stop_limit_price=80)
        _stop_exit(broker, 95)
        broker.on_candle(SYMBOL, "1h", _candle(2, 99, 99, 94, 96))

        self.assertFalse(broker.state.oco_orders[-1].active)
        self.assertEqual([o for o in broker.state.pending_orders if o.is_oco_leg], [])

    def test_plain_orders_in_hedge_keep_opening_positions(self):
        """Sin `reduce_only` no cambia nada: en `hedge` una venta abre un corto."""
        broker, _ = _broker()
        _buy(broker, 1, 100)
        broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.SELL, quantity=1, order_type=OrderType.MARKET))
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 99, 100))

        self.assertEqual(sorted(p[0] for p in _positions(broker)), ["LONG", "SHORT"])


if __name__ == "__main__":
    unittest.main()
