# -*- coding: utf-8 -*-
"""Los flags que leen los consumidores NO cambian con el contrato por
capacidades (Fase 3).

web-server espeja `supports_oco` / `supports_independent_orders` en su
catálogo de brokers y decide con ellos cómo proteger una posición. Estos
valores son los de ANTES de la Fase 3, copiados de las clases; desde la
Fase 3 `supports_oco` y `supports_futures` se derivan de `capabilities()` y
`markets`, y tienen que seguir dando exactamente esto."""

import importlib
import unittest

# broker -> (supports_oco, supports_futures, supports_independent_orders)
ESPERADO = {
    "luarix_exchange.brokers.binance:BinanceBroker": (True, True, True),
    "luarix_exchange.brokers.bitunix:BitunixBroker": (False, True, True),
    "luarix_exchange.brokers.paper:PaperBroker": (True, False, True),
    "luarix_exchange.brokers.mt5:MT5Broker": (True, False, False),
}


def _clase(ruta: str):
    modulo, nombre = ruta.split(":")
    return getattr(importlib.import_module(modulo), nombre)


class FlagsCompatTests(unittest.TestCase):
    def test_los_flags_conservan_sus_valores(self):
        for ruta, esperado in ESPERADO.items():
            with self.subTest(ruta):
                try:
                    cls = _clase(ruta)
                except ImportError as exc:  # MT5 solo existe en Windows con el extra [mt5]
                    self.skipTest(f"{ruta}: {exc}")
                real = (cls.supports_oco, cls.supports_futures, cls.supports_independent_orders)
                self.assertEqual(real, esperado)


if __name__ == "__main__":
    unittest.main()
