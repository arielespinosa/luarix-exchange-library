# Arquitectura

## Las tres capas

```
┌─────────────────────────────────────────────────────────────┐
│  Broker  (brokers/base.py)                                  │
│  El contrato. Devuelve tipos del DOMINIO:                   │
│  Candle, OrderInfo, BrokerPosition, OrderAck, Fill          │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
    ┌───────────▼──────────┐      ┌───────────▼──────────────┐
    │ MarketDataProvider   │      │ AccountDataProvider      │
    │ velas, precio, libro │      │ saldo, posiciones,       │
    │ (público)            │      │ órdenes  (firmado)       │
    │ Devuelve dicts CRUDOS del exchange, sin normalizar     │
    └───────────┬──────────┘      └───────────┬──────────────┘
                │                             │
        ┌───────▼─────────────────────────────▼───────┐
        │  SDK del exchange                           │
        │  python-binance / bitunix / BitunixFutures  │
        └─────────────────────────────────────────────┘
```

### Por qué los proveedores devuelven crudo

Es deliberado. Cuando algo no cuadra contra un exchange real —y pasa— quieres
poder comparar **renglón a renglón** lo que devuelve el proveedor contra la
documentación de la API, sin una capa de traducción en medio adivinando.

La normalización vive un piso más arriba, en `Broker`, donde
`OrderInfo.from_binance()` y `BrokerPosition.from_bitunix_futures()` hacen la
traducción en un solo sitio y de forma auditable. El diccionario original
sobrevive entero en el campo `.raw` de cada objeto normalizado, así que nunca
se pierde información.

## Qué es abstracto y qué no

En `Broker` la distinción tiene un criterio, no es histórica:

| | Qué es | Ejemplos |
|---|---|---|
| **`@abstractmethod`** | Lo que **ningún** broker puede heredar: hablar con su propia API | `place_order`, `cancel_order`, `get_balance`, `get_positions`, `get_ticker`, `connect` |
| **Concreto, delegando** | Lo que se resuelve pidiéndoselo a un proveedor | `get_candles`, `get_order`, `get_open_orders`, `set_leverage` |

Un broker nuevo que traiga sus dos proveedores hereda gratis todas las
consultas. Uno que no los tenga (`PaperBroker`, `MT5Broker`) falla con un
mensaje que lo dice:

```pycon
>>> PaperBroker().get_open_orders()
NotImplementedError: PaperBroker no expone datos de cuenta.
```

## La regla que gobierna todo: fallar en voz alta

**Un broker que no sabe hacer algo levanta. Nunca devuelve vacío.**

No es una preferencia de estilo. Una lista vacía se lee como *"no tienes
posiciones"* o *"no tienes órdenes"*, que en un sistema que coloca dinero es
justo la conclusión equivocada. Los tres casos reales que motivaron la regla:

!!! danger "Los tres incidentes"
    - `BitunixBroker.get_positions()` devolvía `{}` **fijo**. Afirmaba no
      tener ninguna posición abierta pasara lo que pasara.
    - El spot de Bitunix no puede listar las órdenes de toda la cuenta (su
      endpoint exige símbolo). Devolver `[]` hacía creer que la cuenta estaba
      limpia; ahora levanta.
    - `get_order_status` daba por **llenada** toda orden ausente de la lista de
      pendientes. Una orden cancelada a mano también desaparece de ahí:
      tres cortos cancelados quedaron marcados `FILLED` en base de datos, con
      posiciones `ACTIVE` que en el exchange nunca existieron.

    El tercero está reconstruido con ids reales en `docs/plans/bugs.md`.

Corolario práctico: si una llamada devuelve una lista vacía, **es** una lista
vacía, no un fallo tragado.

## Flags de capacidad

Cada broker declara qué sabe hacer. Son atributos de clase, no algo que se
descubra en tiempo de ejecución, para que un motor pueda ramificar **antes** de
comprometerse. Desde la Fase 3, `supports_oco` y `supports_futures` se
**derivan** de las capacidades y los mercados de la clase (ver
[Capacidades](../referencia/capacidades.md)); los valores no cambiaron:

```python
broker.supports_futures              # ¿tiene cuenta de derivados?
broker.supports_oco                  # ¿OCO nativo del exchange?
broker.supports_independent_orders   # ¿Entry+TP+SL como 3 órdenes propias?
```

| Broker | `supports_futures` | `supports_oco` | `supports_independent_orders` |
|---|---|---|---|
| `BinanceBroker` | ✅ | ✅ | ✅ |
| `BitunixBroker` | ✅ | ❌ | ✅ |
| `PaperBroker` | ❌ | ✅ | ✅ |
| `MT5Broker` | ❌ | ✅ (SL/TP de posición) | ❌ |

!!! info "Por qué `supports_oco` existe"
    Antes de este flag, un motor configurado en modo OCO contra Bitunix
    reventaba con `NotImplementedError` **en el instante del fill** — o sea,
    con la posición ya abierta y sin protección. El peor momento posible para
    descubrirlo. Con el flag, el motor lo sabe antes de colocar nada y gestiona
    TP+SL como dos órdenes normales.

## Mapa del paquete

```
luarix_exchange/
├── brokers/
│   ├── base.py                 # el contrato Broker
│   ├── factory.py              # BrokerFactory, AvailableBrokers
│   ├── credentials.py          # BrokerCredentials, load_dotenv
│   ├── binance.py              # BinanceBroker
│   ├── bitunix.py              # BitunixBroker
│   ├── bitunix_futures.py      # cliente HTTP de futuros (el SDK no lo cubre)
│   ├── bitunix_response.py     # unwrap(): Bitunix nunca responde HTTP de error
│   ├── paper.py                # PaperBroker + motor de simulación
│   ├── mt5.py                  # MT5Broker
│   ├── account_data/           # saldo, posiciones, órdenes  (firmado)
│   │   ├── base.py
│   │   ├── binance.py
│   │   └── bitunix.py
│   └── market_data/            # velas, precio, libro  (público)
│       ├── provider.py
│       ├── binance.py
│       ├── bitunix.py
│       ├── yahoo.py
│       └── live_feed.py        # suscripción a velas en vivo
└── models/
    ├── orders.py               # Side, OrderRequest, OrderAck, OrderInfo, Fill...
    ├── brokers.py              # BrokerPosition, BrokerType, Market
    └── market.py               # Candle
```

Los modelos de estrategia (`Signal`, `Portfolio`, `RiskResult`, `BacktestStats`,
`calc_quantity`...) **no** están aquí: viven en `luarix-trading-framework`.

## Por qué el paquete se llama `luarix_exchange`

Instalar `brokers` y `core` como paquetes top-level en `site-packages` es
pedir una colisión de nombres: cualquier otra librería que se llame igual gana
o pierde según el orden de `sys.path`.

No es hipotético — ya mordió a `luarix-orchestator`, donde una carpeta `grpc/`
en la raíz del repo tapaba a la librería `grpcio` instalada. De ahí un único
paquete raíz (`luarix_exchange/`), con `brokers/` y `models/` como subpaquetes.

## Dependencias

La instalación base trae **solo `pandas`**. Cada broker real es un extra
opcional y `BrokerFactory` los importa perezosamente, así que un servicio que
solo corre backtests contra el simulador no instala ningún SDK de exchange.

Este paquete **no depende de nadie del monorepo**. Ni de `luarix-orchestator`,
ni de ningún backend, ni de `pandas-luarix` (su hermano, que cubre indicadores
y Smart Money Concepts). La flecha va siempre hacia acá.
