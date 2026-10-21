# Datos de mercado: estadísticas de 24h, funding y fuentes públicas

Creado el 2026-10-21 (Fase 10 del roadmap).

## Por qué

La pantalla de Mercados de luarix-web-client necesita un resumen del mercado
y un screener con datos reales: qué subió y qué bajó, cuánto volumen se
movió, el funding de los perpetuos, la capitalización total y el sentimiento.
La cadena es: esta librería habla con exchanges y fuentes →
`luarix_trading.market` (luarix-trading-framework) calcula → el backend lo
expone con caché → el frontend lo pinta.

Requisito del usuario: **poder elegir el exchange**, y que sumar exchanges
no obligue a tocar la pantalla. Por eso todo va por capacidades: lo que
implementa `TICKER_STATS` aparece solo (`ExchangeFactory.find`).

## Qué hay

| Pieza | Dónde |
|---|---|
| `Ticker24h`, `FundingRate`, `pct_change`, `ms_to_datetime` | `models/market.py` |
| `TickerStats.get_tickers_24h(market)`, `FundingRates.get_funding_rates()` | `capabilities/market_data.py` |
| `Exchange.capability_markets`, `markets_for()`, `public()` | `capabilities/base.py` |
| Parseo puro por exchange (`ticker_from_*`, `funding_from_*`) | junto a cada exchange |
| `GlobalMarketSource` (CoinGecko), `SentimentSource` (alternative.me) | `sources/` |
| `GlobalMarket`, `AssetMarketCap`, `FearGreed` | `models/global_market.py` |

## Unidades (donde se equivoca un resumen sin que nada falle)

- `change_pct`: puntos porcentuales. Binance lo trae; Bitunix y Kraken no
  (se calcula con `open`/`last`); Bitfinex lo trae relativo (×100).
- `volume_quote`: Binance y Bitunix lo publican; Kraken se estima con el
  VWAP de 24h y Bitfinex con el último precio.
- `FundingRate.rate`: fracción por intervalo. Binance ya lo da así; Bitunix
  en porcentaje (se divide entre 100).
- Binance futuros: solo perpetuos (los trimestrales `BTCUSDT_270326`
  duplican al perpetuo y no tienen funding).

## Verificado contra las APIs reales (2026-10-21)

Binance spot 1387 pares / futuros 567, funding 567 · Bitunix futuros 784,
funding 784 · Kraken 1366 · Bitfinex 180 · CoinGecko global + top 100 ·
alternative.me. El spot de Bitunix no tiene endpoint de tickers en bloque
(probadas varias rutas: 404), por eso `TICKER_STATS` solo en futuros.

## Límites conocidos

- CoinGecko sin clave: unas decenas de peticiones por minuto; quien la use
  debe cachear (el backend cachea minutos).
- Open interest: fuera por ahora (en Binance va par a par).
