# -*- coding: utf-8 -*-
"""`OrderType.STOP_LOSS` en `PaperBroker`: orden stop a mercado de ENTRADA
(R3 de las estrategias de TradingView, `strategy.entry(..., stop=p)`).

- Compra: se activa si el máximo de la vela llega a `stop_price`; venta: si
  el mínimo baja hasta él.
- Precio: el stop, salvo que la vela ABRA ya más allá (hueco) -- entonces la
  apertura, como el emulador de TradingView.
- `metadata["valid_candles"] = N`: si no llenó tras N velas evaluadas, expira.
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


def _stop(broker, side: Side, stop: float, **metadata):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=1, order_type=OrderType.STOP_LOSS,
                                    stop_price=stop, metadata=metadata))


def _fill_prices(reports):
    return [ExecutionReportFactory.parse(r).fill_price for r in reports if r["x"] == "TRADE"]


def _stop_limit(broker, side: Side, stop: float, **metadata):
    """El `stop-order` node real (`workflows/execution/nodes/execution.py::
    StopOrderExecutor`) coloca `STOP_LOSS_LIMIT`, no `STOP_LOSS` -- las dos
    son caminos de código DISTINTOS en `_fill()`, así que el hueco necesita
    su propio test acá, no alcanza con el de `STOP_LOSS` de arriba."""
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=1, order_type=OrderType.STOP_LOSS_LIMIT,
                                    stop_price=stop, metadata=metadata))


class StopEntryTests(unittest.TestCase):
    def test_buy_stop_fills_at_the_stop_when_the_high_reaches_it(self):
        broker, reports = _broker()
        _stop(broker, Side.BUY, 105)
        broker.on_candle(SYMBOL, "1h", _candle(1, 101, 104, 100, 103))  # no llega
        broker.on_candle(SYMBOL, "1h", _candle(2, 103, 107, 102, 106))  # toca 105

        self.assertEqual(_fill_prices(reports), [105.0])
        self.assertEqual([(p.side, p.quantity) for p in broker.get_positions()], [("LONG", 1.0)])

    def test_sell_stop_fills_at_the_open_when_the_candle_gaps_through_it(self):
        broker, reports = _broker()
        _stop(broker, Side.SELL, 97)
        broker.on_candle(SYMBOL, "1h", _candle(1, 95, 96, 90, 92))

        self.assertEqual(_fill_prices(reports), [95.0])

    def test_stop_loss_limit_fills_at_the_stop_when_the_high_reaches_it(self):
        broker, reports = _broker()
        _stop_limit(broker, Side.BUY, 105)
        broker.on_candle(SYMBOL, "1h", _candle(1, 101, 104, 100, 103))  # no llega
        broker.on_candle(SYMBOL, "1h", _candle(2, 103, 107, 102, 106))  # toca 105

        self.assertEqual(_fill_prices(reports), [105.0])

    def test_stop_loss_limit_fills_at_the_open_when_the_candle_gaps_through_it(self):
        broker, reports = _broker()
        _stop_limit(broker, Side.SELL, 97)
        broker.on_candle(SYMBOL, "1h", _candle(1, 95, 96, 90, 92))

        self.assertEqual(_fill_prices(reports), [95.0])

    def test_order_valid_for_one_candle_expires_if_not_triggered(self):
        broker, reports = _broker()
        _stop(broker, Side.BUY, 110, valid_candles=1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 101, 104, 100, 103))  # no llega -> expira
        broker.on_candle(SYMBOL, "1h", _candle(2, 103, 120, 102, 106))  # ya no está

        self.assertEqual(_fill_prices(reports), [])
        self.assertEqual(reports[-1]["x"], "EXPIRED")
        self.assertEqual(broker.state.pending_orders, [])

    def test_order_valid_for_one_candle_still_fills_on_that_candle(self):
        broker, reports = _broker()
        _stop(broker, Side.BUY, 102, valid_candles=1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 101, 104, 100, 103))

        self.assertEqual(_fill_prices(reports), [102.0])
        self.assertNotIn("EXPIRED", [r["x"] for r in reports])

    def test_a_candle_of_another_symbol_does_not_consume_validity(self):
        broker, reports = _broker()
        _stop(broker, Side.BUY, 102, valid_candles=1)
        broker.on_candle("ETHUSDT", "1h", _candle(1, 3000, 3100, 2900, 3050))
        broker.on_candle(SYMBOL, "1h", _candle(1, 101, 104, 100, 103))

        self.assertEqual(_fill_prices(reports), [102.0])

    def test_without_validity_the_stop_stays_pending(self):
        broker, reports = _broker()
        _stop(broker, Side.BUY, 110)
        broker.on_candle(SYMBOL, "1h", _candle(1, 101, 104, 100, 103))

        self.assertEqual(len(broker.state.pending_orders), 1)

    def test_stop_entry_reverses_a_single_position(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=2, order_type=OrderType.MARKET))
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _stop(broker, Side.SELL, 95)
        broker.on_candle(SYMBOL, "1h", _candle(2, 99, 100, 90, 92))

        self.assertEqual(_fill_prices(reports), [100.0, 95.0, 95.0])  # entrada, cierre, reversión
        self.assertEqual([(p.side, p.quantity) for p in broker.get_positions()], [("SHORT", 1.0)])

    def test_stop_without_stop_price_is_rejected_at_placement(self):
        broker, _ = _broker()
        with self.assertRaises(ValueError):
            broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=1, order_type=OrderType.STOP_LOSS))


if __name__ == "__main__":
    unittest.main()


class SingleModeTwoSidedStopsTests(unittest.TestCase):
    """Stops de entrada a los dos lados (TradingView "Volatility expansion-close"):
    en `single` solo se ejecuta la primera que toca el camino intrabar."""

    def _two_sided(self, candle):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        _stop(broker, Side.BUY, 105, valid_candles=1)
        _stop(broker, Side.SELL, 95, valid_candles=1)
        broker.on_candle(SYMBOL, "1h", candle)
        return broker, reports

    def test_high_closer_to_open_fills_the_buy_stop_only(self):
        broker, reports = self._two_sided(_candle(1, 100, 106, 90, 97))  # open->high->low
        self.assertEqual(_fill_prices(reports), [105.0])
        self.assertEqual([p.side for p in broker.get_positions()], ["LONG"])
        self.assertEqual(broker.state.pending_orders, [])  # la de venta expira con su vela

    def test_low_closer_to_open_fills_the_sell_stop_even_if_placed_second(self):
        broker, reports = self._two_sided(_candle(1, 100, 110, 94, 97))  # open->low->high
        self.assertEqual(_fill_prices(reports), [95.0])
        self.assertEqual([p.side for p in broker.get_positions()], ["SHORT"])

    def test_a_same_side_stop_rejected_by_pyramiding_does_not_block_the_reversal(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        broker.place_order(OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=1, order_type=OrderType.MARKET))
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _stop(broker, Side.BUY, 102, valid_candles=1)
        _stop(broker, Side.SELL, 95, valid_candles=1)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 103, 90, 92))  # toca las dos: la compra se rechaza

        self.assertEqual(_fill_prices(reports), [100.0, 95.0, 95.0])  # entrada, cierre, reversión
        self.assertEqual([p.side for p in broker.get_positions()], ["SHORT"])

    def test_hedge_mode_keeps_filling_both(self):
        broker, reports = _broker()
        _stop(broker, Side.BUY, 105, valid_candles=1)
        _stop(broker, Side.SELL, 95, valid_candles=1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 106, 90, 97))
        self.assertEqual(sorted(_fill_prices(reports)), [95.0, 105.0])
