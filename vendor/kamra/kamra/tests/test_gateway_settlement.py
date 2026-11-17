# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

"""Money a gateway reports: how the guest paid, which payment it was, and
refunds or chargebacks that already happened at the gateway.

A link can be paid more than once, so payments dedupe by the gateway's own
payment reference when it sends one; a gateway refund is posted once per
reference, without the till (no cash moved through the drawer).
"""

from unittest.mock import MagicMock, patch

import frappe

from kamra import payments
from kamra.tests.audit_fixtures import (
	PROP_A,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room_type,
)


class TestGatewaySettlement(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		make_property(PROP_A)
		self.rt = make_room_type(PROP_A, "STD")

	def booking(self, status="Confirmed"):
		from kamra.folio import open_folio

		res = make_reservation(PROP_A, self.rt, d(5), d(7), status=status)
		return res.name, open_folio(res)

	def rows(self, folio):
		return frappe.get_doc("Folio", folio).payments

	def advance_paid(self, res):
		return float(frappe.db.get_value("Reservation", res, "advance_paid") or 0)

	# --- settle_payment_link -------------------------------------------------

	def test_settle_keeps_the_old_behaviour_without_mode_or_reference(self):
		_, folio = self.booking()
		self.assertTrue(payments.settle_payment_link(folio, "plink_1", 500))
		self.assertFalse(payments.settle_payment_link(folio, "plink_1", 500))
		rows = self.rows(folio)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].mode, "Payment Link")
		self.assertEqual(rows[0].reference, "plink_1")
		self.assertEqual(rows[0].payment_kind, "Advance")

	def test_settle_records_the_mode_and_reference_the_gateway_reports(self):
		_, folio = self.booking()
		self.assertTrue(payments.settle_payment_link(
			folio, "plink_1", 500, mode="Card", reference="pay_1"))
		rows = self.rows(folio)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].mode, "Card")
		self.assertEqual(rows[0].reference, "pay_1")

	def test_two_payments_of_one_link_post_twice(self):
		_, folio = self.booking()
		self.assertTrue(payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1"))
		self.assertTrue(payments.settle_payment_link(folio, "plink_1", 300, reference="pay_2"))
		self.assertEqual(sorted(r.reference for r in self.rows(folio)), ["pay_1", "pay_2"])

	def test_a_link_paid_on_a_held_booking_confirms_it_and_counts_the_advance(self):
		res, folio = self.booking("Held")
		self.assertTrue(payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1"))
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Confirmed")
		self.assertEqual(self.advance_paid(res), 500)

	def test_the_same_payment_posts_once(self):
		_, folio = self.booking()
		self.assertTrue(payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1"))
		self.assertFalse(payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1"))
		self.assertEqual(len(self.rows(folio)), 1)

	def test_handle_webhook_passes_the_mode_and_reference_through(self):
		_, folio = self.booking()
		paid = {"folio": folio, "property": PROP_A, "link_id": "plink_1",
		        "amount": 500.0, "mode": "Card", "reference": "pay_1"}
		with patch("kamra.localization.parse_payment_webhook", return_value=paid):
			out = payments.handle_webhook("Razorpay", b"{}", {})
		self.assertEqual(out, {"ok": True, "folio": folio, "posted": True})
		row = self.rows(folio)[0]
		self.assertEqual((row.mode, row.reference), ("Card", "pay_1"))

	def test_handle_webhook_without_mode_posts_a_payment_link(self):
		_, folio = self.booking()
		paid = {"folio": folio, "property": PROP_A, "link_id": "plink_1", "amount": 500.0}
		with patch("kamra.localization.parse_payment_webhook", return_value=paid):
			payments.handle_webhook("Razorpay", b"{}", {})
		row = self.rows(folio)[0]
		self.assertEqual((row.mode, row.reference), ("Payment Link", "plink_1"))

	# --- record_gateway_refund ----------------------------------------------

	def test_a_deposit_refund_releases_the_deposit_and_advance_paid(self):
		res, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, mode="Card", reference="pay_1")
		self.assertEqual(self.advance_paid(res), 500)
		with patch("kamra.cashier.record_cashier_txn") as till:
			out = payments.record_gateway_refund(
				folio, "refund:pay_1:r1", 200, "Card", "Refunded at the gateway")
		till.assert_not_called()
		self.assertTrue(out["posted"])
		refund = [r for r in self.rows(folio) if r.payment_kind == "Refund"]
		self.assertEqual(len(refund), 1)
		self.assertEqual((refund[0].amount, refund[0].reference, refund[0].mode),
		                 (-200, "refund:pay_1:r1", "Card"))
		self.assertEqual(self.advance_paid(res), 300)
		deposit = frappe.db.sql(
			"""SELECT COALESCE(SUM(credit - debit), 0) FROM `tabFolio Ledger Entry`
			   WHERE folio = %s AND ledger = 'Deposit'""", (folio,))[0][0]
		self.assertEqual(float(deposit), 300)

	def test_a_gateway_refund_posts_once_per_reference(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.record_gateway_refund(folio, "refund:pay_1:r1", 200, "Payment Link", "x")
		out = payments.record_gateway_refund(folio, "refund:pay_1:r1", 200, "Payment Link", "x")
		self.assertEqual(out, {"posted": False, "reason": "duplicate"})
		self.assertEqual(len([r for r in self.rows(folio) if r.payment_kind == "Refund"]), 1)

	def test_a_gateway_refund_ignores_the_guest_credit_limit(self):
		# the guest owes money on the stay, yet the gateway already gave it back
		_, folio = self.booking()
		doc = frappe.get_doc("Folio", folio)
		doc.append("charges", {"posting_date": d(0), "charge_type": "Minibar",
		                       "description": "UT", "qty": 1, "rate": 1000,
		                       "amount": 1000, "gst_rate": 0, "auto_posted": 0})
		doc.save(ignore_permissions=True)
		payments.settle_payment_link(folio, "plink_1", 400, reference="pay_1")
		out = payments.record_gateway_refund(folio, "chargeback:pay_1", 400, "Payment Link", "x")
		self.assertTrue(out["posted"])
		self.assertAlmostEqual(out["balance"], float(frappe.db.get_value("Folio", folio, "balance")))

	def test_a_closed_folio_is_left_alone(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		frappe.db.set_value("Folio", folio, "status", "Closed")
		out = payments.record_gateway_refund(folio, "refund:pay_1:r1", 200, "Payment Link", "x")
		self.assertEqual(out, {"posted": False, "reason": "folio_closed"})
		self.assertEqual(len(self.rows(folio)), 1)

	def test_a_gateway_refund_reference_names_the_payment_it_returns(self):
		self.assertEqual(payments.gateway_refund_reference("refund", "pay_1", "r1"),
		                 "refund:pay_1:r1")
		self.assertEqual(payments.gateway_refund_reference("chargeback", "pay_1"),
		                 "chargeback:pay_1")

	def test_refunded_for_payment_does_not_mix_up_similar_references(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_10")
		payments.record_gateway_refund(folio, "refund:pay_10:r1", 100, "Payment Link", "x")
		doc = frappe.get_doc("Folio", folio)
		self.assertEqual(payments.refunded_for_payment(doc, "pay_1"), 0)
		self.assertEqual(payments.refunded_for_payment(doc, "pay_10"), 100)

	def test_refunded_for_payment_counts_refunds_and_chargebacks_of_that_payment(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_2")
		payments.record_gateway_refund(folio, "refund:pay_1:r1", 100, "Payment Link", "x")
		payments.record_gateway_refund(folio, "chargeback:pay_1", 50, "Payment Link", "x")
		payments.record_gateway_refund(folio, "refund:pay_2:r1", 70, "Payment Link", "x")
		doc = frappe.get_doc("Folio", folio)
		self.assertEqual(payments.refunded_for_payment(doc, "pay_1"), 150)
		self.assertEqual(payments.refunded_for_payment(doc, "pay_2"), 70)
		self.assertEqual(payments.refunded_for_payment(doc, "pay_3"), 0)

	# --- refund_via_gateway --------------------------------------------------

	def gateway(self, provider):
		settings = frappe._dict(gateway="Razorpay", property=PROP_A)
		stack = [patch("kamra.payments._settings", return_value=settings),
		         patch("kamra.localization._provider", return_value=provider)]
		for p in stack:
			p.start()
			self.addCleanup(p.stop)
		return settings

	def test_refund_via_gateway_asks_the_gateway_and_posts_nothing(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		provider = MagicMock()
		provider.refund.return_value = {"status": "approved"}
		settings = self.gateway(provider)
		out = payments.refund_via_gateway(folio, "pay_1", 200, "Guest cancelled")
		self.assertEqual(out["status"], "approved")
		provider.refund.assert_called_once_with(
			settings, reference="pay_1", amount=200.0, reason="Guest cancelled")
		self.assertEqual(len(self.rows(folio)), 1)

	def test_refund_via_gateway_refuses_more_than_is_left_of_the_payment(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.record_gateway_refund(folio, "refund:pay_1:r1", 400, "Payment Link", "x")
		provider = MagicMock()
		self.gateway(provider)
		with self.assertRaises(frappe.ValidationError):
			payments.refund_via_gateway(folio, "pay_1", 200, "Guest cancelled")
		provider.refund.assert_not_called()

	def test_refund_via_gateway_needs_a_payment_on_the_folio(self):
		_, folio = self.booking()
		provider = MagicMock()
		self.gateway(provider)
		with self.assertRaises(frappe.ValidationError):
			payments.refund_via_gateway(folio, "pay_9", 100, "Guest cancelled")
		provider.refund.assert_not_called()

	def test_refund_via_gateway_without_gateway_refunds_is_not_offered(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		self.gateway(object())  # a provider with no refund()
		with self.assertRaises(frappe.ValidationError):
			payments.refund_via_gateway(folio, "pay_1", 100, "Guest cancelled")
