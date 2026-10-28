# -*- coding: utf-8 -*-
"""El contrato de `Broker` cumplido por Binance y Bitunix, con clientes falsos.

Lo que fija este fichero es que las SEIS operaciones que hacen falta para
operar una cuenta -- velas, saldo, posiciones, órdenes abiertas, detalle de
una orden, crear/editar -- devuelvan lo MISMO con los dos brokers, en spot y
en futuros. Sin eso, un grafo que funciona contra Binance falla contra
Bitunix por razones que no tienen nada que ver con la estrategia.

Los clientes son dobles con las respuestas REALES de cada API (nombres de
campo incluidos: `origQty` vs `qty`, `positionAmt` firmado vs `qty` sin
signo). Ahí es exactamente donde estaban los bugs.
"""

import unittest
from datetime import datetime, timezone

from luarix_exchange.models.brokers import BrokerPosition
from luarix_exchange.models.orders import OrderInfo, OrderStatus
from luarix_exchange.models.orders import Side


# ═══════════════════════════════════════════════════════════════════════
# Normalizadores
# ═══════════════════════════════════════════════════════════════════════

class OrderInfoTests(unittest.TestCase):
    """`OrderInfo` traduciendo las dos APIs al mismo vocabulario."""

    def test_binance_spot(self):
        info = OrderInfo.from_binance({
            "orderId": 28, "symbol": "BTCUSDT", "status": "PARTIALLY_FILLED",
            "side": "BUY", "type": "LIMIT", "origQty": "1.0", "executedQty": "0.4",
            "price": "50000.0", "stopPrice": "0.00000000",
            "clientOrderId": "tb-abc", "time": 1700000000000,
        })
        self.assertEqual(info.order_id, "28")
        self.assertEqual(info.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(info.side, Side.BUY)
        self.assertEqual(info.quantity, 1.0)
        self.assertEqual(info.filled_quantity, 0.4)
        self.assertEqual(info.remaining_quantity, 0.6)
        self.assertTrue(info.is_open)
        # `stopPrice: "0"` significa "esta orden no tiene stop", no "stop en
        # cero" -- que sería un nivel real y catastrófico.
        self.assertIsNone(info.stop_price)

    def test_bitunix_futures_part_filled_es_partially_filled(self):
        # Bitunix dice PART_FILLED donde Binance dice PARTIALLY_FILLED. Sin
        # traducir, un `status == "PARTIALLY_FILLED"` nunca sería cierto acá.
        info = OrderInfo.from_bitunix({
            "orderId": "2082878949264547840", "symbol": "LTCUSDT",
            "status": "PART_FILLED", "side": "SELL", "orderType": "LIMIT",
            "qty": "2.2", "tradeQty": "1.1", "price": "44.99",
            "tpPrice": "45.37", "slPrice": "0", "tradeSide": "OPEN",
            "ctime": "1700000000000",
        }, market="futures")
        self.assertEqual(info.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(info.side, Side.SELL)
        self.assertEqual(info.quantity, 2.2)
        self.assertEqual(info.filled_quantity, 1.1)
        self.assertEqual(info.tp_price, 45.37)
        self.assertIsNone(info.sl_price)      # "0" = sin stop adjunto
        self.assertFalse(info.reduce_only)    # tradeSide=OPEN

    def test_bitunix_spot_usa_el_lado_numerico(self):
        # El spot de Bitunix codifica el lado como 1/2, no como texto.
        # Leerlo como cadena daría `side=None` y recolocar una orden sin
        # saber el lado abriría una posición en la dirección contraria.
        self.assertEqual(OrderInfo.from_bitunix({"side": 1, "id": "9"}).side, Side.BUY)
        self.assertEqual(OrderInfo.from_bitunix({"side": "2", "id": "9"}).side, Side.SELL)

    def test_bitunix_spot_usa_volume_y_dealVolume(self):
        info = OrderInfo.from_bitunix({
            "id": "77", "symbol": "BTCUSDT", "status": "NEW", "side": 1,
            "volume": "0.5", "dealVolume": "0.1", "price": "60000",
        })
        self.assertEqual(info.order_id, "77")
        self.assertEqual(info.quantity, 0.5)
        self.assertEqual(info.filled_quantity, 0.1)

    def test_tradeSide_close_es_reduce_only(self):
        info = OrderInfo.from_bitunix({"id": "1", "tradeSide": "CLOSE"}, market="futures")
        self.assertTrue(info.reduce_only)

    def test_estado_desconocido_se_trata_como_viva(self):
        # Conservador a propósito: dar por cerrada una orden que sigue viva
        # llevaría a colocar un TP/SL contra una posición inexistente.
        self.assertEqual(OrderInfo.from_bitunix({"status": "LO_QUE_SEA"}).status, OrderStatus.NEW)


class BrokerPositionTests(unittest.TestCase):

    def test_binance_firma_la_cantidad_y_bitunix_no(self):
        # EL detalle que hace falta normalizar: los dos describen el mismo
        # corto de 2 unidades, uno con la cantidad negativa y otro con la
        # dirección aparte. Sin esto, `qty > 0` significa cosas distintas.
        binance = BrokerPosition.from_binance_futures({
            "symbol": "BTCUSDT", "positionAmt": "-2.0", "entryPrice": "50000",
            "markPrice": "49000", "unRealizedProfit": "2000", "leverage": "10",
            "positionSide": "BOTH",
        })
        bitunix = BrokerPosition.from_bitunix_futures({
            "symbol": "BTCUSDT", "qty": "2.0", "side": "SELL", "avgOpenPrice": "50000",
            "markPrice": "49000", "unrealizedPNL": "2000", "leverage": "10",
            "positionId": "9", "marginMode": "CROSS",
        })
        for p in (binance, bitunix):
            self.assertEqual(p.side, "SHORT")
            self.assertEqual(p.quantity, 2.0)
            self.assertEqual(p.entry_price, 50000.0)
            self.assertEqual(p.unrealized_pnl, 2000.0)
            self.assertEqual(p.leverage, 10.0)

    def test_position_side_explicito_gana_sobre_el_signo(self):
        # En modo cobertura Binance sí manda LONG/SHORT y hay que respetarlo.
        p = BrokerPosition.from_binance_futures({
            "symbol": "BTCUSDT", "positionAmt": "1.0", "positionSide": "SHORT",
        })
        self.assertEqual(p.side, "SHORT")

    def test_saldo_de_spot_como_posicion(self):
        p = BrokerPosition.from_spot_balance("BTC", 0.5)
        self.assertEqual(p.symbol, "BTCUSDT")
        self.assertEqual(p.side, "LONG")
        self.assertEqual(p.market, "spot")


# ═══════════════════════════════════════════════════════════════════════
# Dobles de las dos APIs
# ═══════════════════════════════════════════════════════════════════════

_KLINE_BINANCE = [
    # [open_time, o, h, l, c, vol, close_time, ...]
    [1700000000000, "1", "2", "0.5", "1.5", "10", 1700003599999],
    [1700003600000, "1.5", "2.5", "1", "2", "20", 1700007199999],
]


class _FakeBinanceClient:
    """`binance.Client` reducido a lo que toca este fichero, con las claves
    tal cual las devuelve la API real."""

    def __init__(self):
        self.llamadas = []

    def _log(self, nombre, **kw):
        self.llamadas.append((nombre, kw))

    # -- mercado
    def get_klines(self, **kw):
        self._log("get_klines", **kw)
        return list(_KLINE_BINANCE)

    def futures_klines(self, **kw):
        self._log("futures_klines", **kw)
        return list(_KLINE_BINANCE)

    def get_symbol_ticker(self, **kw):
        self._log("get_symbol_ticker", **kw)
        return {"price": "100.0"}

    def futures_symbol_ticker(self, **kw):
        self._log("futures_symbol_ticker", **kw)
        return {"price": "101.0"}

    # -- cuenta
    def get_account(self):
        return {"balances": [
            {"asset": "USDT", "free": "1000.0", "locked": "50.0"},
            {"asset": "BTC", "free": "0.5", "locked": "0"},
            {"asset": "ETH", "free": "0", "locked": "0"},
        ]}

    def futures_account_balance(self):
        return [{"asset": "USDT", "balance": "2000.0", "availableBalance": "1500.0"}]

    def futures_position_information(self, **kw):
        self._log("futures_position_information", **kw)
        return [
            {"symbol": "BTCUSDT", "positionAmt": "-2.0", "entryPrice": "50000",
             "markPrice": "49000", "unRealizedProfit": "2000", "leverage": "10"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0"},
        ]

    # -- órdenes
    def get_open_orders(self, **kw):
        self._log("get_open_orders", **kw)
        return [{"orderId": 1, "symbol": "BTCUSDT", "status": "NEW", "side": "BUY",
                 "type": "LIMIT", "origQty": "1.0", "executedQty": "0", "price": "100"}]

    def futures_get_open_orders(self, **kw):
        self._log("futures_get_open_orders", **kw)
        return [{"orderId": 7, "symbol": "BTCUSDT", "status": "NEW", "side": "SELL",
                 "type": "LIMIT", "origQty": "2.0", "executedQty": "0", "price": "200"}]

    def get_order(self, **kw):
        self._log("get_order", **kw)
        return {"orderId": kw["orderId"], "symbol": kw["symbol"], "status": "CANCELED",
                "side": "BUY", "type": "LIMIT", "origQty": "1.0", "executedQty": "0",
                "price": "100"}

    def futures_get_order(self, **kw):
        self._log("futures_get_order", **kw)
        return {"orderId": kw["orderId"], "symbol": kw["symbol"], "status": "NEW",
                "side": "SELL", "type": "LIMIT", "origQty": "2.0", "executedQty": "0",
                "price": "200"}

    def futures_modify_order(self, **kw):
        self._log("futures_modify_order", **kw)
        return {"orderId": kw["orderId"], "symbol": kw["symbol"], "status": "NEW"}

    def futures_exchange_info(self):
        return {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        ]}]}

    def futures_change_leverage(self, **kw):
        self._log("futures_change_leverage", **kw)
        return {"leverage": kw["leverage"], "symbol": kw["symbol"]}


def _binance_broker():
    from luarix_exchange.brokers.account_data.binance import BinanceAccountDataProvider
    from luarix_exchange.brokers.binance import BinanceBroker
    from luarix_exchange.brokers.market_data.binance import BinanceMarketDataProvider

    broker = BinanceBroker.__new__(BinanceBroker)
    cliente = _FakeBinanceClient()
    broker.client = cliente
    broker.testnet = True
    broker.test_order = False
    broker.trading_mode = "testnet"
    broker.allow_live_trading = False
    broker._filters_cache = {}
    broker.market_data_provider = BinanceMarketDataProvider(cliente)
    broker.account_data_provider = BinanceAccountDataProvider(cliente)
    return broker


class _FakeBitunixSpot:
    def __init__(self):
        self.api_key = "k"
        self.api_secret = "s"

    def get_account_balance(self):
        return {"code": "0", "data": [
            {"coin": "USDT", "balance": "1000.0", "balanceLocked": "0"},
            {"coin": "BTC", "balance": "0.5", "balanceLocked": "0"},
        ]}

    def query_current_orders(self, symbol):
        return {"code": "0", "data": [
            {"id": "77", "symbol": symbol, "status": "NEW", "side": 1,
             "volume": "0.5", "dealVolume": "0", "price": "60000"},
        ]}

    def query_matching_orders(self, order_id, symbol):
        return {"code": "0", "data": None}


class _FakeBitunixFutures:
    def __init__(self):
        self.llamadas = []
        self.KLINE_INTERVALS = frozenset({"1m", "1h", "4h"})

    def account(self, margin_coin="USDT"):
        return {"marginCoin": margin_coin, "available": "1500.0", "margin": "500.0"}

    def pending_positions(self, symbol=None):
        self.llamadas.append(("pending_positions", symbol))
        return [{"symbol": "BTCUSDT", "qty": "2.0", "side": "SELL", "avgOpenPrice": "50000",
                 "markPrice": "49000", "unrealizedPNL": "2000", "leverage": "10",
                 "positionId": "9", "marginMode": "CROSS"}]

    def pending_orders(self, symbol=None):
        self.llamadas.append(("pending_orders", symbol))
        return [{"orderId": "2082878949264547840", "symbol": "LTCUSDT", "status": "NEW",
                 "side": "SELL", "orderType": "LIMIT", "qty": "2.2", "tradeQty": "0",
                 "price": "44.99"}]

    def order_detail(self, order_id):
        self.llamadas.append(("order_detail", str(order_id)))
        return {"orderId": str(order_id), "symbol": "LTCUSDT", "status": "PART_FILLED",
                "side": "SELL", "orderType": "LIMIT", "qty": "2.2", "tradeQty": "1.1",
                "price": "44.99", "tpPrice": "45.37"}

    def order_trades(self, symbol, order_id):
        return []

    def modify_order(self, order_id, **kw):
        self.llamadas.append(("modify_order", str(order_id), kw))
        return {"orderId": str(order_id)}

    def set_leverage(self, symbol, leverage, margin_coin="USDT"):
        self.llamadas.append(("set_leverage", symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def kline(self, symbol, interval, limit=200, end_ms=None):
        self.llamadas.append(("kline", symbol, interval, limit, end_ms))
        # Devuelto de la MÁS RECIENTE a la más antigua, como la API real.
        return [
            {"time": "1700003600000", "open": "1.5", "high": "2.5", "low": "1",
             "close": "2", "baseVol": "20"},
            {"time": "1700000000000", "open": "1", "high": "2", "low": "0.5",
             "close": "1.5", "baseVol": "10"},
        ]


def _bitunix_broker():
    from luarix_exchange.brokers.account_data.bitunix import BitunixAccountDataProvider
    from luarix_exchange.brokers.bitunix import BitunixBroker
    from luarix_exchange.brokers.market_data.bitunix import BitunixMarketDataProvider

    broker = BitunixBroker.__new__(BitunixBroker)
    spot, futures = _FakeBitunixSpot(), _FakeBitunixFutures()
    broker.client = spot
    broker.trading_mode = "testnet"
    broker.allow_live_trading = False
    broker.account_data_provider = BitunixAccountDataProvider(spot, futures=futures)
    md = BitunixMarketDataProvider.__new__(BitunixMarketDataProvider)
    md.client = spot
    md.futures = futures
    broker.market_data_provider = md
    return broker


# ═══════════════════════════════════════════════════════════════════════
# El contrato, con los dos brokers
# ═══════════════════════════════════════════════════════════════════════

class VelasTests(unittest.TestCase):

    def test_ambos_devuelven_candles_ascendentes(self):
        for nombre, broker, market in (("binance", _binance_broker(), "spot"),
                                       ("bitunix", _bitunix_broker(), "futures")):
            with self.subTest(nombre):
                velas = broker.get_candles("BTCUSDT", "1h", limit=10, market=market)
                self.assertEqual(len(velas), 2)
                self.assertLess(velas[0].open_time, velas[1].open_time,
                                "las velas tienen que venir de la más antigua a la más nueva")

    def test_la_ultima_vela_es_la_mas_reciente(self):
        # EL bug: Bitunix devuelve descendente, y el `klines[:limit]` que
        # había se quedaba con las MÁS ANTIGUAS. Con limit=1 daba la vela
        # más vieja disponible haciéndola pasar por la última.
        broker = _bitunix_broker()
        ultima = broker.get_last_candle("BTCUSDT", "1h", market="futures")
        self.assertEqual(ultima.close, 2.0)
        self.assertEqual(ultima.open_time,
                         datetime.fromtimestamp(1700003600, tz=timezone.utc))

    def test_binance_usa_el_endpoint_de_futuros_cuando_se_le_pide(self):
        # Antes `market` no llegaba al kline: un grafo en futuros calculaba
        # sus indicadores sobre precios de contado sin decirlo.
        broker = _binance_broker()
        broker.get_candles("BTCUSDT", "1h", limit=5, market="futures")
        self.assertIn("futures_klines", [n for n, _ in broker.client.llamadas])

    def test_ticker_por_mercado(self):
        broker = _binance_broker()
        self.assertEqual(broker.get_ticker("BTCUSDT", "spot"), 100.0)
        self.assertEqual(broker.get_ticker("BTCUSDT", "futures"), 101.0)


class SaldoTests(unittest.TestCase):

    def test_saldo_disponible_no_incluye_lo_bloqueado(self):
        # `free`, no `free + locked`: lo reservado en órdenes abiertas no es
        # saldo con el que se pueda entrar.
        self.assertEqual(_binance_broker().get_balance("spot")["USDT"], 1000.0)

    def test_filtro_por_activo(self):
        for nombre, broker in (("binance", _binance_broker()), ("bitunix", _bitunix_broker())):
            with self.subTest(nombre):
                self.assertEqual(broker.get_balance("spot", asset="BTC"), {"BTC": 0.5})

    def test_un_activo_ausente_vale_cero_y_no_desaparece(self):
        # "No tienes DOGE" y "no pregunté por DOGE" son respuestas distintas.
        for nombre, broker in (("binance", _binance_broker()), ("bitunix", _bitunix_broker())):
            with self.subTest(nombre):
                self.assertEqual(broker.get_balance("spot", asset="DOGE"), {"DOGE": 0.0})

    def test_spot_y_futuros_son_bolsas_distintas(self):
        for nombre, broker in (("binance", _binance_broker()), ("bitunix", _bitunix_broker())):
            with self.subTest(nombre):
                self.assertNotEqual(broker.get_balance("spot", asset="USDT"),
                                    broker.get_balance("futures", asset="USDT"))


class PosicionesTests(unittest.TestCase):

    def test_ambos_describen_el_mismo_corto_igual(self):
        for nombre, broker in (("binance", _binance_broker()), ("bitunix", _bitunix_broker())):
            with self.subTest(nombre):
                posiciones = broker.get_positions("futures")
                self.assertEqual(len(posiciones), 1)
                p = posiciones[0]
                self.assertEqual((p.symbol, p.side, p.quantity), ("BTCUSDT", "SHORT", 2.0))
                self.assertEqual(p.entry_price, 50000.0)

    def test_bitunix_ya_no_devuelve_siempre_vacio(self):
        # Devolvía `{}` FIJO: afirmaba no tener ninguna posición abierta
        # pasara lo que pasara, con el endpoint declarado y sin usar.
        self.assertTrue(_bitunix_broker().get_positions("futures"))

    def test_binance_futuros_no_devuelve_saldos_de_spot(self):
        # Antes `get_positions()` devolvía los saldos de spot > 0 aunque se
        # preguntara por futuros.
        posiciones = _binance_broker().get_positions("futures")
        self.assertEqual([p.symbol for p in posiciones], ["BTCUSDT"])

    def test_se_descartan_las_cerradas(self):
        # Los dos exchanges devuelven también los pares con qty=0.
        self.assertTrue(all(p.quantity > 0 for p in _binance_broker().get_positions("futures")))

    def test_spot_excluye_la_moneda_de_cotizacion(self):
        # Tener USDT es tener el efectivo, no estar en una posición.
        activos = [p.symbol for p in _binance_broker().get_positions("spot")]
        self.assertEqual(activos, ["BTCUSDT"])


class OrdenesTests(unittest.TestCase):

    def test_las_abiertas_llegan_normalizadas(self):
        for nombre, broker, market in (("binance", _binance_broker(), "futures"),
                                       ("bitunix", _bitunix_broker(), "futures")):
            with self.subTest(nombre):
                abiertas = broker.get_open_orders(market)
                self.assertEqual(len(abiertas), 1)
                o = abiertas[0]
                self.assertIsInstance(o, OrderInfo)
                self.assertEqual(o.side, Side.SELL)
                self.assertTrue(o.is_open)
                self.assertTrue(o.raw, "el cuerpo original tiene que conservarse")

    def test_detalle_de_una_orden_cerrada(self):
        # Lo importante: responde por una orden que ya NO está viva. Un
        # broker que solo mirara los pendientes no podría distinguir una
        # cancelada de una llenada -- el bug de las posiciones fantasma.
        info = _binance_broker().get_order("BTCUSDT", "28", "spot")
        self.assertEqual(info.status, OrderStatus.CANCELED)
        self.assertFalse(info.is_open)

    def test_bitunix_futuros_pregunta_por_el_id(self):
        broker = _bitunix_broker()
        info = broker.get_order("LTCUSDT", "2082878949264547840", "futures")
        self.assertEqual(info.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(info.filled_quantity, 1.1)
        self.assertIn(("order_detail", "2082878949264547840"),
                      broker.account_data_provider.futures.llamadas)

    def test_spot_de_bitunix_cae_a_los_pendientes_si_no_hay_ejecuciones(self):
        # `query_matching_orders` devuelve las EJECUCIONES: vacío tanto para
        # una orden viva como para una cancelada. Se completa con la lista de
        # pendientes para poder distinguirlas.
        info = _bitunix_broker().get_order("BTCUSDT", "77", "spot")
        self.assertEqual(info.status, OrderStatus.NEW)
        self.assertEqual(info.side, Side.BUY)

    def test_spot_de_bitunix_levanta_si_no_conoce_la_orden(self):
        # Antes esto devolvía FILLED: la ausencia se traducía a un fill, que
        # es lo que abrió posiciones fantasma en futuros.
        from luarix_exchange.brokers.bitunix_response import BitunixApiError

        broker = _bitunix_broker()
        with self.assertRaises(BitunixApiError):
            broker.get_order("BTCUSDT", "id-que-no-existe", "spot")

    def test_get_order_status_sale_del_detalle(self):
        self.assertEqual(_binance_broker().get_order_status("BTCUSDT", "28", "spot"),
                         OrderStatus.CANCELED)


class EdicionTests(unittest.TestCase):

    def test_binance_futuros_usa_enmienda_nativa_y_conserva_el_id(self):
        broker = _binance_broker()
        ack = broker.update_order("BTCUSDT", "7", price=210.0, market="futures")
        self.assertEqual(ack.exchange_order_id, "7")
        nombres = [n for n, _ in broker.client.llamadas]
        self.assertIn("futures_modify_order", nombres)

    def test_lo_que_no_se_pasa_se_conserva(self):
        # Binance exige mandar side/quantity/price siempre; si no se
        # rellenaran desde la orden actual, cambiar el precio borraría la
        # cantidad.
        broker = _binance_broker()
        broker.update_order("BTCUSDT", "7", price=210.0, market="futures")
        kw = dict(broker.client.llamadas)["futures_modify_order"]
        self.assertEqual(kw["quantity"], 2.0)
        self.assertEqual(kw["side"], "SELL")

    def test_bitunix_futuros_usa_modify_order(self):
        broker = _bitunix_broker()
        broker.update_order("LTCUSDT", "2082878949264547840", price=45.5, market="futures")
        llamada = [c for c in broker.account_data_provider.futures.llamadas if c[0] == "modify_order"]
        self.assertEqual(len(llamada), 1)
        # La cantidad viene de la orden real, no se pierde.
        self.assertEqual(llamada[0][2]["quantity"], 2.2)
        self.assertEqual(llamada[0][2]["price"], 45.5)

    def test_no_se_modifica_una_orden_ya_cerrada(self):
        with self.assertRaises(ValueError):
            _binance_broker().update_order("BTCUSDT", "28", price=1.0, market="spot")

    def test_binance_rechaza_tp_sl_porque_son_ordenes_propias(self):
        # En Binance el TP/SL no son campos de la orden de entrada como en
        # Bitunix: aceptarlos en silencio no haría nada y el usuario creería
        # que movió su stop.
        with self.assertRaises(ValueError):
            _binance_broker().update_order("BTCUSDT", "7", sl_price=1.0, market="futures")


class ApalancamientoTests(unittest.TestCase):

    def test_ambos_lo_fijan(self):
        binance = _binance_broker()
        binance.set_leverage("BTCUSDT", 5)
        self.assertIn("futures_change_leverage", [n for n, _ in binance.client.llamadas])

        bitunix = _bitunix_broker()
        bitunix.set_leverage("BTCUSDT", 5)
        self.assertIn(("set_leverage", "BTCUSDT", 5),
                      bitunix.account_data_provider.futures.llamadas)


class CandadoDeTradingRealTests(unittest.TestCase):

    def test_bitunix_bloquea_live_sin_autorizacion_explicita(self):
        # Este candado existía SOLO en Binance. Bitunix -- el único de los
        # dos con el que se opera de verdad -- colocaba siempre.
        from luarix_exchange.models.orders import OrderRequest, OrderType

        broker = _bitunix_broker()
        broker.trading_mode = "live"
        broker.allow_live_trading = False
        with self.assertRaises(RuntimeError):
            broker.place_order(OrderRequest(
                symbol="BTCUSDT", side=Side.BUY, quantity=1.0,
                order_type=OrderType.MARKET, market="futures",
            ))


if __name__ == "__main__":
    unittest.main()
