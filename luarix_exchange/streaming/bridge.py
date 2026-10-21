"""Puente para consumidores SÍNCRONOS (web-server, orchestator): corre los
streams asíncronos en un hilo con su propio event loop y entrega por callback.

```python
runner = StreamRunner()
handle = runner.start(
    lambda stop: stream.stream_candles([CandleSubscription("BTCUSDT", "1m")], on_candle, stop),
    label="velas BTCUSDT",
)
...
handle.stop()        # para ESE stream y espera a que termine
runner.close()       # para todos y apaga el hilo
```

⚠ Los callbacks se ejecutan EN EL HILO DEL RUNNER. Tienen que ser rápidos:
uno que bloquee frena a todos los streams de ese runner. Si hay trabajo
pesado (una consulta a base de datos, ejecutar un grafo), el callback debe
pasarlo a otra cola o hilo.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

StreamFactory = Callable[[asyncio.Event], Awaitable[None]]


class StreamHandle:
    """Un stream en marcha dentro de un `StreamRunner`."""

    def __init__(self, loop: asyncio.AbstractEventLoop, future: concurrent.futures.Future, stop: asyncio.Event,
                 label: str):
        self._loop = loop
        self._future = future
        self._stop = stop
        self.label = label

    @property
    def running(self) -> bool:
        return not self._future.done()

    def stop(self, timeout: float | None = 10.0) -> None:
        """Pide al stream que pare y espera a que termine. Si el stream ya
        había terminado con error, el error se conserva en `exception()`."""
        if not self._future.done():
            self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._future.result(timeout)
        except concurrent.futures.TimeoutError:
            log.warning("%s: no terminó en %s s tras pedirle que parara", self.label, timeout)
        except BaseException:  # noqa: BLE001 -- ya registrado por el runner; queda en exception()
            pass

    def exception(self) -> BaseException | None:
        """El error con el que terminó el stream, o `None` (sigue vivo o terminó bien)."""
        if not self._future.done() or self._future.cancelled():
            return None
        return self._future.exception()

    def wait(self, timeout: float | None = None) -> None:
        """Bloquea hasta que el stream termine; relanza su error si lo hubo."""
        self._future.result(timeout)


class StreamRunner:
    """Un hilo daemon con un event loop, compartido por todos los streams que
    se le arranquen."""

    def __init__(self, name: str = "luarix-streams"):
        self.name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._handles: list[StreamHandle] = []
        self._lock = threading.Lock()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                listo = threading.Event()

                def correr() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(listo.set)
                    loop.run_forever()

                self._thread = threading.Thread(target=correr, name=self.name, daemon=True)
                self._thread.start()
                listo.wait()
                self._loop = loop
            return self._loop

    def start(self, factory: StreamFactory, label: str = "stream") -> StreamHandle:
        """Arranca `factory(stop)` en el hilo del runner. `factory` recibe el
        `asyncio.Event` de parada y devuelve la corrutina del stream."""
        loop = self._ensure_started()
        stop = asyncio.Event()

        async def protegido() -> None:
            try:
                await factory(stop)
            except Exception:
                log.exception("%s: el stream terminó con error", label)
                raise

        handle = StreamHandle(loop, asyncio.run_coroutine_threadsafe(protegido(), loop), stop, label)
        with self._lock:
            self._handles.append(handle)
        return handle

    def close(self, timeout: float | None = 10.0) -> None:
        """Para todos los streams y apaga el hilo."""
        with self._lock:
            handles, self._handles = self._handles, []
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        for handle in handles:
            handle.stop(timeout)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout)
            loop.close()

    def __enter__(self) -> StreamRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
