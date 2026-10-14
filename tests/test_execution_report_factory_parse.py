# -*- coding: utf-8 -*-
"""`ExecutionReportFactory.parse` -- el inverso de `build()`.

Traduce el mismo shape Binance que `PaperBroker` ya emite a campos tipados,
para que quien lo consume (hoy `backtesting.execution.paper_runner.PaperBacktestRunner
._on_execution_report`) no tenga que leer las claves de una letra del wire
format a mano. Cada test arma la orden con la MISMA fábrica que un fill real
usaría (`ExecutionReportFactory.new/fill/cancel`), nunca un dict a mano --
así confirma que `parse()` entiende exactamente lo que `build()` produce."""

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from luarix_exchange.brokers.paper import (
    ExecutionReportFactory,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperOrder,
    TimeInForce,
)


def _order(**overrides) -> PaperOrder:
    defaults = dict(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, quantity=Decimal("0.045"),
        order_id=1, order_list_id=-1, status=OrderStatus.NEW,
        transact_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return PaperOrder(**defaults)


class ExecutionReportFactoryParseTests(unittest.TestCase):
    def test_new_order_without_oco_is_a_new_entry_signal(self):
        report = ExecutionReportFactory.new(_order(side=OrderSide.BUY))
        parsed = ExecutionReportFactory.parse(report)

        self.assertTrue(parsed.is_new_entry_signal)
        self.assertFalse(parsed.is_oco_leg_cancellation)
        self.assertFalse(parsed.is_trade_fill)
        self.assertEqual(parsed.order_id, "1")
        self.assertIsNone(parsed.oco_list_id)
        self.assertEqual(parsed.direction, "LONG")
        self.assertEqual(parsed.event_time, datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_sell_side_translates_to_short_direction(self):
        report = ExecutionReportFactory.new(_order(side=OrderSide.SELL))
        parsed = ExecutionReportFactory.parse(report)

        self.assertEqual(parsed.direction, "SHORT")

    def test_new_order_that_is_part_of_an_oco_is_not_an_entry_signal(self):
        """Una pata de un OCO (order_list_id != -1) no es una señal de
        entrada nueva -- ver `_on_execution_report`."""
        report = ExecutionReportFactory.new(_order(order_list_id=42))
        parsed = ExecutionReportFactory.parse(report)

        self.assertFalse(parsed.is_new_entry_signal)
        self.assertEqual(parsed.oco_list_id, "42")

    def test_canceled_oco_leg_is_a_leg_cancellation(self):
        report = ExecutionReportFactory.cancel(_order(order_list_id=42, type=OrderType.LIMIT))
        parsed = ExecutionReportFactory.parse(report)

        self.assertTrue(parsed.is_oco_leg_cancellation)
        self.assertEqual(parsed.order_type, "LIMIT")

    def test_fill_carries_the_real_commission_paid(self):
        """`n` (comisión en el activo base) × `L` (precio de ejecución) =
        lo que de verdad salió de la caja -- mismo cálculo que `_fill`
        descuenta del balance."""
        order = _order(
            fill_price=Decimal("66699"), executed_qty=Decimal("0.045"),
            commission=Decimal("0.000045"), status=OrderStatus.FILLED,
        )
        report = ExecutionReportFactory.fill(order)
        parsed = ExecutionReportFactory.parse(report)

        self.assertTrue(parsed.is_trade_fill)
        self.assertAlmostEqual(parsed.commission_paid, 0.000045 * 66699, places=4)

    def test_new_order_has_zero_commission(self):
        report = ExecutionReportFactory.new(_order())
        parsed = ExecutionReportFactory.parse(report)

        self.assertEqual(parsed.commission_paid, 0.0)

    def test_fill_carries_the_real_fill_price_and_quantity(self):
        order = _order(fill_price=Decimal("66699"), executed_qty=Decimal("0.045"), status=OrderStatus.FILLED)
        report = ExecutionReportFactory.fill(order)
        parsed = ExecutionReportFactory.parse(report)

        self.assertAlmostEqual(parsed.fill_price, 66699.0, places=4)
        self.assertAlmostEqual(parsed.quantity, 0.045, places=8)

    def test_new_order_has_zero_fill_price_and_quantity(self):
        """Un report `NEW` todavía no ejecutó nada -- `L`/`z` vienen en
        `"0"` (ver `build()`), y `parse()` no debe inventar un fill donde
        no lo hay."""
        report = ExecutionReportFactory.new(_order())
        parsed = ExecutionReportFactory.parse(report)

        self.assertEqual(parsed.fill_price, 0.0)
        self.assertEqual(parsed.quantity, 0.0)

    def test_carries_the_caller_assigned_client_order_id(self):
        """`build()` emite `c` y el orquestador identifica la entrada por él
        (`position_tracking.py`, `parsed.client_order_id or parsed.order_id`)."""
        report = ExecutionReportFactory.fill(_order(client_order_id="entry-7", status=OrderStatus.FILLED))
        parsed = ExecutionReportFactory.parse(report)

        self.assertEqual(parsed.client_order_id, "entry-7")

    def test_missing_client_order_id_is_none(self):
        """`build()` lo emite como `""` cuando la orden no lo tiene: vacío no es un id."""
        parsed = ExecutionReportFactory.parse(ExecutionReportFactory.new(_order()))

        self.assertIsNone(parsed.client_order_id)


if __name__ == "__main__":
    unittest.main()
