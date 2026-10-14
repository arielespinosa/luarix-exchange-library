from datetime import datetime, timezone

import pandas as pd
from binance import Client, ThreadedWebsocketManager

from luarix_exchange.brokers.market_data.provider import DataFormat, MarketDataProvider
from luarix_exchange.models.market import Candle


class BinanceMarketDataProvider(MarketDataProvider):
    """Velas, precio y libro de Binance, en SPOT y en USDT-M Futures.

    Los dos mercados son endpoints distintos del mismo `Client`
    (`get_klines` vs `futures_klines`). Antes solo se llamaba a los de spot,
    con `market` sin existir: un grafo configurado en futuros calculaba sus
    indicadores sobre precios de contado sin que nada lo dijera.
    """

    def __init__(self, client: Client):
        self.client = client

    def get_last_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        market: str = "spot",
        format: DataFormat = DataFormat.OHLCV,
    ) -> list[Candle] | pd.DataFrame:
        # Se pide UNA MÁS de las que se quieren porque la última que devuelve
        # Binance suele ser la vela EN CURSO, y se descarta abajo. Sin esto,
        # `limit=5` devolvía 4 -- comprobado contra la API real.
        pedidas = limit + 1
        if market == "futures":
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=pedidas)
        else:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=pedidas)
        # Binance ya devuelve ascendente por tiempo, pero la última puede ser
        # la vela EN CURSO (sin cerrar). Se descarta comparando su cierre
        # contra el reloj: incluirla mete un OHLC parcial en los indicadores,
        # que es un error que solo se ve cuando los valores "cambian solos"
        # entre dos consultas del mismo instante.
        cerradas = self._drop_unclosed(klines)[-limit:] if limit else self._drop_unclosed(klines)
        return self._render(symbol, interval, cerradas, format)

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

        if market == "futures":
            klines = self.client.futures_historical_klines(symbol, interval, start_str, end_str)
        else:
            klines = self.client.get_historical_klines(symbol, interval, start_str, end_str)
        if limit is not None:
            klines = klines[:limit]
        return self._render(symbol, interval, klines, format)

    def get_last_price(self, symbol: str, market: str = "spot") -> float:
        if market == "futures":
            return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])
        return float(self.client.get_symbol_ticker(symbol=symbol)["price"])

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> dict:
        if market == "futures":
            return self.client.futures_order_book(symbol=symbol, limit=limit)
        return self.client.get_order_book(symbol=symbol, limit=limit)

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _drop_unclosed(klines: list) -> list:
        """Quita la última vela si todavía no ha cerrado.

        El kline de Binance incluye la vela en formación, con valores
        parciales. `item[6]` es su `close_time` en milisegundos: si está en
        el futuro, la vela sigue viva."""
        if not klines:
            return klines
        ahora_ms = datetime.now(timezone.utc).timestamp() * 1000
        return klines[:-1] if float(klines[-1][6]) > ahora_ms else klines

    def _render(self, symbol: str, interval: str, klines: list, format: DataFormat):
        if format == DataFormat.CANDLE:
            return [self._kline_to_candle(symbol, interval, item) for item in klines]
        return self._klines_to_ohvl_dataframe(klines)

    def _kline_to_candle(self, symbol: str, interval: str, item: list) -> Candle:
        return Candle(
            symbol=symbol,
            timeframe=interval,
            open_time=self._ms_to_datetime(item[0]),
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=float(item[5]),
            close_time=self._ms_to_datetime(item[6]),
            is_closed=True,
        )

    def _klines_to_ohvl_dataframe(self, klines: list) -> pd.DataFrame:
        columns = ["date", "open", "high", "low", "close", "volume", "close_time"]
        data = [(
            self._ms_to_datetime(item[0]),
            float(item[1]),
            float(item[2]),
            float(item[3]),
            float(item[4]),
            float(item[5]),
            self._ms_to_datetime(item[6])) for item in klines]
        return pd.DataFrame(data, columns=columns)


class WebSocketBinanceMarketDataProvider():

    def __init__(self, api_key, api_secret, symbol, interval="1m", testnet=False):
        self.symbol = symbol
        self.interval = interval
        self.running = False
        self.twm = ThreadedWebsocketManager(api_key=api_key, api_secret=api_secret, testnet=testnet)

    def start(self):
        self.running = True
        self.twm.start()
        self.twm.start_kline_socket(
            callback=self._on_message,
            symbol=self.symbol,
            interval=self.interval
        )
        """  self.twm.start_user_socket(
            callback=self._on_message
        ) """

    def _on_message(self, msg):
        if not self.running:
            return

        event = msg.get("e")

        if event == "kline":
            self.on_kline(msg)

        elif event == "executionReport":
            self.on_execution_report(msg)

        elif event == "outboundAccountPosition":
            self.on_account_update(msg)

        elif event == "balanceUpdate":
            self.on_balance_update(msg)


    def on_kline(self, msg):
        """Sobrescribir o registrar un callback externo."""
        pass

    def on_execution_report(self, msg):
        pass

    def on_account_update(self, msg):
        pass

    def on_balance_update(self, msg):
        pass

    def stop(self):
        if self.running:
            self.running = False
            self.twm.stop()

    def wait(self):
        self.twm.join()
