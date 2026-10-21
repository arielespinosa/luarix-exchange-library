# Bitfinex

```bash
pip install "luarix-exchange-library[bitfinex]"
```

Solo **spot**. Portado de luarix-arbitrage-web-server, donde está en producción,
y completado con velas para Luarix.

```python
bitfinex = ExchangeFactory.create("bitfinex")
bitfinex.get_candles("tBTCUST", "1h", limit=200)
bitfinex.get_historical_candles("tBTCUST", "1h", start=..., end=...)   # pagina (10.000 por llamada)
bitfinex.get_order_book("tBTCUST", 25)
```

| Capacidad | |
|---|---|
| `MARKET_DATA` | velas, precio, libro (`P0`), pares |
| `SYMBOL_RULES` | `pub:info:pair`; sin tick fijo (`tick_size = 0`, 5 cifras significativas) |
| `SERVER_CLOCK` | estimado con las marcas del WebSocket (no hay endpoint de hora) |
| `FEES` | `auth/r/summary` (cripto / stable / fiat) |
| `ASSET_INFO` | deducido de los pares (no publica el estado sin claves) |
| `ORDER_BOOK_STREAM` | canal `book` con flags TIMESTAMP y CHECKSUM |
| `CANDLE_STREAM` | canal `candles` como disparador + REST + temporizador |

## Particularidades

- **Símbolos** nativos: `tBTCUST`, `tAVAX:BTC`. USDT se llama `UST` y USDC
  `UDC`; las reglas traducen base/quote a nombres comunes.
- ⚠ **Columnas de las velas**: `[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]`, con el
  cierre ANTES que el máximo.
- **Profundidad de libro**: 1, 25 o 100 niveles.
- Bitfinex limita las conexiones WebSocket por minuto: muchas reconexiones
  seguidas pueden dejar una medición de reloj sin muestras (se conserva la
  anterior, con aviso).
- **Sin cuenta ni órdenes** todavía.
