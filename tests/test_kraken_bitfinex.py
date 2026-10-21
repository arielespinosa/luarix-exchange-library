# -*- coding: utf-8 -*-
"""Kraken y Bitfinex (Fase 6). Los tests de parseo vienen de
luarix-arbitrage-web-server (`engine/tests/test_kraken.py`, `test_bitfinex.py`)
con sus mensajes REALES; los demás cubren lo añadido para Luarix (velas,
libro por WebSocket con checksum, emisor de velas cerradas)."""

import asyncio
import json
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest import mock

from luarix_exchange.capabilities import Capability, CandleSubscription
from luarix_exchange.exchanges import bitfinex as bf
from luarix_exchange.exchanges import kraken as kr
from luarix_exchange.factory import ExchangeFactory
from luarix_exchange.models.market import Candle
from luarix_exchange.streaming.candles import ClosedCandleEmitter

# ── Kraken (parseo, de arbitraje) ─────────────────────────────────────────

ASSETS = {"XXBT": {"altname": "XBT"}, "ZUSD": {"altname": "USD"}, "ZEUR": {"altname": "EUR"}, "USDT": {"altname": "USDT"}}
XBTUSDT = {"altname": "XBTUSDT", "wsname": "XBT/USDT", "aclass_base": "currency", "base": "XXBT", "quote": "USDT",
           "pair_decimals": 1, "lot_decimals": 8, "ordermin": "0.00005", "costmin": "0.5", "tick_size": "0.1",
           "status": "online"}
SNAPSHOT = json.loads(
    '{"channel":"book","type":"snapshot","data":[{"symbol":"ETH/BTC",'
    '"bids":[{"price":0.030729,"qty":0.04060335},{"price":0.030727,"qty":0.20000000},{"price":0.030726,"qty":0.20000000},'
    '{"price":0.030725,"qty":2.96954510},{"price":0.030724,"qty":0.20000000},{"price":0.030723,"qty":0.20000000},'
    '{"price":0.030722,"qty":4.23400781},{"price":0.030721,"qty":0.03670000},{"price":0.030720,"qty":13.66738869},'
    '{"price":0.030719,"qty":0.37381237}],'
    '"asks":[{"price":0.030730,"qty":0.03670000},{"price":0.030734,"qty":0.03853960},{"price":0.030735,"qty":2.73216367},'
    '{"price":0.030736,"qty":0.21192000},{"price":0.030737,"qty":4.34586052},{"price":0.030738,"qty":13.66039750},'
    '{"price":0.030739,"qty":0.03670000},{"price":0.030740,"qty":4.23400781},{"price":0.030742,"qty":0.10000000},'
    '{"price":0.030746,"qty":9.79132390}],"checksum":1291533730,"timestamp":"2026-10-07T13:36:02.329161Z"}]}',
    parse_float=D,
)
UPDATE = json.loads(
    '{"channel":"book","type":"update","data":[{"symbol":"ETH/BTC","bids":[],'
    '"asks":[{"price":0.030734,"qty":0.03853064}],"checksum":2929094311,"timestamp":"2026-10-07T13:36:02.354568Z"}]}',
    parse_float=D,
)
ETHBTC_PRECISION = (6, 8)


class KrakenParseTests(unittest.TestCase):
    def test_activos_a_nombres_comunes(self):
        self.assertEqual((kr.canonical_asset("XXBT", ASSETS), kr.canonical_asset("ZEUR", ASSETS)), ("BTC", "EUR"))
        self.assertEqual((kr.canonical_asset("XBT"), kr.canonical_asset("XDG")), ("BTC", "DOGE"))
        self.assertEqual((kr.ws_symbol("XBT/USDT"), kr.ws_symbol("ETH/XBT")), ("BTC/USDT", "ETH/BTC"))

    def test_reglas_de_un_par(self):
        r = kr.rules_from_pair(XBTUSDT, ASSETS)
        self.assertEqual((r.symbol, r.base, r.quote, r.fee_on), ("BTC/USDT", "BTC", "USDT", "quote"))
        self.assertEqual((r.step_size, r.tick_size, r.min_qty, r.min_notional),
                         (D("0.00000001"), D("0.1"), D("0.00005"), D("0.5")))
        self.assertTrue(kr.tradable(XBTUSDT))
        self.assertFalse(kr.tradable({**XBTUSDT, "status": "post_only"}))

    def test_checksum_con_mensajes_reales(self):
        libro = kr.BookState("ETH/BTC", depth=10)
        libro.apply(SNAPSHOT["data"][0], snapshot=True)
        self.assertEqual(libro.checksum(*ETHBTC_PRECISION), SNAPSHOT["data"][0]["checksum"])
        libro.apply(UPDATE["data"][0])
        self.assertEqual(libro.checksum(*ETHBTC_PRECISION), UPDATE["data"][0]["checksum"])

    def test_libro_borra_y_recorta(self):
        libro = kr.BookState("ETH/BTC", depth=10)
        libro.apply(SNAPSHOT["data"][0], snapshot=True)
        libro.apply({"bids": [{"price": D("0.030729"), "qty": D("0")}], "asks": []})
        self.assertEqual(libro.snapshot(2000).bids[0][0], D("0.030727"))
        libro.apply({"bids": [], "asks": [{"price": D("0.030700"), "qty": D("1")}]})
        asks = libro.snapshot(3000).asks
        self.assertEqual((asks[0], len(asks), asks[-1][0]), ((D("0.030700"), D("1")), 10, D("0.030742")))

    def test_checksum_formatea_sin_punto_ni_ceros(self):
        self.assertEqual(kr.book_checksum([(D("0.03073"), D("0.0367"))], [], 6, 8), zlib.crc32(b"307303670000"))

    def test_timestamp_con_y_sin_nanosegundos(self):
        self.assertEqual(kr.parse_timestamp("2026-10-07T13:36:02.329161Z"), 1791380162329)
        self.assertEqual(kr.parse_timestamp("2026-10-18T16:11:00.000000000Z") % 60_000, 0)

    def test_firma_del_ejemplo_oficial(self):
        secreto = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
        datos = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
        self.assertEqual(kr.sign_request("/0/private/AddOrder", "1616492376594", datos, secreto),
                         "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==")

    def test_comisiones_en_porcentaje(self):
        r = kr.rates_from_trade_volume({"fees": {"XBTUSDT": {"fee": "0.8000"}}, "fees_maker": {"XBTUSDT": {"fee": "0.4000"}}},
                                       "XBTUSDT", "BTC/USDT")
        self.assertEqual((r.taker, r.maker), (D("0.008"), D("0.004")))

    def test_vela_ohlc(self):
        v = kr.candle_from_ohlc("BTC/USDT", "1m", [1792339920, "81140.0", "81150.5", "81130.1", "81134.9", "81140", "0.5", 7])
        self.assertEqual((v.open, v.high, v.low, v.close, v.volume), (81140.0, 81150.5, 81130.1, 81134.9, 0.5))

    def test_solo_velas_cerradas_segun_last(self):
        ex = kr.KrakenExchange()
        ex._key = lambda s: "XBTUSDT"
        filas = [[1792339860, "1", "2", "0.5", "1.5", "1", "1", 1], [1792339920, "1", "2", "0.5", "1.5", "1", "1", 1],
                 [1792339980, "1", "2", "0.5", "1.5", "1", "0", 0]]  # la última está en curso
        with mock.patch.object(ex, "_public", return_value={"XBTUSDT": filas, "last": 1792339920}):
            velas = ex.get_candles("BTC/USDT", "1m", 10)
        self.assertEqual([int(v.open_time.timestamp()) for v in velas], [1792339860, 1792339920])

    def test_historico_mas_antiguo_que_lo_que_sirve_kraken_levanta(self):
        ex = kr.KrakenExchange()
        ex._key = lambda s: "XBTUSDT"
        filas = [[1792339860, "1", "2", "0.5", "1.5", "1", "1", 1]]
        with mock.patch.object(ex, "_public", return_value={"XBTUSDT": filas, "last": 1792339860}), \
                self.assertRaisesRegex(ValueError, "720"):
            ex.get_historical_candles("BTC/USDT", "1m", start=datetime(2020, 1, 1, tzinfo=timezone.utc))

    def test_opciones_y_latencia(self):
        with self.assertRaisesRegex(ValueError, "depth_levels"):
            kr.KrakenExchange(depth_levels=20)
        self.assertEqual(kr.KrakenExchange().latency_symbol(["ETH/BTC", "BTC/USDT"]), "BTC/USDT")
        with self.assertRaisesRegex(ValueError, "solo cubre spot"):
            kr.KrakenExchange().list_symbols("futures")


# ── Bitfinex (parseo, de arbitraje) ───────────────────────────────────────

CMAP = {"UST": "USDt", "UDC": "USDC", "DSH": "DASH"}


class BitfinexParseTests(unittest.TestCase):
    def test_pares_y_monedas(self):
        self.assertEqual((bf.split_pair("BTCUST"), bf.split_pair("AVAX:BTC")), (("BTC", "UST"), ("AVAX", "BTC")))
        self.assertEqual(bf.canonical_currency("UST", CMAP), "USDT")
        r = bf.rules_from_pair("BTCUST", [1, None, None, "0.00004", "2000.0", None, None, None, 0.1, 0.05, None, None], CMAP)
        self.assertEqual((r.symbol, r.base, r.quote, r.min_qty, r.fee_on), ("tBTCUST", "BTC", "USDT", D("0.00004"), "received"))

    def test_libro_foto_y_actualizaciones(self):
        libro = bf.BookState("tBTCUST")
        libro.apply_snapshot([[85595, 2, 0.023], [85594, 1, 1], [85663, 1, -0.1167], [85624, 1, -0.058]])
        snap = libro.snapshot(25, received_ms=1000)
        self.assertEqual(snap.bids, [(D("85595"), D("0.023")), (D("85594"), D("1"))])
        self.assertEqual(snap.asks, [(D("85624"), D("0.058")), (D("85663"), D("0.1167"))])
        libro.apply_update([85624, 0, -1])
        libro.apply_update([85642, 3, -0.397])
        libro.apply_update([85595, 1, 0.5])
        snap = libro.snapshot(1, received_ms=2000, event_ms=1999)
        self.assertEqual((snap.bids, snap.asks, snap.event_ms), ([(D("85595"), D("0.5"))], [(D("85642"), D("0.397"))], 1999))

    def test_checksum(self):
        libro = bf.BookState("tBTCUST")
        libro.apply_snapshot([["85595", "2", "0.02307425"], ["85594", "1", "1"], ["85663", "1", "-0.11674308"],
                              ["85624", "1", "-0.05839818"]])
        esperado = zlib.crc32(b"85595:0.02307425:85624:-0.05839818:85594:1:85663:-0.11674308")
        esperado = esperado - (1 << 32) if esperado >= (1 << 31) else esperado
        self.assertEqual(libro.checksum(), esperado)

    def test_tipos_de_mensaje(self):
        self.assertEqual(bf.parse_message([491518, "hb", 1791225917198]), ("hb", 491518, None, 1791225917198))
        self.assertEqual(bf.parse_message(["491518", "cs", "-1234567", "1791225917198"]),
                         ("cs", 491518, [-1234567], 1791225917198))
        self.assertEqual(bf.parse_message([491518, [85624, 0, -1]])[3], None)

    def test_comision_segun_el_quote(self):
        tasas = [0.002, 0.0015, 0.001]
        self.assertEqual([bf.taker_fee_for(q, tasas) for q in ("BTC", "USDT", "EUR")], [D("0.002"), D("0.0015"), D("0.001")])

    def test_columnas_de_vela_cierre_antes_que_maximo(self):
        v = bf.candle_from_row("tBTCUST", "1m", [1792339920000, 81145, 81158, 81165, 81105, 0.59])
        self.assertEqual((v.open, v.high, v.low, v.close), (81145.0, 81165.0, 81105.0, 81158.0))

    def test_opciones(self):
        with self.assertRaisesRegex(ValueError, "depth_levels"):
            bf.BitfinexExchange(depth_levels=10)
        self.assertEqual(bf.BitfinexExchange().latency_symbol(["tETHBTC", "tBTCUST"]), "tBTCUST")


# ── Registro y capacidades ────────────────────────────────────────────────

class RegistroTests(unittest.TestCase):
    ESPERADAS = {Capability.MARKET_DATA, Capability.SYMBOL_RULES, Capability.SERVER_CLOCK, Capability.FEES,
                 Capability.ASSET_INFO, Capability.ORDER_BOOK_STREAM, Capability.CANDLE_STREAM,
                 Capability.TICKER_STATS}

    def test_matriz(self):
        self.assertEqual(ExchangeFactory.capabilities("kraken"), self.ESPERADAS)
        self.assertEqual(ExchangeFactory.capabilities("bitfinex"), self.ESPERADAS)
        self.assertIn("kraken", ExchangeFactory.find(Capability.ORDER_BOOK_STREAM, market="spot"))

    def test_create_sin_claves_sirve_para_datos_publicos(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertIsInstance(ExchangeFactory.create("kraken"), kr.KrakenExchange)

    def test_lo_privado_sin_claves_levanta_con_motivo(self):
        ex = kr.KrakenExchange()
        with self.assertRaisesRegex(ValueError, "KRAKEN_API_KEY"):
            ex._private("TradeVolume")


# ── Libro de Kraken por WebSocket, contra un servidor local ───────────────

class KrakenBookStreamTests(unittest.TestCase):
    def _correr(self, mensajes, corromper=False):
        from aiohttp import web

        aperturas = []

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            aperturas.append(await ws.receive_json())
            await ws.send_str(json.dumps({"method": "subscribe", "success": True}))
            for m in mensajes:
                texto = json.dumps(m, default=str)
                if corromper and aperturas and len(aperturas) == 1 and m is mensajes[-1]:
                    texto = texto.replace("2929094311", "1")
                await ws.send_str(texto)
            await asyncio.sleep(3)
            return ws

        async def main():
            app = web.Application()
            app.router.add_get("/", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"
            ex = kr.KrakenExchange(depth_levels=10)
            ex._load_pairs = lambda force=False: {}
            ex._precision = {"ETH/BTC": ETHBTC_PRECISION}
            libros, stop = [], asyncio.Event()

            def cb(libro):
                libros.append(libro)
                if len(libros) >= 2:
                    stop.set()

            try:
                with mock.patch.object(kr, "WS_PUBLIC", url):
                    await asyncio.wait_for(ex.stream_order_books(["ETH/BTC"], 10, cb, stop), 10)
            finally:
                await runner.cleanup()
            return libros, aperturas

        return asyncio.run(main())

    def test_foto_y_actualizacion_con_checksum_valido(self):
        libros, aperturas = self._correr([SNAPSHOT, UPDATE])
        self.assertEqual(aperturas[0]["params"], {"channel": "book", "symbol": ["ETH/BTC"], "depth": 10, "snapshot": True})
        self.assertEqual(libros[1].asks[1], (D("0.030734"), D("0.03853064")))
        self.assertEqual(libros[1].event_ms, kr.parse_timestamp("2026-10-07T13:36:02.354568Z"))

    def test_checksum_que_no_cuadra_reabre_la_conexion(self):
        libros, aperturas = self._correr([SNAPSHOT, UPDATE], corromper=True)
        self.assertGreaterEqual(len(aperturas), 2)  # se reconectó y volvió a suscribirse


# ── Emisor de velas cerradas ──────────────────────────────────────────────

T0 = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)


def _vela(minuto):
    return Candle("BTC/USDT", "1m", T0 + timedelta(minutes=minuto), 1, 2, 0.5, 1.5, 1, is_closed=True)


class ClosedCandleEmitterTests(unittest.TestCase):
    SUB = CandleSubscription("BTC/USDT", "1m")

    def test_cambio_de_periodo_emite_solo_la_cerrada_y_no_repite(self):
        llamadas, emitidas = [], []

        def fetch(sub):
            llamadas.append(1)
            return [_vela(0), _vela(1), _vela(2)]

        async def main():
            em = ClosedCandleEmitter(fetch, emitidas.append, {self.SUB: 60_000}, retry_s=0.01)
            base = int(T0.timestamp() * 1000)
            await em.period_seen(self.SUB, base + 2 * 60_000 + 500)    # primer mensaje: fija el periodo
            await em.period_seen(self.SUB, base + 3 * 60_000 + 100)    # cambia: cerró la de las 16:02
            await em.emit_closed(self.SUB, base + 2 * 60_000)          # el temporizador llega tarde: nada
        asyncio.run(main())
        self.assertEqual([v.open_time.minute for v in emitidas], [2])
        self.assertEqual(len(llamadas), 1)

    def test_temporizador_emite_sin_mensajes_del_websocket(self):
        emitidas = []

        async def main():
            stop = asyncio.Event()
            ahora = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            vela = Candle("BTC/USDT", "1m", ahora - timedelta(minutes=0), 1, 2, 0.5, 1.5, 1, is_closed=True)

            def cb(v):
                emitidas.append(v)
                stop.set()
            # periodo de 1 s para no esperar un minuto: la vela "cerrada" es la del segundo anterior
            sub = CandleSubscription("X", "1s")
            em = ClosedCandleEmitter(lambda s: [Candle("X", "1s", datetime.fromtimestamp(
                int(datetime.now(timezone.utc).timestamp()) - 1, tz=timezone.utc), 1, 1, 1, 1, 1)], cb,
                {sub: 1000}, grace_s=0.05, retry_s=0.01)
            await asyncio.wait_for(em.run_boundary_timer(stop), 5)
        asyncio.run(main())
        self.assertEqual(len(emitidas), 1)


class BinanceFeesTests(unittest.TestCase):
    def test_descuento_solo_si_activo_en_cuenta_y_par(self):
        from luarix_exchange.brokers.binance import parse_spot_commission

        base = {"symbol": "BTCUSDT", "standardCommission": {"maker": "0.00100000", "taker": "0.00100000"}}
        con = parse_spot_commission({**base, "discount": {"enabledForAccount": True, "enabledForSymbol": True,
                                                          "discountAsset": "BNB", "discount": "0.75000000"}})
        sin = parse_spot_commission({**base, "discount": {"enabledForAccount": True, "enabledForSymbol": False,
                                                          "discount": "0.75000000"}})
        self.assertEqual((con.effective_taker, sin.discount), (D("0.00075"), None))


if __name__ == "__main__":
    unittest.main()
