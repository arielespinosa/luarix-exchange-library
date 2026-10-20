"""El dinero de una cuenta: saldo valorado en USDT, depósitos/retiros e income.

Existe para el portafolio de Luarix, que necesita responder "¿cuánto tengo en
cada broker?" con UN número comparable entre brokers y mercados. Para eso no
basta `get_balance` (solo el disponible, solo un mercado, cada activo en su
propia unidad): hace falta sumar spot + futuros, con lo bloqueado incluido y
cada activo convertido a USDT.

Ver `docs/plans/historial-de-cuenta.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from luarix_exchange.models.brokers import Market

QUOTE_ASSET = "USDT"


@dataclass(frozen=True)
class AssetBalance:
    """Un activo en un mercado de la cuenta.

    `free` + `locked` = `total`. En futuros, `locked` es el margen
    inmovilizado en posiciones y órdenes; el no realizado NO está aquí, va en
    `MarketBalance.unrealized_pnl` (no es saldo del activo hasta que se cierra).

    `usdt_price` es `None` cuando el exchange no cotiza ese activo contra
    USDT: el activo aparece igual (sería mentir ocultarlo), pero sin valor, y
    queda en `MarketBalance.unpriced` para que el consumidor lo avise.
    """

    asset: str
    market: Market
    free: float
    locked: float
    usdt_price: float | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def total(self) -> float:
        return self.free + self.locked

    @property
    def usdt_value(self) -> float | None:
        return None if self.usdt_price is None else self.total * self.usdt_price

    @property
    def free_usdt(self) -> float | None:
        return None if self.usdt_price is None else self.free * self.usdt_price


@dataclass(frozen=True)
class MarketBalance:
    """El saldo de UNO de los mercados (spot o futuros) de la cuenta."""

    market: Market
    assets: tuple[AssetBalance, ...]
    # En USDT. Siempre 0.0 en spot (no hay posiciones apalancadas que valorar:
    # lo que se tiene ya está en `assets` a precio de mercado).
    unrealized_pnl: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def equity_usdt(self) -> float:
        """Lo que vale este mercado si se cerrara todo ahora: saldos valorados
        + no realizado. Los activos sin precio no suman (ver `unpriced`)."""
        return sum(a.usdt_value for a in self.assets if a.usdt_value is not None) + self.unrealized_pnl

    @property
    def available_usdt(self) -> float:
        return sum(a.free_usdt for a in self.assets if a.free_usdt is not None)

    @property
    def unpriced(self) -> tuple[str, ...]:
        return tuple(a.asset for a in self.assets if a.usdt_price is None)


@dataclass(frozen=True)
class AccountSummary:
    """Spot + futuros de una cuenta, en USDT.

    `errors` es la excepción documentada a la regla "levantar, nunca devolver
    vacío": si UN mercado falla (timeout en futuros, p. ej.) el otro sigue
    siendo información real y útil, así que se devuelve con el fallo
    registrado por mercado. Si fallan TODOS, `build_account_summary` levanta.
    Quien consume debe tratar `errors` no vacío como lectura parcial.
    """

    markets: tuple[MarketBalance, ...]
    errors: Mapping[str, str] = field(default_factory=dict)
    quote_asset: str = QUOTE_ASSET
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def equity_usdt(self) -> float:
        return sum(m.equity_usdt for m in self.markets)

    @property
    def available_usdt(self) -> float:
        return sum(m.available_usdt for m in self.markets)

    @property
    def unrealized_pnl(self) -> float:
        return sum(m.unrealized_pnl for m in self.markets)

    @property
    def assets(self) -> tuple[AssetBalance, ...]:
        return tuple(a for m in self.markets for a in m.assets)

    def market(self, market: Market | str) -> MarketBalance | None:
        wanted = Market(market)
        return next((m for m in self.markets if m.market is wanted), None)


# Monedas que valen 1 USDT por definición en este contrato. Solo USDT: el
# resto de stables (USDC, FDUSD...) se valoran con su propio precio contra
# USDT cuando el exchange lo da, porque no siempre están a la par.
_PAR = {QUOTE_ASSET: 1.0}


def price_for(asset: str, prices: Mapping[str, float]) -> float | None:
    """Precio en USDT de un activo a partir del mapa del exchange.

    Reconoce los activos de Binance Simple Earn (`LDBTC` = BTC bloqueado en
    Earn flexible): valen lo mismo que el subyacente."""
    if asset in _PAR:
        return _PAR[asset]
    if asset in prices:
        return prices[asset]
    if asset.startswith("LD") and asset[2:] in prices:
        return prices[asset[2:]]
    if asset.startswith("LD") and asset[2:] in _PAR:
        return _PAR[asset[2:]]
    return None


@dataclass(frozen=True)
class BalanceRow:
    """Lo que cada proveedor de cuenta devuelve por activo, ya con nombres
    comunes. Paso intermedio entre el JSON del exchange y `AssetBalance`:
    el proveedor no sabe de precios, `build_account_summary` sí."""

    asset: str
    free: float
    locked: float
    # Solo futuros, en la unidad del propio activo (Binance lo da por activo
    # de margen). Se valora con el mismo precio que el saldo.
    unrealized_pnl: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


def build_account_summary(
    markets: list[Market],
    fetch_rows,
    fetch_prices,
) -> AccountSummary:
    """Arma el `AccountSummary` común a todos los brokers.

    `fetch_rows(market) -> list[BalanceRow]` y `fetch_prices() -> {activo:
    precio en USDT}` son las dos llamadas propias de cada exchange. Los
    precios se piden UNA vez para toda la cuenta.

    Si los precios fallan, se sigue sin ellos (todo menos USDT queda sin
    valorar y el error se registra): un saldo en BTC sin convertir es mejor
    respuesta que ninguna. Si fallan todos los mercados, levanta.
    """
    errors: dict[str, str] = {}
    try:
        prices = dict(fetch_prices())
    except Exception as exc:  # noqa: BLE001 -- se registra, ver docstring
        prices = {}
        errors["prices"] = f"{type(exc).__name__}: {exc}"

    balances: list[MarketBalance] = []
    for market in markets:
        try:
            rows = fetch_rows(market)
        except Exception as exc:  # noqa: BLE001 -- lectura parcial, ver AccountSummary
            errors[market.value] = f"{type(exc).__name__}: {exc}"
            continue
        assets = []
        unrealized = 0.0
        for row in rows:
            price = price_for(row.asset, prices)
            assets.append(AssetBalance(row.asset, market, row.free, row.locked, price, row.raw))
            if row.unrealized_pnl and price is not None:
                unrealized += row.unrealized_pnl * price
        balances.append(MarketBalance(market, tuple(assets), unrealized))

    if not balances:
        detalle = "; ".join(f"{k}: {v}" for k, v in errors.items())
        raise RuntimeError(f"No se pudo leer ningún mercado de la cuenta ({detalle}).")
    return AccountSummary(tuple(balances), errors)


@dataclass(frozen=True)
class FundingMovement:
    """Un depósito o un retiro. `amount` siempre > 0; el sentido va en `kind`.

    `status`: `completed`, `pending` o `failed` -- los estados de cada
    exchange se traducen a estos tres, el original queda en `raw`.
    """

    id: str
    kind: str  # "deposit" | "withdrawal"
    asset: str
    amount: float
    fee: float
    network: str | None
    tx_id: str | None
    status: str
    timestamp: datetime
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # Binance, `GET /sapi/v1/capital/deposit/hisrec`: 0 pendiente,
    # 6 acreditado sin poder retirar todavía, 7 depósito erróneo,
    # 8 esperando confirmación del usuario, 1 completado, 2 rechazado.
    _BINANCE_DEPOSIT = {0: "pending", 6: "completed", 7: "failed", 8: "pending", 1: "completed", 2: "failed"}
    # Binance, `GET /sapi/v1/capital/withdraw/history`: 0 email enviado,
    # 1 cancelado, 2 esperando aprobación, 3 rechazado, 4 procesando,
    # 5 fallido, 6 completado.
    _BINANCE_WITHDRAW = {0: "pending", 1: "failed", 2: "pending", 3: "failed", 4: "pending", 5: "failed", 6: "completed"}

    @classmethod
    def from_binance_deposit(cls, raw: Mapping[str, Any]) -> FundingMovement:
        return cls(
            id=str(raw.get("id") or raw.get("txId")),
            kind="deposit",
            asset=str(raw["coin"]),
            amount=float(raw["amount"]),
            fee=0.0,
            network=raw.get("network") or None,
            tx_id=raw.get("txId") or None,
            status=cls._BINANCE_DEPOSIT.get(int(raw.get("status", 0)), "pending"),
            timestamp=datetime.fromtimestamp(int(raw["insertTime"]) / 1000, tz=timezone.utc),
            raw=dict(raw),
        )

    @classmethod
    def from_binance_withdrawal(cls, raw: Mapping[str, Any]) -> FundingMovement:
        # `applyTime` viene como texto UTC ("2026-10-12 11:12:02"), no en ms.
        applied = datetime.strptime(str(raw["applyTime"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return cls(
            id=str(raw["id"]),
            kind="withdrawal",
            asset=str(raw["coin"]),
            amount=float(raw["amount"]),
            fee=float(raw.get("transactionFee") or 0),
            network=raw.get("network") or None,
            tx_id=raw.get("txId") or None,
            status=cls._BINANCE_WITHDRAW.get(int(raw.get("status", 0)), "pending"),
            timestamp=applied,
            raw=dict(raw),
        )


@dataclass(frozen=True)
class IncomeEntry:
    """Un apunte del libro de la cuenta de futuros, con signo.

    `kind`: `realized_pnl`, `funding_fee`, `commission` (negativo = pagada),
    `transfer` (entrada/salida desde spot) u `other`."""

    id: str
    kind: str
    asset: str
    amount: float
    symbol: str | None
    timestamp: datetime
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    _BINANCE_KIND = {
        "REALIZED_PNL": "realized_pnl",
        "FUNDING_FEE": "funding_fee",
        "COMMISSION": "commission",
        "TRANSFER": "transfer",
    }

    @classmethod
    def from_binance(cls, raw: Mapping[str, Any]) -> IncomeEntry:
        return cls(
            id=f"{raw.get('tranId')}:{raw.get('incomeType')}:{raw.get('tradeId') or ''}",
            kind=cls._BINANCE_KIND.get(str(raw.get("incomeType")), "other"),
            asset=str(raw.get("asset") or QUOTE_ASSET),
            amount=float(raw.get("income") or 0),
            symbol=raw.get("symbol") or None,
            timestamp=datetime.fromtimestamp(int(raw["time"]) / 1000, tz=timezone.utc),
            raw=dict(raw),
        )
