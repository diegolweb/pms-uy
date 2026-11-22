"""No vender antes de la apertura (HR-263): el campo de la Property y el guardián de servidor de la reserva en línea.

Con `uy_opening_date` puesta, la búsqueda (search_stay) y la reserva (book) de la página pública rechazan una llegada
anterior con el motivo en el idioma de la página y sin crear nada; la del mismo día o posterior, como Kamra; la vitrina
(showcase) lleva la fecha; y una reserva Website creada por otro camino tampoco pasa. Sin la fecha (vacía, como nace),
nada cambia, y la recepción (una reserva que no es Website) no se frena nunca. Las llamadas se hacen como las hace la
web, también por el despacho de Frappe (frappe.handler.execute_cmd, el de /api/method), para probar que los hooks
llevan al guardián.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.booking.tests.test_opening`).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, nowdate
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_reservation, make_room, make_room_type

from kamra_uy.booking import opening
from kamra_uy.pure import opening as regla

PROP = "UT Apertura HR263"
OPENS = 10  # la apertura, d(10): los días se cuentan desde TODAY de audit_fixtures (en el futuro)


class TestOpening(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		make_property(PROP, country="Uruguay", currency="USD", booking_engine_enabled=1, booking_mode="Instant",
		              booking_payment_mode="Pay at hotel", advance_percent=0, registration_fee=0, cleaning_fee=0,
		              booking_language="", **{opening.FIELD: None})
		self.rt = make_room_type(PROP, "APE", base_price=100, adults_capacity=2)
		make_room(PROP, self.rt, "A101")
		make_room(PROP, self.rt, "A102")
		form, lang = frappe.local.form_dict, getattr(frappe.local, "lang", None)
		self.addCleanup(setattr, frappe.local, "form_dict", form)
		self.addCleanup(setattr, frappe.local, "lang", lang)
		frappe.local.form_dict = frappe._dict()

	# --- ayudas ---

	def opens(self, value) -> None:
		# «» como None: así lo guarda un Date de Frappe al salvar el documento (db.set_value no lo convierte)
		frappe.db.set_value("Property", PROP, opening.FIELD, value or None)
		frappe.clear_document_cache("Property", PROP)

	def search(self, check_in: int, nights: int = 2) -> list[dict]:
		return opening.search_stay(property=PROP, check_in_date=d(check_in), check_out_date=d(check_in + nights),
		                           adults=2, children=0)

	def book(self, check_in: int, nights: int = 2) -> dict:
		return opening.book(property=PROP, room_type=self.rt, check_in_date=d(check_in),
		                    check_out_date=d(check_in + nights), guest_name="UT Persona Apertura",
		                    phone=f"+5989963{uuid.uuid4().int % 10000:04d}", adults=2)

	def reservations(self) -> int:
		return frappe.db.count("Reservation", {"property": PROP})

	def dispatch(self, cmd: str, http: str = "GET", **form):
		"""Como /api/method/<cmd> sin sesión: el despacho de Frappe, con los hooks (frappe.handler.execute_cmd)."""
		from frappe.handler import execute_cmd

		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- la web llama sin sesión; tearDown vuelve a Administrator
		frappe.local.form_dict = frappe._dict({"cmd": cmd, **form})  # después: set_user vacía form_dict
		with patch.object(frappe.local, "request", SimpleNamespace(method=http), create=True):
			return execute_cmd(cmd)

	# --- el campo ---

	def test_el_campo_es_de_kamra_uy_y_nace_vacio(self):
		field = frappe.get_meta("Property").get_field(opening.FIELD)
		self.assertIsNotNone(field, "ensure_fields no creó uy_opening_date (after_install, after_migrate)")
		self.assertEqual(field.fieldtype, "Date")
		self.assertEqual(frappe.db.get_value("Custom Field", {"dt": "Property", "fieldname": opening.FIELD}, "module"),
		                 "Kamra UY")
		self.assertFalse(field.default)
		self.assertIsNone(opening.opening_date(PROP))
		self.opens(d(OPENS))
		self.assertEqual(opening.opening_date(PROP), getdate(d(OPENS)))

	def test_ensure_fields_es_idempotente(self):
		opening.ensure_fields()
		opening.ensure_fields()
		self.assertEqual(frappe.db.count("Custom Field", {"dt": "Property", "fieldname": opening.FIELD}), 1)

	def test_una_propiedad_que_no_existe_no_tiene_apertura(self):
		self.assertIsNone(opening.opening_date("UT No Existe HR263"))
		self.assertIsNone(opening.opening_date(None))

	# --- la búsqueda ---

	def test_search_stay_rechaza_una_llegada_anterior_a_la_apertura(self):
		self.opens(d(OPENS))
		for check_in in (OPENS - 1, OPENS - 5, 1):
			with self.assertRaises(opening.BeforeOpeningError) as cm:
				self.search(check_in, nights=OPENS)  # también la que acabaría después de la apertura
			self.assertEqual(str(cm.exception), regla.motivo(d(OPENS), "es"))

	def test_search_stay_permite_el_dia_de_la_apertura_y_despues(self):
		self.opens(d(OPENS))
		for check_in in (OPENS, OPENS + 1, OPENS + 30):
			rows = self.search(check_in)
			row = next(r for r in rows if r["room_type"] == self.rt)
			self.assertTrue(row["available"], row)

	def test_sin_apertura_o_con_una_pasada_no_hay_restriccion(self):
		for value in (None, "", add_days(nowdate(), -5)):
			self.opens(value)
			row = next(r for r in self.search(1) if r["room_type"] == self.rt)
			self.assertTrue(row["available"], (value, row))

	def test_el_motivo_sale_en_el_idioma_de_la_pagina(self):
		self.opens(d(OPENS))
		for lang, expected in (("pt", "pt"), ("en", "en"), ("es", "es"), ("fr", "es")):
			frappe.local.form_dict = frappe._dict({"_lang": lang})
			with self.assertRaises(opening.BeforeOpeningError) as cm:
				self.search(OPENS - 1)
			self.assertEqual(str(cm.exception), regla.motivo(d(OPENS), expected), lang)
		# sin el de la página, el de la propiedad
		frappe.local.form_dict = frappe._dict()
		frappe.db.set_value("Property", PROP, "booking_language", "en")
		with self.assertRaises(opening.BeforeOpeningError) as cm:
			self.search(OPENS - 1)
		self.assertEqual(str(cm.exception), regla.motivo(d(OPENS), "en"))

	# --- la vitrina ---

	def test_showcase_lleva_la_fecha_de_apertura(self):
		data = opening.showcase(property=PROP)
		self.assertIsNone(data[opening.FIELD])
		self.assertIn(self.rt, [t["name"] for t in data["room_types"]])
		self.opens(d(OPENS))
		self.assertEqual(opening.showcase(property=PROP)[opening.FIELD], d(OPENS))

	# --- la reserva ---

	def test_book_rechaza_una_llegada_anterior_y_no_crea_nada(self):
		self.opens(d(OPENS))
		before = self.reservations()
		with self.assertRaises(opening.BeforeOpeningError):
			self.book(OPENS - 1, nights=3)
		self.assertEqual(self.reservations(), before)

	def test_book_el_dia_de_la_apertura_reserva_como_kamra(self):
		self.opens(d(OPENS))
		out = self.book(OPENS)
		self.assertEqual(out["status"], "Confirmed", out)  # «Pay at hotel»: nada que cobrar ahora
		self.assertEqual(str(frappe.db.get_value("Reservation", out["reservation"], "check_in_date")), d(OPENS))
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], "source"), "Website")

	def test_book_sin_apertura_reserva_como_kamra(self):
		out = self.book(1)
		self.assertEqual(out["status"], "Confirmed", out)

	def test_una_reserva_web_por_otro_camino_tampoco_pasa(self):
		self.opens(d(OPENS))
		with self.assertRaises(opening.BeforeOpeningError):
			make_reservation(PROP, self.rt, d(OPENS - 2), d(OPENS + 1), source="Website")
		self.assertEqual(make_reservation(PROP, self.rt, d(OPENS), d(OPENS + 1), source="Website").source, "Website")

	def test_la_recepcion_puede_alojar_antes_de_abrir(self):
		self.opens(d(OPENS))
		for i, source in enumerate(("Manual", "Phone", "Walk-in")):
			res = make_reservation(PROP, self.rt, d(1 + 2 * i), d(2 + 2 * i), source=source)
			self.assertTrue(res.name, source)

	# --- el despacho de Frappe: /api/method lleva al guardián ---

	def test_los_hooks_llevan_al_guardian(self):
		for method in ("showcase", "search_stay", "book"):
			self.assertEqual(frappe.override_whitelisted_method(f"kamra.public_api.{method}"),
			                 f"kamra_uy.booking.opening.{method}")
			fn = getattr(opening, method)
			self.assertIn(fn, frappe.guest_methods, method)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[opening.book], ["POST"])

	def test_por_api_method_sin_sesion(self):
		self.opens(d(OPENS))
		form = {"property": PROP, "check_out_date": d(OPENS + 2), "adults": "2", "_lang": "pt"}
		with self.assertRaises(opening.BeforeOpeningError) as cm:
			self.dispatch("kamra.public_api.search_stay", check_in_date=d(OPENS - 1), **form)
		self.assertEqual(str(cm.exception), regla.motivo(d(OPENS), "pt"))
		rows = self.dispatch("kamra.public_api.search_stay", check_in_date=d(OPENS), **form)
		self.assertTrue(next(r for r in rows if r["room_type"] == self.rt)["available"])
		self.assertEqual(self.dispatch("kamra.public_api.showcase", property=PROP)[opening.FIELD], d(OPENS))
		before = self.reservations()
		with self.assertRaises(opening.BeforeOpeningError):
			self.dispatch("kamra.public_api.book", "POST", room_type=self.rt, check_in_date=d(OPENS - 1),
			              guest_name="UT Persona Apertura", phone="+59899630000", **form)
		self.assertEqual(self.reservations(), before)
		# book sigue siendo solo POST
		with self.assertRaises(frappe.PermissionError):
			self.dispatch("kamra.public_api.book", "GET", room_type=self.rt, check_in_date=d(OPENS),
			              guest_name="UT Persona Apertura", phone="+59899630000", **form)
