# -*- coding: utf-8 -*-
"""`BitunixBroker.get_order_status` en FUTUROS -- el estado real de una orden.

El bug que estos tests fijan: la implementación anterior no preguntaba por la
orden, sino que miraba la lista de PENDIENTES y daba por LLENADA toda orden
que no apareciera en ella:

    pendientes = ...futures.pending_orders(symbol)
    sigue_viva = any(str(o.get("orderId")) == str(order_id) for o in pendientes)
    return OrderStatus.NEW if sigue_viva else OrderStatus.FILLED

Una orden cancelada a mano en la app de Bitunix también desaparece de esa
lista. Resultado real: tres cortos de LTCUSDT (2082878949264547840,
2082879103115870208, 2082880194939265024) que el usuario canceló a mano
quedaron marcados FILLED en la base de datos, con `Trade` en ACTIVE -- una
"posición abierta" que en el exchange nunca existió. Y como la orden pasaba a
un estado no-vivo, ninguna reconciliación posterior volvía a mirarla: el
estado falso quedaba congelado para siempre.

Ahora se usa `/api/v1/futures/trade/get_order_detail`, que responde por la
orden concreta esté viva o cerrada, e incluye CANCELED como estado propio.
Ver `docs/plans/bugs.md`.
"""

import unittest
from unittest.mock import patch

from luarix_exchange.models.orders import OrderStatus


class _FakeFutures:
    """El cliente de futuros, reducido a lo único que se le pregunta acá."""

    def __init__(self, detalle):
        self.detalle = detalle
        self.consultas = []

    def order_detail(self, order_id):
        self.consultas.append(str(order_id))
        return self.detalle


class _FakeAccountData:
    def __init__(self, futures):
        self.futures = futures


def _broker(detalle):
    """Un `BitunixBroker` sin `__init__` (que abriría clientes HTTP reales),
    con solo el proveedor de datos de cuenta que `get_order_status` usa."""
    from luarix_exchange.brokers.bitunix import BitunixBroker

    broker = BitunixBroker.__new__(BitunixBroker)
    broker.account_data_provider = _FakeAccountData(_FakeFutures(detalle))
    return broker


class FuturesOrderStatusTests(unittest.TestCase):

    def test_cancelada_se_reporta_cancelada_y_no_llenada(self):
        # EL caso del bug. Antes esto devolvía FILLED.
        broker = _broker({"orderId": "2082878949264547840", "status": "CANCELED", "tradeQty": "0"})
        self.assertEqual(
            broker.get_order_status("LTCUSDT", "2082878949264547840", "futures"),
            OrderStatus.CANCELED,
        )

    def test_llenada_se_reporta_llenada(self):
        broker = _broker({"status": "FILLED", "tradeQty": "0.08"})
        self.assertEqual(broker.get_order_status("LTCUSDT", "1", "futures"), OrderStatus.FILLED)

    def test_viva_se_reporta_new(self):
        broker = _broker({"status": "NEW", "tradeQty": "0"})
        self.assertEqual(broker.get_order_status("LTCUSDT", "1", "futures"), OrderStatus.NEW)

    def test_init_cuenta_como_viva(self):
        # "En preparación": todavía no llegó al libro. Para el motor es
        # indistinguible de NEW -- sigue viva y no llenó.
        broker = _broker({"status": "INIT"})
        self.assertEqual(broker.get_order_status("LTCUSDT", "1", "futures"), OrderStatus.NEW)

    def test_part_filled_se_traduce_al_nombre_de_binance(self):
        # Bitunix dice PART_FILLED; el resto del motor habla PARTIALLY_FILLED.
        broker = _broker({"status": "PART_FILLED", "tradeQty": "0.04"})
        self.assertEqual(
            broker.get_order_status("LTCUSDT", "1", "futures"),
            OrderStatus.PARTIALLY_FILLED,
        )

    def test_orden_desconocida_se_trata_como_viva_no_como_llenada(self):
        # `get_order_detail` devuelve {} cuando el exchange no conoce la
        # orden. La ignorancia no es un fill: darla por llenada colocaría un
        # TP/SL real contra una posición inexistente (el `10002 Parameter
        # error` de bugs.md). Se prefiere volver a preguntar.
        broker = _broker({})
        self.assertEqual(broker.get_order_status("LTCUSDT", "1", "futures"), OrderStatus.NEW)

    def test_estado_no_reconocido_se_trata_como_viva(self):
        broker = _broker({"status": "ALGO_QUE_NO_CONOCEMOS"})
        self.assertEqual(broker.get_order_status("LTCUSDT", "1", "futures"), OrderStatus.NEW)

    def test_se_pregunta_por_el_id_de_la_orden_no_por_el_símbolo(self):
        broker = _broker({"status": "CANCELED"})
        broker.get_order_status("LTCUSDT", "2082879103115870208", "futures")
        self.assertEqual(broker.account_data_provider.futures.consultas, ["2082879103115870208"])

    def test_un_fallo_de_la_api_se_propaga_y_no_se_convierte_en_un_fill(self):
        # `unwrap` levanta ante `code=10004 Invalid IP` (la IP del servidor
        # fuera de la whitelist). Quien llama (`order_tracker._reconcile_order`)
        # lo trata como "no se pudo preguntar" y reintenta -- lo que NO puede
        # pasar es que un error de red acabe marcando órdenes como llenadas.
        from luarix_exchange.brokers.bitunix_response import BitunixApiError

        broker = _broker(None)
        with patch.object(
            broker.account_data_provider.futures, "order_detail",
            side_effect=BitunixApiError("code='10004' msg='Invalid IP'"),
        ):
            with self.assertRaises(BitunixApiError):
                broker.get_order_status("LTCUSDT", "1", "futures")


if __name__ == "__main__":
    unittest.main()
