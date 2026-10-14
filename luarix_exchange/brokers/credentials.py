from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Carga un `.env` en `os.environ` y devuelve lo que leyó.

    Implementado a mano (~20 líneas) en vez de con `python-dotenv` a
    propósito: la instalación base de este paquete solo trae `pandas`, y
    añadir una dependencia obligatoria para leer un fichero de texto de
    `CLAVE=valor` no lo justifica. Si `python-dotenv` ya está instalado por
    otro motivo, los dos pueden convivir -- este no toca nada más.

    `override=False` (por defecto) significa que una variable YA presente en
    el entorno gana sobre el fichero. Es lo correcto para producción: el
    `.env` es el apaño de desarrollo local, y lo que inyecta el sistema (un
    contenedor, systemd, el backend) tiene que poder pisarlo.

    Sin `path`, busca un `.env` desde el directorio de trabajo hacia arriba
    -- así funciona igual desde la raíz del repo que desde `notebooks/`.
    """
    archivo = Path(path) if path else _find_dotenv()
    if archivo is None or not archivo.is_file():
        return {}

    leidas: dict[str, str] = {}
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        clave = clave.strip()
        # Las comillas del valor son sintaxis del fichero, no parte de la
        # clave API -- una key entre comillas firma mal y el error que da el
        # exchange ("credenciales inválidas") no apunta a esto en absoluto.
        valor = valor.strip().strip('"').strip("'")
        leidas[clave] = valor
        if override or clave not in os.environ:
            os.environ[clave] = valor
    return leidas


def _find_dotenv(start: Path | None = None) -> Path | None:
    directorio = (start or Path.cwd()).resolve()
    for candidato in [directorio, *directorio.parents]:
        archivo = candidato / ".env"
        if archivo.is_file():
            return archivo
    return None


@dataclass(frozen=True)
class BrokerCredentials:
    """Par de credenciales API (producción + testnet) para un broker.

    El caller es quien resuelve estos valores y los pasa explícitamente a
    `BrokerFactory` -- típicamente desde la base de datos del backend, que
    guarda las claves de CADA usuario. `from_env()` es el atajo para pruebas
    locales: lee el `.env` del repo cuando no hay un backend que las
    proporcione.
    """
    api_key: str = ""
    api_secret: str = ""
    test_api_key: str = ""
    test_api_secret: str = ""
    # Algunos exchanges (KuCoin, OKX) firman con una passphrase además del
    # secreto. Ni Binance ni Bitunix la usan; el campo existe para que
    # añadir uno de esos no obligue a cambiar la firma de la factory.
    passphrase: str = ""

    def resolve(self, testnet: bool) -> tuple[str, str]:
        """Devuelve `(api_key, api_secret)` según el entorno.

        Con `testnet=True` pero SIN claves de testnet cargadas, cae a las de
        producción en vez de devolver dos cadenas vacías. El motivo es
        Bitunix: no tiene testnet en absoluto, así que pedirle credenciales
        de prueba devolvía `("", "")` y la primera llamada fallaba con un
        `10003 API key inexistente` que no dice nada de la causa real.
        """
        if testnet and self.test_api_key and self.test_api_secret:
            return self.test_api_key, self.test_api_secret
        return self.api_key, self.api_secret

    @property
    def has_testnet(self) -> bool:
        return bool(self.test_api_key and self.test_api_secret)

    def __bool__(self) -> bool:
        return bool((self.api_key and self.api_secret) or self.has_testnet)

    # ── Lectura desde el entorno ────────────────────────────────────────

    #-- Por broker, los nombres de variable que se aceptan, EN ORDEN de
    #-- preferencia. Los segundos son alias históricos: el `.env` de este
    #-- repo lleva `TEST_BINANCE_API_KEY` desde antes de que existiera esta
    #-- clase, y renombrarlo a secas dejaría a cualquiera con un `.env`
    #-- viejo mirando un "credenciales vacías" sin explicación.
    #-- `ClassVar` y no una anotación normal: en un dataclass, una anotación
    #-- a secas convierte esto en un CAMPO con default mutable, que revienta
    #-- la definición de la clase con `ValueError: mutable default`.
    _ENV_VARS: ClassVar[dict[str, dict[str, tuple[str, ...]]]] = {
        "BINANCE": {
            "api_key": ("BINANCE_API_KEY",),
            "api_secret": ("BINANCE_API_SECRET",),
            "test_api_key": ("BINANCE_TEST_API_KEY", "TEST_BINANCE_API_KEY"),
            "test_api_secret": ("BINANCE_TEST_API_SECRET", "TEST_BINANCE_API_SECRET"),
        },
        "BITUNIX": {
            "api_key": ("BITUNIX_API_KEY",),
            "api_secret": ("BITUNIX_API_SECRET",),
            # Bitunix NO tiene testnet. Los campos existen para que la forma
            # de la clase sea la misma en todos los brokers, pero se quedan
            # vacíos y `resolve()` cae a las de producción.
            "test_api_key": ("BITUNIX_TEST_API_KEY",),
            "test_api_secret": ("BITUNIX_TEST_API_SECRET",),
        },
    }

    @classmethod
    def from_env(
        cls,
        broker: str,
        *,
        env: dict[str, str] | None = None,
        dotenv_path: str | Path | None = None,
    ) -> BrokerCredentials:
        """Credenciales leídas del entorno / `.env`, para pruebas locales.

        En producción NO se usa: el backend tiene una clave por usuario y se
        la pasa a `BrokerFactory.get_broker(..., credentials=...)`. Esto es
        la red de seguridad para cuadernos y scripts sueltos.

        Args:
            broker: "BINANCE" | "BITUNIX" (o el `AvailableBrokers.X.value`).
            env: de dónde leer. Por defecto `os.environ` tras cargar el `.env`.
            dotenv_path: `.env` concreto; sin él se busca hacia arriba.

        Devuelve un objeto posiblemente VACÍO si no hay nada configurado --
        se comprueba con `if not credentials:`. No levanta acá a propósito:
        quien decide si unas credenciales vacías son un error es la factory,
        que puede nombrar el broker en el mensaje.
        """
        if env is None:
            load_dotenv(dotenv_path)
            env = dict(os.environ)

        nombres = cls._ENV_VARS.get(str(broker).upper())
        if nombres is None:
            return cls()

        def primero(claves: tuple[str, ...]) -> str:
            for clave in claves:
                valor = (env.get(clave) or "").strip()
                if valor:
                    return valor
            return ""

        return cls(**{campo: primero(claves) for campo, claves in nombres.items()})
