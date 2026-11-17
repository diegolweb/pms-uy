"""Las reglas del enlace de pago y de los avisos del servicio de cobros (HR-30), sin frappe.

Kamra pide el enlace (`POST /v1/payments/links`) y recibe tres avisos firmados: `payment.approved`, `payment.refunded` y
`payment.charged_back`. Aquí está lo que no depende de frappe: para qué es el enlace, cuándo caduca, el cuerpo de la
petición y la lectura de un aviso.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PRE_ARRIVAL = ("Confirmed", "Pending Payment", "Held", "Waitlist")
IN_HOUSE = ("Checked In", "Checked Out")
PURPOSES = ("deposit", "balance", "other")
CURRENCIES = ("UYU", "USD")
DEFAULT_MODE = "Payment Link"

# lo que acepta el servicio de cobros como caducidad propia; fuera de eso pone la suya (72 h)
MIN_EXPIRY = timedelta(minutes=5)
MAX_EXPIRY = timedelta(days=30)

PAYER_NAME_MAX = 150
EMAIL_MAX = 254

EVENT_TYPES = ("payment.approved", "payment.refunded", "payment.charged_back")
_COMMON = ("event_id", "type", "property", "folio", "payment_id", "amount", "currency")
_BY_TYPE = {
    "payment.approved": ("link_id",),
    "payment.refunded": ("refund_id", "refunded_total"),
    "payment.charged_back": ("charged_back_at",),
}

_MONEY = re.compile(r"\d+(\.\d{1,2})?")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def link_purpose(reservation_status: str | None, folio_has_charges: bool) -> str:
    """"other" sin reserva; "balance" si el folio tiene cargos (también una cancelación con penalización) o el huésped
    ya llegó; "deposit" antes de la llegada y sin cargos; "other" en lo demás (cancelada o no-show sin cargos)."""
    if not reservation_status:
        return "other"
    if folio_has_charges or reservation_status in IN_HOUSE:
        return "balance"
    if reservation_status in PRE_ARRIVAL:
        return "deposit"
    return "other"


def link_expires_at(now: datetime, hold_expires_on: datetime | None) -> datetime | None:
    """La caducidad de la retención si cae entre ahora + 5 min y ahora + 30 días; si no, None (la pone el servicio)."""
    if hold_expires_on is None:
        return None
    if now + MIN_EXPIRY <= hold_expires_on <= now + MAX_EXPIRY:
        return hold_expires_on
    return None


def money_text(amount) -> str:
    """Un importe positivo con dos decimales, en cadena ("125.50"). ValueError si no es positivo."""
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"importe no válido: {amount!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"el importe tiene que ser mayor que cero: {amount!r}")
    return f"{value:.2f}"


def _instant(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("la caducidad tiene que llevar zona horaria")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def link_request(
    *,
    property_code: str,
    folio: str,
    reservation: str | None,
    amount,
    currency: str | None,
    purpose: str,
    guest_name: str | None,
    guest_email: str | None,
    return_url: str,
    expires_at: datetime | None,
) -> dict:
    """El cuerpo de `POST /v1/payments/links`."""
    code = (currency or "").strip().upper()
    if code not in CURRENCIES:
        raise ValueError(f"moneda no admitida: {currency!r} (solo UYU o USD)")
    if purpose not in PURPOSES:
        raise ValueError(f"motivo no válido: {purpose!r}")
    name = (guest_name or "").strip()[:PAYER_NAME_MAX].strip()
    if not name:
        raise ValueError("el pagador necesita un nombre")
    payer = {"name": name}
    email = (guest_email or "").strip()
    if email and len(email) <= EMAIL_MAX and _EMAIL.fullmatch(email):
        payer["email"] = email
    body = {"property": property_code, "folio": folio}
    if reservation:
        body["reservation"] = reservation
    body.update(amount=money_text(amount), currency=code, purpose=purpose, payer=payer, return_url=return_url)
    if expires_at is not None:
        body["expires_at"] = _instant(expires_at)
    return body


def amount_to_float(value: str) -> float:
    """El importe de un aviso ("125.50") como número. ValueError si no es dinero positivo."""
    if not isinstance(value, str) or not _MONEY.fullmatch(value) or Decimal(value) <= 0:
        raise ValueError(f"importe no válido: {value!r}")
    return float(Decimal(value))


def parse_core_event(body: bytes) -> dict:
    """Un aviso del servicio de cobros, con su forma comprobada. ValueError si no es uno de los tres o le falta algo."""
    try:
        event = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("el aviso no es JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("el aviso no es un objeto JSON")
    kind = event.get("type")
    if kind not in EVENT_TYPES:
        raise ValueError(f"tipo de aviso no admitido: {kind!r}")
    for field in (*_COMMON, *_BY_TYPE[kind]):
        value = event.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"al aviso le falta {field}")
    amount_to_float(event["amount"])
    if kind == "payment.refunded":
        amount_to_float(event["refunded_total"])
    if event["currency"] not in CURRENCIES:
        raise ValueError(f"moneda no admitida: {event['currency']!r}")
    return event


def payment_mode(event: dict) -> str:
    """El modo de pago de Kamra que trae el aviso, o "Payment Link" si no trae ninguno."""
    mode = event.get("mode")
    return mode if isinstance(mode, str) and mode.strip() else DEFAULT_MODE
