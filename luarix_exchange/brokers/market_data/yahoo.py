from datetime import datetime

import pandas as pd
import yfinance as yf
from luarix_exchange.models.market import Candle
from luarix_exchange.brokers.market_data.provider import DataFormat, MarketDataProvider


class YahooMarketDataProvider(MarketDataProvider):

    def __init__(self, client):
        self.client = client

    def get_last_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        market: str = "spot",
        format: DataFormat = DataFormat.OHLCV,
    ) -> list[Candle]:
        raise NotImplementedError("Not implemented...")

    def get_historical_candles(
        self,
        symbol: str,
        interval: str = "1m",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        format: DataFormat = DataFormat.OHLCV,
        market: str = "spot",
    ) -> list[Candle] | pd.DataFrame:
        start_str = start.strftime("%Y-%m-%d %H:%M:%S") if start else None
        end_str = end.strftime("%Y-%m-%d %H:%M:%S") if end else None
    
        df = yf.download(symbol, start=start_str, end=end_str, interval=interval)
        df.columns = df.columns.droplevel(1)
        df = df.reset_index()
        data = pd.DataFrame({
            "date": df["Date"],
            "close": df["Close"],
            "high": df["High"],
            "low": df["Low"],
            "open": df["Open"],
            "volume": df["Volume"],
        })
        if format is DataFormat.OHLCV:
            return data
        else:
            raise ValueError("Cant return list candles yet")
       
    def get_last_price(self, symbol: str, market: str = "spot") -> float:
        raise NotImplementedError("Not implemented...")

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> dict:
        # Yahoo no publica profundidad de mercado en absoluto: es una fuente
        # de velas, no un exchange.
        raise NotImplementedError("Not implemented...")








