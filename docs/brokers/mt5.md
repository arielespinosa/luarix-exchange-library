# MetaTrader 5

`luarix_exchange.brokers.mt5.MT5Broker` — conecta con un terminal MetaTrader 5
instalado, vía el paquete oficial `MetaTrader5`.

```bash
pip install "luarix-exchange-library[mt5]"
```

!!! warning "Solo Windows"
    `MetaTrader5` solo publica ruedas para Windows. Por eso queda **fuera** del
    extra `all`, que tiene que poder instalarse en Linux (CI, Docker).

!!! info "Soporte parcial y sin mantenimiento activo"
    Es el broker menos desarrollado del paquete. El foco del proyecto son
    Binance y Bitunix. Lo que hay funciona; lo que no está, levanta.

## Requisitos

- Terminal MT5 **instalado**.
- Sesión **iniciada** en la cuenta.
- **Algo Trading habilitado** (`Ctrl+E` en el terminal).

## Construcción

No se construye por la factory: **no cabe en su firma**. No usa api_key/secret
sino login/password/servidor.

```python
from luarix_exchange.brokers.mt5 import MT5Broker

broker = MT5Broker(
    login=12345678,
    password="...",
    server="MiBroker-Demo",
    path=None,              # ruta al terminal.exe; None = autodetectar
    magic=123456,           # identificador de las órdenes de este sistema
    lot_multiplier=1.0,     # cantidad -> lotes
)
broker.connect()
```

`magic` es el "magic number" de MT5: marca las órdenes colocadas por este
sistema para poder distinguirlas de las que el usuario ponga a mano en el
terminal.

## Capacidades

| Flag | Valor |
|---|---|
| `supports_futures` | ❌ (no aplica: MT5 no separa spot de derivados) |
| `supports_oco` | ✅ vía SL/TP de la posición |
| `supports_independent_orders` | ❌ (`update_order` ni siquiera modifica) |

!!! info "`supports_oco` estaba mal declarado"
    Los tres flags estaban **sin declarar**, o sea heredando `False` de
    `Broker`. Pero `place_oco_order` de esta clase **sí funciona**: pone el SL y
    el TP sobre la posición y es el servidor de MT5 quien los gestiona — una
    sola llamada con exclusividad garantizada, que es justo lo que el flag
    promete, aunque MT5 no lo llame "OCO".

    Con los dos flags en `False`, un motor que los consultara para decidir cómo
    proteger una posición se quedaba **sin ningún camino válido** contra este
    broker. Corregido.

`market` se ignora en todos los métodos: MT5 expone **una única cuenta** y cada
instrumento es lo que es.

## Qué implementa

| Operación | Notas |
|---|---|
| `connect` | `mt5.initialize` + `mt5.login` |
| `list_active` | `mt5.symbols_get()` |
| `get_ticker` | Punto medio `(ask + bid) / 2` |
| `get_balance(market, asset)` | Una sola cuenta, expuesta como `{"USDT": balance}` |
| `get_positions(market, symbol)` | `mt5.positions_get()` → `list[BrokerPosition]` |
| `place_order` | |
| `cancel_order` | |
| `place_oco_order` | SL/TP sobre la **posición**, no dos órdenes |
| `get_order_book` | Devuelve `{}` |

### Posiciones

`pos.type` es `0` para compra y `1` para venta — MT5 no usa texto. Se traduce a
`"LONG"`/`"SHORT"`. `pos.ticket` va en `position_id`, y `sl`/`tp`/`magic` quedan
en `raw`:

```python
for p in broker.get_positions():
    print(p.symbol, p.side, p.quantity, p.entry_price, p.unrealized_pnl)
    print("  SL:", p.raw["sl"], "TP:", p.raw["tp"])
```

### OCO = SL/TP de la posición

MT5 no tiene OCO como dos órdenes hermanas: tiene un **stop-loss y un
take-profit asociados a la posición**, que el servidor gestiona. `place_oco_order`
busca la posición abierta del símbolo y le pone los dos niveles.

Si no hay posición, devuelve acks rechazados con `raw={"error": "no position"}`.

## Qué NO implementa

| Operación | Qué pasa |
|---|---|
| `get_open_orders` | `NotImplementedError` (sin `account_data_provider`) |
| `cancel_open_orders` | Ídem |
| `get_order` / `get_order_status` | Ídem |
| `get_candles` y compañía | `NotImplementedError` (sin `market_data_provider`) |
| `set_leverage` | Ídem |
| `update_order` **con argumentos** | `NotImplementedError` |

### `update_order` no modifica: cancela

```python
broker.update_order("EURUSD", ticket)                    # cancela
broker.update_order("EURUSD", ticket, price=1.09)        # NotImplementedError
```

MT5 **sí** tiene `TRADE_ACTION_MODIFY`, pero este broker nunca lo implementó.
Devolver un ack de cancelación como si fuera una modificación sería mentir sobre
lo que pasó, así que con argumentos levanta:

```
NotImplementedError: MT5Broker.update_order todavía no sabe modificar una orden
(haría falta `TRADE_ACTION_MODIFY`). Sin argumentos, cancela.
```

## Para usarlo con velas

Necesita un `MarketDataProvider`. MT5 no trae uno en este paquete, así que hay
que asignárselo a mano o usar otro broker como fuente de datos:

```python
from binance import Client
from luarix_exchange.brokers.market_data.binance import BinanceMarketDataProvider

mt5_broker.market_data_provider = BinanceMarketDataProvider(Client())
```

Solo tiene sentido si los símbolos se corresponden, lo cual rara vez es el caso
entre un bróker de CFD y un exchange de cripto.

## Si quisieras completarlo

Lo que falta, por orden de utilidad:

1. Un `MT5MarketDataProvider` sobre `mt5.copy_rates_from_pos` /
   `copy_rates_range`.
2. Un `MT5AccountDataProvider` sobre `mt5.orders_get` / `mt5.history_orders_get`
   → desbloquea `get_open_orders`, `get_order` y `get_order_status` heredados.
3. `update_order` real con `TRADE_ACTION_MODIFY`.
4. Decidir si `get_balance` debe seguir devolviendo la clave literal `"USDT"` o
   la moneda real de la cuenta (`account_info().currency`) — hoy es lo primero,
   que en una cuenta en euros es engañoso.
