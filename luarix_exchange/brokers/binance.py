import decimal
import math
from dataclasses import dataclass, field
from uuid import uuid4

from binance import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException

from luarix_exchange.capabilities import SymbolRulesSource
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.brokers import BrokerPosition, Market
from luarix_exchange.models.rules import SymbolRules
from luarix_exchange.models.orders import (
    Entry, Fill, OrderAck, OrderInfo, OrderRequest, OrderStatus, OrderType, TimeInForce,
)
from luarix_exchange.models.orders import Side
from luarix_exchange.brokers.account_data.binance import BinanceAccountDataProvider
from luarix_exchange.brokers.base import Broker
from luarix_exchange.brokers.credentials import BrokerCredentials, resolve_credentials
from luarix_exchange.brokers.market_data.binance import BinanceMarketDataProvider


def rules_from_binance_symbol(info: dict, market: Market | str) -> SymbolRules:
    """`SymbolRules` desde una entrada de `exchangeInfo["symbols"]` (spot o
    USDT-M futuros: los dos usan los filtros `LOT_SIZE`, `PRICE_FILTER` y
    `NOTIONAL`/`MIN_NOTIONAL`, con el importe mínimo en `minNotional` en spot
    y en `notional` en futuros)."""
    filtros = {f.get("filterType"): f for f in info.get("filters", [])}
    lot = filtros.get("LOT_SIZE", {})
    price = filtros.get("PRICE_FILTER", {})
    notional = filtros.get("NOTIONAL") or filtros.get("MIN_NOTIONAL") or {}
    return SymbolRules(
        symbol=info["symbol"],
        base=info.get("baseAsset", ""),
        quote=info.get("quoteAsset", ""),
        step_size=decimal.Decimal(str(lot.get("stepSize", "0"))),
        tick_size=decimal.Decimal(str(price.get("tickSize", "0"))),
        min_qty=decimal.Decimal(str(lot.get("minQty", "0"))),
        min_notional=decimal.Decimal(str(notional.get("minNotional") or notional.get("notional") or "0")),
        market=Market(market),
        raw=info,
    )


@dataclass
class SymbolFilters:
    """Reglas de precisión que Binance exige para un símbolo.

    Binance rechaza órdenes que no respetan estos límites.
    Se obtienen una sola vez por símbolo y se cachean.

    Atributos:
        step_size:    Mínima variación de cantidad permitida (ej: "0.001" para BTC).
        tick_size:    Mínima variación de precio permitida (ej: "0.01" para BTCUSDT).
        min_notional: Valor mínimo de la orden en moneda quote (ej: 10.0 USDT).
    """
    step_size: str = "1"
    tick_size: str = "0.01"
    min_notional: float = 10.0


class BinanceBroker(Broker, SymbolRulesSource):
    """Broker que conecta el framework con la API de Binance.

    Maneja tres modos de operación controlados por las variables de entorno:
        - paper   (TRADING_MODE=paper):   Simula las órdenes localmente, sin tocar Binance.
        - testnet (testnet=True):          Usa la red de prueba de Binance (dinero ficticio).
        - live    (trading_mode=live):     Opera con dinero real. Requiere ALLOW_LIVE_TRADING=true.

    Además, `test_order=True` añade una capa extra: valida la orden contra la API
    de Binance sin ejecutarla realmente (útil para verificar parámetros).
    """

    name = "binance"
    label = "Binance"
    # OCO nativo (NativeOco, heredado de Broker): `place_oco_order` usa el
    # endpoint real de Binance y es el exchange quien cancela la pierna que
    # no se ejecutó. `supports_oco`/`supports_futures` se derivan de esto.
    markets = frozenset({Market.SPOT, Market.FUTURES})
    supports_independent_orders = True

    @classmethod
    def create(
        cls,
        credentials: BrokerCredentials | None = None,
        testnet: bool | None = True,
        test_order: bool = True,
        trading_mode: str = "testnet",
        allow_live_trading: bool = False,
    ) -> "BinanceBroker":
        """Lo que usa la factory: resuelve las credenciales (las pasadas o
        las del entorno) y elige las de testnet o producción según `testnet`."""
        api_key, api_secret = resolve_credentials("BINANCE", credentials).resolve(bool(testnet))
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            testnet=bool(testnet),
            test_order=test_order,
            trading_mode=trading_mode,
            allow_live_trading=allow_live_trading,
        )

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        test_order: bool = True,
        trading_mode: str = "paper",
        allow_live_trading: bool = False,
    ):
        """Inicializa el broker y establece la conexión con Binance.

        Args:
            api_key:            Clave API de Binance (de testnet o producción según `testnet`).
            api_secret:         Secreto API correspondiente.
            testnet:            Si True, conecta a la testnet de Binance (dinero ficticio).
            test_order:         Si True, todas las órdenes usan `create_test_order` —
                                se validan contra la API pero NO se ejecutan.
            trading_mode:       "paper" | "testnet" | "live". Controla validaciones adicionales.
            allow_live_trading: Segundo candado para modo live. Debe ser True explícitamente
                                además de trading_mode="live" para operar con dinero real.
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.test_order = test_order
        self.trading_mode = trading_mode
        self.allow_live_trading = allow_live_trading
        self.client = Client(self.api_key, self.api_secret, testnet=self.testnet)
        self.market_data_provider = BinanceMarketDataProvider(client=self.client)
        self.account_data_provider = BinanceAccountDataProvider(client=self.client)
        # Caché de filtros por símbolo para evitar llamadas repetidas a la API
        self._filters_cache: dict[str, SymbolFilters] = {}
        # Caché de `get_symbol_rules` por mercado (una llamada a exchangeInfo).
        self._rules_cache: dict[Market, dict[str, SymbolRules]] = {}

    def connect(self) -> None:
        """Verifica que la conexión con Binance está activa (ping)."""
        self.client.ping()

    def _client(self) -> Client:
        """Devuelve el cliente de Binance, inicializando la conexión si es necesario."""
        if self.client is None:
            self.connect()
        return self.client
    
    # ─────────────────────────────────────────────
    # General endpoints
    # ─────────────────────────────────────────────

    def _exchange_info(self, market: str) -> dict:
        if Market(market) is Market.FUTURES:
            return self._client().futures_exchange_info()
        return self._client().get_exchange_info()

    def list_symbols(self, market: str = "spot") -> list[str]:
        """Los pares en estado TRADING del mercado pedido."""
        return [s["symbol"] for s in self._exchange_info(market).get("symbols", [])
                if s.get("status") == "TRADING"]

    def get_symbol_rules(self, market: str = "spot") -> dict[str, SymbolRules]:
        """Reglas de todos los pares en TRADING, cacheadas por mercado (una
        sola llamada a exchangeInfo). Spot y futuros tienen reglas DISTINTAS
        para el mismo par."""
        mercado = Market(market)
        cache = self.__dict__.setdefault("_rules_cache", {})
        if mercado not in cache:
            cache[mercado] = {
                s["symbol"]: rules_from_binance_symbol(s, mercado)
                for s in self._exchange_info(mercado).get("symbols", [])
                if s.get("status") == "TRADING"
            }
        return cache[mercado]

    # ─────────────────────────────────────────────
    # Market data (minimal — only what the strategy needs)
    # ─────────────────────────────────────────────

    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        """Devuelve el último precio de mercado para el símbolo indicado."""
        return self.market_data_provider.get_last_price(symbol, market=market)

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> OrderBook:
        """Libro normalizado. Binance manda `[[precio, cantidad], ...]` en texto
        y `lastUpdateId` como número de secuencia, igual en spot y futuros."""
        raw = self.market_data_provider.get_order_book(symbol, limit, market=market)
        return OrderBook.from_levels(
            symbol, market, raw.get("bids", []), raw.get("asks", []),
            depth=limit, update_id=raw.get("lastUpdateId"), raw=raw,
        )

    # ─────────────────────────────────────────────
    # Account data
    # ─────────────────────────────────────────────

    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        """Saldo disponible por activo en el mercado pedido ("spot"/"futures")."""
        return self.account_data_provider.get_balance(market, asset)

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[BrokerPosition]:
        """Posiciones abiertas.

        En FUTUROS son posiciones de verdad (`futures_position_information`),
        con dirección, precio de entrada, apalancamiento y no realizado.
        Antes este método NO las miraba: devolvía los saldos de SPOT mayores
        que cero incluso preguntando por futuros, así que una cuenta con
        cortos abiertos aparecía vacía y una con USDT parado aparecía "en
        posición".
        """
        crudas = self.account_data_provider.get_positions(market, symbol)
        if market == "futures":
            posiciones = [BrokerPosition.from_binance_futures(p) for p in crudas]
        else:
            posiciones = [BrokerPosition.from_spot_balance(p["asset"], float(p["free"])) for p in crudas]
        return [p for p in posiciones if p.quantity > 0]

    def _to_order_info(self, raw: dict, market: str) -> OrderInfo:
        return OrderInfo.from_binance(raw, market)

    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        return self.account_data_provider.set_leverage(symbol, leverage, market)

    # ─────────────────────────────────────────────
    # Order execution
    # ─────────────────────────────────────────────

    def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        """Envía una orden a Binance y devuelve la confirmación y los fills.

        Flujo:
            1. Valida que el modo de trading lo permite (_validate_pre_trade).
            2. Obtiene las reglas de precisión del símbolo (_get_symbol_filters).
            3. Construye el payload para la API (_build_order_payload).
            4a. Si test_order=True: valida sin ejecutar → devuelve status NEW sin fills.
            4b. Si test_order=False: ejecuta la orden real → devuelve status y fills reales.
            5. En caso de error de Binance: devuelve status REJECTED.

        Args:
            order: El OrderRequest a enviar.

        Returns:
            Tuple (OrderAck, list[Fill]):
                - OrderAck: confirmación con el estado de la orden.
                - list[Fill]: ejecuciones reales. Vacío si es test_order o si la orden
                  quedó en estado NEW (pendiente de ejecución, como una LIMIT no ejecutada).
        """
        self._validate_pre_trade(order)
        if order.market == "futures":
            return self._place_futures_order(order)
        filters = self._get_symbol_filters(order.symbol)
        payload = self._build_order_payload(order, filters)

        try:
            if self.test_order:
                # Valida la orden contra la API sin ejecutarla — no genera fills.
                self._client().create_test_order(**payload)
                ack = OrderAck(
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    status=OrderStatus.NEW,
                    raw={"test_order": True},
                )
                return ack, []

            raw = self._client().create_order(**payload)

        except (BinanceAPIException, BinanceOrderException) as exc:
            # Binance rechazó la orden: fondos insuficientes, precisión inválida, etc.
            ack = OrderAck(
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                status=OrderStatus.REJECTED,
                raw={"error": str(exc)},
            )
            return ack, []

        status = self._map_status(raw.get("status", OrderStatus.FILLED.value))
        ack = OrderAck(
            client_order_id=raw.get("clientOrderId", order.client_order_id),
            symbol=raw.get("symbol", order.symbol),
            side=order.side,
            status=status,
            exchange_order_id=str(raw["orderId"]) if raw.get("orderId") is not None else None,
            raw=raw,
        )
        return ack, self._extract_fills(order, raw)

    def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck:
        """Cancela una orden LIMIT pendiente en Binance.

        Útil para cancelar una entrada que no se ejecutó, o para cancelar
        manualmente una orden de TP o SL. Solo funciona con órdenes en estado
        NEW o PARTIALLY_FILLED — las ya ejecutadas no se pueden cancelar.

        Args:
            symbol:   Par de trading (ej: "BTCUSDT").
            order_id: El exchange_order_id devuelto por Binance al colocar la orden.

        Returns:
            OrderAck con status CANCELED si fue exitoso.
        """
        self._validate_mainnet_write()
        raw = self._client().cancel_order(symbol=symbol, orderId=order_id)
        side = Side(raw["side"]) if raw.get("side") in Side._value2member_map_ else Side.BUY
        return OrderAck(
            client_order_id=raw.get("clientOrderId", order_id),
            symbol=raw.get("symbol", symbol),
            side=side,
            status=self._map_status(raw.get("status", OrderStatus.CANCELED.value)),
            exchange_order_id=str(raw["orderId"]) if raw.get("orderId") is not None else order_id,
            raw=raw,
        )

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

        En FUTUROS hay enmienda nativa (`futures_modify_order`): la orden
        conserva su id y nunca deja de existir. Binance exige mandar `side`,
        `quantity` y `price` SIEMPRE, así que se lee antes la orden y se
        rellena lo que el llamador no pasó.

        En SPOT no hay enmienda: la única vía es CANCELAR y volver a
        colocar, lo que produce un `exchange_order_id` NUEVO y deja un hueco
        sin protección entre las dos llamadas.

        ⚠ Este método estaba ROTO y era imposible que funcionara: su cuerpo
        referenciaba `new_quantity`, `new_price`, `new_stop_price` y
        `new_stop_limit_price`, cuatro variables que no existían en ningún
        sitio (la firma era `(symbol, order_id)` a secas), y remataba con
        `OrderStatus.from_binance(...)`, un método que tampoco existe. O sea
        que cancelaba la orden y ACTO SEGUIDO reventaba con `NameError`,
        dejando la orden cancelada y sin sustituta -- una posición
        desprotegida. Nadie lo notó porque nada lo llamaba.

        `tp_price`/`sl_price` no se aceptan acá: en Binance el TP y el SL son
        órdenes PROPIAS (`TAKE_PROFIT_MARKET`/`STOP_MARKET`), no campos de la
        orden de entrada como en Bitunix. Se modifican llamando a este mismo
        método con el id de ESA orden y `stop_price`.
        """
        if tp_price is not None or sl_price is not None:
            raise ValueError(
                "En Binance el TP/SL son órdenes independientes, no campos de la orden de entrada. "
                "Modifícalos llamando a update_order() con el order_id de la propia orden de "
                "TAKE_PROFIT_MARKET/STOP_MARKET y `stop_price=`."
            )

        actual = self.get_order(symbol, order_id, market)
        if not actual.is_open:
            raise ValueError(
                f"La orden {order_id} de {symbol} está en {actual.status.value}: solo se puede "
                "modificar una orden viva."
            )
        lado = (actual.side or Side.BUY).value
        filtros = self._futures_symbol_filters(symbol) if market == "futures" else self._get_symbol_filters(symbol)
        nueva_cantidad = _round_to_step(
            quantity if quantity is not None else actual.quantity, filtros.step_size,
        )
        nuevo_precio = price if price is not None else actual.price

        if market == "futures":
            raw = self._client().futures_modify_order(
                symbol=symbol, orderId=order_id, side=lado,
                quantity=nueva_cantidad,
                price=_round_to_step(nuevo_precio, filtros.tick_size) if nuevo_precio else None,
            )
            return self._ack_from_raw(raw, symbol, actual.side, order_id)

        # Spot: cancelar y recolocar respetando el tipo original.
        self._client().cancel_order(symbol=symbol, orderId=order_id)
        payload = {
            "symbol": symbol,
            "side": lado,
            "type": actual.order_type or "LIMIT",
            "quantity": nueva_cantidad,
        }
        if payload["type"] != "MARKET":
            if nuevo_precio is None:
                raise ValueError(f"Una orden {payload['type']} necesita precio para recolocarse.")
            payload["price"] = _round_to_step(nuevo_precio, filtros.tick_size)
            payload["timeInForce"] = actual.raw.get("timeInForce") or TimeInForce.GTC.value
        nuevo_stop = stop_price if stop_price is not None else actual.stop_price
        if nuevo_stop is not None:
            payload["stopPrice"] = _round_to_step(nuevo_stop, filtros.tick_size)
        raw = self._client().create_order(**payload)
        return self._ack_from_raw(raw, symbol, actual.side, order_id)

    def _ack_from_raw(self, raw: dict, symbol: str, side: Side | None, fallback_id: str) -> OrderAck:
        return OrderAck(
            exchange_order_id=str(raw.get("orderId") or fallback_id),
            client_order_id=raw.get("clientOrderId"),
            symbol=raw.get("symbol", symbol),
            side=side,
            status=self._map_status(raw.get("status", OrderStatus.NEW.value)),
            raw=raw if isinstance(raw, dict) else {},
        )


    # ─────────────────────────────────────────────
    # OCO execution
    # ─────────────────────────────────────────────

    def place_oco_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        take_profit_price: float,
        stop_price: float,
        stop_limit_price: float,
    ) -> tuple[str, OrderAck, OrderAck]:
        """Coloca un OCO (One-Cancels-Other) para cerrar una posición con TP y SL.

        Un OCO coloca dos órdenes simultáneamente en Binance:
            - Una orden LIMIT al precio de take_profit (se ejecuta si el precio sube/baja hasta ahí).
            - Una orden STOP-LIMIT al precio de stop_loss (se ejecuta si el precio va en contra).
        Binance cancela automáticamente la que no se ejecutó cuando la otra sí lo hace.

        Este método debe llamarse DESPUÉS de que la entrada se haya ejecutado
        (verificar que ack.status == OrderStatus.FILLED antes de llamarlo).

        La `side` debe ser el LADO OPUESTO al de la entrada:
            - Si entraste con BUY → side=SELL para cerrar.
            - Si entraste con SELL → side=BUY para cerrar.

        Diferencia entre stop_price y stop_limit_price:
            stop_price:       Precio de activación del stop. Cuando el mercado llega aquí,
                              se activa la orden STOP.
            stop_limit_price: Precio límite de la orden STOP ya activada. Debe ser
                              ligeramente peor que stop_price para asegurar ejecución:
                              - Para SELL: stop_limit_price < stop_price (ej: SL en 49000,
                                stop_limit en 48900)
                              - Para BUY:  stop_limit_price > stop_price

        Args:
            symbol:             Par de trading (ej: "BTCUSDT").
            side:               Dirección de cierre (opuesta a la entrada).
            quantity:           Cantidad a cerrar (igual a la cantidad de la entrada).
            take_profit_price:  Precio objetivo de ganancia (orden LIMIT).
            stop_price:         Precio de activación del stop loss.
            stop_limit_price:   Precio límite de la orden stop (ligeramente peor que stop_price).

        Returns:
            Tuple (oco_list_id, tp_ack, sl_ack):
                - oco_list_id: id del oco. Este coincide con el de la entrada al que pertenece el oco
                - tp_ack: confirmación de la orden LIMIT de take profit.
                - sl_ack: confirmación de la orden STOP-LIMIT de stop loss.
                Ambas tendrán status NEW si Binance las aceptó (quedan pendientes de ejecutarse).

        Ejemplo:
            # Entrada BUY de 0.01 BTC a 50000
            entry = OrderRequest(symbol="BTCUSDT", side=Side.BUY, quantity=0.01)
            ack, fills = broker.place_order(entry)

            # Una vez ejecutada la entrada, colocar el OCO de cierre:
            if ack.status == OrderStatus.FILLED:
                oco_list_id, tp_ack, sl_ack = broker.place_oco_order(
                    symbol="BTCUSDT",
                    side=Side.SELL,          # opuesto a BUY
                    quantity=0.01,
                    take_profit_price=52000, # TP: 4% de ganancia
                    stop_price=49000,        # SL trigger: 2% de pérdida
                    stop_limit_price=48900,  # SL limit: 100 USD por debajo del trigger
                )
        """
        # Reutilizamos _validate_pre_trade creando un OrderRequest temporal solo para la validación
        self._validate_pre_trade(
            OrderRequest(symbol=symbol, side=side, quantity=quantity)
        )
        filters = self._get_symbol_filters(symbol)
        payload = self._build_oco_payload(
            symbol, side, quantity,
            take_profit_price, stop_price, stop_limit_price,
            filters,
        )

        if self.test_order:
            # Binance no tiene endpoint de test para OCO — simulamos las confirmaciones
            oco_list_id = "1"
            tp_ack = OrderAck(
                client_order_id=payload["limitClientOrderId"],
                symbol=symbol,
                side=side,
                status=OrderStatus.NEW,
                raw={"test_order": True, "type": "LIMIT_MAKER"},
            )
            sl_ack = OrderAck(
                client_order_id=payload["stopClientOrderId"],
                symbol=symbol,
                side=side,
                status=OrderStatus.NEW,
                raw={"test_order": True, "type": "STOP_LOSS_LIMIT"},
            )
            return oco_list_id, tp_ack, sl_ack

        try:
            raw = self._client().create_oco_order(**payload)
        except (BinanceAPIException, BinanceOrderException) as exc:
            rejected = OrderAck(
                client_order_id=payload.get("listClientOrderId", ""),
                symbol=symbol,
                side=side,
                status=OrderStatus.REJECTED,
                raw={"error": str(exc)},
            )
            # TRES elementos, como el camino de éxito y como declara la firma.
            # Devolvía DOS, así que un rechazo del exchange reventaba en el
            # desempaquetado del llamador (`oco_id, tp, sl = ...`) con un
            # ValueError que no menciona a Binance por ninguna parte --
            # justo cuando lo que hacía falta era leer el motivo del rechazo.
            return "", rejected, rejected

        oco_list_id = str(raw["orderListId"])
        # La respuesta incluye "orderReports" con las dos órdenes del OCO
        reports = raw.get("orderReports", [])
        tp_report = next((r for r in reports if r.get("type") == "LIMIT_MAKER"), {})
        sl_report = next((r for r in reports if r.get("type") == "STOP_LOSS_LIMIT"), {})

        tp_ack = OrderAck(
            oco_list_id=oco_list_id,
            exchange_order_id=str(tp_report.get("orderId", "")),
            client_order_id=tp_report.get("clientOrderId", payload["limitClientOrderId"]),
            symbol=symbol,
            side=side,
            status=self._map_status(tp_report.get("status", OrderStatus.NEW.value)),
            raw=tp_report,
        )
        sl_ack = OrderAck(
            oco_list_id=oco_list_id,
            exchange_order_id=str(sl_report.get("orderId", "")),
            client_order_id=sl_report.get("clientOrderId", payload["stopClientOrderId"]),
            symbol=symbol,
            side=side,
            status=self._map_status(sl_report.get("status", OrderStatus.NEW.value)),
            raw=sl_report,
        )
        return oco_list_id, tp_ack, sl_ack

    def cancel_oco(self, symbol: str, oco_list_id: int) -> OrderAck:
        """
        Cancela un OCO completo usando su orderListId.
        """
        self._validate_mainnet_write()
        raw = self._client().cancel_order_list(symbol=symbol, orderListId=oco_list_id)
        return OrderAck(
            client_order_id=str(oco_list_id),
            symbol=symbol,
            side=Side.BUY,
            status=OrderStatus.CANCELED,
            raw=raw,
        )

    def update_oco(self, entry: Entry, new_tp: float, new_sl: float, new_sl_limit: float) -> tuple[str, OrderAck, OrderAck]:
        """
        Actualiza el TP/SL de un OCO existente:
        1. Cancela el OCO actual
        2. Crea un nuevo OCO con los nuevos valores
        3. Actualiza los IDs en el Entry
        """

        # 1. Cancelar el OCO actual
        if entry.oco_list_id is not None:
            self.cancel_oco(entry.symbol, entry.oco_list_id)

        # 2. Crear un nuevo OCO — lado opuesto a la entrada para cerrar la posición
        closing_side = Side.SELL if entry.side == Side.BUY else Side.BUY
        oco_list_id, tp_ack, sl_ack = self.place_oco_order(
                symbol=entry.symbol,
                side=closing_side,
                quantity=entry.quantity,
                take_profit_price=new_tp,
                stop_price=new_sl,
                stop_limit_price=new_sl_limit
            )

        return oco_list_id, tp_ack, sl_ack

    # ─────────────────────────────────────────────
    # Symbol filters — one API call per symbol, cached
    # ─────────────────────────────────────────────

    def _get_symbol_filters(self, symbol: str) -> SymbolFilters:
        """Obtiene las reglas de precisión que Binance exige para este símbolo.

        Binance rechaza órdenes que usen más decimales de los permitidos para
        la cantidad (LOT_SIZE) o el precio (PRICE_FILTER). Este método consulta
        esas reglas una vez y las cachea para no repetir la llamada a la API.

        Reglas que extrae:
            LOT_SIZE:    step_size → incremento mínimo de cantidad (ej: 0.001 BTC).
            PRICE_FILTER: tick_size → incremento mínimo de precio (ej: 0.01 USDT).
            MIN_NOTIONAL: min_notional → valor mínimo de la orden en quote (ej: 10 USDT).

        Args:
            symbol: Par de trading (ej: "BTCUSDT").

        Returns:
            SymbolFilters con las reglas del símbolo.
        """
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]

        info = self._client().get_symbol_info(symbol) or {}
        filters = SymbolFilters()

        for f in info.get("filters", []):
            filter_type = f.get("filterType", "")
            if filter_type == "LOT_SIZE":
                filters.step_size = f.get("stepSize", "1")
            elif filter_type == "PRICE_FILTER":
                filters.tick_size = f.get("tickSize", "0.01")
            elif filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
                filters.min_notional = float(f.get("minNotional", 10.0))

        self._filters_cache[symbol] = filters
        return filters

    # ─────────────────────────────────────────────
    # Pre-trade validation
    # ─────────────────────────────────────────────

    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        """Cancelación MASIVA, con el mismo candado que `cancel_order`: la
        versión de `Broker` delega directo en `account_data_provider` y se
        saltaba `_validate_mainnet_write`."""
        self._validate_mainnet_write()
        return super().cancel_open_orders(market, symbol)

    def _validate_mainnet_write(self) -> None:
        """Cancelar en mainnet modifica órdenes REALES de la cuenta (no hay
        "cancelación de prueba"): mismo par de candados que colocar."""
        if not self.testnet and (self.trading_mode != "live" or not self.allow_live_trading):
            raise RuntimeError(
                "Cancelling on Binance mainnet touches real orders. It needs "
                "trading_mode='live' AND allow_live_trading=True "
                f"(got trading_mode={self.trading_mode!r}, allow_live_trading={self.allow_live_trading})."
            )

    def _validate_pre_trade(self, order: OrderRequest) -> None:
        """Bloquea la ejecución si se intenta operar en live sin autorización explícita.

        Es un candado de seguridad doble: el modo debe ser "live" Y
        allow_live_trading debe ser True. Así se evita operar con dinero real
        por error de configuración.

        Una orden mueve dinero real cuando va a mainnet (`testnet=False`) y no
        es una orden de prueba -- los futuros nunca lo son: `_place_futures_order`
        no pasa por `create_test_order`. Esas exigen `trading_mode="live"` Y
        `allow_live_trading=True` sin importar el modo declarado: antes, en
        `"paper"`/`"testnet"` con `testnet=False`, el guard no saltaba.
        """
        if self.trading_mode == "live" and not self.allow_live_trading:
            raise RuntimeError(
                "Live trading is blocked. Set ALLOW_LIVE_TRADING=true to enable."
            )
        real_money = not self.testnet and (order.market == "futures" or not self.test_order)
        if real_money and (self.trading_mode != "live" or not self.allow_live_trading):
            raise RuntimeError(
                "This Binance order would hit mainnet with real money. Placing it needs "
                "trading_mode='live' AND allow_live_trading=True "
                f"(got trading_mode={self.trading_mode!r}, allow_live_trading={self.allow_live_trading})."
            )

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────

    def _build_order_payload(self, order: OrderRequest, filters: SymbolFilters) -> dict:
        """Construye el diccionario de parámetros que espera la API de Binance.

        Aplica las reglas de precisión del símbolo antes de enviar:
        - Redondea la cantidad al step_size permitido.
        - Para órdenes LIMIT: redondea el precio al tick_size.

        Diferencia entre MARKET, LIMIT y STOP_LOSS_LIMIT en el payload:
            MARKET:          solo necesita symbol, side, type, quantity.
            LIMIT:           además necesita price y timeInForce (cuánto dura activa).
            STOP_LOSS_LIMIT: orden de stop independiente (no OCO) -- necesita
                              stopPrice (nivel de activación) además de price
                              y timeInForce, mismo tipo real que Binance Spot
                              soporta fuera de un OCO.

        Args:
            order:   El OrderRequest con los parámetros de la orden.
            filters: Las reglas de precisión del símbolo.

        Returns:
            Dict listo para pasar a `client.create_order(**payload)`.

        Raises:
            ValueError: Si la cantidad redondeada resulta en 0, si una orden
                        LIMIT no especifica precio, o si una STOP_LOSS_LIMIT
                        no especifica precio y/o stop_price.
        """
        # Redondea hacia abajo al múltiplo más cercano del step_size
        quantity = _round_to_step(order.quantity, filters.step_size)
        if quantity <= 0:
            raise ValueError(f"Rounded quantity is 0 for {order.symbol} (step_size={filters.step_size})")

        payload = {
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": quantity,
            "newClientOrderId": order.client_order_id,
        }

        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise ValueError("Limit orders require price")
            payload["price"] = _round_to_step(order.price, filters.tick_size)
            # GTC por defecto: la orden queda activa hasta ejecutarse o cancelarse
            payload["timeInForce"] = order.time_in_force.value if order.time_in_force else "GTC"
        elif order.order_type == OrderType.STOP_LOSS_LIMIT:
            if order.price is None or order.stop_price is None:
                raise ValueError("Stop-loss-limit orders require both price and stop_price")
            payload["price"] = _round_to_step(order.price, filters.tick_size)
            payload["stopPrice"] = _round_to_step(order.stop_price, filters.tick_size)
            payload["timeInForce"] = order.time_in_force.value if order.time_in_force else "GTC"
        elif order.order_type == OrderType.STOP_LOSS:
            if order.stop_price is None:
                raise ValueError("Stop-loss (stop-market) orders require stop_price")
            payload["stopPrice"] = _round_to_step(order.stop_price, filters.tick_size)

        return payload

    # ─────────────────────────────────────────────
    # Futuros (USDT-M)
    # ─────────────────────────────────────────────
    #
    # Hasta ahora este broker NO tenía camino de futuros en absoluto:
    # `place_order` llamaba siempre a `create_order` (el endpoint de SPOT de
    # python-binance) y `_build_order_payload` ni miraba `order.market`. O sea
    # que un grafo con el sub-nodo Binance puesto en "futures" colocaba en
    # SPOT, en silencio -- el mismo activo, otro mercado, otro apalancamiento
    # y otro saldo. Ver `docs/plans/bugs.md`.
    #
    # ⚠ NO PROBADO CONTRA LA API REAL: la cuenta disponible tiene restricción
    # geográfica sobre los endpoints de futuros de Binance. La forma de las
    # llamadas sale de la documentación de USDT-M Futures y de la firma de
    # `python-binance`; hay que validarla contra la API antes de operar con
    # dinero. Los nombres son deliberadamente los de la API (`futures_*`,
    # `reduceOnly`, `closePosition`) para que el contraste sea directo.

    def _futures_symbol_filters(self, symbol: str) -> SymbolFilters:
        """Precisión del símbolo en el mercado de FUTUROS.

        No se puede reusar `_get_symbol_filters`: lee de `get_exchange_info()`
        (spot), y un mismo par tiene `stepSize`/`tickSize` distintos en
        futuros. Usar los de spot es exactamente el tipo de error que Binance
        rechaza con un -1111 (precisión) y que sería un misterio en el log."""
        info = self._client().futures_exchange_info()
        for entry in info.get("symbols", []):
            if entry.get("symbol") != symbol:
                continue
            filters = SymbolFilters()
            for f in entry.get("filters", []):
                filter_type = f.get("filterType", "")
                if filter_type == "LOT_SIZE":
                    # Como STRING, igual que el camino de spot: `_round_to_step`
                    # hace aritmética con `Decimal` para no arrastrar el error
                    # de punto flotante, y un float acá lo tiraría por tierra.
                    filters.step_size = f.get("stepSize", "1")
                elif filter_type == "PRICE_FILTER":
                    filters.tick_size = f.get("tickSize", "0.01")
                elif filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
                    filters.min_notional = float(f.get("notional") or f.get("minNotional") or 0)
            return filters
        raise ValueError(f"'{symbol}' no existe en el mercado de futuros de Binance.")

    def _place_futures_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        """Entrada en USDT-M Futures, con su TP y su SL si vienen pedidos.

        A diferencia de Bitunix, Binance **no** admite adjuntar el TP/SL
        dentro de la orden de entrada: son órdenes propias
        (`TAKE_PROFIT_MARKET` / `STOP_MARKET`) que se mandan después. Se
        colocan con `closePosition=true`, que cierra la posición ENTERA al
        dispararse y hace innecesario repetir la cantidad -- y, sobre todo,
        no puede abrir nada por sí misma, que es la garantía que faltaba en
        el equivalente de Bitunix.

        Si el TP/SL fallan, la entrada YA está colocada: se propaga el error
        (quien llama tiene que enterarse de que la posición quedó sin
        proteger) pero el `OrderAck` de la entrada no se pierde en el camino
        -- va en el mensaje de la excepción."""
        filters = self._futures_symbol_filters(order.symbol)
        quantity = _round_to_step(order.quantity, filters.step_size)
        if quantity <= 0:
            raise ValueError(f"Rounded quantity is 0 for {order.symbol} (step_size={filters.step_size})")

        payload = {
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": quantity,
            "newClientOrderId": order.client_order_id,
        }
        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise ValueError("Limit orders require price")
            payload["price"] = _round_to_step(order.price, filters.tick_size)
            payload["timeInForce"] = order.time_in_force.value if order.time_in_force else "GTC"
        if order.reduce_only:
            # Una SALIDA nunca debe poder abrir posición. En modo cobertura
            # Binance exige `positionSide` en vez de `reduceOnly`; se deja
            # anotado porque este broker todavía no lee el modo de la cuenta.
            payload["reduceOnly"] = "true"

        raw = self._client().futures_create_order(**payload)
        entry_id = str(raw.get("orderId", ""))
        ack = OrderAck(
            exchange_order_id=entry_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.NEW,
            raw=raw if isinstance(raw, dict) else {},
        )

        exit_side = Side.SELL if order.side == Side.BUY else Side.BUY
        for level, order_type in ((order.tp_price, "TAKE_PROFIT_MARKET"),
                                  (order.sl_price, "STOP_MARKET")):
            if level is None:
                continue
            self._client().futures_create_order(
                symbol=order.symbol,
                side=exit_side.value,
                type=order_type,
                stopPrice=_round_to_step(level, filters.tick_size),
                closePosition="true",
                timeInForce="GTE_GTC",
            )
        return ack, []

    def _build_oco_payload(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        take_profit_price: float,
        stop_price: float,
        stop_limit_price: float,
        filters: SymbolFilters,
    ) -> dict:
        """Construye el payload para la API de Binance de una orden OCO.

        Genera IDs únicos con prefijos "tb-tp-" (take profit) y "tb-sl-" (stop loss)
        para poder identificar cada orden en el historial de Binance.
        """
        quantity_r = _round_to_step(quantity, filters.step_size)
        if quantity_r <= 0:
            raise ValueError(f"Rounded quantity is 0 for {symbol} (step_size={filters.step_size})")

        return {
            "symbol": symbol,
            "side": side.value,
            "quantity": quantity_r,
            "price": _round_to_step(take_profit_price, filters.tick_size),
            "stopPrice": _round_to_step(stop_price, filters.tick_size),
            "stopLimitPrice": _round_to_step(stop_limit_price, filters.tick_size),
            "stopLimitTimeInForce": "GTC",
            "limitClientOrderId": f"tb-tp-{uuid4().hex[:16]}",
            "stopClientOrderId": f"tb-sl-{uuid4().hex[:16]}",
        }

    def _extract_fills(self, order: OrderRequest, raw: dict) -> list[Fill]:
        """Parsea los fills devueltos por Binance y los convierte a objetos Fill.

        Las órdenes MARKET se ejecutan frecuentemente contra múltiples precios
        en el order book (diferentes "niveles de liquidez"), generando varios fills.
        Las órdenes LIMIT generalmente generan un solo fill cuando se ejecutan.

        Args:
            order: La orden original enviada.
            raw:   Respuesta cruda de la API de Binance.

        Returns:
            Lista de Fill con los detalles de cada ejecución (precio, cantidad, comisión).
            Lista vacía si la orden no se ejecutó todavía (ej: LIMIT pendiente).
        """
        fills = []
        for idx, item in enumerate(raw.get("fills", []), start=1):
            fills.append(
                Fill(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=float(item["qty"]),
                    price=float(item["price"]),
                    fee=float(item.get("commission", 0.0)),
                    fee_asset=item.get("commissionAsset"),
                    client_order_id=raw.get("clientOrderId", order.client_order_id),
                    exchange_order_id=str(raw["orderId"]) if raw.get("orderId") is not None else None,
                    fill_id=str(item.get("tradeId", f"{order.client_order_id}-fill-{idx}")),
                )
            )
        return fills

    def _map_status(self, status: str) -> OrderStatus:
        """Convierte el string de estado de Binance al enum interno OrderStatus."""
        return OrderStatus.__members__.get(status, OrderStatus.REJECTED)


def _round_to_step(value: float, step: str) -> float:
    """Redondea un valor al múltiplo más cercano del step permitido (hacia abajo).

    Binance rechaza órdenes si la cantidad o el precio tienen más decimales
    de los permitidos por el símbolo. Esta función aplica el redondeo correcto
    usando aritmética de decimales para evitar errores de punto flotante.

    Ejemplo: value=0.12345, step="0.001" → 0.123 (no 0.124, siempre hacia abajo).

    Args:
        value: El valor a redondear (cantidad o precio).
        step:  El incremento mínimo permitido como string (ej: "0.001", "0.01").

    Returns:
        El valor redondeado hacia abajo al múltiplo más cercano del step.
    """
    step_d = decimal.Decimal(step).normalize()
    value_d = decimal.Decimal(str(value))
    rounded = (value_d / step_d).to_integral_value(rounding=decimal.ROUND_DOWN) * step_d
    return float(rounded)
