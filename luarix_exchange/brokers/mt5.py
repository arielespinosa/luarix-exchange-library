from datetime import datetime, timezone
from typing import Callable

import MetaTrader5 as mt5

from luarix_exchange.brokers.base import Broker
from luarix_exchange.capabilities import Capability
from luarix_exchange.models.brokers import BrokerPosition
from luarix_exchange.models.orders import Entry, Fill, OrderAck, OrderRequest, OrderStatus, OrderType
from luarix_exchange.models.orders import Side


class MT5Broker(Broker):
    """Broker que conecta con MetaTrader 5 terminal via paquete oficial MetaTrader5.

    Requisitos:
      MT5 terminal instalado, con sesión iniciada y Algo Trading habilitado (Ctrl+E).
    """

    name = "mt5"
    label = "MetaTrader 5"

    # `place_oco_order` de esta clase SÍ funciona: pone el stop-loss y el
    # take-profit sobre la POSICIÓN y es el servidor de MT5 quien los
    # gestiona -- una sola llamada y exclusividad garantizada, que es
    # exactamente lo que el flag promete, aunque MT5 no lo llame "OCO".
    #
    # Estaba sin declarar, o sea heredando `False` de `Broker`. Y como
    # `supports_independent_orders` también es False, un motor que
    # consultara los dos flags para elegir cómo proteger una posición se
    # quedaba SIN NINGÚN camino válido contra este broker. NativeOco SÍ (y
    # de ahí `supports_oco = True`).
    #
    # MT5 no separa spot de derivados: cada instrumento es lo que es, y todos
    # los métodos ignoran `market`. `markets` queda en solo spot (el default
    # de `Exchange`), que da `supports_futures = False`: la respuesta correcta
    # a "¿tiene una cuenta de futuros aparte?". Sin proveedor de velas ni
    # libro, MARKET_DATA no se soporta.
    unsupported = frozenset({Capability.MARKET_DATA, Capability.DERIVATIVES})
    # Requiere colocar el TP y el SL como órdenes propias, y este broker no
    # sabe hacerlo -- `update_order` ni siquiera modifica.
    supports_independent_orders = False

    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        path: str | None = None,
        magic: int = 123456,
        lot_multiplier: float = 1.0,
    ):
        self._login = login
        self._password = password
        self._server = server
        self._path = path
        self._magic = magic
        self._lot_multiplier = lot_multiplier
        self._connected = False

        self.on_kline: Callable | None = None
        self.on_execution_report: Callable | None = None

    def connect(self) -> None:
        if not mt5.initialize(path=self._path):
            raise ConnectionError(
                f"MT5 initialize failed (error {mt5.last_error()}). "
                "Asegurate de que el terminal MT5 esté abierto."
            )
        if not mt5.login(self._login, password=self._password, server=self._server):
            raise ConnectionError(
                f"MT5 login failed (error {mt5.last_error()}). "
                "Revisá login/password/server."
            )
        self._connected = True

    # ── Market data ────────────────────────────────────────────────────────

    def list_symbols(self, market: str = "spot") -> list[str]:
        symbols = mt5.symbols_get()
        return [s.name for s in symbols] if symbols else []

    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        # `market` se ignora: MT5 no separa spot de derivados, cada
        # instrumento es lo que es.
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return 0.0
        return float((tick.ask + tick.bid) / 2)

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot"):
        # Antes devolvía `{}`, que se lee como "libro vacío".
        raise NotImplementedError("MT5Broker no implementa el libro de órdenes (market depth).")

    # ── Account ────────────────────────────────────────────────────────────

    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        # `market` se ignora: MT5 expone una única cuenta.
        info = mt5.account_info()
        saldo = {"USDT": float(info.balance) if info is not None else 0.0}
        return saldo if not asset else {asset: saldo.get(asset, 0.0)}

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[BrokerPosition]:
        positions = mt5.positions_get(**({"symbol": symbol} if symbol else {}))
        if not positions:
            return []
        return [
            BrokerPosition(
                symbol=pos.symbol,
                # `pos.type` es 0 para compra y 1 para venta -- MT5 no usa
                # texto acá.
                side="LONG" if pos.type == 0 else "SHORT",
                quantity=abs(float(pos.volume)),
                entry_price=float(pos.price_open),
                mark_price=float(pos.price_current or 0),
                unrealized_pnl=float(pos.profit),
                position_id=str(pos.ticket),
                market=market,
                raw={
                    "ticket": pos.ticket,
                    "sl": float(pos.sl) if pos.sl else None,
                    "tp": float(pos.tp) if pos.tp else None,
                    "magic": pos.magic,
                },
            )
            for pos in positions
        ]

    # ── Orders ─────────────────────────────────────────────────────────────

    def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        symbol = order.symbol
        side = order.side

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            ack = OrderAck(symbol=symbol, side=side, status=OrderStatus.REJECTED, raw={"error": "symbol not found"})
            return ack, []

        if side == Side.BUY:
            price = float(tick.ask)
            mt5_type = mt5.ORDER_TYPE_BUY
        else:
            price = float(tick.bid)
            mt5_type = mt5.ORDER_TYPE_SELL

        volume = round(order.quantity * self._lot_multiplier, 2)
        if volume <= 0:
            ack = OrderAck(symbol=symbol, side=side, status=OrderStatus.REJECTED, raw={"error": "volume <= 0"})
            return ack, []

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5_type,
            "price": price,
            "deviation": 10,
            "magic": self._magic,
            "comment": "nexora",
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            ack = OrderAck(
                symbol=symbol, side=side, status=OrderStatus.REJECTED,
                raw={"retcode": result.retcode, "comment": result.comment},
            )
            return ack, []

        # Build ack and fill
        fill_price = float(result.price)
        ack = OrderAck(
            exchange_order_id=str(result.order),
            client_order_id=order.client_order_id,
            symbol=symbol,
            side=side,
            status=OrderStatus.FILLED,
            raw={"ticket": result.order},
        )
        fills = [
            Fill(
                symbol=symbol, side=side,
                quantity=order.quantity,
                price=fill_price,
                fee=0.0,
                exchange_order_id=str(result.order),
                client_order_id=order.client_order_id,
            )
        ]

        # Notificar a la estrategia
        if self.on_execution_report:
            self._emit_fill(ack, fills)

        return ack, fills

    def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck:
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(order_id),
        })
        return OrderAck(
            exchange_order_id=order_id, symbol=symbol,
            status=OrderStatus.CANCELED if result.retcode == mt5.TRADE_RETCODE_DONE else OrderStatus.REJECTED,
            raw={"retcode": result.retcode},
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
        """⚠ NO modifica: cancela. MT5 sí tiene `TRADE_ACTION_MODIFY`, pero
        este broker nunca lo implementó y devolver un ack de cancelación
        como si fuera una modificación sería mentir sobre lo que pasó."""
        if any(v is not None for v in (price, quantity, stop_price, tp_price, sl_price)):
            raise NotImplementedError(
                "MT5Broker.update_order todavía no sabe modificar una orden (haría falta "
                "`TRADE_ACTION_MODIFY`). Sin argumentos, cancela."
            )
        return self.cancel_order(symbol, order_id)

    # ── OCO (SL/TP sobre la posición) ──────────────────────────────────────

    def place_oco_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        take_profit_price: float,
        stop_price: float,
        stop_limit_price: float,
    ) -> tuple[str, OrderAck, OrderAck]:
        # Buscar la posición abierta para este símbolo
        positions = mt5.positions_get(symbol=symbol)
        if positions is None or len(positions) == 0:
            rejected = OrderAck(symbol=symbol, side=side, status=OrderStatus.REJECTED, raw={"error": "no position"})
            return "", rejected, rejected

        position = positions[0]  # tomamos la primera

        # Armar SL y TP según dirección de la posición
        if position.type == 0:  # BUY
            stop_loss = stop_price
            take_profit = take_profit_price
        else:  # SELL
            stop_loss = stop_price
            take_profit = take_profit_price

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": symbol,
            "sl": stop_loss,
            "tp": take_profit,
            "magic": self._magic,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            rejected = OrderAck(
                symbol=symbol, side=side, status=OrderStatus.REJECTED,
                raw={"retcode": result.retcode, "comment": result.comment},
            )
            return "", rejected, rejected

        ack = OrderAck(
            exchange_order_id=str(position.ticket),
            symbol=symbol, side=side,
            status=OrderStatus.NEW,
            raw={"sl": stop_loss, "tp": take_profit},
        )
        return str(position.ticket), ack, ack

    def cancel_oco(self, symbol: str, oco_list_id: int) -> OrderAck:
        positions = mt5.positions_get(symbol=symbol)
        if positions is None or len(positions) == 0:
            return OrderAck(symbol=symbol, status=OrderStatus.CANCELED)

        position = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": symbol,
            "sl": 0.0,
            "tp": 0.0,
            "magic": self._magic,
        }
        mt5.order_send(request)
        return OrderAck(symbol=symbol, status=OrderStatus.CANCELED)

    def update_oco(self, entry: Entry, new_tp: float, new_sl: float, new_sl_limit: float) -> tuple[str, OrderAck, OrderAck]:
        closing_side = Side.SELL if entry.side == Side.BUY else Side.BUY
        return self.place_oco_order(
            symbol=entry.symbol,
            side=closing_side,
            quantity=entry.quantity,
            take_profit_price=new_tp,
            stop_price=new_sl,
            stop_limit_price=new_sl_limit,
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _emit_fill(self, ack: OrderAck, fills: list[Fill]) -> None:
        """Emula el mismo execution report que enviaría Binance para que la estrategia reaccione."""
        if not self.on_execution_report:
            return
        fill = fills[0]
        msg = {
            "e": "executionReport",
            "E": int(datetime.now(timezone.utc).timestamp() * 1000),
            "s": ack.symbol,
            "c": ack.client_order_id or "",
            "S": ack.side.value,
            "o": "MARKET",
            "q": str(fill.quantity),
            "p": str(fill.price),
            "P": "0",
            "F": "0",
            "g": -1,
            "C": "",
            "x": "TRADE",
            "X": "FILLED",
            "r": "NONE",
            "i": ack.exchange_order_id,
            "l": str(fill.quantity),
            "z": str(fill.quantity),
            "L": str(fill.price),
            "n": str(fill.fee),
            "N": "USDT",
            "T": int(datetime.now(timezone.utc).timestamp() * 1000),
            "t": -1,
            "I": ack.exchange_order_id,
        }
        self.on_execution_report(msg)
