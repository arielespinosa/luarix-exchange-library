"""`BitunixClient` del paquete `bitunix` sin sus `print` de depuración.

El `_post_authenticated` del SDK imprime en stdout el cuerpo, las cabeceras
-- con la `api-key` y la firma -- y la respuesta de CADA petición firmada
(colocar, cancelar, historial...). En `run_live_market_feeds` eso deja la
API key del usuario en la consola y en cualquier log que la capture. Esta
subclase repite la misma petición sin imprimir nada; el resto del cliente
no se toca.
"""
import json
import time
from typing import Any, Dict

from bitunix import BitunixClient


class QuietBitunixClient(BitunixClient):
    def _post_authenticated(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        nonce = self._generate_nonce()
        headers = {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "nonce": nonce,
            "Content-Type": "application/json",
        }
        # Mismo JSON compacto y ordenado que firma el SDK: si cambia un byte,
        # la firma deja de coincidir.
        body_string = json.dumps(data, separators=(",", ":"), sort_keys=True)
        headers["sign"] = self.sign_request(nonce, timestamp, body=body_string)

        response = self.session.post(f"{self.BASE_URL}{endpoint}", data=body_string, headers=headers)
        response.raise_for_status()
        return response.json()
