# -*- coding: utf-8 -*-
"""WebSockets de Binance y Bitunix (Fase 5).

Los mensajes son los REALES capturados el 2026-10-18 (testnet de Binance para
órdenes; producción para datos públicos; claves falsas contra Bitunix para el
rechazo de login). Los flujos completos corren contra un servidor WebSocket
local que hace de exchange (las URLs del módulo se redirigen a él)."""

import asyncio
import hashlib
import hmac
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import mock

from luarix_exchange.brokers import binance_streams as bs
from luarix_exchange.brokers import bitunix_streams as us
from luarix_exchange.capabilities import CandleSubscription
from luarix_exchange.models.market import Candle
from luarix_exchange.models.orders import OrderStatus, Side
from luarix_exchange.streaming import StreamAuthError

KLINE_ABIERTA = {"e": "kline", "E": 1, "s": "BTCUSDT", "k": {
    "t": 1792335540000, "T": 1792335599999, "s": "BTCUSDT", "i": "1m",
    "o": "81100.0", "c": "81103.25", "h": "81110", "l": "81090", "v": "12.5", "x": False}}
KLINE_CERRADA = {**KLINE_ABIERTA, "k": {**KLINE_ABIERTA["k"], "x": True}}

# executionReport reales de testnet (recortados a los campos que se usan).
ER_NEW = {"e": "executionReport", "E": 1792335600000, "s": "BTCUSDT", "c": "tb-0a72", "S": "BUY", "o": "LIMIT",
          "q": "0.00020000", "p": "48717.70", "x": "NEW", "X": "NEW", "i": 4295606, "l": "0.00000000",
          "z": "0.00000000", "L": "0.00000000", "n": "0", "N": None, "Z": "0.00000000", "C": ""}
ER_CANCEL = {**ER_NEW, "c": "uxT3Clc6nK22EWJVrVeImp", "C": "tb-0a72", "x": "CANCELED", "X": "CANCELED"}
ER_FILL = {**ER_NEW, "i": 4295616, "o": "MARKET", "q": "0.00012000", "x": "TRADE", "X": "FILLED",
           "l": "0.00012000", "z": "0.00012000", "L": "81236.71000000", "n": "0.00000000", "N": "BTC",
           "Z": "9.74840520"}


class BinanceParserTests(unittest.TestCase):
    def test_solo_la_vela_cerrada(self):
        self.assertIsNone(bs.kline_to_candle(KLINE_ABIERTA))
        vela = bs.kline_to_candle(KLINE_CERRADA)
        self.assertEqual((vela.symbol, vela.close, vela.is_closed), ("BTCUSDT", 81103.25, True))

    def test_libro_spot_y_futuros(self):
        spot = bs.depth_to_book({"lastUpdateId": 7, "bids": [["100", "1"]], "asks": [["101", "2"]]}, "BTCUSDT", "spot", 5)
        fut = bs.depth_to_book({"e": "depthUpdate", "u": 9, "b": [["100", "1"]], "a": [["101", "2"]]}, "BTCUSDT", "futures", 5)
        self.assertEqual((spot.update_id, spot.best_ask), (7, (D("101"), D("2"))))
        self.assertEqual((fut.update_id, fut.best_bid), (9, (D("100"), D("1"))))

    def test_execution_report_nueva_cancelada_y_llenada(self):
        nueva = bs.execution_report_to_update(ER_NEW)
        self.assertEqual((nueva.status, nueva.side, nueva.last_fill_qty), (OrderStatus.NEW, Side.BUY, None))
        cancelada = bs.execution_report_to_update(ER_CANCEL)
        # En la cancelación, `c` es el id de la PETICIÓN de cancelar; el de la orden va en `C`.
        self.assertEqual((cancelada.status, cancelada.client_order_id), (OrderStatus.CANCELED, "tb-0a72"))
        llena = bs.execution_report_to_update(ER_FILL)
        self.assertEqual((llena.status, llena.last_fill_qty, llena.last_fill_price, llena.fee_asset),
                         (OrderStatus.FILLED, D("0.00012000"), D("81236.71000000"), "BTC"))
        self.assertEqual(llena.average_price.quantize(D("0.01")), D("81236.71"))

    def test_order_trade_update_de_futuros(self):
        ev = {"e": "ORDER_TRADE_UPDATE", "E": 5, "o": {"s": "BTCUSDT", "c": "x1", "S": "SELL", "q": "0.01",
              "x": "TRADE", "X": "PARTIALLY_FILLED", "i": 8, "l": "0.004", "z": "0.004", "L": "81000.5",
              "ap": "81000.5", "n": "0.03", "N": "USDT"}}
        u = bs.order_trade_update_to_update(ev)
        self.assertEqual((u.market.value, u.status, u.last_fill_qty, u.average_price),
                         ("futures", OrderStatus.PARTIALLY_FILLED, D("0.004"), D("81000.5")))
        self.assertIsNone(bs.order_trade_update_to_update({"e": "ACCOUNT_UPDATE"}))

    def test_firma_hmac_del_ejemplo_oficial_de_binance(self):
        # Ejemplo "SIGNED request example (HMAC)" de la documentación de la WebSocket API.
        params = {"apiKey": "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A",
                  "newOrderRespType": "ACK", "price": "52000.00", "quantity": "0.01000000", "recvWindow": 100,
                  "side": "SELL", "symbol": "BTCUSDT", "timeInForce": "GTC", "timestamp": 1645423376532,
                  "type": "LIMIT"}
        secreto = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        self.assertEqual(bs.sign_ws_api(params, secreto),
                         "cc15477742bd704c29492d96c7ead9414dfd8e0ec4a00f947bb5bb454ddbd08a")


class BitunixParserTests(unittest.TestCase):
    def test_login_firma_como_dice_la_documentacion(self):
        msg = us.login_message("KEY", "SECRET", nonce="abc", timestamp=1747402389)
        paso1 = hashlib.sha256(b"abc1747402389KEY").hexdigest()
        esperado = hashlib.sha256((paso1 + "SECRET").encode()).hexdigest()
        self.assertEqual(msg, {"op": "login", "args": [{"apiKey": "KEY", "timestamp": 1747402389,
                                                         "nonce": "abc", "sign": esperado}]})

    def test_respuestas_de_login_reales(self):
        self.assertIsNone(us.login_error({"op": "login", "data": {"result": True}}))
        rechazo = {"op": "login", "data": {"msg": "invalid params，Authorize: Token invalid", "result": False}}
        self.assertIn("Token invalid", us.login_error(rechazo))

    def test_push_de_orden_con_llenado_incremental(self):
        base = {"orderId": "11", "symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "qty": "0.01",
                "price": "80000", "mtime": "2026-10-18T15:00:01.123456789Z", "clientId": "c1"}
        parcial = us.order_push_to_update({**base, "orderStatus": "PART_FILLED", "dealAmount": "0.004",
                                           "averagePrice": "80000"}, 1, None)
        self.assertEqual((parcial.status, parcial.last_fill_qty), (OrderStatus.PARTIALLY_FILLED, D("0.004")))
        self.assertEqual(parcial.event_time_ms, 1792335601123)
        final = us.order_push_to_update({**base, "orderStatus": "PART_FILLED_CANCELED", "dealAmount": "0.006"},
                                        1, parcial.filled_qty)
        self.assertEqual((final.status, final.last_fill_qty), (OrderStatus.CANCELED, D("0.002")))


# ── Flujos completos contra un servidor WebSocket local ──────────────────

class _ServidorWS:
    """Servidor aiohttp en un hilo. `handler(ws, request)` es la lógica del
    falso exchange."""

    def __init__(self, handler):
        self.handler = handler
        self.listo = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._correr, daemon=True).start()
        self.listo.wait(5)

    def _correr(self):
        from aiohttp import web

        async def ruta(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            try:
                await self.handler(ws, request)
            finally:
                await ws.close()
            return ws

        async def iniciar():
            app = web.Application()
            app.router.add_get("/{tail:.*}", ruta)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            sitio = web.TCPSite(self.runner, "127.0.0.1", 0)
            await sitio.start()
            self.url = f"http://127.0.0.1:{sitio._server.sockets[0].getsockname()[1]}"
            self.listo.set()

        self.loop.run_until_complete(iniciar())
        self.loop.run_forever()

    def cerrar(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


async def _hasta(stream_call, cuantos, timeout=8):
    recibidos, stop = [], asyncio.Event()

    def cb(item):
        recibidos.append(item)
        if len(recibidos) >= cuantos:
            stop.set()

    await asyncio.wait_for(stream_call(cb, stop), timeout)
    return recibidos


def _binance():
    from luarix_exchange.brokers.binance import BinanceBroker

    broker = BinanceBroker.__new__(BinanceBroker)
    broker.testnet, broker.api_key, broker.api_secret = False, "KEY", "SECRET"
    broker.client = mock.Mock(timestamp_offset=0)
    broker.sync_clock = lambda: 0
    return broker


class BinanceFlowTests(unittest.TestCase):
    def test_velas_combinadas_solo_cerradas(self):
        async def exchange(ws, request):
            for k in (KLINE_ABIERTA, KLINE_CERRADA):
                await ws.send_json({"stream": "btcusdt@kline_1m", "data": k})
            await asyncio.sleep(5)

        srv = _ServidorWS(exchange)
        try:
            with mock.patch.dict(bs.SPOT_STREAM, {"main": f"{srv.url}/stream"}):
                velas = asyncio.run(_hasta(lambda cb, stop: _binance().stream_candles(
                    [CandleSubscription("BTCUSDT", "1m")], cb, stop), 1))
        finally:
            srv.cerrar()
        self.assertEqual([v.close for v in velas], [81103.25])

    def test_ordenes_spot_firma_y_eventos_envueltos(self):
        firmas_validas = []

        async def exchange(ws, request):
            peticion = await ws.receive_json()
            p = dict(peticion["params"])
            firma = p.pop("signature")
            payload = "&".join(f"{k}={p[k]}" for k in sorted(p))
            firmas_validas.append(firma == hmac.new(b"SECRET", payload.encode(), hashlib.sha256).hexdigest()
                                  and peticion["method"] == "userDataStream.subscribe.signature")
            await ws.send_json({"id": peticion["id"], "status": 200, "result": {"subscriptionId": 0}})
            await ws.send_json({"subscriptionId": 0, "event": ER_FILL})
            await asyncio.sleep(5)

        srv = _ServidorWS(exchange)
        try:
            with mock.patch.dict(bs.WS_API, {"main": srv.url}):
                ups = asyncio.run(_hasta(lambda cb, stop: _binance().stream_order_updates(cb, stop, "spot"), 1))
        finally:
            srv.cerrar()
        self.assertEqual(firmas_validas, [True])
        self.assertEqual((ups[0].order_id, ups[0].status), ("4295616", OrderStatus.FILLED))

    def test_suscripcion_rechazada_detiene_el_stream(self):
        async def exchange(ws, request):
            p = await ws.receive_json()
            await ws.send_json({"id": p["id"], "status": 400,
                                "error": {"code": -1021, "msg": "Timestamp for this request was 1000ms ahead"}})

        srv = _ServidorWS(exchange)
        try:
            with mock.patch.dict(bs.WS_API, {"main": srv.url}), self.assertRaisesRegex(StreamAuthError, "-1021"):
                asyncio.run(_hasta(lambda cb, stop: _binance().stream_order_updates(cb, stop, "spot"), 1))
        finally:
            srv.cerrar()


def _bitunix(velas_rest=None):
    from luarix_exchange.brokers.bitunix import BitunixBroker

    broker = BitunixBroker.__new__(BitunixBroker)
    broker.api_key, broker.api_secret = "KEY", "SECRET"
    broker.get_candles = mock.Mock(return_value=velas_rest or [])
    return broker


class BitunixFlowTests(unittest.TestCase):
    def test_el_cambio_de_periodo_dispara_la_lectura_rest_de_la_vela_cerrada(self):
        minuto = 1792335540000  # 15:39:00 UTC
        inicio = datetime.fromtimestamp(minuto / 1000, timezone.utc)
        cerrada = Candle("BTCUSDT", "1m", inicio, 1, 2, 0.5, 1.5, 10, is_closed=True)
        anterior = Candle("BTCUSDT", "1m", inicio - timedelta(minutes=1), 1, 2, 0.5, 1.5, 10, is_closed=True)
        suscripciones = []

        async def exchange(ws, request):
            suscripciones.append(await ws.receive_json())
            for ts in (minuto + 30_000, minuto + 59_500, minuto + 60_400):   # cruza al minuto siguiente
                await ws.send_json({"ch": "market_kline_1min", "symbol": "BTCUSDT", "ts": ts,
                                    "data": {"o": "1", "h": "2", "l": "0.5", "c": "1.5", "b": "10", "q": "15"}})
            await asyncio.sleep(5)

        broker = _bitunix([anterior, cerrada])
        srv = _ServidorWS(exchange)
        try:
            with mock.patch.object(us, "PUBLIC_WS", srv.url):
                velas = asyncio.run(_hasta(lambda cb, stop: broker.stream_candles(
                    [CandleSubscription("BTCUSDT", "1m", "futures")], cb, stop), 1))
        finally:
            srv.cerrar()
        self.assertEqual(suscripciones, [{"op": "subscribe", "args": [{"symbol": "BTCUSDT", "ch": "market_kline_1min"}]}])
        # Solo la que acaba de cerrar: sin historia.
        self.assertEqual([v.open_time for v in velas], [inicio])
        broker.get_candles.assert_called_with("BTCUSDT", "1m", 3, "futures")

    def test_el_saludo_connect_no_es_la_respuesta_al_login(self):
        async def exchange(ws, request):
            await ws.send_json({"op": "connect", "data": {"result": True}})
            await ws.receive_json()  # login
            await ws.send_json({"op": "login", "data": {"msg": "invalid params，Authorize: Token invalid",
                                                        "result": False}})
            await asyncio.sleep(2)

        srv = _ServidorWS(exchange)
        try:
            with mock.patch.object(us, "PRIVATE_WS", srv.url), self.assertRaisesRegex(StreamAuthError, "Token invalid"):
                asyncio.run(_hasta(lambda cb, stop: _bitunix().stream_order_updates(cb, stop, "futures"), 1))
        finally:
            srv.cerrar()

    def test_login_aceptado_suscribe_al_canal_de_ordenes(self):
        peticiones = []

        async def exchange(ws, request):
            await ws.send_json({"op": "connect", "data": {"result": True}})
            peticiones.append(await ws.receive_json())
            await ws.send_json({"op": "login", "data": {"result": True}})
            peticiones.append(await ws.receive_json())
            await ws.send_json({"ch": "order", "ts": 1, "data": {
                "event": "UPDATE", "orderId": "5", "symbol": "BTCUSDT", "side": "SELL", "orderStatus": "FILLED",
                "dealAmount": "0.01", "averagePrice": "81000", "mtime": "2026-10-18T15:00:01.5Z"}})
            await asyncio.sleep(5)

        srv = _ServidorWS(exchange)
        try:
            with mock.patch.object(us, "PRIVATE_WS", srv.url):
                ups = asyncio.run(_hasta(lambda cb, stop: _bitunix().stream_order_updates(cb, stop, "futures"), 1))
        finally:
            srv.cerrar()
        self.assertEqual(peticiones[0]["op"], "login")
        self.assertEqual(peticiones[1], {"op": "subscribe", "args": [{"ch": "order"}]})
        self.assertEqual((ups[0].status, ups[0].filled_qty, ups[0].last_fill_qty),
                         (OrderStatus.FILLED, D("0.01"), D("0.01")))

    def test_ordenes_de_spot_no_tienen_stream_nativo(self):
        with self.assertRaisesRegex(NotImplementedError, "PollingOrderUpdateStream"):
            asyncio.run(_bitunix().stream_order_updates(lambda u: None, asyncio.Event(), "spot"))


class SelectionByMarketTests(unittest.TestCase):
    def test_bitunix_nativo_en_futuros_polling_en_spot(self):
        from luarix_exchange.brokers.bitunix import BitunixBroker
        from luarix_exchange.streaming import PollingCandleStream, candle_stream

        broker = BitunixBroker.__new__(BitunixBroker)
        self.assertIs(candle_stream(broker, market="futures"), broker)
        self.assertIsInstance(candle_stream(broker, market="spot"), PollingCandleStream)

    def test_binance_nativo_en_los_dos(self):
        from luarix_exchange.streaming import candle_stream, order_update_stream

        broker = _binance()
        self.assertIs(candle_stream(broker, market="spot"), broker)
        self.assertIs(order_update_stream(broker, market="futures"), broker)


if __name__ == "__main__":
    unittest.main()
