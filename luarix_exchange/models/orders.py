from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List
from uuid import uuid4


class Side(Enum):
    """Lado de una orden.

    Vivia en `core/models/signals.py` de luarix-trading-framework, junto a
    `Signal`. `Signal` es un modelo de estrategia y se quedo alla; `Side` lo
    usan todas las ordenes, asi que vive aca. luarix-trading-framework lo
    reexporta, de modo que `Side` es el MISMO objeto en los dos paquetes.
    """

    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """Tipo de orden que se enviará a Binance.

    MARKET:           Se ejecuta inmediatamente al precio actual del mercado.
                       No requiere especificar precio.
    LIMIT:            Se ejecuta solo cuando el precio llega al nivel indicado.
                       Requiere especificar `price` y `time_in_force`.
    STOP_LOSS_LIMIT:  Orden de stop independiente (no ligada a un OCO) -- se
                       activa cuando el precio cruza `stop_price`, y coloca una
                       LIMIT a `price` en ese momento. Requiere `stop_price` y
                       `price`. Usada por el flujo de "órdenes independientes"
                       (Entry+TP+SL como 3 órdenes propias, ver PositionManager
                       en workflows/) -- el leg de Take Profit de ese mismo
                       flujo no necesita un tipo nuevo, es una LIMIT normal.
    STOP_LOSS:        Stop a MERCADO -- se activa cuando el precio cruza
                       `stop_price` y se ejecuta a mercado. Requiere `stop_price`.
                       Es el `strategy.entry(..., stop=...)` de TradingView (orden
                       stop de ENTRADA), y el nombre nativo de Binance spot.
    """
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    STOP_LOSS = "STOP_LOSS"


class TimeInForce(Enum):
    """Controla cuánto tiempo permanece activa una orden LIMIT antes de cancelarse.

    GTC (Good-Till-Canceled): La orden queda abierta indefinidamente hasta que
        se ejecute o el usuario la cancele manualmente. Es la más común.
    IOC (Immediate-Or-Cancel): Se ejecuta de inmediato lo que se pueda y el
        resto se cancela automáticamente.
    FOK (Fill-Or-Kill): Se ejecuta completa de inmediato o se cancela entera.
        No permite ejecuciones parciales.
    """
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(Enum):
    """Estado de una orden después de enviarla a Binance.

    NEW:              La orden fue aceptada por Binance pero aún no se ejecutó.
    PARTIALLY_FILLED: La orden se ejecutó parcialmente (para órdenes LIMIT).
    FILLED:           La orden se ejecutó completamente.
    CANCELED:         La orden fue cancelada (por el usuario o por TIF).
    REJECTED:         Binance rechazó la orden (fondos insuficientes, precio inválido, etc.).
    """
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    TAKE_PROFIT_ACTIVATED = "TAKE_PROFIT_ACTIVATED"
    STOP_LOSE_ACTIVATED = "STOP_LOSE_ACTIVATED"


@dataclass(frozen=False)
class TradeIntent:
    """Representa la intención de un trade — qué quiere hacer la estrategia y por qué.

    Es el "plan de trading": contiene toda la información de la idea, incluyendo los
    niveles de riesgo. NO es una instrucción directa a Binance — para eso se usa
    OrderRequest.

    IMPORTANTE: `stop_loss` y `take_profit` son solo datos informativos aquí.
    No se colocan automáticamente como órdenes en Binance. Para eso habría que
    crear órdenes OCO separadas usando esos niveles como referencia.

    Atributos:
        symbol:        Par de trading (ej: "BTCUSDT").
        side:          Dirección: BUY para comprar, SELL para vender.
        quantity:      Cantidad del activo a operar.
        entry_price:   Precio al que se quiere entrar al mercado.
        stop_loss:     Precio al que se cierra la posición si va en contra (límite de pérdida).
        take_profit:   Precio objetivo al que se cierra la posición con ganancia.
        risk_amount:   Monto en USD/quote que se está dispuesto a perder en este trade.
        strategy_name: Nombre de la estrategia que generó esta intención.
        confidence:    Nivel de confianza de la señal (0.0 a 1.0).
        metadata:      Datos adicionales opcionales (indicadores, notas, etc.).
    """
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    risk_amount: float
    strategy_name: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        """Valor nocional total del trade (cantidad × precio de entrada)."""
        return self.quantity * self.entry_price


@dataclass(frozen=True)
class OrderRequest:
    """Instrucción concreta para enviar una orden a Binance.

    A diferencia de TradeIntent (que es la "idea"), OrderRequest es la
    "instrucción ejecutable" — los datos exactos que se envían al exchange.
    Es inmutable (frozen=True) para evitar modificaciones accidentales antes de enviarla.

    Atributos:
        symbol:           Par de trading (ej: "BTCUSDT").
        side:             BUY o SELL.
        quantity:         Cantidad a operar (será redondeada al step_size del símbolo).
        order_type:       MARKET (precio actual), LIMIT (precio específico), o
                          STOP_LOSS_LIMIT (orden de stop independiente).
        price:            Precio deseado. Requerido para LIMIT y STOP_LOSS_LIMIT.
        stop_price:       Precio de activación del stop. Solo para STOP_LOSS_LIMIT.
        time_in_force:    Cuánto dura la orden activa. Requerido para LIMIT (GTC recomendado).
        client_order_id:  ID único generado automáticamente con prefijo "tb-". Permite
                          rastrear la orden en Binance aunque se pierda el orderId.
        strategy_name:    Nombre de la estrategia que originó esta orden (trazabilidad).
        metadata:         Datos adicionales opcionales.
    """
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    # "spot" | "futures". Qué mercado del broker recibe la orden. Antes no
    # existía y TODO se colocaba en spot -- incluso con el grafo configurado
    # en futuros, porque el selector solo llegaba a las consultas de cuenta
    # (balance, órdenes) y no a la colocación. Default "spot" = comportamiento
    # anterior intacto para cualquier llamador que no lo pase.
    market: str = "spot"
    price: float | None = None
    stop_price: float | None = None
    time_in_force: TimeInForce | None = None
    client_order_id: str = field(default_factory=lambda: f"tb-{uuid4().hex}")
    strategy_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # ── Protección adjunta a la propia entrada ──────────────────────────
    # Take-profit / stop-loss que el exchange debe asociar a ESTA orden en el
    # momento de colocarla, en vez de mandarse después como dos órdenes
    # sueltas. Bitunix futuros lo soporta nativamente (`tpPrice`/`slPrice`) y
    # es la única forma de que la posición no exista nunca sin stop.
    #
    # No es un capricho de eficiencia: mandar la salida como una orden normal
    # del lado opuesto ABRIÓ UN CORTO REAL en una cuenta en modo HEDGE (ver
    # `docs/plans/bugs.md`). Un broker que no las soporte debe ignorarlas, no
    # inventarse una orden con ellas.
    tp_price: float | None = None
    sl_price: float | None = None
    # ¿Esta orden CIERRA una posición existente en vez de abrir una? En un
    # exchange con modo cobertura (HEDGE) la diferencia no es cosmética: una
    # venta marcada como apertura no reduce el largo abierto, abre un corto
    # independiente que convive con él.
    reduce_only: bool = False


@dataclass(frozen=True)
class OrderAck:
    """Confirmación de que Binance recibió (o rechazó) una orden.

    Se obtiene como resultado de `broker.place_order()`. No garantiza que la
    orden se ejecutó — solo que fue recibida. Para saber si se ejecutó,
    verificar que `status == OrderStatus.FILLED`.

    Atributos:
        exchange_order_id: El ID que Binance asigna internamente (None en test orders).
        client_order_id:   El ID que nosotros asignamos (prefijo "tb-").
        symbol:            Par de trading.
        side:              BUY o SELL.
        status:            Estado actual de la orden en Binance.
        oco_list_id:       El ID del OCO al que pertenece si fuese el caso
        raw:               Respuesta completa de la API de Binance (útil para depuración).
    """
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    symbol: str | None = None
    side: Side | None = None
    status: OrderStatus | None = None
    oco_list_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Fill:
    """Representa una ejecución real de una orden en el mercado.

    Una orden puede generar múltiples fills si se ejecuta contra distintos
    precios en el order book (ejecución parcial a distintos niveles).
    Por ejemplo, una orden MARKET de 1 BTC puede ejecutarse parcialmente
    a $50,000 y el resto a $50,001.

    Atributos:
        symbol:            Par de trading.
        side:              BUY o SELL.
        quantity:          Cantidad ejecutada en este fill específico.
        price:             Precio al que se ejecutó.
        fee:               Comisión cobrada por Binance en este fill.
        fee_asset:         Moneda en la que se cobró la comisión (ej: "BNB", "USDT").
        client_order_id:   ID que asignamos a la orden original.
        exchange_order_id: ID de Binance para la orden original.
        fill_id:           ID único de este fill específico (tradeId de Binance).
        timestamp:         Momento en que se registró el fill (UTC).
    """
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float = 0.0
    fee_asset: str | None = None
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    fill_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional(self) -> float:
        """Valor total ejecutado en este fill (cantidad × precio)."""
        return self.quantity * self.price


@dataclass(frozen=True)
class OrderInfo:
    """El estado COMPLETO de una orden que ya vive (o vivió) en el exchange.

    `OrderAck` es lo que el broker respondió al COLOCAR; esto es lo que el
    exchange dice de ella cuando se le pregunta después -- cuánto lleva
    ejecutado, a qué precio medio, con qué TP/SL adjunto. Es lo que
    devuelven `Broker.get_order()` y `Broker.get_open_orders()`.

    Existe porque los dos brokers describen lo mismo con nombres distintos:

    | concepto        | Binance         | Bitunix futuros | Bitunix spot  |
    |-----------------|-----------------|-----------------|---------------|
    | id              | `orderId`       | `orderId`       | `id`          |
    | cantidad        | `origQty`       | `qty`           | `volume`      |
    | ya ejecutado    | `executedQty`   | `tradeQty`      | `dealVolume`  |
    | lado            | `"BUY"`/`"SELL"`| `"BUY"`/`"SELL"`| `1` / `2`     |
    | parcial         | `PARTIALLY_FILLED` | `PART_FILLED` | `PART_FILLED` |

    Sin esta capa, cualquier código que quiera funcionar con los dos
    acaba con un `if broker == ...` por cada campo. El diccionario original
    del exchange sigue disponible entero en `raw`.
    """
    order_id: str
    symbol: str
    status: OrderStatus
    side: Side | None = None
    order_type: str | None = None
    quantity: float = 0.0
    filled_quantity: float = 0.0
    price: float | None = None
    average_price: float | None = None
    stop_price: float | None = None
    tp_price: float | None = None
    sl_price: float | None = None
    client_order_id: str | None = None
    reduce_only: bool = False
    market: str = "spot"
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        """¿Sigue viva en el libro? (aceptada o a medio llenar)."""
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    @property
    def remaining_quantity(self) -> float:
        return max(self.quantity - self.filled_quantity, 0.0)

    # ── Normalizadores ──────────────────────────────────────────────────
    #
    # Los estados que cada exchange devuelve, traducidos a `OrderStatus`
    # (que son los nombres de Binance). Bitunix dice `PART_FILLED` donde
    # Binance dice `PARTIALLY_FILLED`, e `INIT` ("aceptada, todavía no en el
    # libro") no tiene equivalente: para cualquier consumidor es
    # indistinguible de NEW -- sigue viva y no ha llenado.
    _STATUS = {
        "INIT": OrderStatus.NEW,
        "NEW": OrderStatus.NEW,
        "PENDING": OrderStatus.NEW,
        "PART_FILLED": OrderStatus.PARTIALLY_FILLED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "CANCELLED": OrderStatus.CANCELED,
        "EXPIRED": OrderStatus.CANCELED,
        # Binance: cancelada por la protección contra auto-cruce; sigue
        # cancelándose (no es un estado final todavía, pero no ha llenado más).
        "EXPIRED_IN_MATCH": OrderStatus.CANCELED,
        "PENDING_CANCEL": OrderStatus.NEW,
        # Bitunix (canal de órdenes por WebSocket): llenada a medias y el resto
        # cancelado. Lo ejecutado va en la cantidad llenada, no en el estado.
        "PART_FILLED_CANCELED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
    }

    @classmethod
    def _status_of(cls, value: Any) -> OrderStatus:
        return cls._STATUS.get(str(value or "").upper(), OrderStatus.NEW)

    @staticmethod
    def _side_of(value: Any) -> Side | None:
        """El lado, venga como texto (`"BUY"`) o como el código NUMÉRICO del
        spot de Bitunix (`1` = compra, `2` = venta)."""
        text = str(value or "").upper()
        if text in ("1", "BUY"):
            return Side.BUY
        if text in ("2", "SELL"):
            return Side.SELL
        return None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        """`None` tanto si el campo falta como si vale 0.

        No es una licencia: los dos exchanges usan el cero como "este campo
        no aplica a esta orden" -- una MARKET de Binance responde
        `price: "0.00000000"`, y una orden de Bitunix sin stop responde
        `slPrice: "0"`. Devolver `0.0` haría creer que hay un stop puesto en
        cero, que es un nivel real y catastrófico."""
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number or None

    @staticmethod
    def _time_of(value: Any) -> datetime | None:
        """Milisegundos desde epoch -> datetime UTC. Bitunix los manda como
        CADENA de dígitos y Binance como int; los dos caben acá."""
        if value in (None, ""):
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    @classmethod
    def from_binance(cls, raw: dict, market: str = "spot") -> OrderInfo:
        return cls(
            order_id=str(raw.get("orderId", "")),
            symbol=raw.get("symbol", ""),
            status=cls._status_of(raw.get("status")),
            side=cls._side_of(raw.get("side")),
            order_type=raw.get("type"),
            quantity=float(raw.get("origQty") or 0),
            filled_quantity=float(raw.get("executedQty") or 0),
            price=cls._float_or_none(raw.get("price")),
            average_price=cls._float_or_none(raw.get("avgPrice")),
            stop_price=cls._float_or_none(raw.get("stopPrice")),
            client_order_id=raw.get("clientOrderId"),
            reduce_only=bool(raw.get("reduceOnly", False)),
            market=market,
            created_at=cls._time_of(raw.get("time") or raw.get("updateTime")),
            raw=raw,
        )

    @classmethod
    def from_bitunix(cls, raw: dict, market: str = "spot") -> OrderInfo:
        # Un mismo concepto con dos nombres según el mercado: futuros usa
        # `qty`/`tradeQty`, spot usa `volume`/`dealVolume`. Se aceptan los
        # dos en vez de ramificar por `market`, porque Bitunix no es
        # consistente ni dentro del mismo mercado (`id` vs `orderId`).
        return cls(
            order_id=str(raw.get("orderId") or raw.get("id") or ""),
            symbol=raw.get("symbol", ""),
            status=cls._status_of(raw.get("status")),
            side=cls._side_of(raw.get("side")),
            order_type=str(raw.get("orderType") or raw.get("type") or "") or None,
            quantity=float(raw.get("qty") or raw.get("volume") or 0),
            filled_quantity=float(raw.get("tradeQty") or raw.get("dealVolume") or 0),
            price=cls._float_or_none(raw.get("price")),
            average_price=cls._float_or_none(raw.get("avgPrice") or raw.get("dealPrice")),
            tp_price=cls._float_or_none(raw.get("tpPrice")),
            sl_price=cls._float_or_none(raw.get("slPrice")),
            client_order_id=raw.get("clientId") or raw.get("clientOrderId"),
            reduce_only=str(raw.get("tradeSide") or "").upper() == "CLOSE",
            market=market,
            created_at=cls._time_of(raw.get("ctime") or raw.get("createTime")),
            raw=raw,
        )


@dataclass
class OrderPlan:
    """Plan de ejecución con múltiples entradas escalonadas (DCA / pyramid).

    Permite dividir una posición en varias entradas para promediar el precio
    de entrada o construir la posición gradualmente.

    Atributos:
        entries: Lista de OrderRequest a enviar en orden.
        intent:  El TradeIntent original que originó este plan.
        splits:  Proporciones de la cantidad total para cada entrada.
                 Por defecto [1.0] = una sola entrada con el 100% de la cantidad.
                 Ejemplo: [0.5, 0.3, 0.2] = tres entradas con 50%, 30% y 20%.
    """
    entries: list[OrderRequest]
    intent: "TradeIntent"
    splits: list[float] = field(default_factory=lambda: [1.0])


# Backwards-compatible alias while older scripts migrate to OrderRequest.
Order = OrderRequest


class EntryStatus(Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    STOP_lOSS_BROKEN = "STOP_lOSS_BROKEN"
    TAKED = "TAKED"


@dataclass
class Entry:
    symbol: str
    side: Side
    price: float
    quantity: float
    start: datetime
    end: datetime | None = None
    ack: OrderAck | None = None
    fills: List[Fill] = field(default_factory=list)
    status: EntryStatus = EntryStatus.NEW
    score: int | None = None

    # --- Identificadores ---
    client_order_id: str | None = None
    exchange_order_id: str | None = None

    # --- TP/SL dinámicos ---
    tp_price: float | None = None
    sl_price: float | None = None
    tp_ack: OrderAck | None = None
    sl_ack: OrderAck | None = None

    # --- OCO asociado ---
    oco_list_id: int | None = None
    oco_tp_order_id: int | None = None
    oco_sl_order_id: int | None = None

    # --- Métodos útiles ---
    def add_fill(self, fill: Fill):
        self.fills.append(fill)

    def update_tp(self, new_tp: float):
        self.tp_price = new_tp

    def update_sl(self, new_sl: float):
        self.sl_price = new_sl

    def is_open(self) -> bool:
        return self.status == EntryStatus.NEW
