"""El correo de confirmación de la reserva en línea (HR-227).

Al pasar a Confirmed por un pago una reserva Website con correo, kamra_uy encola UN correo al huésped en el idioma con
el que reservó (el `_lang` que manda la web a `book`, guardado en la Reservation al crearla), con fechas, habitación,
huéspedes, total pagado y los datos del hotel, y el aviso del IVA a residentes si alguna noche cae entre el 16 de
noviembre y el domingo de Pascua siguiente (D-HR-40; kamra_uy.pure.turismo, los mismos vectores que
web/src/booking/reglas.ts). Nunca un dato de la tarjeta; y si el correo no puede salir, la reserva se confirma igual.

Las reservas se hacen como las hace la web (`kamra.public_api.book`) y se pagan con el aviso firmado de core
(`kamra_uy.payments.webhook.receive`), con core como doble: ninguna prueba sale a la red.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.tests.test_confirmation_mail`).
"""

import email
import unittest
import uuid
from datetime import date, timedelta
from email import policy
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime
from kamra import public_api
from kamra.tests.audit_fixtures import d, make_guest, make_property, make_reservation, make_room

from kamra_uy.notifications import confirmation, formato, texts
from kamra_uy.payments import webhook
from kamra_uy.payments.tests.fixtures import PROP, CobrosCase, event, signed
from kamra_uy.pure import turismo

CORE = "http://core.example.com"
GUEST_MAIL = "huesped-confirmacion-ut@example.com"
HOTEL_MAIL = "hotel-confirmacion-ut@example.com"
HOTEL_PHONE = "+000 0000 0227"
HOTEL_ADDRESS = "Calle UT 227"
HOTEL_CITY = "Ciudad UT"
PAY_1 = "7a2b0c11-1111-4222-8333-555555555555"
# El aviso del IVA a residentes, el `iva.aviso` de reservar.md (HR-266, D-HR-47: el 10 % se paga al reservar): aprobado
# con todos los textos el 22-11 (D-HR-48), literal también aquí.
IVA = {
	"es": "Los huéspedes residentes en Uruguay deberán abonar el 10% de IVA adicional al momento de la reserva por las "
	      "noches entre el 16 de noviembre y Semana de Turismo.",
	"pt": "Os hóspedes residentes no Uruguai deverão pagar 10% de IVA adicional no momento da reserva pelas noites entre "
	      "16 de novembro e a Semana de Turismo (Semana Santa).",
	"en": "Guests residing in Uruguay must pay an additional 10% VAT when booking for nights between November 16 and "
	      "Tourism Week (Holy Week).",
}
# Los textos de la web que el correo repite (texts.DE_LA_WEB), literales de web/content/{es,pt,en}/reservar.md: el
# contenedor de Kamra no tiene web/, así que la guarda vive aquí.
APPROVED = {
	"es": {"confirmada": "Reserva confirmada", "referencia": "Referencia", "llegada": "Llegada", "salida": "Salida",
	       "noches": "Noches", "habitacion": "Habitación", "huespedes": "Huéspedes", "total": "Total",
	       "pagado": "Pagado", "direccion": "Dirección", "contacto": "Contacto", "iva": IVA["es"]},
	"pt": {"confirmada": "Reserva confirmada", "referencia": "Referência", "llegada": "Chegada", "salida": "Saída",
	       "noches": "Noites", "habitacion": "Quarto", "huespedes": "Hóspedes", "total": "Total", "pagado": "Pago",
	       "direccion": "Endereço", "contacto": "Contato", "iva": IVA["pt"]},
	"en": {"confirmada": "Booking confirmed", "referencia": "Reference", "llegada": "Arrival", "salida": "Departure",
	       "noches": "Nights", "habitacion": "Room", "huespedes": "Guests", "total": "Total", "pagado": "Paid",
	       "direccion": "Address", "contacto": "Contact", "iva": IVA["en"]},
}


# --- la Semana de Turismo y el aviso del IVA (puro) ----------------------------------------------------------------


class TestTurismo(unittest.TestCase):
	def test_easter_sunday_is_computed(self):
		for anio, dia in [(2026, "2026-04-05"), (2027, "2027-03-28"), (2028, "2028-04-16"), (2029, "2029-04-01"),
		                  (2030, "2030-04-21"), (2031, "2031-04-13"), (2038, "2038-04-25"), (2285, "2285-03-22")]:
			with self.subTest(anio=anio):
				self.assertEqual(turismo.domingo_de_pascua(anio), date.fromisoformat(dia))

	def test_tourism_week_runs_monday_to_easter_sunday(self):
		for anio, (lunes, domingo) in {2026: ("2026-03-30", "2026-04-05"), 2027: ("2027-03-22", "2027-03-28"),
		                               2028: ("2028-04-10", "2028-04-16")}.items():
			with self.subTest(anio=anio):
				self.assertEqual(turismo.semana_de_turismo(anio), (date.fromisoformat(lunes), date.fromisoformat(domingo)))

	def test_high_season_is_16_november_to_easter_sunday_both_included(self):
		for noche, si in [("2026-11-15", False), ("2026-11-16", True), ("2026-12-31", True), ("2027-01-01", True),
		                  ("2027-03-28", True), ("2027-03-29", False), ("2026-04-05", True), ("2026-04-06", False),
		                  ("2028-04-16", True), ("2028-04-17", False), ("2027-07-01", False)]:
			with self.subTest(noche=noche):
				self.assertIs(turismo.en_alta_temporada(date.fromisoformat(noche)), si)

	def test_the_notice_goes_when_any_night_is_in_high_season(self):
		# los vectores de web/tests/unit/reglas.test.ts (avisoIvaResidentes): por noches, la salida no cuenta
		for llegada, salida, si in [
			("2026-11-14", "2026-11-16", False), ("2026-11-15", "2026-11-17", True),
			("2027-03-26", "2027-03-29", True), ("2027-03-29", "2027-04-02", False),
			("2026-04-04", "2026-04-06", True), ("2026-04-06", "2026-04-09", False),
			("2028-04-15", "2028-04-17", True), ("2028-04-17", "2028-04-20", False),
			("2027-01-08", "2027-01-11", True),
			("2027-11-10", "2027-11-16", False), ("2027-11-10", "2027-11-17", True),
		]:
			with self.subTest(llegada=llegada, salida=salida):
				self.assertIs(turismo.aviso_iva_residentes(llegada, salida), si)
				self.assertIs(turismo.aviso_iva_residentes(date.fromisoformat(llegada), date.fromisoformat(salida)), si)

	def test_no_nights_no_notice(self):
		self.assertIs(turismo.aviso_iva_residentes("2027-01-08", "2027-01-08"), False)
		self.assertIs(turismo.aviso_iva_residentes("2027-01-09", "2027-01-08"), False)

	def test_a_bad_date_is_refused(self):
		for mala in ("2027-02-30", "08/01/2027", "", None):
			with self.subTest(mala=mala), self.assertRaises((ValueError, TypeError)):
				turismo.aviso_iva_residentes(mala, "2027-03-01")


class TestFormato(unittest.TestCase):
	def test_amounts_like_the_web(self):
		# los vectores de web/tests/unit/reglas.test.ts (importe, D-S53-3)
		for valor, moneda, idioma, texto in [
			(1150, "USD", "es", "US$ 1.150"), (1265.5, "USD", "es", "US$ 1.265,50"), (1150, "USD", "pt", "US$ 1.150"),
			(1150, "USD", "en", "US$ 1,150"), (1265.5, "USD", "en", "US$ 1,265.50"), (390, "USD", "es", "US$ 390"),
			(1150, "UYU", "es", "$ 1.150"), (1234567.8, "EUR", "pt", "EUR 1.234.567,80"), (0, "USD", "en", "US$ 0"),
		]:
			with self.subTest(valor=valor, moneda=moneda, idioma=idioma):
				self.assertEqual(formato.importe(valor, moneda, idioma), texto)

	def test_dates_like_the_web(self):
		# fechaCorta de web/src/booking/reglas.ts: «Vie 8 ene 2027», con las tablas de fechas.dias y fechas.meses
		self.assertEqual(formato.fecha_corta("2027-01-08", "es"), "Vie 8 ene 2027")
		self.assertEqual(formato.fecha_corta(date(2027, 1, 8), "pt"), "Sex 8 jan 2027")
		self.assertEqual(formato.fecha_corta("2027-01-10", "en"), "Sun 10 Jan 2027")
		self.assertEqual(formato.fecha_corta("2031-04-01", "pt"), "Ter 1 abr 2031")

	def test_times_without_seconds(self):
		self.assertEqual(formato.hora("14:00:00"), "14:00")
		self.assertEqual(formato.hora(timedelta(hours=11)), "11:00")
		self.assertEqual(formato.hora(None), "")

	def test_every_language_has_every_text(self):
		claves = set(texts.TEXTOS["es"])
		for idioma in ("es", "pt", "en"):
			with self.subTest(idioma=idioma):
				self.assertEqual(set(texts.TEXTOS[idioma]), claves)
				self.assertTrue(all(isinstance(v, str) and v.strip() for v in texts.TEXTOS[idioma].values()))

	def test_the_approved_texts_are_those_of_hr_175(self):
		for idioma, aprobados in APPROVED.items():
			for clave, literal in aprobados.items():
				with self.subTest(idioma=idioma, clave=clave):
					self.assertEqual(texts.TEXTOS[idioma][clave], literal)
		self.assertEqual(set(texts.DE_LA_WEB), set(APPROVED["es"]))

	def test_the_vat_notice_is_approved_and_no_longer_sends_to_the_check_in(self):
		# HR-266 (D-HR-47): el 10 % del residente se paga al reservar, así que el aviso ya no manda a pagarlo en el
		# check-in; D-HR-48 (22-11): aprobado, como el `iva.aviso` de la web
		for idioma, literal in IVA.items():
			with self.subTest(idioma=idioma):
				self.assertEqual(texts.TEXTOS[idioma]["iva"], literal)
				self.assertNotIn("check-in", literal)
		self.assertIn("iva", texts.DE_LA_WEB)
		self.assertEqual(texts.FUENTES["iva"], "D-HR-48")
		# HR-282 (D-HR-48): ninguno queda a la espera del fundador
		self.assertEqual(set(texts.APROBADOS), set(texts.TEXTOS["es"]))


# --- el correo (Frappe) --------------------------------------------------------------------------------------------


class Core:
	"""El doble de CoreClient para `book`: solo el enlace de pago del folio."""

	def __init__(self):
		self.links = 0

	def __call__(self, base_url, api_key, *a, **k):
		return self

	def post(self, path, body, idempotency_key):
		assert path == "/v1/payments/links", path
		self.links += 1
		return {"link_id": f"link-ut-{self.links}", "url": f"http://mp-mock:8080/checkout/v1/redirect?pref_id={self.links}"}


class TestConfirmationMail(CobrosCase):
	def setUp(self):
		super().setUp()
		frappe.local.lang = "en"
		make_property(PROP, country="Uruguay", currency="USD", uy_iva_room_pct=10, uy_iva_standard_pct=22,
		              booking_engine_enabled=1, booking_mode="Instant", booking_payment_mode="Full online",
		              hold_minutes=30, advance_percent=0, registration_fee=0, cleaning_fee=0, booking_language="",
		              address_line=HOTEL_ADDRESS, city=HOTEL_CITY, phone=HOTEL_PHONE, email=HOTEL_MAIL,
		              checkin_time="14:00:00", checkout_time="11:00:00")
		frappe.db.set_value("Room Type", self.rt, {"base_price": 130, "adults_capacity": 2})
		make_room(PROP, self.rt, "M101")
		make_room(PROP, self.rt, "M102")
		conf = patch.dict(frappe.conf, {"hr_core_base_url": CORE})
		conf.start()
		self.addCleanup(conf.stop)
		double = patch("kamra_uy.payments.mercadopago.CoreClient", Core())
		double.start()
		self.addCleanup(double.stop)
		form = frappe.local.form_dict
		self.addCleanup(setattr, frappe.local, "form_dict", form)
		self.started = now_datetime() - timedelta(seconds=1)
		# el Error Log no se deshace con el rollback: los del test anterior (en ese segundo) no son de este
		self.earlier_logs = set(frappe.get_all("Error Log", filters={"creation": [">=", self.started]}, pluck="name"))
		self.outbox()

	# --- ayudas ---

	def book(self, lang: str | None = "pt", check_in: int = 20, nights: int = 3, mail: str = GUEST_MAIL) -> dict:
		"""Una reserva web retenida, como la hace la web: `book` con el `_lang` de la página, en una petición (Frappe
		fija frappe.local.lang al empezarla, frappe.auth.HTTPRequest.set_lang). Sin `lang`, fuera de una petición."""
		frappe.local.form_dict = frappe._dict({"_lang": lang} if lang else {})
		if lang:
			frappe.local.lang = frappe.translate.get_language()
		try:
			with patch.object(confirmation, "_in_request", return_value=bool(lang)):
				out = public_api.book(property=PROP, room_type=self.rt, check_in_date=d(check_in),
				                      check_out_date=d(check_in + nights), guest_name="UT Ana Confirmación",
				                      phone=f"+5989900{uuid.uuid4().int % 10000:04d}", email=mail, adults=2)
		finally:
			frappe.local.form_dict = frappe._dict()
			frappe.local.lang = "en"
		self.assertEqual(out["status"], "Pending Payment", out)
		return out

	def folio(self, reservation: str) -> str:
		return frappe.db.get_value("Folio", {"reservation": reservation, "folio_type": "Guest"}, "name")

	def pay(self, reservation: str, amount: float | None = None, **over) -> dict:
		folio = self.folio(reservation)
		amount = frappe.db.get_value("Reservation", reservation, "advance_due") if amount is None else amount
		body = event("payment.approved", folio, amount=f"{amount:.2f}",
		             link_id=frappe.db.get_value("Folio", folio, "payment_link_id"), **over)
		return webhook.receive(*signed(body))

	def mails(self, reservation: str) -> list[dict]:
		rows = frappe.get_all("Email Queue", {"reference_doctype": "Reservation", "reference_name": reservation},
		                      pluck="name", order_by="creation asc")
		out = []
		for name in rows:
			doc = frappe.get_doc("Email Queue", name)
			msg = email.message_from_string(doc.message, policy=policy.default)
			html = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html")
			text = next((p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain"), "")
			out.append({"to": [r.recipient for r in doc.recipients], "subject": str(msg["Subject"]), "html": html,
			            "text": text, "reply_to": str(msg["Reply-To"] or "")})
		return out

	def error_logs(self) -> list:
		rows = frappe.get_all("Error Log", filters={"creation": [">=", self.started]},
		                      fields=["name", "method", "error", "reference_doctype", "reference_name"])
		return [r for r in rows if r.name not in self.earlier_logs]

	def status(self, reservation: str) -> str:
		return frappe.db.get_value("Reservation", reservation, "status")

	# --- el cableado ---

	def test_the_fields_are_installed_and_ensuring_them_again_changes_nothing(self):
		confirmation.ensure_fields()
		confirmation.ensure_fields()
		meta = frappe.get_meta("Reservation")
		lang = meta.get_field(confirmation.LANGUAGE_FIELD)
		sent = meta.get_field(confirmation.SENT_FIELD)
		self.assertEqual((lang.fieldtype, lang.read_only), ("Data", 1))
		self.assertEqual((sent.fieldtype, sent.read_only), ("Datetime", 1))
		self.assertEqual(frappe.db.count("Custom Field", {"dt": "Reservation", "fieldname": ["in", [
			confirmation.LANGUAGE_FIELD, confirmation.SENT_FIELD]], "module": "Kamra UY"}), 2)

	def test_the_hooks_call_it(self):
		events = frappe.get_hooks("doc_events")["Reservation"]
		self.assertIn("kamra_uy.notifications.confirmation.remember_language", events["before_insert"])
		self.assertIn("kamra_uy.notifications.confirmation.on_reservation_update", events["on_update"])
		for hook in ("after_install", "after_migrate"):
			with self.subTest(hook=hook):
				self.assertIn("kamra_uy.notifications.confirmation.ensure_fields", frappe.get_hooks(hook))

	def test_the_page_language_survives_act_as(self):
		"""Por qué se lee frappe.local.lang: `book` crea la reserva dentro de kamra.authz.act_as, que llama a
		frappe.set_user y vacía form_dict; el idioma que Frappe sacó de `_lang` al empezar la petición sigue en
		frappe.local.lang. Si Frappe o Kamra cambian esto, el correo saldría en el idioma de la propiedad."""
		from kamra.authz import act_as

		frappe.local.form_dict = frappe._dict({"_lang": "pt"})
		self.assertEqual(frappe.translate.get_language(), "pt")
		frappe.local.lang = "pt"
		try:
			with act_as(public_api.GUEST_AGENT):
				self.assertNotIn("_lang", frappe.local.form_dict)
				self.assertEqual(frappe.local.lang, "pt")
				with patch.object(confirmation, "_in_request", return_value=True):
					self.assertEqual(confirmation._page_language(), "pt")
				self.assertIsNone(confirmation._page_language(), "fuera de una petición, nada")
		finally:
			frappe.local.lang = "en"

	# --- el correo ---

	def test_paying_a_web_booking_queues_one_mail_to_the_guest_in_its_language(self):
		out = self.book("pt")
		res = out["reservation"]
		self.assertEqual(frappe.db.get_value("Reservation", res, confirmation.LANGUAGE_FIELD), "pt")
		self.assertEqual(self.mails(res), [], "retenida: aún no hay correo")
		self.assertEqual(self.pay(res)["status"], "Applied")
		self.assertEqual(self.status(res), "Confirmed")

		(mail,) = self.mails(res)
		t, a = texts.TEXTOS["pt"], APPROVED["pt"]
		self.assertEqual(mail["to"], [GUEST_MAIL])
		self.assertEqual(mail["subject"], t["asunto"].format(hotel=PROP))
		self.assertEqual(mail["reply_to"], HOTEL_MAIL)
		html = mail["html"]
		total = float(out["amount_after_tax"])
		for literal in (a["confirmada"], t["saludo"].format(nombre="UT Ana Confirmación"), t["intro"],
		                a["referencia"], res, a["llegada"], "Ter 1 abr 2031", a["salida"], "Sex 4 abr 2031",
		                a["noches"], a["habitacion"], frappe.db.get_value("Room Type", self.rt, "room_type_name"),
		                a["huespedes"], f"{formato.importe(total, 'USD', 'pt')} · {a['pagado']}",
		                a["direccion"], HOTEL_ADDRESS, HOTEL_CITY, a["contacto"], HOTEL_PHONE, HOTEL_MAIL,
		                t["horario"].format(entrada="14:00", salida="11:00"), t["ayuda"], t["despedida"].format(hotel=PROP)):
			with self.subTest(literal=literal):
				self.assertIn(literal, html)
		# 2031-04-01 a 04-04: la Semana de Turismo de 2031 acaba el domingo 13 de abril
		self.assertIn(IVA["pt"], html)
		self.assertIn(a["confirmada"], mail["text"])
		self.assertTrue(frappe.db.get_value("Reservation", res, confirmation.SENT_FIELD))

	def test_spanish_and_english_bookings_get_their_own_language(self):
		for lang, check_in in (("es", 20), ("en", 30)):
			with self.subTest(lang=lang):
				res = self.book(lang, check_in=check_in)["reservation"]
				self.pay(res, payment_id=str(uuid.uuid4()))
				(mail,) = self.mails(res)
				t, a = texts.TEXTOS[lang], APPROVED[lang]
				self.assertEqual(mail["subject"], t["asunto"].format(hotel=PROP))
				for literal in (a["confirmada"], a["llegada"], a["salida"], a["habitacion"], a["huespedes"],
				                a["referencia"], a["direccion"], a["contacto"], t["intro"]):
					self.assertIn(literal, mail["html"])
				other = "pt" if lang == "es" else "es"
				self.assertNotIn(texts.TEXTOS[other]["intro"], mail["html"])

	def test_the_page_language_comes_in_any_case_and_an_unknown_one_falls_back(self):
		res = self.book("PT-br")["reservation"]
		self.assertEqual(frappe.db.get_value("Reservation", res, confirmation.LANGUAGE_FIELD), "pt")
		res = self.book("fr", check_in=30)["reservation"]
		self.assertFalse(frappe.db.get_value("Reservation", res, confirmation.LANGUAGE_FIELD))
		self.pay(res, payment_id=str(uuid.uuid4()))
		(mail,) = self.mails(res)
		self.assertEqual(mail["subject"], texts.TEXTOS["es"]["asunto"].format(hotel=PROP))

	def test_without_the_page_language_the_property_language_is_used(self):
		frappe.db.set_value("Property", PROP, "booking_language", "en")
		res = self.book(None)["reservation"]
		self.assertFalse(frappe.db.get_value("Reservation", res, confirmation.LANGUAGE_FIELD))
		self.pay(res)
		(mail,) = self.mails(res)
		self.assertEqual(mail["subject"], texts.TEXTOS["en"]["asunto"].format(hotel=PROP))

	def test_another_notice_never_sends_a_second_mail(self):
		res = self.book("es")["reservation"]
		body = event("payment.approved", self.folio(res), amount=f"{frappe.db.get_value('Reservation', res, 'advance_due'):.2f}")
		self.assertEqual(webhook.receive(*signed(body))["status"], "Applied")
		self.assertEqual(webhook.receive(*signed(body)), {"duplicate": True})  # el mismo aviso, otra vez
		self.assertEqual(self.pay(res, amount=1, payment_id=str(uuid.uuid4()))["status"], "Applied")  # otro pago
		frappe.get_doc("Reservation", res).save(ignore_permissions=True)  # la reserva se guarda otra vez
		self.assertEqual(len(self.mails(res)), 1)
		# ni aunque vuelva a retenida y se confirme otra vez
		from kamra.api import _confirm_status

		frappe.db.set_value("Reservation", res, "status", "Pending Payment")
		_confirm_status(frappe.get_doc("Reservation", res))
		self.assertEqual(self.status(res), "Confirmed")
		self.assertEqual(len(self.mails(res)), 1)

	def test_the_vat_notice_only_when_a_night_is_in_high_season(self):
		# TODAY de los fixtures es 2031-03-12: la Semana de Turismo de 2031 acaba el 13-abr y la alta vuelve el 16-nov
		cases = [(40, 3, False),    # 21 a 24-abr-2031: fuera
		         (32, 3, True),     # 13 a 16-abr-2031: el check-in es el domingo de Pascua
		         (247, 3, True)]    # 14 a 17-nov-2031: el check-in, fuera; la noche del 16, dentro (D-HR-40)
		for check_in, nights, si in cases:
			with self.subTest(check_in=d(check_in)):
				self.assertIs(turismo.aviso_iva_residentes(d(check_in), d(check_in + nights)), si)
				res = self.book("es", check_in=check_in, nights=nights)["reservation"]
				self.pay(res, payment_id=str(uuid.uuid4()))
				(mail,) = self.mails(res)
				self.assertIs(IVA["es"] in mail["html"], si)

	def test_a_partial_payment_shows_the_total_and_what_was_paid(self):
		res = self.book("es")["reservation"]
		total = float(frappe.db.get_value("Reservation", res, "amount_after_tax"))
		self.pay(res, amount=100)
		self.assertEqual(self.status(res), "Confirmed")
		(mail,) = self.mails(res)
		a = APPROVED["es"]
		self.assertNotIn(f"{formato.importe(total, 'USD', 'es')} · {a['pagado']}", mail["html"])
		self.assertIn(formato.importe(total, "USD", "es"), mail["html"])
		self.assertIn(formato.importe(100, "USD", "es"), mail["html"])

	def test_no_card_data_anywhere(self):
		res = self.book("en")["reservation"]
		self.pay(res, mode="Tarjeta extranjera", card_country="AR")
		(mail,) = self.mails(res)
		folio = frappe.get_doc("Folio", self.folio(res))
		secrets = {PAY_1, folio.payment_link_id, "Tarjeta", "Mercado Pago"}
		secrets |= {p.reference for p in folio.payments} | {p.mode for p in folio.payments}
		for secret in secrets:
			with self.subTest(secret=secret):
				self.assertNotIn(secret, mail["html"])
				self.assertNotIn(secret, mail["text"])
				self.assertNotIn(secret, mail["subject"])

	def test_what_the_guest_typed_is_escaped(self):
		frappe.db.set_value("Room Type", self.rt, "room_type_name", "UT <b>Doble</b> & Co")
		res = self.book("es")["reservation"]
		self.pay(res)
		(mail,) = self.mails(res)
		self.assertNotIn("<b>Doble</b>", mail["html"])
		self.assertIn("&lt;b&gt;Doble&lt;/b&gt; &amp; Co", mail["html"])

	def test_no_mail_without_an_email_or_for_another_source(self):
		res = self.book("es", mail="")["reservation"]
		self.pay(res)
		self.assertEqual(self.status(res), "Confirmed")
		self.assertEqual(self.mails(res), [])

		from kamra.folio import open_folio

		guest = make_guest("UT Recepción", email="recepcion-ut@example.com")
		desk = make_reservation(PROP, self.rt, d(60), d(62), status="Held", guest=guest, source="Walk-in")
		folio = open_folio(desk)
		webhook.receive(*signed(event("payment.approved", folio, payment_id=str(uuid.uuid4()))))
		self.assertEqual(self.status(desk.name), "Confirmed")
		self.assertEqual(self.mails(desk.name), [])

	def test_no_mail_when_the_desk_confirms_without_money(self):
		res = self.book("es")["reservation"]
		doc = frappe.get_doc("Reservation", res)
		from kamra.api import _confirm_status

		_confirm_status(doc)
		self.assertEqual(self.status(res), "Confirmed")
		self.assertEqual(self.mails(res), [])

	def test_a_mail_that_cannot_go_never_stops_the_confirmation(self):
		for name in frappe.get_all("Email Account", {"enable_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, {"enable_outgoing": 0, "default_outgoing": 0})
		frappe.local.outgoing_email_account = {}
		res = self.book("es")["reservation"]
		self.assertEqual(self.pay(res)["status"], "Applied")
		self.assertEqual(self.status(res), "Confirmed")
		self.assertEqual(self.mails(res), [])
		self.assertFalse(frappe.db.get_value("Reservation", res, confirmation.SENT_FIELD))
		logs = [r for r in self.error_logs() if r.method == confirmation.LOG_TITLE]
		self.assertEqual(len(logs), 1, self.error_logs())
		self.assertEqual((logs[0].reference_doctype, logs[0].reference_name), ("Reservation", res))
		for secret in (GUEST_MAIL, PAY_1, "UT Ana"):
			self.assertNotIn(secret, logs[0].error or "")
