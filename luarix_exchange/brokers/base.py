from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

from luarix_exchange.brokers.account_data.base import AccountDataProvider
from luarix_exchange.brokers.market_data.provider import DataFormat, MarketDataProvider
from luarix_exchange.models.brokers import BrokerPosition
from luarix_exchange.models.market import Candle
from luarix_exchange.models.orders import Entry, Fill, OrderAck, OrderInfo, OrderRequest, OrderStatus
from luarix_exchange.models.orders import Side

class Broker(ABC):
    """La interfaz única detrás de la que vive cualquier broker.

    Todo lo que hace falta para OPERAR una cuenta, con la misma firma para
    Binance y para Bitunix, en spot y en futuros:

    | qué                          | método                                |
    |------------------------------|---------------------------------------|
    | última vela / N velas        | `get_last_candle` / `get_candles`     |
    | velas de un rango de fechas  | `get_historical_candles`              |
    | último precio                | `get_ticker`                          |
    | libro de órdenes             | `get_order_book`                      |
    | saldo (todo o un activo)     | `get_balance`                         |
    | posiciones abiertas          | `get_positions`                       |
    | órdenes vivas                | `get_open_orders`                     |
    | detalle de UNA orden         | `get_order`                           |
    | crear orden                  | `place_order`                         |
    | editar orden                 | `update_order`                        |
    | cancelar orden / todas       | `cancel_order` / `cancel_open_orders` |
    | apalancamiento (futuros)     | `set_leverage`                        |

    Casi todos aceptan `market="spot"|"futures"`. Es un parámetro y no dos
    clases de broker porque una misma API key da acceso a las dos cuentas en
    los dos exchanges, y un grafo puede consultar el saldo de una y colocar
    en la otra.

    Qué es abstracto y qué no, que acá tiene un motivo: lo `@abstractmethod`
    es lo que NINGÚN broker puede heredar (hablar con su propia API). Lo
    concreto delega en `market_data_provider` / `account_data_provider`, así
    que un broker nuevo que traiga esos dos proveedores hereda gratis las
    consultas, y uno que no los tenga (PaperBroker, MT5) falla con un
    mensaje que lo dice en vez de romper por método ausente.
    """
    on_kline: Callable
    on_execution_report: Callable
    market_data_provider: MarketDataProvider
    account_data_provider: AccountDataProvider

    # Capability flag, not abstract -- same pattern as on_order_status_change/
    # on_candle/load_csv below (concrete no-op defaults a subclass may
    # override, not a hard ABC contract). True means this broker's
    # place_order()/cancel_order() can be used to open/close an independent
    # Entry+TP+SL Trade (see workflows/execution/position_manager.py) instead
    # of the OCO-only place_oco_order()/cancel_oco()/update_oco() path.
    # Mirrored (not derived at runtime) onto the Django brokers.Broker
    # catalog row so the frontend can surface it without importing this
    # package -- same relationship already documented on that model.
    supports_independent_orders: bool = False

    # ¿`place_oco_order()` de esta clase habla con un OCO NATIVO del exchange
    # (una sola llamada, y el propio exchange cancela la pierna que no se
    # ejecutó), o levanta NotImplementedError?
    #
    # `False` por defecto a propósito: un broker nuevo no tiene OCO hasta que
    # alguien lo implementa y lo declara, y con este flag en False el motor
    # coloca TP y SL como dos órdenes normales y se encarga él mismo de
    # cancelar la hermana cuando una llena (ver
    # `workflows/execution/position_manager.py`). Antes de este flag, un
    # `order_mode="oco"` contra Bitunix reventaba con NotImplementedError en
    # el instante del fill -- o sea, con la posición ya abierta y sin
    # protección, el peor momento posible para descubrirlo.
    supports_oco: bool = False

    # ¿Tiene este broker cuenta de derivados? `get_balance("futures")` y
    # compañía levantan si es False, en vez de devolver los datos de spot en
    # silencio -- que es el error que un grafo mal configurado no nota nunca.
    supports_futures: bool = False

    @abstractmethod
    def connect(self) -> None:
        """
        Establish a connection to the broker's API or trading platform.
        """
        pass

    @abstractmethod
    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> dict:
        """La profundidad del libro (bids y asks) para un par.

        Se devuelve el diccionario CRUDO del exchange, sin normalizar: cada
        uno usa su propia forma y quien lee un libro suele querer los datos
        exactos. Los futuros de Bitunix levantan `NotImplementedError` --
        su endpoint no está verificado y devolver el de spot sería mentir.

        Args:
            symbol: Par (ej: "BTCUSDT").
            limit:  Cuántos niveles por lado.
            market: "spot" o "futures".
        """
        pass

    @abstractmethod
    def list_active(self) -> list:
        """Los símbolos que este broker puede operar."""
        pass

    # ── Velas ───────────────────────────────────────────────────────────
    # Concretos y delegando en `market_data_provider`: un broker que ya
    # tenga proveedor de datos de mercado los hereda sin tocar su clase.
    #
    # Antes NO EXISTÍAN. Para leer una vela había que alcanzar a mano
    # `broker.market_data_provider.get_last_candles(...)`, que además
    # devuelve cosas distintas según el broker (Binance un DataFrame,
    # Bitunix una lista de `Candle`). Acá el tipo es siempre `list[Candle]`.

    def get_candles(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 100,
        market: str = "spot",
    ) -> list[Candle]:
        """Las `limit` velas CERRADAS más recientes, de la más antigua a la
        más nueva.

        El orden ascendente no es un detalle: los indicadores y cualquier
        cálculo de estructura lo dan por hecho, y los dos exchanges
        devuelven sus klines de la más reciente a la más antigua.

        Args:
            symbol:   Par (ej: "BTCUSDT").
            interval: "1m", "5m", "1h", "4h", "1d"... Ver el `INTERVAL_MAP`
                      de cada proveedor: no todos aceptan los mismos.
            limit:    Cuántas velas. El máximo por llamada lo pone el
                      exchange (1000 en Binance, 200 en Bitunix).
            market:   "spot" o "futures". Importa de verdad -- son series de
                      precios DISTINTAS para el mismo par (base y funding).
        """
        return self._market_data().get_last_candles(
            symbol, interval, limit, market=market, format=DataFormat.CANDLE
        )

    def get_last_candle(
        self,
        symbol: str,
        interval: str = "1m",
        market: str = "spot",
    ) -> Candle | None:
        """La última vela cerrada, o `None` si el exchange no devolvió
        ninguna. Atajo de `get_candles(..., limit=1)`."""
        candles = self.get_candles(symbol, interval, limit=1, market=market)
        return candles[-1] if candles else None

    def get_historical_candles(
        self,
        symbol: str,
        interval: str = "1m",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        market: str = "spot",
    ) -> list[Candle]:
        """Velas de un rango de fechas concreto, paginando si hace falta.

        Distinto de `get_candles`, que da "las N últimas": esto es lo que
        necesita un backtest, y puede costar varias llamadas al exchange.
        """
        return self._market_data().get_historical_candles(
            symbol, interval, start, end, limit, format=DataFormat.CANDLE, market=market
        )

    def _market_data(self) -> MarketDataProvider:
        provider = getattr(self, "market_data_provider", None)
        if provider is None:
            raise NotImplementedError(f"{type(self).__name__} no expone datos de mercado.")
        return provider

    def _account_data(self) -> AccountDataProvider:
        provider = getattr(self, "account_data_provider", None)
        if provider is None:
            raise NotImplementedError(f"{type(self).__name__} no expone datos de cuenta.")
        return provider

    @abstractmethod
    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        """El saldo DISPONIBLE por activo.

        "Disponible" y no "total": es con lo que se puede operar ahora
        mismo. Lo bloqueado en órdenes abiertas o en margen de una posición
        queda fuera -- ver `AccountDataProvider.get_balance`.

        Args:
            market: "spot" (por defecto) o "futures".
            asset:  Filtra por un activo concreto ("USDT", "BTC"). Devuelve
                    `{asset: 0.0}` si la cuenta no tiene ese activo, NO un
                    diccionario vacío: "no tienes BTC" y "no pregunté por
                    BTC" son respuestas distintas y el llamador no debería
                    tener que distinguirlas con un `if asset in saldo`.

        Returns:
            dict[str, float]: activo -> saldo disponible.
        """
        pass

    # ── Órdenes vivas en el exchange ────────────────────────────────────
    # Concretos (no abstractos) y delegando en `account_data_provider`: así
    # cualquier broker que ya tenga proveedor de datos de cuenta los hereda
    # sin tocar su clase, y los que no (PaperBroker, MT5) fallan con un
    # mensaje claro en vez de romper por método ausente.

    def get_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[OrderInfo]:
        """Las órdenes pendientes en el exchange, ya normalizadas.

        `symbol=None` = todos los pares, cuando el broker lo permita. El
        spot de Bitunix NO lo permite (su endpoint exige símbolo) y en ese
        caso levanta en vez de devolver `[]` -- una lista vacía se leería
        como "no tienes órdenes", que es justo la conclusión equivocada.

        Devuelve `OrderInfo` y no el diccionario crudo del exchange: los dos
        brokers llaman distinto a los mismos campos (`origQty` vs `qty`,
        `executedQty` vs `tradeQty`). El original sigue entero en `.raw`.
        """
        return [self._to_order_info(raw, market)
                for raw in self._account_data().get_open_orders(market, symbol)]

    def get_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderInfo:
        """El detalle COMPLETO de una orden por su id, esté viva o cerrada.

        Distinto de `get_order_status`, que solo da el enum: acá vienen
        además la cantidad ejecutada, el precio medio real y el TP/SL
        adjunto si lo lleva.

        Debe responder también por órdenes ya CERRADAS. Un broker que solo
        sepa mirar la lista de pendientes no puede implementar esto
        honestamente: ausente de los pendientes puede significar "llenó" o
        "la cancelaron a mano", y confundirlas ya provocó un bug grave (ver
        `docs/plans/bugs.md`).
        """
        return self._to_order_info(self._account_data().get_order(symbol, order_id, market), market)

    def _to_order_info(self, raw: dict, market: str) -> OrderInfo:
        """Traduce el diccionario del exchange a `OrderInfo`. Cada broker
        concreto elige el normalizador de su API."""
        raise NotImplementedError(f"{type(self).__name__} no sabe interpretar órdenes del exchange.")

    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        """Cancela las pendientes y devuelve los ids cancelados."""
        return self._account_data().cancel_open_orders(market, symbol)

    @abstractmethod
    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        """El último precio negociado del par.

        `market` importa: el perpetuo y el contado del mismo par NO cotizan
        al mismo precio (base y funding los separan), y dimensionar una
        orden de futuros con el precio de spot introduce un error silencioso
        en la cantidad.

        Args:
            symbol: Par (ej: "BTCUSDT").
            market: "spot" o "futures".

        Returns:
            float: el último precio.
        """
        pass

    @abstractmethod
    def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        """
        Place a new order in the market.

        Args:
            order (OrderRequest): The order request object containing order details.

        Returns:
            tuple[OrderAck, list[Fill]]: A tuple containing the order acknowledgment and a list of fills.
        """
        pass

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck:
        """
        Cancel an existing order.

        Args:
            symbol (str): The trading pair symbol (e.g., 'BTC/USD').
            order_id (str): The unique identifier of the order to cancel.
            market (str): "spot" (por defecto) o "futures". Opcional a
                propósito, para que los llamadores que no distinguen (el
                simulador, MT5) sigan invocando con dos argumentos. Importa
                en los brokers cuyas dos cuentas viven en APIs distintas: en
                Bitunix, cancelar el TP de una posición de FUTUROS por la vía
                de spot no encuentra nada y devuelve un OK falso.

        Returns:
            OrderAck: An acknowledgment of the canceled order.
        """
        pass

    def get_order_status(self, symbol: str, order_id: str, market: str = "spot") -> OrderStatus:
        """El estado de una orden ya colocada.

        Concreto, no abstracto, y derivado de `get_order()`: cualquier
        broker que sepa dar el detalle de una orden sabe dar su estado.

        Lo usa el tracker de órdenes para enterarse de que una entrada llenó
        cuando el broker no tiene stream de datos de usuario -- que hoy son
        todos, ver `market_data/binance.py` (su `start_user_socket` está
        comentado).

        ⚠ Antes esto se derivaba de la lista de PENDIENTES: lo que ya no
        aparecía se daba por FILLED. Esa inferencia es falsa -- una orden
        cancelada a mano también desaparece de los pendientes -- y abrió
        posiciones fantasma en producción (ver `docs/plans/bugs.md`).
        """
        return self.get_order(symbol, order_id, market).status

    @abstractmethod
    def update_order(
        self,
        symbol: str,
        order_id: str,
        *,
        price: float | None = None,
        quantity: float | None = None,
        stop_price: float | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
        market: str = "spot",
    ) -> OrderAck:
        """Modifica una orden pendiente y devuelve la orden RESULTANTE.

        Los parámetros son todos opcionales y por nombre: lo que no se pasa
        se conserva de la orden original. `update_order(sym, id)` sin nada
        más es por tanto un no-op deliberado sobre los valores (sirve para
        refrescar el ack).

        ⚠ **El id puede cambiar.** Solo Bitunix futuros y Binance futuros
        tienen enmienda nativa; en spot no existe tal cosa y la única
        manera es CANCELAR y volver a colocar, lo que produce un
        `exchange_order_id` nuevo. Quien guarde el id tiene que releerlo del
        `OrderAck` que se devuelve, no darlo por estable.

        ⚠ **Cancelar y recolocar no es atómico.** Entre las dos llamadas la
        protección no existe. Para mover un stop-loss en un mercado que se
        mueve, eso importa.
        """
        pass

    @abstractmethod
    def place_oco_order(self, symbol: str, side: Side, quantity: float, take_profit_price: float, stop_price: float, stop_limit_price: float) -> tuple[str, OrderAck, OrderAck]:
        pass

    @abstractmethod
    def cancel_oco(self, symbol: str, oco_list_id: int) -> OrderAck:
        pass

    @abstractmethod
    def update_oco(self, entry: Entry, new_tp: float, new_sl: float, new_sl_limit:float) -> tuple[str, OrderAck, OrderAck]:
        pass

    @abstractmethod
    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[BrokerPosition]:
        """Las posiciones ABIERTAS ahora mismo.

        En FUTUROS son posiciones de verdad, con dirección, precio de
        entrada, apalancamiento y no realizado. En SPOT no existe tal cosa:
        lo que hay son saldos de activos, y se devuelven representados igual
        (`side="LONG"`, `entry_price=0.0`) para que listar "lo que tengo
        abierto" no obligue a ramificar por mercado.

        Solo las que tienen cantidad distinta de cero: los dos exchanges
        devuelven también las cerradas con `qty=0` y listarlas sería ruido.

        Args:
            market: "spot" o "futures".
            symbol: Filtra por par. `None` = todos.
        """
        pass

    # ── Futuros ─────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        """Fija el apalancamiento de un par en la cuenta de derivados.

        Concreto y delegando: no todos los brokers tienen futuros. Es una
        operación de CUENTA, no de orden -- afecta a todo lo que se coloque
        después en ese par, incluido lo que colocó otra sesión.
        """
        return self._account_data().set_leverage(symbol, leverage, market)

    def on_order_status_change(self, report):
        pass

    def on_candle(self, tick: Candle):
        pass

    def load_csv(symbol:str, timeframe:str, filepath:str):
        pass
