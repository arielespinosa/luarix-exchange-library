"""Integracion con TradingView: contrato y parser del webhook de senales.

TradingView es el motor de senales (ejecuta la estrategia Pine Script y decide
entradas/salidas); Luarix solo las recibe por webhook. Este subpaquete no abre
conexiones ni depende de Django: ofrece la clase pura que el receptor de
webhook del backend usa para parsear, normalizar y validar cada alerta.

    from luarix_exchange.trading_view import TradingViewWebhookParser

    parser = TradingViewWebhookParser()
    if parser.verify_secret(request.body, instance.webhook_secret_hash):
        signal = parser.parse(request.body)   # -> TradingViewSignal
"""

from __future__ import annotations

from .contract import SCHEMA_VERSION, Direction, EventType, TradingViewSignal
from .errors import (
    TradingViewError,
    TradingViewPayloadError,
    TradingViewSecretError,
)
from .parser import TradingViewWebhookParser

__all__ = [
    "SCHEMA_VERSION",
    "Direction",
    "EventType",
    "TradingViewSignal",
    "TradingViewWebhookParser",
    "TradingViewError",
    "TradingViewPayloadError",
    "TradingViewSecretError",
]
