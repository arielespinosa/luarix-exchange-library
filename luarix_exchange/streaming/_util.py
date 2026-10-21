"""Utilidades internas de streaming."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from datetime import datetime
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


def iso_to_ms(valor) -> int | None:
    """ISO-8601 -> ms desde epoch, aceptando NANOSEGUNDOS
    (`2026-10-18T16:11:00.000000000Z`), que `datetime` no admite. `None` si
    no se puede leer. Solo los dígitos INICIALES tras el punto son la
    fracción; lo que sigue es la zona (juntarlos perdía la zona: bug de 4 h
    cazado por un test, 2026-10-18)."""
    if not valor:
        return None
    texto = str(valor).replace("Z", "+00:00")
    if "." in texto:
        cabeza, resto = texto.split(".", 1)
        fraccion = resto[:len(resto) - len(resto.lstrip("0123456789"))]
        texto = f"{cabeza}.{fraccion[:6]}{resto[len(fraccion):]}"
    try:
        return int(datetime.fromisoformat(texto).timestamp() * 1000)
    except ValueError:
        return None
