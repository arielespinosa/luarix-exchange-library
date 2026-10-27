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


if __name__ == "__main__":
    unittest.main()
