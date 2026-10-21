# -*- coding: utf-8 -*-
"""Bybit, Gate, HTX, KuCoin, MEXC y OKX, portados de luarix-arbitrage-web-server
(`engine/tests/test_new_exchanges.py`, respuestas reales de cada API recortadas):
reglas, activos, libros (fotos, cambios, huecos y checksum), protobuf y firmas."""

import asyncio
import gzip
import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal as D

from luarix_exchange.capabilities import Capability
from luarix_exchange.exchanges import bybit, gate, htx, kucoin, mexc, okx
from luarix_exchange.exchanges._common import LevelBook, levels, loads, protobuf_fields, step_from_decimals
from luarix_exchange.factory import ExchangeFactory


class RegistroTests(unittest.TestCase):
    def test_registrados_y_validan_su_profundidad(self):
        for nombre in ("bybit", "gate", "htx", "kucoin", "mexc", "okx"):
            self.assertIn(nombre, ExchangeFactory.available())
        for cls, malo in ((gate.GateExchange, 7), (kucoin.KucoinExchange, 20), (htx.HtxExchange, 50),
                          (mexc.MexcExchange, 50), (bybit.BybitExchange, 25), (okx.OkxExchange, 500)):
            with self.assertRaisesRegex(ValueError, "depth_levels"):
                cls(depth_levels=malo)
        self.assertEqual(okx.OkxExchange(depth_levels=5).channel, "books5")

    def test_capacidades(self):
        base = {Capability.SYMBOL_RULES, Capability.SERVER_CLOCK, Capability.ORDER_BOOK_STREAM, Capability.FEES,
                Capability.ASSET_INFO}
        for nombre in ("gate", "htx", "kucoin", "mexc"):
            self.assertEqual(ExchangeFactory.capabilities(nombre), base)
        for nombre in ("bybit", "okx"):  # testnet / modo demo
            self.assertEqual(ExchangeFactory.capabilities(nombre), base | {Capability.ORDER_ROUNDTRIP})

    def test_sin_claves_lo_privado_levanta_con_motivo(self):
        with self.assertRaisesRegex(ValueError, "OKX_API_KEY"):
            okx.OkxExchange().get_commissions(["BTC-USDT"])
        with self.assertRaisesRegex(ValueError, "passphrase"):
            kucoin.KucoinExchange(api_key="k", api_secret="s").get_commissions(["BTC-USDT"])

    def test_pedir_mas_niveles_de_los_configurados_levanta(self):
        with self.assertRaisesRegex(ValueError, "depth_levels"):
            asyncio.run(gate.GateExchange(depth_levels=5).stream_order_books(["BTC_USDT"], 20, lambda b: None,
                                                                             asyncio.Event()))


class ComunesTests(unittest.TestCase):
    def test_ayudantes(self):
        self.assertEqual((step_from_decimals(6), step_from_decimals(0)), (D("0.000001"), D(1)))
        self.assertEqual(loads('{"p": 81938.47, "q": 6.1E-5}'), {"p": D("81938.47"), "q": D("0.000061")})
        self.assertEqual(levels([["1.5", "2", "0", "6"]]), [(D("1.5"), D(2))])
        libro = LevelBook("X")
        libro.reset([(D(10), D(1)), (D(9), D(2))], [(D(11), D(1))], 1)
        libro.apply([(D(10), D(0)), (D("9.5"), D(3))], [(D(12), D(4))], 2)
        snap = libro.snapshot(0, 5, event_ms=99)
        self.assertEqual((snap.bids, snap.asks, snap.update_id, snap.event_ms),
                         ([(D("9.5"), D(3)), (D(9), D(2))], [(D(11), D(1)), (D(12), D(4))], 2, 99))


GATE_PAIR = {"id": "BTC_USDT", "base": "BTC", "quote": "USDT", "min_base_amount": "0.000001", "min_quote_amount": "3",
             "amount_precision": 6, "precision": 1, "trade_status": "tradable", "type": "normal"}


class GateTests(unittest.TestCase):
    def test_reglas_activos_y_libro(self):
        r = gate.rules_from_pair(GATE_PAIR)
        self.assertEqual((r.symbol, r.base, r.quote, r.step_size, r.tick_size, r.min_qty, r.min_notional, r.fee_on),
                         ("BTC_USDT", "BTC", "USDT", D("0.000001"), D("0.1"), D("0.000001"), D(3), "received"))
        self.assertFalse(gate.tradable({**GATE_PAIR, "trade_status": "untradable"}))
        usdt = gate.listing_from_currency({"currency": "USDT", "chains": [
            {"deposit_disabled": True, "withdraw_disabled": True}, {"deposit_disabled": False, "withdraw_disabled": True}]})
        self.assertEqual((usdt.deposit_enabled, usdt.withdraw_enabled), (True, False))
        self.assertIsNone(gate.listing_from_currency({"currency": "USDT_EOS"}))
        eth = gate.listing_from_currency({"currency": "ETH", "name": "Ethereum", "chains": [
            {"name": "ETH", "addr": "", "deposit_disabled": False, "withdraw_disabled": True}]})
        self.assertEqual((eth.name, eth.networks[0].withdraw_enabled, eth.networks[0].withdraw_fee), ("Ethereum", False, None))
        self.assertFalse(gate.listing_from_currency({"currency": "OLD", "delisted": True}).withdraw_enabled)
        libro = gate.parse_book({"t": 1792014399000, "lastUpdateId": 7, "s": "BTC_USDT",
                                 "bids": [["81947.6", "0.5"], ["81946.4", "0.03"]], "asks": [["81948", "1"]]}, 5, 20)
        self.assertEqual((libro.bids[0], libro.asks, libro.update_id, libro.event_ms),
                         ((D("81947.6"), D("0.5")), [(D(81948), D(1))], 7, 1792014399000))

    def test_firma(self):
        firma = gate.sign("secreto", "GET", "/api/v4/wallet/fee", "currency_pair=BTC_USDT", "", "1700000000")
        self.assertEqual(len(firma), 128)
        self.assertNotEqual(firma, gate.sign("secreto", "GET", "/api/v4/wallet/fee", "currency_pair=ETH_USDT", "", "1700000000"))
        self.assertEqual(gate.rates_from_fee("BTC_USDT", {"maker_fee": "0.002", "taker_fee": "0.002"}).taker, D("0.002"))


class KucoinTests(unittest.TestCase):
    INFO = {"symbol": "BTC-USDT", "baseCurrency": "BTC", "quoteCurrency": "USDT", "feeCurrency": "USDT",
            "baseMinSize": "0.00001", "baseIncrement": "0.00000001", "priceIncrement": "0.1", "minFunds": "0.1",
            "enableTrading": True}

    def test_reglas_activos_libro_y_firma(self):
        r = kucoin.rules_from_symbol(self.INFO)
        self.assertEqual((r.base, r.quote, r.step_size, r.tick_size, r.min_qty, r.min_notional, r.fee_on),
                         ("BTC", "USDT", D("0.00000001"), D("0.1"), D("0.00001"), D("0.1"), "quote"))
        self.assertEqual(kucoin.rules_from_symbol({**self.INFO, "feeCurrency": "BTC"}).fee_on, "received")
        self.assertIsNone(kucoin.listing_from_currency({"currency": "X", "chains": None}).deposit_enabled)
        usdt = kucoin.listing_from_currency({"currency": "USDT", "fullName": "Tether", "chains": [
            {"chainName": "ERC20", "contractAddress": "0xdac", "isDepositEnabled": True, "isWithdrawEnabled": False,
             "withdrawalMinFee": "3.2", "withdrawalMinSize": "10"}]})
        [red] = usdt.networks
        self.assertEqual((usdt.name, red.network, red.contract_address, red.withdraw_fee, red.min_withdraw),
                         ("Tether", "ERC20", "0xdac", D("3.2"), D(10)))
        self.assertEqual(kucoin.book_topic(50, ["BTC-USDT", "ETH-USDT"]), "/spotMarket/level2Depth50:BTC-USDT,ETH-USDT")
        libro = kucoin.parse_book("/spotMarket/level2Depth50:BTC-USDT", {"asks": [["81934.2", "0.003"]],
                                  "bids": [["81934.1", "1"]], "timestamp": 1792014399000}, 9, 50)
        self.assertEqual((libro.symbol, libro.update_id, libro.event_ms, libro.bids),
                         ("BTC-USDT", 1792014399000, 1792014399000, [(D("81934.1"), D(1))]))
        cab = kucoin.auth_headers("clave", "secreto", "frase", "GET", "/api/v1/trade-fees?symbols=BTC-USDT",
                                  timestamp="1700000000000")
        self.assertEqual(cab["KC-API-PASSPHRASE"], kucoin.sign("secreto", "frase"))
        self.assertEqual(cab["KC-API-SIGN"], kucoin.sign("secreto", "1700000000000GET/api/v1/trade-fees?symbols=BTC-USDT"))
        with self.assertRaisesRegex(LookupError, "passphrase"):
            kucoin.auth_headers("k", "s", "", "GET", "/x")


class HtxTests(unittest.TestCase):
    def test_reglas_activos_libro_y_firma(self):
        info = loads('{"symbol": "btcusdt", "state": "online", "bc": "btc", "qc": "usdt", "pp": 2, "ap": 6, '
                     '"minoa": 1e-05, "minov": 1, "at": "enabled"}')
        r = htx.rules_from_symbol(info)
        self.assertEqual((r.symbol, r.base, r.quote, r.step_size, r.tick_size, r.min_qty, r.min_notional),
                         ("btcusdt", "BTC", "USDT", D("0.000001"), D("0.01"), D("0.00001"), D(1)))
        rica = htx.listing_from_currency({"currency": "usdt", "chains": [
            {"chain": "trc20usdt", "displayName": "TRC20", "contractAddress": "TR7", "depositStatus": "allowed",
             "withdrawStatus": "allowed", "transactFeeWithdraw": "1", "minWithdrawAmt": "10"}]})
        self.assertEqual((rica.asset, rica.symbol, rica.networks[0].network, rica.networks[0].withdraw_fee),
                         ("USDT", "usdt", "TRC20", D(1)))
        crudo = gzip.compress(b'{"ch":"market.btcusdt.mbp.refresh.20","ts":1792014399403,"tick":{"seqNum":5,'
                              b'"bids":[[81938.47,0.315009]],"asks":[[81939.1,6.1E-5]]}}')
        libro = htx.parse_book(htx.decode(crudo), 0, 20)
        self.assertEqual((libro.symbol, libro.bids, libro.asks, libro.event_ms),
                         ("btcusdt", [(D("81938.47"), D("0.315009"))], [(D("81939.1"), D("0.000061"))], 1792014399403))
        consulta = htx.signed_query("clave", "secreto", "GET", "/v2/reference/transact-fee-rate", {"symbols": "btcusdt"},
                                    datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
        self.assertTrue(consulta.startswith("AccessKeyId=clave&SignatureMethod=HmacSHA256&SignatureVersion=2&"
                                            "Timestamp=2026-10-14T12%3A00%3A00&symbols=btcusdt"))
        tasas = htx.rates_from_fee({"symbol": "btcusdt", "makerFeeRate": "0.002", "takerFeeRate": "0.002",
                                    "actualTakerRate": "0.0018"})
        self.assertEqual((tasas.taker, tasas.maker), (D("0.0018"), D("0.002")))


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number: int, value: bytes | int) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _mexc_message(symbol, asks, bids, sent):
    def level(p, q):
        return _field(1, p.encode()) + _field(2, q.encode())

    body = b"".join(_field(1, level(*a)) for a in asks) + b"".join(_field(2, level(*b)) for b in bids) + _field(4, b"42")
    return (_field(1, f"spot@public.limit.depth.v3.api.pb@{symbol}@20".encode()) + _field(3, symbol.encode())
            + _field(6, sent) + _field(303, body))


class MexcTests(unittest.TestCase):
    def test_reglas_y_libro_protobuf(self):
        info = {"symbol": "BTCUSDT", "status": "1", "baseAsset": "BTC", "quoteAsset": "USDT", "quotePrecision": 2,
                "baseAssetPrecision": 8, "quoteAmountPrecision": "1", "baseSizePrecision": "0.000001",
                "isSpotTradingAllowed": True}
        r = mexc.rules_from_symbol(info)
        self.assertEqual((r.step_size, r.tick_size, r.min_qty, r.min_notional), (D("0.000001"), D("0.01"), D("0.000001"), D(1)))
        self.assertEqual(mexc.rules_from_symbol({**info, "baseSizePrecision": "0"}).step_size, D("0.00000001"))
        crudo = _mexc_message("BTCUSDT", [("81946.27", "0.1"), ("81946.26", "0.5")], [("81946.20", "2")], 1792014399000)
        self.assertEqual(protobuf_fields(crudo)[3], [b"BTCUSDT"])
        libro, enviado = mexc.parse_book(crudo, 0, 20)
        self.assertEqual((enviado, libro.symbol, libro.update_id, libro.event_ms), (1792014399000, "BTCUSDT", 42, 1792014399000))
        self.assertEqual(libro.asks, [(D("81946.26"), D("0.5")), (D("81946.27"), D("0.1"))])
        self.assertIsNone(mexc.parse_book(_field(1, b"otro canal"), 0, 20))
        self.assertEqual(mexc.rates_from_fee("BTCUSDT", {"data": {"makerCommission": 0, "takerCommission": D("0.0005")}}).taker,
                         D("0.0005"))


class BybitTests(unittest.TestCase):
    def test_reglas_foto_delta_y_hueco(self):
        info = {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading",
                "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000001", "minOrderAmt": "5"},
                "priceFilter": {"tickSize": "0.1"}}
        r = bybit.rules_from_instrument(info)
        self.assertEqual((r.step_size, r.tick_size, r.min_qty, r.min_notional), (D("0.000001"), D("0.1"), D("0.000001"), D(5)))
        libros: dict = {}
        with self.assertRaises(bybit.BookGap):
            bybit.apply_message(libros, {"type": "delta", "data": {"s": "BTCUSDT", "b": [], "a": [], "u": 5}})
        bybit.apply_message(libros, {"type": "snapshot", "data": {"s": "BTCUSDT", "b": [["100", "1"], ["99", "2"]],
                                                                   "a": [["101", "1"]], "u": 10}})
        libro = bybit.apply_message(libros, {"type": "delta", "data": {"s": "BTCUSDT", "b": [["100", "0"]],
                                                                        "a": [["100.5", "3"]], "u": 11}})
        out = libro.snapshot(0, 50)
        self.assertEqual((out.bids, out.asks[0], out.update_id), ([(D(99), D(2))], (D("100.5"), D(3)), 11))
        cab = bybit.auth_headers("clave", "secreto", "category=spot", timestamp="1700000000000")
        self.assertEqual(cab["X-BAPI-SIGN"], bybit.sign("secreto", "1700000000000", "clave", "category=spot"))
        limite, qty = bybit.far_limit_order(D(80000), r, D("0.01"))
        self.assertEqual(limite, D("79360.0"))
        self.assertGreaterEqual(qty * limite, r.min_notional)


class OkxTests(unittest.TestCase):
    def test_reglas_secuencia_y_checksum(self):
        info = {"instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT", "lotSz": "0.00000001", "tickSz": "0.1",
                "minSz": "0.00001", "state": "live"}
        r = okx.rules_from_instrument(info)
        self.assertEqual((r.symbol, r.base, r.step_size, r.tick_size, r.min_qty),
                         ("BTC-USDT", "BTC", D("0.00000001"), D("0.1"), D("0.00001")))
        libros: dict = {}
        okx.apply_message(libros, "BTC-USDT", "snapshot", {"bids": [["100", "1", "0", "1"]], "asks": [["101", "2", "0", "1"]],
                                                           "seqId": 10, "prevSeqId": -1, "checksum": 0})
        libro = okx.apply_message(libros, "BTC-USDT", "update", {"bids": [["100.5", "3", "0", "1"]], "asks": [],
                                                                 "seqId": 11, "prevSeqId": 10})
        self.assertEqual(libro.snapshot(0, 25).bids[0], (D("100.5"), D(3)))
        with self.assertRaisesRegex(okx.BookGap, "perdió"):
            okx.apply_message(libros, "BTC-USDT", "update", {"bids": [], "asks": [], "seqId": 13, "prevSeqId": 12})
        bids, asks = levels([["100.5", "3"], ["100", "1"]]), levels([["101", "2"]])
        bueno = okx.book_checksum(bids, asks)
        fresco: dict = {}
        okx.apply_message(fresco, "BTC-USDT", "snapshot", {"bids": [["100.5", "3"], ["100", "1"]], "asks": [["101", "2"]],
                                                           "seqId": 1, "checksum": bueno})
        with self.assertRaisesRegex(okx.BookGap, "checksum"):
            okx.apply_message(fresco, "BTC-USDT", "update", {"bids": [["100", "5"]], "asks": [], "seqId": 2, "prevSeqId": 1,
                                                             "checksum": bueno})
        general, grupos = okx.rates_by_group({"maker": "-0.0008", "taker": "-0.001",
                                              "feeGroup": [{"groupId": "12", "maker": "-0.0006", "taker": "-0.0008"}]})
        self.assertEqual((general.taker, grupos["12"].maker), (D("0.001"), D("0.0006")))
        cab = okx.auth_headers("clave", "secreto", "frase", "GET", "/api/v5/account/trade-fee?instType=SPOT",
                               now=datetime(2026, 10, 14, 12, 0, 0, 123000, tzinfo=UTC))
        self.assertEqual((cab["OK-ACCESS-TIMESTAMP"], cab["OK-ACCESS-PASSPHRASE"]), ("2026-10-14T12:00:00.123Z", "frase"))
        with self.assertRaisesRegex(LookupError, "passphrase"):
            okx.auth_headers("k", "s", "", "GET", "/x")

    def test_las_suscripciones_viajan_como_json_plano(self):
        self.assertEqual(json.loads(json.dumps({"op": "subscribe", "args": [okx.OkxExchange().channel]}))["args"], ["books"])


if __name__ == "__main__":
    unittest.main()
