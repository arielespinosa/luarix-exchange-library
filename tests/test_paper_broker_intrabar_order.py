# -*- coding: utf-8 -*-
"""Orden dentro de la vela en `PaperBroker` con `position_mode="single"`.

Cuando una vela toca varias órdenes pendientes (p. ej. el buy stop y el sell
stop de "Pivot Reversal" de TradingView), se llenan en el orden en que el
precio las alcanzaría, con el recorrido del emulador de TradingView:
open→high→low→close si el máximo está más cerca de la apertura que el mínimo;
si no, open→low→high→close. En `hedge` se mantiene el orden de inserción.

Y solo se ejecuta UNA entrada stop por vela (la primera del recorrido): la otra
sigue pendiente. Medido contra TradingView en "Volatility expansion-close"
(stops a los dos lados cada vela): 1.440 operaciones / 556 ganadoras con esta
regla = TradingView; llenando las dos, 1.510 / 562. Ver
`test_paper_broker_stop_entry.py`.
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


def _stop(broker, side: Side, stop: float):
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=1, order_type=OrderType.STOP_LOSS,
                                    stop_price=stop))


def _fills(reports):
    parsed = (ExecutionReportFactory.parse(r) for r in reports if r["x"] == "TRADE")
    return [(p.side.upper(), p.fill_price) for p in parsed]


class IntrabarOrderSinglePositionTests(unittest.TestCase):
    def test_high_first_when_the_high_is_closer_to_the_open(self):
        broker, reports = _broker(position_mode="single")
        _stop(broker, Side.SELL, 95)  # insertada primero, pero el precio pasa antes por 105
        _stop(broker, Side.BUY, 105)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 106, 93, 98))  # 6 hasta el máximo, 7 hasta el mínimo

        self.assertEqual(_fills(reports), [("BUY", 105.0)])  # la de venta sigue pendiente
        self.assertEqual([p.side for p in broker.get_positions()], ["LONG"])

    def test_low_first_when_the_low_is_closer_to_the_open(self):
        broker, reports = _broker(position_mode="single")
        _stop(broker, Side.BUY, 105)  # insertada primero, pero el precio pasa antes por 95
        _stop(broker, Side.SELL, 95)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 108, 94, 106))  # 8 hasta el máximo, 6 hasta el mínimo

        self.assertEqual(_fills(reports), [("SELL", 95.0)])  # la de compra sigue pendiente
        self.assertEqual([p.side for p in broker.get_positions()], ["SHORT"])

    def test_gap_fills_at_the_open_before_the_other_side(self):
        broker, reports = _broker(position_mode="single")
        _stop(broker, Side.BUY, 105)
        _stop(broker, Side.SELL, 95)
        broker.on_candle(SYMBOL, "1h", _candle(1, 94, 110, 90, 108))  # abre ya por debajo de 95

        self.assertEqual(_fills(reports), [("SELL", 94.0)])
        self.assertEqual([p.side for p in broker.get_positions()], ["SHORT"])

    def test_hedge_keeps_insertion_order(self):
        broker, reports = _broker()
        _stop(broker, Side.SELL, 95)
        _stop(broker, Side.BUY, 105)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 106, 93, 98))

        self.assertEqual(_fills(reports), [("SELL", 95.0), ("BUY", 105.0)])


if __name__ == "__main__":
    unittest.main()
