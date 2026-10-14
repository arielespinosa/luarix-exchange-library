# Inicio rápido

De cero a leer una vela, un saldo y colocar una orden. Cinco minutos.

## 1. Instalar

```bash
pip install -e ".[binance,bitunix]"
```

## 2. Credenciales

Copia `.env.example` a `.env` y rellena las del broker que vayas a usar:

```ini
BITUNIX_API_KEY=tu_key
BITUNIX_API_SECRET=tu_secret
```

!!! warning "Bitunix no tiene testnet"
    Esas claves operan con **dinero real**. Si quieres practicar sin riesgo,
    usa Binance testnet (`BINANCE_TEST_API_KEY`, claves de
    [testnet.binance.vision](https://testnet.binance.vision)) o el
    [`PaperBroker`](brokers/paper.md).

## 3. Instanciar el broker

```python
from luarix_exchange.brokers.factory import AvailableBrokers, BrokerFactory

broker = BrokerFactory.get_broker(AvailableBrokers.BITUNIX)
```

Sin `credentials=`, la factory las lee del `.env`. Para pasarlas explícitas
(lo que hará el backend, una por usuario):

```python
from luarix_exchange.brokers.credentials import BrokerCredentials

broker = BrokerFactory.get_broker(
    AvailableBrokers.BITUNIX,
    credentials=BrokerCredentials(api_key="...", api_secret="..."),
)
```

## 4. Leer datos de mercado

Esto es **público**: funciona aunque las credenciales estén mal. Úsalo para
comprobar que hay conexión antes de sospechar de las claves.

```python
# La última vela cerrada
vela = broker.get_last_candle("BTCUSDT", "1h", market="futures")
print(f"{vela.open_time:%Y-%m-%d %H:%M}  O={vela.open} H={vela.high} "
      f"L={vela.low} C={vela.close}")

# Las 500 últimas, ascendentes por tiempo
velas = broker.get_candles("BTCUSDT", "1h", limit=500, market="futures")

# El último precio
precio = broker.get_ticker("BTCUSDT", market="futures")
```

→ [Guía completa de velas](guia/velas.md)

## 5. Leer la cuenta

Esto va **firmado**: si falla aquí pero el paso 4 funcionó, el problema son
las credenciales o la whitelist de IP.

```python
# Todo el saldo disponible
broker.get_balance(market="futures")
# {'USDT': 1500.0}

# Filtrado por activo
broker.get_balance(market="futures", asset="USDT")
# {'USDT': 1500.0}

# Posiciones abiertas
for p in broker.get_positions(market="futures"):
    print(f"{p.symbol} {p.side} qty={p.quantity} @ {p.entry_price} "
          f"pnl={p.unrealized_pnl}")
```

→ [Guía de balance y posiciones](guia/balance-y-posiciones.md)

## 6. Colocar una orden

```python
from luarix_exchange.models.orders import OrderRequest, OrderType
from luarix_exchange.models.orders import Side

precio = broker.get_ticker("BTCUSDT", market="futures")

orden = OrderRequest(
    symbol="BTCUSDT",
    side=Side.BUY,
    quantity=0.001,
    order_type=OrderType.LIMIT,
    price=round(precio * 0.98, 2),
    market="futures",
    sl_price=round(precio * 0.95, 2),   # stop-loss ADJUNTO a la entrada
    tp_price=round(precio * 1.05, 2),   # take-profit ADJUNTO
)

ack, fills = broker.place_order(orden)
print(ack.exchange_order_id, ack.status)
```

`sl_price` y `tp_price` viajan **dentro** de la orden en Bitunix futuros. Es
atómico: la posición nunca llega a existir sin protección. Es la parte más
delicada del paquete y tiene su propia página →
[Take profit y stop loss](guia/tp-sl.md).

## 7. Consultar, editar, cancelar

```python
order_id = ack.exchange_order_id

# Detalle completo (funciona también con órdenes ya cerradas)
info = broker.get_order("BTCUSDT", order_id, market="futures")
print(info.status, info.filled_quantity, "de", info.quantity)

# Editar: lo que no se pasa se conserva
ack2 = broker.update_order("BTCUSDT", order_id, price=nuevo, market="futures")
order_id = ack2.exchange_order_id      # ⚠ en spot el id CAMBIA

# Cancelar
broker.cancel_order("BTCUSDT", order_id, market="futures")
```

→ [Guía de órdenes](guia/ordenes.md)

## 8. Probarlo sin riesgo

Todo lo anterior funciona igual contra el simulador, sin red y sin dinero:

```python
from luarix_exchange.brokers.paper import PaperBroker

broker = PaperBroker(initial_balance=10_000)
```

→ [PaperBroker](brokers/paper.md)

## Cuadernos

`notebooks/` tiene un Jupyter por bloque de operaciones, listos para ejecutar:

| | |
|---|---|
| `00_setup_y_credenciales.ipynb` | Distingue "no hay conexión" de "las credenciales o la IP están mal" |
| `01_velas.ipynb` | Última vela, listado, rango, spot vs futuros, huecos |
| `02_saldo_y_posiciones.ipynb` | Saldo, filtros, las dos bolsas, posiciones |
| `03_ordenes.ipynb` | Ciclo completo. **Las celdas que colocan están comentadas** |

## Errores más frecuentes al empezar

| Síntoma | Causa casi segura |
|---|---|
| La orden "se coloca" pero no aparece en el exchange | `test_order=True` (el **default** de la factory) en Binance |
| `10004 Invalid IP` en todo lo firmado, pero los precios van | La IP no está en la whitelist de la API key de Bitunix |
| `10003 API key inexistente` | Pediste testnet a Bitunix, que no la tiene |
| `ValueError: No hay credenciales para BITUNIX` | Ni `credentials=` ni variables en el `.env` |
| `NotImplementedError: PaperBroker no expone datos de cuenta` | Le pediste al simulador algo que solo tiene un broker con cuenta remota |

Todos, con el diagnóstico completo, en
[Errores y diagnóstico](guia/errores.md).
