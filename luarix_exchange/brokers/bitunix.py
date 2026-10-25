import logging
from decimal import Decimal

from luarix_exchange.brokers.account_data.bitunix import BitunixAccountDataProvider
from luarix_exchange.brokers.base import Broker
from luarix_exchange.brokers.bitunix_streams import BitunixStreamsMixin
from luarix_exchange.brokers.bitunix_response import BitunixApiError, unwrap
from luarix_exchange.brokers.bitunix_spot_client import QuietBitunixClient
from luarix_exchange.brokers.credentials import BrokerCredentials, resolve_credentials
from luarix_exchange.brokers.market_data.bitunix import BitunixMarketDataProvider
from luarix_exchange.capabilities import (
    AccountSummarySource,
    Capability,
    FundingRates,
    SymbolRulesSource,
    TickerStats,
)
from luarix_exchange.models.market import FundingRate, Ticker24h, ms_to_datetime, pct_change
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import BrokerPosition, Market
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.models.orders import (
    Fill, OrderAck, OrderInfo, OrderRequest, OrderStatus, OrderType,
)
from luarix_exchange.models.orders import Side

logger = logging.getLogger(__name__)


def ticker_from_bitunix(row: dict, rules: SymbolRules) -> Ticker24h:
    """Una fila de `/futures/market/tickers` → `Ticker24h` (no trae % de cambio)."""
    last = float(row.get("last") or row.get("lastPrice") or 0)
    open_ = float(row.get("open") or 0)
    return Ticker24h(
        symbol=row["symbol"], base=rules.base, quote=rules.quote, market=Market.FUTURES.value,
        last=last, open=open_, high=float(row.get("high") or 0), low=float(row.get("low") or 0),
        change_pct=pct_change(open_, last),
        volume_base=float(row.get("baseVol") or 0), volume_quote=float(row.get("quoteVol") or 0),
    )


def funding_from_bitunix(row: dict, rules: SymbolRules) -> FundingRate:
    """Una fila de `/futures/market/funding_rate/batch`; su `fundingRate` va
    en porcentaje y se pasa a fracción."""
    return FundingRate(
        symbol=row["symbol"], base=rules.base, quote=rules.quote, rate=float(row["fundingRate"]) / 100,
        mark_price=float(row["markPrice"]) if row.get("markPrice") else None,
        interval_hours=float(row.get("fundingInterval") or 8),
        next_funding_time=ms_to_datetime(row.get("nextFundingTime")),
    )


class BitunixBroker(Broker, SymbolRulesSource, AccountSummarySource, TickerStats, FundingRates, BitunixStreamsMixin):
    """Bitunix: spot y futuros perpetuos USDT-M.

    Dos APIs distintas en dos hosts distintos. El paquete `bitunix` de PyPI
    solo cubre el spot (`/api/spot/v1/...`); los futuros van por
    `BitunixFuturesClient`, escrito en este repo. De ahí que casi todo lleve
    un `market="spot"|"futures"`: no hay un endpoint común al que llamar.

    Bitunix NO tiene testnet. `BrokerFactory` ignora el parámetro `testnet`
    para este broker en vez de buscar unas credenciales de prueba que no
    pueden existir.
    """

    name = "bitunix"
    label = "Bitunix"
    markets = frozenset({Market.SPOT, Market.FUTURES})
    # WebSocket solo en FUTUROS (el spot de Bitunix tiene otra API WebSocket,
    # no implementada): ahí la selección automática usa polling.
    native_stream_markets = {
        Capability.CANDLE_STREAM: frozenset({Market.FUTURES}),
        Capability.ORDER_BOOK_STREAM: frozenset({Market.FUTURES}),
        Capability.ORDER_UPDATE_STREAM: frozenset({Market.FUTURES}),
    }

    # Bitunix NO tiene OCO: `place_oco_order` levanta NotImplementedError más
    # abajo (su API no expone el endpoint, y el paquete `bitunix` de PyPI es
    # solo spot). Hereda `NativeOco` a través de `Broker`, así que lo declara
    # NO soportado; de ahí sale `supports_oco = False`, con el que el motor deja de intentarlo y
    # gestiona TP+SL como dos órdenes normales, cancelando él mismo la
    # hermana cuando una llena -- ver `workflows/execution/position_manager.py`
    # y `execution_report_bridge._resolve_order_mode`. Antes de este flag un
    # `order_mode="oco"` contra Bitunix reventaba justo en el fill, con la
    # posición abierta y sin proteger.
    #
    # `supports_independent_orders = True`: ese camino solo necesita
    # `place_order()`/`cancel_order()`, y los dos funcionan ahora que
    # `cancel_order` construye un `OrderAck` con los campos que la clase
    # realmente tiene (antes pasaba `order_id=`/`quantity=`, que no existen,
    # y reventaba con TypeError en la primera cancelación real).
    supports_independent_orders = True
    unsupported = frozenset({Capability.NATIVE_OCO})
    # Su spot no publica las 24h de todos los pares (solo velas y último
    # precio par a par): la estadística masiva existe únicamente en futuros.
    capability_markets = {Capability.TICKER_STATS: frozenset({Market.FUTURES})}

    @classmethod
    def create(
        cls,
        credentials: BrokerCredentials | None = None,
        testnet: bool | None = None,
        test_order: bool | None = None,
        trading_mode: str = "testnet",
        allow_live_trading: bool = False,
        public_only: bool = False,
    ) -> "BitunixBroker":
        """Lo que usa la factory. `testnet` y `test_order` se aceptan y se
        IGNORAN: Bitunix no tiene testnet. Por eso las credenciales se
        resuelven con `resolve(False)`: pedirle las de prueba devolvía
        `("", "")` y la primera llamada fallaba con un `10003 API key
        inexistente` que no dice nada de la causa real."""
        if public_only and not credentials and not BrokerCredentials.from_env("BITUNIX"):
            api_key, api_secret = "", ""  # solo datos públicos
        else:
            api_key, api_secret = resolve_credentials("BITUNIX", credentials).resolve(False)
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            trading_mode=trading_mode,
            allow_live_trading=allow_live_trading,
        )

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        trading_mode: str = "paper",
        allow_live_trading: bool = False,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_mode = trading_mode
        self.allow_live_trading = allow_live_trading
        self.client = QuietBitunixClient(api_key, api_secret)
        self.market_data_provider = BitunixMarketDataProvider(api_key, api_secret)
        self.account_data_provider = BitunixAccountDataProvider(self.client)

    def connect(self) -> None:
        pass

    # ─────────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────────

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> OrderBook:
        """Libro normalizado. Futuros manda `[[precio, cantidad], ...]`; spot
        manda `[{"price", "volume"}, ...]` (ver el proveedor)."""
        raw = self.market_data_provider.get_order_book(symbol, limit, market=market) or {}

        def niveles(lado: str) -> list[tuple]:
            return [(n["price"], n["volume"]) if isinstance(n, dict) else (n[0], n[1]) for n in raw.get(lado) or []]

        return OrderBook.from_levels(symbol, market, niveles("bids"), niveles("asks"), depth=limit, raw=raw)

    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        return self.market_data_provider.get_last_price(symbol, market=market)

    @classmethod
    def public(cls) -> "BitunixBroker":
        """Datos públicos sin claves (Bitunix no tiene testnet). Su SDK
        exige una clave aunque los endpoints públicos no firmen: se le da una
        de relleno, y cualquier llamada privada fallará en el exchange."""
        credentials = BrokerCredentials.from_env("BITUNIX")
        if credentials:
            return cls.create(credentials=credentials)
        return cls(api_key="public", api_secret="public")

    def get_tickers_24h(self, market: str = "spot") -> list[Ticker24h]:
        self.require(Capability.TICKER_STATS, market)
        rules = self.get_symbol_rules(Market.FUTURES)
        return [
            ticker_from_bitunix(row, rules[row["symbol"]])
            for row in self.market_data_provider.futures.tickers()
            if row.get("symbol") in rules
        ]

    def get_funding_rates(self) -> list[FundingRate]:
        rules = self.get_symbol_rules(Market.FUTURES)
        return [
            funding_from_bitunix(row, rules[row["symbol"]])
            for row in self.market_data_provider.futures.funding_rates()
            if row.get("symbol") in rules and row.get("fundingRate") not in (None, "")
        ]

    def list_symbols(self, market: str = "spot") -> list[str]:
        """Los pares abiertos a la operación en ese mercado, en MAYÚSCULAS.

        (Era `list_active()`, solo spot; antes de eso devolvía `[]` fijo.)"""
        if Market(market) is Market.FUTURES:
            return [p["symbol"] for p in self._futures_pairs() if p.get("symbolStatus") == "OPEN"]
        return [s for s, p in self.market_data_provider.spot_pairs().items() if p.get("isOpen", 1) == 1]

    def _futures_pairs(self) -> list[dict]:
        return self.market_data_provider.futures.trading_pairs()

    def get_symbol_rules(self, market: str = "spot") -> dict[str, SymbolRules]:
        """Precisión y mínimos por par, desde las listas PÚBLICAS de pares.

        Bitunix da la precisión como número de decimales (`basePrecision=5`
        -> step 0.00001; `quotePrecision=2` -> tick 0.01). Forma verificada
        contra la API el 2026-10-16 y coherente con los precios del libro;
        todavía NO contrastada con el rechazo de una orden real. El importe
        mínimo de futuros no lo publica: queda en 0."""
        mercado = Market(market)

        def paso(decimales) -> Decimal:
            return Decimal(1).scaleb(-int(decimales or 0))

        if mercado is Market.FUTURES:
            return {
                p["symbol"]: SymbolRules(
                    symbol=p["symbol"], base=p.get("base", ""), quote=p.get("quote", ""),
                    step_size=paso(p.get("basePrecision")), tick_size=paso(p.get("quotePrecision")),
                    min_qty=Decimal(str(p.get("minTradeVolume") or "0")), min_notional=Decimal("0"),
                    market=mercado, raw=p,
                )
                for p in self._futures_pairs() if p.get("symbolStatus") == "OPEN"
            }
        return {
            simbolo: SymbolRules(
                symbol=simbolo, base=p.get("base", ""), quote=p.get("quote", ""),
                step_size=paso(p.get("basePrecision")), tick_size=paso(p.get("quotePrecision")),
                min_qty=Decimal(str(p.get("minVolume") or "0")), min_notional=Decimal(str(p.get("minAmount") or "0")),
                market=mercado, raw=p,
            )
            for simbolo, p in self.market_data_provider.spot_pairs().items() if p.get("isOpen", 1) == 1
        }

    # ─────────────────────────────────────────────
    # Account data
    # ─────────────────────────────────────────────

    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        return self.account_data_provider.get_balance(market, asset)

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[BrokerPosition]:
        """Posiciones abiertas.

        Antes devolvía `{}` FIJO: este broker afirmaba no tener ninguna
        posición abierta pasara lo que pasara, aunque la constante del
        endpoint (`PENDING_POSITIONS`) llevara tiempo declarada y sin usar.
        """
        crudas = self.account_data_provider.get_positions(market, symbol)
        if market == "futures":
            posiciones = [BrokerPosition.from_bitunix_futures(p) for p in crudas]
        else:
            posiciones = [BrokerPosition.from_spot_balance(p["asset"], float(p["free"])) for p in crudas]
        return [p for p in posiciones if p.quantity > 0]

    def _to_order_info(self, raw: dict, market: str) -> OrderInfo:
        return OrderInfo.from_bitunix(raw, market)

    # ─────────────────────────────────────────────
    # Order execution
    # ─────────────────────────────────────────────

    # Códigos del spot de Bitunix: side 1 = SELL, 2 = BUY; type 1 = LIMIT,
    # 2 = MARKET (docstring de `BitunixClient.place_order`, y confirmado con
    # el historial real de la cuenta). Estuvieron invertidos los dos: cada
    # compra a mercado del motor salía como una VENTA LIMIT al cierre de la
    # vela, y cada venta como una compra -- 9 órdenes de LTCUSDT el
    # 2026-10-23, ver `docs/plans/bugs.md` del web-server.
    _SIDE_MAP = {Side.BUY: 2, Side.SELL: 1}
    _TYPE_MAP = {OrderType.LIMIT: 1, OrderType.MARKET: 2}

    def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        self._validate_pre_trade()
        if order.market == "futures":
            return self._place_futures_order(order)

        side = self._SIDE_MAP[order.side]
        # Sin default: un tipo que el spot no tiene (stop, trailing...) caía
        # en silencio a una orden con el código 1 -- una LIMIT al precio de la
        # orden, que no es lo que se pidió. Mejor que reviente y el nodo quede
        # FAILED con el motivo en el tab Logs.
        order_type = self._TYPE_MAP.get(order.order_type)
        if order_type is None:
            raise BitunixApiError(
                f"El spot de Bitunix solo acepta órdenes MARKET y LIMIT, no {order.order_type.value}."
            )
        volume = str(order.quantity)
        price = str(order.price) if order.price else "0"

        raw = self.client.place_order(side=side, order_type=order_type, volume=volume, price=price, symbol=order.symbol)
        # SIN `on_null`: si Bitunix rechaza la orden, esto tiene que reventar.
        # Antes era `raw.get("data", {})`, que ante un `"data": null` dejaba
        # `data` en `None`... y `None.get("orderId")` habría explotado igual,
        # pero de haber devuelto `{}` el resultado era peor: un `OrderAck` con
        # `order_id=""` y `status=NEW`, o sea una orden FANTASMA que el motor
        # daba por colocada y a la que luego colgaría su SL/TP.
        data = unwrap(raw, "la colocación de la orden")
        order_id = str(data.get("orderId", ""))
        if not order_id:
            raise BitunixApiError(f"Bitunix aceptó la orden pero no devolvió `orderId`: {data!r}")
        return self._ack(order_id, order, data), []

    def _validate_pre_trade(self) -> None:
        """El mismo candado doble que Binance: para operar en real hacen
        falta `trading_mode="live"` Y `allow_live_trading=True`.

        Faltaba por completo en este broker -- o sea que la protección
        contra ejecutar con dinero real por un error de configuración
        existía solo en Binance, mientras que Bitunix (el único de los dos
        con el que el usuario opera de verdad) colocaba siempre.

        Bitunix NO tiene testnet: toda orden que llega a su API es dinero
        real. Por eso el candado es "solo se coloca con los dos en verde", no
        "solo se bloquea en live sin permiso": antes, con el modo por defecto
        (`"paper"`), el guard no saltaba y la orden salía igual al mercado --
        el nombre prometía una simulación que no existía."""
        if self.trading_mode != "live" or not self.allow_live_trading:
            raise RuntimeError(
                "Bitunix has no testnet: every order is real money. Placing needs "
                "trading_mode='live' AND allow_live_trading=True "
                f"(got trading_mode={self.trading_mode!r}, allow_live_trading={self.allow_live_trading})."
            )

    @staticmethod
    def _ack(order_id: str, order: OrderRequest, raw: dict) -> OrderAck:
        """`OrderAck` con los campos que la clase REALMENTE tiene.

        Todos los constructores de este broker pasaban `order_id=` y
        `quantity=`, que no existen en `OrderAck` (sus campos son
        `exchange_order_id`, `client_order_id`, `symbol`, `side`, `status`,
        `oco_list_id`, `raw`). O sea que `place_order` reventaba con
        `TypeError: unexpected keyword argument 'order_id'` en cuanto se
        llamaba de verdad -- y como nadie lo llamaba (el motor colocaba
        siempre contra el PaperBroker), el bug llevaba ahí desde que se
        escribió."""
        return OrderAck(
            exchange_order_id=order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.NEW,
            raw=raw if isinstance(raw, dict) else {},
        )

    def _place_futures_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        """Orden en el mercado de derivados.

        El paquete `bitunix` no lo cubre (es solo spot), así que va por
        `BitunixFuturesClient`. Los nombres de campo (`qty`, `side`,
        `orderType`, `effect`, `tradeSide`) están verificados contra la API
        real con un símbolo inválido, que la rechaza DESPUÉS de validar el
        cuerpo -- así se confirmaron sin colocar nada.

        ⚠ Modo COBERTURA: si la cuenta está en HEDGE (`positionMode`), una
        venta NO reduce un largo abierto -- abre una posición corta
        independiente que convive con él. El neteo automático solo ocurre en
        ONE_WAY. `BitunixFuturesClient.place_order` consulta el modo real de
        la cuenta y manda `tradeSide` solo cuando hace falta."""
        data = self.account_data_provider.futures.place_order(
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            price=order.price,
            order_type=order.order_type.value,
            # TP/SL adjuntos a la propia entrada, y `reduce_only` para que una
            # salida se marque `tradeSide="CLOSE"` en modo HEDGE. Sin lo
            # segundo, el take-profit de un largo abría un corto real -- ver
            # `BitunixFuturesClient.place_order` y `docs/plans/bugs.md`.
            tp_price=order.tp_price,
            sl_price=order.sl_price,
            reduce_only=order.reduce_only,
        )
        order_id = str(data.get("orderId", "")) if isinstance(data, dict) else ""
        if not order_id:
            raise BitunixApiError(f"Bitunix aceptó la orden de futuros pero no devolvió `orderId`: {data!r}")
        return self._ack(order_id, order, data), []

    def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck:
        """Cancela UNA orden. `market="futures"` va por el cliente de
        derivados -- el paquete `bitunix` es solo spot, así que cancelar el
        TP/SL de una posición de futuros por la vía de spot no encontraba
        nada y devolvía un OK falso.

        `OrderAck` con los campos que la clase REALMENTE tiene: antes se
        construía con `order_id=`/`quantity=`, que no existen (`exchange_order_id`
        sí), así que la primera cancelación real reventaba con
        `TypeError: unexpected keyword argument 'order_id'`. Nadie lo notó
        porque hasta ahora el motor solo cancelaba contra el PaperBroker."""
        # Cancelar también toca dinero real (una orden viva de la cuenta):
        # mismo candado que colocar.
        self._validate_pre_trade()
        if market == "futures":
            raw = self.account_data_provider.futures.cancel_orders(symbol, [str(order_id)])
        else:
            raw = self.client.cancel_orders([{"orderId": order_id, "symbol": symbol}])
        return OrderAck(
            exchange_order_id=str(order_id), symbol=symbol, status=OrderStatus.CANCELED,
            raw=raw if isinstance(raw, dict) else {},
        )

    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        """Cancelación MASIVA, con el mismo candado que `cancel_order`.

        La versión de `Broker` delega directo en `account_data_provider`, así
        que este camino (el del nodo Cancel All Orders) se saltaba
        `_validate_pre_trade`: el candado existía para cancelar UNA orden,
        pero no para cancelar todas."""
        self._validate_pre_trade()
        return super().cancel_open_orders(market, symbol)

    #-- Los estados que Bitunix devuelve en `get_order_detail`, traducidos a
    #-- los nuestros (que son los de Binance, ver `core/models/orders.py`).
    #-- `PART_FILLED` es el mismo concepto que `PARTIALLY_FILLED` con otro
    #-- nombre; `INIT` ("en preparación") todavía no llegó al libro, así que
    #-- para el motor es indistinguible de NEW: sigue viva y no llenó.
    _FUTURES_STATUS = {
        "INIT": OrderStatus.NEW,
        "NEW": OrderStatus.NEW,
        "PART_FILLED": OrderStatus.PARTIALLY_FILLED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "CANCELLED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
    }

    def get_order_status(self, symbol: str, order_id: str, market: str = "spot") -> OrderStatus:
        """El estado real de una orden en el exchange.

        Lo necesita el tracker de órdenes: Bitunix no tiene stream de datos
        de usuario, así que la única forma de saber qué pasó con una entrada
        es preguntarlo.

        En futuros se pregunta por `get_order_detail`, que responde por la
        orden concreta esté viva o cerrada. ANTES se miraba la lista de
        PENDIENTES y se daba por LLENADA toda orden que no apareciera en
        ella. Esa inferencia es falsa y hacía daño real: una orden cancelada
        a mano en la app de Bitunix también desaparece de los pendientes, así
        que el tracker la marcaba FILLED, abría un `Trade` en ACTIVE por una
        posición que nunca existió, y a partir de ahí la orden ya no estaba en
        un estado "vivo" -- ninguna reconciliación posterior volvía a mirarla.
        Ocurrió con tres cortos de LTCUSDT (ids 2082878949264547840,
        2082879103115870208, 2082880194939265024), ver `docs/plans/bugs.md`.

        Una orden que el exchange dice no conocer devuelve NEW, no FILLED: la
        ignorancia no es un fill. El coste de equivocarse hacia NEW es que el
        tracker vuelve a preguntar en la siguiente pasada; el de equivocarse
        hacia FILLED es colocar un TP/SL real contra una posición inexistente
        (el `10002 Parameter error` que ya está documentado).

        En SPOT delega en `get_order()`, que combina ejecuciones y pendientes
        y LEVANTA si la orden no aparece en ninguno de los dos sitios --
        antes ese caso se traducía a FILLED, con el mismo riesgo.
        """
        if market == "futures":
            detalle = self.account_data_provider.futures.order_detail(order_id)
            crudo = str(detalle.get("status") or "").upper()
            estado = self._FUTURES_STATUS.get(crudo)
            if estado is not None:
                return estado
            logger.warning(
                "Bitunix no dio un estado reconocible para la orden de futuros %s (%s): status=%r, "
                "cuerpo=%r. Se la trata como VIVA -- se volverá a preguntar.",
                order_id, symbol, crudo or None, detalle,
            )
            return OrderStatus.NEW

        return self.get_order(symbol, order_id, market).status

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
        """Modifica una orden pendiente.

        En FUTUROS hay enmienda nativa (`/futures/trade/modify_order`): la
        orden nunca deja de existir y conserva su `orderId`. Bitunix exige
        mandar `qty` y `price` aunque solo se toque uno, así que se lee
        antes la orden y se rellena lo que el llamador no pasó -- si no, un
        "sube el precio" borraría la cantidad.

        En SPOT no existe enmienda: la única vía es CANCELAR y volver a
        colocar, lo que produce un `exchange_order_id` NUEVO y deja un hueco
        sin protección entre las dos llamadas. El `OrderAck` devuelto lleva
        el id nuevo; quien tuviera guardado el viejo tiene que releerlo.

        Antes este método no modificaba nada: consultaba el estado de la
        orden y devolvía un ack, ignorando por completo cualquier valor
        nuevo (que además ni siquiera podía recibir -- su firma era
        `(symbol, order_id)`).
        """
        if stop_price is not None and sl_price is None:
            # En Bitunix el stop no es una orden aparte: es un nivel adjunto.
            # Se acepta el nombre genérico del contrato y se traduce, en vez
            # de ignorarlo en silencio.
            sl_price = stop_price

        if market == "futures":
            actual = self.get_order(symbol, order_id, market)
            data = self.account_data_provider.futures.modify_order(
                order_id,
                quantity=quantity if quantity is not None else actual.quantity,
                price=price if price is not None else actual.price,
                tp_price=tp_price if tp_price is not None else actual.tp_price,
                sl_price=sl_price if sl_price is not None else actual.sl_price,
            )
            nuevo_id = str(data.get("orderId") or order_id) if isinstance(data, dict) else str(order_id)
            return OrderAck(
                exchange_order_id=nuevo_id, symbol=symbol, side=actual.side,
                status=OrderStatus.NEW, raw=data if isinstance(data, dict) else {},
            )

        return self._replace_spot_order(symbol, order_id, price, quantity)

    def _replace_spot_order(
        self, symbol: str, order_id: str, price: float | None, quantity: float | None,
    ) -> OrderAck:
        """Cancelar + recolocar, que es todo lo que el spot de Bitunix
        permite. NO es atómico: entre las dos llamadas la orden no existe."""
        actual = self.get_order(symbol, order_id, "spot")
        if not actual.is_open:
            raise BitunixApiError(
                f"La orden {order_id} de {symbol} está en {actual.status.value}: solo se puede "
                "modificar una orden viva."
            )
        if actual.side is None:
            raise BitunixApiError(
                f"Bitunix no dijo el lado de la orden {order_id}, y recolocarla sin saberlo "
                f"abriría una posición en la dirección equivocada. Respuesta: {actual.raw!r}"
            )

        self.cancel_order(symbol, order_id, "spot")
        nueva = OrderRequest(
            symbol=symbol,
            side=actual.side,
            quantity=quantity if quantity is not None else actual.remaining_quantity,
            order_type=OrderType.LIMIT if (price or actual.price) else OrderType.MARKET,
            price=price if price is not None else actual.price,
            market="spot",
        )
        ack, _ = self.place_order(nueva)
        return ack

    # ─────────────────────────────────────────────
    # OCO -- no existe en Bitunix
    # ─────────────────────────────────────────────

    def place_oco_order(self, symbol, side, quantity, take_profit_price, stop_price, stop_limit_price):
        raise NotImplementedError(
            "Bitunix no tiene OCO nativo. En futuros el TP/SL van ADJUNTOS a la orden de entrada "
            "(`OrderRequest.tp_price`/`sl_price`), que además es atómico; en spot hay que "
            "gestionarlos como dos órdenes y cancelar la hermana al llenar una."
        )

    def cancel_oco(self, symbol, oco_list_id):
        raise NotImplementedError("Bitunix no tiene OCO nativo -- ver `place_oco_order`.")

    def update_oco(self, entry, new_tp, new_sl, new_sl_limit):
        raise NotImplementedError("Bitunix no tiene OCO nativo -- ver `place_oco_order`.")

    # ─────────────────────────────────────────────
    # Futuros
    # ─────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        return self.account_data_provider.set_leverage(symbol, leverage, market)
