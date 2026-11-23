"""El conector fiscal de Kamra (HR-28) contra Kamra de verdad y un core simulado: IVA 0 y CFE en el check-in de un
no residente con la copia del documento, core caído sin parar a recepción, ajuste al cerrar, nota de crédito al anular,
aviso cfe.updated, bloque CFE del folio y el descuento de la estancia repartido por noches (HR-235)."""

import json
import uuid
from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.utils import add_days
from kamra import api as kamra_api
from kamra.tests.audit_fixtures import (
	AuditTestCase,
	d,
	ensure_user,
	make_guest,
	make_reservation,
	make_room,
	make_room_type,
)

from kamra_uy.fiscal import api, connector, model, sync, texts, webhook
from kamra_uy.payments.tests.fixtures import CODE, PROP, SECRET, gateway_settings, make_uy_property, signed

CORE = "http://core.test"
# domingo de Pascua de los años que usan las pruebas: los vectores de services/core/src/fiscal/iva/season.ts (HR-234),
# que es quien decide de verdad la alta temporada (16-nov al domingo de Pascua, los dos inclusive)
EASTER = {"2025": "04-20", "2026": "04-05", "2027": "03-28"}


def high_season(day: str) -> bool:
	month_day = day[5:]
	return month_day >= "11-16" or month_day <= EASTER[day[:4]]


class _Resp:
	def __init__(self, status: int, body: dict):
		self.status_code = status
		self.content = json.dumps(body).encode()


class FakeCore:
	"""Lo que contesta core (HR-25 y HR-26) para lo que usa el conector."""

	def __init__(self):
		self.calls, self.cfes, self.by_key, self.down, self.seq = [], {}, {}, False, 0
		# sin Gsoft conectado (HR-304): core guarda cada CFE en contingencia, sin serie ni número
		self.contingency = False
		# la alta temporada (D-HR-40, Decreto 267/001): el consumidor final que no está exento paga el 10 % solo en sus
		# noches. `season`, las noches de temporada a mano; `seasons`, el calendario de verdad (high_season, HR-235);
		# sin ninguno de los dos, todas las noches lo son
		self.season: set[str] | None = None
		self.seasons = False

	def _high_season(self, day: str) -> bool:
		if self.season is not None:
			return day in self.season
		return not self.seasons or high_season(day)

	def _rate(self, guest: dict, buyer: dict, day: str) -> tuple[str, str, str]:
		if buyer.get("type") == "rut":  # el comprador con RUT paga el 10 todo el año, resida donde resida
			return "10", "residente_hospedaje", "minimum"
		if guest["residence_country"] != "UY" and (guest.get("document") or {}).get("copy_on_file"):
			return "0", "no_residente", "exempt"
		if not self._high_season(day):
			return "0", "hospedaje_fuera_temporada", "exempt"
		return "10", "residente_hospedaje", "minimum"

	@staticmethod
	def _negative_rate(lines: list[dict]) -> bool:
		"""Core no emite un CFE si un descuento deja en negativo el total de una tasa (fiscal/cfe/compute.ts)."""
		totals: dict[str, float] = {}
		for ln in lines:
			totals[ln["rate"]] = totals.get(ln["rate"], 0) + float(ln["unit_amount"]) * float(ln["quantity"])
		return any(total < -0.005 for total in totals.values())

	def paths(self, method: str = "POST") -> list[str]:
		return [p for m, p, _b, _h in self.calls if m == method]

	def bodies(self, path: str) -> list[dict]:
		return [b for _m, p, b, _h in self.calls if p == path]

	def _cfe(self, kind: str, total, **extra) -> dict:
		self.seq += 1
		now = f"2026-12-20T15:00:{self.seq:02d}Z"
		cfe = {"cfe_id": str(uuid.uuid4()), "status": "issued", "kind": kind, "serie": "A", "number": str(self.seq),
		       "cae": "90123456789", "security_code": "aB3dE5", "cae_range": {"from": "1", "to": "1000"},
		       "cae_expires_on": "2027-12-31", "qr_url": f"https://www.dgi.gub.uy/ctdcfe?n={self.seq}",
		       "created_at": now, "updated_at": now, "currency": "USD", "total": f"{total:.2f}", **extra}
		if self.contingency:
			cfe.update(status="contingency", serie=None, number=None, cae=None, security_code=None, qr_url=None,
			           contingency_since=now)
		self.cfes[cfe["cfe_id"]] = cfe
		return cfe

	def request(self, method, url, data=None, headers=None, timeout=None):
		path = url[len(CORE):]
		body = json.loads(data) if data else None
		self.calls.append((method, path, body, headers or {}))
		if self.down:
			raise OSError("core is down")
		key = (headers or {}).get("Idempotency-Key")
		if key and key in self.by_key:
			return _Resp(202, self.by_key[key])
		if path == "/v1/fiscal/iva/determine":
			out = []
			for ln in body["lines"]:
				rate, reason, category = self._rate(body["guest"], body.get("buyer") or {}, ln["date"])
				out.append({"line_id": ln["line_id"], "rate": rate, "reason_code": reason, "rule_id": reason,
				            "legal_ref": "x", "pending_accountant": True, "tax_category": category, "sources": []})
			return _Resp(200, {"rules_version": "test-1", "lines": out})
		if path == "/v1/fiscal/cfe" and method == "POST" and self._negative_rate(body["lines"]):
			return _Resp(422, {"code": "validation_failed", "status": 422, "title": "x", "type": "about:blank",
			                   "request_id": "r", "errors": [{"path": "/body/lines", "message":
			                                                  "un descuento no puede dejar en negativo el total de una tasa"}]})
		if path == "/v1/fiscal/cfe" and method == "POST":
			total = sum(float(ln["unit_amount"]) * float(ln["quantity"]) for ln in body["lines"])
			kind = "e-factura" if body["buyer"]["type"] == "rut" else "e-ticket"
			out = self._cfe("original", total, type=kind, folio=body["folio"], invoice_number=body["invoice_number"])
		elif path.endswith("/credit-note"):
			original = self.cfes[path.split("/")[4]]
			total = sum(float(ln["amount"]) for ln in body.get("lines") or []) or float(original["total"])
			out = self._cfe("credit_note", total, type=f"nc-{original['type']}", references_cfe_id=original["cfe_id"])
		elif path.endswith("/manual-issue"):
			out = self.cfes[path.split("/")[4]]
			out.update(status="issued", issued_via=body["via"], serie=body["serie"], number=body["number"],
			           issued_at=body["issued_at"], contingency_since=None)
		elif method == "GET" and path.startswith("/v1/fiscal/cfe/"):
			return _Resp(200, self.cfes[path.split("/")[4]])
		else:
			return _Resp(404, {"code": "not_found", "status": 404, "title": "x", "type": "about:blank",
			                   "request_id": "r"})
		if key:
			self.by_key[key] = out
		return _Resp(202, out)


class FiscalCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		make_uy_property()
		self.rt = make_room_type(PROP, "UYF", base_price=100)
		# Kamra no deja hacer el check-in sin habitación asignada (Reservation._occupied_rooms): como en recepción
		self.room = make_room(PROP, self.rt, "UYF-101")
		gateway_settings()
		frappe.get_doc({"doctype": "Exchange Rate", "property": PROP, "currency": "UYU", "rate_date": d(0),
		                "buy_rate": 0.025, "sell_rate": 0.025, "market_rate": 0.025}).insert(ignore_permissions=True)
		self.core = FakeCore()
		self._stack.enter_context(patch.object(connector, "_session", return_value=self.core))
		self._stack.enter_context(patch.object(connector, "core_base_url", return_value=CORE))
		self.enqueue = self._stack.enter_context(patch.object(connector.frappe, "enqueue"))

	def stay(self, residence="Argentina", copy=True, nights=2):
		fields = {"residence_country": residence, "id_type": "DNI", "id_number": "30111222"}
		if copy:
			fields["id_file"] = "/private/files/ut-dni.png"
		guest = make_guest("UT Huésped Fiscal", **fields)
		return make_reservation(PROP, self.rt, d(0), d(nights), status="Checked In", room=self.room, guest=guest)

	def documents(self, res, kind=None) -> list:
		filters = {"reservation": res.name, "kind": kind or ["!=", "Reconcile"]}
		return frappe.get_all(connector.DOCTYPE, filters=filters, fields=["*"], order_by="creation asc, name asc")

	def waiting(self, res) -> list:
		return frappe.get_all(connector.DOCTYPE, filters={"reservation": res.name, "kind": "Reconcile",
		                                                  "status": "Pending"}, fields=["name", "error"])

	def settle_and_close(self, res, nights: int, start: str | None = None) -> str:
		from kamra.folio import _recalculate, close_folio, post_room_night

		res = frappe.get_doc("Reservation", res.name)
		for i in range(nights):
			post_room_night(res, str(add_days(start, i)) if start else d(i))
		folio = frappe.get_doc("Folio", connector._guest_folio(res.name))
		_recalculate(folio)
		folio.append("payments", {"posting_date": d(0), "payment_kind": "Payment", "mode": "Tarjeta extranjera",
		                          "amount": folio.balance})
		_recalculate(folio)
		folio.save(ignore_permissions=True)
		close_folio(folio.name)
		return folio.name


class TestCheckIn(FiscalCase):
	def test_a_non_resident_with_the_copy_pays_iva_0_and_gets_the_cfe_at_check_in(self):
		res = self.stay()
		res.reload()
		self.assertTrue(res.night_rates)
		self.assertEqual({float(r.gst_rate) for r in res.night_rates}, {0.0})
		asked = self.core.bodies("/v1/fiscal/iva/determine")[0]
		self.assertEqual(asked["guest"], {"residence_country": "AR",
		                                  "document": {"type": "dni", "country": "AR", "copy_on_file": True}})
		self.assertEqual(asked["property"], CODE)
		self.enqueue.assert_called()

		out = connector.process_reservation(res.name)
		self.assertEqual(out, {"sent": 1, "pending": 0, "attention": 0})
		[cfe] = self.documents(res)
		self.assertEqual((cfe.kind, cfe.status, cfe.core_status, cfe.serie), ("Original", "Sent", "issued", "A"))
		sent = self.core.bodies("/v1/fiscal/cfe")[0]
		self.assertEqual([ln["rate"] for ln in sent["lines"]], ["0", "0"])
		self.assertEqual(sent["invoice_number"], f"{res.name}/1")
		self.assertEqual(sent["fx_rate"], "40")
		self.assertEqual(sent["buyer"]["doc_type"], "dni")

		block = api.folio_cfe(connector._guest_folio(res.name))
		self.assertFalse(block["contingency"])
		[doc] = block["documents"]
		self.assertEqual((doc["title"], doc["serie"], doc["number"]), ("e-Ticket", "A", cfe.number))
		self.assertTrue(doc["qr_url"].startswith("https://www.dgi.gub.uy/"))

	def test_without_the_copy_of_the_document_the_guest_pays_the_resident_rate(self):
		res = self.stay(copy=False)
		res.reload()
		self.assertEqual({float(r.gst_rate) for r in res.night_rates}, {10.0})

	def booked(self, residence="Argentina", copy=True, nights=3, **fields):
		"""Una reserva confirmada, antes del check-in: lo que cotizó Kamra (la web, recepción)."""
		guest = {"residence_country": residence, "id_type": "DNI", "id_number": "30111222"}
		if copy:
			guest["id_file"] = "/private/files/ut-dni.png"
		return make_reservation(PROP, self.rt, d(0), d(nights), room=self.room,
		                        guest=make_guest("UT Huésped Fiscal", **guest), **fields)

	def check_in_and_bill(self, res) -> tuple[list, list]:
		"""El check-in, el CFE y las noches en el folio: ([(importe, IVA)] del folio, [(importe, IVA)] del CFE)."""
		from kamra.folio import post_room_night

		res.status = "Checked In"
		res.save(ignore_permissions=True)
		connector.process_reservation(res.name)
		res = frappe.get_doc("Reservation", res.name)
		nights = (frappe.utils.getdate(res.check_out_date) - frappe.utils.getdate(res.check_in_date)).days
		for i in range(nights):
			post_room_night(res, d(i))
		# de todas las cuentas de la reserva: con empresa, la habitación puede ir a la suya (default_company_billing)
		charges = [c for f in frappe.get_all("Folio", filters={"reservation": res.name}, pluck="name")
		           for c in frappe.get_doc("Folio", f).charges if c.charge_type == "Room"]
		charged = [(round(float(c.amount), 2), float(c.gst_rate))
		           for c in sorted(charges, key=lambda c: str(c.posting_date))]
		[sent] = self.core.bodies("/v1/fiscal/cfe")
		billed = [(round(float(ln["unit_amount"]) * float(ln["quantity"]), 2), float(ln["rate"])) for ln in sent["lines"]]
		return charged, billed

	def assert_quoted_without_iva(self, res):
		self.assertEqual({float(r.gst_rate) for r in res.night_rates}, {0.0})
		self.assertEqual((float(res.tax_amount or 0), float(res.amount_after_tax)), (0.0, float(res.amount_before_tax)))

	def test_the_stay_is_quoted_without_iva_and_check_in_adds_what_core_bills_night_by_night(self):
		# HR-239: la reserva se cotiza sin IVA para todos; en el check-in el folio suma el 10 % donde core lo factura
		# (HR-28): al residente, solo en las noches de temporada (aquí, todas menos la primera; D-HR-40)
		res = self.booked(copy=False)  # sin la copia del documento, core lo trata como residente
		self.assert_quoted_without_iva(res)
		self.core.season = {d(1), d(2)}
		charged, billed = self.check_in_and_bill(res)
		self.assertEqual([rate for _, rate in charged], [0.0, 10.0, 10.0])
		self.assertEqual(charged, billed)
		res.reload()
		self.assertAlmostEqual(float(res.tax_amount), sum(float(r.rate) * float(r.gst_rate) / 100
		                                                  for r in res.night_rates), places=2)

	def test_a_foreigner_pays_no_iva_at_check_in_either(self):
		res = self.booked()
		self.assert_quoted_without_iva(res)
		charged, billed = self.check_in_and_bill(res)
		self.assertEqual([rate for _, rate in charged], [0.0, 0.0, 0.0])
		self.assertEqual(charged, billed)
		self.assertEqual(float(frappe.db.get_value("Reservation", res.name, "tax_amount") or 0), 0.0)

	def test_a_buyer_with_rut_pays_the_10_on_every_night(self):
		company = frappe.get_doc({"doctype": "Corporate Account", "company_name": "UT Viajes SA",
		                          "gstin": "21-999999-0016"}).insert(ignore_permissions=True).name
		res = self.booked(company=company)  # el huésped es extranjero con la copia: sin el RUT, pagaría 0
		self.assert_quoted_without_iva(res)
		self.core.season = {d(1)}  # el RUT no mira la temporada
		charged, billed = self.check_in_and_bill(res)
		self.assertEqual([rate for _, rate in charged], [10.0, 10.0, 10.0])
		self.assertEqual(charged, billed)
		self.assertEqual(self.core.bodies("/v1/fiscal/iva/determine")[0]["buyer"], {"type": "rut"})

	def test_a_core_down_at_check_in_leaves_no_iva_until_the_cfe_task_brings_it(self):
		self.core.down = True
		res = self.stay(copy=False)
		res.reload()
		self.assert_quoted_without_iva(res)
		self.core.down = False
		connector.process_reservation(res.name)
		res.reload()
		self.assertEqual({float(r.gst_rate) for r in res.night_rates}, {10.0})
		before = float(res.amount_before_tax)
		self.assertAlmostEqual(float(res.tax_amount), before * 0.10, places=2)
		self.assertAlmostEqual(float(res.amount_after_tax), before * 1.10, places=2)
		[sent] = self.core.bodies("/v1/fiscal/cfe")
		self.assertEqual({ln["rate"] for ln in sent["lines"]}, {"10"})

	def test_a_core_that_is_down_does_not_stop_check_in_and_the_folio_shows_it(self):
		self.core.down = True
		res = self.stay()
		self.assertEqual(frappe.db.get_value("Reservation", res.name, "status"), "Checked In")
		out = connector.process_reservation(res.name)
		self.assertEqual(out["sent"], 0)
		block = api.folio_cfe(connector._guest_folio(res.name))
		self.assertTrue(block["contingency"])
		self.assertTrue(block["status_text"])
		[task] = self.documents(res, kind="Reconcile")
		self.assertEqual(task.status, "Pending")
		self.assertTrue(task.error)

		self.core.down = False
		sync.sweep()
		[cfe] = self.documents(res)
		self.assertEqual((cfe.status, cfe.core_status), ("Sent", "issued"))
		self.assertFalse(api.folio_cfe(connector._guest_folio(res.name))["contingency"])


class TestClosingAndVoiding(FiscalCase):
	def test_without_the_residence_the_cfe_waits_and_goes_out_once_the_desk_gives_it(self):
		# HR-304: un huésped en casa sin país de residencia (de antes de HR-304, o borrado después) no se factura como
		# residente: la tarea espera, el folio lo dice, y sale en cuanto recepción indica el país
		res = self.stay()
		frappe.db.set_value("Guest", res.guest, "residence_country", None)  # sin ganchos, como un dato viejo
		connector.process_reservation(res.name)
		self.assertEqual(self.core.bodies("/v1/fiscal/cfe"), [])
		pending = self.waiting(res)
		self.assertEqual([t.error for t in pending], [texts.RESIDENCE_MISSING])
		block = api.folio_cfe(connector._guest_folio(res.name))
		self.assertTrue(block["residence_missing"])
		self.assertEqual(block["status_text"], texts.RESIDENCE_MISSING)
		self.assertTrue(api.preview(res.name)["residence_missing"])

		self.enqueue.reset_mock()
		guest = frappe.get_doc("Guest", res.guest)
		guest.residence_country = "Argentina"
		guest.save(ignore_permissions=True)
		self.assertTrue(any(c.kwargs.get("reservation") == res.name for c in self.enqueue.call_args_list))
		connector.process_reservation(res.name)
		self.assertEqual(self.waiting(res), [])
		self.assertEqual(len(self.core.bodies("/v1/fiscal/cfe")), 1)
		self.assertFalse(api.folio_cfe(connector._guest_folio(res.name))["residence_missing"])

	def test_without_gsoft_the_desk_sees_the_pending_invoice_and_records_the_one_made_in_the_portal(self):
		# HR-304: lo que queda en contingencia sale en /facturas con lo que hay que copiar en el portal, y recepción
		# registra la serie y el número del portal: deja de estar pendiente y core ya no lo emitirá
		self.core.contingency = True
		res = self.stay()
		connector.process_reservation(res.name)
		[item] = [i for i in api.pending(PROP)["items"] if i["reservation"] == res.name]
		self.assertTrue(item["in_core"])
		self.assertEqual(item["codes"], [101, 201])
		self.assertEqual(item["title"], "e-Ticket")
		self.assertTrue(item["lines"])
		self.assertEqual(item["state"], model.STATUS_TEXTS["contingency"])
		self.assertEqual(api.pending(PROP, "2000-01-01")["items"], [])
		desk = ensure_user("recepcion-facturas@roosevelt.test", ["Front Desk"])
		frappe.set_user(desk)  # nosemgrep: frappe-setuser -- test fixture
		try:
			out = api.record_manual(item["name"], "portal", 201, "a", "0001234", "2026-12-07T10:30")
		finally:
			frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		self.assertEqual((out["serie"], out["number"], out["core_status"]), ("A", "1234", "issued"))
		core_id = frappe.db.get_value(connector.DOCTYPE, item["name"], "core_cfe_id")
		[body] = self.core.bodies(f"/v1/fiscal/cfe/{core_id}/manual-issue")
		self.assertEqual(body, {"via": "portal", "type_code": 201, "serie": "A", "number": "1234",
		                        "issued_at": "2026-12-07T10:30:00-03:00"})
		self.assertNotIn(res.name, [i["reservation"] for i in api.pending(PROP)["items"]])
		with self.assertRaises(frappe.ValidationError):
			api.record_manual(item["name"], "portal", 201, "A", "1235", "2026-12-07T10:31")
		with self.assertRaises(frappe.ValidationError):
			api.record_manual(item["name"], "fax", 201, "A", "1235", "2026-12-07T10:31")

	def test_the_page_of_pending_invoices_is_for_hotel_staff(self):
		from kamra_uy.www import facturas as page

		ctx = page.get_context(frappe._dict())
		self.assertIn(PROP, ctx.choices + [ctx.property])
		nobody = ensure_user("sin-rol-facturas@roosevelt.test", ["Housekeeping"])
		for user in (nobody, "Guest"):
			frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture
			try:
				with self.assertRaises(frappe.PermissionError):
					page.get_context(frappe._dict())
			finally:
				frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture

	def test_closing_what_was_billed_at_check_in_issues_nothing_more(self):
		res = self.stay()
		connector.process_reservation(res.name)
		self.settle_and_close(res, 2)
		connector.process_reservation(res.name)
		self.assertEqual(len(self.documents(res)), 1)
		tasks = self.documents(res, kind="Reconcile")
		self.assertEqual([t.status for t in tasks], ["Done", "Not needed"])

	def test_voiding_the_bill_issues_the_credit_note_of_the_cfe(self):
		res = self.stay()
		connector.process_reservation(res.name)
		folio = self.settle_and_close(res, 2)
		connector.process_reservation(res.name)
		res.reload()
		res.status = "Checked Out"
		res.db_set("status", "Checked Out", update_modified=False)

		from kamra.folio import cancel_invoice

		cancel_invoice(folio, "Nombre equivocado")
		connector.process_reservation(res.name)
		original, credit = self.documents(res)
		self.assertEqual((credit.kind, credit.status, credit.references_cfe), ("Credit note", "Sent", original.name))
		self.assertEqual(credit.cfe_type, "nc-e-ticket")
		self.assertIn("Nombre equivocado", credit.reason)
		sent = self.core.bodies(f"/v1/fiscal/cfe/{original.core_cfe_id}/credit-note")[0]
		self.assertEqual(sum(float(ln["amount"]) for ln in sent["lines"]), float(original.total))

	def test_undoing_the_check_in_credits_the_cfe(self):
		res = self.stay()
		connector.process_reservation(res.name)
		frappe.db.set_value("Reservation", res.name, "status", "Confirmed")
		connector.queue_task(frappe.get_doc("Reservation", res.name), "Void", None, texts.REASON_CHECK_IN_UNDONE)
		connector.process_reservation(res.name)
		original, credit = self.documents(res)
		self.assertEqual((original.kind, credit.kind, credit.reason), ("Original", "Credit note",
		                                                              texts.REASON_CHECK_IN_UNDONE))


class TestTheCfeOnTheFolio(FiscalCase):
	"""HR-307: recepción ve el CFE de la reserva en su folio (kamra.api.folio_invoice, `fiscal_documents`), pendiente,
	en contingencia o emitido con su serie y número, y el aviso de la residencia; y llega a /facturas desde el menú
	(kamra.api.property_locale, `desk_pages`) y desde la propia tarjeta mientras algo no esté emitido."""

	PAGE = "/facturas?property=" + quote(PROP)

	def card(self, res) -> dict | None:
		return kamra_api.folio_invoice(connector._guest_folio(res.name))["fiscal_documents"]

	def test_the_issued_cfe_shows_with_its_type_series_and_number_before_the_bill_is_settled(self):
		res = self.stay()
		connector.process_reservation(res.name)
		[cfe] = self.documents(res)
		bill = kamra_api.folio_invoice(connector._guest_folio(res.name))
		card = bill["fiscal_documents"]
		self.assertEqual((card["title"], card["state"], card["notice"], card["link"]),
		                 (model.DESK_TITLE, "issued", "", None))
		[doc] = card["documents"]
		self.assertEqual((doc["label"], doc["state"], doc["number"]),
		                 ("e-Ticket", "issued", f"Serie A, número {cfe.number}"))
		# la factura de Kamra sigue siendo provisional y no lleva bloque impreso: el CFE no es el de ZATCA
		self.assertIsNone(bill["statutory"])
		self.assertFalse(bill["document"]["is_final"])

	def test_a_cfe_in_contingency_shows_as_such_and_leads_to_the_pending_invoices_until_it_is_recorded(self):
		self.core.contingency = True
		res = self.stay()
		connector.process_reservation(res.name)
		card = self.card(res)
		self.assertEqual(card["state"], "contingency")
		[doc] = card["documents"]
		self.assertEqual((doc["state"], doc["number"], doc["text"]), ("contingency", None, model.DESK_CONTINGENCY))
		self.assertEqual(card["link"], {"label": model.PENDING_PAGE, "url": self.PAGE})

		[item] = [i for i in api.pending(PROP)["items"] if i["reservation"] == res.name]
		api.record_manual(item["name"], "portal", 201, "A", "0001234", "2026-12-07T10:30")
		card = self.card(res)
		self.assertEqual((card["state"], card["link"]), ("issued", None))
		self.assertEqual(card["documents"][0]["number"], "Serie A, número 1234")

	def test_a_core_that_does_not_answer_leaves_the_cfe_pending(self):
		self.core.down = True
		res = self.stay()
		connector.process_reservation(res.name)
		card = self.card(res)
		self.assertEqual((card["state"], card["notice"]), ("pending", ""))
		self.assertEqual(card["link"], {"label": model.PENDING_PAGE, "url": self.PAGE})

	def test_without_the_residence_the_folio_says_what_to_do(self):
		res = self.stay()
		frappe.db.set_value("Guest", res.guest, "residence_country", None)  # sin ganchos, como un dato viejo
		connector.process_reservation(res.name)
		card = self.card(res)
		self.assertEqual((card["state"], card["notice"], card["documents"]), ("pending", texts.RESIDENCE_MISSING, []))
		self.assertEqual(card["link"], {"label": model.PENDING_PAGE, "url": self.PAGE})

	def test_a_booking_not_checked_in_has_no_card(self):
		res = make_reservation(PROP, self.rt, d(1), d(3), room=self.room)
		folio = connector._guest_folio(res.name)
		if folio:
			self.assertIsNone(kamra_api.folio_invoice(folio)["fiscal_documents"])
		self.assertIsNone(api.folio_fiscal_documents("FOLIO-QUE-NO-EXISTE"))

	def test_the_menu_takes_the_desk_to_the_pending_invoices(self):
		self.assertEqual(kamra_api.property_locale(PROP)["desk_pages"], [
			{"label": model.PENDING_PAGE, "url": self.PAGE,
			 "roles": ["Front Desk", "Finance", "Hotel Admin", "System Manager", "Administrator"]}])


class TestDiscountByNight(FiscalCase):
	"""HR-235: el descuento de la reserva se reparte entre las noches, proporcional al importe de cada una y con el
	redondeo en la última, y cada trozo va con la fecha de su noche, para que core le ponga la tasa de esa noche."""

	def setUp(self):
		super().setUp()
		self.core.seasons = True

	@staticmethod
	def nights(res) -> list:
		return sorted(res.night_rates, key=lambda row: str(row.stay_date))

	def resident_stay(self, check_in: str, check_out: str, discount: float, rates=(), room: str | None = None):
		"""Un residente (consumidor final) con su descuento, que hace el check-in como en recepción."""
		guest = make_guest("UT Huésped Residente", residence_country="Uruguay", id_type="National ID",
		                   id_number="19119187")
		res = make_reservation(PROP, self.rt, check_in, check_out, room=room or self.room, guest=guest)
		for row, rate in zip(self.nights(res), rates, strict=False):
			frappe.db.set_value("Reservation Night Rate", row.name, "rate", rate)
		frappe.db.set_value("Reservation", res.name, "discount_amount", discount)
		res = frappe.get_doc("Reservation", res.name)
		res.flags.allow_past_check_in = True
		res.status = "Checked In"
		res.save(ignore_permissions=True)
		return res

	@staticmethod
	def by_rate(lines: list[dict]) -> dict[str, float]:
		out: dict[str, float] = {}
		for ln in lines:
			out[ln["rate"]] = out.get(ln["rate"], 0) + float(ln["unit_amount"])
		return out

	def test_each_night_takes_its_share_of_the_discount_and_the_last_one_the_rounding(self):
		res = make_reservation(PROP, self.rt, "2025-11-14", "2025-11-17", room=self.room)

		def pieces() -> list[tuple]:
			return [(ln["line_id"], ln["date"], ln["kind"], ln["amount"]) for ln in connector.stay_lines(res)
			        if ln["line_id"].endswith(":Discount")]

		for row, rate in zip(self.nights(res), (100, 100, 200), strict=True):
			row.rate = rate
		res.discount_amount = 50
		self.assertEqual(pieces(), [("2025-11-14:Discount", "2025-11-14", "room", "-12.50"),
		                            ("2025-11-15:Discount", "2025-11-15", "room", "-12.50"),
		                            ("2025-11-16:Discount", "2025-11-16", "room", "-25.00")])
		for row in res.night_rates:
			row.rate = 100
		res.discount_amount = 100
		self.assertEqual([p[3] for p in pieces()], ["-33.33", "-33.33", "-33.34"])
		# el importe de la noche es todo lo de esa noche: los huéspedes adicionales también cuentan
		for row, rate in zip(self.nights(res), (100, 100, 200), strict=True):
			row.rate = rate
		res.over_capacity_charge = 50
		res.discount_amount = 55
		self.assertEqual([p[3] for p in pieces()], ["-15.00", "-15.00", "-25.00"])
		# sin ninguna noche con importe no hay con qué repartir: el descuento va entero con la primera, como antes
		for row in res.night_rates:
			row.rate = 0
		res.over_capacity_charge = 0
		res.discount_amount = 10
		self.assertEqual(pieces(), [("2025-11-14:Discount", "2025-11-14", "room", "-10.00")])

	def test_across_the_season_each_piece_of_the_discount_takes_the_rate_of_its_night(self):
		cases = (("2025-11-14", "2025-11-17", ["0", "0", "10"]),   # 14 y 15-nov exentas, 16-nov al 10 %
		         ("2026-04-04", "2026-04-07", ["10", "10", "0"]))  # sábado y domingo de Pascua al 10 %, el lunes no
		for n, (check_in, check_out, rates) in enumerate(cases):
			with self.subTest(check_in=check_in):
				self.core.calls.clear()
				room = make_room(PROP, self.rt, f"UYF-2{n}")
				res = self.resident_stay(check_in, check_out, discount=60, rates=(100, 100, 200), room=room)
				res.reload()
				self.assertEqual([float(r.gst_rate) for r in self.nights(res)], [float(r) for r in rates])
				[asked] = self.core.bodies("/v1/fiscal/iva/determine")
				nights = [ln["date"] for ln in asked["lines"] if ln["line_id"].endswith(":Room")]
				self.assertEqual([ln["date"] for ln in asked["lines"] if ln["line_id"].endswith(":Discount")], nights)

				self.assertEqual(connector.process_reservation(res.name), {"sent": 1, "pending": 0, "attention": 0})
				[sent] = self.core.bodies("/v1/fiscal/cfe")
				rooms = [ln for ln in sent["lines"] if ln["line_id"].endswith(":Room")]
				pieces = [ln for ln in sent["lines"] if ln["line_id"].endswith(":Discount")]
				self.assertEqual([(ln["line_id"].split(":")[0], ln["rate"]) for ln in pieces],
				                 [(ln["line_id"].split(":")[0], ln["rate"]) for ln in rooms])
				self.assertEqual([ln["unit_amount"] for ln in pieces], ["-15.00", "-15.00", "-30.00"])
				# el IVA del descuento cuadra con el de las noches que rebaja: cada tasa baja lo mismo que la estancia
				# entera (60 de 400, el 15 %)
				self.assertEqual({r: -total / self.by_rate(rooms)[r] for r, total in self.by_rate(pieces).items()},
				                 {"0": 0.15, "10": 0.15})

				desired = connector.adjust_iva(res)["lines"]
				without = [ln for ln in desired if not ln["line_id"].endswith(":Discount")]
				self.assertEqual(model.room_rate_by_date(desired), model.room_rate_by_date(without))

	def test_a_discount_bigger_than_the_nights_before_the_season_no_longer_leaves_a_rate_negative(self):
		# 15-nov exenta y 16-nov al 10 %: con los 150 del descuento en la primera noche, la tasa exenta quedaba en -50 y
		# core no emitía el CFE
		res = self.resident_stay("2025-11-15", "2025-11-17", discount=150, rates=(100, 100))
		self.assertEqual(connector.process_reservation(res.name), {"sent": 1, "pending": 0, "attention": 0})
		[sent] = self.core.bodies("/v1/fiscal/cfe")
		self.assertEqual([(ln["line_id"], ln["unit_amount"], ln["rate"]) for ln in sent["lines"]
		                  if ln["line_id"].endswith(":Discount")],
		                 [("2025-11-15:Discount", "-75.00", "0"), ("2025-11-16:Discount", "-75.00", "10")])

	def test_with_core_down_at_check_in_the_cfe_task_brings_each_night_its_rate_and_its_share_of_the_discount(self):
		# HR-239: core no contestó en el check-in y las noches siguen con el IVA de la cotización; la tarea del CFE se lo
		# pone con lo que factura, y cada trozo del descuento, que va a la tasa de su noche, no le cambia la tasa a ninguna
		self.core.down = True
		res = self.resident_stay("2025-11-14", "2025-11-17", discount=60, rates=(100, 100, 200))
		res.reload()
		self.assertEqual({float(r.gst_rate) for r in res.night_rates}, {0.0})
		self.core.down = False
		self.assertEqual(connector.process_reservation(res.name), {"sent": 1, "pending": 0, "attention": 0})
		res.reload()
		self.assertEqual([float(r.gst_rate) for r in self.nights(res)], [0.0, 0.0, 10.0])
		[sent] = self.core.bodies("/v1/fiscal/cfe")
		self.assertEqual([(ln["line_id"], ln["unit_amount"], ln["rate"]) for ln in sent["lines"]
		                  if ln["line_id"].endswith(":Discount")],
		                 [("2025-11-14:Discount", "-15.00", "0"), ("2025-11-15:Discount", "-15.00", "0"),
		                  ("2025-11-16:Discount", "-30.00", "10")])

	def test_at_the_close_the_bill_spreads_the_discount_like_the_check_in(self):
		res = self.resident_stay("2025-11-14", "2025-11-17", discount=60, rates=(100, 100, 200))
		connector.process_reservation(res.name)
		[sent] = self.core.bodies("/v1/fiscal/cfe")
		folio = frappe.get_doc("Folio", self.settle_and_close(res, 3, start="2025-11-14"))
		bill = connector.account_lines([folio])
		self.assertEqual([(ln["date"], ln["amount"]) for ln in bill if ln.get("charge")],
		                 [(ln["line_id"].split(":")[0], ln["unit_amount"]) for ln in sent["lines"]
		                  if ln["line_id"].endswith(":Discount")])
		core_property, client = connector.client_for(PROP)
		desired, _ = connector.determine(client, core_property, res, bill, connector.guest_inputs(res),
		                                 connector._currency(PROP), [])
		self.assertEqual([ln["rate"] for ln in desired if ln.get("charge")], ["0", "0", "10"])
		# Kamra cobra el cargo de descuento del folio a la tasa de la primera noche: el aviso de siempre lo cuenta
		self.assertEqual(connector._posted_at_other_rate([folio], desired), 1)

	def test_on_the_bill_the_discount_follows_the_nights_still_charged(self):
		def charge(name: str, kind: str, amount: float, day: str, **extra) -> frappe._dict:
			return frappe._dict(name=name, charge_type=kind, amount=amount, posting_date=day, description=kind, gst_rate=0,
			                    **extra)

		def lines(*charges) -> list[tuple]:
			return [(ln["line_id"], ln["date"], ln["amount"]) for ln in connector.account_lines(
				[frappe._dict(charges=list(charges))]) if ln["line_id"].startswith("dto")]

		# la noche anulada ya no lleva descuento
		self.assertEqual(lines(charge("r14", "Room", 100, "2025-11-14"), charge("r15", "Room", 100, "2025-11-15"),
		                       charge("r16", "Room", 200, "2025-11-16"),
		                       charge("v16", "Room", -200, "2025-11-17", reversal_of="r16"),
		                       charge("dto", "Discount", -60, "2025-11-14")),
		                 [("dto:2025-11-14", "2025-11-14", "-30.00"), ("dto:2025-11-15", "2025-11-15", "-30.00")])
		# sin noches en la cuenta, el descuento va como está
		self.assertEqual(lines(charge("dto", "Discount", -60, "2025-11-14")), [("dto", "2025-11-14", "-60.00")])


class TestCoreEvent(FiscalCase):
	def test_cfe_updated_brings_the_dgi_state_and_ignores_older_or_foreign_events(self):
		res = self.stay()
		connector.process_reservation(res.name)
		[row] = self.documents(res)
		cfe = {**self.core.cfes[row.core_cfe_id], "dgi_status": "accepted", "updated_at": "2026-12-20T16:00:00Z"}
		event = {"event_id": str(uuid.uuid4()), "type": "cfe.updated", "property": CODE, "cfe": cfe}
		self.assertEqual(webhook.receive(*signed(event, SECRET))["ok"], True)
		self.assertEqual(frappe.db.get_value(connector.DOCTYPE, row.name, "dgi_status"), "accepted")

		older = {**event, "event_id": str(uuid.uuid4()), "cfe": {**cfe, "updated_at": "2026-12-20T15:59:00Z"}}
		self.assertEqual(webhook.receive(*signed(older, SECRET)), {"duplicate": True})
		unknown = {**event, "event_id": str(uuid.uuid4()), "cfe": {**cfe, "cfe_id": str(uuid.uuid4())}}
		self.assertEqual(webhook.receive(*signed(unknown, SECRET)), {"ignored": True})
		with self.assertRaises(frappe.PermissionError):
			webhook.receive(*signed(event, "otro-secreto-que-no-es"))
		with self.assertRaises(webhook.InvalidEvent):
			webhook.receive(b"{}", {})
