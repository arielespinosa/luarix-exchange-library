"""Cliente de la API de FUTUROS de Bitunix.

El paquete `bitunix` de PyPI es SOLO spot: todos sus endpoints cuelgan de
`/api/spot/v1/...` y no expone nada de derivados. La cuenta de futuros vive
en otro host (`fapi.bitunix.com`) con el MISMO esquema de firma, así que se
implementa aquí con `requests` en vez de añadir una dependencia.

Firma verificada contra la cuenta real: dos SHA-256 encadenados, primero
sobre `nonce + timestamp + api_key + payload` y el resultado otra vez con el
secreto concatenado. En GET el payload son las claves y valores ORDENADOS y
CONCATENADOS sin separadores (`marginCoinUSDT`, no `marginCoin=USDT` -- con
el formato de `urlencode` que usa `BitunixClient.sign_request` la firma no
valida). En POST es el cuerpo JSON tal cual viaja.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import requests

from luarix_exchange.brokers.bitunix_response import BitunixApiError, unwrap

BASE_URL = "https://fapi.bitunix.com"

ACCOUNT = "/api/v1/futures/account"
PENDING_ORDERS = "/api/v1/futures/trade/get_pending_orders"
# El estado REAL de UNA orden, viva o no. `get_pending_orders` no sirve para
# esto: solo lista las vivas, así que una orden ausente de ahí puede haber
# llenado O haber sido cancelada, y no hay forma de distinguirlas. Ver
# `BitunixBroker.get_order_status` y `docs/plans/bugs.md`.
ORDER_DETAIL = "/api/v1/futures/trade/get_order_detail"
CANCEL_ORDERS = "/api/v1/futures/trade/cancel_orders"
PLACE_ORDER = "/api/v1/futures/trade/place_order"
# Enmienda NATIVA de una orden pendiente: cambia precio/cantidad/TP/SL sin
# cancelarla. Es lo que hace que editar una orden en Bitunix futuros no
# tenga la ventana de desprotección del cancelar-y-recolocar que sí sufre
# el spot (ver `BitunixBroker.update_order`).
MODIFY_ORDER = "/api/v1/futures/trade/modify_order"
PENDING_POSITIONS = "/api/v1/futures/position/get_pending_positions"
CHANGE_LEVERAGE = "/api/v1/futures/account/change_leverage"
# Kline PÚBLICO de futuros (sin firma): no le afecta la whitelist de IP de
# la API key. Es además el único kline de Bitunix que respeta `limit` y
# `endTime` -- el de spot los ignora, ver `market_data/bitunix.py`.
KLINE = "/api/v1/futures/market/kline"

# La cuenta de futuros se consulta por moneda de margen, de una en una (a
# diferencia de spot, que devuelve todas). USDT es la única realista hoy.
DEFAULT_MARGIN_COIN = "USDT"


class BitunixFuturesClient:

    def __init__(self, api_key: str, api_secret: str, timeout: int = 20):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self._position_mode: str | None = None

    # ── Transporte firmado ──────────────────────────────────────────────

    def _signature(self, timestamp: str, nonce: str, payload: str) -> str:
        digest = hashlib.sha256(f"{nonce}{timestamp}{self.api_key}{payload}".encode()).hexdigest()
        return hashlib.sha256((digest + self.api_secret).encode()).hexdigest()

    def _headers(self, timestamp: str, nonce: str, sign: str) -> dict:
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "nonce": nonce,
            "sign": sign,
            "language": "en-US",
        }

    def get(self, path: str, params: dict | None = None, *, action: str, on_null=None):
        params = params or {}
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        query = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        response = requests.get(
            f"{BASE_URL}{path}", params=params,
            headers=self._headers(timestamp, nonce, self._signature(timestamp, nonce, query)),
            timeout=self.timeout,
        )
        response.raise_for_status()
        if on_null is None:
            return unwrap(response.json(), action)
        return unwrap(response.json(), action, on_null=on_null)

    def post(self, path: str, body: dict, *, action: str, on_null=None):
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        # Se serializa UNA vez y se manda ese mismo texto como `data`: la
        # firma es sobre el cuerpo tal cual viaja, así que no se puede dejar
        # que `requests` lo vuelva a generar con otro espaciado.
        payload = json.dumps(body, separators=(",", ":"))
        headers = self._headers(timestamp, nonce, self._signature(timestamp, nonce, payload))
        headers["Content-Type"] = "application/json"
        response = requests.post(f"{BASE_URL}{path}", data=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        if on_null is None:
            return unwrap(response.json(), action)
        return unwrap(response.json(), action, on_null=on_null)

    # ── Cuenta ──────────────────────────────────────────────────────────

    def account(self, margin_coin: str = DEFAULT_MARGIN_COIN) -> dict:
        return self.get(ACCOUNT, {"marginCoin": margin_coin}, action="la consulta de balance de futuros")

    def position_mode(self, margin_coin: str = DEFAULT_MARGIN_COIN) -> str:
        """`"HEDGE"` o `"ONE_WAY"`, cacheado por instancia.

        Decide si una orden lleva `tradeSide`. En ONE_WAY el exchange netea
        solo -- una venta contra un largo abierto lo reduce. En HEDGE NO:
        abre una posición corta independiente que convive con la larga, y
        cada orden debe declarar si abre o cierra."""
        if self._position_mode is None:
            self._position_mode = str(self.account(margin_coin).get("positionMode") or "ONE_WAY").upper()
        return self._position_mode

    # ── Órdenes ─────────────────────────────────────────────────────────

    def pending_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        data = self.get(PENDING_ORDERS, params, action="la consulta de órdenes pendientes de futuros", on_null={})
        return list(data.get("orderList") or []) if isinstance(data, dict) else list(data or [])

    def order_detail(self, order_id: str) -> dict:
        """La orden completa por su `orderId`, esté viva o cerrada.

        Devuelve `{}` cuando el exchange no la conoce -- que NO es lo mismo
        que "se cerró". Quien llama decide qué hacer con esa ignorancia; lo
        que no puede es inventarse un estado.

        Campos que interesan del cuerpo (hay más: fee, realizedPNL, tpPrice,
        slPrice, leverage...): `status` -- `INIT` / `NEW` / `PART_FILLED` /
        `CANCELED` / `FILLED` -- y `tradeQty`, la cantidad realmente
        ejecutada.

        Ojo con el nombre del estado parcial: Bitunix dice `PART_FILLED`,
        Binance `PARTIALLY_FILLED`. La traducción vive en
        `BitunixBroker.get_order_status`, no acá."""
        data = self.get(
            ORDER_DETAIL, {"orderId": str(order_id)},
            action="la consulta del detalle de una orden de futuros", on_null={},
        )
        return data if isinstance(data, dict) else {}

    def cancel_orders(self, symbol: str, order_ids: list[str]) -> dict:
        return self.post(
            CANCEL_ORDERS,
            {"symbol": symbol, "orderList": [{"orderId": order_id} for order_id in order_ids]},
            action="la cancelación de órdenes de futuros", on_null={},
        )

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: float | None = None,
        price: float | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
    ) -> dict:
        """Enmienda NATIVA de una orden pendiente -- no la cancela.

        Es la diferencia importante con el spot: acá la orden nunca deja de
        existir, así que mover un stop-loss no abre una ventana en la que la
        posición esté desprotegida.

        Bitunix exige mandar `qty` y `price` SIEMPRE, aunque solo se quiera
        tocar uno: los campos ausentes no se "conservan", se interpretan
        como falta de parámetro. Por eso `BitunixBroker.update_order` lee
        antes la orden con `order_detail()` y rellena lo que el llamador no
        pasó. Hacerlo acá dentro escondería una llamada extra a la API
        dentro de algo que parece una sola.

        `tpPrice`/`slPrice` son los niveles ADJUNTOS a la orden (los que
        viajaron en `place_order`), no órdenes propias.
        """
        body: dict[str, str] = {"orderId": str(order_id)}
        if quantity is not None:
            body["qty"] = str(quantity)
        if price is not None:
            body["price"] = str(price)
        if tp_price is not None:
            body["tpPrice"] = str(tp_price)
            body["tpStopType"] = self.TRIGGER_TYPE
        if sl_price is not None:
            body["slPrice"] = str(sl_price)
            body["slStopType"] = self.TRIGGER_TYPE
        return self.post(MODIFY_ORDER, body, action="la modificación de la orden de futuros", on_null={})

    # ── Posiciones ──────────────────────────────────────────────────────

    def pending_positions(self, symbol: str | None = None) -> list[dict]:
        """Las posiciones ABIERTAS ahora mismo.

        El endpoint ya estaba declarado como constante desde hace tiempo
        pero NADIE lo llamaba: `BitunixBroker.get_positions()` devolvía
        `{}` fijo, o sea que este broker afirmaba no tener ninguna posición
        abierta pasara lo que pasara. Es un endpoint firmado, así que le
        afecta la whitelist de IP de la API key.

        Campos del cuerpo: `positionId`, `symbol`, `qty` (SIN signo),
        `side` (`BUY`/`SELL`), `avgOpenPrice`, `markPrice`, `unrealizedPNL`,
        `leverage`, `marginMode`, `positionMode`.
        """
        params = {"symbol": symbol} if symbol else {}
        data = self.get(PENDING_POSITIONS, params,
                        action="la consulta de posiciones abiertas de futuros", on_null=[])
        if isinstance(data, dict):
            # Bitunix no es consistente: unos endpoints devuelven la lista
            # pelada y otros la envuelven en una clave. Se aceptan las dos
            # formas en vez de apostar por una.
            return list(data.get("positionList") or data.get("list") or [])
        return list(data or [])

    def set_leverage(self, symbol: str, leverage: int, margin_coin: str = DEFAULT_MARGIN_COIN) -> dict:
        """Apalancamiento del par. Afecta a TODA la cuenta para ese símbolo,
        no solo a la siguiente orden -- incluida cualquier posición que otra
        sesión tenga abierta ahí."""
        return self.post(
            CHANGE_LEVERAGE,
            {"symbol": symbol, "marginCoin": margin_coin, "leverage": int(leverage)},
            action="el cambio de apalancamiento", on_null={},
        )

    # ── Velas (público, sin firma) ──────────────────────────────────────

    #-- Los intervalos que acepta el kline de futuros, verificados uno a uno
    #-- contra la API. Ojo: NO son los códigos numéricos del kline de spot
    #-- ("240"), que acá devuelven `data` vacío -- son los nombres estándar.
    KLINE_INTERVALS = frozenset({
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    })

    def kline(self, symbol: str, interval: str, limit: int = 200,
              end_ms: int | None = None) -> list[dict]:
        """Hasta `limit` velas que TERMINAN en `end_ms` (por defecto, ahora).

        Devuelve de la más reciente a la más antigua, que es como responde
        el endpoint. Ordenarlas es responsabilidad de quien las consume
        (`market_data/bitunix.py`), que además tiene que deduplicar al
        paginar.
        """
        if interval not in self.KLINE_INTERVALS:
            raise BitunixApiError(
                f"El kline de futuros de Bitunix no acepta el intervalo {interval!r}. "
                f"Disponibles: {', '.join(sorted(self.KLINE_INTERVALS))}."
            )
        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_ms is not None:
            params["endTime"] = end_ms
        # Sin firma: es público. Se usa `requests` directo en vez de `self.get`
        # para no gastar una firma (y para que funcione con una API key cuya
        # IP no esté en la whitelist, que es el caso más común al depurar).
        response = requests.get(f"{BASE_URL}{KLINE}", params=params, timeout=self.timeout)
        response.raise_for_status()
        return list(unwrap(response.json(), "la consulta de velas de futuros", on_null=[]) or [])

    # Los ÚNICOS `orderType` que acepta `/futures/trade/place_order`,
    # verificados contra la API real (un valor cualquiera devuelve
    # `code=10008 ... alternative value ["ALL","LIMIT","MARKET"]`). "ALL" es
    # un filtro de consulta, no algo que se pueda colocar. En particular NO
    # existe ningún tipo de stop acá: el stop-loss va adjunto a la orden
    # (`slPrice`, abajo), nunca como una orden propia.
    PLACEABLE_ORDER_TYPES = ("LIMIT", "MARKET")

    # Con qué precio se dispara el TP/SL adjunto. LAST_PRICE = el último
    # precio negociado, que es lo que ve el usuario en el gráfico; la
    # alternativa (MARK_PRICE) dispara con el precio de marca y sorprende a
    # cualquiera que haya puesto el nivel mirando la vela.
    TRIGGER_TYPE = "LAST_PRICE"

    def place_order(self, *, symbol: str, side: str, quantity: float,
                    price: float | None, order_type: str,
                    tp_price: float | None = None, sl_price: float | None = None,
                    reduce_only: bool = False,
                    margin_coin: str = DEFAULT_MARGIN_COIN) -> dict:
        """Coloca una orden de futuros, opcionalmente con su TP/SL adjunto.

        `tp_price`/`sl_price` viajan EN la misma orden (`tpPrice`/`slPrice`).
        Es la forma nativa de Bitunix y la única atómica: la posición nunca
        llega a existir sin protección. Antes el motor mandaba el TP y el SL
        como dos órdenes normales del lado contrario -- ver `reduce_only`.

        `reduce_only=True` marca la orden como CIERRE de una posición abierta.
        ⚠ Esto es lo que arregla el bug más grave que ha tenido este broker:
        en modo HEDGE toda orden llevaba `tradeSide="OPEN"` fijo, así que el
        take-profit de un LARGO (un SELL) no lo cerraba -- ABRÍA UN CORTO
        REAL que convivía con él. Caso documentado con ids de orden en
        `docs/plans/bugs.md`. En ONE_WAY el exchange netea solo y el campo ni
        se manda, que es por lo que nadie lo vio antes."""
        if order_type not in self.PLACEABLE_ORDER_TYPES:
            # Falla acá, con el motivo, en vez de dejar que la API devuelva un
            # `10008` genérico desde dentro de `unwrap`. El caso real fue un
            # `STOP_LOSS_LIMIT` (un tipo de Binance) que llegó hasta el
            # exchange: la entrada quedó abierta y SIN STOP, y el traceback no
            # decía en ningún sitio que el stop no se había colocado.
            raise BitunixApiError(
                f"Bitunix futuros no puede colocar una orden de tipo {order_type!r} "
                f"(solo acepta {', '.join(self.PLACEABLE_ORDER_TYPES)}). "
                "Un stop-loss se adjunta a la orden con `sl_price`, no se coloca por separado."
            )

        body = {
            "symbol": symbol,
            "qty": str(quantity),
            "side": side,                 # BUY / SELL
            "orderType": order_type,      # LIMIT / MARKET
        }
        if price is not None:
            body["price"] = str(price)
        if order_type == "LIMIT":
            body["effect"] = "GTC"
        if tp_price is not None:
            body["tpPrice"] = str(tp_price)
            body["tpStopType"] = self.TRIGGER_TYPE
        if sl_price is not None:
            body["slPrice"] = str(sl_price)
            body["slStopType"] = self.TRIGGER_TYPE
        if self.position_mode(margin_coin) == "HEDGE":
            # En modo cobertura cada orden declara si abre o cierra. Valores
            # aceptados: exactamente ["OPEN","CLOSE"] (verificado contra la
            # API; en minúsculas la rechaza).
            body["tradeSide"] = "CLOSE" if reduce_only else "OPEN"
        return self.post(PLACE_ORDER, body, action="la colocación de la orden de futuros")
