"""Capacidades de cuenta y de operación: saldo, posiciones, órdenes, OCO,
apalancamiento y comisiones."""

from __future__ import annotations

from abc import abstractmethod

from datetime import datetime

from luarix_exchange.capabilities.base import Capability, CapabilityInterface
from luarix_exchange.models.account import AccountSummary, FundingMovement, IncomeEntry
from luarix_exchange.models.brokers import BrokerPosition
from luarix_exchange.models.fees import CommissionRates
from luarix_exchange.models.orders import Entry, Fill, OrderAck, OrderInfo, OrderRequest, OrderStatus, Side


class Account(CapabilityInterface):
    """Lectura de la cuenta (firmado): saldo, posiciones y órdenes."""

    capability = Capability.ACCOUNT

    @abstractmethod
    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        """Saldo DISPONIBLE por activo. Con `asset`, `{asset: 0.0}` si no lo hay (nunca `{}`)."""

    @abstractmethod
    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[BrokerPosition]:
        """Posiciones abiertas, solo las de cantidad distinta de cero."""

    @abstractmethod
    def get_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[OrderInfo]:
        """Órdenes pendientes, normalizadas."""

    @abstractmethod
    def get_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderInfo:
        """El detalle de una orden, viva o CERRADA. Quien solo sabe mirar la
        lista de pendientes no puede implementarlo: ausente de ahí puede ser
        "llenó" o "la cancelaron a mano"."""

    def get_order_status(self, symbol: str, order_id: str, market: str = "spot") -> OrderStatus:
        return self.get_order(symbol, order_id, market).status


class AccountSummarySource(CapabilityInterface):
    """El saldo COMPLETO de la cuenta, valorado en USDT: spot + futuros, libre
    + bloqueado, cada activo con su equivalencia y los totales.

    No sustituye a `Account.get_balance`, que sigue siendo el saldo
    DISPONIBLE en la unidad de cada activo (la pregunta de un grafo antes de
    entrar). Este responde otra: "¿cuánto vale mi cuenta?"."""

    capability = Capability.ACCOUNT_SUMMARY

    @abstractmethod
    def get_account_summary(self, markets: list[str] | None = None) -> AccountSummary:
        """`markets=None` = todos los de `Exchange.markets`. Un mercado que
        falla queda en `AccountSummary.errors`; si fallan todos, levanta."""


class FundingHistory(CapabilityInterface):
    """Depósitos y retiros de la cuenta."""

    capability = Capability.FUNDING_HISTORY

    @abstractmethod
    def get_deposits(self, start: datetime, end: datetime) -> list[FundingMovement]:
        """Depósitos en `[start, end]`, más antiguos primero. Las ventanas
        máximas por petición del exchange se resuelven dentro."""

    @abstractmethod
    def get_withdrawals(self, start: datetime, end: datetime) -> list[FundingMovement]:
        """Retiros en `[start, end]`, más antiguos primero."""


class IncomeHistory(CapabilityInterface):
    """El libro de la cuenta de futuros: PnL realizado, funding, comisiones y
    transferencias, apunte a apunte. Incluye lo que el usuario operó a mano,
    no solo lo de Luarix."""

    capability = Capability.INCOME_HISTORY

    @abstractmethod
    def get_income(self, start: datetime, end: datetime) -> list[IncomeEntry]:
        """Apuntes en `[start, end]`, más antiguos primero."""


class Trading(CapabilityInterface):
    """Colocar, modificar y cancelar órdenes."""

    capability = Capability.TRADING

    @abstractmethod
    def place_order(self, order: OrderRequest) -> tuple[OrderAck, list[Fill]]:
        """Coloca la orden y devuelve el ack del exchange y los llenados inmediatos."""

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str, market: str = "spot") -> OrderAck:
        """Cancela una orden pendiente."""

    @abstractmethod
    def update_order(
        self,
        symbol: str,
        order_id: str,
        *,
        price: float | None = None,
        quantity: float | None = None,
        stop_price: float | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
        market: str = "spot",
    ) -> OrderAck:
        """Modifica una orden y devuelve la RESULTANTE. ⚠ El id puede cambiar
        (sin enmienda nativa se cancela y se recoloca)."""

    @abstractmethod
    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        """Cancela las pendientes y devuelve los ids cancelados."""


class Derivatives(CapabilityInterface):
    """Operaciones de la cuenta de derivados."""

    capability = Capability.DERIVATIVES

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        """Fija el apalancamiento del par. Afecta a TODO lo que se coloque
        después en ese par, también desde otra sesión."""


class NativeOco(CapabilityInterface):
    """OCO nativo: una sola llamada, y el propio exchange cancela la pierna
    que no se ejecuta. Sin esta capacidad, el motor coloca TP y SL como dos
    órdenes y cancela él mismo la hermana."""

    capability = Capability.NATIVE_OCO

    @abstractmethod
    def place_oco_order(
        self, symbol: str, side: Side, quantity: float,
        take_profit_price: float, stop_price: float, stop_limit_price: float,
    ) -> tuple[str, OrderAck, OrderAck]:
        """Coloca el OCO; devuelve (id de la lista, ack TP, ack SL)."""

    @abstractmethod
    def cancel_oco(self, symbol: str, oco_list_id: int) -> OrderAck:
        """Cancela el OCO entero."""

    @abstractmethod
    def update_oco(self, entry: Entry, new_tp: float, new_sl: float, new_sl_limit: float) -> tuple[str, OrderAck, OrderAck]:
        """Sustituye el OCO de una entrada por otro con niveles nuevos."""


class Fees(CapabilityInterface):
    """Comisión real de la cuenta (claves de solo lectura bastan)."""

    capability = Capability.FEES

    @abstractmethod
    def get_commissions(self, symbols: list[str], market: str = "spot") -> dict[str, CommissionRates]:
        """Comisión maker/taker por par."""


class OrderRoundtripProbe(CapabilityInterface):
    """Mide cuánto tarda una orden real en ir y volver, en el entorno de
    pruebas del exchange (testnet / demo): coloca compras límite lejos del
    precio y las cancela. Lo usa arbitraje para que su paper trading espere
    entre operaciones un tiempo real."""

    capability = Capability.ORDER_ROUNDTRIP

    @abstractmethod
    def measure_order_roundtrip_ms(self, credentials, samples: int = 8) -> list[int]:
        """Milisegundos de ida y vuelta de `samples` órdenes de prueba.
        `credentials`: claves del ENTORNO DE PRUEBAS (`api_key`, `api_secret`,
        `passphrase` si la hay)."""
