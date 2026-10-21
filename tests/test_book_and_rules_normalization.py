# -*- coding: utf-8 -*-
"""`get_order_book` normalizado y `get_symbol_rules`, con las respuestas REALES
de cada API (capturadas de los endpoints públicos el 2026-10-16).

Incluye la regresión del libro de spot de Bitunix: se pedía `precision=2` y
la API respondía `success` con las dos listas vacías."""

import unittest
from decimal import Decimal as D
from unittest import mock

from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import Market

BINANCE_SPOT_DEPTH = {
    "lastUpdateId": 101557290174,
    "bids": [["81584.19000000", "4.10421000"], ["81584.18000000", "0.00055000"]],
    "asks": [["81584.20000000", "4.34822000"], ["81584.21000000", "0.00062000"]],
}
BINANCE_FUTURES_DEPTH = {
    "lastUpdateId": 11842621896285, "E": 1792209126152, "T": 1792209126149,
    "bids": [["81542.40", "2.224"], ["81542.30", "0.001"]],
    "asks": [["81542.50", "16.961"], ["81542.60", "0.012"]],
}
BINANCE_SPOT_SYMBOL = {
    "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True},
    ],
}
BINANCE_FUTURES_SYMBOL = {
    "symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
        {"tickSize": "0.10", "filterType": "PRICE_FILTER", "minPrice": "556.80", "maxPrice": "4529764"},
        {"stepSize": "0.001", "maxQty": "1000", "filterType": "LOT_SIZE", "minQty": "0.001"},
        {"filterType": "MIN_NOTIONAL", "notional": "50"},
    ],
}
BITUNIX_SPOT_PAIR = {
    "id": 1, "symbol": "btcusdt", "base": "BTC", "quote": "USDT", "basePrecision": 5, "quotePrecision": 2,
    "minPrice": "10", "minVolume": "0.00005", "minAmount": "0", "isOpen": 1,
    "precisions": ["0.01", "0.1", "1", "10", "100"],
}
BITUNIX_SPOT_DEPTH = {"asks": [{"price": "81584.69", "volume": "0.09196"}, {"price": "81584.20", "volume": "0.22763"}],
                      "bids": [{"price": "81584.10", "volume": "0.5"}], "ts": ""}
BITUNIX_FUTURES_DEPTH = {"asks": [["81542.5", "2.5259"], ["81542.6", "0.7088"]],
                         "bids": [["81542.4", "1.2614"], ["81542.3", "1.923"]]}
BITUNIX_FUTURES_PAIR = {"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT", "minTradeVolume": "0.0001",
                        "basePrecision": 4, "quotePrecision": 1, "symbolStatus": "OPEN"}


class _FakeBinanceClient:
    def get_order_book(self, symbol, limit):
        return BINANCE_SPOT_DEPTH

    def futures_order_book(self, symbol, limit):
        return BINANCE_FUTURES_DEPTH

    def get_exchange_info(self):
        return {"symbols": [BINANCE_SPOT_SYMBOL, dict(BINANCE_SPOT_SYMBOL, symbol="OLDUSDT", status="BREAK")]}

    def futures_exchange_info(self):
        return {"symbols": [BINANCE_FUTURES_SYMBOL]}


def _binance():
    from luarix_exchange.brokers.binance import BinanceBroker
    from luarix_exchange.brokers.market_data.binance import BinanceMarketDataProvider

    broker = BinanceBroker.__new__(BinanceBroker)
    broker.client = _FakeBinanceClient()
    broker.market_data_provider = BinanceMarketDataProvider(broker.client)
    return broker


class _FakeBitunixSpotClient:
    def __init__(self):
        self.precisiones_pedidas = []

    def get_trading_pairs(self):
        return {"code": "0", "data": [BITUNIX_SPOT_PAIR, dict(BITUNIX_SPOT_PAIR, symbol="deadusdt", isOpen=0)]}

    def get_depth_data(self, symbol, precision):
        self.precisiones_pedidas.append(precision)
        return {"code": "0", "data": BITUNIX_SPOT_DEPTH}


class _FakeBitunixFutures:
    def depth(self, symbol, limit=20):
        return BITUNIX_FUTURES_DEPTH

    def trading_pairs(self):
        return [BITUNIX_FUTURES_PAIR, dict(BITUNIX_FUTURES_PAIR, symbol="OFFUSDT", symbolStatus="CLOSE")]


def _bitunix():
    from luarix_exchange.brokers.bitunix import BitunixBroker
    from luarix_exchange.brokers.market_data.bitunix import BitunixMarketDataProvider

    broker = BitunixBroker.__new__(BitunixBroker)
    md = BitunixMarketDataProvider.__new__(BitunixMarketDataProvider)
    md.client, md.futures = _FakeBitunixSpotClient(), _FakeBitunixFutures()
    broker.market_data_provider = md
    return broker


class BinanceTests(unittest.TestCase):
    def test_libro_spot_normalizado(self):
        libro = _binance().get_order_book("BTCUSDT", 2)
        self.assertIsInstance(libro, OrderBook)
        self.assertEqual(libro.best_bid, (D("81584.19"), D("4.10421")))
        self.assertEqual(libro.best_ask, (D("81584.20"), D("4.34822")))
        self.assertEqual(libro.update_id, 101557290174)
        self.assertIs(libro.raw, BINANCE_SPOT_DEPTH)

    def test_libro_futuros_va_al_endpoint_de_futuros(self):
        libro = _binance().get_order_book("BTCUSDT", 2, market="futures")
        self.assertIs(libro.market, Market.FUTURES)
        self.assertEqual(libro.best_ask[0], D("81542.50"))

    def test_reglas_spot_y_futuros_son_distintas(self):
        broker = _binance()
        spot = broker.get_symbol_rule("BTCUSDT")
        fut = broker.get_symbol_rule("BTCUSDT", market="futures")
        self.assertEqual((spot.step_size, spot.tick_size, spot.min_qty, spot.min_notional),
                         (D("0.00001"), D("0.01"), D("0.00001"), D("5")))
        self.assertEqual((fut.step_size, fut.tick_size, fut.min_qty, fut.min_notional),
                         (D("0.001"), D("0.10"), D("0.001"), D("50")))
        self.assertIs(fut.market, Market.FUTURES)

    def test_solo_pares_en_trading(self):
        broker = _binance()
        self.assertEqual(broker.list_symbols(), ["BTCUSDT"])
        self.assertNotIn("OLDUSDT", broker.get_symbol_rules())


class BitunixTests(unittest.TestCase):
    def test_libro_spot_pide_la_precision_mas_fina_del_par(self):
        # Regresión: con `precision=2` la API devolvía el libro VACÍO.
        broker = _bitunix()
        libro = broker.get_order_book("BTCUSDT", 5)
        self.assertEqual(broker.market_data_provider.client.precisiones_pedidas, ["0.01"])
        self.assertEqual(libro.best_ask, (D("81584.20"), D("0.22763")))
        self.assertEqual(libro.best_bid, (D("81584.10"), D("0.5")))

    def test_libro_futuros_normalizado(self):
        libro = _bitunix().get_order_book("BTCUSDT", 2, market="futures")
        self.assertEqual(libro.best_bid, (D("81542.4"), D("1.2614")))
        self.assertEqual(libro.best_ask, (D("81542.5"), D("2.5259")))

    def test_un_par_de_spot_inexistente_levanta(self):
        with self.assertRaisesRegex(ValueError, "no es un par de spot"):
            _bitunix().get_order_book("NOPEUSDT")

    def test_list_symbols_en_mayusculas_y_solo_abiertos(self):
        broker = _bitunix()
        self.assertEqual(broker.list_symbols(), ["BTCUSDT"])
        self.assertEqual(broker.list_symbols("futures"), ["BTCUSDT"])

    def test_reglas_desde_la_precision_en_decimales(self):
        broker = _bitunix()
        spot = broker.get_symbol_rule("BTCUSDT")
        fut = broker.get_symbol_rule("BTCUSDT", market="futures")
        self.assertEqual((spot.step_size, spot.tick_size, spot.min_qty), (D("0.00001"), D("0.01"), D("0.00005")))
        self.assertEqual((fut.step_size, fut.tick_size, fut.min_qty), (D("0.0001"), D("0.1"), D("0.0001")))


class BitunixFuturesDepthLimitTests(unittest.TestCase):
    """La API solo acepta limit en 1, 5, 15, 50 o "max"."""

    def _limite_enviado(self, pedido):
        from luarix_exchange.brokers.bitunix_futures import BitunixFuturesClient

        respuesta = mock.Mock()
        respuesta.json.return_value = {"code": 0, "data": BITUNIX_FUTURES_DEPTH, "msg": "Success"}
        with mock.patch("luarix_exchange.brokers.bitunix_futures.requests.get", return_value=respuesta) as get:
            BitunixFuturesClient("k", "s").depth("BTCUSDT", pedido)
        return get.call_args.kwargs["params"]["limit"]

    def test_se_pide_el_menor_limite_permitido_que_cubre_lo_pedido(self):
        self.assertEqual([self._limite_enviado(n) for n in (1, 3, 15, 20, 50, 51)], [1, 5, 15, 50, 50, "max"])


class SinLibroTests(unittest.TestCase):
    def test_paper_levanta_en_vez_de_devolver_un_libro_vacio(self):
        from luarix_exchange.brokers.paper import PaperBroker

        with self.assertRaises(NotImplementedError):
            PaperBroker().get_order_book("BTCUSDT")


if __name__ == "__main__":
    unittest.main()
