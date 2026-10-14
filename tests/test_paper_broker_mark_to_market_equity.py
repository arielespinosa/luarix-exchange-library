# -*- coding: utf-8 -*-
"""`PaperBroker.mark_to_market_equity`/`get_positions` -- el capital real
(caja + valor de mercado de lo abierto), y las posiciones abiertas mismas.

El bug que esto fija: `get_balance()["USDT"]` a secas no es el capital de un
simulador que opera al contado -- vender (abrir un corto) acredita el
importe en USDT de inmediato, así que una posición corta todavía abierta
deja la caja inflada con el producto de una venta que aún no se recompró.

Caso real que lo destapó: una corrida que terminó con UN corto abierto de
0,045 BTC @ 66.699 informaba `final_capital = 12.998` sobre 10.000
iniciales -- +30% con CERO operaciones cerradas. Con la posición valorada a
mercado, la misma corrida da ~9.997: los 10.000 menos la comisión de abrir.
Ver `docs/plans/bugs.md`.

**Corrección 2026-08-13**: la versión anterior de este archivo armaba el
estado a mano vía `PaperPosition`/`state.positions` -- un campo que,
confirmado con `grep` exhaustivo del repo, NUNCA se popula en código de
producción (solo lo hacía este test). O sea que `mark_to_market_equity`/
`get_positions` pasaban el test pero seguían rotos contra cualquier corrida
real. Reescrito para armar el estado vía `state.filled_orders`/
`state.oco_orders` -- el mismo dato que SÍ puebla `_fill()` de verdad, y el
mismo criterio de reconstrucción que ya usaba (y seguía usando, evitando
`get_positions()` por este motivo) `workflows/execution/operations_snapshot.py`
en `luarix-web-server`. Ver `docs/plans/bugs.md` para el detalle completo.
"""

import unittest
from decimal import Decimal
from datetime import datetime, timezone

from luarix_exchange.brokers.paper import OrderSide, OrderStatus, OrderType, PaperBroker, PaperOCO, PaperOrder


def _filled_entry(
    broker: PaperBroker, *, side: OrderSide, quantity: str, entry_price: str,
    symbol: str = "BTCUSDT", order_id: int = 1,
) -> PaperOrder:
    """Agrega una orden de ENTRADA ya llenada directamente al estado del
    broker -- sin pasar por `place_order`/`_fill` (mecánica de matching no
    relevante acá): `mark_to_market_equity`/`get_positions` son funciones
    puras sobre `self._balance`/`self.state.filled_orders`/`self.state.oco_orders`,
    así que alcanza con darles exactamente ese estado."""
    order = PaperOrder(
        symbol=symbol, side=side, type=OrderType.MARKET, quantity=Decimal(quantity),
        order_id=order_id, status=OrderStatus.FILLED, executed_qty=Decimal(quantity),
        fill_price=Decimal(entry_price), transact_time=datetime.now(timezone.utc),
    )
    broker.state.filled_orders.append(order)
    return order


def _protect_with_oco(broker: PaperBroker, entry: PaperOrder, *, active: bool) -> None:
    """Asocia una OCO (activa = posición sigue abierta, o ya disparada =
    posición cerrada) a `entry` -- por emparejamiento de símbolo/orden
    cronológico, no por FK directo (`PaperBroker` no tiene uno)."""
    closing_side = OrderSide.SELL if entry.side == OrderSide.BUY else OrderSide.BUY
    tp = PaperOrder(symbol=entry.symbol, side=closing_side, type=OrderType.TAKE_PROFIT_LIMIT, quantity=entry.quantity)
    sl = PaperOrder(symbol=entry.symbol, side=closing_side, type=OrderType.STOP_LOSS, quantity=entry.quantity)
    broker.state.oco_orders.append(PaperOCO(
        list_id=entry.order_id + 1000, symbol=entry.symbol, tp_order=tp, sl_order=sl, active=active,
        created_at=datetime.now(timezone.utc),
    ))


class MarkToMarketEquityTests(unittest.TestCase):
    def test_flat_equity_equals_cash(self):
        """Sin ninguna posición abierta, el capital real es exactamente la
        caja -- caso base, sin exposición que valorar."""
        broker = PaperBroker(initial_balance=10_000.0)
        self.assertEqual(broker.mark_to_market_equity("BTCUSDT", price=66_699.0), 10_000.0)

    def test_open_short_does_not_inflate_equity_with_the_sale_proceeds(self):
        """El caso real documentado: la caja quedó en 12.998,45 (10.000 +
        el importe de la venta corta menos comisión) pero el capital real
        es ~9.997 -- la posición corta resta su valor de mercado, no lo
        suma."""
        broker = PaperBroker(initial_balance=10_000.0)
        broker._balance = 12_998.45  # caja inflada por la venta corta pendiente de recomprar
        _filled_entry(broker, side=OrderSide.SELL, quantity="0.045", entry_price="66699")

        equity = broker.mark_to_market_equity("BTCUSDT", price=66_699.0)

        self.assertAlmostEqual(equity, 9_996.995, places=2)
        self.assertLess(equity, broker.get_balance()["USDT"])

    def test_open_long_adds_its_market_value_to_cash(self):
        """Un largo abierto SÍ suma su valor de mercado -- la caja ya
        descontó el costo de comprarlo, así que sin sumar la exposición el
        capital real se subestimaría."""
        broker = PaperBroker(initial_balance=10_000.0)
        broker._balance = 6_995.545  # caja ya debitada por el costo + comisión de la compra
        _filled_entry(broker, side=OrderSide.BUY, quantity="0.045", entry_price="66699")

        equity = broker.mark_to_market_equity("BTCUSDT", price=66_699.0)

        self.assertAlmostEqual(equity, 9_997.0, places=1)

    def test_closed_positions_are_ignored(self):
        """Una entrada cuya OCO ya disparó (TP o SL) no debe seguir
        aportando exposición -- el capital vuelve a ser solo la caja."""
        broker = PaperBroker(initial_balance=10_000.0)
        entry = _filled_entry(broker, side=OrderSide.SELL, quantity="0.045", entry_price="66699")
        _protect_with_oco(broker, entry, active=False)

        self.assertEqual(broker.mark_to_market_equity("BTCUSDT", price=66_699.0), 10_000.0)

    def test_other_symbols_are_ignored(self):
        broker = PaperBroker(initial_balance=10_000.0)
        _filled_entry(broker, side=OrderSide.SELL, quantity="1", entry_price="3000", symbol="ETHUSDT")

        self.assertEqual(broker.mark_to_market_equity("BTCUSDT", price=66_699.0), 10_000.0)


class GetPositionsTests(unittest.TestCase):
    """`get_positions()` -- reconstruida por el mismo mecanismo que
    `mark_to_market_equity`, ver el docstring del módulo. Antes SIEMPRE
    devolvía `[]` en cualquier corrida real (ver `docs/plans/bugs.md`)."""

    def test_reports_an_open_long_position(self):
        broker = PaperBroker(initial_balance=10_000.0)
        _filled_entry(broker, side=OrderSide.BUY, quantity="0.045", entry_price="66699")

        positions = broker.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "BTCUSDT")
        self.assertEqual(positions[0].side, "LONG")
        self.assertAlmostEqual(positions[0].quantity, 0.045)
        self.assertAlmostEqual(positions[0].entry_price, 66699.0)

    def test_a_position_without_any_oco_stays_open(self):
        """Una entrada sin stop-loss/take-profit conectado no tiene OCO que
        la cierre -- sigue abierta indefinidamente, mismo criterio
        documentado en `workflow.backtesting.position_tracking` del lado
        del orchestrator."""
        broker = PaperBroker(initial_balance=10_000.0)
        _filled_entry(broker, side=OrderSide.BUY, quantity="0.045", entry_price="66699")

        self.assertEqual(len(broker.get_positions()), 1)

    def test_a_position_closed_by_its_oco_is_not_reported(self):
        broker = PaperBroker(initial_balance=10_000.0)
        entry = _filled_entry(broker, side=OrderSide.BUY, quantity="0.045", entry_price="66699")
        _protect_with_oco(broker, entry, active=False)

        self.assertEqual(broker.get_positions(), [])

    def test_filters_by_symbol(self):
        broker = PaperBroker(initial_balance=10_000.0)
        _filled_entry(broker, side=OrderSide.BUY, quantity="0.045", entry_price="66699", symbol="BTCUSDT", order_id=1)
        _filled_entry(broker, side=OrderSide.BUY, quantity="1", entry_price="3000", symbol="ETHUSDT", order_id=2)

        positions = broker.get_positions(symbol="BTCUSDT")

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "BTCUSDT")

    def test_two_sequential_entries_on_the_same_symbol_pair_with_their_own_oco_in_order(self):
        """Confirma el emparejamiento cronológico: la primera entrada se
        empareja con la primera OCO creada para ese símbolo, la segunda con
        la segunda -- no todas contra la misma."""
        broker = PaperBroker(initial_balance=10_000.0)
        first = _filled_entry(broker, side=OrderSide.BUY, quantity="0.01", entry_price="60000", order_id=1)
        _protect_with_oco(broker, first, active=False)  # la primera ya cerró
        second = _filled_entry(broker, side=OrderSide.BUY, quantity="0.02", entry_price="65000", order_id=2)
        _protect_with_oco(broker, second, active=True)  # la segunda sigue abierta

        positions = broker.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0].quantity, 0.02)


if __name__ == "__main__":
    unittest.main()
