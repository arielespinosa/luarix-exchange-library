# -*- coding: utf-8 -*-
"""De dónde salen las credenciales, y qué construye la factory.

El caso de producción es que las pase el backend (una clave por usuario);
el `.env` es la red de seguridad para cuadernos y scripts. Estos tests fijan
esa precedencia y los dos fallos que antes eran silenciosos: un broker
inexistente devolvía `None`, y pedirle credenciales de testnet a Bitunix
--que no tiene testnet-- devolvía dos cadenas vacías.
"""

import os
import tempfile
import unittest
from pathlib import Path

from luarix_exchange.brokers.credentials import BrokerCredentials, load_dotenv
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory


class DotenvTests(unittest.TestCase):

    def _escribir(self, contenido: str) -> Path:
        directorio = Path(tempfile.mkdtemp())
        archivo = directorio / ".env"
        archivo.write_text(contenido, encoding="utf-8")
        self.addCleanup(lambda: archivo.unlink(missing_ok=True))
        return archivo

    def test_lee_pares_clave_valor_e_ignora_comentarios(self):
        archivo = self._escribir("# comentario\nA=1\n\nB = 2\nsin_igual\n")
        self.assertEqual(load_dotenv(archivo), {"A": "1", "B": "2"})

    def test_quita_las_comillas(self):
        # Una clave API entre comillas firma mal, y el error que devuelve el
        # exchange ("credenciales inválidas") no apunta a esto en absoluto.
        archivo = self._escribir('K="abc"\nJ=\'def\'\n')
        self.assertEqual(load_dotenv(archivo), {"K": "abc", "J": "def"})

    def test_no_pisa_lo_que_ya_hay_en_el_entorno(self):
        # El `.env` es el apaño local; lo que inyecta el sistema (systemd, un
        # contenedor, el backend) tiene que poder ganarle.
        os.environ["_LUARIX_TEST_VAR"] = "del_entorno"
        self.addCleanup(os.environ.pop, "_LUARIX_TEST_VAR", None)
        load_dotenv(self._escribir("_LUARIX_TEST_VAR=del_fichero\n"))
        self.assertEqual(os.environ["_LUARIX_TEST_VAR"], "del_entorno")

    def test_con_override_si_pisa(self):
        os.environ["_LUARIX_TEST_VAR2"] = "del_entorno"
        self.addCleanup(os.environ.pop, "_LUARIX_TEST_VAR2", None)
        load_dotenv(self._escribir("_LUARIX_TEST_VAR2=del_fichero\n"), override=True)
        self.assertEqual(os.environ["_LUARIX_TEST_VAR2"], "del_fichero")


class CredencialesTests(unittest.TestCase):

    def test_desde_el_entorno(self):
        creds = BrokerCredentials.from_env("BITUNIX", env={
            "BITUNIX_API_KEY": "k", "BITUNIX_API_SECRET": "s",
        })
        self.assertEqual(creds.resolve(testnet=False), ("k", "s"))

    def test_acepta_el_alias_historico_de_binance(self):
        # El `.env` de este repo lleva `TEST_BINANCE_API_KEY` desde antes de
        # que existiera esta clase; renombrarlo a secas habría dejado a
        # cualquiera con un `.env` viejo mirando un "credenciales vacías".
        creds = BrokerCredentials.from_env("BINANCE", env={
            "TEST_BINANCE_API_KEY": "tk", "TEST_BINANCE_API_SECRET": "ts",
        })
        self.assertEqual(creds.resolve(testnet=True), ("tk", "ts"))

    def test_el_nombre_nuevo_gana_sobre_el_alias(self):
        creds = BrokerCredentials.from_env("BINANCE", env={
            "BINANCE_TEST_API_KEY": "nuevo", "BINANCE_TEST_API_SECRET": "nuevo_s",
            "TEST_BINANCE_API_KEY": "viejo", "TEST_BINANCE_API_SECRET": "viejo_s",
        })
        self.assertEqual(creds.resolve(testnet=True), ("nuevo", "nuevo_s"))

    def test_sin_testnet_cae_a_produccion(self):
        # EL caso de Bitunix: no tiene testnet. Antes `resolve(True)`
        # devolvía `("", "")` y la primera llamada fallaba con un
        # `10003 API key inexistente` que no dice nada de la causa real.
        creds = BrokerCredentials(api_key="k", api_secret="s")
        self.assertEqual(creds.resolve(testnet=True), ("k", "s"))
        self.assertFalse(creds.has_testnet)

    def test_vacias_son_falsy(self):
        # Un backend sin claves para ese usuario manda un objeto vacío; la
        # factory tiene que verlo y caer al `.env` en vez de mandar cadenas
        # vacías al exchange.
        self.assertFalse(BrokerCredentials())
        self.assertFalse(BrokerCredentials(api_key="solo_la_key"))
        self.assertTrue(BrokerCredentials(api_key="k", api_secret="s"))

    def test_broker_desconocido_devuelve_vacio_sin_reventar(self):
        self.assertFalse(BrokerCredentials.from_env("KUCOIN", env={}))


class FactoryTests(unittest.TestCase):

    def test_paper_no_necesita_credenciales(self):
        from luarix_exchange.brokers.paper import PaperBroker

        self.assertIsInstance(BrokerFactory.get_broker(AvailableBrokers.PAPER), PaperBroker)

    def test_acepta_el_nombre_en_texto(self):
        # Es como llega desde una API o una fila de base de datos.
        from luarix_exchange.brokers.paper import PaperBroker

        self.assertIsInstance(BrokerFactory.get_broker("paper"), PaperBroker)

    def test_un_broker_inexistente_levanta_y_nombra_los_disponibles(self):
        # Antes devolvía `None` en silencio y quien lo recibía reventaba
        # mucho más tarde con un AttributeError que no mencionaba el broker.
        with self.assertRaises(ValueError) as ctx:
            BrokerFactory.get_broker("KUCOIN")
        self.assertIn("BITUNIX", str(ctx.exception))

    def test_sin_credenciales_en_ningun_sitio_levanta_diciendo_que_falta(self):
        with self.assertRaises(ValueError) as ctx:
            BrokerFactory.get_broker(AvailableBrokers.BITUNIX, credentials=BrokerCredentials())
        mensaje = str(ctx.exception)
        # Sin credenciales en el entorno el mensaje tiene que decir QUÉ
        # variable definir. Si la máquina sí las tiene, el broker se
        # construye y este caso no aplica.
        self.assertIn("BITUNIX_API_KEY", mensaje)

    def test_bitunix_ignora_testnet(self):
        from luarix_exchange.brokers.bitunix import BitunixBroker

        creds = BrokerCredentials(api_key="prod_k", api_secret="prod_s",
                                  test_api_key="test_k", test_api_secret="test_s")
        # `testnet=True` es el DEFAULT de la factory, así que sin esta regla
        # el camino normal mandaba las claves de prueba a un exchange que no
        # tiene testnet.
        broker = BrokerFactory.get_broker(AvailableBrokers.BITUNIX, testnet=True, credentials=creds)
        self.assertIsInstance(broker, BitunixBroker)
        self.assertEqual(broker.api_key, "prod_k")

    def test_binance_si_respeta_testnet(self):
        creds = BrokerCredentials(api_key="prod_k", api_secret="prod_s",
                                  test_api_key="test_k", test_api_secret="test_s")
        broker = BrokerFactory.get_broker(AvailableBrokers.BINANCE, testnet=True, credentials=creds)
        self.assertEqual(broker.api_key, "test_k")


if __name__ == "__main__":
    unittest.main()
