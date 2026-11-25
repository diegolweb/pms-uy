"""La seña perdida (HR-347) contra Kamra de verdad y un core simulado: la cancelación fuera de plazo de una reserva
pagada deja un CFE «Forfeit» por lo retenido con el 22 % dentro, el no-show de la auditoría deja uno solo, la empresa
con RUT va en e-Factura, la devolución después es una nota de crédito y, sin documento por encima del tope, queda en
Facturas pendientes con «falta el documento del comprador» hasta que recepción lo carga."""

import json
from unittest.mock import patch

import frappe
from kamra import folio as folio_mod
from kamra.tests.audit_fixtures import d, make_guest, make_reservation

from kamra_uy.fiscal import api, connector, forfeit
from kamra_uy.fiscal.tests.test_connector import FakeCore, FiscalCase, _Resp
from kamra_uy.payments.tests.fixtures import PROP


class ForfeitCore(FakeCore):
	"""El core de las pruebas del conector, más la regla de la seña (HR-344: `forfeit` → 22 %, sena_retenida) y el
	tope del e-Ticket sin documento (compute.ts, checkBuyer) en `cap`."""

	def __init__(self):
		super().__init__()
		self.cap: float | None = None

	def request(self, method, url, data=None, headers=None, timeout=None):
		body = json.loads(data) if data else None
		path = url[len("http://core.test"):]
		if path == "/v1/fiscal/iva/determine" and any(ln["kind"] == "forfeit" for ln in body["lines"]):
			self.calls.append((method, path, body, headers or {}))
			return _Resp(200, {"rules_version": "test-1", "lines": [
				{"line_id": ln["line_id"], "rate": "22", "reason_code": "sena_retenida", "rule_id": "sena_retenida",
				 "legal_ref": "x", "pending_accountant": False, "tax_category": "basic", "sources": []}
				for ln in body["lines"]]})
		if (path == "/v1/fiscal/cfe" and method == "POST" and self.cap is not None
		        and body["buyer"]["type"] == "consumer" and "doc_number" not in body["buyer"]
		        and sum(float(ln["unit_amount"]) for ln in body["lines"]) > self.cap):
			self.calls.append((method, path, body, headers or {}))
			error = {"path": "/body/buyer/doc_number", "message": "por el monto tiene que identificar al comprador"}
			return _Resp(422, {"code": "validation_failed", "status": 422, "title": "x", "type": "about:blank",
			                   "request_id": "r", "errors": [error]})
		return super().request(method, url, data=data, headers=headers, timeout=timeout)


class TestForfeit(FiscalCase):
	def setUp(self):
		super().setUp()
		self.core = ForfeitCore()
		self._stack.enter_context(patch.object(connector, "_session", return_value=self.core))

	def booked(self, paid: float | None = None, **fields):
		"""Una reserva confirmada (aún no llegó) con lo pagado por adelantado en su cuenta; sin `paid`, todo."""
		guest = fields.pop("guest", None) or make_guest("UT Seña", residence_country="Argentina",
		                                                uy_document_country="Argentina", id_type="Passport",
		                                                id_number="AB123456")
		res = make_reservation(PROP, self.rt, d(3), d(5), room=self.room, guest=guest, **fields)
		folio = frappe.get_doc("Folio", folio_mod.open_folio(res))
		folio.append("payments", {"posting_date": d(0), "payment_kind": "Advance", "mode": "Tarjeta extranjera",
		                          "amount": paid if paid is not None else float(res.amount_after_tax or 0)})
		folio_mod._recalculate(folio)
		folio.save(ignore_permissions=True)
		return frappe.get_doc("Reservation", res.name)

	@staticmethod
	def refund(res, amount: float) -> None:
		folio = frappe.get_doc("Folio", connector._guest_folio(res.name))
		if amount > 0.005:
			folio.append("payments", {"posting_date": d(0), "payment_kind": "Refund", "mode": "Tarjeta extranjera",
			                          "amount": -round(amount, 2)})
		folio_mod._recalculate(folio)
		folio.save(ignore_permissions=True)

	def cancel_late(self, res):
		"""Lo que hace kamra.api._do_cancel dentro del plazo: el cargo de la política y la reserva «Cancelled»."""
		folio_mod.post_policy_fee(res, "Full Stay", "Cancellation fee (Full Stay)")
		res = frappe.get_doc("Reservation", res.name)
		res.status = "Cancelled"
		frappe.flags.kamra_cancelling = True  # como _do_cancel: la guarda de Reservation exige pasar por ahí
		try:
			res.save(ignore_permissions=True)
		finally:
			frappe.flags.kamra_cancelling = False
		return res

	def fee_total(self, res) -> float:
		folio = frappe.get_doc("Folio", connector._guest_folio(res.name))
		return round(sum(float(c.total) for c in folio.charges if forfeit.is_forfeit_charge(c.as_dict())), 2)

	def forfeits(self, res, **filters) -> list:
		return frappe.get_all(connector.DOCTYPE, filters={"reservation": res.name, "purpose": forfeit.PURPOSE,
		                                                  "kind": ["!=", "Reconcile"], **filters},
		                      fields=["*"], order_by="creation asc, name asc")

	def sent_forfeits(self) -> list[dict]:
		return [b for b in self.core.bodies("/v1/fiscal/cfe") if b["lines"][0]["line_id"].startswith("forfeit-")]

	def test_a_late_cancellation_of_a_paid_booking_bills_what_is_retained_with_22_inside_once(self):
		res = self.booked()
		res = self.cancel_late(res)
		total = self.fee_total(res)
		self.assertGreater(total, 0)
		[task] = frappe.get_all(connector.DOCTYPE, filters={"reservation": res.name, "kind": "Reconcile"},
		                        fields=["purpose", "status"])
		self.assertEqual((task.purpose, task.status), (forfeit.PURPOSE, forfeit.QUEUED))
		self.assertIn("kamra_uy.fiscal.forfeit.process", [c.args[0] for c in self.enqueue.call_args_list])

		forfeit.process(res.name)
		forfeit.process(res.name)
		[row] = self.forfeits(res)
		self.assertEqual((row.kind, row.status, row.cfe_type), ("Original", "Sent", "e-ticket"))
		[body] = self.sent_forfeits()
		self.assertTrue(body["prices_include_iva"])
		self.assertEqual([(ln["unit_amount"], ln["rate"]) for ln in body["lines"]], [(f"{total:.2f}", "22")])
		[asked] = [b for b in self.core.bodies("/v1/fiscal/iva/determine") if b["lines"][0]["kind"] == "forfeit"]
		self.assertEqual(asked["lines"][0]["amount"], f"{total:.2f}")

		# la cuenta de una reserva que no se alojó no se vuelve a facturar al cerrarla
		self.refund(res, float(res.amount_after_tax) - total)
		folio_mod.close_folio(connector._guest_folio(res.name))
		self.assertFalse(frappe.get_all(connector.DOCTYPE, filters={"reservation": res.name, "purpose": "Close"}))
		forfeit.process(res.name)
		self.assertEqual(len(self.forfeits(res)), 1)

	def test_a_no_show_from_the_night_audit_leaves_one_document_even_if_the_hook_runs_twice(self):
		res = self.booked()
		# kamra.folio.apply_no_show: la reserva pasa a «No Show» sin sus ganchos y el cargo guarda el folio
		frappe.db.set_value("Reservation", res.name, "status", "No Show")
		res = frappe.get_doc("Reservation", res.name)
		folio_mod.post_policy_fee(res, "First Night", "No-show charge (First Night)", posting_date=d(0))
		folio = frappe.get_doc("Folio", connector._guest_folio(res.name))
		forfeit.on_folio_update(folio)
		forfeit.on_folio_update(folio)
		self.assertEqual(len(frappe.get_all(connector.DOCTYPE, filters={"reservation": res.name,
		                                                                  "kind": "Reconcile"})), 1)
		forfeit.process(res.name)
		forfeit.sweep()
		[row] = self.forfeits(res)
		self.assertEqual(row.status, "Sent")
		self.assertIn("no presentarse", self.sent_forfeits()[0]["lines"][0]["description"])

	def test_a_company_with_rut_gets_an_e_factura(self):
		company = frappe.get_doc({"doctype": "Corporate Account", "company_name": "UT Viajes SA",
		                          "gstin": "21-999999-0016"}).insert(ignore_permissions=True).name
		res = self.cancel_late(self.booked(company=company))
		forfeit.process(res.name)
		[row] = self.forfeits(res)
		self.assertEqual(row.cfe_type, "e-factura")
		self.assertEqual(self.sent_forfeits()[0]["buyer"]["rut"], "219999990016")

	def test_a_waived_fee_bills_nothing_and_a_refund_afterwards_is_a_credit_note(self):
		waived = self.cancel_late(self.booked())
		folio = connector._guest_folio(waived.name)
		[fee] = [c.name for c in frappe.get_doc("Folio", folio).charges if c.description.startswith("Cancellation")]
		folio_mod.void_charge(folio, fee, "Exonerado por el Gerente")
		forfeit.process(waived.name)
		self.assertEqual(self.forfeits(waived), [])

		res = self.cancel_late(self.booked())
		total = self.fee_total(res)
		forfeit.process(res.name)
		# vuelve todo lo pagado de más y, además, 30 de la seña
		self.refund(res, float(res.amount_after_tax) - total + 30)
		forfeit.process(res.name)
		original, credit = self.forfeits(res)
		self.assertEqual((credit.kind, credit.references_cfe, credit.status), ("Credit note", original.name, "Sent"))
		self.assertEqual(json.loads(credit.lines_json), [{"line_id": "forfeit-1", "amount": "30.00"}])
		self.assertEqual(credit.reason, forfeit.REASON_REFUND)

	def test_over_the_cap_without_a_document_it_waits_in_pending_invoices_until_the_document_is_loaded(self):
		guest = make_guest("UT Seña Sin Documento")
		res = self.cancel_late(self.booked(guest=guest))
		self.core.cap = 1.0
		forfeit.process(res.name)
		[row] = self.forfeits(res)
		self.assertEqual((row.status, row.error), ("Needs attention", forfeit.DOC_MISSING))
		[item] = [i for i in api.pending(PROP)["items"] if i["reservation"] == res.name]
		self.assertEqual(item["state"], forfeit.DOC_MISSING)
		# otro cambio de la cuenta no duplica lo que espera el documento
		forfeit.on_folio_update(frappe.get_doc("Folio", connector._guest_folio(res.name)))
		self.assertEqual(len(self.forfeits(res)), 1)

		g = frappe.get_doc("Guest", guest)
		# el país del documento lo completa (HR-346): sin él, el pasaporte no dice de qué país es
		g.update({"residence_country": "Argentina", "uy_document_country": "Argentina", "id_type": "Passport",
		          "id_number": "AB123456"})
		g.save(ignore_permissions=True)
		forfeit.process(res.name)
		old, new = self.forfeits(res)
		self.assertEqual((old.status, new.status), ("Not needed", "Sent"))
		self.assertEqual(self.sent_forfeits()[-1]["buyer"]["doc_number"], "AB123456")
		self.assertNotIn(res.name, [i["reservation"] for i in api.pending(PROP)["items"]])

	def test_the_buyer_document_carries_the_country_that_issued_it_not_the_residence(self):
		# HR-346 con HR-347: un uruguayo con pasaporte que vive en Argentina; la seña lleva su documento uruguayo
		guest = make_guest("UT Seña Uruguayo", residence_country="Argentina", uy_document_country="Uruguay",
		                   id_type="Passport", id_number="UY765432")
		res = self.cancel_late(self.booked(guest=guest))
		self.core.cap = 1.0
		forfeit.process(res.name)
		[row] = self.forfeits(res)
		self.assertEqual(row.status, "Sent")
		buyer = self.sent_forfeits()[-1]["buyer"]
		self.assertEqual((buyer["country"], buyer["doc_type"], buyer["doc_number"]), ("UY", "passport", "UY765432"))
