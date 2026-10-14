# -*- coding: utf-8 -*-
"""`PaperBroker(position_mode=..., fill_model=...)` -- las dos opciones que
necesitan las estrategias de TradingView (paridad de backtest):

- `fill_model="next_open"`: una orden MARKET se llena a la APERTURA de la vela
  siguiente, como el emulador de TradingView. El default (`"signal_close"`)
  conserva el comportamiento de siempre: LIMIT interna al cierre de la señal.
- `position_mode="single"`: una sola posición por símbolo, con la semántica de
  `strategy.entry` y pirámide 1 -- una orden contraria cierra la posición y
  abre la opuesta; una en la misma dirección se rechaza. El default
  (`"hedge"`) conserva el de siempre: cada fill es una entrada independiente.

Cada test ejercita el broker real con velas mínimas (`on_candle`)."""

import unittest

import pandas as pd

from luarix_exchange.brokers.paper import ExecutionReportFactory, PaperBroker
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

SYMBOL = "BTCUSDT"


def _candle(hour: int, o: float, h: float, l: float, c: float) -> pd.Series:
    return pd.Series({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
                      "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _broker(**options) -> tuple[PaperBroker, list[dict]]:
    reports: list[dict] = []
    broker = PaperBroker(initial_balance=100_000, commission=0.0, on_execution_report=reports.append, **options)
    broker.on_kline = lambda tick: None
    return broker, reports


def _market(broker: PaperBroker, side: Side, quantity: float, **extra) -> None:
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=quantity, order_type=OrderType.MARKET, **extra))


def _fills(reports: list[dict]):
    return [ExecutionReportFactory.parse(r) for r in reports if r["x"] == "TRADE"]


def _positions(broker: PaperBroker) -> list[tuple[str, float]]:
    return [(p.side, p.quantity) for p in broker.get_positions(symbol=SYMBOL)]


class OptionValidationTests(unittest.TestCase):
    def test_defaults_keep_the_historical_behaviour(self):
        broker = PaperBroker()
        self.assertEqual((broker.position_mode, broker.fill_model), ("hedge", "signal_close"))

    def test_unknown_values_are_rejected(self):
        with self.assertRaises(ValueError):
            PaperBroker(position_mode="netting-ish")
        with self.assertRaises(ValueError):
            PaperBroker(fill_model="tomorrow")


class OptionsFromNodeConfigTests(unittest.TestCase):
    """`PaperBroker.options_from_config`: la config del nodo `paperbroker`
    traducida a kwargs del constructor -- la MISMA función en los dos motores
    (Django y orquestador), para que no puedan divergir."""

    def test_empty_config_gives_the_historical_defaults(self):
        self.assertEqual(PaperBroker.options_from_config({}),
                         {"commission": 0.001, "position_mode": "hedge", "fill_model": "signal_close"})
        self.assertEqual(PaperBroker.options_from_config(None)["commission"], 0.001)

    def test_reads_the_three_options_with_commission_in_percent(self):
        options = PaperBroker.options_from_config(
            {"commission_pct": 0, "position_mode": "single", "fill_model": "next_open", "market": "spot"})
        self.assertEqual(options, {"commission": 0.0, "position_mode": "single", "fill_model": "next_open"})
        self.assertAlmostEqual(PaperBroker.options_from_config({"commission_pct": "0.075"})["commission"], 0.00075)

    def test_blank_values_fall_back_to_defaults(self):
        options = PaperBroker.options_from_config({"commission_pct": "", "position_mode": "", "fill_model": None})
        self.assertEqual(options, {"commission": 0.001, "position_mode": "hedge", "fill_model": "signal_close"})

    def test_invalid_values_fail_loudly(self):
        for config in ({"commission_pct": "abc"}, {"commission_pct": -1}, {"position_mode": "x"}, {"fill_model": "x"}):
            with self.subTest(config), self.assertRaises(ValueError):
                PaperBroker.options_from_config(config)


class NextOpenFillModelTests(unittest.TestCase):
    def test_market_order_fills_at_the_next_candle_open(self):
        broker, reports = _broker(fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.SELL, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 110, 120, 105, 115))

        (fill,) = _fills(reports)
        self.assertEqual(fill.fill_price, 110.0)
        self.assertEqual(fill.event_time, pd.Timestamp("2026-01-01 01:00", tz="UTC"))

    def test_market_order_fills_even_if_the_signal_close_is_never_touched_again(self):
        """Con el default, una compra a mercado colocada al cierre 100 no llena si
        la vela siguiente no vuelve a tocar 100 (abre y se queda en 110-120)."""
        broker, reports = _broker(fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 110, 120, 109, 115))

        self.assertEqual([f.fill_price for f in _fills(reports)], [110.0])

    def test_default_fill_model_is_unchanged(self):
        broker, reports = _broker()
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.SELL, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 110, 120, 105, 115))

        self.assertEqual([f.fill_price for f in _fills(reports)], [100.0])


class SinglePositionModeTests(unittest.TestCase):
    def _long_then(self, side: Side, quantity: float, **extra):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 2)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, side, quantity, **extra)
        broker.on_candle(SYMBOL, "1h", _candle(2, 120, 121, 119, 120))
        return broker, reports

    def test_opposite_order_closes_the_position_and_opens_the_reverse_with_its_own_quantity(self):
        broker, reports = self._long_then(Side.SELL, 3)

        entry, close, reverse = _fills(reports)
        self.assertTrue(close.is_position_close)
        self.assertEqual(close.closes_order_id, entry.order_id)
        self.assertEqual((close.side, close.quantity, close.fill_price), ("SELL", 2.0, 120.0))
        self.assertFalse(reverse.is_position_close)
        self.assertEqual((reverse.side, reverse.quantity, reverse.fill_price), ("SELL", 3.0, 120.0))
        self.assertEqual(_positions(broker), [("SHORT", 3.0)])
        # 100.000 - 200 (compra 2 @100) + 240 (cierre 2 @120) + 360 (corto 3 @120)
        self.assertAlmostEqual(broker.get_balance()["USDT"], 100_400.0)
        self.assertAlmostEqual(broker.mark_to_market_equity(SYMBOL, 120.0), 100_040.0)

    def test_close_report_carries_the_closed_entry_client_order_id(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1, client_order_id="entry-1")
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, Side.SELL, 1)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 99, 100))

        close = next(f for f in _fills(reports) if f.is_position_close)
        self.assertEqual(close.closes_client_order_id, "entry-1")

    def test_same_direction_order_is_rejected_pyramiding_one(self):
        broker, reports = self._long_then(Side.BUY, 1)

        self.assertEqual(len(_fills(reports)), 1)
        self.assertEqual(reports[-1]["x"], "REJECTED")
        self.assertEqual(_positions(broker), [("LONG", 2.0)])
        self.assertEqual(broker.state.pending_orders, [])

    def test_reduce_only_order_only_closes(self):
        broker, reports = self._long_then(Side.SELL, 2, reduce_only=True)

        entry, close = _fills(reports)
        self.assertEqual(close.closes_order_id, entry.order_id)
        self.assertEqual(_positions(broker), [])
        self.assertAlmostEqual(broker.get_balance()["USDT"], 100_040.0)

    def test_reduce_only_without_a_position_is_rejected(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.SELL, 1, reduce_only=True)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))

        self.assertEqual(_fills(reports), [])
        self.assertEqual(reports[-1]["x"], "REJECTED")
        self.assertEqual(_positions(broker), [])

    def test_reversal_cancels_the_oco_that_protected_the_closed_entry(self):
        broker, reports = _broker(position_mode="single", fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        broker.place_oco_order(SYMBOL, Side.SELL, 1, take_profit_price=150, stop_price=50, stop_limit_price=50)
        _market(broker, Side.SELL, 1)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 99, 100))

        self.assertEqual(broker.state.pending_orders, [])
        self.assertFalse(broker.state.oco_orders[0].active)
        self.assertEqual(_positions(broker), [("SHORT", 1.0)])

    def test_hedge_mode_is_unchanged_two_independent_positions(self):
        broker, reports = _broker(fill_model="next_open")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, Side.SELL, 1)
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 99, 100))

        self.assertEqual(sorted(_positions(broker)), [("LONG", 1.0), ("SHORT", 1.0)])
        self.assertFalse(any(f.is_position_close for f in _fills(reports)))


if __name__ == "__main__":
    unittest.main()
