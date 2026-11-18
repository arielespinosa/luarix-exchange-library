"""Errores de la integracion con TradingView.

Jerarquia propia para que el consumidor (el receptor de webhook del backend)
pueda distinguir un payload invalido (respuesta 400) de un secreto que no
coincide (respuesta 401) sin acoplarse a detalles internos.
"""

from __future__ import annotations


class TradingViewError(Exception):
    """Base de todos los errores de la integracion TradingView."""


class TradingViewPayloadError(TradingViewError):
    """El cuerpo del webhook no es JSON valido o le faltan campos obligatorios."""


class TradingViewSecretError(TradingViewError):
    """El secreto del webhook no coincide con el esperado."""
