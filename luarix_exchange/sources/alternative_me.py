"""alternative.me: el índice Crypto Fear & Greed (diario, sin clave)."""

from __future__ import annotations

from datetime import UTC, datetime

from luarix_exchange.models.global_market import FearGreed
from luarix_exchange.sources.base import SentimentSource

API = "https://api.alternative.me/fng/"


def fear_greed_from_alternative_me(row: dict) -> FearGreed:
    """Una lectura de `/fng/`: `value` y `timestamp` vienen como texto."""
    return FearGreed(
        value=int(row["value"]),
        classification=row.get("value_classification", ""),
        time=datetime.fromtimestamp(int(row["timestamp"]), tz=UTC),
    )


class AlternativeMeSource(SentimentSource):
    name = "alternative_me"
    label = "alternative.me"

    def get_fear_greed(self, days: int = 30) -> list[FearGreed]:
        payload = self._get(API, limit=max(1, days))
        return [fear_greed_from_alternative_me(row) for row in payload.get("data", [])]
