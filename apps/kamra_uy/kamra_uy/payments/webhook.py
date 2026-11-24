"""El receptor de los avisos firmados del servicio de cobros (HR-30): cobro, devolución y contracargo en el folio.

`POST /api/method/kamra_uy.payments.webhook.core_event`, con `X-HR-Signature`, `X-HR-Event-Id` y `X-HR-Event-Type`.
Respuestas: cuerpo que no es un aviso → 400; firma mala, propiedad desconocida, `X-HR-Event-Id` cambiado o folio de
otra propiedad → 403; aviso ya recibido → 200 {"duplicate": true}; lo que no se arregla reintentando (folio cerrado,
otra moneda, folio que no existe, todo contracargo, el pago que llega con la reserva ya cancelada) → 200, «Needs
attention» y un correo a Finanzas. Un error inesperado no guarda nada y da 500: el servicio reintenta todo lo que no
sea 2xx.

Cada aviso queda en UY Core Event (una vez por `event_id`); cada pago y cada devolución, una vez por su referencia.
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import now_datetime

from kamra_uy.payments import texts
from kamra_uy.payments.mercadopago import GATEWAY
from kamra_uy.pure.core_signature import verify
from kamra_uy.pure.payments import amount_to_float, charged_fx, parse_core_event

EVENT_DOCTYPE = "UY Core Event"
APPLIED, DUPLICATE, NEEDS_ATTENTION = "Applied", "Duplicate", "Needs attention"
_KIND = {"payment.approved": texts.KIND_PAYMENT, "payment.refunded": texts.KIND_REFUND,
         "payment.charged_back": texts.KIND_CHARGEBACK}
# una reserva así ya no espera ningún pago: si llega uno (la retención venció mientras el huésped pagaba), es tardío
LATE = ("Cancelled", "No Show")


class InvalidEvent(frappe.ValidationError):
	http_status_code = 400


@frappe.whitelist(allow_guest=True, methods=["POST"])
def core_event():
	"""Lo llama el servicio de cobros; la ruta la fija el servicio (`x-hr-target-path`)."""
	headers = {k.lower(): v for k, v in (frappe.request.headers or {}).items()}
	out = receive(frappe.request.get_data() or b"", headers)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persists the completed operation before returning to an external/public caller; reviewed as intentional
	return out


def receive(payload: bytes, headers: dict) -> dict:
	"""Comprueba, deduplica, asienta y guarda un aviso. `headers` en minúsculas."""
	try:
		event = parse_core_event(payload)
	except ValueError as e:
		frappe.throw(str(e), InvalidEvent)
	settings = _settings_for(event["property"])
	secret = settings.get_password("webhook_secret", raise_exception=False)
	ok, _ = verify(secret, headers.get("x-hr-signature"), payload, int(time.time())) if secret else (False, "")
	if not ok:
		frappe.throw("Invalid webhook signature", frappe.PermissionError)
	if headers.get("x-hr-event-id") != event["event_id"]:
		frappe.throw("The event id does not match the signed event", frappe.PermissionError)
	if frappe.db.exists(EVENT_DOCTYPE, event["event_id"]):
		return {"duplicate": True}
	prop = settings.property
	folio_property = frappe.db.get_value("Folio", event["folio"], "property")
	if folio_property and folio_property != prop:
		frappe.throw("This event belongs to another property", frappe.PermissionError)
	folio = event["folio"] if folio_property else None

	status, detail, action = _apply(event, folio, prop, payload, headers)
	doc = frappe.get_doc({
		"doctype": EVENT_DOCTYPE, "event_id": event["event_id"], "event_type": event["type"],
		"core_property": event["property"], "property": prop, "folio": folio, "payment_id": event["payment_id"],
		"refund_id": event.get("refund_id"), "status": status, "detail": detail, "received_at": now_datetime(),
	}).insert(ignore_permissions=True)
	if action:
		_mail_finance(doc, event, action)
	return {"ok": True, "status": status}


def _settings_for(core_property: str):
	name = frappe.db.get_value("Payment Gateway Settings",
	                           {"gateway": GATEWAY, "enabled": 1, "key_id": core_property})
	if not name:
		frappe.throw("No property takes Mercado Pago payments with this code", frappe.PermissionError)
	return frappe.get_doc("Payment Gateway Settings", name)


def _apply(event: dict, folio: str | None, prop: str, payload: bytes, headers: dict) -> tuple[str, str, str | None]:
	"""(estado, detalle sin datos personales, qué hacer si lo tiene que revisar Finanzas)."""
	if not folio:
		return NEEDS_ATTENTION, f"folio_not_found: {event['folio'][:140]}", texts.ACTION_NO_FOLIO
	currency = (frappe.get_cached_value("Property", prop, "currency") or "").upper()
	if currency != event["currency"]:
		return NEEDS_ATTENTION, f"currency {event['currency']} != {currency or '-'}", texts.ACTION_CURRENCY
	if event["type"] == "payment.approved" and _booking_status(folio) in LATE:
		return _late_payment(folio, payload, headers)
	if frappe.db.get_value("Folio", folio, "status") == "Closed":
		return NEEDS_ATTENTION, "folio_closed", texts.ACTION_FOLIO_CLOSED

	from kamra import payments

	kind = event["type"]
	if kind == "payment.approved":
		out = payments.handle_webhook(GATEWAY, payload, headers)
		if out.get("ignored"):
			return NEEDS_ATTENTION, "ignored by the gateway door", texts.ACTION_NO_FOLIO
		return (APPLIED, "posted" + _charged_note(event), None) if out.get("posted") else \
			(DUPLICATE, "payment already posted", None)

	payment_id = event["payment_id"]
	doc = frappe.get_doc("Folio", folio)
	mode = next((p.mode for p in doc.payments if p.reference == payment_id and p.payment_kind != "Refund"),
	            None) or "Payment Link"
	amount = amount_to_float(event["amount"])
	if kind == "payment.refunded":
		out = payments.record_gateway_refund(
			folio, payments.gateway_refund_reference("refund", payment_id, event["refund_id"]), amount, mode,
			texts.REFUND_REASON)
		return _refund_outcome(out, None)

	# contracargo: lo que quedaba del pago, y siempre lo revisa una persona
	left = round(amount - payments.refunded_for_payment(doc, payment_id), 2)
	if left <= 0:
		return NEEDS_ATTENTION, "chargeback of a payment already refunded: nothing posted", texts.ACTION_CHARGEBACK
	out = payments.record_gateway_refund(
		folio, payments.gateway_refund_reference("chargeback", payment_id), left, mode, texts.CHARGEBACK_REASON)
	if out.get("reason") == "duplicate":
		return DUPLICATE, "chargeback already posted", None
	return NEEDS_ATTENTION, f"chargeback posted: {left:.2f}", texts.ACTION_CHARGEBACK


def _charged_note(event: dict) -> str:
	"""D-HR-68: lo que Mercado Pago cobró en otra moneda, para quien mire el aviso («; charged UYU 4478.14 at 40.71»)."""
	fx = charged_fx(event)
	return f"; charged {fx['currency']} {event['charged']['amount']} at {fx['charged_rate']}" if fx else ""


def _booking_status(folio: str) -> str | None:
	reservation = frappe.db.get_value("Folio", folio, "reservation")
	return frappe.db.get_value("Reservation", reservation, "status") if reservation else None


def _late_payment(folio: str, payload: bytes, headers: dict) -> tuple[str, str, str | None]:
	"""El pago de una reserva que ya no lo esperaba (HR-31): el dinero existe, así que se asienta si el folio está
	abierto, y desde ahí se puede devolver; pero la reserva NO se reactiva sola (su habitación pudo venderse otra vez).
	Siempre lo revisa Finanzas."""
	if frappe.db.get_value("Folio", folio, "status") == "Closed":
		return NEEDS_ATTENTION, "late payment: folio closed, not posted", texts.ACTION_LATE_PAYMENT
	from kamra import payments

	out = payments.handle_webhook(GATEWAY, payload, headers)
	if out.get("ignored"):
		return NEEDS_ATTENTION, "ignored by the gateway door", texts.ACTION_NO_FOLIO
	if not out.get("posted"):
		return DUPLICATE, "payment already posted", None
	return NEEDS_ATTENTION, "late payment: posted", texts.ACTION_LATE_PAYMENT


def _refund_outcome(out: dict, action: str | None) -> tuple[str, str, str | None]:
	if out.get("posted"):
		return APPLIED, "posted", action
	if out.get("reason") == "duplicate":
		return DUPLICATE, "refund already posted", None
	return NEEDS_ATTENTION, str(out.get("reason") or "not posted"), texts.ACTION_FOLIO_CLOSED


def _finance_users() -> list[str]:
	holders = frappe.get_all("Has Role", filters={"role": "Finance", "parenttype": "User"}, pluck="parent")
	if not holders:
		return []
	users = frappe.get_all("User", filters={"name": ["in", holders], "enabled": 1}, pluck="name")
	return sorted(u for u in users if u not in ("Administrator", "Guest"))


def _mail_finance(doc, event: dict, action: str) -> None:
	recipients = _finance_users()
	if not recipients:
		return
	from frappe.utils import escape_html

	from kamra.localization import money_text

	prop_name = frappe.get_cached_value("Property", doc.property, "property_name") or doc.property
	subject = texts.REVIEW_SUBJECT.format(property=prop_name)
	message = f"<p>{escape_html(subject)}</p>" + texts.REVIEW_BODY.format(
		kind=_KIND[event["type"]], folio=escape_html(event["folio"]),
		amount=escape_html(money_text(doc.property, amount_to_float(event["amount"]))),
		action=escape_html(action))
	# El aviso vale más que su correo: si no se puede encolar (sin cuenta saliente, sendmail LANZA OutgoingEmailError,
	# HTTP 501), se deshace solo el correo, el aviso se guarda y queda en «Needs attention», y el porqué va al Error Log
	# del aviso. Antes, el error tumbaba la transacción entera y core reintentaba un aviso que nunca entraba (HR-224).
	savepoint = "kamra_uy_mail_finance"
	frappe.db.savepoint(savepoint)
	try:
		frappe.sendmail(recipients=recipients, subject=subject, message=message,
		                reference_doctype=EVENT_DOCTYPE, reference_name=doc.name)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title="kamra_uy cobros: correo a Finanzas sin enviar",  # sin traza: ni destinatarios ni locales
		                 message=f"{type(e).__name__}: {e}", reference_doctype=EVENT_DOCTYPE, reference_name=doc.name)
	else:
		frappe.db.release_savepoint(savepoint)
