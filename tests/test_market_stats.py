# -*- coding: utf-8 -*-
"""Estadísticas de 24h, funding y fuentes de mercado, con las respuestas
REALES de cada API (capturadas el 2026-10-21) y sin red.

Lo que fija este fichero: que los cuatro exchanges y las dos fuentes se
traduzcan al MISMO vocabulario (`Ticker24h`, `FundingRate`, `GlobalMarket`,
`FearGreed`). Las unidades cambian de un exchange a otro -- Bitunix da el
funding en porcentaje y Binance en fracción, Bitfinex el cambio relativo y
Binance en puntos -- y es exactamente ahí donde un resumen de mercado
mentiría sin que nada falle.
"""

import unittest
from decimal import Decimal

from luarix_exchange.brokers.binance import BinanceBroker, funding_from_binance, ticker_from_binance
from luarix_exchange.brokers.bitunix import BitunixBroker, funding_from_bitunix, ticker_from_bitunix
from luarix_exchange.capabilities import Capability
from luarix_exchange.exchanges.bitfinex import ticker_from_bitfinex
from luarix_exchange.exchanges.kraken import KrakenExchange, ticker_from_kraken
from luarix_exchange.factory import ExchangeFactory
from luarix_exchange.models.brokers import Market
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.sources.alternative_me import fear_greed_from_alternative_me
from luarix_exchange.sources.coingecko import asset_from_coingecko, global_from_coingecko


def rules(symbol: str, base: str, quote: str, market: Market = Market.SPOT, raw=None) -> SymbolRules:
    return SymbolRules(
        symbol=symbol, base=base, quote=quote, step_size=Decimal("0.001"), tick_size=Decimal("0.01"),
        min_qty=Decimal("0"), min_notional=Decimal("0"), market=market, raw=raw,
    )


BTC = rules("BTCUSDT", "BTC", "USDT")


class TickerParsingTests(unittest.TestCase):
    def test_binance_keeps_its_own_change_and_quote_volume(self):
        ticker = ticker_from_binance({
            "symbol": "BTCUSDT", "priceChangePercent": "2.314", "lastPrice": "84524.01", "openPrice": "82612.22",
            "highPrice": "84898.00", "lowPrice": "82300.00", "volume": "16000.5", "quoteVolume": "1345920000.93",
            "closeTime": 1792613897002,
        }, BTC, "spot")
        self.assertEqual((ticker.base, ticker.quote, ticker.market), ("BTC", "USDT", "spot"))
        self.assertAlmostEqual(ticker.change_pct, 2.314)
        self.assertAlmostEqual(ticker.volume_quote, 1345920000.93)
        self.assertEqual(ticker.time.year, 2026)

    def test_bitunix_has_no_change_percent_so_it_is_computed(self):
        ticker = ticker_from_bitunix({
            "symbol": "BTCUSDT", "markPrice": "84489.3", "lastPrice": "84489.3", "open": "82590.2",
            "last": "84477.1", "quoteVol": "2340878100.9", "baseVol": "27928.3", "high": "84871.9", "low": "82542.1",
        }, rules("BTCUSDT", "BTC", "USDT", Market.FUTURES))
        self.assertEqual(ticker.market, "futures")
        self.assertAlmostEqual(ticker.change_pct, (84477.1 - 82590.2) / 82590.2 * 100)

    def test_kraken_uses_the_24h_half_and_estimates_quote_volume_with_vwap(self):
        ticker = ticker_from_kraken({
            "c": ["84470.5", "0.1"], "v": ["900.0", "2000.0"], "p": ["84000.0", "84100.0"],
            "l": ["83000.0", "82500.0"], "h": ["85000.0", "85100.0"], "o": "82930.0",
        }, rules("BTC/USD", "BTC", "USD"))
        self.assertEqual((ticker.high, ticker.low), (85100.0, 82500.0))
        self.assertAlmostEqual(ticker.volume_quote, 2000.0 * 84100.0)

    def test_bitfinex_relative_change_is_turned_into_points(self):
        ticker = ticker_from_bitfinex(
            ["tBTCUSD", 84575, 2.3, 84584, 2.5, 1765, 0.02131514, 84570, 438.98, 84930, 82615],
            rules("tBTCUSD", "BTC", "USD"),
        )
        self.assertAlmostEqual(ticker.change_pct, 2.131514)
        self.assertAlmostEqual(ticker.open, 84570 - 1765)
        self.assertAlmostEqual(ticker.volume_quote, 438.98 * 84570)


class FundingParsingTests(unittest.TestCase):
    def test_binance_rate_is_already_a_fraction(self):
        funding = funding_from_binance(
            {"symbol": "BTCUSDT", "lastFundingRate": "0.00000587", "markPrice": "84489.2", "nextFundingTime": 1792627200000},
            BTC,
        )
        self.assertAlmostEqual(funding.rate, 0.00000587)
        self.assertEqual(funding.next_funding_time.hour, 0)

    def test_bitunix_rate_comes_in_percent(self):
        funding = funding_from_bitunix(
            {"symbol": "BTCUSDT", "markPrice": "84489.3", "fundingRate": "-0.000606", "fundingInterval": 8,
             "nextFundingTime": "1792627200000"},
            BTC,
        )
        self.assertAlmostEqual(funding.rate, -0.00000606)
        self.assertEqual(funding.interval_hours, 8.0)


class _FakeBinanceClient:
    def get_ticker(self):
        return [
            {"symbol": "BTCUSDT", "priceChangePercent": "1", "lastPrice": "2", "openPrice": "1"},
            {"symbol": "DELISTEDUSDT", "priceChangePercent": "1", "lastPrice": "2", "openPrice": "1"},
        ]

    def futures_ticker(self):
        return [
            {"symbol": "BTCUSDT", "priceChangePercent": "1", "lastPrice": "2", "openPrice": "1"},
            {"symbol": "BTCUSDT_270326", "priceChangePercent": "1", "lastPrice": "2", "openPrice": "1"},
        ]

    def futures_mark_price(self):
        return [
            {"symbol": "BTCUSDT", "lastFundingRate": "0.0001", "markPrice": "1"},
            {"symbol": "BTCUSDT_270326", "lastFundingRate": "", "markPrice": "1"},
        ]


class BinanceStatsTests(unittest.TestCase):
    def setUp(self):
        self.broker = object.__new__(BinanceBroker)
        self.broker.client = _FakeBinanceClient()
        self.broker._rules_cache = {
            Market.SPOT: {"BTCUSDT": BTC},
            Market.FUTURES: {
                "BTCUSDT": rules("BTCUSDT", "BTC", "USDT", Market.FUTURES, {"contractType": "PERPETUAL"}),
                "BTCUSDT_270326": rules("BTCUSDT_270326", "BTC", "USDT", Market.FUTURES, {"contractType": "CURRENT_QUARTER"}),
            },
        }

    def test_only_trading_pairs_get_stats(self):
        self.assertEqual([t.symbol for t in self.broker.get_tickers_24h("spot")], ["BTCUSDT"])

    def test_futures_stats_and_funding_are_perpetuals_only(self):
        self.assertEqual([t.symbol for t in self.broker.get_tickers_24h("futures")], ["BTCUSDT"])
        self.assertEqual([f.symbol for f in self.broker.get_funding_rates()], ["BTCUSDT"])


class CapabilityMarketTests(unittest.TestCase):
    def test_every_exchange_with_ticker_stats_is_found(self):
        self.assertEqual(
            set(ExchangeFactory.find(Capability.TICKER_STATS)), {"binance", "bitunix", "kraken", "bitfinex"}
        )
        self.assertEqual(set(ExchangeFactory.find(Capability.FUNDING_RATES)), {"binance", "bitunix"})

    def test_a_capability_can_be_limited_to_some_markets(self):
        self.assertFalse(BitunixBroker.supports(Capability.TICKER_STATS, "spot"))
        self.assertTrue(BitunixBroker.supports(Capability.TICKER_STATS, "futures"))
        self.assertEqual(BitunixBroker.markets_for(Capability.TICKER_STATS), frozenset({Market.FUTURES}))
        self.assertEqual(BinanceBroker.markets_for(Capability.TICKER_STATS), frozenset({Market.SPOT, Market.FUTURES}))
        self.assertEqual(KrakenExchange.markets_for(Capability.FUNDING_RATES), frozenset())

    def test_require_names_the_missing_market(self):
        broker = object.__new__(BitunixBroker)
        with self.assertRaisesRegex(NotImplementedError, "ticker_stats en spot"):
            broker.require(Capability.TICKER_STATS, "spot")


class MarketSourceParsingTests(unittest.TestCase):
    def test_coingecko_global(self):
        market = global_from_coingecko({"data": {
            "active_cryptocurrencies": 22417, "total_market_cap": {"usd": 2.84e12}, "total_volume": {"usd": 9.1e10},
            "market_cap_percentage": {"btc": 59.6, "eth": 10.7}, "market_cap_change_percentage_24h_usd": -0.54,
            "updated_at": 1792613897,
        }})
        self.assertEqual(market.dominance, {"BTC": 59.6, "ETH": 10.7})
        self.assertAlmostEqual(market.total_market_cap, 2.84e12)
        self.assertAlmostEqual(market.market_cap_change_pct_24h, -0.54)

    def test_coingecko_asset_with_sparkline(self):
        asset = asset_from_coingecko({
            "id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "current_price": 84476, "market_cap": 1.69e12,
            "market_cap_rank": 1, "total_volume": 3.1e10, "price_change_percentage_24h": 2.33,
            "price_change_percentage_7d_in_currency": 3.48, "sparkline_in_7d": {"price": [1.0, None, 2.0]},
        })
        self.assertEqual((asset.symbol, asset.rank), ("BTC", 1))
        self.assertEqual(asset.change_pct_24h, 2.33)
        self.assertEqual(asset.sparkline_7d, [1.0, 2.0])

    def test_fear_greed(self):
        reading = fear_greed_from_alternative_me({"value": "63", "value_classification": "Greed", "timestamp": "1792540800"})
        self.assertEqual((reading.value, reading.classification), (63, "Greed"))


if __name__ == "__main__":
    unittest.main()
