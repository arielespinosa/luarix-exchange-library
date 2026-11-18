# -*- coding: utf-8 -*-
"""Parser/validador del webhook de TradingView.

Fija el contrato de ingesta: que se parsea, que se normaliza, que se rechaza
y como se verifica el secreto. Es logica pura (sin red ni Django), asi que se
prueba entera aqui, en la libreria.
"""

import json
import unittest
from datetime import timezone
from decimal import Decimal

from luarix_exchange.trading_view import (
    Direction,
    EventType,
    TradingViewPayloadError,
    TradingViewWebhookParser,
)


def _entry(**over):
    base = {
        "schema_version": 1,
        "strategy_id": "smc_btc_001",
        "instance_id": "tv_instance_001",
        "event_id": "evt_000123",
        "sequence": 1528,
        "event": "ENTRY",
        "trade_id": "trade_001",
        "symbol": "btcusdt",
        "timeframe": "4H",
        "direction": "long",
        "entry_price": "95000",
        "stop_loss": "93000",
        "take_profit": "100000",
        "timestamp": "2026-11-18T12:00:00Z",
    }
    base.update(over)
    return base


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.parser = TradingViewWebhookParser()

    def test_parsea_y_normaliza_una_entrada(self):
        sig = self.parser.parse(json.dumps(_entry()))
        self.assertEqual(sig.event, EventType.ENTRY)
        self.assertEqual(sig.event_id, "evt_000123")
        self.assertEqual(sig.sequence, 1528)
        self.assertEqual(sig.symbol, "BTCUSDT")          # upper-case
        self.assertEqual(sig.direction, Direction.LONG)  # enum, upper-case
        self.assertEqual(sig.entry_price, Decimal("95000"))
        self.assertEqual(sig.stop_loss, Decimal("93000"))
        self.assertIsNotNone(sig.timestamp)
        self.assertEqual(sig.timestamp.tzinfo, timezone.utc)

    def test_acepta_bytes_y_dict(self):
        self.assertEqual(self.parser.parse(json.dumps(_entry()).encode()).event, EventType.ENTRY)
        self.assertEqual(self.parser.parse(_entry()).event, EventType.ENTRY)

    def test_cada_tipo_de_evento_es_valido(self):
        for ev in ("EXIT", "PARTIAL_EXIT", "STOP_LOSS", "TAKE_PROFIT", "UPDATE_SL",
                   "UPDATE_TP", "CANCEL"):
            sig = self.parser.parse(_entry(event=ev, direction=None))
            self.assertEqual(sig.event, EventType(ev))

    def test_heartbeat_con_posicion_no_exige_simbolo(self):
        payload = {
            "event": "HEARTBEAT",
            "event_id": "hb_1",
            "strategy_id": "smc_btc_001",
            "sequence": 1530,
            "position": {"status": "OPEN", "direction": "LONG", "entry_price": 95000},
        }
        sig = self.parser.parse(payload)
        self.assertEqual(sig.event, EventType.HEARTBEAT)
        self.assertEqual(sig.position["status"], "OPEN")

    def test_timestamp_epoch_segundos_y_ms(self):
        s = self.parser.parse(_entry(timestamp=1763467200)).timestamp
        ms = self.parser.parse(_entry(timestamp=1763467200000)).timestamp
        self.assertEqual(s, ms)

    def test_dedup_key(self):
        sig = self.parser.parse(_entry())
        self.assertEqual(sig.dedup_key, ("tv_instance_001", "evt_000123"))


class RejectTests(unittest.TestCase):
    def setUp(self):
        self.parser = TradingViewWebhookParser()

    def test_json_malformado(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse("{no es json")

    def test_json_no_objeto(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse("[1, 2, 3]")

    def test_falta_event(self):
        p = _entry()
        del p["event"]
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(p)

    def test_falta_event_id(self):
        p = _entry()
        del p["event_id"]
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(p)

    def test_evento_desconocido(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(_entry(event="FOO"))

    def test_direccion_desconocida(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(_entry(direction="SIDEWAYS"))

    def test_entrada_sin_simbolo(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(_entry(symbol=None))

    def test_entrada_sin_direccion(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(_entry(direction=None))

    def test_precio_invalido(self):
        with self.assertRaises(TradingViewPayloadError):
            self.parser.parse(_entry(entry_price="x"))


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.parser = TradingViewWebhookParser()

    def test_hash_y_verificacion_correcta(self):
        secret = self.parser.generate_secret()
        h = self.parser.hash_secret(secret)
        payload = _entry(secret=secret)
        self.assertTrue(self.parser.verify_secret(payload, h))

    def test_secreto_incorrecto(self):
        h = self.parser.hash_secret("el-bueno")
        self.assertFalse(self.parser.verify_secret(_entry(secret="otro"), h))

    def test_secreto_ausente(self):
        h = self.parser.hash_secret("el-bueno")
        self.assertFalse(self.parser.verify_secret(_entry(), h))

    def test_hash_esperado_vacio(self):
        self.assertFalse(self.parser.verify_secret(_entry(secret="x"), ""))

    def test_el_secreto_no_queda_en_raw(self):
        sig = self.parser.parse(_entry(secret="muy-secreto"))
        self.assertNotIn("secret", sig.raw)
        self.assertIn("strategy_id", sig.raw)

    def test_generate_secret_es_aleatorio(self):
        self.assertNotEqual(self.parser.generate_secret(), self.parser.generate_secret())


if __name__ == "__main__":
    unittest.main()
