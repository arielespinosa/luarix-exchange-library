# -*- coding: utf-8 -*-
"""Infraestructura de streaming (Fase 4): reconexión, polling de velas,
libros y órdenes, puente síncrono, selección y sesión WebSocket."""

import asyncio
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from luarix_exchange.capabilities import CandleStream, CandleSubscription, Capability, Exchange, MarketData
from luarix_exchange.models.book import OrderBook
from luarix_exchange.models.market import Candle
from luarix_exchange.models.orders import OrderInfo, OrderStatus, Side
from luarix_exchange.streaming import (
    PollingCandleStream,
    PollingOrderBookStream,
    PollingOrderUpdateStream,
    ReconnectPolicy,
    StreamRunner,
    candle_stream,
    order_book_stream,
    order_update_stream,
    run_with_reconnect,
    websocket_session,
)

RAPIDO = ReconnectPolicy(backoff_s=0.01, backoff_max_s=0.02)
T0 = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)


def _vela(minuto, symbol="BTCUSDT", cerrada=True):
    return Candle(symbol=symbol, timeframe="1m", open_time=T0 + timedelta(minutes=minuto),
                  open=1, high=1, low=1, close=1, volume=1, is_closed=cerrada)


def _correr(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class _Secuencia:
    """Devuelve las respuestas en orden y luego repite la última. Un elemento
    que sea una excepción se lanza."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = 0

    def siguiente(self):
        self.llamadas += 1
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, BaseException):
            raise r
        return r


class ReconnectTests(unittest.TestCase):
    def test_reintenta_tras_errores_de_red(self):
        intentos = []

        async def main():
            stop = asyncio.Event()

            async def sesion():
                intentos.append(1)
                if len(intentos) < 3:
                    raise ConnectionError("caída")
                stop.set()

            await run_with_reconnect(sesion, stop, RAPIDO)

        _correr(main())
        self.assertEqual(len(intentos), 3)

    def test_un_error_que_no_es_de_red_se_propaga(self):
        async def main():
            async def sesion():
                raise ValueError("símbolo inválido")

            await run_with_reconnect(sesion, asyncio.Event(), RAPIDO)

        with self.assertRaises(ValueError):
            _correr(main())

    def test_una_sesion_que_termina_limpia_se_reabre(self):
        aperturas = []

        async def main():
            stop = asyncio.Event()

            async def sesion():
                aperturas.append(1)
                if len(aperturas) == 3:
                    stop.set()

            await run_with_reconnect(sesion, stop, RAPIDO)

        _correr(main())
        self.assertEqual(len(aperturas), 3)


class _FakeMarketData(MarketData):
    def __init__(self, velas=None, libros=None):
        self.velas = velas or {}
        self.libros = libros

    def get_candles(self, symbol, interval="1m", limit=100, market="spot"):
        return self.velas[symbol].siguiente()

    def get_order_book(self, symbol, limit=20, market="spot"):
        return self.libros.siguiente()

    def get_historical_candles(self, *a, **kw):
        return []

    def get_ticker(self, symbol, market="spot"):
        return 1.0

    def list_symbols(self, market="spot"):
        return []


async def _recoger(stream_call, cuantos, timeout=5.0):
    recibidos, stop = [], asyncio.Event()

    def cb(item):
        recibidos.append(item)
        if len(recibidos) >= cuantos:
            stop.set()

    await asyncio.wait_for(stream_call(cb, stop), timeout)
    return recibidos


class PollingCandleTests(unittest.TestCase):
    def test_solo_velas_nuevas_cerradas_una_vez_y_en_orden(self):
        md = _FakeMarketData(velas={"BTCUSDT": _Secuencia([
            [_vela(0), _vela(1)],                              # marca de agua, no emite historia
            [_vela(0), _vela(1), _vela(2)],                    # 2
            [_vela(1), _vela(2), _vela(3, cerrada=False)],     # nada: la 3 sigue abierta
            [_vela(2), _vela(3), _vela(4)],                    # 3 y 4
        ])})
        stream = PollingCandleStream(md, poll_s=0.01, policy=RAPIDO)
        subs = [CandleSubscription("BTCUSDT", "1m")]
        velas = _correr(_recoger(lambda cb, stop: stream.stream_candles(subs, cb, stop), 3))
        self.assertEqual([v.open_time.minute for v in velas], [2, 3, 4])

    def test_un_error_de_red_no_pierde_la_marca_de_agua(self):
        md = _FakeMarketData(velas={"BTCUSDT": _Secuencia([
            [_vela(0)], ConnectionError("timeout"), [_vela(0), _vela(1)],
        ])})
        stream = PollingCandleStream(md, poll_s=0.01, policy=RAPIDO)
        velas = _correr(_recoger(lambda cb, stop: stream.stream_candles([CandleSubscription("BTCUSDT", "1m")], cb, stop), 1))
        self.assertEqual([v.open_time.minute for v in velas], [1])

    def test_cada_suscripcion_lleva_su_marca(self):
        md = _FakeMarketData(velas={
            "BTCUSDT": _Secuencia([[_vela(0)], [_vela(0), _vela(1)]]),
            "ETHUSDT": _Secuencia([[_vela(5, "ETHUSDT")], [_vela(5, "ETHUSDT"), _vela(6, "ETHUSDT")]]),
        })
        stream = PollingCandleStream(md, poll_s=0.01, policy=RAPIDO)
        subs = [CandleSubscription("BTCUSDT", "1m"), CandleSubscription("ETHUSDT", "1m")]
        velas = _correr(_recoger(lambda cb, stop: stream.stream_candles(subs, cb, stop), 2))
        self.assertEqual(sorted((v.symbol, v.open_time.minute) for v in velas), [("BTCUSDT", 1), ("ETHUSDT", 6)])


class PollingBookTests(unittest.TestCase):
    def test_solo_emite_cuando_cambian_los_mejores_niveles(self):
        a = OrderBook.from_levels("BTCUSDT", "spot", [(100, 1)], [(101, 1)])
        b = OrderBook.from_levels("BTCUSDT", "spot", [(100, 2)], [(101, 1)])
        md = _FakeMarketData(libros=_Secuencia([a, a, a, b]))
        stream = PollingOrderBookStream(md, poll_s=0.01, policy=RAPIDO)
        libros = _correr(_recoger(lambda cb, stop: stream.stream_order_books(["BTCUSDT"], 1, cb, stop), 2))
        self.assertEqual([l.bids[0][1] for l in libros], [1, 2])
        self.assertEqual(md.libros.llamadas, 4)


def _orden(oid, status, filled=0.0, symbol="BTCUSDT"):
    return OrderInfo(order_id=oid, symbol=symbol, status=status, side=Side.BUY, quantity=1.0,
                     filled_quantity=filled, average_price=100.0 if filled else None)


class _FakeAccount:
    name = "fake"

    def __init__(self, pendientes, detalles=None):
        self.pendientes = pendientes          # _Secuencia de listas
        self.detalles = detalles or {}        # order_id -> _Secuencia
        self.consultas_pendientes = []

    def get_open_orders(self, market="spot", symbol=None):
        self.consultas_pendientes.append(symbol)
        return self.pendientes.siguiente()

    def get_order(self, symbol, order_id, market="spot"):
        return self.detalles[order_id].siguiente()


# El stream solo exige la interfaz; los dobles no necesitan heredar de Account.
class PollingOrderTests(unittest.TestCase):
    def test_cambios_de_estado_y_llenado_final_preguntado_no_inferido(self):
        cuenta = _FakeAccount(
            pendientes=_Secuencia([
                [_orden("1", OrderStatus.NEW)],                          # estado de partida
                [_orden("1", OrderStatus.PARTIALLY_FILLED, 0.5)],        # llenado parcial
                [],                                                       # desaparece
            ]),
            detalles={"1": _Secuencia([_orden("1", OrderStatus.FILLED, 1.0)])},
        )
        stream = PollingOrderUpdateStream(cuenta, poll_s=0.01, policy=RAPIDO)
        ups = _correr(_recoger(lambda cb, stop: stream.stream_order_updates(cb, stop), 2))
        self.assertEqual([(u.status, str(u.last_fill_qty)) for u in ups],
                         [(OrderStatus.PARTIALLY_FILLED, "0.5"), (OrderStatus.FILLED, "0.5")])
        self.assertEqual(str(ups[1].filled_qty), "1.0")
        self.assertEqual(str(ups[1].average_price), "100.0")

    def test_si_no_se_puede_leer_el_estado_final_reintenta_en_vez_de_inventar(self):
        cuenta = _FakeAccount(
            pendientes=_Secuencia([[_orden("7", OrderStatus.NEW)], []]),
            detalles={"7": _Secuencia([RuntimeError("Bitunix: no encuentro la orden"),
                                        _orden("7", OrderStatus.CANCELED)])},
        )
        stream = PollingOrderUpdateStream(cuenta, poll_s=0.01, policy=RAPIDO)
        with self.assertLogs("luarix_exchange.streaming.polling", level="WARNING") as logs:
            ups = _correr(_recoger(lambda cb, stop: stream.stream_order_updates(cb, stop), 1))
        self.assertEqual(ups[0].status, OrderStatus.CANCELED)
        self.assertIsNone(ups[0].last_fill_qty)
        self.assertTrue(any("no encuentro la orden" in linea for linea in logs.output))

    def test_track_sigue_una_orden_que_nunca_aparece_en_pendientes(self):
        cuenta = _FakeAccount(pendientes=_Secuencia([[]]),
                              detalles={"9": _Secuencia([_orden("9", OrderStatus.FILLED, 1.0)])})
        stream = PollingOrderUpdateStream(cuenta, poll_s=0.01, policy=RAPIDO)
        stream.track("BTCUSDT", "9")
        ups = _correr(_recoger(lambda cb, stop: stream.stream_order_updates(cb, stop), 1))
        self.assertEqual((ups[0].order_id, ups[0].status, str(ups[0].last_fill_qty)), ("9", OrderStatus.FILLED, "1.0"))

    def test_con_symbols_consulta_par_a_par(self):
        cuenta = _FakeAccount(pendientes=_Secuencia([[]]))
        stream = PollingOrderUpdateStream(cuenta, symbols=["BTCUSDT", "ETHUSDT"], poll_s=0.01, policy=RAPIDO)

        async def una_vuelta():
            stop = asyncio.Event()
            tarea = asyncio.ensure_future(stream.stream_order_updates(lambda u: None, stop))
            await asyncio.sleep(0.05)
            stop.set()
            await tarea

        _correr(una_vuelta())
        self.assertEqual(cuenta.consultas_pendientes[:2], ["BTCUSDT", "ETHUSDT"])


class StreamRunnerTests(unittest.TestCase):
    def test_entrega_por_callback_desde_su_hilo_y_para(self):
        md = _FakeMarketData(velas={"BTCUSDT": _Secuencia([[_vela(0)], [_vela(0), _vela(1)], [_vela(1), _vela(2)]])})
        stream = PollingCandleStream(md, poll_s=0.01, policy=RAPIDO)
        recibidas, hilos, listo = [], set(), threading.Event()

        def cb(vela):
            recibidas.append(vela)
            hilos.add(threading.current_thread().name)
            if len(recibidas) >= 2:
                listo.set()

        with StreamRunner(name="test-runner") as runner:
            handle = runner.start(lambda stop: stream.stream_candles([CandleSubscription("BTCUSDT", "1m")], cb, stop))
            self.assertTrue(listo.wait(5))
            handle.stop()
            self.assertFalse(handle.running)
            self.assertIsNone(handle.exception())
        self.assertEqual(hilos, {"test-runner"})
        self.assertEqual([v.open_time.minute for v in recibidas[:2]], [1, 2])

    def test_el_error_de_un_stream_queda_visible(self):
        async def roto(stop):
            raise ValueError("formato inesperado")

        with StreamRunner() as runner:
            handle = runner.start(roto, label="roto")
            with self.assertRaises(ValueError):
                handle.wait(5)
            self.assertIsInstance(handle.exception(), ValueError)


class _ConStreamNativo(Exchange, CandleStream):
    async def stream_candles(self, subscriptions, on_candle, stop):
        pass


class SelectionTests(unittest.TestCase):
    def test_nativo_si_lo_declara(self):
        ex = _ConStreamNativo()
        self.assertIs(candle_stream(ex), ex)

    def test_polling_si_solo_tiene_rest(self):
        from luarix_exchange.brokers.binance import BinanceBroker

        broker = BinanceBroker.__new__(BinanceBroker)
        self.assertIsInstance(candle_stream(broker), PollingCandleStream)
        self.assertIsInstance(order_book_stream(broker), PollingOrderBookStream)
        self.assertIsInstance(order_update_stream(broker), PollingOrderUpdateStream)

    def test_paper_no_tiene_datos_de_mercado_pero_si_cuenta(self):
        from luarix_exchange.brokers.paper import PaperBroker

        paper = PaperBroker()
        self.assertNotIn(Capability.MARKET_DATA, paper.capabilities())
        with self.assertRaises(NotImplementedError):
            candle_stream(paper)
        self.assertIsInstance(order_update_stream(paper), PollingOrderUpdateStream)


class WebSocketSessionTests(unittest.TestCase):
    """Contra un servidor aiohttp REAL en localhost."""

    def test_suscribe_entrega_json_y_termina_cuando_el_servidor_cierra(self):
        from aiohttp import web

        suscripciones = []

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            suscripciones.append(await ws.receive_json())
            for i in range(3):
                await ws.send_json({"n": i})
            await ws.close()
            return ws

        async def main():
            app = web.Application()
            app.router.add_get("/ws", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            puerto = site._server.sockets[0].getsockname()[1]
            recibidos = []
            try:
                await websocket_session(f"http://127.0.0.1:{puerto}/ws", recibidos.append, asyncio.Event(),
                                        subscribe=[{"method": "SUBSCRIBE", "params": ["btcusdt@kline_1m"]}])
            finally:
                await runner.cleanup()
            return recibidos

        recibidos = _correr(main())
        self.assertEqual(suscripciones, [{"method": "SUBSCRIBE", "params": ["btcusdt@kline_1m"]}])
        self.assertEqual(recibidos, [{"n": 0}, {"n": 1}, {"n": 2}])

    def test_stop_corta_una_conexion_que_no_manda_nada(self):
        from aiohttp import web

        fin_servidor = None

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await fin_servidor.wait()        # conexión abierta y muda hasta el final del test
            return ws

        async def main():
            nonlocal fin_servidor
            fin_servidor = asyncio.Event()
            app = web.Application()
            app.router.add_get("/ws", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            puerto = site._server.sockets[0].getsockname()[1]
            stop = asyncio.Event()
            asyncio.get_running_loop().call_later(0.2, stop.set)
            inicio = time.monotonic()
            try:
                await websocket_session(f"http://127.0.0.1:{puerto}/ws", lambda m: None, stop)
                return time.monotonic() - inicio
            finally:
                fin_servidor.set()
                await runner.cleanup()

        self.assertLess(_correr(main()), 3)


if __name__ == "__main__":
    unittest.main()
