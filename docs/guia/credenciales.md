# Credenciales

Hay **dos escenarios** y el paquete los soporta de forma distinta a propósito.

| Escenario | De dónde salen las claves |
|---|---|
| **Producción** (varios usuarios) | El backend las pasa explícitas, una por usuario |
| **Local** (cuadernos, scripts) | Del entorno o del `.env` del repo |

`BrokerFactory` **no lee ninguna configuración global**. Eso es deliberado: un
servidor que atiende a varios usuarios no puede depender de unas claves
"ambiente", y este paquete tiene que poder instanciar varios brokers vivos a la
vez, uno por cuenta.

---

## `BrokerCredentials`

`luarix_exchange.brokers.credentials.BrokerCredentials` — inmutable.

```python
from luarix_exchange.brokers.credentials import BrokerCredentials

creds = BrokerCredentials(
    api_key="...",
    api_secret="...",
    test_api_key="...",       # opcional, solo si el broker tiene testnet
    test_api_secret="...",
    passphrase="",            # KuCoin/OKX. Ni Binance ni Bitunix la usan
)
```

| Campo | Por defecto |
|---|---|
| `api_key` | `""` |
| `api_secret` | `""` |
| `test_api_key` | `""` |
| `test_api_secret` | `""` |
| `passphrase` | `""` |

### `resolve(testnet)`

Devuelve la tupla `(api_key, api_secret)` que toca:

```python
creds.resolve(testnet=True)    # las de prueba, si las hay
creds.resolve(testnet=False)   # las de producción
```

!!! warning "Con `testnet=True` pero sin claves de prueba, cae a producción"
    No es una licencia: es por **Bitunix, que no tiene testnet en absoluto**.
    Antes, pedirle credenciales de prueba devolvía `("", "")` y la primera
    llamada fallaba con un `10003 API key inexistente` que no dice nada de la
    causa real. Y ocurría con el valor **por defecto** de la factory
    (`testnet=True`), o sea siempre que alguien no lo pensara.

### `has_testnet` y `bool()`

```python
creds.has_testnet     # ¿hay claves de prueba completas?

if not creds:         # ¿son utilizables en absoluto?
    ...
```

`__bool__` es `False` cuando no hay ningún par completo. Importa porque un
backend puede mandar un objeto **vacío** (todos los campos en `""`) para un
usuario que aún no ha conectado su cuenta: la factory lo detecta y cae al
`.env` en vez de mandar cadenas vacías al exchange.

```python
bool(BrokerCredentials())                          # False
bool(BrokerCredentials(api_key="solo_la_key"))     # False -- falta el secreto
bool(BrokerCredentials(api_key="k", api_secret="s"))  # True
```

---

## Producción: el backend pasa las claves

Este es el camino normal cuando hay varios usuarios.

```python
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory
from luarix_exchange.brokers.credentials import BrokerCredentials

def broker_para(cuenta):
    """`cuenta` es una fila de la base de datos del backend."""
    return BrokerFactory.get_broker(
        cuenta.broker_id,                    # "BINANCE" | "BITUNIX", en texto
        testnet=cuenta.testnet,
        credentials=BrokerCredentials(
            api_key=cuenta.api_key,          # ya descifradas
            api_secret=cuenta.api_secret,
            passphrase=cuenta.api_passphrase,
        ),
        trading_mode="live",
        allow_live_trading=cuenta.puede_operar_real,
    )
```

En `luarix-web-server` esas claves viven cifradas en reposo (Fernet,
`EncryptedCharField`) en el modelo `BrokerAccount`.

!!! danger "No loguees las credenciales"
    Ni enteras ni "solo para depurar". Si necesitas verificar cuál se está
    usando, imprime un prefijo:

    ```python
    print(f"key={creds.api_key[:6]}..." if creds.api_key else "(vacía)")
    ```

    Los cuadernos guardan su salida **dentro del `.ipynb`**, así que una clave
    impresa ahí viaja con el fichero a cualquier sitio donde lo abras o lo
    commitees.

---

## Local: el `.env`

```python
from luarix_exchange.brokers.credentials import BrokerCredentials

creds = BrokerCredentials.from_env("BITUNIX")
```

O directamente, sin pasar nada a la factory:

```python
broker = BrokerFactory.get_broker(AvailableBrokers.BITUNIX)  # lee del .env
```

### Variables que se leen

| Broker | Producción | Testnet |
|---|---|---|
| **Binance** | `BINANCE_API_KEY`, `BINANCE_API_SECRET` | `BINANCE_TEST_API_KEY`, `BINANCE_TEST_API_SECRET` |
| **Bitunix** | `BITUNIX_API_KEY`, `BITUNIX_API_SECRET` | — (no tiene) |

**Alias históricos aceptados**: `TEST_BINANCE_API_KEY` y
`TEST_BINANCE_API_SECRET`, que es como se llamaban en el `.env` viejo de este
repo. El nombre nuevo gana si están los dos.

### `from_env()` en detalle

```python
BrokerCredentials.from_env(
    broker,              # "BINANCE" | "BITUNIX"
    env=None,            # de dónde leer; por defecto os.environ tras cargar .env
    dotenv_path=None,    # un .env concreto; sin él, se busca hacia arriba
)
```

Devuelve un objeto posiblemente **vacío** si no hay nada configurado. No
levanta a propósito: quien decide si eso es un error es la factory, que puede
nombrar el broker en el mensaje.

Un broker desconocido devuelve vacío en vez de reventar:

```python
BrokerCredentials.from_env("KUCOIN")   # BrokerCredentials()  -- falsy
```

---

## `load_dotenv()`

```python
from luarix_exchange.brokers.credentials import load_dotenv

leidas = load_dotenv()                          # busca .env hacia arriba
leidas = load_dotenv("/ruta/al/.env")
leidas = load_dotenv(override=True)             # el fichero pisa el entorno
```

Devuelve un `dict` con lo que leyó, y además lo mete en `os.environ`.

**Está implementado a mano, ~20 líneas.** No usa `python-dotenv` porque la
instalación base del paquete solo trae `pandas`, y añadir una dependencia
obligatoria para leer un fichero de `CLAVE=valor` no lo justifica. Si
`python-dotenv` ya está instalado por otro motivo, los dos conviven sin
problema.

### `override=False` por defecto

Una variable **ya presente en el entorno gana** sobre el fichero. Es lo
correcto para producción: el `.env` es el apaño de desarrollo local, y lo que
inyecta el sistema (un contenedor, systemd, el backend) tiene que poder
pisarlo.

### Búsqueda del fichero

Sin `path`, busca un `.env` desde el directorio de trabajo **hacia arriba**.
Así funciona igual ejecutando desde la raíz del repo que desde `notebooks/`.

### Comillas

Las comillas del valor son sintaxis del fichero, no parte de la clave:

```ini
BITUNIX_API_KEY="abc"     # se lee como abc, no como "abc"
```

Importa porque una clave API entre comillas **firma mal**, y el error que
devuelve el exchange ("credenciales inválidas") no apunta a esto en absoluto.

---

## Comprobar que las credenciales funcionan

Haz **dos llamadas distintas**: una pública y una firmada.

```python
# Pública: funciona sin credenciales válidas
print(broker.get_ticker("BTCUSDT", market="futures"))

# Firmada: es la que falla si la key está mal o la IP no está en la whitelist
try:
    print(broker.get_balance(market="futures", asset="USDT"))
except Exception as e:
    print(type(e).__name__, ":", e)
```

Si la primera va y la segunda no, **el problema son las credenciales o la IP**,
no la conexión. Esa asimetría es la herramienta de diagnóstico más útil que hay
con Bitunix.

---

## Avisos específicos por broker

### Bitunix restringe las API keys por IP

Si la IP de la máquina no está en la whitelist de la key, **toda** llamada
firmada falla con `code=10004 Invalid IP`. Los klines siguen funcionando porque
son públicos.

La IP que hay que poner en la whitelist es la que devuelve:

```bash
curl https://fapi.bitunix.com/cdn-cgi/trace     # campo ip=
```

y **no** la de un "what is my IP" genérico — pueden no coincidir.

!!! warning "En un servidor esto rompe la reconciliación entera"
    Si la IP del backend no está en la whitelist, ninguna consulta de estado
    funciona. Los errores de red se tragan correctamente (un fallo de red no es
    un fill), pero entonces **nada se reconcilia nunca** y el sistema parece
    estar al día cuando no lo está.

### Bitunix no tiene testnet

Lo que se coloque con esas claves es **dinero real**. Para practicar sin
riesgo: Binance testnet o el [`PaperBroker`](../brokers/paper.md).

### Binance testnet usa claves distintas

No son las de producción con un interruptor. Se sacan de
[testnet.binance.vision](https://testnet.binance.vision) y van en
`BINANCE_TEST_API_KEY` / `BINANCE_TEST_API_SECRET`.
