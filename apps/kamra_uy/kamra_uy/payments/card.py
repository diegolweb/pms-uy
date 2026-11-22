"""La tarjeta y el botón del saldo de Mercado Pago de una reserva web retenida, por su token de estado (HR-222).

La web del hotel (HR-220) habla solo con Kamra; Kamra no habla con Mercado Pago: pide a core (HR-221) el cobro con el
token de la tarjeta que crea el formulario de Mercado Pago en el navegador, o la preferencia que solo admite el saldo.
Las tres son públicas (sin sesión, solo POST, con su `rate_limit`) y se apoyan en lo que la reserva web ya tiene
(HR-31, `kamra.public_api`): el token de estado, la retención y el enlace del folio. El enlace es el de siempre, así que
la confirmación, el aviso firmado, la conciliación, el pago tardío y la devolución desde el folio valen igual.

- El importe lo pone el servidor: lo que falta de lo que se paga al reservar (`advance_due − advance_paid`; con
  «Full online», el total), y core cobra el del enlace. `pay` no tiene parámetro de importe.
- Devuelven códigos, no textos (`kamra_uy.pure.payments.CARD_CODES`): la web los traduce.
- Ni el token de la tarjeta ni el documento salen en un Error Log: se registra el estado y el código de core, y un fallo
  inesperado con su traza sin variables y con esos dos valores tachados.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import get_datetime, get_system_timezone

from kamra.public_api import _deposit_link, _guest_folio, _hold_is_live, _res_by_status_token, _return_url
from kamra_uy.core.client import CoreError
from kamra_uy.payments import mercadopago
from kamra_uy.pure.payments import (
	amount_left,
	card_code,
	card_idempotency_key,
	card_payment_request,
	card_result,
	core_error_is_logged,
	wallet_idempotency_key,
)

LOG_TITLE = "kamra_uy: tarjeta y saldo de la reserva web"
REDACTED = "[redactado]"


class _Refused(Exception):
	def __init__(self, code: str):
		super().__init__(code)
		self.code = code


def _zoned(value) -> str | None:
	"""Una fecha guardada (en la zona del sitio, sin zona) en ISO 8601 con su desfase, como la da `booking_status`."""
	if not value:
		return None
	return get_datetime(value).replace(microsecond=0, tzinfo=ZoneInfo(get_system_timezone())).isoformat()


def _settings(property: str):
	"""Los Payment Gateway Settings «Mercado Pago» activos de la propiedad, o None."""
	name = frappe.db.get_value("Payment Gateway Settings",
	                           {"property": property, "enabled": 1, "gateway": mercadopago.GATEWAY})
	return frappe.get_doc("Payment Gateway Settings", name) if name else None


def _currency(res) -> str:
	from kamra.localization import default_currency, pack_for

	return frappe.get_cached_value("Property", res.property, "currency") or default_currency(pack_for(res.property))


def _log(res, link_id: str | None, what: str, secrets: tuple[str, ...] = ()) -> None:
	"""El Error Log de un fallo, sin variables de los marcos y con los valores de la tarjeta tachados."""
	message = f"reserva {res.name}, enlace {link_id or '-'}: {what}"
	for secret in secrets:
		if secret:
			message = message.replace(secret, REDACTED)
	frappe.log_error(title=LOG_TITLE, message=message, reference_doctype="Reservation", reference_name=res.name)


def _payable(res, return_url: str | None) -> tuple[str, object]:
	"""(link_id, settings) del enlace del folio por lo que falta pagar; lo pide si el del folio es de otro importe.

	_Refused con el código si no falta nada, la retención venció o la propiedad no cobra con Mercado Pago. Sin llamar a
	core en ninguno de esos casos."""
	# primero lo que falta: una reserva ya pagada (el doble clic tras el aprobado) no está retenida, pero no «venció»
	left = amount_left(res.advance_due, res.advance_paid)
	if left <= 0:
		raise _Refused("link_not_payable")
	if not _hold_is_live(res):
		raise _Refused("hold_expired")
	settings = _settings(res.property)
	if settings is None:
		raise _Refused("card_not_enabled")
	folio = _guest_folio(res.name)
	link_id = None
	if folio and folio.payment_link_url and abs(float(folio.payment_link_amount or 0) - left) < 0.005:
		link_id = frappe.db.get_value("Folio", folio.name, "payment_link_id")
	if not link_id:
		try:
			_deposit_link(res.name, left, return_url)
		except frappe.ValidationError:  # el enlace no se pudo pedir (core caído, la pasarela sin claves)
			_log(res, None, "no se pudo pedir el enlace del folio")
			raise _Refused("unavailable") from None
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- el enlace, en el folio antes de cobrarlo
		folio = _guest_folio(res.name)
		link_id = frappe.db.get_value("Folio", folio.name, "payment_link_id") if folio else None
		if not link_id:
			_log(res, None, "el folio no guardó el enlace")
			raise _Refused("unavailable")
	return link_id, settings


def _core_failed(res, link_id: str, error: CoreError) -> str:
	"""Lo que la web le dice al huésped de un error de core; deja Error Log de lo que recepción tiene que ver (una
	avería, o el 409 `idempotency_conflict`, HR-238). El mensaje lleva el estado y el código, nunca la tarjeta."""
	if core_error_is_logged(error.status, error.code):
		_log(res, link_id, f"core respondió {error.status or 'sin respuesta'} {error.code}")
	return card_code(error.status, error.code)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=240, seconds=3600)
def checkout(token: str) -> dict:
	"""Lo que el paso de pago necesita: si hay tarjeta (y su `public_key` de Mercado Pago), el importe, la moneda y
	hasta cuándo se guarda la habitación. `code` solo si no se puede pagar (hold_expired, link_not_payable,
	unavailable); sin tarjeta, `card` es False y la web ofrece el pago en Mercado Pago."""
	res = _res_by_status_token(token)
	live = _hold_is_live(res)
	out = {"card": False, "public_key": None, "amount": max(amount_left(res.advance_due, res.advance_paid), 0.0),
	       "currency": _currency(res), "hold_expires_on": _zoned(res.hold_expires_on) if live else None, "code": None}
	try:
		link_id, settings = _payable(res, None)
	except _Refused as refused:
		return out if refused.code == "card_not_enabled" else {**out, "code": refused.code}
	try:
		link = mercadopago.link_card(settings, link_id)
	except CoreError as error:
		code = _core_failed(res, link_id, error)
		return out if code == "card_not_enabled" else {**out, "code": "unavailable" if code == "invalid" else code}
	key = (link.get("card") or {}).get("public_key") if isinstance(link.get("card"), dict) else None
	return {**out, "card": bool(key), "public_key": key or None}


def _failed(code: str) -> dict:
	return {"status": "error", "status_detail": None, "payment_id": None, "provider_payment_id": None, "three_ds": None,
	        "code": code}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=3600)
def pay(token: str, card_token: str, payment_method_id: str, issuer_id: str = "", payer_email: str = "",
        identification_type: str = "", identification_number: str = "", device_id: str = "") -> dict:
	"""Cobra con el token de la tarjeta lo que falta de la reserva, por el enlace de su folio. Sin importe: lo pone core.

	{"status": approved | pending | in_process | rejected | error, "status_detail", "payment_id",
	"provider_payment_id" (el de Mercado Pago, para el Status Screen Brick), "three_ds", "code"}.
	Con desafío del banco, `three_ds` trae `external_resource_url` y `creq`. La Idempotency-Key es el sha256 de
	"<link_id>:<token>": el mismo token, el mismo cobro."""
	res = _res_by_status_token(token)
	secrets = (card_token, identification_number)
	link_id = None
	try:
		link_id, settings = _payable(res, None)
		try:
			body = card_payment_request(
				card_token=card_token, payment_method_id=payment_method_id, issuer_id=issuer_id,
				payer_email=payer_email or frappe.db.get_value("Guest", res.guest, "email") or "",
				identification_type=identification_type, identification_number=identification_number,
				device_id=device_id)
		except ValueError:
			return _failed("invalid")
		try:
			reply = mercadopago.card_payment(settings, link_id, body, card_idempotency_key(link_id, body["token"]))
		except CoreError as error:
			return _failed(_core_failed(res, link_id, error))
		return card_result(reply)
	except _Refused as refused:
		return _failed(refused.code)
	except Exception:
		_log(res, link_id, frappe.get_traceback(with_context=False), secrets)
		return _failed("unavailable")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=3600)
def wallet(token: str, return_url: str = "") -> dict:
	"""La dirección de Mercado Pago para pagar con el saldo de la cuenta lo que falta de la reserva.

	{"url", "code"}. `return_url` (adónde vuelve el huésped) tiene que ser de este sitio o de un origen de
	`booking_return_origins`; si no, `invalid` sin pedir nada. Va a core con el token de estado (como la de `book`);
	vacía, el huésped vuelve a la del enlace."""
	res = _res_by_status_token(token)
	try:
		back = _return_url(return_url, token) if (return_url or "").strip() else None
	except frappe.ValidationError:
		return {"url": None, "code": "invalid"}
	link_id = None
	try:
		link_id, settings = _payable(res, return_url)
		try:
			reply = mercadopago.wallet_link(settings, link_id, wallet_idempotency_key(link_id, back), back)
		except CoreError as error:
			return {"url": None, "code": _core_failed(res, link_id, error)}
	except _Refused as refused:
		return {"url": None, "code": refused.code}
	except Exception:
		_log(res, link_id, frappe.get_traceback(with_context=False))
		return {"url": None, "code": "unavailable"}
	url = reply.get("url")
	if not isinstance(url, str) or not url:
		_log(res, link_id, "core no dio la dirección del saldo")
		return {"url": None, "code": "unavailable"}
	return {"url": url, "code": None}
