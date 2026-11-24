"""La pasarela «Mercado Pago» de Uruguay para Kamra (HR-30): lo que devuelve `uruguay.payment_provider("Mercado Pago")`.

Kamra no habla con Mercado Pago: pide el enlace y las devoluciones al servicio de cobros (`kamra_uy.core.client`) y
recibe de él avisos firmados (`kamra_uy.payments.webhook`). Implementa lo que piden `kamra.localization`
(`create_link`, `test_link`, `info`, `webhook_folio`, `verify_webhook`) y `kamra.payments.refund_via_gateway`
(`refund`); y lo que `kamra_uy.payments.card` pide para la web: el enlace con su `public_key`, el cobro con tarjeta y la
preferencia del saldo (HR-222). Las credenciales son las del Payment Gateway Settings de la propiedad: `key_id` = el
código de la propiedad en el servicio, `key_secret` = su clave, `webhook_secret` = el secreto de sus avisos.
"""

from __future__ import annotations

import time
import uuid
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, get_url, now_datetime

from kamra_uy.core.client import CoreClient, CoreError, core_base_url
from kamra_uy.payments import texts
from kamra_uy.pure.core_signature import verify
from kamra_uy.pure.payments import (
	CURRENCIES,
	amount_to_float,
	charged_fx,
	link_expires_at,
	link_purpose,
	link_request,
	money_text,
	parse_core_event,
	payment_mode,
)

GATEWAY = "Mercado Pago"
SIGNUP_URL = "https://www.mercadopago.com.uy/"
WEBHOOK_PATH = "/api/method/kamra_uy.payments.webhook.core_event"
LINKS_PATH = "/v1/payments/links"


def _client(settings) -> CoreClient:
	key = settings.get_password("key_secret", raise_exception=False)
	if not settings.get("key_id") or not key:
		raise CoreError(None, "not_configured", texts.SETTINGS_INCOMPLETE)
	return CoreClient(core_base_url(), key)


def _throw(error: CoreError, failed_text: str):
	if error.code == "not_configured":
		frappe.throw(error.detail or texts.NOT_CONFIGURED)
	if error.retryable or error.status is None:
		frappe.throw(texts.UNAVAILABLE)
	frappe.throw(failed_text.format(reason=error.detail or error.code))


def _aware(value):
	"""Un Datetime de frappe (sin zona, en la del sistema) con su zona."""
	return get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone()))


def create_link(settings, *, amount: float, currency: str, reference: str, description: str, customer: dict,
                notes: dict, return_url: str | None = None) -> tuple[str, str]:
	"""(link_id, url) de un enlace que el servicio de cobros pide a Mercado Pago. `reference` es el folio.

	`return_url` es adónde vuelve el huésped tras pagar cuando Kamra la pide (la reserva web: /kamra/book?booking=…,
	HR-31); sin ella, `hr_payment_return_url` del sitio o la página /pago."""
	if (currency or "").upper() not in CURRENCIES:
		frappe.throw(texts.CURRENCY)
	folio = frappe.get_doc("Folio", reference)
	status, hold = (frappe.db.get_value("Reservation", folio.reservation, ["status", "hold_expires_on"])
	                if folio.reservation else (None, None))
	now = _aware(now_datetime())
	try:
		body = link_request(
			property_code=settings.key_id,
			folio=folio.name,
			reservation=folio.reservation,
			amount=amount,
			currency=currency,
			purpose=link_purpose(status, bool(folio.get("charges"))),
			guest_name=customer.get("name") or folio.get("guest_name"),
			guest_email=customer.get("email"),
			return_url=return_url or frappe.conf.get("hr_payment_return_url") or get_url("/pago"),
			expires_at=link_expires_at(now, _aware(hold)) if hold else None,
		)
	except ValueError as e:
		frappe.throw(texts.LINK_FAILED.format(reason=e))
	try:
		link = _client(settings).post(LINKS_PATH, body, str(uuid.uuid4()))
	except CoreError as e:
		_throw(e, texts.LINK_FAILED)
	return link["link_id"], link["url"]


def _link_path(link_id: str, tail: str = "") -> str:
	from urllib.parse import quote

	return f"{LINKS_PATH}/{quote(str(link_id), safe='')}{tail}"


def link_card(settings, link_id: str) -> dict:
	"""El enlace tal como lo tiene core (`GET /v1/payments/links/{id}`), con `card: {public_key} | null` (HR-221)."""
	return _client(settings).get(_link_path(link_id))


def card_payment(settings, link_id: str, body: dict, idempotency_key: str) -> dict:
	"""Cobra el enlace con el token de una tarjeta (`POST …/card-payments`, HR-221). CoreError si core no lo cobra."""
	return _client(settings).post(_link_path(link_id, "/card-payments"), body, idempotency_key)


def wallet_link(settings, link_id: str, idempotency_key: str, return_url: str | None = None) -> dict:
	"""La preferencia que solo admite el saldo de Mercado Pago (`POST …/wallet`, HR-221): {"url"}. Una por enlace.

	`return_url`, adónde vuelve el huésped tras pagar; sin ella, la del enlace. Core solo la usa en la primera llamada.
	La `idempotency_key` tiene que ir con el cuerpo (`wallet_idempotency_key(link_id, return_url)`): con otra vuelta y
	la misma clave, core da 409 `idempotency_conflict` (HR-238)."""
	body = {"return_url": return_url} if return_url else {}
	return _client(settings).post(_link_path(link_id, "/wallet"), body, idempotency_key)


def test_link(link_id: str) -> str:
	"""El modo prueba de Kamra: nada se cobra (lo asienta `kamra.api.simulate_payment_link`)."""
	return get_url("/pago?demo=1")


def info() -> dict:
	return {"gateway": GATEWAY, "webhook_url": get_url(WEBHOOK_PATH), "signup_url": SIGNUP_URL, "needs_webhook": False}


def webhook_folio(payload: bytes) -> str | None:
	"""El folio de un `payment.approved`; de los demás avisos, nada (los asienta el receptor propio)."""
	try:
		event = parse_core_event(payload)
	except ValueError:
		return None
	return event["folio"] if event["type"] == "payment.approved" else None


def verify_webhook(settings, payload: bytes, headers: dict) -> dict | None:
	"""Firmado con el secreto de la propiedad, y de ESA propiedad: con su firma válida, una propiedad no asienta en el
	folio de otra. Devuelve lo que asienta `kamra.payments.settle_payment_link`."""
	secret = settings.get_password("webhook_secret", raise_exception=False)
	ok, _ = verify(secret, headers.get("x-hr-signature"), payload, int(time.time())) if secret else (False, "")
	if not ok:
		frappe.throw("Invalid webhook signature", frappe.PermissionError)
	try:
		event = parse_core_event(payload)
	except ValueError:
		return None
	if event["property"] != settings.key_id:
		frappe.throw("This event belongs to another property", frappe.PermissionError)
	out = {"link_id": event.get("link_id") or "", "reference": event["payment_id"],
	       "amount": amount_to_float(event["amount"]), "mode": payment_mode(event)}
	fx = charged_fx(event)  # D-HR-68: cobrado en otra moneda; `amount` es lo que pedía el enlace
	if fx:
		out["fx"] = {k: fx[k] for k in ("currency", "fx_amount", "exchange_rate")}
	return out


def refund(settings, *, reference: str, amount: float, reason: str) -> dict:
	"""Pide al servicio que devuelva (parte de) un pago. {"status": "approved" | "pending" | "rejected", "message"}."""
	body = {"amount": money_text(amount), "reason": (reason or "").strip()[:500]}
	try:
		out = _client(settings).post(f"/v1/payments/{reference}/refunds", body, str(uuid.uuid4()))
	except CoreError as e:
		if e.status == 422:
			return {"status": "rejected", "message": texts.REFUND_REJECTED.format(reason=e.detail or e.code)}
		_throw(e, texts.REFUND_REJECTED)
	status = out.get("status")
	if status == "rejected":
		reason_text = (out.get("error") or {}).get("message") or (out.get("error") or {}).get("code") or ""
		return {"status": "rejected", "message": texts.REFUND_REJECTED.format(reason=reason_text)}
	return {"status": status or "pending", "message": texts.REFUND_REQUESTED}
