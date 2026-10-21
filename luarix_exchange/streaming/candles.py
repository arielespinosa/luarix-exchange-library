"""Velas CERRADAS para exchanges cuyo WebSocket de velas no marca el cierre.

Bitunix, Kraken y Bitfinex mandan por WebSocket la vela EN CURSO, actualizada,
pero ninguno dice "esta vela acaba de cerrar". `ClosedCandleEmitter` combina:

1. El WebSocket como DISPARADOR: cuando el periodo de un mensaje es posterior
   al anterior, la vela previa cerró (`period_seen`).
2. Un TEMPORIZADOR de respaldo en cada frontera de periodo (+ `grace_s`): en un
   par sin operaciones el "periodo nuevo" solo llegaría con el siguiente trade,
   y la vela se emitiría tarde (`run_boundary_timer`).
3. La vela EXACTA se lee por REST (`fetch_closed`, que devuelve solo cerradas):
   la última instantánea del WebSocket podría no incluir los últimos trades.

Cada vela se emite una vez, en orden. La primera de cada suscripción es solo
la que acaba de cerrar (sin historia), igual que un WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from luarix_exchange.capabilities import CandleSubscription
from luarix_exchange.capabilities.streams import OnCandle
from luarix_exchange.models.market import Candle
from luarix_exchange.streaming._util import deliver, sleep_or_stop

log = logging.getLogger(__name__)

FetchClosed = Callable[[CandleSubscription], Sequence[Candle]]


def _ms(vela: Candle) -> int:
    return int(vela.open_time.timestamp() * 1000)


class ClosedCandleEmitter:

    def __init__(
        self,
        fetch_closed: FetchClosed,
        on_candle: OnCandle,
        periods_ms: dict[CandleSubscription, int],
        *,
        grace_s: float = 3.0,
        retries: int = 4,
        retry_s: float = 1.0,
        clock_offset_ms: int = 0,
        label: str = "velas",
    ):
        self.fetch_closed = fetch_closed
        self.on_candle = on_candle
        self.periods_ms = periods_ms
        self.grace_s = grace_s
        self.retries = retries
        self.retry_s = retry_s
        self.clock_offset_ms = clock_offset_ms  # hora_exchange ≈ hora_local + offset
        self.label = label
        self._periodo: dict[CandleSubscription, int] = {}
        self._ultima: dict[CandleSubscription, datetime] = {}
        self._locks = {sub: asyncio.Lock() for sub in periods_ms}

    async def period_seen(self, sub: CandleSubscription, ts_ms: int) -> None:
        """Un mensaje del WebSocket con un instante (`ts_ms`) del periodo vigente."""
        periodo = int(ts_ms) // self.periods_ms[sub]
        anterior = self._periodo.get(sub)
        if anterior is None or periodo > anterior:
            self._periodo[sub] = periodo
        if anterior is not None and periodo > anterior:
            await self.emit_closed(sub, (periodo - 1) * self.periods_ms[sub])

    async def emit_closed(self, sub: CandleSubscription, inicio_ms: int) -> None:
        """Emite por REST la vela que empieza en `inicio_ms` (y cualquier otra
        cerrada posterior a la última emitida que se hubiera perdido)."""
        async with self._locks[sub]:
            previa = self._ultima.get(sub)
            if previa is not None and int(previa.timestamp() * 1000) >= inicio_ms:
                return  # ya emitida (el WebSocket y el temporizador llegan a la vez)
            for _ in range(self.retries):
                velas = await asyncio.to_thread(self.fetch_closed, sub)
                cerradas = sorted((v for v in velas if v.is_closed), key=_ms)
                if previa is None:
                    nuevas = [v for v in cerradas if _ms(v) == inicio_ms]
                else:
                    nuevas = [v for v in cerradas if v.open_time > previa and _ms(v) <= inicio_ms]
                if nuevas:
                    for vela in nuevas:
                        await deliver(self.on_candle, vela)
                        self._ultima[sub] = vela.open_time
                    return
                await asyncio.sleep(self.retry_s)
            log.warning("%s: %s %s, la vela que empieza en %d ms no apareció por REST tras %d intentos",
                        self.label, sub.symbol, sub.timeframe, inicio_ms, self.retries)

    async def run_boundary_timer(self, stop: asyncio.Event) -> None:
        """Respaldo: en cada frontera de periodo (+ `grace_s`), emite la vela
        que acaba de cerrar aunque el WebSocket no haya mandado nada."""
        while not stop.is_set():
            ahora = time.time() * 1000 + self.clock_offset_ms
            fronteras = {sub: (int(ahora) // p + 1) * p for sub, p in self.periods_ms.items()}
            proxima = min(fronteras.values())
            await sleep_or_stop(stop, max(0.0, (proxima - ahora) / 1000 + self.grace_s))
            if stop.is_set():
                return
            for sub, frontera in fronteras.items():
                if frontera == proxima:
                    await self.emit_closed(sub, frontera - self.periods_ms[sub])
