"""Las reglas del enlace de pago y de los avisos del servicio de cobros (HR-30), sin frappe.

Kamra pide el enlace (`POST /v1/payments/links`) y recibe tres avisos firmados: `payment.approved`, `payment.refunded` y
`payment.charged_back`. Aquí está lo que no depende de frappe: para qué es el enlace, cuándo caduca, el cuerpo de la
petición y la lectura de un aviso.
"""

from __future__ import annotations

import hashlib
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


# --- el cobro con tarjeta y el botón del saldo que Kamra abre a la web (HR-222) --------------------------------------
# Contrato de core: KICKOFF-HR-220 §7 (HR-221). La web nunca manda importe: lo pone el enlace de core.

CARD_CODES = ("hold_expired", "link_not_payable", "card_not_enabled", "too_many_attempts", "unavailable", "invalid")
CARD_STATUSES = ("approved", "pending", "in_process", "rejected")
_KEPT_409 = ("link_not_payable", "card_not_enabled", "too_many_attempts")
_LATER = ("upstream_unavailable", "idempotency_in_progress", "not_ready")
# Los largos de `CardPaymentRequest` en core: lo que core rechazaría se rechaza aquí, sin preguntarle.
TOKEN_MAX = 256
METHOD_MAX = 40
ISSUER_MAX = 40
ID_TYPE_MAX = 20
ID_NUMBER_MAX = 30
DEVICE_MAX = 200


def amount_left(advance_due, advance_paid) -> float:
    """Lo que falta de lo que se paga al reservar (con «Full online», el total), redondeado al céntimo."""
    left = Decimal(str(advance_due or 0)) - Decimal(str(advance_paid or 0))
    return float(left.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def card_idempotency_key(link_id: str, card_token: str) -> str:
    """La Idempotency-Key de un cobro con tarjeta: sha256 de "<link_id>:<token>". El mismo token, el mismo cobro."""
    return hashlib.sha256(f"{link_id}:{card_token}".encode()).hexdigest()


def wallet_idempotency_key(link_id: str, return_url: str | None) -> str:
    """La del botón del saldo: sha256 de "wallet:<link_id>:<return_url>", la vuelta que va en el cuerpo ("" sin ella).

    Core compara el cuerpo con el de la clave: la misma clave con otra vuelta (el huésped cambió de idioma) le daría
    409 `idempotency_conflict` y el botón se moriría para ese enlace (HR-238). Con la vuelta en la clave, otra vuelta
    es otra clave, y core devuelve igual la preferencia que guardó la primera vez: una por enlace."""
    return hashlib.sha256(f"wallet:{link_id}:{return_url or ''}".encode()).hexdigest()


def _text(value, name: str, maximum: int, required: bool = False) -> str:
    """Un campo de texto recortado. El mensaje de error nombra el campo, nunca su valor (un token, un documento)."""
    text = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    if required and not text:
        raise ValueError(f"falta {name}")
    if len(text) > maximum:
        raise ValueError(f"{name} es demasiado largo")
    return text


def card_payment_request(
    *,
    card_token: str,
    payment_method_id: str,
    issuer_id: str,
    payer_email: str,
    identification_type: str,
    identification_number: str,
    device_id: str,
) -> dict:
    """El cuerpo de `POST /v1/payments/links/{link_id}/card-payments`, sin importe. ValueError si core lo rechazaría."""
    body = {"token": _text(card_token, "el token de la tarjeta", TOKEN_MAX, required=True),
            "payment_method_id": _text(payment_method_id, "el medio de pago", METHOD_MAX, required=True)}
    issuer = _text(issuer_id, "el emisor", ISSUER_MAX)
    if issuer:
        body["issuer_id"] = issuer
    email = _text(payer_email, "el correo", EMAIL_MAX, required=True)
    if not _EMAIL.fullmatch(email):
        raise ValueError("el correo no es válido")
    payer = {"email": email}
    kind = _text(identification_type, "el tipo de documento", ID_TYPE_MAX)
    number = _text(identification_number, "el documento", ID_NUMBER_MAX)
    if kind and number:
        payer["identification"] = {"type": kind, "number": number}
    body["payer"] = payer
    device = _text(device_id, "la huella del dispositivo", DEVICE_MAX)
    if device:
        body["device_id"] = device
    return body


def card_code(status: int | None, code: str) -> str:
    """El código para la web de un error de core: el de su 409, «unavailable» si puede ir más tarde, si no «invalid».

    `not_configured` (Kamra sin `hr_core_base_url`, o los ajustes sin clave) es una avería, no «sin tarjeta»: da
    «unavailable», que deja su Error Log. «Esta casa no cobra con tarjeta» es el 409 `card_not_enabled` de core."""
    if code == "not_configured":
        return "unavailable"
    if status == 409 and code in _KEPT_409:
        return code
    if status is None or status >= 500 or status == 429 or code in _LATER:
        return "unavailable"
    return "invalid"


def core_error_is_logged(status: int | None, code: str) -> bool:
    """Si un error de core deja Error Log: lo que recepción tiene que ver, no lo que la web le dice al huésped.

    Una avería («unavailable») y el 409 `idempotency_conflict`: la misma Idempotency-Key con otro cuerpo es un fallo
    de Kamra, no del huésped (la web lo da como «invalid», pero si llega hay que verlo; HR-238)."""
    return card_code(status, code) == "unavailable" or (status == 409 and code == "idempotency_conflict")


def card_result(reply: dict) -> dict:
    """Lo que la web ve de la respuesta de core: estado, detalle, id del pago (el de core y el de Mercado Pago, que el
    Status Screen Brick pide para el desafío 3DS) y el desafío; nada más."""
    status = reply.get("status") if isinstance(reply, dict) else None
    if status not in CARD_STATUSES:
        return {"status": "error", "status_detail": None, "payment_id": None, "provider_payment_id": None,
                "three_ds": None, "code": "unavailable"}
    three_ds = reply.get("three_ds")
    return {"status": status, "status_detail": reply.get("status_detail"), "payment_id": reply.get("payment_id"),
            "provider_payment_id": reply.get("provider_payment_id"),
            "three_ds": three_ds if isinstance(three_ds, dict) else None, "code": None}
