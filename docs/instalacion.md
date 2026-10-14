# Instalación

Requiere **Python 3.11 o superior** (el paquete usa `match`, `X | None` y
genéricos nativos).

## Instalación base

```bash
pip install luarix-exchange-library
```

O, desde el repo clonado, en modo editable:

```bash
pip install -e .
```

La instalación base trae **solo `pandas`**. Es suficiente para:

- todos los modelos de `luarix_exchange.models` (`OrderRequest`, `Candle`,
  `BrokerPosition`, `OrderInfo`...),
- el `PaperBroker` completo,
- `BrokerFactory` y `BrokerCredentials`.

No trae ningún SDK de exchange. Eso es deliberado: un servicio que solo corre
backtests contra el simulador no tiene por qué instalar `python-binance`.

## Extras por broker

Cada broker real arrastra su propio SDK y **ninguno es obligatorio**:

```bash
pip install -e ".[binance]"    # python-binance
pip install -e ".[bitunix]"    # bitunix + requests
pip install -e ".[yahoo]"      # yfinance (solo datos de mercado)
pip install -e ".[mt5]"        # MetaTrader5 -- solo Windows
pip install -e ".[all]"        # todos menos mt5
pip install -e ".[test]"       # pytest
```

Se pueden combinar:

```bash
pip install -e ".[binance,bitunix,test]"
```

!!! info "Por qué `mt5` queda fuera de `all`"
    `MetaTrader5` solo publica ruedas para Windows, y `all` tiene que poder
    instalarse en Linux (CI, Docker). Instalarlo ahí falla al compilar, no al
    resolver — un error largo y confuso por una dependencia que probablemente
    ni se quería.

### Qué pasa si falta un extra

`BrokerFactory` importa cada broker **de forma perezosa**, dentro del método,
no arriba del módulo. Así una instalación base puede pedir un `PaperBroker`
sin tener ningún SDK instalado. Si pides uno cuyo SDK falta, el fallo es un
`ModuleNotFoundError` limpio nombrando el paquete que falta:

```pycon
>>> BrokerFactory.get_broker(AvailableBrokers.BINANCE, credentials=creds)
ModuleNotFoundError: No module named 'binance'
```

## Instalación desde otro repo del monorepo

`luarix-trading-framework` (y a través de él `luarix-orchestator` y
`luarix-web-server`) lo consume, y `luarix-arbitrage-web-server` lo hará. En
desarrollo, instalado en editable desde la ruta local:

```bash
# desde cualquiera de los repos consumidores
pip install -e ../luarix-exchange-library[binance,bitunix]
```

Ver la [Guía de migración](integracion/migracion.md).

## Configuración (`.env`)

Solo hace falta para **pruebas locales**. En producción las credenciales las
pasa el backend, una por usuario — ver [Credenciales](guia/credenciales.md).

Copia `.env.example` a `.env` y rellena:

```ini
TRADING_MODE=testnet
ALLOW_LIVE_TRADING=false

BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TEST_API_KEY=
BINANCE_TEST_API_SECRET=

BITUNIX_API_KEY=
BITUNIX_API_SECRET=
```

`.env` está en `.gitignore`. El paquete lo lee con un cargador propio de ~20
líneas (`luarix_exchange.brokers.credentials.load_dotenv`) para no añadir
`python-dotenv` como dependencia obligatoria.

## Comprobar que funciona

```bash
pytest
```

Y una llamada pública, que no necesita credenciales de ningún tipo:

```python
from binance import Client
from luarix_exchange.brokers.market_data.binance import BinanceMarketDataProvider

md = BinanceMarketDataProvider(Client())
print(md.get_candles("BTCUSDT", "1h", limit=3)[-1])
```

## Documentación

Este sitio se construye con MkDocs:

```bash
pip install mkdocs mkdocs-material
mkdocs serve      # http://127.0.0.1:8000
mkdocs build      # genera site/
```
