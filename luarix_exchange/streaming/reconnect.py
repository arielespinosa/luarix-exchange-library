"""Reconexión común a todos los streams.

Cada stream implementa UNA sesión (abrir, leer, entregar hasta que se cierre)
y `run_with_reconnect` la reabre cuando cae por un error de red, con espera
exponencial. Es el *template method* de `engine/exchanges/base.py` de
luarix-arbitrage-web-server, generalizado para velas, libros y órdenes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from luarix_exchange.streaming._util import sleep_or_stop, transient_errors

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconnectPolicy:
    """Espera entre reintentos: empieza en `backoff_s` y se dobla hasta
    `backoff_max_s`. Una sesión que termina limpia (el servidor cerró) vuelve
    a la espera inicial."""

    backoff_s: float = 1.0
    backoff_max_s: float = 60.0
    transient: tuple[type[BaseException], ...] = field(default_factory=transient_errors)


async def run_with_reconnect(
    session: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    policy: ReconnectPolicy | None = None,
    label: str = "stream",
) -> None:
    """Ejecuta `session()` una y otra vez hasta que `stop` se active.

    - Termina limpia (el servidor cerró la conexión): se reabre tras
      `backoff_s`.
    - Error transitorio (`policy.transient`, errores de red): se registra con
      su detalle y se reabre con espera creciente.
    - Cualquier otro error se PROPAGA: es un bug o un dato inválido, y
      reintentarlo solo lo escondería.
    """
    policy = policy or ReconnectPolicy()
    backoff = policy.backoff_s
    while not stop.is_set():
        try:
            await session()
            espera, backoff = policy.backoff_s, policy.backoff_s
            if not stop.is_set():
                log.info("%s: la sesión terminó (cierre del servidor); se reabre en %.1f s", label, espera)
        except policy.transient as exc:
            espera, backoff = backoff, min(backoff * 2, policy.backoff_max_s)
            log.warning("%s: conexión perdida (%s: %s); reintento en %.1f s", label, type(exc).__name__, exc, espera)
        if stop.is_set():
            break
        await sleep_or_stop(stop, espera)
