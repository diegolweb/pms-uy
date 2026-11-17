"""Los avisos firmados de core en el folio (HR-30): cobro, devolución y contracargo, una vez por aviso y por pago.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.payments.tests.test_core_event`).
"""

import frappe

from kamra import payments as kamra_payments
from kamra_uy.payments import texts, webhook
from kamra_uy.payments.tests.fixtures import (
	CODE,
	FINANCE,
	OTHER_CODE,
	OTHER_SECRET,
	PROP,
	CobrosCase,
	event,
	signed,
)

PAY_1 = "7a2b0c11-1111-4222-8333-555555555555"
PAY_2 = "7a2b0c11-1111-4222-8333-999999999999"


class TestCoreEvent(CobrosCase):
	def receive(self, body: dict, **sign_over) -> dict:
		raw, headers = signed(body, **sign_over)
		return webhook.receive(raw, headers)

	def stored(self, body: dict):
		return frappe.get_doc("UY Core Event", body["event_id"])

	def mails(self) -> int:
		return frappe.db.count("Email Queue", {"reference_doctype": "UY Core Event"})

	# --- cobro --------------------------------------------------------------

	def test_an_approved_payment_posts_once_with_its_mode_and_confirms_the_hold(self):
		res, folio = self.booking("Held")
		body = event("payment.approved", folio, mode="Tarjeta extranjera", card_country="AR")
		self.assertEqual(self.receive(body)["status"], "Applied")
		(row,) = self.rows(folio)
		self.assertEqual((row.mode, row.reference, row.payment_kind, row.amount),
		                 ("Tarjeta extranjera", PAY_1, "Advance", 1250.5))
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Confirmed")
		self.assertEqual(float(frappe.db.get_value("Reservation", res, "advance_paid")), 1250.5)
		stored = self.stored(body)
		self.assertEqual((stored.status, stored.event_type, stored.property, stored.folio, stored.payment_id,
		                  stored.core_property), ("Applied", "payment.approved", PROP, folio, PAY_1, CODE))

	def test_the_same_event_twice_is_a_duplicate(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		self.receive(body)
		self.assertEqual(self.receive(body), {"duplicate": True})
		self.assertEqual(len(self.rows(folio)), 1)

	def test_two_payments_of_one_link_are_two_rows(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.approved", folio, payment_id=PAY_2, amount="100.00"))
		self.assertEqual(sorted(r.reference for r in self.rows(folio)), sorted([PAY_1, PAY_2]))

	def test_another_event_for_a_payment_already_posted_is_a_duplicate(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		again = event("payment.approved", folio)
		self.assertEqual(self.receive(again)["status"], "Duplicate")
		self.assertEqual(len(self.rows(folio)), 1)

	def test_without_the_card_country_it_is_a_payment_link(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio, card_country=None, mode="Payment Link"))
		self.assertEqual(self.rows(folio)[0].mode, "Payment Link")

	# --- firma y propiedad --------------------------------------------------

	def test_a_bad_signature_is_refused_and_posts_nothing(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		with self.assertRaises(frappe.PermissionError):
			self.receive(body, secret="not-the-secret")
		self.assertEqual(self.rows(folio), [])
		self.assertFalse(frappe.db.exists("UY Core Event", body["event_id"]))

	def test_a_stale_signature_is_refused(self):
		_, folio = self.booking()
		with self.assertRaises(frappe.PermissionError):
			self.receive(event("payment.approved", folio), t=1)

	def test_another_property_cannot_post_on_this_folio(self):
		_, folio = self.booking()
		for kind in ("payment.approved", "payment.refunded", "payment.charged_back"):
			with self.subTest(kind=kind), self.assertRaises(frappe.PermissionError):
				self.receive(event(kind, folio, property=OTHER_CODE), secret=OTHER_SECRET)
		self.assertEqual(self.rows(folio), [])

	def test_an_unknown_property_is_refused(self):
		_, folio = self.booking()
		with self.assertRaises(frappe.PermissionError):
			self.receive(event("payment.approved", folio, property="HR-NADIE"))

	def test_a_changed_event_id_header_is_refused(self):
		_, folio = self.booking()
		with self.assertRaises(frappe.PermissionError):
			self.receive(event("payment.approved", folio), event_id="otro-id")

	def test_a_body_that_is_not_an_event_is_a_400(self):
		with self.assertRaises(webhook.InvalidEvent) as err:
			webhook.receive(b'{"type": "cfe.accepted"}', {})
		self.assertEqual(err.exception.http_status_code, 400)

	# --- devolución y contracargo ------------------------------------------

	def test_a_partial_refund_posts_once(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio, mode="Tarjeta local"))
		body = event("payment.refunded", folio, amount="100.00")
		self.assertEqual(self.receive(body)["status"], "Applied")
		self.assertEqual(self.receive(body), {"duplicate": True})
		again = event("payment.refunded", folio, amount="100.00", refund_id=body["refund_id"])
		self.assertEqual(self.receive(again)["status"], "Duplicate")
		refunds = [r for r in self.rows(folio) if r.payment_kind == "Refund"]
		self.assertEqual([(r.amount, r.reference, r.mode) for r in refunds],
		                 [(-100.0, f"refund:{PAY_1}:{body['refund_id']}", "Tarjeta local")])

	def test_a_total_refund_returns_the_whole_payment(self):
		res, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.refunded", folio, amount="1250.50"))
		self.assertAlmostEqual(kamra_payments.refunded_for_payment(frappe.get_doc("Folio", folio), PAY_1), 1250.5)
		self.assertEqual(float(frappe.db.get_value("Reservation", res, "advance_paid")), 0)

	def test_a_chargeback_after_a_partial_refund_takes_only_the_rest_and_needs_a_person(self):
		self.outbox()
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.refunded", folio, amount="100.00"))
		body = event("payment.charged_back", folio, amount="1250.50")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		chargeback = [r for r in self.rows(folio) if r.reference == f"chargeback:{PAY_1}"]
		self.assertEqual([r.amount for r in chargeback], [-1150.5])
		self.assertEqual(self.stored(body).status, "Needs attention")
		self.assertEqual(self.mails(), 1)

	def test_a_chargeback_of_a_payment_already_refunded_posts_nothing(self):
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		self.receive(event("payment.refunded", folio, amount="1250.50"))
		body = event("payment.charged_back", folio, amount="1250.50")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertFalse([r for r in self.rows(folio) if r.reference.startswith("chargeback:")])

	def test_a_refund_on_a_closed_folio_needs_attention_and_mails_finance(self):
		self.outbox()
		_, folio = self.booking()
		self.receive(event("payment.approved", folio))
		frappe.db.set_value("Folio", folio, "status", "Closed")
		body = event("payment.refunded", folio, amount="100.00")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(len(self.rows(folio)), 1)
		self.assertEqual(self.stored(body).detail, "folio_closed")
		mail = frappe.get_all("Email Queue", {"reference_doctype": "UY Core Event",
		                                      "reference_name": body["event_id"]}, pluck="name")
		self.assertEqual(len(mail), 1)
		recipients = frappe.get_all("Email Queue Recipient", {"parent": mail[0]}, pluck="recipient")
		self.assertIn(FINANCE, recipients)
		message = frappe.db.get_value("Email Queue", mail[0], "message")
		self.assertIn(texts.REVIEW_SUBJECT.format(property=PROP), message)

	def test_a_payment_on_a_closed_folio_needs_attention(self):
		self.outbox()
		_, folio = self.booking()
		frappe.db.set_value("Folio", folio, "status", "Closed")
		body = event("payment.approved", folio)
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(self.rows(folio), [])
		self.assertEqual(self.mails(), 1)

	def test_another_currency_needs_attention(self):
		self.outbox()
		_, folio = self.booking()
		body = event("payment.approved", folio, currency="UYU")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		self.assertEqual(self.rows(folio), [])
		self.assertEqual(self.mails(), 1)

	def test_a_folio_that_does_not_exist_needs_attention(self):
		self.outbox()
		body = event("payment.approved", "FOLIO-NO-EXISTE")
		self.assertEqual(self.receive(body)["status"], "Needs attention")
		stored = self.stored(body)
		self.assertFalse(stored.folio)
		self.assertEqual(stored.property, PROP)
		self.assertEqual(self.mails(), 1)

	def test_an_unexpected_error_stores_nothing(self):
		_, folio = self.booking()
		body = event("payment.approved", folio)
		from unittest.mock import patch

		with patch("kamra.payments.handle_webhook", side_effect=RuntimeError("boom")), \
				self.assertRaises(RuntimeError):
			self.receive(body)
		self.assertFalse(frappe.db.exists("UY Core Event", body["event_id"]))

	def test_the_event_log_is_read_only_for_people(self):
		meta = frappe.get_meta("UY Core Event")
		roles = {p.role: p for p in meta.permissions}
		self.assertTrue({"Finance", "Hotel Admin"} <= set(roles))
		for role in ("Finance", "Hotel Admin"):
			self.assertTrue(roles[role].read)
			self.assertFalse(roles[role].write or roles[role].create or roles[role].delete)
		self.assertEqual(meta.autoname, "field:event_id")
		self.assertEqual(meta.module, "Kamra UY")
