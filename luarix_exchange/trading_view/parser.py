"""Parser/validador puro del webhook de TradingView.

`TradingViewWebhookParser` transforma el cuerpo de un webhook de TradingView
(JSON) en un `TradingViewSignal` normalizado y verifica el secreto contra un
hash que le pasa el backend. No guarda estado ni secretos, no abre conexiones
ni importa Django: se instancia en el receptor de webhook del backend (y en
tests) y se tira.

Por que el secreto va en el payload: TradingView entrega las alertas por HTTP
POST y NO permite configurar cabeceras personalizadas ni firma HMAC, asi que
el unico sitio donde el emisor puede poner un secreto compartido es el propio
cuerpo del mensaje. El parser lo extrae, lo compara en tiempo constante y lo
quita del `raw` para que nunca acabe persistido en claro.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .contract import SCHEMA_VERSION, Direction, EventType, TradingViewSignal
from .errors import TradingViewPayloadError

# Eventos de operacion: exigen al menos un simbolo para ser utiles.
_SYMBOL_REQUIRED = {
    EventType.ENTRY,
    EventType.EXIT,
    EventType.PARTIAL_EXIT,
    EventType.STOP_LOSS,
    EventType.TAKE_PROFIT,
    EventType.UPDATE_SL,
    EventType.UPDATE_TP,
}


class TradingViewWebhookParser:
    """Parsea, normaliza y valida el cuerpo de un webhook de TradingView."""

    #: Campo del payload que transporta el secreto compartido.
    SECRET_FIELD = "secret"

    # ------------------------------------------------------------------ secreto

    @staticmethod
    def generate_secret(nbytes: int = 32) -> str:
        """Genera un secreto nuevo, largo y aleatorio (url-safe), para la
        instancia. El backend guarda solo su hash (`hash_secret`) y muestra
        este valor en claro una unica vez."""
        import secrets

        return secrets.token_urlsafe(nbytes)

    @staticmethod
    def hash_secret(secret: str) -> str:
        """Hash SHA-256 (hex) del secreto. Al ser un token de alta entropia no
        necesita un KDF lento; basta un hash para no guardarlo en claro."""
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def verify_secret(self, raw_body: str | bytes | dict, expected_secret_hash: str) -> bool:
        """True si el secreto del payload coincide con `expected_secret_hash`.

        Comparacion en tiempo constante. Secreto ausente o hash vacio -> False
        (el backend lo traduce en 401). No lanza: el que decide la respuesta es
        el consumidor.
        """
        if not expected_secret_hash:
            return False
        try:
            payload = self._load(raw_body)
        except TradingViewPayloadError:
            return False
        provided = payload.get(self.SECRET_FIELD)
        if not isinstance(provided, str) or not provided:
            return False
        return hmac.compare_digest(self.hash_secret(provided), expected_secret_hash)

    # ------------------------------------------------------------------- parseo

    def parse(self, raw_body: str | bytes | dict, *, require_fields: bool = True) -> TradingViewSignal:
        """Convierte el cuerpo del webhook en un `TradingViewSignal`.

        Lanza `TradingViewPayloadError` si el JSON es invalido o faltan campos
        obligatorios (`event`, `event_id`; y `symbol` en eventos de operacion,
        `direction` en ENTRY cuando `require_fields`).
        """
        payload = self._load(raw_body)

        event = self._event(payload.get("event"))
        event_id = self._req_str(payload, "event_id")

        direction = self._direction(payload.get("direction"))
        symbol = self._opt_upper(payload.get("symbol"))

        if require_fields:
            if event in _SYMBOL_REQUIRED and not symbol:
                raise TradingViewPayloadError(f"Falta 'symbol' para el evento {event.value}")
            if event is EventType.ENTRY and direction is None:
                raise TradingViewPayloadError("Falta 'direction' para el evento ENTRY")

        position = payload.get("position")
        if position is not None and not isinstance(position, dict):
            raise TradingViewPayloadError("'position' debe ser un objeto")

        # `raw` conserva el payload completo salvo el secreto (nunca se persiste).
        raw = {k: v for k, v in payload.items() if k != self.SECRET_FIELD}

        return TradingViewSignal(
            schema_version=self._opt_int(payload.get("schema_version")) or SCHEMA_VERSION,
            event=event,
            event_id=event_id,
            sequence=self._opt_int(payload.get("sequence")),
            strategy_id=self._opt_str(payload.get("strategy_id")),
            instance_id=self._opt_str(payload.get("instance_id")),
            trade_id=self._opt_str(payload.get("trade_id")),
            symbol=symbol,
            timeframe=self._opt_str(payload.get("timeframe")),
            direction=direction,
            entry_price=self._opt_decimal(payload.get("entry_price")),
            stop_loss=self._opt_decimal(payload.get("stop_loss")),
            take_profit=self._opt_decimal(payload.get("take_profit")),
            reason=self._opt_str(payload.get("reason")),
            position=position,
            timestamp=self._opt_ts(payload.get("timestamp")),
            raw=raw,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _load(raw_body: str | bytes | dict) -> dict:
        if isinstance(raw_body, dict):
            return raw_body
        if isinstance(raw_body, (bytes, bytearray)):
            raw_body = raw_body.decode("utf-8", errors="replace")
        if not isinstance(raw_body, str):
            raise TradingViewPayloadError("El cuerpo del webhook no es texto ni objeto JSON")
        try:
            data = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise TradingViewPayloadError(f"JSON invalido: {exc}") from exc
        if not isinstance(data, dict):
            raise TradingViewPayloadError("El cuerpo del webhook debe ser un objeto JSON")
        return data

    @staticmethod
    def _event(value: Any) -> EventType:
        if not isinstance(value, str) or not value.strip():
            raise TradingViewPayloadError("Falta el campo 'event'")
        try:
            return EventType(value.strip().upper())
        except ValueError as exc:
            raise TradingViewPayloadError(f"Tipo de evento desconocido: {value!r}") from exc

    @staticmethod
    def _direction(value: Any) -> Direction | None:
        if value is None or value == "":
            return None
        try:
            return Direction(str(value).strip().upper())
        except ValueError as exc:
            raise TradingViewPayloadError(f"Direccion desconocida: {value!r}") from exc

    @staticmethod
    def _req_str(payload: dict, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise TradingViewPayloadError(f"Falta el campo '{key}'")
        return value.strip()

    @staticmethod
    def _opt_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _opt_upper(value: Any) -> str | None:
        text = TradingViewWebhookParser._opt_str(value)
        return text.upper() if text else None

    @staticmethod
    def _opt_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (ValueError, TypeError) as exc:
            raise TradingViewPayloadError(f"Entero invalido: {value!r}") from exc

    @staticmethod
    def _opt_decimal(value: Any) -> Decimal | None:
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise TradingViewPayloadError(f"Numero invalido: {value!r}") from exc

    @staticmethod
    def _opt_ts(value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        # epoch (segundos o ms) ----------------------------------------------
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        text = str(value).strip()
        if text.isdigit():
            seconds = int(text)
            seconds = seconds / 1000 if seconds > 1e12 else seconds
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        # ISO 8601 (acepta sufijo 'Z') ---------------------------------------
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TradingViewPayloadError(f"Timestamp invalido: {value!r}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
