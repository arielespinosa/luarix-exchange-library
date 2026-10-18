# -*- coding: utf-8 -*-
"""Modelos nuevos de la Fase 3, en `Decimal`: OrderBook, SymbolRules,
CommissionRates, OrderUpdate."""

import unittest
from decimal import Decimal as D

from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.orders import OrderStatus, Side
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.models.updates import OrderUpdate


class OrderBookTests(unittest.TestCase):
    def test_ordena_cada_lado_del_mejor_al_peor_y_convierte_a_decimal(self):
        libro = OrderBook.from_levels(
            "BTCUSDT", "spot",
            bids=[("99.5", "1"), ("100", "2"), (99.9, 3)],
            asks=[("101", "1"), ("100.5", "2")],
        )
        self.assertEqual(libro.bids, [(D("100"), D("2")), (D("99.9"), D("3")), (D("99.5"), D("1"))])
        self.assertEqual(libro.asks, [(D("100.5"), D("2")), (D("101"), D("1"))])
        self.assertIs(libro.market, Market.SPOT)

    def test_un_float_no_arrastra_su_representacion_binaria(self):
        libro = OrderBook.from_levels("X", "spot", bids=[(0.1, 0.3)], asks=[])
        self.assertEqual(libro.bids[0], (D("0.1"), D("0.3")))

    def test_recorta_a_depth_y_descarta_niveles_con_cantidad_cero(self):
        libro = OrderBook.from_levels(
            "X", "futures", bids=[(10, 1), (9, 0), (8, 1), (7, 1)], asks=[(11, 1), (12, 1), (13, 1)], depth=2,
        )
        self.assertEqual([p for p, _ in libro.bids], [D(10), D(8)])
        self.assertEqual([p for p, _ in libro.asks], [D(11), D(12)])

    def test_mejores_niveles_spread_y_medio(self):
        libro = OrderBook.from_levels("X", "spot", bids=[(100, 1)], asks=[(102, 1)])
        self.assertEqual((libro.best_bid[0], libro.best_ask[0]), (D(100), D(102)))
        self.assertEqual((libro.spread, libro.mid), (D(2), D(101)))

    def test_sin_un_lado_no_hay_spread(self):
        libro = OrderBook.from_levels("X", "spot", bids=[], asks=[(102, 1)])
        self.assertIsNone(libro.spread)
        self.assertIsNone(libro.best_bid)

    def test_same_top_solo_mira_los_mejores_niveles(self):
        a = OrderBook.from_levels("X", "spot", bids=[(100, 1), (90, 1)], asks=[(101, 1)])
        b = OrderBook.from_levels("X", "spot", bids=[(100, 1), (80, 5)], asks=[(101, 1)])
        self.assertTrue(a.same_top(b, levels=1))
        self.assertFalse(a.same_top(b, levels=2))


class SymbolRulesTests(unittest.TestCase):
    REGLAS = SymbolRules(symbol="BTCUSDT", base="BTC", quote="USDT", step_size=D("0.00001"),
                         tick_size=D("0.01"), min_qty=D("0.00001"), min_notional=D("5"))

    def test_la_cantidad_se_redondea_hacia_abajo(self):
        # Hacia arriba podría pasarse del saldo.
        self.assertEqual(self.REGLAS.floor_quantity("0.123456789"), D("0.12345"))

    def test_el_precio_va_al_tick_mas_cercano(self):
        self.assertEqual(self.REGLAS.round_price("81584.196"), D("81584.20"))

    def test_una_orden_valida_no_tiene_motivos(self):
        self.assertEqual(self.REGLAS.violations("0.001", "60000"), [])

    def test_explica_cada_motivo_de_rechazo(self):
        motivos = self.REGLAS.violations("0.0000015", "60000.005")
        self.assertTrue(any("mínimo" in m and "BTC" in m for m in motivos))
        self.assertTrue(any("múltiplo de 0.00001" in m for m in motivos))
        self.assertTrue(any("múltiplo de 0.01" in m for m in motivos))
        self.assertTrue(any("importe" in m for m in motivos))


class CommissionAndUpdateTests(unittest.TestCase):
    def test_comision_efectiva_con_y_sin_descuento(self):
        self.assertEqual(CommissionRates("X", D("0.001"), D("0.001")).effective_taker, D("0.001"))
        self.assertEqual(CommissionRates("X", D("0.001"), D("0.001"), D("0.75")).effective_taker, D("0.00075"))

    def test_order_update_distingue_llenados_de_otros_eventos(self):
        base = dict(exchange="binance", market=Market.SPOT, symbol="BTCUSDT", order_id="1",
                    side=Side.BUY, filled_qty=D("0.5"), event_time_ms=1)
        self.assertTrue(OrderUpdate(status=OrderStatus.PARTIALLY_FILLED, last_fill_qty=D("0.5"), **base).is_fill)
        self.assertFalse(OrderUpdate(status=OrderStatus.CANCELED, **base).is_fill)


if __name__ == "__main__":
    unittest.main()
