"""La pasarela «Mercado Pago» de Uruguay dentro de Kamra (HR-30): el enlace se pide a core y la devolución también, con
el cliente de core sustituido por un doble (ninguna prueba sale a la red).

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.payments.tests.test_mercadopago`).
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import get_url, now_datetime

from kamra import localization
from kamra import payments as kamra_payments
from kamra_uy.core.client import CoreError
from kamra_uy.localization import uruguay
from kamra_uy.payments import mercadopago, texts
from kamra_uy.payments.tests.fixtures import CODE, CORE_KEY, PROP, SECRET, CobrosCase, event, signed

CORE = "http://core.example.com"
RETURN = "https://hotel.example.com/pago"


class Core:
	"""El doble de CoreClient: apunta cada llamada y responde lo que se le da."""

	def __init__(self, reply=None, error: CoreError | None = None):
		self.reply = reply if reply is not None else {}
		self.error = error
		self.calls = []
		self.made_with = None

	def __call__(self, base_url, api_key, *a, **k):
		self.made_with = (base_url, api_key)
		return self

	def post(self, path, body, idempotency_key):
		self.calls.append((path, body, idempotency_key))
		if self.error:
			raise self.error
		return self.reply


class TestMercadoPago(CobrosCase):
	def setUp(self):
		super().setUp()
		conf = patch.dict(frappe.conf, {"hr_core_base_url": CORE, "hr_payment_return_url": RETURN})
		conf.start()
		self.addCleanup(conf.stop)

	def core(self, **kw) -> Core:
		double = Core(**kw)
		p = patch("kamra_uy.payments.mercadopago.CoreClient", double)
		p.start()
		self.addCleanup(p.stop)
		return double

	# --- el paquete -----------------------------------------------------------

	def test_uruguay_offers_mercado_pago(self):
		self.assertEqual(localization.payment_gateways(localization.pack_for(PROP)), ["Mercado Pago"])
		self.assertIs(uruguay.payment_provider("Mercado Pago"), mercadopago)
		options = frappe.get_meta("Payment Gateway Settings").get_field("gateway").options.split("\n")
		self.assertEqual(options, ["Razorpay", "Cashfree", "Mercado Pago"])

	def test_info_for_the_settings_card(self):
		self.assertEqual(kamra_payments.gateway_info(PROP), {
			"gateway": "Mercado Pago",
			"webhook_url": get_url("/api/method/kamra_uy.payments.webhook.core_event"),
			"signup_url": "https://www.mercadopago.com.uy/",
			"needs_webhook": False,
		})

	# --- el enlace ------------------------------------------------------------

	def test_a_deposit_link_is_asked_to_core_and_kept_on_the_folio(self):
		hold = now_datetime() + timedelta(hours=6)
		res, folio = self.booking("Held", hold_expires_on=hold)
		core = self.core(reply={"link_id": "8c3d1e22-1111-4222-8333-666666666666", "url": "http://mp-mock:8080/checkout/v1/redirect?pref_id=1",
		                        "status": "pending", "expires_at": None})
		out = kamra_payments.create_payment_link(folio, amount=1250.5, purpose="Deposit")
		self.assertEqual(out["link_id"], "8c3d1e22-1111-4222-8333-666666666666")
		self.assertEqual(core.made_with, (CORE, CORE_KEY))
		((path, body, key),) = core.calls
		self.assertEqual(path, "/v1/payments/links")
		uuid.UUID(key)
		self.assertEqual({k: v for k, v in body.items() if k != "expires_at"}, {
			"property": CODE, "folio": folio, "reservation": res, "amount": "1250.50", "currency": "USD",
			"purpose": "deposit", "payer": {"name": frappe.db.get_value("Folio", folio, "guest_name"),
			                                "email": "huesped-cobros@example.com"},
			"return_url": RETURN,
		})
		self.assertTrue(body["expires_at"].endswith("Z"))
		saved = frappe.db.get_value("Folio", folio, ["payment_link_id", "payment_link_url"])
		self.assertEqual(saved, ("8c3d1e22-1111-4222-8333-666666666666", "http://mp-mock:8080/checkout/v1/redirect?pref_id=1"))

	def test_the_return_page_names_the_property_of_the_link_and_nothing_else(self):
		from kamra_uy.www import pago

		_, folio = self.booking("Confirmed")
		self.core(reply={"link_id": "link-pago-1", "url": "http://mp/1"})
		kamra_payments.create_payment_link(folio, amount=10)
		name = frappe.db.get_value("Property", PROP, "property_name")
		self.assertEqual(pago._property_name("link-pago-1"), name)
		self.assertIsNone(pago._property_name("otro-enlace"))
		self.assertIsNone(pago._property_name(None))

	def test_each_link_has_its_own_idempotency_key(self):
		_, folio = self.booking("Confirmed")
		core = self.core(reply={"link_id": "l", "url": "http://mp/1"})
		kamra_payments.create_payment_link(folio, amount=10)
		kamra_payments.create_payment_link(folio, amount=10)
		self.assertNotEqual(core.calls[0][2], core.calls[1][2])
		self.assertNotIn("expires_at", core.calls[0][1])

	def test_without_a_return_url_it_comes_back_to_the_pago_page(self):
		_, folio = self.booking("Confirmed")
		core = self.core(reply={"link_id": "l", "url": "http://mp/1"})
		with patch.dict(frappe.conf, {"hr_payment_return_url": None}):
			kamra_payments.create_payment_link(folio, amount=10)
		self.assertEqual(core.calls[0][1]["return_url"], get_url("/pago"))

	def test_what_reception_reads_when_core_fails(self):
		_, folio = self.booking("Confirmed")
		cases = [
			(CoreError(503, "upstream_unavailable", "MP caído", retryable=True), texts.UNAVAILABLE),
			(CoreError(None, "upstream_unavailable", "sin respuesta", retryable=True), texts.UNAVAILABLE),
			(CoreError(422, "validation_failed", "falta payer"), texts.LINK_FAILED.format(reason="falta payer")),
		]
		for error, text in cases:
			with self.subTest(code=error.code, status=error.status):
				self.core(error=error)
				with self.assertRaises(frappe.ValidationError) as err:
					kamra_payments.create_payment_link(folio, amount=10)
				self.assertIn(text, str(err.exception))

	def test_without_the_core_address_reception_is_told_what_is_missing(self):
		_, folio = self.booking("Confirmed")
		with patch.dict(frappe.conf, {"hr_core_base_url": None}), self.assertRaises(frappe.ValidationError) as err:
			kamra_payments.create_payment_link(folio, amount=10)
		self.assertIn(texts.NOT_CONFIGURED, str(err.exception))

	def test_only_pesos_or_dollars(self):
		_, folio = self.booking("Confirmed")
		settings = frappe.get_doc("Payment Gateway Settings", {"property": PROP})
		self.core(reply={"link_id": "l", "url": "u"})
		with self.assertRaises(frappe.ValidationError) as err:
			mercadopago.create_link(settings, amount=10, currency="EUR", reference=folio, description="x",
			                        customer={"name": "UT"}, notes={})
		self.assertIn(texts.CURRENCY, str(err.exception))

	def test_a_test_mode_link_points_to_the_demo_page(self):
		self.assertEqual(mercadopago.test_link("plink_TEST1"), get_url("/pago?demo=1"))

	# --- el aviso, por la puerta de Kamra ------------------------------------

	def test_the_folio_of_an_approved_event_only(self):
		raw, _ = signed(event("payment.approved", "FOLIO-1"))
		self.assertEqual(mercadopago.webhook_folio(raw), "FOLIO-1")
		self.assertIsNone(mercadopago.webhook_folio(signed(event("payment.refunded", "FOLIO-1"))[0]))
		self.assertIsNone(mercadopago.webhook_folio(b"nope"))

	def test_verify_webhook_returns_what_settle_needs(self):
		settings = frappe.get_doc("Payment Gateway Settings", {"property": PROP})
		raw, headers = signed(event("payment.approved", "FOLIO-1", mode="Tarjeta local", amount="99.90"))
		self.assertEqual(mercadopago.verify_webhook(settings, raw, headers), {
			"link_id": "8c3d1e22-1111-4222-8333-666666666666", "reference": "7a2b0c11-1111-4222-8333-555555555555",
			"amount": 99.9, "mode": "Tarjeta local"})
		with self.assertRaises(frappe.PermissionError):
			mercadopago.verify_webhook(settings, raw, {**headers, "x-hr-signature": "t=1,v1=" + "0" * 64})
		raw, headers = signed(event("payment.approved", "FOLIO-1", property="HR-OTRA"))
		with self.assertRaises(frappe.PermissionError):
			mercadopago.verify_webhook(settings, raw, headers)

	# --- la devolución --------------------------------------------------------

	def paid_folio(self) -> str:
		_, folio = self.booking("Confirmed")
		kamra_payments.settle_payment_link(folio, "l1", 500, mode="Tarjeta local", reference="pay-1")
		return folio

	def test_a_refund_is_asked_to_core_and_posts_nothing(self):
		folio = self.paid_folio()
		core = self.core(reply={"refund_id": "r1", "payment_id": "pay-1", "amount": "200.00", "currency": "USD",
		                        "status": "approved", "created_at": "2031-03-12T12:00:00Z"})
		out = kamra_payments.refund_via_gateway(folio, "pay-1", 200, "Cancela")
		self.assertEqual(out, {"status": "approved", "message": texts.REFUND_REQUESTED})
		((path, body, key),) = core.calls
		self.assertEqual((path, body), ("/v1/payments/pay-1/refunds", {"amount": "200.00", "reason": "Cancela"}))
		uuid.UUID(key)
		self.assertEqual(len(frappe.get_doc("Folio", folio).payments), 1)

	def test_a_pending_refund_says_so_too(self):
		folio = self.paid_folio()
		self.core(reply={"status": "pending"})
		self.assertEqual(kamra_payments.refund_via_gateway(folio, "pay-1", 10, "x"),
		                 {"status": "pending", "message": texts.REFUND_REQUESTED})

	def test_a_refund_mercado_pago_refuses(self):
		folio = self.paid_folio()
		self.core(reply={"status": "rejected", "error": {"code": "cc_rejected", "message": "fondos"}})
		self.assertEqual(kamra_payments.refund_via_gateway(folio, "pay-1", 10, "x"),
		                 {"status": "rejected", "message": texts.REFUND_REJECTED.format(reason="fondos")})
		self.core(error=CoreError(422, "validation_failed", "más de lo cobrado"))
		self.assertEqual(kamra_payments.refund_via_gateway(folio, "pay-1", 10, "x"),
		                 {"status": "rejected", "message": texts.REFUND_REJECTED.format(reason="más de lo cobrado")})

	def test_a_refund_when_core_is_down(self):
		folio = self.paid_folio()
		self.core(error=CoreError(None, "upstream_unavailable", "", retryable=True))
		with self.assertRaises(frappe.ValidationError) as err:
			kamra_payments.refund_via_gateway(folio, "pay-1", 10, "x")
		self.assertIn(texts.UNAVAILABLE, str(err.exception))

	def test_the_secrets_stay_in_the_settings(self):
		settings = frappe.get_doc("Payment Gateway Settings", {"property": PROP})
		self.assertEqual(settings.get_password("webhook_secret"), SECRET)
		self.assertNotIn(CORE_KEY, json.dumps(settings.as_dict(), default=str))
