"""Utilidades internas de streaming."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import Any, Callable


async def deliver(callback: Callable[[Any], Any], item: Any) -> None:
    """Entrega `item` a un callback que puede ser síncrono o asíncrono."""
    resultado = callback(item)
    if inspect.isawaitable(resultado):
        await resultado


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Espera `seconds` o hasta que `stop` se active, lo que llegue antes."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def transient_errors() -> tuple[type[BaseException], ...]:
    """Errores de RED, tras los que tiene sentido reintentar.

    `OSError` cubre `ConnectionError`, `TimeoutError` y, por herencia,
    `requests.RequestException`. `aiohttp.ClientError` no hereda de
    `OSError` y se añade si aiohttp está instalado. Un error de cualquier
    otro tipo (un `ValueError` por un símbolo inválido, un `KeyError` por un
    cambio de formato) NO es transitorio: reintentar solo lo escondería."""
    errores: list[type[BaseException]] = [OSError, asyncio.TimeoutError]
    try:
        import aiohttp

        errores.append(aiohttp.ClientError)
    except ImportError:
        pass
    return tuple(errores)
