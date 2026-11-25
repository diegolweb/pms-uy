"""El recibo del hotel al cobrar la reserva (HR-349, D-HR-74), contra la base: un pago aprobado deja un recibo
numerado por Property y sin huecos; el mismo aviso dos veces, uno solo; la devolución, su constancia negativa; y el
correo de confirmación lo enlaza. HR-359, con una segunda conexión de verdad: el recibo no espera el cerrojo del
inventario (la fila de la Property), dos cobros a la vez siguen la serie y el que no salió lo emite el barrido. Lo que
dice el recibo, sin base: tests/unit/receipts."""

import email
from contextlib import contextmanager
from email import policy
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.utils import add_to_date, now_datetime

from kamra import payments as kamra_payments
from kamra.folio import open_folio
from kamra.tests.audit_fixtures import SAVEPOINT, d, make_guest, make_reservation, make_room_type
from kamra_uy.payments import webhook
from kamra_uy.payments.tests.fixtures import OTHER, PROP, CobrosCase, event, make_uy_property, signed
from kamra_uy.receipts import issue, model

PAY_2 = "7a2b0c11-1111-4222-8333-999999999999"
# HR-359: una Property CONFIRMADA en la base, para que otra conexión pueda bloquear su fila o cobrar en su serie
LOCKED = "UT UY Recibos Cerrojo"
INVENTORY_LOCK = "kamra.kamra.doctype.reservation.reservation.lock_property_for_inventory"


class TestReceipts(CobrosCase):
	properties = (PROP, OTHER, LOCKED)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_uy_property(LOCKED)
		issue.ensure_series(LOCKED)  # como el barrido en producción: el contador ya existe cuando llega el cobro
		frappe.db.commit()
		cls.addClassCleanup(cls._drop_locked)

	@classmethod
	def _drop_locked(cls):
		frappe.db.rollback()
		frappe.db.delete(issue.DOCTYPE, {"property": LOCKED})
		frappe.db.delete("Property Setting", {"property": LOCKED})
		frappe.db.sql("delete from `tabSeries` where name = %s", issue._series_key(LOCKED))
		frappe.delete_doc("Property", LOCKED, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Property", PROP, {"legal_name": "UT Hotel Recibos SA", "gstin": "210000000019",
		                                       "short_code": "UTR"})
		# los folios se llaman igual en cada test (la serie se deshace): solo cuentan los Error Log de este test
		self._old_logs = set(frappe.get_all("Error Log", filters={"method": issue.LOG_TITLE}, pluck="name"))

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

	# --- (4) HR-359: el recibo no comparte cerrojo con el inventario, y el que no salió sale en el barrido ---

	@contextmanager
	def other_transaction(self):
		"""Otra conexión a la base, con su propia transacción (otro proceso de Kamra). No usa secondary_connection de
		IntegrationTestCase: la primera vez guarda como «conexión de antes» la nueva y el test sigue en ella."""
		primary = frappe.local.db
		if getattr(self, "_other", None) is None:
			frappe.connect(set_admin_as_user=False)
			self._other, frappe.local.db = frappe.local.db, primary
			self.addCleanup(self._close_other)
			with self.other_transaction():
				frappe.db.sql("set session innodb_lock_wait_timeout = 3")
		frappe.local.db = self._other
		try:
			yield
		finally:
			frappe.local.db = primary

	def _close_other(self):
		self._other.rollback()
		self._other.close()
		self._other = None

	def fresh_transaction(self) -> None:
		"""Una transacción nueva para esta conexión: la de la clase arrastra los cerrojos de los tests anteriores
		(ROLLBACK TO SAVEPOINT no los suelta) y la otra conexión esperaría por ellos. Al final, también la suelta."""
		frappe.db.rollback()
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(frappe.db.rollback)

	def stay_on_locked(self) -> str:
		"""Una reserva confirmada de LOCKED y su folio, sin tomar la fila de la Property (Reservation.validate la toma:
		este test la necesita libre para que la bloquee la otra conexión)."""
		with patch(INVENTORY_LOCK):
			rt = make_room_type(LOCKED, "UYL")
			res = make_reservation(LOCKED, rt, d(5), d(7), status="Confirmed", guest=make_guest("UT Cerrojo"))
			return open_folio(res)

	def pay_at_desk(self, folio: str, amount: float) -> None:
		doc = frappe.get_doc("Folio", folio)
		doc.append("payments", {"posting_date": d(0), "payment_kind": "Advance", "mode": "Cash", "amount": amount})
		doc.save(ignore_permissions=True)

	def lock_wait(self, seconds: int) -> None:
		"""Esta conexión se rinde a los `seconds` de esperar un cerrojo (por defecto, 50)."""
		previous = frappe.db.sql("select @@session.innodb_lock_wait_timeout")[0][0]
		frappe.db.sql(f"set session innodb_lock_wait_timeout = {int(seconds)}")
		self.addCleanup(frappe.db.sql, f"set session innodb_lock_wait_timeout = {int(previous)}")

	def failed_logs(self, folio: str) -> list:
		logs = frappe.get_all("Error Log", filters={"method": issue.LOG_TITLE, "reference_name": folio},
		                      fields=["name", "error"], order_by="creation asc")
		return [log for log in logs if log.name not in self._old_logs]

	def test_a_payment_while_the_inventory_holds_the_property_gets_its_receipt_without_waiting(self):
		self.fresh_transaction()
		folio = self.stay_on_locked()
		with self.other_transaction():  # otra transacción decide inventario: tiene la fila de la Property
			frappe.db.sql("select name from `tabProperty` where name = %s for update", LOCKED)
		try:
			self.lock_wait(3)  # si el recibo esperara ese cerrojo, se rendiría aquí y el cobro saldría sin recibo
			self.pay_at_desk(folio, 30.0)
		finally:
			with self.other_transaction():
				frappe.db.rollback()
		row = frappe.get_doc("Folio", folio).payments[0]
		(r,) = self.receipts(folio=folio)
		self.assertEqual((r.payment_row, r.amount, r.property), (row.name, 30.0, LOCKED))
		self.assertEqual(self.failed_logs(folio), [])

	def test_two_payments_at_once_keep_the_series_without_gaps(self):
		self.fresh_transaction()
		folio = self.stay_on_locked()  # este cobro ya leyó: su instantánea es de antes del otro
		with self.other_transaction():  # otro cobro de la misma Property emite su recibo y confirma, en medio
			before = frappe.db.sql("select `current` from `tabSeries` where name = %s", issue._series_key(LOCKED))[0][0]
			taken = issue._next_number(LOCKED)
			frappe.db.sql(f"""insert into `tab{issue.DOCTYPE}` (name, creation, modified, owner, modified_by, kind,
			                  number, receipt_no, property, payment_row, amount, currency)
			                  values (%s, now(), now(), 'Administrator', 'Administrator', %s, %s, %s, %s, %s, 1, 'USD')""",
			              (model.doc_name(LOCKED, taken), model.RECEIPT, taken, model.number_label(taken), LOCKED,
			               f"ut-otro-cobro-{taken}"))
			frappe.db.commit()
		self.addCleanup(self._drop_other_payment, model.doc_name(LOCKED, taken), before)
		self.lock_wait(3)
		self.pay_at_desk(folio, 30.0)
		self.pay_at_desk(folio, 20.0)
		mine = self.receipts(folio=folio)
		self.assertEqual([(r.number, r.amount) for r in mine], [(taken + 1, 30.0), (taken + 2, 20.0)])
		self.assertEqual(self.failed_logs(folio), [])
		self.assertEqual(len(frappe.get_doc("Folio", folio).payments), 2)  # la transacción del cobro sigue entera

	def _drop_other_payment(self, name: str, before: int) -> None:
		frappe.db.rollback()  # suelta el contador (fresh_transaction lo haría después: aquí hace falta antes)
		with self.other_transaction():
			frappe.db.delete(issue.DOCTYPE, {"name": name})
			frappe.db.sql("update `tabSeries` set `current` = %s where name = %s", (before, issue._series_key(LOCKED)))
			frappe.db.commit()

	def test_a_receipt_that_failed_comes_out_in_the_next_sweep_with_the_next_number_and_only_once(self):
		res, folio = self.held()
		with patch.object(issue, "_next_number", side_effect=RuntimeError("cerrojo agotado (ut)")):
			self.receive(event("payment.approved", folio, amount="110.00"))
			self.receive(event("payment.refunded", folio, amount="40.00"))  # la devolución, antes que el recibo
		paid = frappe.get_doc("Folio", folio).payments[0]
		self.assertEqual(self.receipts(reservation=res), [])
		(log,) = self.failed_logs(folio)
		self.assertIn(f"payment_row={paid.name} kind={model.RECEIPT}", log.error)
		self.assertNotIn("UT Huésped Cobros", log.error)
		# otro cobro de la Property, mientras, se lleva el número siguiente
		_, other = self.held()
		self.receive(event("payment.approved", other, payment_id=PAY_2, amount="50.00"))
		(between,) = self.receipts(folio=other)

		self.assertEqual(issue.sweep()["issued"], 1)
		self.assertEqual(issue.sweep(), {"issued": 0, "resolved": 0, "failed": 0})
		self.receive(event("payment.approved", folio, amount="110.00"))  # el aviso repetido tampoco hace otro
		receipt, note = self.receipts(reservation=res)
		self.assertEqual((receipt.kind, receipt.payment_row, receipt.number, receipt.amount),
		                 (model.RECEIPT, paid.name, between.number + 1, 110.0))
		self.assertEqual((note.kind, note.refund_of, note.number, note.amount),
		                 (model.REFUND, receipt.name, between.number + 2, -40.0))
		self.assertEqual(str(receipt.issued_on), str(paid.posting_date))
		(log,) = self.failed_logs(folio)
		self.assertIn(f"{issue.RESOLVED} {receipt.name}, {note.name}", log.error)

	def test_the_sweep_also_takes_a_failed_receipt_logged_before_hr359(self):
		res, folio = self.held()
		with patch.object(issue, "pending_note", side_effect=lambda error, *a: error):  # el Error Log de HR-349
			with patch.object(issue, "_next_number", side_effect=RuntimeError("ut")):
				self.receive(event("payment.approved", folio, amount="110.00"))
		(log,) = self.failed_logs(folio)
		self.assertEqual(log.error, "RuntimeError")
		self.assertEqual(issue.sweep()["issued"], 1)
		(r,) = self.receipts(reservation=res)
		self.assertEqual((r.payment_row, r.amount), (frappe.get_doc("Folio", folio).payments[0].name, 110.0))

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
