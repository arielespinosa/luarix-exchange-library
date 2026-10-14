# -*- coding: utf-8 -*-
"""`unwrap` -- el desempaquetado de las respuestas de Bitunix.

Bitunix responde 200 tanto si acepta como si rechaza; lo que cambia es
`code` y que `data` viene en null. Estos casos son respuestas reales
capturadas del broker.
"""

import unittest

from luarix_exchange.brokers.bitunix_response import BitunixApiError, unwrap

# Respuesta real ante una IP no autorizada en la whitelist de la API key.
IP_BLOQUEADA = {"code": "100011", "msg": "Trigger IP risk control rules", "data": None, "success": False}
# Respuesta real de /api/spot/v1/user/account con la IP ya autorizada.
BALANCE_OK = {
    "code": 0, "msg": "Success", "success": True,
    "data": [{"coin": "USDT", "balance": "12.5", "balanceLocked": "0"}],
}


class UnwrapTests(unittest.TestCase):
    def test_devuelve_el_payload_cuando_la_llamada_fue_bien(self):
        self.assertEqual(unwrap(BALANCE_OK, "la consulta de balance"), BALANCE_OK["data"])

    def test_rechazo_levanta_con_code_y_msg(self):
        with self.assertRaises(BitunixApiError) as ctx:
            unwrap(IP_BLOQUEADA, "la consulta de balance")
        mensaje = str(ctx.exception)
        self.assertIn("100011", mensaje)
        self.assertIn("Trigger IP risk control rules", mensaje)

    def test_rechazo_levanta_aunque_haya_on_null(self):
        # `on_null` es para "fue bien pero no hay datos", NUNCA para tapar un
        # rechazo -- si no, una API key caducada seria indistinguible de un
        # rango sin velas.
        with self.assertRaises(BitunixApiError):
            unwrap(IP_BLOQUEADA, "la consulta de velas", on_null=[])

    def test_exito_sin_datos_usa_on_null(self):
        vacio = {"code": 0, "msg": "Success", "data": None}
        self.assertEqual(unwrap(vacio, "la consulta de velas", on_null=[]), [])

    def test_exito_sin_datos_y_sin_on_null_levanta(self):
        vacio = {"code": 0, "msg": "Success", "data": None}
        with self.assertRaises(BitunixApiError):
            unwrap(vacio, "la colocacion de la orden")

    def test_code_desconocido_con_datos_pasa(self):
        # No hay documentacion fiable de todos los codigos de exito de
        # Bitunix; tratar uno legitimo como error romperia llamadas que hoy
        # funcionan. El corte se hace donde es inequivoco: sin datos.
        raro = {"code": "00000", "msg": "ok", "data": [{"coin": "BTC"}]}
        self.assertEqual(unwrap(raro, "algo"), [{"coin": "BTC"}])

    def test_respuesta_sin_envoltorio_pasa_tal_cual(self):
        self.assertEqual(unwrap([1, 2, 3], "algo"), [1, 2, 3])
        self.assertEqual(unwrap({"price": "5"}, "algo"), {"price": "5"})

    def test_respuesta_vacia_levanta(self):
        with self.assertRaises(BitunixApiError):
            unwrap(None, "algo")


class FakeClient:
    """Solo lo que el proveedor le pide al cliente `bitunix` (spot)."""

    api_key = "k"
    api_secret = "s"

    def __init__(self, balance=None, pending=None):
        self._balance = balance
        self._pending = pending
        self.cancelled = None

    def get_account_balance(self):
        return self._balance

    def query_current_orders(self, symbol):
        self.pedido_para = symbol
        return self._pending

    def cancel_orders(self, order_id_list):
        self.cancelled = order_id_list
        return {"code": 0, "data": {}}


def _provider(**kwargs):
    from luarix_exchange.brokers.account_data.bitunix import BitunixAccountDataProvider

    return BitunixAccountDataProvider(FakeClient(**kwargs))


class SpotBalanceTests(unittest.TestCase):
    """El mapeo de claves de /api/spot/v1/user/account. Antes leia
    `asset`/`available`, claves que esa respuesta NO tiene, asi que el
    filtro descartaba todo y el balance salia siempre vacio."""

    def test_mapea_coin_y_devuelve_el_disponible(self):
        # `balance` (disponible), NO `balance + balanceLocked`: el contrato de
        # AccountDataProvider.get_balance es el saldo con el que se puede
        # operar, igual en todos los brokers y mercados.
        raw = {"code": 0, "data": [
            {"coin": "USDT", "balance": "10", "balanceLocked": "2.5"},
            {"coin": "BTC", "balance": "0", "balanceLocked": "0"},
        ]}
        self.assertEqual(_provider(balance=raw).get_balance(), {"USDT": 10.0, "BTC": 0.0})

    def test_rechazo_del_broker_se_propaga(self):
        with self.assertRaises(BitunixApiError):
            _provider(balance=IP_BLOQUEADA).get_balance()


class SpotOpenOrdersTests(unittest.TestCase):
    def test_lista_las_pendientes_del_simbolo(self):
        raw = {"code": "0", "data": [{"orderId": "7", "symbol": "BTCUSDT"}], "success": True}
        p = _provider(pending=raw)
        self.assertEqual(p.get_open_orders("spot", "BTCUSDT"), [{"orderId": "7", "symbol": "BTCUSDT"}])
        self.assertEqual(p.client.pedido_para, "BTCUSDT")

    def test_sin_simbolo_avisa_en_vez_de_devolver_vacio(self):
        # El endpoint spot de Bitunix EXIGE simbolo. Devolver [] aqui seria
        # leerse como "no tienes ordenes", que es la conclusion contraria.
        with self.assertRaises(BitunixApiError) as ctx:
            _provider(pending={"code": "0", "data": []}).get_open_orders("spot", None)
        self.assertIn("exige un símbolo", str(ctx.exception))

    def test_cancelar_pide_los_ids_listados(self):
        raw = {"code": "0", "data": [{"orderId": "7"}, {"orderId": "9"}], "success": True}
        p = _provider(pending=raw)
        self.assertEqual(p.cancel_open_orders("spot", "BTCUSDT"), ["7", "9"])
        self.assertEqual(p.client.cancelled,
                         [{"orderId": "7", "symbol": "BTCUSDT"}, {"orderId": "9", "symbol": "BTCUSDT"}])

    def test_cancelar_sin_pendientes_no_llama_al_broker(self):
        p = _provider(pending={"code": "0", "data": []})
        self.assertEqual(p.cancel_open_orders("spot", "BTCUSDT"), [])
        self.assertIsNone(p.client.cancelled)


class _FakeFuturesTransport:
    """Sustituye el transporte HTTP de `BitunixFuturesClient`, conservando su
    logica de desempaquetado (`pending_orders`, `position_mode`...)."""

    def __init__(self, payload):
        self.payload = payload
        self.enviado = None

    def __call__(self, path, params=None, **kwargs):
        self.enviado = (path, params)
        return self.payload


class FuturesOpenOrdersTests(unittest.TestCase):
    """Los futuros devuelven `{"total": "0", "orderList": [...]}` envuelto en
    el mismo sobre `code/msg/data` -- forma verificada contra la cuenta real."""

    def _con_respuesta(self, payload):
        from luarix_exchange.brokers.bitunix_response import unwrap

        p = _provider()
        transporte = _FakeFuturesTransport(payload)
        # Se parchea el GET crudo, no `pending_orders`: asi el test sigue
        # ejercitando el desempaquetado real del `orderList`.
        p.futures.get = lambda path, params=None, *, action, on_null=None: (
            unwrap(transporte(path, params), action, on_null=on_null)
            if on_null is not None else unwrap(transporte(path, params), action)
        )
        return p

    def test_desenvuelve_orderlist(self):
        payload = {"code": 0, "msg": "Success",
                   "data": {"total": "1", "orderList": [{"orderId": "42", "symbol": "BNBUSDT"}]}}
        self.assertEqual(self._con_respuesta(payload).get_open_orders("futures"),
                         [{"orderId": "42", "symbol": "BNBUSDT"}])

    def test_sin_ordenes_devuelve_lista_vacia(self):
        payload = {"code": 0, "msg": "Success", "data": {"total": "0", "orderList": []}}
        self.assertEqual(self._con_respuesta(payload).get_open_orders("futures"), [])

    def test_sin_simbolo_es_valido_en_futuros(self):
        # A diferencia del spot: aqui "todos los pares" si existe.
        payload = {"code": 0, "data": {"orderList": []}}
        self.assertEqual(self._con_respuesta(payload).get_open_orders("futures", None), [])


if __name__ == "__main__":
    unittest.main()
