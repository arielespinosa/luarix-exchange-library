# luarix-exchange-library

Todo lo que sabe hablar con un exchange o broker: conectores reales (Binance,
Bitunix, MetaTrader 5) detrás de **una sola interfaz**, el `PaperBroker`
—el broker simulado— que implementa ese mismo contrato, y los modelos que
intercambian.

```python
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory
from luarix_exchange.brokers.credentials import BrokerCredentials

broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    credentials=BrokerCredentials(api_key="...", api_secret="..."),
)

broker.get_last_candle("BTCUSDT", "1h", market="futures")
broker.get_balance(market="futures", asset="USDT")
broker.get_positions(market="futures")
```

Cambiar `BITUNIX` por `BINANCE` o por `PAPER` **no cambia nada más del código
que lo rodea**. Ese es el punto entero del paquete.

## Qué resuelve

Los dos exchanges describen las mismas cosas de forma incompatible, y no de
manera cosmética:

| Concepto | Binance | Bitunix futuros | Bitunix spot |
|---|---|---|---|
| Cantidad de una orden | `origQty` | `qty` | `volume` |
| Ya ejecutado | `executedQty` | `tradeQty` | `dealVolume` |
| Lado | `"BUY"` / `"SELL"` | `"BUY"` / `"SELL"` | `1` / `2` |
| Llenado parcial | `PARTIALLY_FILLED` | `PART_FILLED` | `PART_FILLED` |
| Cantidad de una posición | `positionAmt` (**con signo**) | `qty` + `side` aparte | — |
| Velas | ascendente | descendente | descendente |

Sin una capa que unifique esto, cualquier código que quiera funcionar con los
dos acaba con un `if broker == ...` por cada campo. Y los detalles que se
escapan no fallan ruidosamente: un `positionAmt` negativo leído como cantidad
positiva es un corto que parece un largo.

## Qué sabe hacer

Todo acepta `market="spot"` o `market="futures"`.

| | |
|---|---|
| **Velas** | la última, las N últimas, un rango de fechas |
| **Precio** | último precio negociado, libro de órdenes |
| **Cuenta** | saldo (total o por activo), posiciones abiertas |
| **Órdenes** | listar abiertas, crear, consultar por id, editar, cancelar |
| **Futuros** | TP/SL adjuntos a la entrada, apalancamiento, `reduce_only` |

La lista completa con firmas está en [Contrato `Broker`](referencia/broker.md).

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

Es un **paquete instalable e independiente**. No importa nada de sus
consumidores: la dependencia va siempre al revés, ellos lo consumen a él.

Si vienes de `luarix_trading.brokers.*`, empieza por la
[Guía de migración](integracion/migracion.md). El orden de trabajo hasta que
`luarix-arbitrage-web-server` también lo use está en `docs/plans/roadmap.md`
del repo.

## Por dónde empezar

<div class="grid cards" markdown>

- **[Instalación](instalacion.md)** — `pip install`, los extras opcionales.
- **[Inicio rápido](inicio-rapido.md)** — de cero a leer una vela y un saldo.
- **[Arquitectura](conceptos/arquitectura.md)** — qué capa hace qué y por qué.
- **[Estado y limitaciones](estado.md)** — **léelo antes de operar con dinero.**

</div>

!!! danger "Lo mínimo que hay que saber antes de operar"
    - **Bitunix no tiene testnet.** Lo que se coloque con esas claves es dinero real.
    - **Las API keys de Bitunix están restringidas por IP.** Si la IP no está
      en la whitelist, toda llamada firmada falla con `10004 Invalid IP`.
    - **`test_order=True` en Binance no coloca nada** — y es el valor por
      defecto de la factory.
    - **El camino de Binance futuros no está validado contra la API real.**

    Los cuatro están explicados en [Estado y limitaciones](estado.md).
