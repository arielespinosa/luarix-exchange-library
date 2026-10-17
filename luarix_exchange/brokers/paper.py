from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from time import sleep
from typing import Callable, Optional

import pandas as pd

from luarix_exchange.brokers.base import Broker
from luarix_exchange.models.brokers import BrokerPosition
from luarix_exchange.models.market import Candle
from luarix_exchange.models.orders import (
    Entry,
    Fill,
    OrderAck,
    OrderRequest,
    OrderStatus as CoreOrderStatus,
    OrderType as CoreOrderType,
)
from luarix_exchange.models.orders import Side


# ============================================================================
# ENUMS
# ============================================================================

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class FillPolicy(str, Enum):
    EXACT = "EXACT"
    BINANCE = "BINANCE"
    WORST_CASE = "WORST_CASE"
    BEST_CASE = "BEST_CASE"


# ============================================================================
# ORDER
# ============================================================================

@dataclass(slots=True)
class PaperOrder:

    symbol: str
    side: OrderSide
    type: OrderType
    quantity: Decimal
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: Optional[str] = None
    order_id: Optional[int] = None
    order_list_id: int = -1
    status: OrderStatus = OrderStatus.NEW
    executed_qty: Decimal = Decimal("0")
    cumulative_quote_qty: Decimal = Decimal("0")
    fill_price: Optional[Decimal] = None
    transact_time: Optional[datetime] = None
    working_time: Optional[datetime] = None
    commission: Decimal = Decimal("0")
    commission_asset: Optional[str] = None
    is_oco_leg: bool = False
    parent_oco_id: Optional[int] = None
    metadata: dict = field(default_factory=dict)

    @property
    def remaining_qty(self) -> Decimal:
        return self.quantity - self.executed_qty

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_closed(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


# ============================================================================
# OCO
# ============================================================================

@dataclass(slots=True)
class PaperOCO:

    list_id: int
    symbol: str
    tp_order: PaperOrder
    sl_order: PaperOrder
    active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)

    def cancel(self):
        self.active = False
        if self.tp_order.is_open:
            self.tp_order.status = OrderStatus.CANCELED
        if self.sl_order.is_open:
            self.sl_order.status = OrderStatus.CANCELED


# ============================================================================
# POSITION
# ============================================================================

@dataclass(slots=True)
class PaperPosition:

    symbol: str
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    order_id: int
    oco_id: Optional[int] = None
    closed: bool = False
    exit_price: Optional[Decimal] = None
    exit_time: Optional[datetime] = None
    pnl: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PositionExit:
    """Salida de la posición al estilo `strategy.exit(loss=, trail_points=, trail_offset=)` de TradingView, en
    DISTANCIAS de precio desde el precio de entrada (Pine las da en ticks: `ticks × mintick`).

    - `loss`: stop loss a `entrada ∓ loss`.
    - `trail_points` + `trail_offset`: trailing stop que se activa cuando el precio llega a
      `entrada ± trail_points` y desde ahí sigue al extremo favorable a `trail_offset`.

    Vale para UNA vela (la siguiente a la que se pide): se re-emite en cada vela, como el `strategy.exit` de las
    estrategias integradas. Ver `PaperBroker.set_position_exit`.
    """

    loss: float | None = None
    trail_points: float | None = None
    trail_offset: float | None = None

    def __post_init__(self):
        if self.loss is None and self.trail_points is None:
            raise ValueError("PositionExit necesita `loss`, `trail_points` o ambos.")
        if (self.trail_points is None) != (self.trail_offset is None):
            raise ValueError("`trail_points` y `trail_offset` van juntos.")
        for name in ("loss", "trail_points", "trail_offset"):
            value = getattr(self, name)
            if value is not None and not value > 0:
                raise ValueError(f"`{name}` debe ser una distancia positiva, no {value!r}.")


# ============================================================================
# ENGINE STATE
# ============================================================================

@dataclass(slots=True)
class EngineState:

    next_order_id: int = 1
    next_oco_id: int = 1
    pending_orders: list[PaperOrder] = field(default_factory=list)
    filled_orders: list[PaperOrder] = field(default_factory=list)
    canceled_orders: list[PaperOrder] = field(default_factory=list)
    rejected_orders: list[PaperOrder] = field(default_factory=list)
    oco_orders: list[PaperOCO] = field(default_factory=list)
    positions: list[PaperPosition] = field(default_factory=list)

    def generate_order_id(self) -> int:
        oid = self.next_order_id
        self.next_order_id += 1
        return oid

    def generate_oco_id(self) -> int:
        oid = self.next_oco_id
        self.next_oco_id += 1
        return oid


def _fmt(value) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


@dataclass(frozen=True, slots=True)
class ParsedExecutionReport:
    """Un execution report (mismo shape Binance que emite
    `ExecutionReportFactory.build`) traducido a campos tipados -- evita que
    cada consumidor (backtesting, dispatch en vivo) tenga que acordarse de
    las claves de una letra del wire format.

    Parameters
    ----------
    execution_type:
        Tipo de evento (``"NEW"``, ``"TRADE"``, ``"CANCELED"``, ...).
    symbol:
        El par operado (`s` del wire format).
    order_id:
        Identificador de la orden en el broker (simulado o real).
    oco_list_id:
        Identificador del grupo OCO al que pertenece esta orden, `None` si
        no es parte de uno.
    side:
        Lado de la orden tal como lo reporta el broker (``"BUY"``/``"SELL"``).
    direction:
        `side` traducido a la nomenclatura de posición (``"LONG"``/``"SHORT"``).
    order_type:
        Tipo de orden (``"MARKET"``, ``"LIMIT"``, ...).
    event_time:
        Instante del evento, en UTC.
    commission_paid:
        Comisión real cobrada en este evento, en la moneda de cotización.
        `0.0` en eventos que no representan un fill (``NEW``/``CANCELED``).
    fill_price:
        Precio de ejecución de este evento (`L`, "last price" del wire
        format). `0.0` en eventos que no representan un fill.
    quantity:
        Cantidad ejecutada acumulada de la orden (`z`, "cumulative filled
        quantity"). `0.0` en eventos que no representan un fill.
    client_order_id:
        Identificador que el llamador asignó a la orden (`c` del wire
        format). `None` si la orden no tiene uno (`build()` lo emite vacío).
    closes_order_id, closes_client_order_id:
        Solo en el fill que cierra una posición (`position_mode="single"`):
        el `order_id`/`client_order_id` de la entrada que cierra.
    close_reason:
        Por qué se cerró, si no fue una señal: `"SL"` cuando la cerró una
        `PositionExit` (stop loss o trailing stop). `None` = señal (reversión
        o `reduce_only`).
    """

    execution_type: str
    symbol: str
    order_id: str
    oco_list_id: str | None
    side: str
    direction: str
    order_type: str
    event_time: datetime
    commission_paid: float
    fill_price: float
    quantity: float
    client_order_id: str | None = None
    closes_order_id: str | None = None
    closes_client_order_id: str | None = None
    close_reason: str | None = None

    @property
    def is_position_close(self) -> bool:
        """`True` si este fill CIERRA la entrada `closes_order_id` (solo en
        `position_mode="single"`: reversión o `reduce_only`)."""
        return self.execution_type == "TRADE" and self.closes_order_id is not None

    @property
    def is_new_entry_signal(self) -> bool:
        """`True` si este evento es la apertura de una orden nueva que no
        forma parte de un OCO -- la señal de entrada de un trade.
        """
        return self.execution_type == "NEW" and self.oco_list_id is None

    @property
    def is_oco_leg_cancellation(self) -> bool:
        """`True` si este evento es la cancelación automática de la pata
        perdedora de un OCO (el TP o SL que no se ejecutó porque la otra
        pata ya cerró la posición).
        """
        return self.execution_type == "CANCELED" and self.oco_list_id is not None

    @property
    def is_trade_fill(self) -> bool:
        """`True` si este evento es un fill real (entrada o salida)."""
        return self.execution_type == "TRADE"

    @property
    def is_rejected_entry(self) -> bool:
        """`True` si esta orden se canceló/expiró/rechazó SIN llegar a
        llenar -- pyramid-1, `reduce_only` sin posición, `candles_left`
        agotado (`_expire_orders_out_of_candles`), o cancelada a mano.
        Distinto de `is_oco_leg_cancellation` (esa es la pata PERDEDORA de un
        OCO cuya hermana SÍ llenó -- acá no hay ningún fill de por medio, la
        señal de entrada simplemente nunca se volvió posición real). El
        caller (`BacktestStats.on_cancel`) usa esto para descartar el
        registro `on_signal` que nunca llegó a `on_fill` -- si no, queda como
        una operación "abierta" fantasma para siempre."""
        return self.execution_type in ("CANCELED", "EXPIRED", "REJECTED") and self.oco_list_id is None


class ExecutionReportFactory:
    """Genera el mismo diccionario que envía Binance mediante el UserDataStream."""

    @staticmethod
    def build(order: PaperOrder, execution_type: str, event_time: datetime | None = None) -> dict:
        if event_time is None:
            event_time = datetime.utcnow()

        last_price = order.fill_price or order.price or Decimal("0")
        last_qty = order.executed_qty

        report = {
            "e": "executionReport",
            "E": int(event_time.timestamp() * 1000),
            "s": order.symbol,
            "c": order.client_order_id or "",
            "S": order.side.value,
            "o": order.type.value,
            "f": order.time_in_force.value,
            "q": _fmt(order.quantity),
            "p": _fmt(order.price),
            "P": _fmt(order.stop_price),
            "F": "0",
            "g": order.order_list_id,
            "C": "",
            "x": execution_type,
            "X": order.status.value,
            "r": "NONE",
            "i": order.order_id,
            "l": _fmt(last_qty),
            "z": _fmt(order.executed_qty),
            "L": _fmt(last_price),
            "n": _fmt(order.commission),
            "N": order.commission_asset,
            "T": int((order.transact_time or event_time).timestamp() * 1000),
            "t": -1,
            "I": order.order_id,
            "w": order.is_open,
            "m": False,
            "M": False,
            "O": int((order.working_time or event_time).timestamp() * 1000),
            "Z": _fmt(order.cumulative_quote_qty),
            "Y": _fmt(last_qty * last_price),
            "Q": "0",
            "W": int((order.working_time or event_time).timestamp() * 1000),
            "V": "NONE",
        }
        # Claves propias (no existen en Binance): solo en la orden que CIERRA
        # una posición en `position_mode="single"`, para que el consumidor sepa
        # qué entrada terminó sin reconstruirlo. Ausentes en cualquier otro report.
        if order.metadata.get("closes_order_id") is not None:
            report["closes"] = str(order.metadata["closes_order_id"])
            report["closes_c"] = order.metadata.get("closes_client_order_id") or ""
            if order.metadata.get("close_reason"):
                report["closes_r"] = order.metadata["close_reason"]
        return report

    @staticmethod
    def parse(report: dict) -> ParsedExecutionReport:
        """El inverso de `build()`: traduce un execution report ya emitido de
        vuelta a un `ParsedExecutionReport` tipado.

        La comisión (`commission_paid`) se deriva de `n` (comisión en el
        activo base) × `L` (precio de ejecución) -- lo que de verdad salió
        de la caja en la moneda de cotización, igual que `_fill` la
        descuenta del balance. Vale `0.0` en reports que no representan un
        fill (`n` viene en `"0"` para esos, ver `build()`).
        """
        side = report["S"]
        oco_list_id = str(report["g"]) if report.get("g") not in (-1, None) else None
        try:
            fill_price = float(report.get("L") or 0)
        except (TypeError, ValueError):
            fill_price = 0.0
        try:
            quantity = float(report.get("z") or 0)
        except (TypeError, ValueError):
            quantity = 0.0
        try:
            commission_paid = float(report.get("n") or 0) * fill_price
        except (TypeError, ValueError):
            commission_paid = 0.0
        return ParsedExecutionReport(
            execution_type=report["x"],
            symbol=report["s"],
            order_id=str(report["i"]),
            oco_list_id=oco_list_id,
            side=side,
            direction="LONG" if side == "BUY" else "SHORT",
            order_type=report["o"],
            event_time=datetime.fromtimestamp(report["T"] / 1000, tz=timezone.utc),
            commission_paid=commission_paid,
            fill_price=fill_price,
            quantity=quantity,
            client_order_id=report.get("c") or None,
            closes_order_id=report.get("closes") or None,
            closes_client_order_id=report.get("closes_c") or None,
            close_reason=report.get("closes_r") or None,
        )

    @staticmethod
    def new(order: PaperOrder):
        return ExecutionReportFactory.build(order, execution_type="NEW")

    @staticmethod
    def fill(order: PaperOrder):
        return ExecutionReportFactory.build(order, execution_type="TRADE")

    @staticmethod
    def cancel(order: PaperOrder):
        return ExecutionReportFactory.build(order, execution_type="CANCELED")

    @staticmethod
    def reject(order: PaperOrder):
        return ExecutionReportFactory.build(order, execution_type="REJECTED")

    @staticmethod
    def expire(order: PaperOrder):
        return ExecutionReportFactory.build(order, execution_type="EXPIRED")


# ============================================================================
# PAPER BROKER
# ============================================================================

class PaperBroker(Broker):
    """Simulador de broker que imita la interfaz de BinanceBroker para backtesting.

    No consulta datos de mercado. El precio actual se actualiza llamando a on_candle()
    en cada iteración del backtest. Las órdenes LIMIT se llenan cuando la vela
    toca el precio objetivo.
    """
    DATA_PATH = "backtests_data"
    supports_independent_orders = True
    # El simulador implementa OCO de verdad (`place_oco_order` + la
    # cancelación de la pierna hermana en `_cancel_other_leg`), así que se
    # comporta como Binance y no como Bitunix.
    supports_oco = True

    #: `hedge`: cada fill que no es pata de OCO es una entrada independiente
    #: (comportamiento histórico). `single`: una sola posición por símbolo,
    #: con la semántica de `strategy.entry` de TradingView y pirámide 1 -- una
    #: orden contraria cierra la posición y abre la opuesta con su propia
    #: cantidad; una en la misma dirección se rechaza; `reduce_only` solo cierra;
    #: `close_opposite` (`strategy.order`) abre sin posición y solo cierra una contraria.
    POSITION_MODES = ("hedge", "single")
    #: `signal_close`: una MARKET es una LIMIT interna al cierre de la vela de
    #: la señal y solo llena si el precio se vuelve a tocar (histórico).
    #: `next_open`: una MARKET se llena a la apertura de la vela siguiente,
    #: como el emulador de TradingView.
    FILL_MODELS = ("signal_close", "next_open")

    def __init__(
        self,
        initial_balance: float = 10_000.0,
        commission: float = 0.001,
        on_kline: Callable | None = None,
        on_execution_report: Callable | None = None,
        speed: float = 0.2,
        position_mode: str = "hedge",
        fill_model: str = "signal_close",
    ):
        if position_mode not in self.POSITION_MODES:
            raise ValueError(f"position_mode desconocido: {position_mode!r} (válidos: {self.POSITION_MODES})")
        if fill_model not in self.FILL_MODELS:
            raise ValueError(f"fill_model desconocido: {fill_model!r} (válidos: {self.FILL_MODELS})")
        self.position_mode = position_mode
        self.fill_model = fill_model
        self.state = EngineState()
        #: símbolo (mayúsculas) -> (salida, `order_id` de la entrada que cubre o `None` = la que se abra)
        self._position_exits: dict[str, tuple[PositionExit, int | None]] = {}
        self._balance = initial_balance
        self._commission = commission
        self._last_close: float = 0.0
        self.on_execution_report = on_execution_report
        self.on_kline = on_kline
        self.on_candle_processed = None
        self._candles: dict[str, dict[str, pd.DataFrame]] = {}
        self.speed = speed

    @classmethod
    def options_from_config(cls, config: dict | None) -> dict:
        """Traduce la config del nodo `paperbroker` del grafo a kwargs del
        constructor. Es la única traducción: la usan los dos motores
        (Django y orquestador), así que no pueden interpretarla distinto.

        `commission_pct` va en porcentaje (0,1 = 0,1 %). Vacío o ausente = el
        default histórico; un valor explícito inválido levanta `ValueError`
        en vez de correr el backtest con otra cosa en silencio."""
        config = config or {}
        options = {"commission": 0.001, "position_mode": "hedge", "fill_model": "signal_close"}

        raw_commission = config.get("commission_pct")
        if raw_commission not in (None, ""):
            try:
                commission_pct = float(raw_commission)
            except (TypeError, ValueError):
                raise ValueError(f"commission_pct no es un número: {raw_commission!r}") from None
            if commission_pct < 0:
                raise ValueError(f"commission_pct no puede ser negativa: {commission_pct}")
            options["commission"] = commission_pct / 100

        for key, valid in (("position_mode", cls.POSITION_MODES), ("fill_model", cls.FILL_MODELS)):
            value = config.get(key)
            if value in (None, ""):
                continue
            if value not in valid:
                raise ValueError(f"{key} desconocido: {value!r} (válidos: {valid})")
            options[key] = value
        return options

    def list_active(self) -> list:
        return list(self._candles.keys())

    # ── Carga de datos históricos ────────────────────────────────────────────

    def load_csv(self, symbol: str, timeframe: str, filepath: str):
        """Carga velas desde un CSV y las almacena internamente."""
        df = pd.read_csv(filepath)
        if 'Unnamed: 0' in df.columns:
            df = df.drop(columns='Unnamed: 0')
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
        self._candles.setdefault(symbol, {})[timeframe] = df

    def get_historical_data(
            self, 
            symbol: str, 
            timeframe: str,
            start: datetime | None = None,
            limit: int | None = None
        ) -> pd.DataFrame:
        """Devuelve velas históricas almacenadas."""
        df = self._candles.get(symbol, {}).get(timeframe)
        if df is None:
            return pd.DataFrame()
        result = df.copy()
        if start is not None:
            result = result[result["date"] > start]
        if limit is not None:
            result = result.head(limit)
        return result

    def prepare_data(self, symbol: str, data_path: str | None = None) -> None:
        """Carga los CSVs de 4h y 1d para un símbolo.

        Cada broker sabe cómo preparar sus propios datos de mercado.
        PaperBroker los lee de disco; BinanceBroker los bajaría de la API, etc.
        """
        base = data_path or self.DATA_PATH
        self.load_csv(symbol, "4h", f"{base}/{symbol}/4h.csv")
        self.load_csv(symbol, "1d", f"{base}/{symbol}/1d.csv")

    # ── Broker ABC: conexión y mercado ────────────────────────────────────────

    def connect(self) -> None:
        pass

    def get_ticker(self, symbol: str, market: str = "spot") -> float:
        return self._last_close

    def get_order_book(self, symbol: str, limit: int = 20, market: str = "spot") -> dict:
        return {}

    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        # `market` se ignora: el broker simulado tiene una sola bolsa de
        # capital, no cuentas separadas de spot y futuros.
        saldo = {"USDT": self._balance}
        # Mismo contrato que los brokers reales: un activo pedido y ausente
        # vale 0.0, no desaparece del resultado.
        return saldo if not asset else {asset: saldo.get(asset, 0.0)}

    def _open_position_entries(self) -> list["PaperOrder"]:
        """Las órdenes de ENTRADA (no piernas de OCO) que siguen abiertas --
        `EngineState.positions`/`PaperPosition` se declaran pero NUNCA se
        instancian en código de producción (confirmado con `grep`
        exhaustivo del repo: el único uso es un test que las arma a mano) --
        `get_positions()`/`mark_to_market_equity()` leían de ahí y por eso
        siempre veían el simulador vacío, sin importar cuántas posiciones
        reales hubiera. Ver `docs/plans/bugs.md`.

        Reconstruye desde `state.filled_orders`/`state.oco_orders`, que SÍ
        se populan de verdad (`_fill()`) -- mismo criterio, portado tal
        cual, que `workflows/execution/operations_snapshot.py::_pair_entries_with_ocos`
        en `luarix-web-server` (que ya evitaba `get_positions()` por este
        motivo): empareja cada entrada llenada con la próxima OCO creada
        para el mismo símbolo, en orden cronológico -- `PaperOrder` no
        tiene un FK estructural hacia la OCO que la protege (`place_order()`/
        `place_oco_order()` los arma un `NodeAction` distinto cada uno, sin
        enlazarlos). Una entrada sin ninguna OCO asociada (sin
        stop-loss/take-profit conectado) queda abierta indefinidamente --
        mismo comportamiento ya documentado en `workflow.backtesting.position_tracking`
        del lado del orchestrator.
        """
        # Las órdenes que CIERRAN una posición (`position_mode="single"`) no
        # son entradas. Una entrada ya cerrada sí sigue en la lista hasta
        # después del emparejamiento: si se quitara antes, la entrada siguiente
        # se emparejaría con el OCO (cancelado) de la cerrada.
        entries = sorted(
            (order for order in self.state.filled_orders
             if not order.is_oco_leg and order.metadata.get("closes_order_id") is None),
            key=lambda order: order.transact_time or order.working_time or datetime.min,
        )
        ocos_by_symbol: dict[str, list[PaperOCO]] = {}
        for oco in sorted(self.state.oco_orders, key=lambda oco: oco.created_at):
            ocos_by_symbol.setdefault(oco.symbol, []).append(oco)

        cursor: dict[str, int] = {}
        open_entries: list[PaperOrder] = []
        for entry in entries:
            available = ocos_by_symbol.get(entry.symbol, [])
            index = cursor.get(entry.symbol, 0)
            matched = available[index] if index < len(available) else None
            if matched is not None:
                cursor[entry.symbol] = index + 1
            if (matched is None or matched.active) and not entry.metadata.get("closed"):
                open_entries.append(entry)
        return open_entries

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[BrokerPosition]:
        """Las posiciones abiertas del simulador, con la MISMA forma que las
        de un broker real.

        Antes devolvía `{símbolo: cantidad}`, que no se parecía a lo que
        devuelve ningún otro broker -- o sea que un grafo que leyera
        posiciones funcionaba en simulación y fallaba en real, o al revés.
        """
        posiciones = [
            BrokerPosition(
                symbol=entry.symbol,
                # El lado de la orden de ENTRADA es BUY/SELL y su `quantity`
                # siempre positiva, igual que en Bitunix -- la dirección va
                # aparte.
                side="LONG" if entry.side == OrderSide.BUY else "SHORT",
                quantity=abs(float(entry.quantity)),
                entry_price=float(entry.fill_price) if entry.fill_price is not None else 0.0,
                mark_price=self._last_close,
                market=market,
                raw={"paper": True},
            )
            for entry in self._open_position_entries()
            if symbol is None or entry.symbol == symbol
        ]
        return [p for p in posiciones if p.quantity > 0]

    def mark_to_market_equity(self, symbol: str, price: float) -> float:
        """El capital de verdad: efectivo MÁS el valor de mercado de lo que
        sigue abierto para `symbol`, valorado a `price`.

        `get_balance()["USDT"]` a secas NO es el capital: es la caja. Este
        simulador opera al contado, así que VENDER acredita el importe en
        USDT en el acto y COMPRAR lo debita (`_fill`). Una posición corta
        todavía abierta deja por tanto la caja inflada con el producto de
        una venta que aún no se ha recomprado.

        Caso real que lo destapó: una corrida que terminó con UN corto
        abierto de 0,045 BTC @ 66.699 informaba `final_capital = 12.998`
        sobre 10.000 iniciales -- +30% y un APR del 198% con **cero
        operaciones cerradas**. Esos 2.998 eran, exactamente, 0,045 ×
        66.699: el importe de la venta pendiente de recomprar. Dinero que no
        existía.

        Con la posición valorada a precio de mercado, esa misma corrida da
        ~9.997: los 10.000 menos la comisión de abrir. Que es lo correcto --
        abrir una posición no gana ni pierde nada todavía.

        `price` es el cierre de la vela actual. Solo se valoran las
        posiciones de `symbol`: un consumidor multi-par necesita el precio
        de cada uno y llamar esto una vez por símbolo.

        **Corrección 2026-08-13**: leía `self.state.positions`, que nunca se
        popula en producción (ver `_open_position_entries`) -- SIEMPRE
        devolvía solo la caja, nunca el valor de mercado de lo abierto. El
        caso real documentado abajo (el corto de 0,045 BTC) se verificaba
        contra un `PaperPosition` armado a mano en el test, no contra una
        corrida real -- el bug de fondo seguía ahí. Ahora reconstruye desde
        `state.filled_orders`/`state.oco_orders`, igual que `get_positions()`.
        """
        efectivo = self._balance
        simbolo = symbol.upper()
        exposicion = 0.0
        for entry in self._open_position_entries():
            if entry.symbol.upper() != simbolo:
                continue
            cantidad = float(entry.quantity)
            lado = str(getattr(entry.side, "value", entry.side)).upper()
            # Un largo TIENE el activo (suma); un corto lo DEBE (resta). La
            # caja ya lleva la pata contraria de la entrada, así que esto la
            # compensa y deja el no realizado como única diferencia.
            exposicion += cantidad * price if lado == "BUY" else -cantidad * price
        return efectivo + exposicion

    # ── Broker ABC: órdenes ───────────────────────────────────────────────────

    def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        side = OrderSide(order.side.value)
        qty = Decimal(str(order.quantity))
        price = Decimal(str(order.price)) if order.price else None
        # Históricamente esto ignoraba `order.order_type` por completo (MARKET
        # también terminaba como LIMIT interno -- comportamiento pre-existente,
        # no se toca). La única rama nueva es STOP_LOSS_LIMIT, para una orden
        # de stop independiente (no ligada a un OCO) -- ver
        # workflows/execution/position_manager.py.
        stop_price = None
        if order.order_type == CoreOrderType.STOP_LOSS_LIMIT:
            order_type = OrderType.STOP_LOSS_LIMIT
            stop_price = Decimal(str(order.stop_price)) if order.stop_price is not None else None
        elif order.order_type == CoreOrderType.STOP_LOSS:
            # Stop a mercado de ENTRADA (`strategy.entry(..., stop=...)`).
            if order.stop_price is None:
                raise ValueError("Una orden STOP_LOSS necesita stop_price")
            order_type = OrderType.STOP_LOSS
            stop_price = Decimal(str(order.stop_price))
            price = None
        elif order.order_type == CoreOrderType.MARKET and self.fill_model == "next_open":
            # Sin precio: `_fill` la ejecuta a la apertura de la próxima vela.
            order_type = OrderType.MARKET
            price = None
        else:
            order_type = OrderType.LIMIT
            if price is None:
                # Una orden MARKET llega aca sin `price` (se trata como LIMIT
                # interno, ver el comentario de arriba) -- `_filled()` hace
                # `float(order.price)` sin resguardo para esta rama, asi que
                # dejarlo en `None` revienta con TypeError en el primer
                # `on_candle()` que la intente emparejar. `_last_close` es el
                # cierre de la vela que se esta procesando AHORA MISMO (se
                # setea al principio de `on_candle`, antes de la cola de
                # matching) -- ejecutarla "a mercado" contra ese precio es
                # exactamente lo que el nombre promete.
                price = Decimal(str(self._last_close))

        paper_order = PaperOrder(
            symbol=order.symbol,
            side=side,
            type=order_type,
            quantity=qty,
            price=price,
            stop_price=stop_price,
            client_order_id=order.client_order_id,
            order_id=self.state.generate_order_id(),
            status=OrderStatus.NEW,
            working_time=datetime.utcnow(),
            metadata=self._paper_metadata(order),
        )

        self.state.pending_orders.append(paper_order)
        self._emit(ExecutionReportFactory.new(paper_order))

        return OrderAck(
            exchange_order_id=str(paper_order.order_id),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            status=CoreOrderStatus.NEW,
        ), []

    @staticmethod
    def _paper_metadata(order: OrderRequest) -> dict:
        """Lo que el simulador necesita recordar de la petición: `reduce_only`,
        `close_opposite` y `valid_candles` (expira si no llenó tras N velas
        evaluadas)."""
        metadata: dict = {}
        if order.reduce_only:
            metadata["reduce_only"] = True
        if (order.metadata or {}).get("close_opposite"):
            metadata["close_opposite"] = True
        valid_candles = (order.metadata or {}).get("valid_candles")
        if valid_candles:
            metadata["candles_left"] = int(valid_candles)
        return metadata

    def get_order_status(self, symbol: str, order_id: str, market: str = "spot") -> CoreOrderStatus:
        """El simulador tiene la verdad en memoria -- no hace falta derivarla
        de la lista de pendientes como en `Broker.get_order_status`."""
        for order in self.state.pending_orders:
            if str(order.order_id) == str(order_id):
                return CoreOrderStatus.NEW
        for order in self.state.filled_orders:
            if str(order.order_id) == str(order_id):
                return CoreOrderStatus.FILLED
        return CoreOrderStatus.CANCELED

    def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck:
        for order in self.state.pending_orders:
            if str(order.order_id) != order_id:
                continue
            order.status = OrderStatus.CANCELED
            self.state.pending_orders.remove(order)
            self._emit(ExecutionReportFactory.cancel(order))
            return OrderAck(
                exchange_order_id=order_id,
                symbol=symbol,
                side=Side(order.side.value),
                status=CoreOrderStatus.CANCELED,
            )
        return OrderAck(symbol=symbol, status=CoreOrderStatus.REJECTED)

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
        """Modifica una orden pendiente del simulador.

        Se hace IN SITU sobre la orden: acá no hay ninguna razón para
        cancelar y recolocar (que es lo único que permite el spot de un
        exchange real), y hacerlo cambiaría el `order_id` y rompería
        cualquier seguimiento del backtest.

        Sin ningún valor nuevo, cancela -- que es lo que este método hacía
        SIEMPRE antes, ignorando por completo que se le pidiera una
        modificación.
        """
        if price is None and quantity is None and stop_price is None:
            return self.cancel_order(symbol, order_id)

        for order in self.state.pending_orders:
            if str(order.order_id) != str(order_id):
                continue
            if price is not None:
                order.price = Decimal(str(price))
            if quantity is not None:
                order.quantity = Decimal(str(quantity))
            if stop_price is not None:
                order.stop_price = Decimal(str(stop_price))
            return OrderAck(
                exchange_order_id=str(order_id),
                client_order_id=order.client_order_id,
                symbol=symbol,
                side=Side(order.side.value),
                status=CoreOrderStatus.NEW,
                raw={"paper": True, "updated": True},
            )
        return OrderAck(symbol=symbol, status=CoreOrderStatus.REJECTED,
                        raw={"error": f"no hay ninguna orden pendiente con id {order_id}"})

    # ── Broker ABC: OCO ───────────────────────────────────────────────────────

    def place_oco_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        take_profit_price: float,
        stop_price: float,
        stop_limit_price: float,
    ) -> tuple[str, OrderAck, OrderAck]:
        list_id = self.state.generate_oco_id()
        paper_side = OrderSide(side.value)
        qty = Decimal(str(quantity))
        now = datetime.utcnow()

        tp = PaperOrder(
            symbol=symbol,
            side=paper_side,
            type=OrderType.LIMIT,
            quantity=qty,
            price=Decimal(str(take_profit_price)),
            order_id=self.state.generate_order_id(),
            order_list_id=list_id,
            is_oco_leg=True,
            working_time=now,
        )
        sl = PaperOrder(
            symbol=symbol,
            side=paper_side,
            type=OrderType.STOP_LOSS_LIMIT,
            quantity=qty,
            price=Decimal(str(stop_limit_price)),
            stop_price=Decimal(str(stop_price)),
            order_id=self.state.generate_order_id(),
            order_list_id=list_id,
            is_oco_leg=True,
            working_time=now,
        )

        oco = PaperOCO(list_id=list_id, symbol=symbol, tp_order=tp, sl_order=sl)
        self.state.oco_orders.append(oco)
        self.state.pending_orders.append(tp)
        self.state.pending_orders.append(sl)

        self._emit(ExecutionReportFactory.new(tp))
        self._emit(ExecutionReportFactory.new(sl))

        tp_ack = OrderAck(
            exchange_order_id=str(tp.order_id),
            symbol=symbol,
            side=side,
            status=CoreOrderStatus.NEW,
        )
        sl_ack = OrderAck(
            exchange_order_id=str(sl.order_id),
            symbol=symbol,
            side=side,
            status=CoreOrderStatus.NEW,
        )
        return str(list_id), tp_ack, sl_ack

    def cancel_oco(self, symbol: str, oco_list_id: int) -> OrderAck:
        target_id = int(oco_list_id)
        for oco in self.state.oco_orders:
            if oco.list_id != target_id:
                continue
            for leg in (oco.tp_order, oco.sl_order):
                if leg in self.state.pending_orders:
                    leg.status = OrderStatus.CANCELED
                    self.state.pending_orders.remove(leg)
                    self._emit(ExecutionReportFactory.cancel(leg))
            oco.active = False
            return OrderAck(
                exchange_order_id=str(oco_list_id),
                symbol=symbol,
                side=Side(oco.tp_order.side.value),
                status=CoreOrderStatus.CANCELED,
            )
        return OrderAck(symbol=symbol, status=CoreOrderStatus.REJECTED)

    def update_oco(
        self,
        entry: "Entry",
        new_tp: float,
        new_sl: float,
        new_sl_limit: float,
    ) -> tuple[str, OrderAck, OrderAck]:
        if entry.oco_list_id is not None:
            self.cancel_oco(entry.symbol, int(entry.oco_list_id))
        closing_side = Side.SELL if entry.side == Side.BUY else Side.BUY
        return self.place_oco_order(
            symbol=entry.symbol,
            side=closing_side,
            quantity=entry.quantity,
            take_profit_price=new_tp,
            stop_price=new_sl,
            stop_limit_price=new_sl_limit,
        )

    # ── Backtesting ───────────────────────────────────────────────────────────

    def on_candle(self, symbol:str, interval:str, tick: pd.Series):
        """Avanza la simulación con una nueva vela. Se llama en cada tick del backtest."""
        candle = Candle.from_dataframe_row(symbol=symbol, timeframe=interval, row=tick)
        self._last_close = float(candle.close)

        # Check if current tick activate any pending order
        single = self.position_mode == "single"
        stop_entry_filled = False
        exit_checked = False
        while True:
            filled_any = False
            # Las MARKET (`fill_model="next_open"`) se ejecutan a la apertura:
            # van antes que cualquier orden que la vela active después.
            # `sorted` es estable, así que el resto conserva su orden de siempre.
            # En `single`, además, las demás van en el orden en que el precio
            # las tocaría dentro de la vela (`_intrabar_touch`).
            if single:
                path = self._intrabar_path(candle)
                order_key = lambda o: (o.type != OrderType.MARKET, self._intrabar_touch(o, path))  # noqa: E731
            else:
                order_key = lambda o: o.type != OrderType.MARKET  # noqa: E731
            for order in sorted(self.state.pending_orders, key=order_key):
                if not exit_checked and order.type != OrderType.MARKET:
                    # La salida de la posición va después de las MARKET (que llenan a la apertura) y antes de lo
                    # que la vela active después.
                    self._apply_position_exit(candle)
                    exit_checked = True
                if single and order.status != OrderStatus.NEW:
                    # Cancelada o rechazada en esta misma pasada (p. ej. el OCO
                    # de una entrada que acaba de revertirse). Solo en `single`
                    # para no alterar el comportamiento histórico de `hedge`.
                    continue
                if single and stop_entry_filled and self._is_stop_entry(order):
                    # Una sola entrada stop ejecutada por vela, como el emulador
                    # de TradingView (medido en "Volatility expansion-close",
                    # stops a los dos lados: 1.440 operaciones con esta regla,
                    # 1.510 llenando las dos). La otra sigue pendiente.
                    continue
                if self._filled(order, candle):
                    self._fill(order, candle)
                    filled_any = True
                    if single and self._is_stop_entry(order) and order.status == OrderStatus.FILLED:
                        stop_entry_filled = True
            if not filled_any:
                break
        if not exit_checked:
            self._apply_position_exit(candle)
        self._position_exits.pop(str(candle.symbol).upper(), None)  # vale una vela: se re-emite en cada una
        self._expire_orders_out_of_candles(candle)
        
        # Submit tick to strategy.on_kline simulating websockets
        self.on_kline(tick=candle)

    def set_position_exit(self, symbol: str, position_exit: PositionExit) -> None:
        """Pide una salida de la posición de `symbol` para la vela SIGUIENTE (`strategy.exit` de TradingView;
        solo `position_mode="single"`). Volver a pedirla en la misma vela la sustituye.

        Qué posición cubre (medido contra TradingView, lote A de estrategias):
        - con una posición abierta, ESA posición: si la vela siguiente la revierte, la nueva no tiene salida
          en su primera vela;
        - en plano, la que abra la próxima entrada (puede salir en la misma vela en que entra).

        En cada vela el trailing parte de cero (no se arrastra el extremo de velas anteriores): solo salta si se
        activa y se toca dentro de la vela. Si la vela abre ya más allá de la activación, se activa en la apertura.
        """
        if self.position_mode != "single":
            raise ValueError("set_position_exit solo está disponible con position_mode='single'.")
        position = self._open_entry_for(symbol)
        self._position_exits[symbol.upper()] = (position_exit, None if position is None else position.order_id)

    def _open_entry_for(self, symbol: str) -> PaperOrder | None:
        entries = [e for e in self._open_position_entries() if e.symbol.upper() == symbol.upper()]
        return entries[-1] if entries else None

    def _apply_position_exit(self, candle) -> None:
        requested = self._position_exits.get(str(candle.symbol).upper())
        if requested is None:
            return
        position_exit, covered_order_id = requested
        position = self._open_entry_for(str(candle.symbol))
        if position is None or (covered_order_id is not None and covered_order_id != position.order_id):
            return
        exit_price = self._position_exit_price(position, position_exit, candle)
        if exit_price is None:
            return

        for oco in self.state.oco_orders:
            if oco.active and oco.symbol.upper() == position.symbol.upper():
                self.cancel_oco(oco.symbol, oco.list_id)
        closing_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        price = Decimal(str(exit_price))
        closing = PaperOrder(
            symbol=position.symbol, side=closing_side, type=OrderType.STOP_LOSS, quantity=position.quantity,
            price=price, stop_price=price, order_id=self.state.generate_order_id(), status=OrderStatus.NEW,
        )
        closing.metadata.update({
            "closes_order_id": position.order_id, "closes_client_order_id": position.client_order_id,
            "close_reason": "SL",
        })
        position.metadata["closed"] = True
        self._execute(closing, price, candle)

    @staticmethod
    def _position_exit_price(position: PaperOrder, position_exit: PositionExit, candle) -> float | None:
        """Recorre el camino intrabar (`_intrabar_path`, el mismo que ordena las órdenes) con el stop loss y el trailing.
        Trabaja en espacio "largo" (precios × dirección) para tratar igual los cortos."""
        direction = 1 if position.side == OrderSide.BUY else -1
        points = [direction * price for price in PaperBroker._intrabar_path(candle)]
        entry = direction * float(position.fill_price)
        stop_loss = entry - position_exit.loss if position_exit.loss is not None else float("-inf")
        activation = entry + position_exit.trail_points if position_exit.trail_points is not None else float("inf")
        trail_active, best = False, float("-inf")

        def stop_level() -> float:
            return max(stop_loss, best - position_exit.trail_offset) if trail_active else stop_loss

        if points[0] <= stop_level():
            return direction * points[0]  # abre ya más allá del stop: a la apertura
        if points[0] >= activation:
            trail_active, best = True, points[0]
        for start, end in zip(points, points[1:]):
            if end > start:
                if trail_active:
                    best = max(best, end)
                elif end >= activation:
                    trail_active, best = True, end
            elif end < start:
                level = stop_level()
                if end <= level:
                    return direction * min(start, level)
        return None

    def _is_stop_entry(self, order: PaperOrder) -> bool:
        return order.type == OrderType.STOP_LOSS and not order.is_oco_leg

    @staticmethod
    def _intrabar_path(candle) -> tuple[float, ...]:
        """Recorrido supuesto dentro de la vela, como el emulador de
        TradingView sin lupa de barras: si el máximo está más cerca de la
        apertura que el mínimo, open→high→low→close; si no,
        open→low→high→close. Mismo criterio que
        `luarix-web-server/trading/backtesting/tests/tv_parity/tv_broker.py::_intrabar_path`."""
        o, h, l, c = (float(candle.open), float(candle.high), float(candle.low), float(candle.close))
        return (o, h, l, c) if (h - o) <= (o - l) else (o, l, h, c)

    @staticmethod
    def _intrabar_touch(order: PaperOrder, path: tuple[float, ...]) -> int:
        """En qué paso de `path` tocaría el precio a `order` (0 = ya en la
        apertura). Solo ordena: si la vela llena o no la orden lo sigue
        decidiendo `_filled`. Una orden que no se toca va al final."""
        if order.type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT) and order.stop_price:
            level, rises = float(order.stop_price), order.side == OrderSide.BUY
        elif order.price is not None:
            level, rises = float(order.price), order.side == OrderSide.SELL
        else:
            return 0
        for step, price in enumerate(path):
            if (price >= level) if rises else (price <= level):
                return step
        return len(path)

    # ── Simulación interna ────────────────────────────────────────────────────

    def _fill_market(self, paper_order: PaperOrder, original: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        price = Decimal(str(self._last_close))
        paper_order.price = price
        paper_order.fill_price = price
        paper_order.executed_qty = paper_order.quantity
        paper_order.cumulative_quote_qty = paper_order.quantity * price
        paper_order.status = OrderStatus.FILLED
        paper_order.transact_time = datetime.utcnow()
        paper_order.commission = paper_order.quantity * Decimal(str(self._commission))

        notional = float(paper_order.quantity * price)
        commission_quote = float(paper_order.commission * price)
        if paper_order.side == OrderSide.BUY:
            self._balance -= notional + commission_quote
        else:
            self._balance += notional - commission_quote

        self.state.filled_orders.append(paper_order)
        self._emit(ExecutionReportFactory.fill(paper_order))

        ack = OrderAck(
            exchange_order_id=str(paper_order.order_id),
            client_order_id=original.client_order_id,
            symbol=original.symbol,
            side=original.side,
            status=CoreOrderStatus.FILLED,
        )
        fill = Fill(
            symbol=original.symbol,
            side=original.side,
            quantity=float(paper_order.executed_qty),
            price=float(price),
            fee=float(paper_order.commission),
            exchange_order_id=str(paper_order.order_id),
            client_order_id=original.client_order_id,
        )
        return ack, [fill]

    def _filled(self, order: PaperOrder, candle) -> bool:
        # Una vela solo puede llenar órdenes de SU MISMO par. Parece obvio y no
        # se comprobaba: `on_candle` recorre TODAS las pendientes y las
        # comparaba por precio a secas. Con una estrategia de LTCUSDT corriendo
        # sobre velas de BTCUSDT (caso real de un backtest mal configurado), una
        # orden límite de LTC a 63 dólares se llenaba en la primera vela de BTC
        # -- cualquier precio de BTC está por encima. El resultado era un
        # backtest lleno de operaciones que nunca podrían haber ocurrido.
        #
        # Comparación laxa en mayúsculas: los símbolos entran por varios
        # caminos (config del nodo, `Instrument.pk`, el propio broker) y no
        # todos garantizan el mismo casing. Una vela SIN símbolo no bloquea:
        # este helper también se llama con objetos mínimos (tests, un
        # `SimpleNamespace` con solo high/low), y reventar ahí sería peor que
        # no poder comprobar el par.
        candle_symbol = getattr(candle, "symbol", None)
        if candle_symbol and str(order.symbol).upper() != str(candle_symbol).upper():
            return False
        if order.type == OrderType.MARKET:
            return True
        if order.type == OrderType.STOP_LOSS:
            trigger = float(order.stop_price)
            return candle.high >= trigger if order.side == OrderSide.BUY else candle.low <= trigger
        if order.type == OrderType.STOP_LOSS_LIMIT and order.stop_price:
            trigger = float(order.stop_price)
            if order.side == OrderSide.SELL:
                return candle.low <= trigger
            return candle.high >= trigger
        if order.side == OrderSide.BUY:
            return candle.low <= float(order.price)
        return candle.high >= float(order.price)

    def _fill(self, order: PaperOrder, candle):
        if order.type == OrderType.MARKET:
            fill_price = Decimal(str(candle.open))
        elif order.type == OrderType.STOP_LOSS:
            # Al stop, salvo que la vela ABRA ya más allá (hueco): entonces a la
            # apertura, que es el primer precio al que se pudo ejecutar.
            opened = Decimal(str(candle.open))
            gapped = opened >= order.stop_price if order.side == OrderSide.BUY else opened <= order.stop_price
            fill_price = opened if gapped else order.stop_price
        elif order.type == OrderType.STOP_LOSS_LIMIT and order.stop_price:
            # Mismo criterio de hueco que `STOP_LOSS` arriba -- un stop nunca
            # "mejora" el precio, se llena al primer precio disponible una vez
            # cruzado el nivel. Antes esta rama SIEMPRE llenaba a `stop_price`
            # ignorando huecos, mientras `STOP_LOSS` (unas líneas arriba) sí
            # los maneja -- el nodo `stop-order` real (`workflows/execution/
            # nodes/execution.py::StopOrderExecutor`) coloca `STOP_LOSS_LIMIT`,
            # así que esta era la rama que de verdad se ejercitaba, y la que
            # se validó contra el oráculo de paridad (`tv_parity/tv_broker.py
            # ::_stop_fill_price`): sin esto, una vela con hueco grande sobre
            # el stop llenaba a un precio que TradingView nunca hubiera dado.
            opened = Decimal(str(candle.open)) if getattr(candle, "open", None) is not None else None
            if opened is None:
                fill_price = order.stop_price
            else:
                gapped = opened >= order.stop_price if order.side == OrderSide.BUY else opened <= order.stop_price
                fill_price = opened if gapped else order.stop_price
        elif order.type == OrderType.LIMIT and self.position_mode == "single":
            # Como TradingView: si la vela ABRE ya más allá del límite (una
            # compra por debajo, una venta por encima), a la apertura, que es
            # mejor precio. Solo en `single` para no tocar el histórico de
            # `hedge`, donde la LIMIT de una MARKET (`signal_close`) se llena
            # siempre al cierre de la señal.
            opened = Decimal(str(candle.open))
            better = opened <= order.price if order.side == OrderSide.BUY else opened >= order.price
            fill_price = opened if better else order.price
        else:
            fill_price = order.price

        if self.position_mode == "single" and not order.is_oco_leg:
            if not self._resolve_single_position(order, fill_price, candle):
                return
            if order.metadata.get("closes_order_id") is not None:
                return  # `reduce_only`: la propia orden fue el cierre

        self._execute(order, fill_price, candle)

    def _resolve_single_position(self, order: PaperOrder, fill_price: Decimal, candle) -> bool:
        """`position_mode="single"`: decide qué hace `order` con la posición
        abierta de su símbolo antes de llenarla. Devuelve `False` si la orden
        no debe llenarse (rechazada)."""
        open_entries = [e for e in self._open_position_entries() if e.symbol.upper() == order.symbol.upper()]
        position = open_entries[-1] if open_entries else None
        reduce_only = bool(order.metadata.get("reduce_only"))
        # `close_opposite` (`strategy.order` de TradingView): sin posición
        # abre como cualquier entrada, pero contra una posición contraria solo
        # la CIERRA, sin abrir la opuesta (como `reduce_only` en ese caso).
        if order.metadata.get("close_opposite") and position is not None and position.side != order.side:
            reduce_only = True

        if position is None or position.side == order.side:
            if position is None and not reduce_only:
                return True  # abre la posición
            # Pirámide 1 (misma dirección) o un cierre sin nada que cerrar.
            self._reject(order)
            return False

        for oco in self.state.oco_orders:
            if oco.active and oco.symbol.upper() == order.symbol.upper():
                self.cancel_oco(oco.symbol, oco.list_id)

        if reduce_only:
            closing = order
            closing.quantity = position.quantity
        else:
            closing = PaperOrder(
                symbol=order.symbol, side=order.side, type=order.type, quantity=position.quantity,
                price=fill_price, order_id=self.state.generate_order_id(), status=OrderStatus.NEW,
                working_time=order.working_time,
            )
        closing.metadata["closes_order_id"] = position.order_id
        closing.metadata["closes_client_order_id"] = position.client_order_id
        position.metadata["closed"] = True
        self._execute(closing, fill_price, candle)
        return True

    def _expire_orders_out_of_candles(self, candle) -> None:
        """Las órdenes con validez en velas (`candles_left`) de ESTE par que
        esta vela no llenó gastan una vela; al llegar a 0, expiran."""
        for order in list(self.state.pending_orders):
            if "candles_left" not in order.metadata:
                continue
            if str(order.symbol).upper() != str(candle.symbol).upper():
                continue
            order.metadata["candles_left"] -= 1
            if order.metadata["candles_left"] <= 0:
                order.status = OrderStatus.EXPIRED
                self.state.pending_orders.remove(order)
                self.state.canceled_orders.append(order)
                self._emit(ExecutionReportFactory.expire(order))

    def _reject(self, order: PaperOrder) -> None:
        order.status = OrderStatus.REJECTED
        if order in self.state.pending_orders:
            self.state.pending_orders.remove(order)
        self.state.rejected_orders.append(order)
        self._emit(ExecutionReportFactory.reject(order))

    def _execute(self, order: PaperOrder, fill_price: Decimal, candle):
        """La contabilidad de un fill: estado de la orden, caja, comisión y report."""
        order.status = OrderStatus.FILLED
        order.executed_qty = order.quantity
        order.fill_price = fill_price
        # `open_time`, not `close_time` -- same "open_time represents this
        # candle" convention used everywhere else (paper_runner.py's
        # tick.open_time, the frontend's toEpochTime/toTime). close_time
        # (e.g. "19:59:59.999" for the 16:00-20:00 candle) is still the SAME
        # candle, but reads as if it belongs to the NEXT one, which looked
        # like an off-by-one candle offset on the chart even though the fill
        # itself was always on the correct candle.
        order.transact_time = candle.open_time
        order.cumulative_quote_qty = order.quantity * fill_price
        order.commission = order.quantity * Decimal(str(self._commission))

        notional = float(order.quantity * fill_price)
        commission_quote = float(order.commission * fill_price)
        if order.side == OrderSide.BUY:
            self._balance -= notional + commission_quote
        else:
            self._balance += notional - commission_quote

        if order in self.state.pending_orders:
            self.state.pending_orders.remove(order)

        self.state.filled_orders.append(order)
        self._emit(ExecutionReportFactory.fill(order))

        if order.is_oco_leg:
            self._cancel_other_leg(order)

        tag = "ENTRY" if not order.is_oco_leg else ("TP" if order.type == OrderType.LIMIT else "SL")
        print(f"  [FILL] {tag} {order.side.value} qty={float(order.quantity):.4f} "
              f"price={float(fill_price):.2f} balance={self._balance:.2f}")

    def _cancel_other_leg(self, filled_order: PaperOrder):
        for oco in self.state.oco_orders:
            if oco.list_id != filled_order.order_list_id:
                continue
            other = oco.sl_order if filled_order == oco.tp_order else oco.tp_order
            if other in self.state.pending_orders:
                other.status = OrderStatus.CANCELED
                self.state.pending_orders.remove(other)
                self._emit(ExecutionReportFactory.cancel(other))
            oco.active = False
            break

    def _emit(self, report):
        if self.on_execution_report:
            self.on_execution_report(report)

    # ── Simulación de websocket de velas ────────────────────────────────────────
    def start_simulation(self, symbol:str, interval:str, start:datetime, limit:int | None = None):
        if not (self.on_kline and callable(self.on_kline)) or \
           not (self.on_execution_report and callable(self.on_execution_report)):
            raise ValueError("on_kline and on_execution_report cant be None and must be callable")
        operative_dataframe = self.get_historical_data(symbol=symbol, timeframe=interval, start=start, limit=limit)
        for _, tick in operative_dataframe.iterrows():
            self.on_candle(symbol=symbol, interval=interval, tick=tick)

            # Post-candle callback (stats, graph, reset after strategy)
            if self.on_candle_processed:
                self.on_candle_processed(tick_dict=tick.to_dict())

            sleep(self.speed)