from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BrokerType(Enum):
    BINANCE = "binance"
    BITUNIX = "bitunix"
    MT5 = "mt5"
    PAPER = "paper"


class Market(str, Enum):
    """Qué cuenta del broker recibe la llamada.

    Es un `str` Enum a propósito: todo el paquete pasa el mercado como
    cadena (`market="futures"`) desde antes de que este enum existiera, y
    heredar de `str` deja que `Market.FUTURES == "futures"` siga siendo
    cierto. Así los llamadores nuevos pueden usar el enum sin romper a los
    viejos.
    """
    SPOT = "spot"
    FUTURES = "futures"


@dataclass(frozen=True)
class BrokerPosition:
    """Una posición abierta, con la MISMA forma venga de donde venga.

    Existe porque los dos brokers describen lo mismo con nombres distintos
    y ninguno se parece al otro:

    · Binance futuros -> `{"symbol", "positionAmt", "entryPrice",
      "markPrice", "unRealizedProfit", "leverage", "positionSide"}`
    · Bitunix futuros -> `{"symbol", "qty", "avgOpenPrice", "markPrice",
      "unrealizedPNL", "leverage", "side", "positionId", "marginMode"}`

    `quantity` es SIEMPRE positiva y la dirección vive en `side`. Binance
    firma la cantidad (`positionAmt` negativo = corto) y Bitunix no, así
    que sin normalizar esto un `qty > 0` significaría cosas distintas según
    el exchange -- el tipo de detalle que se descubre operando en real.

    En SPOT no existen posiciones: lo que hay son SALDOS de activos. Se
    representan igual (`side="LONG"`, `entry_price=0.0`) para que un
    cuaderno o un grafo pueda listar "lo que tengo abierto" sin ramificar
    por mercado, pero `market="spot"` lo deja claro.
    """
    symbol: str
    side: str                       # "LONG" | "SHORT"
    quantity: float                 # siempre > 0; la dirección va en `side`
    entry_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float | None = None
    margin_mode: str | None = None
    position_id: str | None = None
    market: str = "spot"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        """Valor de la posición al precio de marca (o al de entrada si el
        broker no dio marca)."""
        return self.quantity * (self.mark_price or self.entry_price)

    @classmethod
    def from_binance_futures(cls, raw: dict) -> BrokerPosition:
        # `positionAmt` viene FIRMADO: negativo = corto. Es la única fuente
        # de la dirección en modo ONE_WAY -- `positionSide` dice "BOTH" ahí.
        amount = float(raw.get("positionAmt") or 0)
        side = raw.get("positionSide") or ""
        if side in ("", "BOTH"):
            side = "LONG" if amount >= 0 else "SHORT"
        return cls(
            symbol=raw.get("symbol", ""),
            side=side,
            quantity=abs(amount),
            entry_price=float(raw.get("entryPrice") or 0),
            mark_price=float(raw.get("markPrice") or 0),
            unrealized_pnl=float(raw.get("unRealizedProfit") or 0),
            leverage=float(raw["leverage"]) if raw.get("leverage") else None,
            margin_mode="isolated" if raw.get("isolated") else "cross",
            market="futures",
            raw=raw,
        )

    @classmethod
    def from_bitunix_futures(cls, raw: dict) -> BrokerPosition:
        # Bitunix manda la cantidad SIN signo y la dirección aparte, en
        # `side` ("BUY"/"SELL") o `positionSide`. Se traduce a LONG/SHORT
        # para que signifique lo mismo que en Binance.
        crudo = str(raw.get("side") or raw.get("positionSide") or "").upper()
        side = {"BUY": "LONG", "LONG": "LONG", "SELL": "SHORT", "SHORT": "SHORT"}.get(crudo, crudo)
        return cls(
            symbol=raw.get("symbol", ""),
            side=side,
            quantity=abs(float(raw.get("qty") or 0)),
            entry_price=float(raw.get("avgOpenPrice") or raw.get("entryValue") or 0),
            mark_price=float(raw.get("markPrice") or 0),
            unrealized_pnl=float(raw.get("unrealizedPNL") or 0),
            leverage=float(raw["leverage"]) if raw.get("leverage") else None,
            margin_mode=(str(raw["marginMode"]).lower() if raw.get("marginMode") else None),
            position_id=str(raw["positionId"]) if raw.get("positionId") else None,
            market="futures",
            raw=raw,
        )

    @classmethod
    def from_spot_balance(cls, asset: str, quantity: float, quote: str = "USDT") -> BrokerPosition:
        """Un saldo de spot visto como posición. `symbol` se construye
        pegándole la moneda de cotización (`BTC` -> `BTCUSDT`) para que el
        campo signifique lo mismo que en futuros y se pueda pedir su precio
        sin más traducción."""
        return cls(
            symbol=f"{asset}{quote}" if asset != quote else asset,
            side="LONG",
            quantity=quantity,
            market="spot",
            raw={"asset": asset, "free": quantity},
        )
