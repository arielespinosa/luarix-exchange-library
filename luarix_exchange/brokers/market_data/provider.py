from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum

import pandas as pd

from luarix_exchange.models.market import Candle


class DataFormat(Enum):
    """En qué forma se quieren las velas.

    OHLCV:  `pd.DataFrame` con columnas `date, open, high, low, close,
            volume, close_time`. Lo que consumen los indicadores.
    CANDLE: `list[Candle]`. Lo que consume el motor de ejecución.
    """
    OHLCV = "ohlcv"
    CANDLE = "candle"


class MarketDataProvider(ABC):
    """Datos de MERCADO: velas, último precio y libro de órdenes.

    Público en los dos exchanges -- no le afecta la whitelist de IP de la
    API key, a diferencia de todo lo de `AccountDataProvider`.

    Todos los métodos aceptan `market="spot"|"futures"`. Para el mismo par
    son series de precios DISTINTAS (base y funding las separan), así que
    pedir velas de spot para operar el perpetuo no es una aproximación
    inofensiva: cambia los niveles que calcula una estrategia.
    """

    def _ms_to_datetime(self, value: int) -> datetime:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)

    def get_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        market: str = "spot",
    ) -> list[Candle]:
        """Las `limit` últimas velas cerradas, SIEMPRE como `list[Candle]`.

        Fija el formato a propósito: `get_last_candles` tiene defaults
        distintos según el proveedor (Binance devolvía un DataFrame,
        Bitunix una lista), así que esta firma prometía `list[Candle]` y
        devolvía otra cosa la mitad de las veces.
        """
        return self.get_last_candles(symbol, interval, limit, market=market, format=DataFormat.CANDLE)

    @abstractmethod
    def get_last_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        market: str = "spot",
        format: DataFormat = DataFormat.OHLCV,
    ) -> list[Candle] | pd.DataFrame:
        """Las `limit` últimas velas cerradas, de la MÁS ANTIGUA a la más
        reciente.

        El orden ascendente es parte del contrato: los indicadores lo dan
        por hecho y los dos exchanges devuelven sus klines al revés.
        """
        pass

    @abstractmethod
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
        """Velas de `[start, end]`, paginando contra el exchange si hace
        falta. Sin `start` equivale a `get_last_candles`."""
        pass

    @abstractmethod
    def get_last_price(self, symbol: str, market: str = "spot") -> float:
        pass

    @abstractmethod
    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> dict:
        pass

    def async_get_candles(self, symbol: str, interval: str = "1m", limit: int = 100) -> list[Candle]:
        """Reservado para el camino asíncrono. Concreto y levantando: era
        `@abstractmethod` y NINGUNA implementación lo cumplía -- las tres
        (Binance, Bitunix, Yahoo) lo tenían como `pass`, o sea que devolvían
        `None` en silencio. Obligar a escribir un stub vacío no aporta nada;
        fallar cuando alguien lo llame, sí."""
        raise NotImplementedError(f"{type(self).__name__} no tiene camino asíncrono de velas.")
