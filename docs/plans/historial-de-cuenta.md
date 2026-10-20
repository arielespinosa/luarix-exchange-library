# Historial de cuenta: lo que necesita el portafolio de Luarix

Creado el 2026-10-19. Estado vivo: marcar cada paso al cerrarlo, con fecha.

Plan global (contrato, backend, cliente):
`luarix-web-server/docs/plans/portfolio-dashboard.md`. Aquí solo la parte de la
librería.

## Por qué

La página `/portfolio` del cliente muestra, por cuenta de broker: equity,
disponible, PnL realizado y no realizado, depósitos, retiros, comisiones y una
curva de equity. Hoy la librería solo sabe leer el **estado actual**:

- `Account.get_balance` devuelve solo el disponible (Binance descarta `locked`
  y el `balance` de futuros; Bitunix solo `available`; MT5 mete `balance` bajo
  la clave "USDT").
- Nadie expone equity total, aunque los tres brokers ya la devuelven en
  respuestas que llamamos y tiramos (`futures_account_balance` de Binance,
  `account()` de Bitunix futuros, `account_info()` de MT5).
- No hay ningún método de historial: ni fills, ni depósitos, ni retiros, ni
  income/funding, ni snapshot diario.

## Contrato nuevo

Modelos en `models/account.py` (frozen, `raw` como el resto):

- `AssetBalance`: `asset`, `market`, `free`, `locked`, `total`.
- `AccountSummary`: `market`, `equity`, `wallet_balance`, `available`,
  `unrealized_pnl`, `margin_used`, `balances: list[AssetBalance]`.
- `FundingMovement`: `id`, `kind` (`deposit`/`withdrawal`), `asset`, `amount`
  (siempre > 0, el sentido va en `kind`), `network`, `tx_id`, `status`
  (`completed`/`pending`/`failed`), `timestamp`.
- `TradeFill`: los campos de `Fill` + `market`, `realized_pnl` (si el exchange
  lo da), `is_maker`.
- `IncomeEntry`: `kind` (`realized_pnl`/`funding_fee`/`commission`/`transfer`),
  `asset`, `amount` (con signo), `symbol`, `timestamp`.

Capacidad nueva `ACCOUNT_HISTORY` con su ABC `AccountHistory` en
`capabilities/trading.py`:

```python
def get_account_summary(self, market: str = "spot") -> AccountSummary: ...
def get_deposits(self, start: datetime, end: datetime) -> list[FundingMovement]: ...
def get_withdrawals(self, start: datetime, end: datetime) -> list[FundingMovement]: ...
def get_trade_history(self, start: datetime, end: datetime, market: str = "spot",
                      symbol: str | None = None) -> list[TradeFill]: ...
def get_income(self, start: datetime, end: datetime, market: str = "futures") -> list[IncomeEntry]: ...
```

Reglas de siempre: lo que el exchange no tiene **levanta** `NotImplementedError`
(el backend lo traduce a "no disponible"), nunca lista vacía. Cantidades
positivas con el sentido en un campo aparte. Las ventanas máximas por petición
de cada exchange se paginan dentro del adaptador, no en el consumidor.

`get_account_summary` va aparte del resto (podría ir en `Account`) porque es lo
primero que se implementa y desbloquea la equity real sin esperar al historial.

## Pasos

### 1. Resumen de cuenta y saldo correcto ✅ 2026-10-19

- [x] `AccountSummary` + `get_account_summary` (capacidad `ACCOUNT_SUMMARY`)
      en Binance (spot `get_account` + futuros `futures_account`, precios con
      `get_all_tickers`) y Bitunix (spot `/user/account` + futuros `account()`,
      precios con `/common/rate/list`). Spot + futuros, libre + bloqueado,
      cada activo en USDT y totales. Un mercado caído queda en `errors`.
- [x] `get_balance` se queda como está (saldo disponible). Decidido con el
      usuario: el portafolio necesita spot + futuros valorado en USDT, y eso
      es `get_account_summary`; el nodo Balance sigue igual.
- [ ] MT5 (`account_info()`) y Paper: no aplican al portafolio por ahora
      (el backend no construye MT5 y el portafolio es solo dinero real).

### 2. Depósitos y retiros (Binance ✅ 2026-10-19)

- [x] Binance: `get_deposit_history` / `get_withdraw_history` (ventana de 90
      días por petición → tramos). `/sapi` no existe en testnet: con
      `testnet=True` levanta.
- [ ] MT5: `history_deals_get(from, to)`, deals `DEAL_TYPE_BALANCE` (+ depósito,
      − retiro).
- [ ] Bitunix: **verificar contra la API real** si la OpenAPI tiene algún
      endpoint de depósitos/retiros. Si no, levanta y queda documentado en
      `docs/brokers/bitunix.md`.

### 3. Fills e income

- [ ] Binance spot: `get_my_trades` exige símbolo → el adaptador recibe la
      lista de símbolos a consultar (los del usuario con saldo o con
      estrategias), no intenta adivinarla.
- [x] Binance futuros: `futures_income_history` (`get_income`, tramos de 7
      días + paginación por 1000). ✅ 2026-10-19. `futures_account_trades`
      (fills) pendiente: el portafolio usa income para el realizado.
- [ ] Bitunix futuros: historial de trades y de posiciones cerradas (por
      verificar rutas exactas contra la API real).
- [ ] MT5: deals de trading con `profit`, `commission`, `swap`, `fee`.
- [ ] Paper: exponer `state.filled_orders`.

### 4. Snapshot diario (opcional)

- [ ] Binance `get_account_snapshot` (30 días hacia atrás) para que el backend
      pueda rellenar la curva de equity el primer día en vez de empezar vacía.

## Tests y docs

- Fakes con payloads reales en `tests/test_broker_contract.py` (clases nuevas
  `ResumenCuentaTests`, `MovimientosTests`, `HistorialTests`), fila en
  `tests/test_capabilities.py::BrokerMatrixTests`.
- `docs/referencia/capacidades.md` (la matriz), una guía nueva
  `docs/guia/historial-de-cuenta.md` en el nav de `mkdocs.yml`, cada
  `docs/brokers/<x>.md` y `docs/estado.md` (cada endpoint verificado o no).
- Release: subir el SHA en `luarix-web-server` (y en el framework/orquestador
  si lo usan).
