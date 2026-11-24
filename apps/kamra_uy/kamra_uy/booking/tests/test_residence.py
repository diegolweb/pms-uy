"""El 10 % de IVA del residente se cobra AL RESERVAR (HR-266, D-HR-47): la reserva en línea pregunta la residencia, el
servidor pone el IVA de cada noche (el que facturará core en el check-in) y, en el check-in, solo se apunta la diferencia
si el documento contradice lo declarado.

Las reservas se hacen por el book de kamra_uy (`residence.book`, la ruta de kamra.public_api.book) y, la del despacho,
por /api/method sin sesión, como la web.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import getdate, nowdate
from kamra.tests.audit_fixtures import (AuditTestCase, d, make_guest, make_property, make_reservation, make_room,
                                        make_room_type)

from kamra_uy.booking import residence
from kamra_uy.fiscal import connector
from kamra_uy.pure import turismo, uy_tax

PROP = "UT Residencia UY"
RUT = "21-999999-0016"


def _future(month: int, day: int) -> date:
	"""El próximo día/mes que quede al menos 2 días por delante (el book no admite llegadas pasadas)."""
	today = getdate(nowdate())
	for year in (today.year, today.year + 1):
		when = date(year, month, day)
		if when > today + timedelta(days=1):
			return when
	raise AssertionError("sin fecha")


def _easter() -> date:
	today = getdate(nowdate())
	return next(e for e in (turismo.domingo_de_pascua(y) for y in range(today.year, today.year + 3))
	            if e > today + timedelta(days=1))


class TestResidence(AuditTestCase):
	properties = (PROP,)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		residence.ensure_fields()

	def setUp(self):
		super().setUp()
		make_property(PROP, country="Uruguay", currency="USD", uy_iva_room_pct=10, uy_iva_standard_pct=22,
		              booking_engine_enabled=1, booking_mode="Instant", booking_payment_mode="Full online",
		              hold_minutes=30, advance_percent=0, registration_fee=0, cleaning_fee=0, booking_language="")
		self.rt = make_room_type(PROP, "RES", base_price=100, adults_capacity=2)
		self.room = make_room(PROP, self.rt, "R101")
		make_room(PROP, self.rt, "R102")
		form, lang = frappe.local.form_dict, getattr(frappe.local, "lang", None)
		self.addCleanup(setattr, frappe.local, "form_dict", form)
		self.addCleanup(setattr, frappe.local, "lang", lang)
		frappe.local.form_dict = frappe._dict()
		self.ip = f"198.51.100.{uuid.uuid4().int % 254 + 1}"  # TEST-NET-2: la IP del huésped de las peticiones

	# --- ayudas ---

	def book(self, check_in: date, nights: int, resident) -> dict:
		return residence.book(property=PROP, room_type=self.rt, check_in_date=str(check_in),
		                      check_out_date=str(check_in + timedelta(days=nights)), guest_name="UT Persona Residencia",
		                      phone=f"+5989964{uuid.uuid4().int % 10000:04d}", adults=2, uy_resident=resident)

	def res(self, out: dict):
		return frappe.get_doc("Reservation", out["reservation"])

	def rates(self, res) -> list[float]:
		return [float(r.gst_rate) for r in sorted(res.night_rates, key=lambda r: str(r.stay_date))]

	def reservations(self) -> int:
		return frappe.db.count("Reservation", {"property": PROP})

	def limit_key(self, counter: str) -> bytes:
		"""La clave de redis del rate_limit de Frappe para `counter` (módulo.función) y la IP de las peticiones del test;
		se borra al acabar."""
		key = frappe.cache.make_key(f"rl:{counter}:{self.ip}") + b":3600"
		self.addCleanup(frappe.cache.delete, key)
		return key

	def dispatch(self, cmd: str, **form):
		"""Una petición POST de la web a /api/method/<cmd>, sin sesión y desde self.ip: una petición de werkzeug de
		verdad (con su host, que Kamra usa para la vuelta de la pasarela) y la IP que le pone Frappe a cada una
		(frappe.auth.HTTPRequest.set_request_ip: X-Forwarded-For, REMOTE_ADDR o, si no, 127.0.0.1). Con esa IP cuenta
		el rate_limit de Frappe; sin ella, el limitador rechaza toda petición («Either key, IP flag, or User flag is
		required»)."""
		from frappe.handler import execute_cmd
		from frappe.utils import get_url
		from werkzeug.test import EnvironBuilder

		request = EnvironBuilder(method="POST", base_url=get_url(), path=f"/api/method/{cmd}",
		                         environ_base={"REMOTE_ADDR": self.ip}).get_request()
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- la web llama sin sesión; tearDown vuelve a Administrator
		frappe.local.form_dict = frappe._dict({"cmd": cmd, **form})
		self.limit_key(cmd)  # el contador de esta IP no sobrevive al test
		with (patch.object(frappe.local, "request", request, create=True),
		      patch.object(frappe.local, "request_ip", request.remote_addr, create=True)):
			return execute_cmd(cmd)

	# --- el campo ---

	def test_el_campo_es_de_kamra_uy_y_nace_vacio(self):
		field = frappe.get_meta("Reservation").get_field(residence.FIELD)
		self.assertEqual(field.fieldtype, "Select")
		self.assertEqual(field.options.split("\n"), ["", uy_tax.RESIDENT, uy_tax.NON_RESIDENT])
		self.assertEqual(frappe.db.get_value("Custom Field", {"dt": "Reservation", "fieldname": residence.FIELD},
		                                     "module"), "Kamra UY")
		res = make_reservation(PROP, self.rt, d(3), d(5), source="Website")
		self.assertFalse(res.get(residence.FIELD))

	# --- book: la residencia es obligatoria ---

	def test_book_sin_residencia_se_rechaza_y_no_crea_nada(self):
		before = self.reservations()
		for value in (None, "", "2", "sí", "yes"):
			with self.assertRaises(residence.ResidenceRequiredError, msg=repr(value)):
				self.book(_future(11, 20), 2, value)
		self.assertEqual(self.reservations(), before)

	def test_el_motivo_sale_en_el_idioma_de_la_pagina(self):
		for lang in ("es", "pt", "en"):
			frappe.local.form_dict = frappe._dict({"_lang": lang})
			with self.assertRaises(residence.ResidenceRequiredError) as cm:
				self.book(_future(11, 20), 2, None)
			self.assertEqual(str(cm.exception), residence.MISSING[lang], lang)

	# --- el IVA de cada noche, al reservar ---

	def test_el_residente_paga_el_10_desde_el_16_de_noviembre_al_reservar(self):
		out = self.book(_future(11, 13), 6, "1")
		res = self.res(out)
		self.assertEqual(res.get(residence.FIELD), uy_tax.RESIDENT)
		self.assertEqual(self.rates(res), [0.0, 0.0, 0.0, 10.0, 10.0, 10.0])
		self.assertAlmostEqual(float(res.tax_amount), 30.0, places=2)
		self.assertAlmostEqual(float(res.amount_after_tax), 630.0, places=2)
		# lo que se cobra al reservar (Full online) es el total con su IVA, y lo dice book
		self.assertAlmostEqual(float(out["amount_after_tax"]), 630.0, places=2)
		self.assertAlmostEqual(float(out["advance_due"]), 630.0, places=2)
		self.assertAlmostEqual(float(res.advance_due), 630.0, places=2)
		self.assertEqual(out["uy_iva"], {"residence": "resident", "amount_before_tax": 600.0, "tax_amount": 30.0,
		                                 "amount_after_tax": 630.0, "iva_pct": 10.0, "iva_nights": 3})

	def test_el_extranjero_no_paga_iva(self):
		out = self.book(_future(11, 13), 6, "0")
		res = self.res(out)
		self.assertEqual(res.get(residence.FIELD), uy_tax.NON_RESIDENT)
		self.assertEqual(set(self.rates(res)), {0.0})
		self.assertAlmostEqual(float(out["advance_due"]), 600.0, places=2)
		self.assertEqual(out["uy_iva"], {"residence": "non_resident", "amount_before_tax": 600.0, "tax_amount": 0.0,
		                                 "amount_after_tax": 600.0, "iva_pct": 0.0, "iva_nights": 0})

	def test_el_domingo_de_pascua_lleva_iva_y_el_lunes_no(self):
		res = self.res(self.book(_easter(), 2, "1"))
		self.assertEqual(self.rates(res), [10.0, 0.0])
		self.assertAlmostEqual(float(res.amount_after_tax), 210.0, places=2)

	def test_el_estado_de_la_reserva_lleva_el_desglose(self):
		out = self.book(_future(11, 13), 6, "1")
		status = residence.booking_status(out["status_token"])
		self.assertEqual(status["uy_iva"], out["uy_iva"])
		self.assertAlmostEqual(float(status["amount_after_tax"]), 630.0, places=2)
		# HR-273: y su Room Type, por el que la web casa la ficha del resumen (no por el nombre)
		self.assertEqual(status["room_type"], self.rt)

	def test_cambiar_las_fechas_vuelve_a_poner_el_iva_de_las_noches(self):
		res = self.res(self.book(_future(11, 13), 2, "1"))
		self.assertEqual(self.rates(res), [0.0, 0.0])
		res.check_out_date = str(getdate(res.check_out_date) + timedelta(days=2))
		res.save(ignore_permissions=True)
		res.reload()
		self.assertEqual(self.rates(res), [0.0, 0.0, 0.0, 10.0])
		self.assertAlmostEqual(float(res.tax_amount), 10.0, places=2)

	def test_un_residente_con_rut_paga_todas_las_noches(self):
		company = frappe.get_doc({"doctype": "Corporate Account", "company_name": "UT Residencia SA",
		                          "gstin": RUT}).insert(ignore_permissions=True).name
		start = _future(7, 10)
		res = make_reservation(PROP, self.rt, str(start), str(start + timedelta(days=2)), source="Website",
		                       company=company, **{residence.FIELD: uy_tax.RESIDENT})
		self.assertEqual(self.rates(res), [10.0, 10.0])
		# sin RUT, en julio, nada
		res2 = make_reservation(PROP, self.rt, str(start), str(start + timedelta(days=2)), source="Website",
		                        **{residence.FIELD: uy_tax.RESIDENT})
		self.assertEqual(self.rates(res2), [0.0, 0.0])

	def test_una_reserva_sin_declaracion_sigue_sin_iva_hasta_el_check_in(self):
		res = make_reservation(PROP, self.rt, str(_future(11, 20)), str(_future(11, 20) + timedelta(days=2)),
		                       source="Website")
		self.assertEqual(set(self.rates(res)), {0.0})

	def test_una_propiedad_de_otro_pais_no_pregunta_nada(self):
		self.assertFalse(residence.applies("UT Propiedad Inexistente"))
		self.assertTrue(residence.applies(PROP))

	# --- el despacho de Frappe: /api/method, sin sesión ---

	def test_por_api_method_sin_sesion(self):
		start = _future(11, 13)
		form = {"property": PROP, "room_type": self.rt, "check_in_date": str(start),
		        "check_out_date": str(start + timedelta(days=6)), "guest_name": "UT Persona Residencia",
		        "phone": "+59899640001", "adults": "2", "_lang": "pt"}
		before = self.reservations()
		with self.assertRaises(residence.ResidenceRequiredError) as cm:
			self.dispatch("kamra.public_api.book", **form)
		self.assertEqual(str(cm.exception), residence.MISSING["pt"])
		self.assertEqual(self.reservations(), before)
		out = self.dispatch("kamra.public_api.book", uy_resident="1", **form)
		self.assertEqual(frappe.db.get_value("Reservation", out["reservation"], residence.FIELD), uy_tax.RESIDENT)
		self.assertAlmostEqual(float(out["advance_due"]), 630.0, places=2)
		status = self.dispatch("kamra.public_api.booking_status", token=out["status_token"])
		self.assertEqual(status["uy_iva"]["tax_amount"], 30.0)
		self.assertFalse(frappe.flags.get(residence.FLAG))  # la declaración no se queda para la siguiente petición

	def test_el_limite_de_book_cuenta_la_reserva_de_la_residencia(self):
		"""El rate_limit de kamra.public_api.book (10 por hora y por IP) sigue contando por la ruta de kamra_uy, con su
		mismo contador: la reserva lo sube en uno y, con el cupo lleno, no se crea ninguna."""
		start = _future(11, 13)
		form = {"property": PROP, "room_type": self.rt, "check_in_date": str(start),
		        "check_out_date": str(start + timedelta(days=2)), "guest_name": "UT Persona Residencia",
		        "phone": "+59899640002", "adults": "2", "uy_resident": "1"}
		key = self.limit_key("kamra.public_api.book")
		self.assertEqual(int(frappe.cache.get(key) or 0), 0)
		self.dispatch("kamra.public_api.book", **form)
		self.assertEqual(int(frappe.cache.get(key)), 1)
		frappe.cache.setex(key, 3600, 10)
		before = self.reservations()
		with self.assertRaises(frappe.RateLimitExceededError):
			self.dispatch("kamra.public_api.book", **form)
		self.assertEqual(self.reservations(), before)

	def test_los_hooks_llevan_a_la_residencia(self):
		self.assertEqual(frappe.override_whitelisted_method("kamra.public_api.booking_status"),
		                 "kamra_uy.booking.residence.booking_status")
		self.assertEqual(frappe.override_whitelisted_method("kamra.public_api.book"), "kamra_uy.booking.residence.book")
		self.assertIn(residence.booking_status, frappe.guest_methods)
		self.assertIn(residence.book, frappe.guest_methods)
		# HR-275: la vitrina es la de Kamra tal cual; la búsqueda, la de Kamra con el presupuesto por coste de HR-337
		self.assertEqual(frappe.override_whitelisted_method("kamra.public_api.showcase"), "kamra.public_api.showcase")
		self.assertEqual(frappe.override_whitelisted_method("kamra.public_api.search_stay"),
		                 "kamra_uy.booking.stay_search.search_stay")

	# --- el check-in: solo la diferencia, si el documento contradice lo declarado ---

	def check_in(self, declared: str, core_rate: float):
		"""Una reserva web de hoy a pasado mañana con lo declarado; en el check-in, core contesta `core_rate` por noche.
		El huésped tiene el país de residencia que dice su documento (HR-304: sin él no hay check-in)."""
		guest = make_guest("UT Huésped Residencia", residence_country="Uruguay" if core_rate else "Argentina")
		res = make_reservation(PROP, self.rt, d(0), d(2), room=self.room, source="Website", guest=guest,
		                       **{residence.FIELD: declared})

		def fake_core(doc, timeout=None):
			connector.apply_room_rates(doc, {str(getdate(r.stay_date)): Decimal(str(core_rate))
			                                 for r in doc.night_rates})
			return {}

		with patch.object(connector, "adjust_iva", side_effect=fake_core):
			res.status = "Checked In"
			res.save(ignore_permissions=True)
		return res

	def notes(self, res) -> list[str]:
		folios = frappe.get_all("Folio", filters={"reservation": res.name}, pluck="name")
		return frappe.get_all("Comment", filters={"reference_doctype": ["in", ["Folio", "Reservation"]],
		                                          "reference_name": ["in", [*folios, res.name]],
		                                          "comment_type": "Comment"}, pluck="content")

	def test_si_el_documento_confirma_lo_declarado_no_hay_nada_que_cobrar(self):
		res = self.check_in(uy_tax.NON_RESIDENT, 0)
		self.assertAlmostEqual(float(res.tax_amount), 0.0, places=2)
		self.assertEqual([n for n in self.notes(res) if "IVA" in n], [])

	def test_si_el_documento_dice_residente_el_folio_apunta_lo_que_falta_cobrar(self):
		res = self.check_in(uy_tax.NON_RESIDENT, 10)
		self.assertAlmostEqual(float(res.tax_amount), 20.0, places=2)
		notes = [n for n in self.notes(res) if "IVA" in n]
		self.assertEqual(len(notes), 1, notes)
		self.assertIn(residence.COLLECT_WORD, notes[0])
		self.assertIn("20", notes[0])
		folio = frappe.db.get_value("Folio", {"reservation": res.name, "folio_type": "Guest"})
		if folio:
			self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Folio", "reference_name": folio}))

	def test_si_el_documento_dice_extranjero_el_folio_apunta_lo_que_hay_que_devolver(self):
		with patch("kamra_uy.pure.turismo.en_alta_temporada", return_value=True):
			res = self.check_in(uy_tax.RESIDENT, 0)
		notes = [n for n in self.notes(res) if "IVA" in n]
		self.assertEqual(len(notes), 1, notes)
		self.assertIn(residence.REFUND_WORD, notes[0])
		self.assertIn("20", notes[0])



class TestResidenceAtTheDesk(AuditTestCase):
	"""HR-304: recepción ve y marca la residencia (al reservar por teléfono, en una reserva de plataforma y en el
	check-in), su cotización lleva el 10 % del residente como la web, y el check-in no se hace sin ella."""

	properties = (PROP,)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		residence.ensure_fields()

	def setUp(self):
		super().setUp()
		make_property(PROP, country="Uruguay", currency="USD", uy_iva_room_pct=10, uy_iva_standard_pct=22,
		              advance_percent=0, registration_fee=0, cleaning_fee=0)
		self.rt = make_room_type(PROP, "RES", base_price=100, adults_capacity=2)
		self.room = make_room(PROP, self.rt, "R101")
		for number in ("R102", "R103"):  # la reserva por teléfono hace tres en las mismas fechas
			make_room(PROP, self.rt, number)
		self.high = _future(11, 13)  # 3 noches sin IVA y 3 con él desde el 16 de noviembre

	def quote(self, resident):
		from kamra import api

		return api.get_quote(property=PROP, room_type=self.rt, check_in_date=str(self.high),
		                     check_out_date=str(self.high + timedelta(days=6)), adults=2, tax_resident=resident)

	def booking(self, resident, **kw):
		from kamra import api

		out = api.create_booking(property=PROP, room_type=self.rt, check_in_date=str(self.high),
		                         check_out_date=str(self.high + timedelta(days=6)), guest_name="UT Teléfono Residencia",
		                         phone=f"+5989963{uuid.uuid4().int % 10000:04d}", adults=2, tax_resident=resident,
		                         **kw)
		return frappe.get_doc("Reservation", out["reservation"])

	def rates(self, res) -> list[float]:
		return [float(r.gst_rate) for r in sorted(res.night_rates, key=lambda r: str(r.stay_date))]

	# --- la reserva por teléfono ---

	def test_la_cotizacion_de_recepcion_lleva_el_10_del_residente_como_la_web(self):
		plain, resident, foreign = self.quote(None), self.quote("1"), self.quote("0")
		self.assertNotIn("residence", plain)
		self.assertAlmostEqual(float(resident["tax_amount"]) - float(plain["tax_amount"]), 30.0, places=2)
		self.assertEqual([n["gst_rate"] for n in resident["nightly"]], [0.0, 0.0, 0.0, 10.0, 10.0, 10.0])
		self.assertEqual(resident["residence"], "resident")
		self.assertAlmostEqual(float(foreign["amount_after_tax"]), float(plain["amount_after_tax"]), places=2)
		self.assertEqual(foreign["residence"], "non_resident")

	def test_la_reserva_por_telefono_nace_con_lo_declarado_y_cobra_lo_cotizado(self):
		res = self.booking("1")
		self.assertEqual(res.get(residence.FIELD), uy_tax.RESIDENT)
		self.assertEqual(self.rates(res), [0.0, 0.0, 0.0, 10.0, 10.0, 10.0])
		self.assertAlmostEqual(float(res.amount_after_tax), float(self.quote("1")["amount_after_tax"]), places=2)
		self.assertEqual(self.booking("0").get(residence.FIELD), uy_tax.NON_RESIDENT)
		self.assertFalse(self.booking(None).get(residence.FIELD))

	# --- una reserva ya hecha (la de una plataforma) ---

	def test_recepcion_declara_la_residencia_de_una_reserva_de_plataforma(self):
		from kamra import api

		res = make_reservation(PROP, self.rt, str(self.high), str(self.high + timedelta(days=6)), room=self.room,
		                       source="OTA")
		seen = api.reservation_detail(res.name)["residence"]
		self.assertEqual(seen, {"home_country": "Uruguay", "declared": None, "country": None, "can_declare": True,
		                        "missing": True})
		out = api.declare_residence(res.name, "1")
		self.assertEqual(out["declared"], "resident")
		self.assertFalse(out["missing"])
		self.assertEqual(self.rates(frappe.get_doc("Reservation", res.name)), [0.0, 0.0, 0.0, 10.0, 10.0, 10.0])
		api.declare_residence(res.name, "0")
		self.assertEqual(set(self.rates(frappe.get_doc("Reservation", res.name))), {0.0})
		with self.assertRaises(frappe.ValidationError):
			api.declare_residence(res.name, "sí")

	def test_con_el_huesped_en_casa_ya_no_se_declara_en_la_reserva(self):
		from kamra import api

		guest = make_guest("UT En Casa", residence_country="Argentina")
		res = make_reservation(PROP, self.rt, d(0), d(2), room=self.room, status="Checked In", guest=guest)
		self.assertFalse(api.reservation_detail(res.name)["residence"]["can_declare"])
		with self.assertRaises(frappe.ValidationError):
			api.declare_residence(res.name, "1")

	# --- el check-in ---

	def check_in(self, guest=None, **fields):
		# una habitación propia en cada llamada: un check-in rechazado deja su reserva en esas fechas
		room = make_room(PROP, self.rt, f"C{uuid.uuid4().hex[:5]}")
		res = make_reservation(PROP, self.rt, d(0), d(2), room=room, guest=guest, **fields)
		res.status = "Checked In"
		with patch.object(connector, "adjust_iva", return_value={}):
			res.save(ignore_permissions=True)
		return res

	def test_sin_residencia_no_hay_check_in(self):
		for fields in ({}, {residence.FIELD: uy_tax.NON_RESIDENT}):
			with self.assertRaises(residence.ResidenceRequiredError) as cm:
				self.check_in(make_guest("UT Sin País"), **fields)
			self.assertEqual(str(cm.exception), residence.CHECK_IN_NEEDS)

	def test_con_la_residencia_declarada_o_el_pais_del_huesped_si(self):
		self.assertEqual(self.check_in(make_guest("UT Declaró"), **{residence.FIELD: uy_tax.RESIDENT}).status,
		                 "Checked In")
		self.assertEqual(self.check_in(make_guest("UT Con País", residence_country="Brazil")).status, "Checked In")

	def test_el_flujo_del_check_in_la_pide_y_la_guarda_en_el_huesped(self):
		from kamra import checkin

		guest = make_guest("UT Flujo Residencia")
		res = make_reservation(PROP, self.rt, d(0), d(2), room=self.room, guest=guest)
		flow = checkin.checkin_flow(res.name)
		self.assertIn("residence", flow["missing_required"])
		self.assertTrue(flow["vocabulary"]["asks_residence"])
		flow = checkin.save_guest_details(res.name, residence_country="ar")
		self.assertEqual(frappe.db.get_value("Guest", guest, "residence_country"), "Argentina")
		self.assertNotIn("residence", flow["missing_required"])
		self.assertEqual(flow["residence"]["country"], "Argentina")
		with self.assertRaises(frappe.ValidationError):
			checkin.save_guest_details(res.name, residence_country="Narnia")
