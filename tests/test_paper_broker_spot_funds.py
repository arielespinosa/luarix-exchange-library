# -*- coding: utf-8 -*-
"""Al contado no se compra lo que no se puede pagar.

El bug (E2E de producción 2026-11-20, BacktestRun 18): `_execute` restaba el
nocional de la caja sin mirar si alcanzaba. 56 compras de ~1.000 USD con
10.000 de capital dejaron la caja en −46.081 y el backtest terminó con
`final_capital` −1.248 y un drawdown del 113 %, presentado como válido.
"""

import unittest

import pandas as pd

from luarix_exchange.brokers.paper import ExecutionReportFactory, PaperBroker
from luarix_exchange.models.orders import OrderRequest, OrderType, Side

SYMBOL = "BTCUSDT"


def _candle(hour: int, price: float) -> pd.Series:
    return pd.Series({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
                      "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1.0})


def _broker(**options) -> tuple[PaperBroker, list[dict]]:
    reports: list[dict] = []
    options.setdefault("commission", 0.0)
    broker = PaperBroker(initial_balance=1_000, on_execution_report=reports.append, fill_model="next_open", **options)
    broker.on_kline = lambda tick: None
    return broker, reports


def _buy(broker: PaperBroker, quantity: float, side: Side = Side.BUY) -> None:
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=quantity, order_type=OrderType.MARKET))


def _kinds(reports: list[dict]) -> list[str]:
    return [r["X"] for r in reports]


class SpotFundsTests(unittest.TestCase):

    def test_default_market_is_spot(self):
        self.assertEqual(PaperBroker().market, "spot")

    def test_buy_within_cash_fills(self):
        broker, reports = _broker(market="spot")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100))
        _buy(broker, 5)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100))
        self.assertAlmostEqual(broker.get_balance()["USDT"], 500.0)
        self.assertEqual(_kinds(reports)[-1], "FILLED")

    def test_buy_beyond_cash_is_rejected_and_cash_is_untouched(self):
        broker, reports = _broker(market="spot")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100))
        _buy(broker, 5)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100))
        _buy(broker, 6)  # 600 con 500 en caja
        broker.on_candle(SYMBOL, "1h", _candle(2, 100))

        self.assertEqual(_kinds(reports)[-1], "REJECTED")
        self.assertTrue(ExecutionReportFactory.parse(reports[-1]).is_rejected_entry)
        self.assertAlmostEqual(broker.get_balance()["USDT"], 500.0)
        self.assertEqual([(p.side, p.quantity) for p in broker.get_positions(symbol=SYMBOL)], [("LONG", 5.0)])

    def test_commission_counts_against_cash(self):
        broker, reports = _broker(market="spot", commission=0.01)
        broker.on_candle(SYMBOL, "1h", _candle(0, 100))
        _buy(broker, 10)  # 1.000 + 10 de comisión con 1.000 en caja
        broker.on_candle(SYMBOL, "1h", _candle(1, 100))
        self.assertEqual(_kinds(reports)[-1], "REJECTED")

    def test_selling_a_held_long_is_always_allowed(self):
        broker, reports = _broker(market="spot", position_mode="single")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100))
        _buy(broker, 10)  # toda la caja
        broker.on_candle(SYMBOL, "1h", _candle(1, 100))
        self.assertAlmostEqual(broker.get_balance()["USDT"], 0.0)
        _buy(broker, 10, side=Side.SELL)  # caja a 0: vender (cerrar) no se bloquea nunca
        broker.on_candle(SYMBOL, "1h", _candle(2, 100))
        self.assertNotIn("REJECTED", _kinds(reports))
        self.assertTrue(any(r.get("closes") for r in reports))

    def test_futures_keep_the_historical_behaviour(self):
        broker, reports = _broker(market="futures")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100))
        _buy(broker, 20)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100))
        self.assertEqual(_kinds(reports)[-1], "FILLED")


class OptionsFromConfigMarketTests(unittest.TestCase):

    def test_market_defaults_to_spot(self):
        self.assertEqual(PaperBroker.options_from_config(None)["market"], "spot")

    def test_market_from_config(self):
        self.assertEqual(PaperBroker.options_from_config({"market": "futures"})["market"], "futures")

    def test_unknown_market_raises(self):
        with self.assertRaises(ValueError):
            PaperBroker.options_from_config({"market": "options"})


if __name__ == "__main__":
    unittest.main()
