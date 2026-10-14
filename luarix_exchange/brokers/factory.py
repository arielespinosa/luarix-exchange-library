from enum import Enum

from luarix_exchange.brokers.base import Broker
from luarix_exchange.brokers.credentials import BrokerCredentials
from luarix_exchange.brokers.paper import PaperBroker


class AvailableBrokers(Enum):
    """Brokers que esta factory sabe construir.

    Solo los que existen de verdad. `KUCOIN` estaba en esta lista sin
    ninguna implementación detrás: `get_broker(KUCOIN)` devolvía `None` en
    silencio, y quien recibiera ese `None` reventaba mucho después con un
    `AttributeError` que no mencionaba KuCoin por ningún lado. `MT5` estaba
    igual (`case AvailableBrokers.MT5: pass`), y además no cabe en esta
    firma: `MT5Broker` no se conecta con api_key/secret sino con
    login/password/servidor de un terminal instalado. Se construye
    directamente (`MT5Broker(login=..., password=..., server=...)`).
    """
    PAPER = "PAPER"
    BINANCE = "BINANCE"
    BITUNIX = "BITUNIX"


class BrokerFactory:
    """Construye brokers listos para usar, con las credenciales resueltas.

    **De dónde salen las credenciales.** El caso normal en producción es que
    las pase el backend: hay un usuario por cuenta y cada uno tiene sus
    propias claves guardadas. Por eso `credentials` es un parámetro
    explícito y esta clase NO lee ninguna configuración global.

    Si NO se pasan, se leen del entorno / del `.env` del repo
    (`BINANCE_API_KEY`, `BITUNIX_API_KEY`...). Eso es para pruebas locales
    -- cuadernos, scripts sueltos -- no para servir a varios usuarios.

    ```python
    # Producción: las claves vienen del backend, una por usuario.
    creds = BrokerCredentials(api_key=fila.api_key, api_secret=fila.api_secret)
    broker = BrokerFactory.get_broker(AvailableBrokers.BITUNIX, credentials=creds)

    # Local: sin credenciales, salen del .env.
    broker = BrokerFactory.get_broker(AvailableBrokers.BITUNIX)
    ```
    """

    @classmethod
    def get_broker(
        cls,
        broker_name: str | AvailableBrokers,
        testnet: bool | None = True,
        test_order: bool = True,
        credentials: BrokerCredentials | None = None,
        trading_mode: str = "testnet",
        allow_live_trading: bool = False,
    ) -> Broker:
        """Devuelve una instancia del broker solicitado, lista para usar.

        Args:
            broker_name: El broker deseado. Acepta el enum
                (`AvailableBrokers.BINANCE`) o su nombre en texto
                ("BINANCE", "binance") -- que es como llega desde una API o
                una base de datos.
            testnet:     Si True, conecta a la testnet (dinero ficticio).
                         Bitunix NO tiene testnet y lo ignora.
            test_order:  Solo Binance. Si True, las órdenes se VALIDAN contra
                         la API pero no se ejecutan. Ojo: con esto en True
                         nada llega nunca al mercado.
            credentials: Credenciales del broker. Sin ellas se leen del
                         entorno / `.env` (ver el docstring de la clase).
            trading_mode: "paper" | "testnet" | "live".
            allow_live_trading: Segundo candado contra ejecuciones
                         accidentales en real. Para operar con dinero hacen
                         falta LOS DOS: `trading_mode="live"` y esto en True.

        Raises:
            ValueError: Si el broker no existe, o si no hay credenciales ni
                en el argumento ni en el entorno. Antes devolvía `None` en
                los dos casos y el fallo aparecía mucho más tarde, ya lejos
                de la causa.
        """
        broker = cls._normalize(broker_name)

        if broker is AvailableBrokers.PAPER:
            return PaperBroker()

        creds = cls._resolve_credentials(broker, credentials)

        if broker is AvailableBrokers.BINANCE:
            # Import PEREZOSO, no por estilo: cada broker real arrastra su
            # propio SDK y ninguno es obligatorio (son extras de
            # `pyproject.toml`). Importándolos arriba, una instalación base
            # -- o una que solo tenga el extra `[bitunix]` -- no podía ni
            # importar esta factory para pedir un PaperBroker.
            from luarix_exchange.brokers.binance import BinanceBroker

            api_key, api_secret = creds.resolve(bool(testnet))
            return BinanceBroker(
                api_key=api_key,
                api_secret=api_secret,
                testnet=bool(testnet),
                test_order=test_order,
                trading_mode=trading_mode,
                allow_live_trading=allow_live_trading,
            )

        from luarix_exchange.brokers.bitunix import BitunixBroker

        # `resolve(False)` a propósito: Bitunix NO tiene testnet. Pedirle las
        # credenciales de prueba devolvía `("", "")` y la primera llamada
        # fallaba con un `10003 API key inexistente` que no dice nada de la
        # causa real -- y eso ocurría con el DEFAULT de esta factory
        # (`testnet=True`), o sea siempre que alguien no lo pensara.
        api_key, api_secret = creds.resolve(False)
        return BitunixBroker(
            api_key=api_key,
            api_secret=api_secret,
            trading_mode=trading_mode,
            allow_live_trading=allow_live_trading,
        )

    @staticmethod
    def _normalize(broker_name: str | AvailableBrokers) -> AvailableBrokers:
        if isinstance(broker_name, AvailableBrokers):
            return broker_name
        try:
            return AvailableBrokers(str(broker_name).upper())
        except ValueError:
            disponibles = ", ".join(b.value for b in AvailableBrokers)
            raise ValueError(
                f"No existe el broker {broker_name!r}. Disponibles: {disponibles}."
            ) from None

    @staticmethod
    def _resolve_credentials(
        broker: AvailableBrokers, credentials: BrokerCredentials | None,
    ) -> BrokerCredentials:
        if credentials:
            return credentials
        # `credentials` puede llegar como un objeto VACÍO (todos los campos
        # en "") desde un backend que aún no tiene claves para ese usuario.
        # `BrokerCredentials.__bool__` lo detecta, así que ese caso también
        # cae al `.env` en vez de mandar cadenas vacías al exchange.
        del_entorno = BrokerCredentials.from_env(broker.value)
        if del_entorno:
            return del_entorno
        raise ValueError(
            f"No hay credenciales para {broker.value}. Pásalas con "
            f"`credentials=BrokerCredentials(api_key=..., api_secret=...)`, o define "
            f"{broker.value}_API_KEY y {broker.value}_API_SECRET en el entorno o en el .env."
        )
