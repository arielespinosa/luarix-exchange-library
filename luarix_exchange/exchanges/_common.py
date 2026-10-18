"""Piezas comunes de los conectores portados de luarix-arbitrage-web-server
(`engine/exchanges/common.py`): lectura de JSON con `Decimal`, niveles de libro,
libro incremental por nivel, fotos, un decodificador mínimo de protobuf (MEXC)
y el cliente REST síncrono (decisión híbrida)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from luarix_exchange.models.book import Level, OrderBook
from luarix_exchange.models.brokers import Market

REST_TIMEOUT_S = 30


def now_ms() -> int:
    return int(time.time() * 1000)


def loads(text: str | bytes) -> Any:
    """JSON con los números decimales como `Decimal` (HTX manda precios como
    NÚMEROS, no como texto: así no pasan por `float`)."""
    return json.loads(text, parse_float=Decimal)


def levels(raw: Iterable[Iterable[Any]]) -> list[Level]:
    """[[precio, cantidad, …], …] → [(Decimal, Decimal), …]. Las columnas de más
    (OKX manda 4) se ignoran."""
    out: list[Level] = []
    for row in raw:
        price, qty = list(row)[:2]
        out.append((Decimal(str(price)), Decimal(str(qty))))
    return out


def step_from_decimals(decimals: int | str) -> Decimal:
    """Número de decimales → paso: 6 → 0.000001, 0 → 1."""
    return Decimal(1).scaleb(-int(decimals))


def snapshot_book(symbol: str, bids: list[Level], asks: list[Level], update_id: int, received_ms: int, depth: int,
                  event_ms: int | None = None) -> OrderBook:
    """Una foto completa (Gate, KuCoin, HTX, MEXC), ordenada y recortada."""
    bids = sorted((lv for lv in bids if lv[1] > 0), key=lambda lv: lv[0], reverse=True)[:depth]
    asks = sorted((lv for lv in asks if lv[1] > 0), key=lambda lv: lv[0])[:depth]
    return OrderBook(symbol=symbol, market=Market.SPOT, bids=bids, asks=asks, update_id=update_id,
                     received_ms=received_ms, event_ms=event_ms)


class LevelBook:
    """Libro incremental por nivel de precio (Bybit, OKX): una foto y luego
    cambios nivel a nivel, donde cantidad 0 quita el nivel."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.update_id = 0

    def reset(self, bids: list[Level], asks: list[Level], update_id: int) -> None:
        self.bids = {p: q for p, q in bids if q > 0}
        self.asks = {p: q for p, q in asks if q > 0}
        self.update_id = update_id

    def apply(self, bids: list[Level], asks: list[Level], update_id: int) -> None:
        for side, changes in ((self.bids, bids), (self.asks, asks)):
            for price, qty in changes:
                if qty == 0:
                    side.pop(price, None)
                else:
                    side[price] = qty
        self.update_id = update_id

    def sorted_levels(self, depth: int | None = None) -> tuple[list[Level], list[Level]]:
        bids = sorted(self.bids.items(), key=lambda lv: lv[0], reverse=True)
        asks = sorted(self.asks.items(), key=lambda lv: lv[0])
        return (bids[:depth], asks[:depth]) if depth else (bids, asks)

    def snapshot(self, received_ms: int, depth: int, event_ms: int | None = None) -> OrderBook:
        bids, asks = self.sorted_levels(depth)
        return OrderBook(symbol=self.symbol, market=Market.SPOT, bids=bids, asks=asks, update_id=self.update_id,
                         received_ms=received_ms, event_ms=event_ms)


# --- protobuf mínimo (MEXC) ---------------------------------------------------

def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def protobuf_fields(data: bytes) -> dict[int, list[Any]]:
    """Mensaje protobuf → {número de campo: [valores]}. Varint → int; campos de
    longitud (texto, bytes, mensajes anidados) → bytes. Basta para los libros
    de MEXC sin depender de `protobuf` ni de ficheros generados."""
    fields: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 0x7
        value: Any
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            length, pos = _varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire == 1:
            value, pos = int.from_bytes(data[pos:pos + 8], "little"), pos + 8
        elif wire == 5:
            value, pos = int.from_bytes(data[pos:pos + 4], "little"), pos + 4
        else:
            raise ValueError(f"protobuf: tipo de campo {wire} no soportado")
        fields.setdefault(number, []).append(value)
    return fields


# --- REST síncrono --------------------------------------------------------------

class RestClient:
    """Sesión `requests` perezosa con lectura en `Decimal`. Los errores HTTP se
    levantan con el cuerpo del exchange en el mensaje (el motivo real)."""

    def __init__(self, timeout: float = REST_TIMEOUT_S):
        self.timeout = timeout
        self._http = None

    def session(self):
        if self._http is None:
            import requests

            self._http = requests.Session()
        return self._http

    def request(self, method: str, url: str, *, ok_status: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        resp = self.session().request(method, url, timeout=self.timeout, **kwargs)
        try:
            data = loads(resp.content) if resp.content else None
        except ValueError:
            data = resp.text
        if resp.status_code not in ok_status:
            raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {resp.status_code}: {data}")
        return data

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


def run_async(coro):
    """Corre una corrutina desde un método SÍNCRONO. Desde código asíncrono hay
    que llamar al método con `asyncio.to_thread(...)`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("Este método es síncrono y abre su propio bucle: desde código asíncrono, "
                       "llámalo con `await asyncio.to_thread(...)`.")
