from abc import ABC, abstractmethod


class AccountDataProvider(ABC):
    """Todo lo que se le pregunta a la CUENTA de un broker: saldo,
    posiciones, órdenes vivas y el detalle de una orden concreta.

    Devuelve los diccionarios CRUDOS del exchange, tal cual. La
    normalización a `OrderInfo`/`BrokerPosition` vive un piso más arriba, en
    `Broker`: así esta capa se puede comparar renglón a renglón contra la
    documentación de la API cuando algo no cuadra, que es exactamente lo que
    hace falta al depurar contra un exchange real.

    Solo `get_balance` es abstracto. El resto tiene un default que LEVANTA:
    un broker que no sepa hacer algo debe decirlo, nunca devolver una lista
    vacía que se lea como "no tienes nada".
    """

    @abstractmethod
    def get_balance(self, market: str = "spot", asset: str | None = None) -> dict[str, float]:
        """
        Retrieve the AVAILABLE balance per asset.

        "Available" and not "total": es el saldo con el que se puede operar
        ahora mismo, que es la pregunta que hace un grafo antes de entrar.
        Lo bloqueado (`locked` en Binance, `balanceLocked` en el spot de
        Bitunix, `margin`/`frozen` en sus futuros) queda fuera.

        Args:
            market: "spot" (por defecto) o "futures". Un broker que no
                soporte el mercado pedido debe levantar `NotImplementedError`
                en vez de devolver el otro en silencio.
            asset: Filtra por un activo ("USDT"). Ver `Broker.get_balance`:
                si el activo no está en la cuenta se devuelve `{asset: 0.0}`,
                no `{}`.

        Returns:
            dict[str, float]: activo -> saldo disponible.
        """
        pass

    @staticmethod
    def _filter_asset(balances: dict[str, float], asset: str | None) -> dict[str, float]:
        """Aplica el filtro por activo con la semántica del contrato: un
        activo pedido y ausente vale 0.0, no desaparece del resultado."""
        if not asset:
            return balances
        return {asset: float(balances.get(asset, 0.0))}

    def get_positions(self, market: str = "spot", symbol: str | None = None) -> list[dict]:
        """Posiciones abiertas, crudas. Ver `Broker.get_positions`."""
        raise NotImplementedError(f"{type(self).__name__} no sabe listar posiciones.")

    def get_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[dict]:
        """Órdenes pendientes (colocadas y sin llenar) en el exchange.

        `symbol` vacío = todos los pares, cuando el broker lo permita. No
        todos: el spot de Bitunix exige un símbolo, y en ese caso la
        implementación debe decirlo levantando, nunca devolver `[]` -- una
        lista vacía se lee como "no tienes órdenes", que es justo la
        conclusión equivocada.

        Sin implementar por defecto: un broker que no sepa listarlas debe
        fallar de forma explícita en vez de aparentar una cuenta limpia.
        """
        raise NotImplementedError(f"{type(self).__name__} no sabe listar órdenes pendientes.")

    def get_order(self, symbol: str, order_id: str, market: str = "spot") -> dict:
        """El detalle crudo de UNA orden, viva o cerrada.

        Tiene que responder también por las cerradas. Derivarlo de la lista
        de pendientes NO vale: ausente de ahí significa "llenó" O "la
        cancelaron", y tratar las dos igual abrió posiciones fantasma en
        producción (ver `docs/plans/bugs.md`).
        """
        raise NotImplementedError(f"{type(self).__name__} no sabe consultar una orden por id.")

    def cancel_open_orders(self, market: str = "spot", symbol: str | None = None) -> list[str]:
        """Cancela las órdenes pendientes y devuelve los ids cancelados.

        Mismo criterio que `get_open_orders` con `symbol`. Devolver la lista
        de ids (y no un booleano) permite que el grafo registre exactamente
        qué se canceló."""
        raise NotImplementedError(f"{type(self).__name__} no sabe cancelar órdenes.")

    def set_leverage(self, symbol: str, leverage: int, market: str = "futures") -> dict:
        """Apalancamiento de un par en la cuenta de derivados."""
        raise NotImplementedError(f"{type(self).__name__} no tiene cuenta de futuros.")
