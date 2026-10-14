# Binance

`luarix_exchange.brokers.binance.BinanceBroker` — spot y USDT-M Futures.

```bash
pip install "luarix-exchange-library[binance]"
```

## Capacidades

| Flag | Valor |
|---|---|
| `supports_futures` | ✅ |
| `supports_oco` | ✅ **nativo** |
| `supports_independent_orders` | ✅ |

## Construcción

```python
broker = BrokerFactory.get_broker(
    AvailableBrokers.BINANCE,
    testnet=True,
    test_order=False,
    credentials=BrokerCredentials(
        api_key=..., api_secret=...,
        test_api_key=..., test_api_secret=...,
    ),
)
```

O directamente:

```python
from luarix_exchange.brokers.binance import BinanceBroker

broker = BinanceBroker(
    api_key="...", api_secret="...",
    testnet=True,
    test_order=True,
    trading_mode="paper",
    allow_live_trading=False,
)
```

### Los tres modos

| | Qué hace |
|---|---|
| `trading_mode="paper"` | Simula localmente, sin tocar Binance |
| `testnet=True` | `testnet.binance.vision`, dinero ficticio |
| `trading_mode="live"` + `allow_live_trading=True` | Dinero real |

!!! danger "`test_order=True` no coloca nada"
    Capa **adicional** a las anteriores. Con `True`, `place_order` llama a
    `create_test_order`: valida contra la API y devuelve `status=NEW` sin fills
    y **sin colocar nada**. Es el valor por defecto de la factory.

    No aplica al camino de futuros.

### Testnet

Claves **distintas** de las de producción, de
[testnet.binance.vision](https://testnet.binance.vision). En el `.env`:
`BINANCE_TEST_API_KEY` / `BINANCE_TEST_API_SECRET` (alias histórico:
`TEST_BINANCE_API_KEY`).

---

## Endpoints usados

| Operación | Spot | Futuros |
|---|---|---|
| Velas | `get_klines` | `futures_klines` |
| Histórico | `get_historical_klines` | `futures_historical_klines` |
| Precio | `get_symbol_ticker` | `futures_symbol_ticker` |
| Libro | `get_order_book` | `futures_order_book` |
| Saldo | `get_account` → `free` | `futures_account_balance` → `availableBalance` |
| Posiciones | (saldos) | `futures_position_information` |
| Órdenes abiertas | `get_open_orders` | `futures_get_open_orders` |
| Detalle | `get_order` | `futures_get_order` |
| Colocar | `create_order` / `create_test_order` | `futures_create_order` |
| Modificar | — (cancelar+recolocar) | `futures_modify_order` |
| Cancelar | `cancel_order` | `futures_cancel_order` |
| Cancelar todas | `cancel_all_open_orders` | `futures_cancel_all_open_orders` |
| OCO | `create_oco_order` | — |
| Apalancamiento | — | `futures_change_leverage` |
| Filtros | `get_symbol_info` | `futures_exchange_info` |
| Pares | `get_exchange_info` | — |

---

## Precisión

Binance rechaza órdenes con más decimales de los permitidos. El broker consulta
las reglas y redondea **hacia abajo**, con aritmética de `Decimal` para no
arrastrar el error de coma flotante:

```python
_round_to_step(0.12345, "0.001")   # 0.123, nunca 0.124
```

| Filtro | Qué limita | Cacheado |
|---|---|---|
| `LOT_SIZE` → `step_size` | Cantidad | ✅ por símbolo |
| `PRICE_FILTER` → `tick_size` | Precio | ✅ |
| `MIN_NOTIONAL` / `NOTIONAL` | Valor mínimo | ✅ |

!!! danger "Los filtros de spot NO valen para futuros"
    El mismo par tiene `stepSize` y `tickSize` **distintos** en cada mercado.
    Usar los de spot en una orden de futuros da un `-1111` de precisión
    imposible de diagnosticar desde el log. Por eso hay dos métodos:
    `_get_symbol_filters` (spot, cacheado) y `_futures_symbol_filters`.

    El de futuros **no está cacheado**: hace una llamada a
    `futures_exchange_info()` por orden.

---

## Errores al colocar

`place_order` **captura** `BinanceAPIException` y `BinanceOrderException` y
devuelve un ack rechazado en vez de propagarlas:

```python
ack, fills = broker.place_order(orden)
if ack.status == OrderStatus.REJECTED:
    print(ack.raw["error"])
```

En el resto de métodos se propagan.

---

## OCO nativo

Binance es el **único** de los dos con OCO de exchange. Ver
[Take profit y stop loss](../guia/tp-sl.md#oco-binance-spot).

```python
oco_id, tp_ack, sl_ack = broker.place_oco_order(
    symbol="BTCUSDT", side=Side.SELL, quantity=0.01,
    take_profit_price=52000, stop_price=49000, stop_limit_price=48900,
)
```

Los IDs de cliente se generan con prefijos `tb-tp-` y `tb-sl-` para poder
identificar cada pata en el historial de Binance.

!!! note "No hay endpoint de test para OCO"
    Con `test_order=True`, `place_oco_order` **simula** las confirmaciones
    (`oco_list_id="1"`, dos acks con `status=NEW` y `raw={"test_order": True}`).
    No llega nada al exchange.

---

## Futuros

!!! warning "Implementado pero SIN VALIDAR contra la API real"
    La cuenta disponible tiene **restricción geográfica** sobre los endpoints de
    futuros de Binance. La forma de las llamadas sale de la documentación de
    USDT-M Futures y de la firma de `python-binance`, no de una respuesta real.

    Los nombres son deliberadamente los de la API (`futures_*`, `reduceOnly`,
    `closePosition`) para que el contraste sea directo.

### TP/SL en futuros

Binance **no** admite adjuntarlos a la entrada. Se colocan acto seguido como
órdenes propias:

```
LIMIT               SELL qty=0.083 price=45.30 timeInForce=GTC
TAKE_PROFIT_MARKET  BUY  stopPrice=45.07 closePosition=true
STOP_MARKET         BUY  stopPrice=45.75 closePosition=true
```

`closePosition="true"` cierra la posición **entera** al dispararse y, por
construcción, **no puede abrir nada** — la garantía que faltaba en el
equivalente de Bitunix.

!!! danger "Modo cobertura: pendiente"
    En hedge, Binance exige `positionSide` en vez de `reduceOnly`, y este broker
    **todavía no lee el modo de la cuenta**. Hay que resolverlo antes de operar
    en hedge.

### Posiciones

`futures_position_information` devuelve **todos** los pares con los que la
cuenta ha operado alguna vez, la mayoría con `positionAmt = 0`. El paquete los
filtra.

`positionAmt` viene **firmado**: negativo = corto. En ONE_WAY, `positionSide`
dice `"BOTH"`, así que el signo es la única fuente de la dirección. Ver
[`BrokerPosition`](../conceptos/modelos.md#brokerposition).

### Modificar

`futures_modify_order` exige `side`, `quantity` y `price` **siempre**, aunque
solo se toque uno. El broker lee antes la orden y rellena lo que no se pasó — si
no, cambiar el precio borraría la cantidad.

---

## Datos de mercado en vivo

`WebSocketBinanceMarketDataProvider` (en `market_data/binance.py`) da velas por
websocket con `ThreadedWebsocketManager`.

```python
from luarix_exchange.brokers.market_data.binance import WebSocketBinanceMarketDataProvider

ws = WebSocketBinanceMarketDataProvider(api_key, api_secret, "BTCUSDT", "1m")
ws.on_kline = lambda msg: print(msg)
ws.start()
```

!!! warning "El stream de USUARIO está comentado"
    `start_user_socket` está comentado en el código. Consecuencia: **Binance no
    empuja cambios de estado de órdenes** en este paquete. Hay que preguntar con
    `get_order_status()`.

    Es el motivo de que exista todo el aparato de reconciliación por sondeo.

---

## Limitaciones conocidas

- Todo el camino de **futuros sin validar** contra la API real.
- **Modo cobertura** sin soporte (`positionSide`).
- `_futures_symbol_filters` **no cachea**: una llamada por orden.
- `start_user_socket` comentado: sin push de estado de órdenes.
- `update_order` en **spot** cancela y recoloca → el `order_id` cambia y no es
  atómico.
- `test_order=True` no aplica a futuros.
