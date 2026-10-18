"""Una sesión WebSocket genérica sobre aiohttp: conectar, suscribirse,
decodificar JSON y entregar cada mensaje, hasta que `stop` se active o el
servidor cierre. No reintenta: de eso se encarga `run_with_reconnect`.

Los WebSockets concretos (Fase 5) solo traducen mensajes:

```python
async def session():
    await websocket_session(URL, on_message, stop, subscribe=[{"method": "SUBSCRIBE", ...}])
await run_with_reconnect(session, stop, label="binance klines")
```

Necesita el extra `[streams]` (aiohttp).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from luarix_exchange.streaming._util import deliver

log = logging.getLogger(__name__)

OnMessage = Callable[[Any], Awaitable[None] | None]


def _aiohttp():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise ModuleNotFoundError(
            "Los streams por WebSocket necesitan aiohttp: pip install \"luarix-exchange-library[streams]\""
        ) from exc
    return aiohttp


async def websocket_session(
    url: str,
    on_message: OnMessage,
    stop: asyncio.Event,
    *,
    subscribe: Sequence[Any] = (),
    heartbeat: float | None = 30.0,
    http_session: Any = None,
    max_msg_size: int = 4 * 1024 * 1024,
    label: str = "websocket",
) -> None:
    """UNA conexión. Entrega cada mensaje de texto/binario ya decodificado
    como JSON. Vuelve cuando `stop` se activa o el servidor cierra; levanta
    `ConnectionError` si el socket informa de un error."""
    aiohttp = _aiohttp()
    propia = http_session is None
    http = http_session or aiohttp.ClientSession()
    try:
        async with http.ws_connect(url, heartbeat=heartbeat, max_msg_size=max_msg_size) as ws:
            for mensaje in subscribe:
                if isinstance(mensaje, str):
                    await ws.send_str(mensaje)
                else:
                    await ws.send_json(mensaje)
            espera_stop = asyncio.ensure_future(stop.wait())
            try:
                while not stop.is_set():
                    recibir = asyncio.ensure_future(ws.receive())
                    hechos, _ = await asyncio.wait({recibir, espera_stop}, return_when=asyncio.FIRST_COMPLETED)
                    if recibir not in hechos:
                        recibir.cancel()
                        return
                    msg = recibir.result()
                    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                        await deliver(on_message, json.loads(msg.data))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError(f"{label}: error en el socket: {ws.exception()!r}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                        log.info("%s: el servidor cerró la conexión (código %s)", label, ws.close_code)
                        return
            finally:
                espera_stop.cancel()
    finally:
        if propia:
            await http.close()
