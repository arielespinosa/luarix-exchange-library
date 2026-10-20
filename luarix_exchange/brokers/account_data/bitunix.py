from bitunix import BitunixClient

from luarix_exchange.brokers.account_data.base import AccountDataProvider
from luarix_exchange.brokers.bitunix_futures import DEFAULT_MARGIN_COIN, BitunixFuturesClient
from luarix_exchange.brokers.bitunix_response import BitunixApiError, unwrap
from luarix_exchange.models.account import QUOTE_ASSET, BalanceRow


class BitunixAccountDataProvider(AccountDataProvider):
    """Cuenta de Bitunix: spot por el paquete `bitunix` de PyPI, futuros por
    `BitunixFuturesClient`.

    Son DOS APIs distintas en dos hosts distintos, y el paquete de PyPI solo
    cubre la primera. Por eso casi todo lo de acá empieza con un
    `if market == "futures"`: no es una elección de estilo, es que no hay un
    endpoint común al que llamar.
    """

    def __init__(self, client: BitunixClient, futures: BitunixFuturesClient | None = None):
        self.client = client
        # El paquete `bitunix` es solo spot; los futuros van por su propio
        # cliente (ver luarix_exchange/brokers/bitunix_futures.py).
        self.futures = futures or BitunixFuturesClient(client.api_key, client.api_secret)

    # ── Balance ─────────────────────────────────────────────────────────

    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        if market == "futures":
            # En futuros el saldo se consulta POR moneda de margen, de una en
            # una -- así que el filtro por activo no es un filtro: es la
            # propia consulta. Pedir "USDT" hace una llamada distinta, no
            # recorta el resultado de otra.
            return self._futures_balance(asset or DEFAULT_MARGIN_COIN)
        return self._filter_asset(self._spot_balance(), asset)

    def _spot_balance(self) -> dict[str, float]:
        # `/api/spot/v1/user/account` devuelve
        # `[{"coin": "USDT", "balance": "0.0", "balanceLocked": "0"}, ...]`.
        # Antes esto leía `item["asset"]` / `item["available"]`, claves que
        # esa respuesta NO tiene: el `if "asset" in item` descartaba
        # silenciosamente los 24 activos y `get_balance()` devolvía `{}`
        # SIEMPRE, incluso con la llamada respondiendo `code: 0 Success`.
        data = unwrap(self.client.get_account_balance(), "la consulta de balance spot", on_null=[])
        return {item["coin"]: float(item.get("balance") or 0) for item in data if "coin" in item}

    def _futures_balance(self, margin_coin: str = DEFAULT_MARGIN_COIN) -> dict[str, float]:
        """Saldo DISPONIBLE de la cuenta de futuros.

        La respuesta trae además `frozen`, `margin` (bloqueado en posiciones
        abiertas), `crossUnrealizedPNL` y `bonus`. Se devuelve `available`
        porque es lo que responde a la pregunta que hace un grafo antes de
        entrar -- "¿tengo con qué abrir?" -- y porque es lo único
        equiparable al saldo disponible de spot y de Binance."""
        data = self.futures.account(margin_coin)
        if not isinstance(data, dict):
            raise BitunixApiError(f"Bitunix devolvió un balance de futuros con forma inesperada: {data!r}")
        return {data.get("marginCoin", margin_coin): float(data.get("available") or 0)}

    # ── Posiciones ──────────────────────────────────────────────────────

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[dict]:
        if market == "futures":
            return self.futures.pending_positions(symbol)

        # Spot no tiene posiciones, tiene saldos. Se devuelven con la forma
        # del propio endpoint para que la capa de arriba decida cómo
        # representarlos. Fuera las monedas de cotización: tener USDT es
        # tener el efectivo, no estar en una posición.
        posiciones = [
            {"asset": coin, "free": cantidad}
            for coin, cantidad in self._spot_balance().items()
            if cantidad > 0 and coin not in _QUOTE_ASSETS
        ]
        if symbol:
            # En spot el "símbolo" es el par (BTCUSDT) y el saldo es del
            # activo base (BTC), así que se compara por prefijo.
            posiciones = [p for p in posiciones if str(symbol).upper().startswith(p["asset"].upper())]
        return posiciones

    # ── Órdenes pendientes ──────────────────────────────────────────────

    def get_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[dict]:
        if market == "futures":
            # Verificado contra la cuenta real: sin `symbol` devuelve todos
            # los pares; con `symbol` filtra el exchange por nosotros.
            return self.futures.pending_orders(symbol)
        return self._spot_open_orders(symbol)

    def _spot_open_orders(self, symbol: str | None) -> list[dict]:
        """OJO: `/api/spot/v1/order/pending/list` EXIGE un símbolo -- no
        existe un "dame todas las pendientes de la cuenta" en el spot de
        Bitunix, a diferencia de futuros. Sin `symbol` no se puede responder
        y se dice en voz alta en vez de devolver una lista vacía que
        parecería "no tienes órdenes"."""
        if not symbol:
            raise BitunixApiError(
                "El spot de Bitunix no permite listar las órdenes pendientes de toda la cuenta: "
                "su endpoint exige un símbolo."
            )
        data = unwrap(self.client.query_current_orders(symbol),
                      "la consulta de órdenes pendientes spot", on_null=[])
        return list(data or [])

    # ── Detalle de UNA orden ────────────────────────────────────────────

    def get_order(self, symbol: str, order_id: str, market: str = "spot") -> dict:
        """El detalle de una orden por su id, esté viva o cerrada.

        En FUTUROS va por `get_order_detail`, que responde por la orden
        concreta. Un `{}` significa "el exchange no la conoce", que NO es lo
        mismo que "se cerró" -- quien llama decide qué hacer con esa
        ignorancia (ver `BitunixBroker.get_order_status`).

        ⚠ En SPOT no hay un endpoint de detalle equivalente verificado.
        `query_matching_orders` devuelve las EJECUCIONES de una orden, así
        que responde bien por una llenada y devuelve vacío tanto por una
        pendiente como por una cancelada. Se completa con la lista de
        pendientes para poder distinguir esos dos casos: si aparece ahí,
        sigue viva; si no aparece en ninguno de los dos sitios, no se
        inventa un estado -- se levanta. Esto cierra el "pendiente de spot"
        anotado en `docs/plans/bugs.md`, donde la ausencia se traducía a
        FILLED y podía dar por llenada una orden cancelada a mano.
        """
        if market == "futures":
            return self.futures.order_detail(order_id)

        crudo = unwrap(self.client.query_matching_orders(order_id=order_id, symbol=symbol),
                       "la consulta del detalle de la orden spot", on_null={})
        if crudo:
            # `query_matching_orders` puede devolver la lista de trades de la
            # orden en vez de la orden. Si trae ejecuciones, la orden llenó.
            if isinstance(crudo, list):
                return {"orderId": str(order_id), "symbol": symbol, "status": "FILLED",
                        "dealVolume": sum(float(t.get("volume") or 0) for t in crudo),
                        "trades": crudo}
            return dict(crudo)

        for pendiente in self._spot_open_orders(symbol):
            if str(pendiente.get("id") or pendiente.get("orderId") or "") == str(order_id):
                return dict(pendiente)

        raise BitunixApiError(
            f"El spot de Bitunix no reconoce la orden {order_id} de {symbol}: no tiene ejecuciones "
            "ni está entre las pendientes. Puede haberse cancelado, o el id/símbolo no ser correctos. "
            "No se asume un estado: ver docs/plans/bugs.md."
        )

    # ── Cancelación ─────────────────────────────────────────────────────

    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        """Cancela las órdenes pendientes y devuelve los ids cancelados.

        Se listan primero y se cancelan por id -- ninguno de los dos mercados
        de Bitunix ofrece un "cancel all" de cuenta completa, así que el
        listado previo es obligatorio de todos modos y además deja el
        registro exacto de qué se tocó."""
        orders = self.get_open_orders(market, symbol)
        if not orders:
            return []

        por_simbolo: dict[str, list[str]] = {}
        for order in orders:
            order_id = order.get("orderId") or order.get("id")
            if order_id:
                por_simbolo.setdefault(order.get("symbol") or symbol or "", []).append(str(order_id))

        for sym, order_ids in por_simbolo.items():
            if market == "futures":
                self.futures.cancel_orders(sym, order_ids)
            else:
                unwrap(
                    self.client.cancel_orders([{"orderId": oid, "symbol": sym} for oid in order_ids]),
                    "la cancelación de órdenes spot", on_null={},
                )
        return [oid for ids in por_simbolo.values() for oid in ids]

    # ── Futuros ─────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        if market != "futures":
            raise NotImplementedError("El apalancamiento solo existe en la cuenta de futuros.")
        return self.futures.set_leverage(symbol, leverage)


    # ── Portafolio ──────────────────────────────────────────────────────

    def get_balance_rows(self, market: str = "spot") -> list[BalanceRow]:
        if market == "futures":
            # Una consulta por moneda de margen; Luarix solo opera USDT-M.
            # `available` es lo libre; `frozen` lo reservado por órdenes y
            # `margin` lo inmovilizado en posiciones: el saldo total es la
            # suma de los tres. El no realizado viene partido en cruzado y
            # aislado. (Lectura de la documentación de la API, sin verificar
            # aún contra una cuenta real con posiciones -- ver docs/estado.md.)
            data = self.futures.account(DEFAULT_MARGIN_COIN)
            if not isinstance(data, dict):
                raise BitunixApiError(f"Bitunix devolvió una cuenta de futuros con forma inesperada: {data!r}")
            free = float(data.get("available") or 0)
            locked = float(data.get("frozen") or 0) + float(data.get("margin") or 0)
            unrealized = float(data.get("crossUnrealizedPNL") or 0) + float(data.get("isolationUnrealizedPNL") or 0)
            if free + locked == 0 and unrealized == 0:
                return []
            coin = str(data.get("marginCoin") or DEFAULT_MARGIN_COIN)
            return [BalanceRow(coin, free, locked, unrealized, raw=data)]

        # `/api/spot/v1/user/account`: `balance` es lo libre y `balanceLocked`
        # lo reservado en órdenes (ver `_spot_balance`).
        data = unwrap(self.client.get_account_balance(), "la consulta de balance spot", on_null=[])
        filas = []
        for item in data:
            if "coin" not in item:
                continue
            free, locked = float(item.get("balance") or 0), float(item.get("balanceLocked") or 0)
            if free + locked > 0:
                filas.append(BalanceRow(str(item["coin"]), free, locked, raw=item))
        return filas

    def get_usdt_prices(self) -> dict[str, float]:
        # `/api/spot/v1/common/rate/list` (público): ~1000 pares
        # `{"baseSymbol", "quoteSymbol", "rate"}` contra USDT y monedas fiat,
        # en UNA llamada. Se quedan los que cotizan en USDT.
        data = unwrap(self.client.get_rate_data(), "la consulta de precios en USDT")
        return {
            str(item["baseSymbol"]): float(item["rate"])
            for item in data
            if item.get("quoteSymbol") == QUOTE_ASSET and float(item.get("rate") or 0) > 0
        }


# Monedas de cotización: tenerlas no es una posición abierta, es el efectivo
# con el que se abre. Se excluyen de `get_positions` en spot para que la
# lista signifique lo mismo que en futuros.
_QUOTE_ASSETS = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI"})
