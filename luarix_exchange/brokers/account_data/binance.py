from binance import Client

from luarix_exchange.brokers.account_data.base import AccountDataProvider


class BinanceAccountDataProvider(AccountDataProvider):
    """Cuenta de Binance: spot y USDT-M Futures con la misma API key.

    Los dos mercados viven en hosts distintos pero el mismo `Client` de
    `python-binance` los cubre: los métodos `futures_*` apuntan a
    `fapi.binance.com` y el resto a `api.binance.com`.
    """

    def __init__(self, client: Client):
        self.client = client

    # ── Balance ─────────────────────────────────────────────────────────

    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        if market == "futures":
            # `availableBalance` y no `balance`: el contrato es el saldo
            # DISPONIBLE, y en futuros el margen inmovilizado por una
            # posición abierta sigue contando dentro de `balance`.
            saldos = {
                item["asset"]: float(item.get("availableBalance") or 0)
                for item in self.client.futures_account_balance()
            }
        else:
            # `free` a secas, no `free + locked`: el contrato de
            # `AccountDataProvider.get_balance` es el saldo DISPONIBLE, para
            # que signifique lo mismo en todos los brokers y mercados. Lo
            # reservado en órdenes abiertas no es saldo con el que entrar.
            account = self.client.get_account()
            saldos = {item["asset"]: float(item["free"]) for item in account.get("balances", [])}
        return self._filter_asset(saldos, asset)

    # ── Posiciones ──────────────────────────────────────────────────────

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[dict]:
        if market == "futures":
            # `symbol=None` no se pasa como kwarg: python-binance lo mandaría
            # literal en la query y Binance devolvería un -1121.
            crudas = self.client.futures_position_information(**({"symbol": symbol} if symbol else {}))
            # Binance devuelve TODOS los pares con los que la cuenta ha
            # operado alguna vez, la mayoría con `positionAmt = 0`. Filtrar
            # acá evita que cada llamador repita el mismo `if`.
            return [p for p in crudas if float(p.get("positionAmt") or 0) != 0]

        # En spot no hay posiciones, hay SALDOS. Se devuelven los que tienen
        # cantidad, con el nombre de campo del propio endpoint (`asset`,
        # `free`), y es `BrokerPosition.from_spot_balance` quien decide cómo
        # representarlos. Se excluyen las stablecoins de cotización: tener
        # USDT no es "estar en una posición", es tener el efectivo.
        account = self.client.get_account()
        return [
            item for item in account.get("balances", [])
            if float(item.get("free") or 0) > 0 and item.get("asset") not in _QUOTE_ASSETS
        ]

    # ── Órdenes ─────────────────────────────────────────────────────────

    def get_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[dict]:
        # Binance sí acepta "todas las de la cuenta" en los DOS mercados
        # (`symbol` opcional), a diferencia del spot de Bitunix. `symbol=None`
        # no se pasa como kwarg porque python-binance lo mandaría literal.
        kwargs = {"symbol": symbol} if symbol else {}
        if market == "futures":
            return list(self.client.futures_get_open_orders(**kwargs))
        return list(self.client.get_open_orders(**kwargs))

    def get_order(self, symbol: str, order_id: str, market: str = "spot") -> dict:
        """`GET /api/v3/order` (o su gemelo de futuros): responde por la
        orden concreta esté viva, llenada o cancelada.

        Binance EXIGE el símbolo además del id -- sus ids solo son únicos
        dentro de un par."""
        if market == "futures":
            return dict(self.client.futures_get_order(symbol=symbol, orderId=order_id))
        return dict(self.client.get_order(symbol=symbol, orderId=order_id))

    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        # `cancel_all_open_orders`/`futures_cancel_all_open_orders` exigen
        # símbolo, así que se agrupa por par a partir del listado. Además el
        # listado previo es lo que permite devolver los ids exactos.
        orders = self.get_open_orders(market, symbol)
        by_symbol: dict[str, list[str]] = {}
        for order in orders:
            by_symbol.setdefault(order["symbol"], []).append(str(order["orderId"]))
        for sym in by_symbol:
            if market == "futures":
                self.client.futures_cancel_all_open_orders(symbol=sym)
            else:
                self.client.cancel_all_open_orders(symbol=sym)
        return [order_id for ids in by_symbol.values() for order_id in ids]

    # ── Futuros ─────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        if market != "futures":
            raise NotImplementedError("El apalancamiento solo existe en la cuenta de futuros.")
        return dict(self.client.futures_change_leverage(symbol=symbol, leverage=int(leverage)))


# Monedas de cotización: tenerlas no es una posición abierta, es el efectivo
# con el que se abre. Se excluyen de `get_positions` en spot para que la
# lista signifique lo mismo que en futuros.
_QUOTE_ASSETS = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI"})
