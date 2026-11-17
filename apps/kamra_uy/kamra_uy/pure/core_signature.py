"""La firma X-HR-Signature de los avisos que manda el servicio de cobros (HR-30), sin frappe.

`X-HR-Signature: t=<segundos Unix>,v1=<hex>`, con `v1 = HMAC-SHA256(secreto, "<t>." + cuerpo crudo)`. Un aviso vale si
su `t` está a menos de `TOLERANCE_SECONDS` del reloj de quien lo recibe, a cada lado. La comparación es en tiempo
constante.
"""

from __future__ import annotations

import hashlib
import hmac
import re

TOLERANCE_SECONDS = 300

_HEADER = re.compile(r"t=(\d{1,12}),v1=([0-9a-f]{64})")


def sign(secret: str, t: int, body: bytes) -> str:
    """El `v1` de un cuerpo crudo firmado en el instante `t`, en hex."""
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def verify(
    secret: str, header: str | None, body: bytes, now: int, tolerance: int = TOLERANCE_SECONDS
) -> tuple[bool, str]:
    """(True, "ok") o (False, "malformed" | "stale" | "mismatch")."""
    match = _HEADER.fullmatch(header or "")
    if not match:
        return False, "malformed"
    t = int(match.group(1))
    if abs(now - t) > tolerance:
        return False, "stale"
    if hmac.compare_digest(sign(secret, t, body), match.group(2)):
        return True, "ok"
    return False, "mismatch"
