"""Contrato de la senal de TradingView (versionado).

Define la forma estable de un evento de TradingView tal y como lo recibe
Luarix por webhook, independiente de TradingView y de Django. El parser
(`parser.py`) convierte el JSON crudo de la alerta en un `TradingViewSignal`
normalizado; el backend lo persiste.

El contrato es deliberadamente tolerante: TradingView no garantiza orden ni
entrega, asi que los campos dependientes del tipo de evento son opcionales y
el consumidor decide que hacer con cada uno. Lo unico imprescindible para
poder enrutar y deduplicar es el tipo de evento y su identificador.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

#: Version del esquema que entiende este contrato. Va en el payload como
#: `schema_version`; si cambia la forma de los mensajes, se sube aqui.
SCHEMA_VERSION = 1


class EventType(Enum):
    """Tipos de evento que una estrategia de TradingView puede emitir.

    Una senal no es una ejecucion: describe lo que la estrategia decidio en
    TradingView. La ejecucion real (si la hay) la decide el backend aparte.
    """

    ENTRY = "ENTRY"               # abre posicion
    EXIT = "EXIT"                 # cierra posicion (completo)
    PARTIAL_EXIT = "PARTIAL_EXIT"  # cierre parcial
    STOP_LOSS = "STOP_LOSS"       # salida por stop
    TAKE_PROFIT = "TAKE_PROFIT"   # salida por objetivo
    UPDATE_SL = "UPDATE_SL"       # mueve el stop loss
    UPDATE_TP = "UPDATE_TP"       # mueve el take profit
    CANCEL = "CANCEL"             # cancela una operacion/orden pendiente
    HEARTBEAT = "HEARTBEAT"       # latido periodico (la estrategia sigue viva)
    SNAPSHOT = "SNAPSHOT"         # estado actual de la posicion para reconciliar


class Direction(Enum):
    """Direccion de la operacion."""

    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class TradingViewSignal:
    """Una senal de TradingView ya parseada y normalizada.

    `raw` conserva el payload original (sin el secreto, que nunca se guarda),
    por si el consumidor necesita un campo que el contrato aun no modela.
    """

    schema_version: int
    event: EventType
    event_id: str
    sequence: int | None = None
    strategy_id: str | None = None
    instance_id: str | None = None          # id de la instancia en TradingView (externo)
    trade_id: str | None = None             # id estable de operacion (liga ENTRY con su EXIT)
    symbol: str | None = None
    timeframe: str | None = None
    direction: Direction | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    reason: str | None = None
    position: dict[str, Any] | None = None  # usado por HEARTBEAT/SNAPSHOT
    timestamp: datetime | None = None       # momento del evento (de la estrategia)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> tuple[str | None, str]:
        """Clave de idempotencia: (instancia, evento). El backend deduplica por
        (StrategyInstance, event_id); aqui se expone el par para tests/consumo."""
        return (self.instance_id, self.event_id)
