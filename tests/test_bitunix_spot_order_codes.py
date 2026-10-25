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


if __name__ == "__main__":
    unittest.main()
