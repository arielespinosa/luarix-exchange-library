"""luarix-exchange-library: todo lo que sabe hablar con un exchange o broker.

Libreria instalable e INDEPENDIENTE, compartida por dos familias de
proyectos que no se conocen entre si:

- luarix-trading-framework / luarix-orchestator / luarix-web-server
  (estrategias sobre velas, ordenes con TP/SL, backtesting con PaperBroker).
- luarix-arbitrage-web-server (arbitraje sobre libros de ordenes).

No importa nada de ninguno de ellos; la dependencia va siempre al reves.

Dos subpaquetes:

- `luarix_exchange.brokers` -- conectores concretos (Binance, Bitunix, MT5,
  Yahoo para datos) detras de la interfaz abstracta `brokers.base.Broker`,
  mas `brokers.paper.PaperBroker`, el broker simulado que implementa el
  mismo contrato sin tocar la red.
- `luarix_exchange.models` -- los modelos que esos brokers intercambian:
  velas, ordenes, fills, posiciones.

Toda dependencia de broker concreto es un extra opcional (ver
`pyproject.toml`): `pip install luarix-exchange-library[binance]`. La
instalacion base solo trae `pandas` -- suficiente para `PaperBroker` y los
modelos.
"""
