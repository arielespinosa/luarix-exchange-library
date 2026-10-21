# luarix-exchange-library

Todo lo que sabe hablar con un exchange o broker: conectores reales (Binance,
Bitunix, MetaTrader 5, Yahoo para datos) detrás de una interfaz única, el
`PaperBroker` —el broker simulado— que implementa ese mismo contrato, y los
modelos que intercambian (velas, órdenes, fills, posiciones).

Es la librería **compartida** por los proyectos de Luarix que operan con
exchanges. Se instala como `luarix-exchange-library` y se importa como
`luarix_exchange`.

## Quién lo usa

```
  luarix-web-server ──gRPC──▶ luarix-orchestator     luarix-arbitrage-web-server
          │                          │                          │
          └─────▶ luarix-trading-framework ◀──┘                 │
                  (estadísticas, modelos                        │
                   de estrategia)                               │
                           │                                    │
                           ▼                                    ▼
                  ┌───────────────────────────────────────────────────┐
                  │           ESTE REPO: luarix-exchange-library      │
                  │  (brokers reales, PaperBroker, órdenes, velas)    │
                  └───────────────────────────────────────────────────┘
```

Este repo **no** conoce a ninguno de sus consumidores: la flecha va siempre
hacia acá. El código salió de `luarix-trading-framework` el 2026-10-14; la
[Guía de migración](docs/integracion/migracion.md) tiene la tabla de rutas
viejas → nuevas. Lo que falta para que `luarix-arbitrage-web-server` también
lo use (contrato por capacidades, websockets, Kraken, Bitfinex) está en
[`docs/plans/roadmap.md`](docs/plans/roadmap.md).

## Documentación

La documentación completa vive en `docs/` y se sirve con MkDocs:

```bash
pip install -e ".[docs]"
mkdocs serve          # http://127.0.0.1:8000
```

Empieza por **Inicio rápido**. **Antes de operar con dinero**, lee la página
**Estado y limitaciones**.

## Estructura

```
luarix_exchange/
├── capabilities/      # Qué sabe hacer un exchange: MarketData, Account, Trading,
│                      #   NativeOco, SymbolRulesSource, streams... (Fase 3)
├── factory.py         # ExchangeFactory: crear por nombre y consultar capacidades
├── brokers/           # Conectores concretos detrás de brokers.base.Broker
│   ├── base.py        #   la interfaz abstracta que todos implementan
│   ├── factory.py     #   BrokerFactory: nombre -> broker configurado
│   ├── paper.py       #   broker simulado: mismo contrato, sin red. Resuelve
│   │                  #   fills contra el rango high/low de cada vela
│   ├── binance.py     #   brokers reales -- cada uno arrastra su propio SDK,
│   ├── bitunix.py     #   y ninguno es obligatorio (ver extras)
│   ├── mt5.py
│   ├── account_data/  #   balance, posiciones y órdenes abiertas por broker
│   └── market_data/   #   velas históricas/recientes y feeds en vivo
└── models/            # Velas, órdenes, fills, posiciones; y en Decimal: OrderBook,
                       #   SymbolRules, CommissionRates, OrderUpdate
```

## Instalación

Desde GitHub (lo que usan los otros repos; fija siempre un commit, ver abajo):

```bash
pip install "luarix-exchange-library[all] @ git+https://github.com/arielespinosa/luarix-exchange-library.git@<commit>"
```

Desde el repo clonado, en editable (desarrollo):

```bash
pip install -e ".[all]"
```

> **Ojo — fijar el commit:** `luarix-trading-framework`, `luarix-web-server`
> y `luarix-orchestator` fijan esta librería con la MISMA URL
> (`...luarix-exchange-library.git@<sha>`). Si dos de ellos piden URLs
> distintas, `pip` aborta con `ResolutionImpossible`. Al publicar una versión
> nueva, cambia el commit en los tres a la vez.

La instalación base solo trae `pandas` — suficiente para `PaperBroker` y
todos los modelos.

Cada broker real es un extra opcional:

```bash
pip install -e ".[binance]"    # python-binance
pip install -e ".[bitunix]"    # bitunix + requests
pip install -e ".[yahoo]"      # yfinance (solo datos de mercado)
pip install -e ".[mt5]"        # MetaTrader5 -- solo Windows
pip install -e ".[all]"        # todos menos mt5 (ver abajo)
```

`mt5` queda fuera de `all` a propósito: `MetaTrader5` solo publica ruedas
para Windows, y `all` tiene que poder instalarse en Linux (CI, Docker).

## Uso

```python
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory
from luarix_exchange.brokers.credentials import BrokerCredentials

broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    credentials=BrokerCredentials(api_key="...", api_secret="..."),
)
```

Sin `credentials`, se leen del entorno o del `.env` (`BITUNIX_API_KEY`,
`BINANCE_API_KEY`...). Eso es para pruebas locales; en producción las pasa
el backend, una por usuario. Ver `.env.example`.

Cambiar `BITUNIX` por `BINANCE` o por `PAPER` no cambia nada más del código
que lo rodea: los tres implementan `brokers.base.Broker`.

### Lo que sabe hacer un `Broker`

Todo acepta `market="spot"` o `market="futures"`.

```python
broker.get_last_candle("BTCUSDT", "1h", market="futures")
broker.get_candles("BTCUSDT", "1h", limit=500, market="futures")
broker.get_historical_candles("BTCUSDT", "4h", start=..., end=...)
broker.get_ticker("BTCUSDT", market="futures")
broker.get_order_book("BTCUSDT", limit=20)

broker.get_balance(market="futures")                  # todos los activos
broker.get_balance(market="futures", asset="USDT")    # uno solo
broker.get_positions(market="futures")                # -> list[BrokerPosition]

broker.get_open_orders(market="futures")              # -> list[OrderInfo]
broker.get_order("BTCUSDT", order_id, market="futures")
broker.get_order_status("BTCUSDT", order_id, market="futures")

broker.place_order(OrderRequest(...))
broker.update_order("BTCUSDT", order_id, price=..., quantity=...)
broker.cancel_order("BTCUSDT", order_id, market="futures")
broker.cancel_open_orders(market="futures", symbol="BTCUSDT")

broker.set_leverage("BTCUSDT", 5)
```

`get_positions` y `get_open_orders` devuelven tipos **normalizados**
(`BrokerPosition`, `OrderInfo`): los dos exchanges llaman distinto a los
mismos campos, y Binance además firma la cantidad de una posición mientras
Bitunix no. El diccionario original del exchange se conserva entero en
`.raw`.

Lo que un broker no sabe hacer **levanta**, nunca devuelve vacío: una lista
vacía se leería como "no tienes posiciones", que es justo la conclusión
equivocada.

## Probarlo

`notebooks/` tiene un cuaderno por bloque de operaciones — credenciales,
velas, saldo y posiciones, órdenes. Empieza por
`00_setup_y_credenciales.ipynb`, que distingue "no hay conexión" de "las
credenciales o la whitelist de IP están mal".

⚠ **Bitunix no tiene testnet**: lo que se coloque con esas claves es dinero
real. Binance sí, con claves distintas (`BINANCE_TEST_API_KEY`).

## Tests

```bash
pytest
```

`tests/test_broker_contract.py` es el que importa: comprueba con clientes
falsos que Binance y Bitunix responden **lo mismo** a las mismas preguntas,
en spot y en futuros. Los dobles usan las respuestas reales de cada API,
nombres de campo incluidos, que es donde estaban los bugs.

## Estado y limitaciones conocidas

`docs/plans/bugs.md` lleva el registro. Lo que hay que saber antes de operar:

- El camino de **Binance futuros** está implementado pero **no validado
  contra la API real** (restricción geográfica en la cuenta disponible).
- Las API keys de **Bitunix están restringidas por IP**. Si la IP no está en
  la whitelist, toda llamada firmada falla con `10004 Invalid IP` — los
  klines siguen funcionando porque son públicos.
- **Editar una orden en spot cambia su `order_id`** (no hay enmienda nativa:
  es cancelar y recolocar, y no es atómico). En futuros sí la hay.
