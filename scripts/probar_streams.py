"""Prueba REAL de los streams contra un exchange. NO coloca ninguna orden.

Escucha velas cerradas y libro del par, y con `--privado` también los cambios
de TUS órdenes: coloca o cancela una orden a mano desde la web del exchange
mientras corre y deberías verla llegar aquí.

Ejemplos:

    python scripts/probar_streams.py --exchange bitunix --market futures
    python scripts/probar_streams.py --exchange bitunix --market futures --privado --env ../luarix-trading-framework/.env
    python scripts/probar_streams.py --exchange binance --market spot --privado --testnet

Credenciales: de `--env` (o del `.env` que se encuentre hacia arriba), con las
variables de siempre (`BITUNIX_API_KEY`, `BINANCE_API_KEY`,
`BINANCE_TEST_API_KEY`...). Las claves nunca se imprimen.

Qué mirar en la salida:
- "WebSocket nativo" o "polling": qué camino eligió la librería.
- Velas: deben llegar 1-2 s después del cierre (con reloj bien sincronizado).
- Órdenes de Bitunix: comprueba que `lleno` es la CANTIDAD ejecutada (en BTC)
  y no el importe en USDT -- es lo único del canal que la documentación deja
  ambiguo (`dealAmount`). Si no cuadra, copia la línea `raw=` y avisa.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luarix_exchange.brokers.credentials import BrokerCredentials, load_dotenv  # noqa: E402
from luarix_exchange.capabilities import Capability, CandleSubscription  # noqa: E402
from luarix_exchange.factory import ExchangeFactory  # noqa: E402
from luarix_exchange.streaming import (  # noqa: E402
    StreamRunner,
    candle_stream,
    order_book_stream,
    order_update_stream,
)


def _hora() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _camino(broker, capacidad: Capability, market: str) -> str:
    return "WebSocket nativo" if type(broker).has_native(capacidad, market) else "polling (REST)"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exchange", choices=["binance", "bitunix"], required=True)
    p.add_argument("--market", choices=["spot", "futures"], default="futures")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--depth", type=int, default=5)
    p.add_argument("--seconds", type=int, default=150)
    p.add_argument("--privado", action="store_true", help="escuchar también tus órdenes (necesita claves)")
    p.add_argument("--testnet", action="store_true", help="solo Binance: usar testnet")
    p.add_argument("--env", help="ruta del .env con las claves")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    load_dotenv(args.env)
    creds = BrokerCredentials.from_env(args.exchange.upper(), dotenv_path=args.env)
    if args.privado and not creds:
        print(f"Faltan claves de {args.exchange} en el .env para --privado.")
        return 2
    opciones = {"testnet": args.testnet, "test_order": True, "trading_mode": "testnet"}
    if not creds:  # datos públicos: no hacen falta claves reales
        creds = BrokerCredentials(api_key="publico", api_secret="publico")
    broker = ExchangeFactory.create(args.exchange, credentials=creds, **opciones)

    m = args.market
    print(f"{args.exchange} {m} {args.symbol} -- {args.seconds}s")
    print(f"  velas:   {_camino(broker, Capability.CANDLE_STREAM, m)}")
    print(f"  libro:   {_camino(broker, Capability.ORDER_BOOK_STREAM, m)}")
    if args.privado:
        print(f"  órdenes: {_camino(broker, Capability.ORDER_UPDATE_STREAM, m)}")
    print("  (Ctrl+C para terminar antes)\n")

    libros = {"n": 0, "ultimo": None}
    lock = threading.Lock()

    def on_candle(v):
        print(f"[{_hora()}] VELA cerrada {v.symbol} {v.timeframe} {v.open_time:%H:%M} "
              f"O={v.open} H={v.high} L={v.low} C={v.close} V={v.volume}")

    def on_book(libro):
        with lock:
            libros["n"] += 1
            libros["ultimo"] = libro

    def on_update(u):
        print(f"[{_hora()}] ORDEN {u.symbol} id={u.order_id} {u.status.value} lado={u.side and u.side.value} "
              f"lleno={u.filled_qty} este_llenado={u.last_fill_qty}@{u.last_fill_price} medio={u.average_price} "
              f"fee={u.fee} {u.fee_asset or ''}\n           raw={u.raw}")

    with StreamRunner() as runner:
        handles = [
            runner.start(lambda stop: candle_stream(broker, market=m).stream_candles(
                [CandleSubscription(args.symbol, args.interval, m)], on_candle, stop), "velas"),
            runner.start(lambda stop: order_book_stream(broker, market=m).stream_order_books(
                [args.symbol], args.depth, on_book, stop, m), "libro"),
        ]
        if args.privado:
            handles.append(runner.start(lambda stop: order_update_stream(broker, symbols=[args.symbol], market=m)
                                        .stream_order_updates(on_update, stop, m), "órdenes"))
        inicio = time.time()
        try:
            while time.time() - inicio < args.seconds:
                time.sleep(10)
                with lock:
                    ultimo = libros["ultimo"]
                    resumen = (f"{libros['n']} libros; mejor bid={ultimo.best_bid[0]} ask={ultimo.best_ask[0]}"
                               if ultimo else "sin libros todavía")
                print(f"[{_hora()}] {resumen}")
                for h in handles:
                    if h.exception() is not None:
                        print(f"[{_hora()}] ERROR en {h.label}: {type(h.exception()).__name__}: {h.exception()}")
                if all(not h.running for h in handles):
                    break
        except KeyboardInterrupt:
            pass
    fallos = [h for h in handles if h.exception() is not None]
    print("\nResultado:", "OK" if not fallos else f"{len(fallos)} stream(s) con error")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
