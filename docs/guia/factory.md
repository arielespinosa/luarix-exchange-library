# BrokerFactory

El único punto que sabe construir un `Broker` listo para usar.

```python
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory
from luarix_exchange.brokers.credentials import BrokerCredentials

broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    credentials=BrokerCredentials(api_key="...", api_secret="..."),
)
```

## Firma completa

```python
BrokerFactory.get_broker(
    broker_name,                  # AvailableBrokers | str
    testnet=True,                 # bool | None
    test_order=True,              # bool   -- ⚠ solo Binance, y NO coloca nada
    credentials=None,             # BrokerCredentials | None
    trading_mode="testnet",       # "paper" | "testnet" | "live"
    allow_live_trading=False,     # bool
) -> Broker
```

### `broker_name`

Acepta el enum o su nombre en texto (mayúsculas o minúsculas), que es como
llega desde una API o una fila de base de datos:

```python
BrokerFactory.get_broker(AvailableBrokers.BINANCE)
BrokerFactory.get_broker("BINANCE")
BrokerFactory.get_broker("binance")
BrokerFactory.get_broker(cuenta.broker_id)      # desde el backend
```

Un nombre desconocido **levanta `ValueError`** nombrando los disponibles:

```pycon
>>> BrokerFactory.get_broker("KUCOIN")
ValueError: No existe el broker 'KUCOIN'. Disponibles: PAPER, BINANCE, BITUNIX.
```

!!! info "Antes devolvía `None` en silencio"
    `AvailableBrokers` incluía `KUCOIN` y `MT5` **sin implementación detrás**
    (`case ...: pass`), y el `case _: pass` final tragaba cualquier nombre
    desconocido. Quien recibiera ese `None` reventaba mucho después con un
    `AttributeError` que no mencionaba KuCoin por ningún lado.

### `testnet`

Si `True`, conecta a la red de prueba (dinero ficticio).

| Broker | Efecto |
|---|---|
| **Binance** | Usa `testnet.binance.vision` y las claves `test_*` |
| **Bitunix** | **Se ignora.** No tiene testnet: siempre usa las de producción |
| **Paper** | Irrelevante: no hay red |

### `test_order`

!!! danger "`test_order=True` no coloca nada — y es el valor por defecto"
    Solo aplica a **Binance spot**. Con `True`, `place_order` llama a
    `create_test_order`: valida la orden contra la API y devuelve `status=NEW`
    **sin fills y sin colocar nada** en el mercado.

    Si esperas ver una orden en el exchange y no aparece, mira esto primero.
    Para colocar de verdad: `test_order=False`.

### `trading_mode` y `allow_live_trading`

Candado **doble** contra ejecuciones accidentales con dinero real. Para colocar
en real hacen falta **los dos**:

```python
broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    trading_mode="live",
    allow_live_trading=True,      # ← sin esto, place_order levanta
)
```

Sin el segundo:

```pycon
>>> broker.place_order(orden)
RuntimeError: Live trading is blocked. Set ALLOW_LIVE_TRADING=true to enable.
```

!!! info "Este candado faltaba en Bitunix"
    Existía **solo en Binance**. Bitunix — el único de los dos con el que se
    opera de verdad, y sin testnet donde equivocarse gratis — colocaba siempre.
    Añadido en la revisión de 2026-08-11.

### `credentials`

Si se pasan, se usan. Si no (o si llegan **vacías**), se leen del entorno y del
`.env`. Si tampoco hay ahí, levanta:

```pycon
>>> BrokerFactory.get_broker(AvailableBrokers.BITUNIX)
ValueError: No hay credenciales para BITUNIX. Pásalas con
`credentials=BrokerCredentials(api_key=..., api_secret=...)`, o define
BITUNIX_API_KEY y BITUNIX_API_SECRET en el entorno o en el .env.
```

Detalle completo en [Credenciales](credenciales.md).

---

## `AvailableBrokers`

```python
class AvailableBrokers(Enum):
    PAPER = "PAPER"
    BINANCE = "BINANCE"
    BITUNIX = "BITUNIX"
```

Solo los que existen de verdad.

!!! note "`MT5` no está, y no es un olvido"
    `MT5Broker` **no cabe en esta firma**: no se conecta con api_key/secret sino
    con login/password/servidor de un terminal MetaTrader instalado. Se
    construye directamente:

    ```python
    from luarix_exchange.brokers.mt5 import MT5Broker
    broker = MT5Broker(login=12345, password="...", server="Broker-Demo")
    ```

---

## Imports perezosos

La factory importa cada broker **dentro del método**, no arriba del módulo:

```python
if broker is AvailableBrokers.BINANCE:
    from luarix_exchange.brokers.binance import BinanceBroker
    ...
```

No es estilo. Cada broker real arrastra su propio SDK y **ninguno es
obligatorio** (son extras de `pyproject.toml`). Importándolos arriba, una
instalación base — o una que solo tenga el extra `[bitunix]` — no podía ni
importar esta factory para pedir un `PaperBroker`.

---

## Recetas

### Desarrollo local, sin riesgo

```python
broker = BrokerFactory.get_broker(AvailableBrokers.PAPER)
```

Ni credenciales ni red. Ver [PaperBroker](../brokers/paper.md).

### Binance testnet

```python
broker = BrokerFactory.get_broker(
    AvailableBrokers.BINANCE,
    testnet=True,
    test_order=False,           # ← si no, no coloca nada ni en testnet
    trading_mode="testnet",
)
```

### Bitunix real, solo lectura

Para inspeccionar la cuenta sin poder colocar por accidente:

```python
broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    trading_mode="live",
    allow_live_trading=False,   # ← place_order levanta; las consultas van
)
```

### Bitunix real, operando

```python
broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    trading_mode="live",
    allow_live_trading=True,
    credentials=BrokerCredentials(api_key=..., api_secret=...),
)
```

### Multi-usuario en un backend

```python
def broker_de(cuenta):
    return BrokerFactory.get_broker(
        cuenta.broker_id,
        testnet=cuenta.testnet,
        test_order=False,
        credentials=BrokerCredentials(
            api_key=cuenta.api_key,
            api_secret=cuenta.api_secret,
            passphrase=cuenta.api_passphrase,
        ),
        trading_mode="live" if cuenta.es_real else "paper",
        allow_live_trading=cuenta.es_real,
    )
```

La factory no guarda estado global, así que se pueden tener tantos brokers
vivos como usuarios.

!!! tip "Cada instancia abre su propio cliente HTTP"
    Construir un broker por petición HTTP funciona, pero desperdicia conexiones.
    Si el patrón de uso lo permite, cachéalos por `(usuario, broker, cuenta)`
    mientras dure la operación.
