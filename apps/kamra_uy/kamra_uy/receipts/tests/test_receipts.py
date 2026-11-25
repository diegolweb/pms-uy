"""El recibo del hotel al cobrar la reserva (HR-349, D-HR-74), contra la base: un pago aprobado deja un recibo
numerado por Property y sin huecos; el mismo aviso dos veces, uno solo; la devolución, su constancia negativa; y el
correo de confirmación lo enlaza. Lo que dice el recibo, sin base: tests/unit/receipts."""

import email
from email import policy
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.utils import add_to_date, now_datetime

from kamra import payments as kamra_payments
from kamra.folio import open_folio
from kamra.tests.audit_fixtures import d, make_guest, make_reservation, make_room_type
from kamra_uy.payments import webhook
from kamra_uy.payments.tests.fixtures import OTHER, PROP, CobrosCase, event, signed
from kamra_uy.receipts import issue, model

PAY_2 = "7a2b0c11-1111-4222-8333-999999999999"


class TestReceipts(CobrosCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_value("Property", PROP, {"legal_name": "UT Hotel Recibos SA", "gstin": "210000000019",
		                                       "short_code": "UTR"})

	def receive(self, body: dict) -> dict:
		return webhook.receive(*signed(body))

	def receipts(self, **filters) -> list:
		return frappe.get_all(issue.DOCTYPE, filters=filters, fields=["*"], order_by="number asc")

	def held(self, **fields) -> tuple[str, str]:
		return self.booking("Pending Payment", hold_expires_on=add_to_date(now_datetime(), minutes=30), **fields)

	# --- (1) uno por pago, correlativo por Property y sin huecos ---

	def test_an_approved_payment_leaves_one_receipt_and_the_same_event_twice_still_one(self):
		res, folio = self.held()
		body = event("payment.approved", folio, amount="110.00")
		self.receive(body)
		self.receive(body)  # el webhook repetido (mismo aviso)
		self.receive(event("payment.approved", folio, amount="110.00"))  # otro aviso del mismo pago
		(r,) = self.receipts(reservation=res)
		row = frappe.get_doc("Folio", folio).payments[0]
		self.assertEqual((r.kind, r.payment_row, r.folio, r.property), (model.RECEIPT, row.name, folio, PROP))
		self.assertEqual(frappe.db.get_value("Reservation", res, "status"), "Confirmed")

	def test_numbers_run_one_by_one_per_property_without_gaps(self):
		start = len(self.receipts(property=PROP))
		other_start = len(self.receipts(property=OTHER))
		_, f1 = self.held()
		self.receive(event("payment.approved", f1, amount="50.00"))
		self.receive(event("payment.approved", f1, payment_id=PAY_2, amount="60.00"))
		# un cobro que se deshace (el aviso cae después de asentar) no se lleva su número
		frappe.db.savepoint("ut_receipt_undone")
		_, f2 = self.held()
		kamra_payments.settle_payment_link(f2, "link-ut-undone", 70.0, reference="pay-ut-undone")
		self.assertEqual(len(self.receipts(property=PROP)), start + 3)
		frappe.db.rollback(save_point="ut_receipt_undone")
		_, f3 = self.held()
		self.receive(event("payment.approved", f3, amount="80.00"))
		# la otra Property lleva su propia serie
		rt = make_room_type(OTHER, "UYO")
		other = make_reservation(OTHER, rt, d(5), d(7), status="Held", guest=make_guest("UT Otra"))
		kamra_payments.settle_payment_link(open_folio(other), "link-ut-other", 90.0, reference="pay-ut-other")

		ours = self.receipts(property=PROP)
		self.assertEqual([r.number for r in ours], list(range(1, start + 4)))
		self.assertEqual([r.receipt_no for r in ours[-3:]], [f"R-{n}" for n in range(start + 1, start + 4)])
		self.assertEqual([r.amount for r in ours[-3:]], [50.0, 60.0, 80.0])
		self.assertEqual(ours[-1].name, f"UTR-R-{start + 3}")
		theirs = self.receipts(property=OTHER)
		self.assertEqual([r.number for r in theirs], list(range(1, other_start + 2)))

	def test_money_after_check_in_or_a_security_deposit_gets_no_receipt(self):
		res, folio = self.booking("Confirmed")
		# en casa (el check-in de Uruguay pide la residencia, HR-304: aquí basta el estado)
		frappe.db.set_value("Reservation", res, "status", "Checked In")
		doc = frappe.get_doc("Folio", folio)
		doc.append("payments", {"posting_date": d(5), "payment_kind": "Payment", "mode": "Cash", "amount": 40.0})
		doc.save(ignore_permissions=True)
		res2, folio2 = self.booking("Confirmed")
		doc = frappe.get_doc("Folio", folio2)
		doc.append("payments", {"posting_date": d(0), "payment_kind": "Security Deposit", "mode": "Cash",
		                        "amount": 100.0})
		doc.append("payments", {"posting_date": d(0), "payment_kind": "Advance", "mode": "Cash", "amount": 30.0,
		                        "reference": "deposit:ut"})
		doc.save(ignore_permissions=True)
		self.assertEqual(self.receipts(reservation=res), [])
		(r,) = self.receipts(reservation=res2)  # el cobro en mostrador antes del check-in, sí
		self.assertEqual((r.amount, r.mode), (30.0, "Cash"))

	# --- (2) lo que lleva ---

	def test_it_carries_issuer_payer_date_amount_currency_pesos_charged_and_the_stay(self):
		res, folio = self.held()
		self.receive(event("payment.approved", folio, amount="110.00",
		                   charged={"amount": "4478.14", "currency": "UYU", "exchange_rate": "40.710364"}))
		(r,) = self.receipts(reservation=res)
		stay = frappe.db.get_value("Reservation", res, ["check_in_date", "check_out_date", "guest_name"], as_dict=True)
		self.assertEqual((r.issuer_name, r.issuer_rut, r.amount, r.currency, r.charged_amount, r.charged_currency),
		                 ("UT Hotel Recibos SA", "210000000019", 110.0, "USD", 4478.14, "UYU"))
		self.assertEqual((r.payer_name, str(r.check_in_date), str(r.check_out_date)),
		                 ("UT Huésped Cobros", str(stay.check_in_date), str(stay.check_out_date)))
		self.assertEqual(str(r.issued_on), frappe.utils.nowdate())
		rows = dict(model.view(r)["filas"])
		self.assertEqual(rows["Concepto"], f"Pago de la reserva {res}, estadía del {model.fecha(stay.check_in_date)} "
		                                   f"al {model.fecha(stay.check_out_date)}")
		self.assertEqual(rows["Cobrado en pesos"], "$ 4.478,14")

	# --- (3) la devolución ---

	def test_a_refund_leaves_its_note_with_the_amount_negative(self):
		res, folio = self.held()
		self.receive(event("payment.approved", folio, amount="110.00"))
		refund = event("payment.refunded", folio, amount="40.00")
		self.receive(refund)
		self.receive(refund)
		paid, back = self.receipts(reservation=res)
		self.assertEqual((back.kind, back.amount, back.refund_of, back.number),
		                 (model.REFUND, -40.0, paid.name, paid.number + 1))

	def test_a_desk_refund_against_the_payment_row_also_leaves_its_note(self):
		res, folio = self.booking("Confirmed")
		doc = frappe.get_doc("Folio", folio)
		doc.append("payments", {"posting_date": d(0), "payment_kind": "Advance", "mode": "Cash", "amount": 30.0})
		doc.save(ignore_permissions=True)
		doc.reload()
		doc.append("payments", {"posting_date": d(0), "payment_kind": "Refund", "mode": "Cash", "amount": -30.0,
		                        "refund_of": doc.payments[0].name})
		doc.save(ignore_permissions=True)
		paid, back = self.receipts(reservation=res)
		self.assertEqual((back.kind, back.amount, back.refund_of), (model.REFUND, -30.0, paid.name))

	# --- el correo y la página ---

	def test_the_confirmation_mail_links_the_receipt_and_the_page_needs_its_key(self):
		self.outbox()  # una cuenta saliente solo dentro del test (hr-gate no tiene)
		res, folio = self.held(source="Website")
		self.receive(event("payment.approved", folio, amount="110.00"))
		(r,) = self.receipts(reservation=res)
		(queued,) = frappe.get_all("Email Queue", {"reference_doctype": "Reservation", "reference_name": res},
		                           pluck="name")
		msg = email.message_from_string(frappe.get_doc("Email Queue", queued).message, policy=policy.default)
		html = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html")
		self.assertIn("data-recibo", html)
		self.assertIn(r.receipt_no, html)
		link = urlparse(r.receipt_url)
		self.assertEqual(link.path, "/recibo")
		self.assertIn(link.geturl().replace("&", "&amp;"), html)
		q = parse_qs(link.query)
		self.assertEqual(issue.by_key(q["n"][0], q["k"][0]).name, r.name)
		self.assertIsNone(issue.by_key(r.name, "0" * 32))
		self.assertIsNone(issue.by_key(r.name, None))

	def test_the_reservation_shows_its_receipts(self):
		data = frappe.get_meta("Reservation").get_dashboard_data()
		self.assertTrue(any(issue.DOCTYPE in (g.get("items") or []) for g in data.transactions))
		self.assertEqual(data.non_standard_fieldnames.get(issue.DOCTYPE, data.fieldname), "reservation")
