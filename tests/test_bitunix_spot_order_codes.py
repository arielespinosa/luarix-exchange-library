# -*- coding: utf-8 -*-
"""Los códigos enteros que el spot de Bitunix espera en `place_order`.

side 1 = SELL, 2 = BUY; type 1 = LIMIT, 2 = MARKET. Estuvieron invertidos
los dos: las compras a mercado del motor salían como ventas LIMIT. Los
brokers se construyen con `object.__new__` y un cliente falso: ninguna
prueba abre red.
"""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock

from luarix_exchange.brokers.bitunix import BitunixBroker
from luarix_exchange.brokers.bitunix_response import BitunixApiError
from luarix_exchange.brokers.bitunix_spot_client import QuietBitunixClient
from luarix_exchange.models.orders import OrderRequest, OrderType, Side


def _broker() -> BitunixBroker:
    broker = object.__new__(BitunixBroker)
    broker.trading_mode = "live"
    broker.allow_live_trading = True
    broker.client = MagicMock()
    broker.client.place_order.return_value = {"code": 0, "data": {"orderId": "42"}}
    broker.account_data_provider = MagicMock()
    return broker


def _sent(broker: BitunixBroker) -> dict:
    return broker.client.place_order.call_args.kwargs


class SpotOrderCodesTests(unittest.TestCase):
    def test_market_buy_is_side_2_type_2(self):
        broker = _broker()
        broker.place_order(OrderRequest(symbol="LTCUSDT", side=Side.BUY, quantity=0.25,
                                        order_type=OrderType.MARKET, price=59.04))
        self.assertEqual(_sent(broker)["side"], 2)
        self.assertEqual(_sent(broker)["order_type"], 2)

    def test_market_sell_is_side_1_type_2(self):
        broker = _broker()
        broker.place_order(OrderRequest(symbol="LTCUSDT", side=Side.SELL, quantity=0.25,
                                        order_type=OrderType.MARKET, price=58.79))
        self.assertEqual(_sent(broker)["side"], 1)
        self.assertEqual(_sent(broker)["order_type"], 2)

    def test_limit_buy_is_type_1_with_its_price(self):
        broker = _broker()
        broker.place_order(OrderRequest(symbol="LTCUSDT", side=Side.BUY, quantity=0.25,
                                        order_type=OrderType.LIMIT, price=57.1))
        self.assertEqual(_sent(broker)["side"], 2)
        self.assertEqual(_sent(broker)["order_type"], 1)
        self.assertEqual(_sent(broker)["price"], "57.1")

    def test_order_types_spot_lacks_are_rejected_not_sent(self):
        broker = _broker()
        with self.assertRaises(BitunixApiError):
            broker.place_order(OrderRequest(symbol="LTCUSDT", side=Side.SELL, quantity=0.25,
                                            order_type=OrderType.STOP_LOSS_LIMIT, price=58.0,
                                            stop_price=58.5))
        broker.client.place_order.assert_not_called()


class QuietClientTests(unittest.TestCase):
    def test_signed_post_prints_nothing(self):
        client = QuietBitunixClient("my-api-key", "my-secret")
        client.session = MagicMock()
        client.session.post.return_value.json.return_value = {"code": "0", "data": {}}
        out = io.StringIO()
        with redirect_stdout(out):
            client.place_order(side=2, order_type=2, volume="0.25", symbol="LTCUSDT")
        self.assertEqual(out.getvalue(), "")
        headers = client.session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["api-key"], "my-api-key")
        self.assertIn("sign", headers)



class FuturesOrderBodyTests(unittest.TestCase):
    """El cuerpo de una orden de futuros: el mismo formato que usan los bots
    del usuario contra esta API (`effect` siempre, `price` solo en LIMIT),
    con cantidad y precios ajustados al step/tick del par."""

    def _client(self):
        from luarix_exchange.brokers.bitunix_futures import BitunixFuturesClient

        client = BitunixFuturesClient("k", "s")
        client._position_mode = "HEDGE"
        client.post = MagicMock(return_value={"orderId": "42"})
        return client

    def test_market_sends_effect_and_no_price(self):
        client = self._client()
        client.place_order(symbol="LTCUSDT", side="SELL", quantity=0.18, price=57.08, order_type="MARKET",
                           tp_price=56.79, sl_price=57.37)
        body = client.post.call_args.args[1]
        self.assertEqual(body["effect"], "GTC")
        self.assertNotIn("price", body)
        self.assertEqual((body["tpPrice"], body["slPrice"], body["tradeSide"]), ("56.79", "57.37", "OPEN"))

    def test_limit_keeps_its_price(self):
        client = self._client()
        client.place_order(symbol="LTCUSDT", side="BUY", quantity=0.18, price=56.5, order_type="LIMIT")
        self.assertEqual(client.post.call_args.args[1]["price"], "56.5")

    def test_broker_fits_quantity_and_prices_to_the_pair(self):
        from decimal import Decimal

        from luarix_exchange.models.rules import SymbolRules

        broker = _broker()
        broker._futures_rules_cache = {"LTCUSDT": SymbolRules(
            symbol="LTCUSDT", base="LTC", quote="USDT", step_size=Decimal("0.001"),
            tick_size=Decimal("0.01"), min_qty=Decimal("0.053"), min_notional=Decimal("0"),
        )}
        broker.account_data_provider.futures.place_order.return_value = {"orderId": "7"}
        broker.place_order(OrderRequest(
            symbol="LTCUSDT", side=Side.SELL, quantity=0.17519, order_type=OrderType.MARKET,
            price=57.08, market="futures", tp_price=56.7946, sl_price=57.833800000000004,
        ))
        sent = broker.account_data_provider.futures.place_order.call_args.kwargs
        self.assertEqual((sent["quantity"], sent["tp_price"], sent["sl_price"]), (0.175, 56.79, 57.83))

    def test_the_ack_reports_what_was_sent_not_what_was_asked(self):
        from decimal import Decimal

        from luarix_exchange.models.rules import SymbolRules

        broker = _broker()
        broker._futures_rules_cache = {"LTCUSDT": SymbolRules(
            symbol="LTCUSDT", base="LTC", quote="USDT", step_size=Decimal("0.001"),
            tick_size=Decimal("0.01"), min_qty=Decimal("0.053"), min_notional=Decimal("0"),
        )}
        broker.account_data_provider.futures.place_order.return_value = {"orderId": "7"}
        ack, _ = broker.place_order(OrderRequest(
            symbol="LTCUSDT", side=Side.SELL, quantity=0.26, order_type=OrderType.LIMIT, price=57.41,
            market="futures", tp_price=57.122949999999996, sl_price=57.69704999999999,
        ))
        self.assertEqual((ack.tp_price, ack.sl_price, ack.price, ack.quantity), (57.12, 57.70, 57.41, 0.26))


class FillPriceTests(unittest.TestCase):
    """El precio REAL de una orden llenada: la media ponderada de sus
    ejecuciones, no el precio pedido ni el cierre de la vela."""

    def _provider(self):
        from luarix_exchange.brokers.account_data.bitunix import BitunixAccountDataProvider

        return BitunixAccountDataProvider(MagicMock(), futures=MagicMock())

    def test_a_spot_market_buy_averages_its_fills_and_prices_the_fee_in_usdt(self):
        provider = self._provider()
        provider.client.query_matching_orders.return_value = {"code": 0, "data": [
            {"price": "57.40", "volume": "0.06", "fee": "0.00006", "feeCoin": "LTC"},
            {"price": "57.43", "volume": "0.20", "fee": "0.0002", "feeCoin": "LTC"},
        ]}
        raw = provider.get_order("LTCUSDT", "1", "spot")
        self.assertAlmostEqual(raw["avgPrice"], (57.40 * 0.06 + 57.43 * 0.20) / 0.26)
        self.assertAlmostEqual(raw["fee"], 0.00006 * 57.40 + 0.0002 * 57.43)

    def test_a_spot_sell_fee_already_in_usdt_is_kept(self):
        provider = self._provider()
        provider.client.query_matching_orders.return_value = {"code": 0, "data": [
            {"price": "57.36", "volume": "0.26", "fee": "0.01043952", "feeCoin": "USDT"},
        ]}
        self.assertAlmostEqual(self._provider_fee(provider), 0.01043952)

    def _provider_fee(self, provider):
        return provider.get_order("LTCUSDT", "1", "spot")["fee"]

    def test_a_filled_futures_order_without_avg_price_reads_its_trades(self):
        provider = self._provider()
        provider.futures.order_detail.return_value = {"status": "FILLED", "tradeQty": "0.26", "avgPrice": None}
        provider.futures.order_trades.return_value = [{"price": "57.41", "qty": "0.26"}]
        from luarix_exchange.models.orders import OrderInfo

        info = OrderInfo.from_bitunix(provider.get_order("LTCUSDT", "9", "futures"), "futures")
        self.assertAlmostEqual(info.average_price, 57.41)

    def test_closed_orders_expose_the_exchange_triggered_exit(self):
        broker = _broker()
        broker.account_data_provider.futures.history_orders.return_value = [{
            "orderId": "4350947096047929693", "symbol": "LTCUSDT", "qty": "0.26", "tradeQty": "0.26",
            "price": "MARKET", "avgPrice": "57.72", "side": "BUY", "orderType": "MARKET", "reduceOnly": True,
            "status": "FILLED", "fee": "0.0037518", "ctime": "1793158360517",
        }]
        [closing] = broker.get_closed_orders("LTCUSDT", "futures")
        self.assertEqual((closing.average_price, closing.reduce_only, closing.side), (57.72, True, Side.BUY))
        self.assertIsNone(closing.price)


if __name__ == "__main__":
    unittest.main()
