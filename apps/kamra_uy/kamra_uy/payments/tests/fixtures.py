"""Lo común de las pruebas de cobros (HR-30): una propiedad de Uruguay con su pasarela «Mercado Pago» y avisos de core
firmados como los firma core. Los secretos son inventados (sin el prefijo de los de core)."""

import json
import time
import uuid

import frappe

from kamra.tests.audit_fixtures import AuditTestCase, d, ensure_user, make_guest, make_property, make_reservation, make_room_type
from kamra_uy.pure.core_signature import sign

PROP = "UT UY Cobros"
OTHER = "UT UY Cobros Otra"
CODE = "HR-UT-PAY"
OTHER_CODE = "HR-UT-PAY-OTRA"
CORE_KEY = "test-core-api-key-not-real-0000"
SECRET = "test-kamra-uy-signing-secret-not-real"
OTHER_SECRET = "test-kamra-uy-other-signing-secret-not-real"
FINANCE = "finanzas-cobros-ut@example.com"


def _ensure_currency(code: str) -> None:
	if not frappe.db.exists("Currency", code):
		frappe.get_doc({"doctype": "Currency", "currency_name": code, "enabled": 1}).insert(ignore_permissions=True)


def make_uy_property(name: str = PROP, currency: str = "USD") -> str:
	_ensure_currency(currency)
	return make_property(name, country="Uruguay", currency=currency, uy_iva_room_pct=10, uy_iva_standard_pct=22)


def gateway_settings(prop: str = PROP, code: str = CODE, secret: str = SECRET, **over) -> str:
	for name in frappe.get_all("Payment Gateway Settings", {"property": prop}, pluck="name"):
		frappe.delete_doc("Payment Gateway Settings", name, ignore_permissions=True, force=True)
	doc = frappe.get_doc({"doctype": "Payment Gateway Settings", "property": prop, "gateway": "Mercado Pago",
	                      "enabled": 1, "test_mode": 0, "key_id": code, "key_secret": CORE_KEY,
	                      "webhook_secret": secret, **over})
	return doc.insert(ignore_permissions=True).name


class CobrosCase(AuditTestCase):
	properties = (PROP, OTHER)

	def setUp(self):
		super().setUp()
		make_uy_property()
		make_uy_property(OTHER)
		self.rt = make_room_type(PROP, "UYC")
		gateway_settings()
		gateway_settings(OTHER, OTHER_CODE, OTHER_SECRET)

	def booking(self, status: str = "Held", **fields) -> tuple[str, str]:
		from kamra.folio import open_folio

		guest = make_guest("UT Huésped Cobros", email="huesped-cobros@example.com")
		res = make_reservation(PROP, self.rt, d(5), d(7), status=status, guest=guest, **fields)
		return res.name, open_folio(res)

	def outbox(self):
		"""Una cuenta saliente solo dentro del test (hr-gate no tiene), para que frappe.sendmail encole."""
		for name in frappe.get_all("Email Account", {"default_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, "default_outgoing", 0)
		frappe.get_doc({"doctype": "Email Account", "email_account_name": "UT Cobros", "email_id": "cobros@example.com",
		                "enable_outgoing": 1, "default_outgoing": 1, "smtp_server": "smtp.example.com",
		                "no_smtp_authentication": 1}).insert(ignore_permissions=True)
		ensure_user(FINANCE, ["Finance"])

	def rows(self, folio: str) -> list:
		return frappe.get_doc("Folio", folio).payments


def event(kind: str, folio: str, **over) -> dict:
	base = {"event_id": str(uuid.uuid4()), "type": kind, "property": CODE, "folio": folio,
	        "link_id": "8c3d1e22-1111-4222-8333-666666666666", "payment_id": "7a2b0c11-1111-4222-8333-555555555555",
	        "amount": "1250.50", "currency": "USD"}
	if kind == "payment.approved":
		base.update(card_country=None, mode="Payment Link", paid_at="2031-03-12T12:00:00Z", purpose="deposit")
	elif kind == "payment.refunded":
		base.update(refund_id=str(uuid.uuid4()), refunded_total=over.get("amount", base["amount"]),
		            refunded_at="2031-03-13T12:00:00Z")
	else:
		base.update(charged_back_at="2031-03-14T12:00:00Z")
	base.update(over)
	return base


def signed(body: dict, secret: str = SECRET, event_id: str | None = None, t: int | None = None) -> tuple[bytes, dict]:
	raw = json.dumps(body).encode()
	t = int(time.time()) if t is None else t
	headers = {"x-hr-signature": f"t={t},v1={sign(secret, t, raw)}", "x-hr-event-id": event_id or body["event_id"],
	           "x-hr-event-type": body["type"], "content-type": "application/json"}
	return raw, headers
