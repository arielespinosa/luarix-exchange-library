from datetime import datetime, timezone

import pandas as pd
import requests
from bitunix import BitunixClient

from luarix_exchange.brokers.bitunix_futures import BitunixFuturesClient
from luarix_exchange.brokers.bitunix_response import unwrap
from luarix_exchange.brokers.market_data.provider import DataFormat, MarketDataProvider
from luarix_exchange.models.market import Candle


def _parse_ts(val) -> datetime:
    """Convierte a datetime un timestamp de Bitunix, en cualquiera de sus tres
    formas: milisegundos como número, milisegundos como CADENA de dígitos, o
    ISO-8601.

    La segunda es la que falta detectar y la que rompía: el kline de futuros
    manda `{"time": "1785585600000"}` -- dígitos, pero de tipo `str`, así que
    caía en la rama ISO y reventaba con `month must be in 1..12`."""
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val / 1000, tz=timezone.utc)
    texto = str(val).strip()
    if texto.isdigit():
        return datetime.fromtimestamp(int(texto) / 1000, tz=timezone.utc)
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


class BitunixMarketDataProvider(MarketDataProvider):
    """Velas, precio y libro de Bitunix.

    ⚠ Asimetría importante entre sus dos klines, comprobada contra la API:

    | endpoint                          | `limit`  | `startTime` | `endTime`  |
    |-----------------------------------|----------|-------------|------------|
    | `/api/spot/v1/market/kline`       | ignorado | ignorado    | ignorado   |
    | `/api/v1/futures/market/kline`    | respeta  | ignorado    | respeta    |

    El de SPOT devuelve SIEMPRE las mismas ~201 velas más recientes, se le
    mande lo que se le mande. Por eso todo lo que necesite un rango o un
    número concreto de velas va por el de futuros, que además es público
    (sin firma) y no le afecta la whitelist de IP.
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.bitunix.com"):
        self.client = BitunixClient(api_key, api_secret)
        self.base_url = base_url
        self.session = requests.Session()
        # El kline de futuros vive en `fapi.bitunix.com` y es público. Se
        # reutiliza el cliente de futuros en vez de repetir la URL acá.
        self.futures = BitunixFuturesClient(api_key, api_secret)

    # Códigos de intervalo reales del kline de SPOT (ver el docstring de
    # `BitunixClient.get_kline_data`: "1,3,5,15,30,60,120,240,360,720,D,M,W").
    # NO son los mismos que acepta el kline de futuros, que usa los nombres
    # estándar ("4h", no "240") -- ver `BitunixFuturesClient.KLINE_INTERVALS`.
    INTERVAL_MAP = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
        "1d": "D", "1w": "W", "1M": "M",
    }

    # Cuántas velas pide cada llamada al kline de futuros. Bitunix no
    # documenta el máximo de forma estable; 200 es el tamaño que devuelve por
    # defecto y sirve como paso de paginación conservador.
    PAGE_SIZE = 200

    # Tope de páginas por llamada a `get_historical_candles`. Existe como
    # cortacircuitos, no como límite de negocio: si el endpoint dejara de
    # respetar `endTime` devolvería SIEMPRE la misma página y el bucle no
    # terminaría nunca. 500 × 200 = 100.000 velas, muy por encima de
    # cualquier rango razonable de backtest.
    MAX_PAGES = 500

    def get_last_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        market: str = "spot",
        format: DataFormat = DataFormat.OHLCV,
    ) -> list[Candle] | pd.DataFrame:
        """Las `limit` últimas velas, ascendentes por tiempo.

        Con `market="futures"` va al kline de futuros, que SÍ respeta
        `limit` -- se pide exactamente lo que se necesita en una llamada.

        Con `market="spot"` no hay más remedio que traer el bloque fijo de
        ~201 velas que devuelve ese endpoint y quedarse con las últimas
        `limit`. Pedir más de eso en spot es imposible: no es una limitación
        de este código.
        """
        if market == "futures":
            # Una más por si la última es la vela en curso -- ver
            # `_drop_unclosed`. El kline de futuros hoy no la incluye, pero
            # pedir una de sobra cuesta nada y deja el comportamiento igual
            # que el de spot y el de Binance.
            crudas = self.futures.kline(symbol, interval, limit=limit + 1)
        else:
            raw = self.client.get_kline_data(symbol, self.INTERVAL_MAP.get(interval, "60"))
            # `on_null=[]` conserva el comportamiento previo para "no hay
            # velas en ese rango" (respuesta correcta con `data: null`), pero
            # un RECHAZO ahora levanta en vez de devolver una lista vacía.
            # Antes las dos cosas eran indistinguibles: un símbolo mal
            # escrito, un intervalo no soportado o unas credenciales
            # caducadas daban exactamente el mismo "0 velas" que un rango sin
            # datos, y quien pedía cobertura se quedaba reintentando un hueco
            # que nunca se iba a llenar.
            crudas = unwrap(raw, "la consulta de velas", on_null=[]) or []

        candles = [c for c in (self._to_candle(symbol, interval, item)
                               for item in crudas if isinstance(item, dict)) if c]
        # Ascendente y recortando por el FINAL. Los dos klines devuelven de
        # la más reciente a la más antigua, así que el `klines[:limit]` que
        # había acá se quedaba con las `limit` MÁS ANTIGUAS del bloque y las
        # devolvía además en orden inverso -- o sea, ni eran las últimas ni
        # estaban ordenadas. Con `limit=1` eso daba la vela más VIEJA
        # disponible como si fuera la última.
        candles.sort(key=lambda c: c.open_time)
        candles = self._drop_unclosed(candles)
        candles = candles[-limit:] if limit else candles
        return candles if format == DataFormat.CANDLE else self._to_dataframe(candles)

    @staticmethod
    def _drop_unclosed(candles: list[Candle]) -> list[Candle]:
        """Quita la última vela si todavía no ha cerrado.

        El kline de SPOT de Bitunix incluye la vela EN FORMACIÓN, con
        valores parciales -- comprobado contra la API: pidiendo 1h a las
        15:5x devuelve la de las 15:00. Marcarla `is_closed=True` (que es lo
        que hace `_to_candle`, porque el endpoint no lo dice) la convierte en
        una vela cerrada mentirosa cuyo OHLC cambia entre dos consultas del
        mismo instante.

        La duración del intervalo se DEDUCE del espaciado entre velas en vez
        de sacarla de una tabla: los dos klines de Bitunix usan códigos
        distintos para el mismo intervalo ("240" vs "4h"), así que una tabla
        habría que mantenerla por duplicado y se desincronizaría.
        """
        if len(candles) < 2:
            return candles
        paso = candles[-1].open_time - candles[-2].open_time
        if paso.total_seconds() <= 0:
            return candles
        if candles[-1].open_time + paso > datetime.now(tz=timezone.utc):
            return candles[:-1]
        return candles

    def _to_candle(self, symbol: str, interval: str, item: dict) -> Candle | None:
        """Una vela, venga del kline de spot o del de futuros.

        Los dos endpoints devuelven formas DISTINTAS y hay que aceptar las dos:

        · spot   -> `{"ts": "2026-08-01T16:00:00Z", "open": "44.11", ...,
                      "volume": "137.149"}`  (timestamp ISO)
        · futuros-> `{"time": "1785585600000", "open": "44.31", ...,
                      "baseVol": "232675.4", "quoteVol": "5268.1"}`  (ms)

        Sin `time`/`baseVol` en esta lista, toda vela de futuros se descartaba
        en silencio (`ts_val` salía `None`) y el volumen quedaba en 0."""
        ts_val = item.get("t") or item.get("ts") or item.get("time")
        if ts_val is None:
            return None
        volumen = item.get("v")
        if volumen is None:
            volumen = item.get("volume", item.get("baseVol", 0))
        return Candle(
            symbol=symbol,
            timeframe=interval,
            open_time=_parse_ts(ts_val),
            open=float(item.get("o", item.get("open", 0))),
            high=float(item.get("h", item.get("high", 0))),
            low=float(item.get("l", item.get("low", 0))),
            close=float(item.get("c", item.get("close", 0))),
            volume=float(volumen or 0),
            close_time=_parse_ts(item.get("T") or item.get("ts") or ts_val),
            is_closed=True,
        )

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
        """Velas de `[start, end]`, paginando hacia atrás.

        Sin `start` cae al comportamiento de siempre (`get_last_candles`: la
        ventana más reciente), que es lo que quieren los llamadores en vivo.
        Con `start` pagina de verdad -- necesario para cualquier backtest,
        que pide meses o años y antes recibía en silencio las últimas 200
        velas.

        ⚠ Siempre contra el kline de FUTUROS, incluso con `market="spot"`:
        es la única fuente de Bitunix con histórico paginable. Son precios
        de perpetuo, que para el mismo par se mueven casi igual pero no son
        idénticos (base, funding). Si algún día el kline de spot admite
        rangos, este es el sitio donde volver.

        Lo que devuelve SIEMPRE está dentro de `[start, end]`: filtrar es lo
        que convierte un endpoint que quizá ignore el rango en un fallo
        honesto y diagnosticable ("faltan velas") en vez de un backtest
        corriendo sobre datos de otra época."""
        if start is None:
            return self.get_last_candles(symbol, interval, limit or self.PAGE_SIZE,
                                         market=market, format=format)

        if interval not in self.futures.KLINE_INTERVALS:
            raise ValueError(
                f"Bitunix no ofrece histórico paginable de {interval!r}. Intervalos "
                f"disponibles: {', '.join(sorted(self.futures.KLINE_INTERVALS))}."
            )

        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end = end or datetime.now(tz=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)

        por_tiempo: dict[datetime, Candle] = {}
        start_ms = int(start.timestamp() * 1000)
        cursor_end_ms = int(end.timestamp() * 1000)
        paginas = 0

        # Se pagina HACIA ATRÁS. El kline de futuros ancla en `endTime` y
        # devuelve las `limit` velas anteriores (más reciente primero), así que
        # cada vuelta pide "las 200 anteriores a la más antigua que ya tengo".
        # Verificado contra la API real: 6 páginas bastan para traer 1.200
        # velas de 4h sin un solo hueco.
        #
        # Antes se paginaba hacia ADELANTE moviendo `startTime`, y eso no podía
        # funcionar: `startTime` no lo respeta ningún kline de Bitunix. La
        # primera página ya devolvía la ventana más reciente, el cursor saltaba
        # a su último timestamp -- pasándose del final pedido -- y el bucle
        # terminaba con una sola página. De ahí "201 de 923 esperadas".
        while paginas < self.MAX_PAGES and cursor_end_ms >= start_ms:
            paginas += 1
            page = self.futures.kline(symbol, interval, limit=self.PAGE_SIZE, end_ms=cursor_end_ms)
            if not page:
                break

            ts_pagina: list[int] = []
            for item in page:
                if not isinstance(item, dict):
                    continue
                candle = self._to_candle(symbol, interval, item)
                if candle is None:
                    continue
                ts_pagina.append(int(candle.open_time.timestamp() * 1000))
                if start <= candle.open_time <= end and candle.open_time not in por_tiempo:
                    por_tiempo[candle.open_time] = candle

            if not ts_pagina:
                break

            siguiente = min(ts_pagina) - 1
            # Si el cursor no RETROCEDE, el endpoint está devolviendo siempre
            # lo mismo -- se corta en vez de girar en vacío hasta MAX_PAGES.
            if siguiente >= cursor_end_ms:
                break
            cursor_end_ms = siguiente

            if limit is not None and len(por_tiempo) >= limit:
                break

        # Orden ascendente por tiempo: el resto del sistema (indicadores,
        # los gráficos) lo da por hecho, y estas páginas llegan de la más
        # reciente a la más antigua.
        candles = [por_tiempo[t] for t in sorted(por_tiempo)]
        if limit is not None:
            candles = candles[:limit]
        return candles if format == DataFormat.CANDLE else self._to_dataframe(candles)

    @staticmethod
    def _to_dataframe(candles: list[Candle]) -> pd.DataFrame:
        return pd.DataFrame([{
            "date": c.open_time, "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume,
            "close_time": c.close_time,
        } for c in candles])

    def get_last_price(self, symbol: str, market: str = "spot") -> float:
        """El último precio negociado.

        En futuros se lee del propio kline (1 vela de 1 minuto): es público,
        así que funciona aunque la IP no esté en la whitelist -- que es
        precisamente cuando más falta hace poder mirar un precio.
        """
        if market == "futures":
            velas = self.futures.kline(symbol, "1m", limit=1)
            if not velas:
                raise ValueError(f"Bitunix no devolvió ninguna vela de futuros para {symbol}.")
            return float(velas[0].get("c") or velas[0].get("close") or 0)

        # Sin `on_null`: un precio no tiene valor neutro razonable. El `0.0`
        # que devolvía antes ante un rechazo era el peor resultado posible --
        # se propagaba a cálculos de cantidad y niveles como si fuera real.
        data = unwrap(self.client.get_latest_price(symbol), "la consulta del último precio")
        if isinstance(data, str):
            return float(data)
        return float(data.get("price", 0.0))

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> dict:
        if market == "futures":
            raise NotImplementedError(
                "El libro de órdenes de los futuros de Bitunix no está implementado -- "
                "no se ha verificado su endpoint contra la API real."
            )
        return unwrap(self.client.get_depth_data(symbol, 2), "la consulta del libro de órdenes", on_null={})
