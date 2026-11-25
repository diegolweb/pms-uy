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

	# --- the folio's Refund button (HR-124) ---------------------------------

	def desk_payment(self, folio, amount, reference):
		"""A payment typed at the desk, slip number and all - not the gateway's."""
		doc = frappe.get_doc("Folio", folio)
		doc.append("payments", {"posting_date": d(0), "payment_kind": "Payment", "mode": "Card",
		                        "amount": amount, "reference": reference})
		doc.save(ignore_permissions=True)

	def key(self):
		"""A fresh idempotency key: the cache that remembers answers is not
		rolled back with the test, and a folio name can come round again."""
		return frappe.generate_hash()

	def refunding(self, *answers):
		"""The property's gateway gives money back, answering each refund()
		in turn (the last answer repeats)."""
		provider = MagicMock()
		provider.refund.side_effect = lambda *a, **k: dict(
			answers[min(provider.refund.call_count, len(answers)) - 1])
		self.gateway(provider)
		return provider

	def test_settle_marks_a_payment_the_gateway_names_as_its_own(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.settle_payment_link(folio, "plink_2", 300)  # the link only: no payment to ask back
		by_ref = {r.reference: r for r in self.rows(folio)}
		self.assertEqual(by_ref["pay_1"].idempotency_key, payments.gateway_payment_key("pay_1"))
		self.assertFalse(by_ref["plink_2"].idempotency_key)

	def test_gateway_refunds_lists_the_gateway_s_payments_with_what_is_left(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.settle_payment_link(folio, "plink_2", 300, reference="pay_2")
		payments.settle_payment_link(folio, "plink_3", 80)
		payments.record_gateway_refund(folio, "refund:pay_1:r1", 200, "Payment Link", "x")
		payments.record_gateway_refund(folio, "refund:pay_2:r1", 300, "Payment Link", "x")
		self.desk_payment(folio, 100, "4321")
		self.refunding({"status": "approved"})
		self.assertEqual(payments.gateway_refunds(folio), {
			"gateway": "Razorpay",
			"payments": [{"reference": "pay_1", "amount": 500.0, "left": 300.0}],
		})

	def test_gateway_refunds_is_empty_when_the_gateway_gives_nothing_back(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		frappe.local.message_log = []
		# no gateway configured for the property: nothing to offer, nothing said
		self.assertEqual(payments.gateway_refunds(folio), {"gateway": None, "payments": []})
		self.assertEqual(frappe.local.message_log, [])
		self.gateway(object())  # a provider with no refund()
		self.assertEqual(payments.gateway_refunds(folio), {"gateway": None, "payments": []})

	def test_gateway_refunds_of_a_closed_folio_is_empty(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		frappe.db.set_value("Folio", folio, "status", "Closed")
		self.refunding({"status": "approved"})
		self.assertEqual(payments.gateway_refunds(folio), {"gateway": None, "payments": []})

	def test_a_refund_asked_twice_with_one_key_reaches_the_gateway_once(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		provider = self.refunding({"status": "pending", "message": "asked"})
		key = self.key()
		first = payments.refund_via_gateway(folio, "pay_1", 200, "Guest cancelled", idempotency_key=key)
		again = payments.refund_via_gateway(folio, "pay_1", 200, "Guest cancelled", idempotency_key=key)
		self.assertEqual(provider.refund.call_count, 1)
		self.assertEqual(first, {"status": "pending", "message": "asked"})
		self.assertEqual(again, {"status": "pending", "message": "asked", "idempotent_replay": 1})
		# another key is another refund
		payments.refund_via_gateway(folio, "pay_1", 100, "Second one", idempotency_key=self.key())
		self.assertEqual(provider.refund.call_count, 2)
		# no key: every call asks, as before
		payments.refund_via_gateway(folio, "pay_1", 50, "No key")
		payments.refund_via_gateway(folio, "pay_1", 50, "No key")
		self.assertEqual(provider.refund.call_count, 4)

	def test_a_replay_answers_even_once_the_refund_is_on_the_folio(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		provider = self.refunding({"status": "approved"})
		key = self.key()
		payments.refund_via_gateway(folio, "pay_1", 500, "Guest cancelled", idempotency_key=key)
		# the gateway reported it: nothing is left of the payment
		payments.record_gateway_refund(folio, "refund:pay_1:r1", 500, "Payment Link", "x")
		again = payments.refund_via_gateway(folio, "pay_1", 500, "Guest cancelled", idempotency_key=key)
		self.assertEqual(again, {"status": "approved", "idempotent_replay": 1})
		self.assertEqual(provider.refund.call_count, 1)

	def test_a_refused_refund_is_asked_again_with_the_same_key(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		provider = self.refunding({"status": "rejected", "message": "no"}, {"status": "approved"})
		key = self.key()
		self.assertEqual(payments.refund_via_gateway(
			folio, "pay_1", 200, "Guest cancelled", idempotency_key=key)["status"], "rejected")
		self.assertEqual(payments.refund_via_gateway(
			folio, "pay_1", 200, "Guest cancelled", idempotency_key=key), {"status": "approved"})
		self.assertEqual(provider.refund.call_count, 2)

	def says_why(self, lang, messages):
		"""Each refusal of refund_via_gateway reads in the desk's language:
		messages has its text per refusal, {0} the payment or what is left."""
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		payments.record_gateway_refund(folio, "refund:pay_1:r1", 400, "Payment Link", "x")
		provider = self.refunding({"status": "approved"})
		from kamra.localization import money_text
		left = money_text(PROP_A, 100)
		cases = [
			((folio, "pay_1", 0, "Guest cancelled"), messages["positive"]),
			((folio, "pay_1", 50, "  "), messages["reason"]),
			((folio, "pay_9", 50, "Guest cancelled"), messages["no_payment"].format("pay_9")),
			((folio, "pay_1", 200, "Guest cancelled"), messages["left"].format(left)),
			(None, messages["closed"]),
		]
		for args, message in cases:
			if args is None:
				frappe.db.set_value("Folio", folio, "status", "Closed")
				args = (folio, "pay_1", 50, "Guest cancelled")
			with self.subTest(lang=lang, message=message):
				frappe.local.lang = lang
				try:
					with self.assertRaises(frappe.ValidationError) as caught:
						payments.refund_via_gateway(*args)
				finally:
					frappe.local.lang = "en"
				self.assertEqual(str(caught.exception), message)
		provider.refund.assert_not_called()

	def test_refund_via_gateway_says_why_in_the_desk_s_language(self):
		self.says_why("es", {
			"positive": "El importe de la devolución debe ser positivo.",
			"reason": "El motivo de la devolución es obligatorio.",
			"no_payment": "Este folio no tiene ningún pago {0} de la pasarela.",
			"left": "De este pago solo quedan {0} por devolver.",
			"closed": "Este folio está cerrado.",
		})

	def test_refund_via_gateway_says_why_in_brazilian_portuguese(self):
		# a Brazilian desk (pt-BR) reads Kamra's pt rows
		self.says_why("pt-BR", {
			"positive": "O valor do reembolso deve ser positivo.",
			"reason": "O motivo do reembolso é obrigatório.",
			"no_payment": "Este fólio não tem nenhum pagamento {0} do gateway de pagamento.",
			"left": "Só restam {0} deste pagamento para reembolsar.",
			"closed": "Este fólio está fechado.",
		})

	# --- a transaction the database dropped (HR-365) -------------------------
	# A deadlock, or the 1020 of MariaDB's snapshot isolation, rolls back the
	# whole transaction, savepoint included, and Frappe raises it as
	# QueryDeadlockError. Money posted on the folio is gone with it, so these
	# must fail (and be retried), never report the money as posted.

	def lost(self):
		return frappe.QueryDeadlockError("Record has changed since last read in table 'tabSeries'")

	def full_rollbacks(self) -> list:
		"""Count frappe.db.rollback() calls without a savepoint and do nothing
		for them (one would end the test's savepoint); a rollback to a
		savepoint, the tearDown's included, still runs."""
		real, calls = frappe.local.db.rollback, []

		def rollback(*args, save_point=None, **kwargs):
			if save_point:
				return real(*args, save_point=save_point, **kwargs)
			calls.append(1)

		self._stack.enter_context(patch.object(frappe.local.db, "rollback", side_effect=rollback))
		return calls

	def test_a_gateway_refund_raises_when_its_transaction_is_lost(self):
		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		for n, where in enumerate(("kamra.savings._insert", "kamra.ledger.record_payment_ledger"), 1):
			with self.subTest(lost_in=where), patch(where, side_effect=self.lost()), \
					self.assertRaises(frappe.QueryDeadlockError):
				payments.record_gateway_refund(folio, f"refund:pay_1:r{n}", 100, "Payment Link", "x")

	def test_a_link_payment_raises_when_its_transaction_is_lost(self):
		_, folio = self.booking()
		for n, where in enumerate(("kamra.savings._insert", "kamra.ledger.record_payment_ledger"), 1):
			with self.subTest(lost_in=where), patch(where, side_effect=self.lost()), \
					self.assertRaises(frappe.QueryDeadlockError):
				payments.settle_payment_link(folio, f"plink_{n}", 100, reference=f"pay_{n}")

	def test_refund_via_gateway_logs_its_ask_again_when_the_transaction_is_lost(self):
		# nothing of its own is lost (the folio row comes from the gateway's
		# notice): the desk still gets the gateway's answer, and the ask is logged
		from kamra import savings

		_, folio = self.booking()
		payments.settle_payment_link(folio, "plink_1", 500, reference="pay_1")
		provider = self.refunding({"status": "approved"})
		rollbacks = self.full_rollbacks()
		real, tried = savings._insert, []

		def insert(row):
			tried.append(row["action_type"])
			if len(tried) == 1:
				raise self.lost()
			return real(row)

		with patch("kamra.savings._insert", side_effect=insert):
			out = payments.refund_via_gateway(folio, "pay_1", 200, "Guest cancelled")
		self.assertEqual(out, {"status": "approved"})
		self.assertEqual(provider.refund.call_count, 1)
		self.assertEqual(len(rollbacks), 1)
		(row,) = frappe.get_all("Agent Action Log",
		                        {"action_type": "gateway_refund_requested", "reference_name": folio},
		                        ["reference_doctype", "rationale"])
		self.assertEqual(row.reference_doctype, "Folio")
		self.assertIn("asked back at Razorpay: approved", row.rationale)
		self.assertNotIn("log fallback", row.rationale)
