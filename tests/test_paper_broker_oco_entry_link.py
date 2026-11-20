# -*- coding: utf-8 -*-
"""Una OCO protege a SU entrada, no a "la i-ésima entrada del símbolo".

El bug (E2E de producción 2026-11-20, BacktestRun 19): `_open_position_entries`
emparejaba la i-ésima entrada llenada con la i-ésima OCO creada para el mismo
símbolo. Con SL/TP en solo una pata del grafo (el largo protegido, el corto
no), las OCO se corrían de sitio: la OCO del largo se le asignaba al corto
anterior, el largo quedaba "sin OCO" y, cuando su SL saltaba, seguía contando
como abierto. Resultado: equity fantasma (+qty×precio del SL) y una señal
contraria posterior "cerraba" una posición que ya no existía. El backtest
reportó +15,2 % con trades cerrados que sumaban −202.
"""

import unittest

import pandas as pd

from luarix_exchange.brokers.paper import ExecutionReportFactory, PaperBroker, pair_entries_with_ocos
from luarix_exchange.models.orders import OrderRequest, OrderType, Side

SYMBOL = "BTCUSDT"


def _candle(hour: int, o: float, h: float, l: float, c: float) -> pd.Series:
    return pd.Series({"date": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=hour),
                      "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def _broker(**options) -> tuple[PaperBroker, list[dict]]:
    reports: list[dict] = []
    options.setdefault("market", "futures")
    broker = PaperBroker(initial_balance=100_000, commission=0.0, on_execution_report=reports.append,
                         fill_model="next_open", **options)
    broker.on_kline = lambda tick: None
    return broker, reports


def _market(broker: PaperBroker, side: Side, quantity: float) -> None:
    broker.place_order(OrderRequest(symbol=SYMBOL, side=side, quantity=quantity, order_type=OrderType.MARKET))


def _protect_long(broker: PaperBroker, quantity: float, tp: float, sl: float) -> None:
    """Como `execution_report_bridge._handle_entry_fill`: la OCO se coloca DESPUÉS del fill de la entrada."""
    broker.place_oco_order(SYMBOL, Side.SELL, quantity, take_profit_price=tp, stop_price=sl, stop_limit_price=sl)


def _fills(reports: list[dict]):
    return [ExecutionReportFactory.parse(r) for r in reports if r["x"] == "TRADE"]


class SinglePositionOneLegProtectedTests(unittest.TestCase):
    """El caso real: corto sin OCO → reversa a largo con OCO → salta el SL del largo."""

    def _run_until_stop_loss(self):
        broker, reports = _broker(position_mode="single")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.SELL, 1)                       # corto, sin SL/TP
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)                        # reversa: cierra el corto, abre el largo
        broker.on_candle(SYMBOL, "1h", _candle(2, 100, 101, 99, 100))
        _protect_long(broker, 1, tp=110, sl=95)
        broker.on_candle(SYMBOL, "1h", _candle(3, 99, 99, 94, 96))  # toca el SL a 95
        return broker, reports

    def test_stopped_out_long_is_no_longer_open(self):
        broker, _ = self._run_until_stop_loss()
        self.assertEqual(broker.get_positions(symbol=SYMBOL), [])

    def test_equity_has_no_phantom_position_after_the_stop(self):
        broker, _ = self._run_until_stop_loss()
        cash = broker.get_balance()["USDT"]
        self.assertAlmostEqual(broker.mark_to_market_equity(SYMBOL, 90.0), cash)
        # Corto 100→100 (0) + largo 100→95 (−5): 100.000 − 5.
        self.assertAlmostEqual(cash, 99_995.0)

    def test_next_opposite_signal_opens_instead_of_closing_a_ghost(self):
        broker, reports = self._run_until_stop_loss()
        _market(broker, Side.SELL, 1)
        broker.on_candle(SYMBOL, "1h", _candle(4, 96, 97, 95, 96))
        last = _fills(reports)[-1]
        self.assertEqual(last.side, "SELL")
        self.assertIsNone(reports[-1].get("closes"))
        self.assertEqual([(p.side, p.quantity) for p in broker.get_positions(symbol=SYMBOL)], [("SHORT", 1.0)])


class HedgeOcoBindsToItsOwnEntryTests(unittest.TestCase):

    def test_stop_closes_the_protected_entry_not_the_older_one(self):
        broker, _ = _broker(position_mode="hedge")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)                        # A @100, sin protección
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)                        # B @105, protegida
        broker.on_candle(SYMBOL, "1h", _candle(2, 105, 106, 104, 105))
        _protect_long(broker, 1, tp=120, sl=101)
        broker.on_candle(SYMBOL, "1h", _candle(3, 104, 104, 100, 102))  # SL de B

        positions = broker.get_positions(symbol=SYMBOL)
        self.assertEqual([(p.side, p.entry_price) for p in positions], [("LONG", 100.0)])

    def test_pairing_uses_the_link(self):
        broker, _ = _broker(position_mode="hedge")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(2, 105, 106, 104, 105))
        _protect_long(broker, 1, tp=120, sl=101)

        pairs = pair_entries_with_ocos(broker.state)
        by_price = {float(entry.fill_price): oco for entry, oco in pairs}
        self.assertIsNone(by_price[100.0])
        self.assertIsNotNone(by_price[105.0])
        self.assertEqual(by_price[105.0].entry_order_id, next(e.order_id for e, _ in pairs if float(e.fill_price) == 105.0))

    def test_explicit_entry_order_id_wins(self):
        broker, _ = _broker(position_mode="hedge")
        broker.on_candle(SYMBOL, "1h", _candle(0, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(1, 100, 101, 99, 100))
        _market(broker, Side.BUY, 1)
        broker.on_candle(SYMBOL, "1h", _candle(2, 105, 106, 104, 105))
        first = next(o for o in broker.state.filled_orders if float(o.fill_price) == 100.0)
        broker.place_oco_order(SYMBOL, Side.SELL, 1, take_profit_price=120, stop_price=95, stop_limit_price=95,
                               entry_order_id=first.order_id)
        self.assertEqual(broker.state.oco_orders[-1].entry_order_id, first.order_id)


class UnlinkedOcoFallbackTests(unittest.TestCase):
    """Estados sembrados a mano (orquestador al rehidratar) sin enlace: se conserva la heurística por orden."""

    def test_unlinked_oco_still_pairs_by_order(self):
        from decimal import Decimal
        from luarix_exchange.brokers.paper import OrderSide, OrderStatus, PaperOCO, PaperOrder
        from luarix_exchange.brokers.paper import OrderType as PaperOrderType

        broker, _ = _broker(position_mode="hedge")
        entry = PaperOrder(symbol=SYMBOL, side=OrderSide.BUY, type=PaperOrderType.MARKET, quantity=Decimal("1"),
                           order_id=1, status=OrderStatus.FILLED, executed_qty=Decimal("1"), fill_price=Decimal("100"))
        broker.state.filled_orders.append(entry)
        tp = PaperOrder(symbol=SYMBOL, side=OrderSide.SELL, type=PaperOrderType.LIMIT, quantity=Decimal("1"),
                        price=Decimal("110"), order_id=2, order_list_id=1, is_oco_leg=True)
        sl = PaperOrder(symbol=SYMBOL, side=OrderSide.SELL, type=PaperOrderType.STOP_LOSS_LIMIT, quantity=Decimal("1"),
                        price=Decimal("95"), stop_price=Decimal("95"), order_id=3, order_list_id=1, is_oco_leg=True)
        broker.state.oco_orders.append(PaperOCO(list_id=1, symbol=SYMBOL, tp_order=tp, sl_order=sl))

        self.assertEqual([(e.order_id, o.list_id) for e, o in pair_entries_with_ocos(broker.state)], [(1, 1)])


if __name__ == "__main__":
    unittest.main()
