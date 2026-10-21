# -*- coding: utf-8 -*-
"""Saldo completo en USDT, depósitos/retiros e income: lo que necesita el
portafolio de Luarix (ver `docs/plans/historial-de-cuenta.md`).

Reutiliza los dobles de `test_broker_contract.py` y les añade las respuestas
de los endpoints nuevos, con los nombres de campo de la API real."""

import unittest
from datetime import datetime, timedelta, timezone

from luarix_exchange.brokers.binance import BinanceBroker
from luarix_exchange.brokers.bitunix import BitunixBroker
from luarix_exchange.capabilities import Capability
from luarix_exchange.models.account import price_for
from luarix_exchange.models.brokers import Market

from tests.test_broker_contract import _binance_broker, _bitunix_broker

_TICKERS = [
    {"symbol": "BTCUSDT", "price": "60000"},
    {"symbol": "ETHBTC", "price": "0.05"},
    {"symbol": "BNBUSDT", "price": "600"},
]

_FUTURES_ACCOUNT = {
    "totalWalletBalance": "2000.0",
    "assets": [
        {"asset": "USDT", "walletBalance": "2000.0", "unrealizedProfit": "150.0",
         "availableBalance": "1500.0", "marginBalance": "2150.0"},
        {"asset": "BNB", "walletBalance": "0", "unrealizedProfit": "0", "availableBalance": "0"},
    ],
}


def _binance(**overrides):
    broker = _binance_broker()
    cliente = broker.client
    cliente.get_all_tickers = lambda: list(_TICKERS)
    cliente.futures_account = lambda: dict(_FUTURES_ACCOUNT)
    for nombre, fn in overrides.items():
        setattr(cliente, nombre, fn)
    return broker


def _bitunix(**overrides):
    broker = _bitunix_broker()
    spot = broker.account_data_provider.client
    spot.get_rate_data = lambda: {"code": "0", "data": [
        {"baseSymbol": "BTC", "quoteSymbol": "USDT", "rate": "60000"},
        {"baseSymbol": "BTC", "quoteSymbol": "BRL", "rate": "300000"},
    ]}
    for nombre, fn in overrides.items():
        setattr(broker.account_data_provider.futures if nombre == "account" else spot, nombre, fn)
    return broker


def _boom(*_a, **_k):
    raise ConnectionError("timeout")


class ResumenCuentaBinanceTests(unittest.TestCase):

    def test_spot_incluye_lo_bloqueado_y_valora_en_usdt(self):
        spot = _binance().get_account_summary(["spot"]).market("spot")
        por_activo = {a.asset: a for a in spot.assets}
        # USDT 1000 libre + 50 bloqueado; BTC 0.5; ETH a cero no aparece.
        self.assertEqual(set(por_activo), {"USDT", "BTC"})
        self.assertEqual(por_activo["USDT"].total, 1050.0)
        self.assertEqual(por_activo["BTC"].usdt_value, 30_000.0)
        self.assertEqual(spot.equity_usdt, 31_050.0)
        self.assertEqual(spot.available_usdt, 31_000.0)

    def test_futuros_suman_el_no_realizado_a_la_equity(self):
        futuros = _binance().get_account_summary(["futures"]).market("futures")
        self.assertEqual([a.asset for a in futuros.assets], ["USDT"])
        self.assertEqual(futuros.unrealized_pnl, 150.0)
        self.assertEqual(futuros.equity_usdt, 2_150.0)
        self.assertEqual(futuros.available_usdt, 1_500.0)

    def test_sin_mercados_explicitos_lee_spot_y_futuros(self):
        resumen = _binance().get_account_summary()
        self.assertEqual({m.market for m in resumen.markets}, {Market.SPOT, Market.FUTURES})
        self.assertEqual(resumen.equity_usdt, 31_050.0 + 2_150.0)
        self.assertEqual(resumen.errors, {})

    def test_disponible_acotado_al_saldo_con_ganancia_latente(self):
        # Con PnL a favor Binance da `availableBalance` > `walletBalance`.
        cuenta = {"assets": [{"asset": "USDT", "walletBalance": "100", "unrealizedProfit": "40",
                              "availableBalance": "130"}]}
        futuros = _binance(futures_account=lambda: cuenta).get_account_summary(["futures"]).market("futures")
        usdt = futuros.assets[0]
        self.assertEqual((usdt.free, usdt.locked), (100.0, 0.0))
        self.assertEqual(futuros.equity_usdt, 140.0)

    def test_un_mercado_caido_no_tumba_al_otro(self):
        resumen = _binance(futures_account=_boom).get_account_summary()
        self.assertIsNotNone(resumen.market("spot"))
        self.assertIsNone(resumen.market("futures"))
        self.assertIn("futures", resumen.errors)
        self.assertIn("timeout", resumen.errors["futures"])

    def test_si_caen_todos_los_mercados_levanta(self):
        with self.assertRaises(RuntimeError):
            _binance(futures_account=_boom, get_account=_boom).get_account_summary()

    def test_sin_precios_solo_se_valora_usdt_y_se_dice(self):
        resumen = _binance(get_all_tickers=_boom).get_account_summary(["spot"])
        spot = resumen.market("spot")
        self.assertEqual(spot.unpriced, ("BTC",))
        self.assertEqual(spot.equity_usdt, 1_050.0)
        self.assertIn("prices", resumen.errors)


class ResumenCuentaBitunixTests(unittest.TestCase):

    def test_spot_valorado_con_la_lista_de_tasas(self):
        spot = _bitunix().get_account_summary(["spot"]).market("spot")
        self.assertEqual(spot.equity_usdt, 1_000.0 + 0.5 * 60_000)

    def test_futuros_total_es_libre_mas_congelado_mas_margen(self):
        cuenta = {"marginCoin": "USDT", "available": "1500", "frozen": "100", "margin": "400",
                  "crossUnrealizedPNL": "25", "isolationUnrealizedPNL": "-5"}
        futuros = _bitunix(account=lambda coin="USDT": cuenta).get_account_summary(["futures"]).market("futures")
        usdt = futuros.assets[0]
        self.assertEqual((usdt.free, usdt.locked), (1500.0, 500.0))
        self.assertEqual(futuros.unrealized_pnl, 20.0)
        self.assertEqual(futuros.equity_usdt, 2_020.0)


class PrecioTests(unittest.TestCase):

    def test_activos_de_earn_valen_lo_del_subyacente(self):
        self.assertEqual(price_for("LDBTC", {"BTC": 60_000.0}), 60_000.0)
        self.assertEqual(price_for("LDUSDT", {}), 1.0)

    def test_sin_cotizacion_es_none_y_no_cero(self):
        self.assertIsNone(price_for("XYZ", {"BTC": 1.0}))


class MovimientosBinanceTests(unittest.TestCase):

    def test_depositos_por_tramos_de_90_dias(self):
        llamadas = []

        def historial(**kw):
            llamadas.append(kw)
            if len(llamadas) > 1:
                return []
            return [{"id": "d1", "amount": "5000", "coin": "USDT", "network": "TRX", "status": 1,
                     "txId": "abc", "insertTime": 1_760_000_000_000}]

        broker = _binance(get_deposit_history=historial)
        broker.testnet = False
        fin = datetime(2026, 10, 19, tzinfo=timezone.utc)
        movimientos = broker.get_deposits(fin - timedelta(days=200), fin)
        self.assertEqual(len(llamadas), 3)  # 89 + 89 + 22 días
        for kw in llamadas:
            self.assertLessEqual(kw["endTime"] - kw["startTime"], 90 * 86_400_000)
        [deposito] = movimientos
        self.assertEqual((deposito.kind, deposito.amount, deposito.status), ("deposit", 5000.0, "completed"))
        self.assertEqual(deposito.network, "TRX")

    def test_retiros_con_fecha_en_texto_y_comision(self):
        def historial(**_kw):
            return [{"id": "w1", "amount": "250", "transactionFee": "1", "coin": "USDT", "status": 6,
                     "applyTime": "2026-10-12 11:12:02", "network": "BSC", "txId": "0xdef"}]

        broker = _binance(get_withdraw_history=historial)
        broker.testnet = False
        fin = datetime(2026, 10, 19, tzinfo=timezone.utc)
        [retiro] = broker.get_withdrawals(fin - timedelta(days=30), fin)
        self.assertEqual((retiro.kind, retiro.fee, retiro.status), ("withdrawal", 1.0, "completed"))
        self.assertEqual(retiro.timestamp, datetime(2026, 10, 12, 11, 12, 2, tzinfo=timezone.utc))

    def test_testnet_levanta_en_vez_de_devolver_vacio(self):
        broker = _binance()  # el doble es testnet
        with self.assertRaises(NotImplementedError):
            broker.get_deposits(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc))


class IncomeBinanceTests(unittest.TestCase):

    def test_pagina_cuando_llegan_1000_apuntes(self):
        llamadas = []
        base = 1_760_000_000_000

        def historial(**kw):
            llamadas.append(kw)
            if len(llamadas) == 1:
                return [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1",
                         "asset": "USDT", "time": base + i, "tranId": i} for i in range(1000)]
            return [{"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.1",
                     "asset": "USDT", "time": base + 2000, "tranId": 99999}]

        broker = _binance(futures_income_history=historial)
        fin = datetime.fromtimestamp((base + 5000) / 1000, tz=timezone.utc)
        apuntes = broker.get_income(fin - timedelta(days=3), fin)
        self.assertEqual(len(apuntes), 1001)
        self.assertEqual(llamadas[1]["startTime"], base + 999 + 1)
        self.assertEqual({a.kind for a in apuntes}, {"realized_pnl", "commission"})


class CapacidadesTests(unittest.TestCase):

    def test_quien_declara_que(self):
        self.assertTrue(BinanceBroker.supports(Capability.ACCOUNT_SUMMARY))
        self.assertTrue(BinanceBroker.supports(Capability.FUNDING_HISTORY))
        self.assertTrue(BinanceBroker.supports(Capability.INCOME_HISTORY))
        self.assertTrue(BitunixBroker.supports(Capability.ACCOUNT_SUMMARY))
        self.assertFalse(BitunixBroker.supports(Capability.FUNDING_HISTORY))
        self.assertFalse(BitunixBroker.supports(Capability.INCOME_HISTORY))


if __name__ == "__main__":
    unittest.main()
