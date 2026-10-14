# -*- coding: utf-8 -*-
"""El candado de dinero real de Binance y Bitunix.

Regla: una escritura que toca dinero real (colocar o cancelar en un exchange
sin testnet, o en mainnet) exige `trading_mode="live"` Y
`allow_live_trading=True`. Antes, Bitunix con el modo por defecto (`"paper"`)
colocaba igual en el mercado.

Los brokers se construyen con `object.__new__` y clientes falsos: ninguna
prueba abre red, y si el candado falla, el cliente falso lo delata.
"""
import unittest
from unittest.mock import MagicMock

from luarix_exchange.brokers.binance import BinanceBroker
from luarix_exchange.brokers.bitunix import BitunixBroker
from luarix_exchange.models.orders import OrderRequest
from luarix_exchange.models.orders import Side


def _bitunix(trading_mode: str, allow_live_trading: bool) -> BitunixBroker:
    broker = object.__new__(BitunixBroker)
    broker.trading_mode = trading_mode
    broker.allow_live_trading = allow_live_trading
    broker.client = MagicMock()
    broker.client.place_order.return_value = {"code": 0, "data": {"orderId": "42"}}
    broker.account_data_provider = MagicMock()
    return broker


def _binance(*, testnet: bool, test_order: bool, trading_mode: str, allow_live_trading: bool) -> BinanceBroker:
    broker = object.__new__(BinanceBroker)
    broker.testnet = testnet
    broker.test_order = test_order
    broker.trading_mode = trading_mode
    broker.allow_live_trading = allow_live_trading
    broker.client = MagicMock()
    broker._filters_cache = {}
    return broker


def _order(market: str = "spot") -> OrderRequest:
    return OrderRequest(symbol="BTCUSDT", side=Side.BUY, quantity=0.001, market=market)


class BitunixGuardTests(unittest.TestCase):
    def test_default_paper_mode_never_reaches_the_exchange(self):
        broker = _bitunix("paper", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker.place_order(_order())
        broker.client.place_order.assert_not_called()

    def test_testnet_mode_is_real_money_too_because_bitunix_has_no_testnet(self):
        broker = _bitunix("testnet", allow_live_trading=True)
        with self.assertRaises(RuntimeError):
            broker.place_order(_order())
        broker.client.place_order.assert_not_called()

    def test_live_without_permission_is_blocked(self):
        broker = _bitunix("live", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker.place_order(_order())
        broker.client.place_order.assert_not_called()

    def test_live_with_permission_places(self):
        broker = _bitunix("live", allow_live_trading=True)
        ack, _fills = broker.place_order(_order())
        broker.client.place_order.assert_called_once()
        self.assertEqual(ack.exchange_order_id, "42")

    def test_cancel_is_guarded_like_placing(self):
        broker = _bitunix("paper", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker.cancel_order("BTCUSDT", "42")
        broker.client.cancel_orders.assert_not_called()
        broker.account_data_provider.futures.cancel_orders.assert_not_called()

    def test_bulk_cancel_is_guarded_like_placing(self):
        # Cancel All Orders usa la cancelación masiva; antes no tenía candado.
        broker = _bitunix("paper", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker.cancel_open_orders("futures", "BTCUSDT")
        broker.account_data_provider.cancel_open_orders.assert_not_called()

    def test_bulk_cancel_live_with_permission_cancels(self):
        broker = _bitunix("live", allow_live_trading=True)
        broker.account_data_provider.cancel_open_orders.return_value = ["7"]
        self.assertEqual(broker.cancel_open_orders("futures", "BTCUSDT"), ["7"])
        broker.account_data_provider.cancel_open_orders.assert_called_once_with("futures", "BTCUSDT")


class BinanceGuardTests(unittest.TestCase):
    def test_mainnet_real_spot_order_needs_live_and_permission(self):
        broker = _binance(testnet=False, test_order=False, trading_mode="paper", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker._validate_pre_trade(_order())

    def test_mainnet_futures_order_is_real_even_with_test_order(self):
        broker = _binance(testnet=False, test_order=True, trading_mode="paper", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker._validate_pre_trade(_order("futures"))

    def test_mainnet_spot_test_order_is_allowed_outside_live(self):
        broker = _binance(testnet=False, test_order=True, trading_mode="paper", allow_live_trading=False)
        broker._validate_pre_trade(_order())  # create_test_order: nunca se ejecuta

    def test_testnet_orders_are_allowed_outside_live(self):
        broker = _binance(testnet=True, test_order=False, trading_mode="testnet", allow_live_trading=False)
        broker._validate_pre_trade(_order("futures"))

    def test_mainnet_live_with_permission_is_allowed(self):
        broker = _binance(testnet=False, test_order=False, trading_mode="live", allow_live_trading=True)
        broker._validate_pre_trade(_order("futures"))

    def test_mainnet_cancel_needs_live_and_permission(self):
        broker = _binance(testnet=False, test_order=True, trading_mode="paper", allow_live_trading=False)
        with self.assertRaises(RuntimeError):
            broker.cancel_order("BTCUSDT", "42")
        broker.client.cancel_order.assert_not_called()

    def test_mainnet_bulk_cancel_needs_live_and_permission(self):
        broker = _binance(testnet=False, test_order=True, trading_mode="paper", allow_live_trading=False)
        broker.account_data_provider = MagicMock()
        with self.assertRaises(RuntimeError):
            broker.cancel_open_orders("spot", "BTCUSDT")
        broker.account_data_provider.cancel_open_orders.assert_not_called()

    def test_testnet_cancel_is_allowed(self):
        broker = _binance(testnet=True, test_order=False, trading_mode="testnet", allow_live_trading=False)
        broker.client.cancel_order.return_value = {"orderId": 42, "status": "CANCELED", "side": "BUY"}
        broker.cancel_order("BTCUSDT", "42")
        broker.client.cancel_order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
