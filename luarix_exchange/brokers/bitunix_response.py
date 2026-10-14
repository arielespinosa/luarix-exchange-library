"""Desempaquetado de las respuestas de Bitunix.

Bitunix NUNCA responde con un HTTP de error: cuando rechaza una llamada
devuelve 200 con `{"code": "100011", "msg": "Trigger IP risk control rules",
"data": null, "success": false}`. El envoltorio es el mismo tanto si salió
bien como si no; lo único que cambia es `code` y que `data` viene en `null`.

Todos los wrappers de este broker hacían `raw.get("data", <default>)`, que
NO cubre ese caso: el default de `.get()` solo aplica cuando la clave FALTA,
y acá está presente con valor null. Resultado: `data` quedaba en `None` y
cada llamador reventaba más adelante con un error sin relación con la causa
(`'NoneType' object is not iterable` al pedir el balance) o, peor, seguía
adelante con datos vacíos como si nada -- `place_order` construía un
`OrderAck` con `order_id=""` en lugar de fallar, o sea una orden fantasma
que el sistema daba por colocada.

`unwrap` centraliza el criterio y hace visible el motivo real.
"""

from __future__ import annotations

from typing import Any

_RAISE = object()

# El único código de éxito observado en las respuestas reales. Se compara en
# texto porque Bitunix lo manda como int en las respuestas buenas (`0`) y
# como string en las malas (`"100011"`).
_SUCCESS_CODE = "0"


class BitunixApiError(RuntimeError):
    """Bitunix rechazó la llamada. El mensaje lleva su `code` y su `msg`
    tal cual los devolvió, que es lo que hace falta para diagnosticar."""


def unwrap(raw: Any, action: str, *, on_null: Any = _RAISE) -> Any:
    """El payload útil de una respuesta de Bitunix, o `BitunixApiError`.

    `action` describe qué se estaba pidiendo, para que el mensaje de error
    sea legible ("consultar el balance", "colocar la orden"...).

    `on_null` es qué devolver cuando la llamada fue BIEN (`code` de éxito o
    ausente) pero `data` vino en null -- típicamente una lista vacía para un
    endpoint de consulta que no encontró nada. Sin `on_null`, ese caso
    también levanta excepción.

    Deliberadamente NO se levanta excepción por un `code` desconocido
    mientras venga `data` utilizable: no hay documentación fiable de todos
    los códigos de éxito de Bitunix, y tratar uno legítimo como error
    rompería llamadas que hoy funcionan. El corte se hace donde sí es
    inequívoco -- no hay datos con los que seguir.
    """
    if raw is None:
        raise BitunixApiError(f"Bitunix no devolvió nada al {action}.")

    # Algunos endpoints devuelven la carga directamente, sin envoltorio.
    if not isinstance(raw, dict):
        return raw
    if "data" not in raw:
        return raw

    code = raw.get("code")
    data = raw["data"]
    if data is not None:
        return data

    ok = code is None or str(code) == _SUCCESS_CODE
    if ok and on_null is not _RAISE:
        return on_null
    hints = {
        "100011": "IP no autorizada (whitelist) o credenciales incorrectas.",
        "100012": "Credenciales inválidas o API key desactivada.",
        "10004": (
            "IP de origen no autorizada para esta API key (la key SÍ es válida). "
            "La IP a poner en la whitelist es la que devuelve "
            "`curl https://fapi.bitunix.com/cdn-cgi/trace` (campo `ip=`), NO la de un "
            "'what is my IP' genérico -- pueden no coincidir."
        ),
        "10003": "API key inexistente o mal copiada (Bitunix no reconoce el token).",
    }
    hint = ""
    for c, h in hints.items():
        if str(code) == c:
            hint = f" {h}"
    if not hint and str(code) == "1":
        hint = " Error interno del servidor Bitunix (System Error)."
    raise BitunixApiError(
        f"Bitunix rechazó {action} — code={code!r} msg={raw.get('msg')!r}.{hint}"
    )
