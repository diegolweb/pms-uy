"""El modo contingencia sin sincronización (HR-34) dentro de Kamra: el informe por plataforma con las libres del mismo
motor que la guarda de doble reserva, la página /contingencia y su botón, el apagado automático con Channex y los
avisos por correo (en `Email Queue`: en hr-gate no hay cuenta de correo).

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.contingency.tests.test_contingency`).
"""

import email
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, nowdate
from kamra import api as kamra_api
from kamra.tests.audit_fixtures import (
	AuditTestCase,
	ensure_user,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

from kamra_uy.contingency import alerts, api, report
from kamra_uy.www import contingencia as page

PROP = "UT UY Contingencia"
DESK = "recepcion-contingencia@roosevelt.test"
NOBODY = "sin-rol-contingencia@roosevelt.test"
NOTIFIED = "uy_contingency_notified:" + PROP


class ContingencyCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		frappe.cache.delete_value(NOTIFIED)
		self._forget_email_accounts()
		# hr-gate no tiene cuenta de correo (y así sigue): sin una saliente, frappe.sendmail no encola. Esta vive solo
		# dentro del test (se deshace con el savepoint) y en tests Frappe nunca conecta al SMTP.
		frappe.get_doc({"doctype": "Email Account", "email_account_name": "UT Contingencia",
		                "email_id": "contingencia@roosevelt.test", "enable_outgoing": 1, "default_outgoing": 1,
		                "smtp_server": "smtp.roosevelt.test", "no_smtp_authentication": 1}).insert(ignore_permissions=True)
		make_property(PROP)
		self.room_type = make_room_type(PROP, "CDBL")
		self.label = frappe.db.get_value("Room Type", self.room_type, "room_type_name")
		self.rooms = [make_room(PROP, self.room_type, f"C{n}") for n in range(1, 8)]
		self.night = getdate(add_days(nowdate(), 10))
		self.settings = frappe.get_doc({
			"doctype": "UY Contingency Settings", "property": PROP, "enabled": 1,
			"recipients": "recepcion@roosevelt.test, fundador@roosevelt.test",
			"platforms": [
				{"platform": "Booking.com", "aliases": "Booking", "room_type": self.room_type, "quota": 4, "priority": 1},
				{"platform": "Expedia", "room_type": self.room_type, "quota": 4, "priority": 2},
				{"platform": "Airbnb", "room_type": self.room_type, "quota": 4, "priority": 3},
			],
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.cache.delete_value(NOTIFIED)
		super().tearDown()
		self._forget_email_accounts()

	@staticmethod
	def _forget_email_accounts():
		# EmailAccount.find_outgoing guarda lo encontrado en frappe.local, que dura todo run-tests: sin esto, la cuenta
		# de prueba (ya deshecha) seguiría «existiendo» para las suites que vienen detrás.
		frappe.local.outgoing_email_account = {}

	# --- ayudas

	def book(self, n: int, assigned: int = 0, channel: str | None = None):
		"""n reservas de una noche (`self.night`); las `assigned` primeras con habitación, el resto sin número (como
		llega una reserva de plataforma)."""
		out = []
		for i in range(n):
			room = self.rooms[-(i + 1)] if i < assigned else None
			out.append(make_reservation(PROP, self.room_type, str(self.night), str(add_days(self.night, 1)),
			                            room=room, channel=channel))
		return out

	def row(self, result: dict) -> dict:
		return next(r for r in result["rows"] if r["date"] == str(self.night) and r["room_type"] == self.room_type)

	def actions(self, row: dict) -> list[tuple[str, str, int]]:
		return [(i.platform, i.action, i.rooms) for i in row["instructions"]]

	def mails(self) -> int:
		return frappe.db.count("Email Queue", {"reference_doctype": "UY Contingency Settings", "reference_name": PROP})

	def last_mail(self) -> str:
		name = frappe.get_all("Email Queue", {"reference_doctype": "UY Contingency Settings", "reference_name": PROP},
		                      pluck="name", order_by="creation desc", limit=1)[0]
		raw = frappe.db.get_value("Email Queue", name, "message")
		# la cola guarda el MIME entero (con tildes, en base64 o quoted-printable): se lee el texto decodificado
		parts = [p.get_payload(decode=True) for p in email.message_from_string(raw).walk()
		         if p.get_content_maintype() == "text"]
		return "\n".join(part.decode("utf-8", "replace") for part in parts if part)


class TestContingencyReport(ContingencyCase):
	def test_three_free_closes_every_platform_and_alerts(self):
		# la aceptación: cupos 4/4/4 y 3 libres → las tres cerradas. Dos de las reservas no tienen habitación (como
		# una de plataforma): `_available_rooms_raw` solo no las ve; las libres se cuentan como el channel manager.
		# El prompt pide además `alert` True con 3 libres: con el umbral por defecto (≤ 2) no lo es, así que aquí el
		# umbral se sube a 3; el caso del umbral por defecto es el test siguiente.
		self.settings.db_set("alert_threshold", 3)
		self.book(4, assigned=2)
		row = self.row(report.build_report(PROP))
		self.assertEqual(row["free"], 3)
		self.assertEqual(self.actions(row), [("Booking.com", "close", 0), ("Expedia", "close", 0), ("Airbnb", "close", 0)])
		self.assertIs(row["alert"], True)

	def test_three_free_is_no_alert_with_the_default_threshold(self):
		self.book(4)
		self.assertIs(self.row(report.build_report(PROP))["alert"], False)

	def test_alert_at_the_threshold(self):
		self.book(5, assigned=5)
		row = self.row(report.build_report(PROP))
		self.assertEqual(row["free"], 2)
		self.assertTrue(row["alert"])

	def test_seven_free_opens_only_booking(self):
		row = self.row(report.build_report(PROP))
		self.assertEqual(row["free"], 7)
		self.assertEqual(self.actions(row), [("Booking.com", "open", 4), ("Expedia", "close", 0), ("Airbnb", "close", 0)])

	def test_a_channel_alias_counts_as_its_platform_without_case(self):
		self.book(1, channel="booking")
		self.book(1, channel="Expedia")
		self.book(1, channel="Llamada")
		row = self.row(report.build_report(PROP))
		self.assertEqual(row["sold"], {"Booking.com": 1, "Expedia": 1, "Airbnb": 0})

	def test_the_report_covers_the_horizon_from_the_start(self):
		result = report.build_report(PROP, start=self.night)
		self.assertTrue(result["active"])
		self.assertEqual(result["start"], str(self.night))
		self.assertEqual(result["days"], 120)
		self.assertEqual(len(result["rows"]), 120)
		self.assertEqual(result["rows"][0]["date"], str(self.night))

	def test_pending_changes_against_the_last_applied_plan(self):
		self.book(3, assigned=1)
		self.assertGreater(report.build_report(PROP)["pending_changes"], 0)  # nada marcado aún
		api.mark_applied(PROP)
		self.assertEqual(report.build_report(PROP)["pending_changes"], 0)
		self.book(1)  # 3 libres: Booking se cierra
		result = report.build_report(PROP)
		self.assertEqual(result["pending_changes"], 1)
		self.assertTrue(self.row(result)["changed"])
		snapshot = frappe.get_last_doc("UY Contingency Snapshot", filters={"property": PROP})
		self.assertEqual(snapshot.applied_by, "Administrator")

	def test_mark_applied_refuses_a_plan_that_changed_since_the_page_was_read(self):
		seen = report.build_report(PROP)["plan_hash"]
		self.book(4)
		with self.assertRaises(frappe.ValidationError):
			api.mark_applied(PROP, plan_hash=seen)
		self.assertEqual(api.mark_applied(PROP, plan_hash=report.build_report(PROP)["plan_hash"])["pending_changes"], 0)

	def test_an_active_channex_connection_switches_the_mode_off(self):
		frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "Channex",
		                "active": 1}).insert(ignore_permissions=True)
		result = report.build_report(PROP)
		self.assertIs(result["active"], False)
		self.assertEqual(result["rows"], [])
		self.assertEqual(result["reason"], "synced")
		self.assertIs(report.is_active(PROP), False)

	def test_an_inactive_or_other_connection_keeps_the_mode_on(self):
		frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "Channex",
		                "active": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "AioSell",
		                "active": 1, "api_username": "u", "api_key": "k"}).insert(ignore_permissions=True)
		self.assertIs(report.is_active(PROP), True)

	def test_disabled_or_missing_settings_are_off(self):
		self.settings.db_set("enabled", 0)
		self.assertEqual(report.build_report(PROP)["reason"], "disabled")
		self.assertIs(report.is_active("UT UY No existe"), False)

	def test_active_without_quotas_says_so(self):
		self.settings.platforms = []
		self.settings.save(ignore_permissions=True)
		result = report.build_report(PROP)
		self.assertTrue(result["active"])
		self.assertTrue(result["missing_quotas"])
		self.assertEqual(result["rows"], [])

	def test_settings_refuse_a_negative_quota(self):
		self.settings.platforms[0].quota = -1
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_settings_refuse_a_room_type_of_another_property(self):
		make_property("UT UY Contingencia Otra")
		other = make_room_type("UT UY Contingencia Otra", "COTR")
		self.settings.append("platforms", {"platform": "Despegar", "room_type": other, "quota": 1, "priority": 4})
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)

	def test_settings_refuse_a_bad_recipient(self):
		self.settings.recipients = "recepcion@roosevelt.test, no-es-un-correo"
		with self.assertRaises(frappe.ValidationError):
			self.settings.save(ignore_permissions=True)


class TestContingencyAccess(ContingencyCase):
	def test_a_user_without_the_role_is_refused_on_the_page_and_the_button(self):
		ensure_user(NOBODY, ["Housekeeping"])
		frappe.set_user(NOBODY)  # nosemgrep: frappe-setuser -- test fixture
		with self.assertRaises(frappe.PermissionError):
			page.get_context(frappe._dict())
		with self.assertRaises(frappe.PermissionError):
			api.mark_applied(PROP)

	def test_a_guest_is_refused_on_the_page(self):
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- test fixture
		with self.assertRaises(frappe.PermissionError):
			page.get_context(frappe._dict())

	def test_front_desk_sees_the_page_and_marks_it_done(self):
		ensure_user(DESK, ["Front Desk"])
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- test fixture
		api.mark_applied(PROP)
		self.book(4)
		with patch.dict(frappe.form_dict, {"property": PROP}, clear=True):
			context = frappe._dict()
			page.get_context(context)
		self.assertEqual(context.property, PROP)
		self.assertTrue(context.report["active"])
		self.assertTrue(context.csrf_token)
		# lo que cambió va arriba, aunque no sea la primera fecha
		self.assertEqual((context.rows[0]["date"], context.rows[0]["changed"]), (str(self.night), True))
		self.assertFalse(context.rows[1]["changed"])
		self.assertEqual(api.mark_applied(PROP)["pending_changes"], 0)
		snapshot = frappe.get_last_doc("UY Contingency Snapshot", filters={"property": PROP})
		self.assertEqual(snapshot.applied_by, DESK)

	def test_the_page_when_the_mode_is_off(self):
		frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "Channex",
		                "active": 1}).insert(ignore_permissions=True)
		with patch.dict(frappe.form_dict, {"property": PROP}, clear=True):
			context = frappe._dict()
			page.get_context(context)
		self.assertEqual(context.off_message,
		                 "La sincronización con las plataformas está activa: el modo contingencia está apagado.")


class TestContingencyAlerts(ContingencyCase):
	def test_a_booking_that_closes_a_date_sends_one_mail(self):
		self.book(3)
		api.mark_applied(PROP)
		before = self.mails()
		self.book(1)
		self.assertEqual(self.mails(), before + 1)
		day = self.night.strftime("%d-%m")
		self.assertIn(f"Cerrar Booking.com el {day} en {self.label}: quedan 3", self.last_mail())
		self.assertIn("/contingencia?property=", self.last_mail())

	def test_nothing_changes_no_mail(self):
		api.mark_applied(PROP)
		before = self.mails()
		self.book(1)  # 6 libres: Booking sigue abierto con 4
		self.assertEqual(self.mails(), before)

	def test_the_same_change_is_not_mailed_twice(self):
		self.book(3)
		api.mark_applied(PROP)
		self.book(1)
		after = self.mails()
		alerts.recompute(PROP, str(self.night), str(add_days(self.night, 1)))
		self.assertEqual(self.mails(), after)

	def test_entering_the_alert_mails_even_without_a_plan_change(self):
		self.book(4)
		api.mark_applied(PROP)  # todo cerrado y aplicado
		before = self.mails()
		self.book(1)  # 2 libres: entra en alerta
		self.assertEqual(self.mails(), before + 1)
		self.assertIn(f"Últimas habitaciones: quedan 2 en {self.label}", self.last_mail())

	def test_mode_off_sends_nothing(self):
		self.settings.db_set("enabled", 0)
		before = self.mails()
		self.book(5)
		alerts.recompute(PROP, str(self.night), str(add_days(self.night, 1)))
		self.assertEqual(self.mails(), before)

	def test_channex_on_sends_nothing(self):
		frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "Channex",
		                "active": 1}).insert(ignore_permissions=True)
		before = self.mails()
		self.book(5)
		self.assertEqual(self.mails(), before)

	def test_a_cancellation_reopens_and_mails(self):
		booked = self.book(4)
		api.mark_applied(PROP)
		before = self.mails()
		kamra_api.cancel_reservation(booked[0].name, waive_fee=1)
		self.assertEqual(self.mails(), before + 1)
		self.assertIn(f"Abrir Booking.com con 4 el {self.night.strftime('%d-%m')}", self.last_mail())

	def test_a_failing_alert_never_breaks_the_booking(self):
		with patch.object(alerts, "recompute", side_effect=RuntimeError("caído")):
			booked = self.book(1)
		self.assertTrue(frappe.db.exists("Reservation", booked[0].name))

	def test_the_daily_report_goes_once_at_the_hotel_hour(self):
		before = self.mails()
		with patch.object(alerts, "is_daily_report_hour", return_value=False):
			alerts.daily_report()
		self.assertEqual(self.mails(), before)
		with patch.object(alerts, "is_daily_report_hour", return_value=True):
			alerts.daily_report()
		self.assertEqual(self.mails(), before + 1)
		self.assertIn("/contingencia?property=", self.last_mail())
		self.assertIn("Booking.com, abrir con 4", self.last_mail())
		# El punto 24 (D-HR-30): «en los próximos N días, desde el dd-mm», sin la fecha en ISO.
		start = getdate(nowdate())
		self.assertIn(f"en los próximos {self.settings.horizon_days or report.DEFAULT_HORIZON} días, desde el {start.strftime('%d-%m')}:",
		              self.last_mail())
		self.assertNotIn(f"del {start.isoformat()}", self.last_mail())

	def test_the_daily_report_skips_a_property_with_the_mode_off(self):
		self.settings.db_set("enabled", 0)
		before = self.mails()
		with patch.object(alerts, "is_daily_report_hour", return_value=True):
			alerts.daily_report()
		self.assertEqual(self.mails(), before)


class TestContingencyPageRenders(ContingencyCase):
	"""La plantilla entera, como la sirve Frappe (no solo get_context)."""

	def render(self, user: str):
		from frappe.utils import set_request
		from frappe.website.serve import get_response

		frappe.set_user(user)  # nosemgrep: frappe-setuser -- test fixture
		saved = getattr(frappe.local, "request", None)
		try:
			set_request(method="GET", path="/contingencia", query_string=f"property={PROP}")
			with patch.dict(frappe.form_dict, {"property": PROP}, clear=True):
				response = get_response("contingencia")
		finally:
			frappe.local.request = saved
		return response.status_code, response.get_data(as_text=True)

	def test_front_desk_gets_the_table_with_the_change_on_top_and_the_button(self):
		ensure_user(DESK, ["Front Desk"])
		api.mark_applied(PROP)
		self.book(4)
		status, html = self.render(DESK)
		self.assertEqual(status, 200)
		self.assertIn("Ya lo hice en las extranets", html)
		self.assertIn("Hay 1 cambio para poner en las extranets", html)
		first_row = html.index('<tr class="changed')
		self.assertLess(first_row, html.index("Abrir con 4"))  # el resto de fechas, con Booking abierto, va debajo
		self.assertIn("kamra_uy.contingency.api.mark_applied", html)

	def test_a_guest_gets_403(self):
		status, _html = self.render("Guest")
		self.assertEqual(status, 403)

	def test_the_off_page_is_one_line(self):
		frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "Channex",
		                "active": 1}).insert(ignore_permissions=True)
		status, html = self.render("Administrator")
		self.assertEqual(status, 200)
		self.assertIn("La sincronización con las plataformas está activa: el modo contingencia está apagado.", html)
		self.assertNotIn("<table>", html)


# --- HR-82: el Error Log sin variables locales, y la página y el botón limitados a la propiedad del usuario

OTHER = "UT UY Contingencia Otra"
THIRD = "UT UY Contingencia Tercera"
SCOPED = "recepcion-otra-contingencia@roosevelt.test"
GUEST_DATA = {"first_name": "Mariela", "last_name": "Quintana Ferreyra",
              "email": "mariela.quintana@roosevelt.test", "phone": "+598 99 123 456"}


class TestContingencyErrorLog(ContingencyCase):
	"""Un aviso que falla deja su Error Log 'Contingencia' con la traza SOLA: con `log_error` sin `message`, Frappe
	vuelca las variables locales de cada marco (en developer_mode; `frappe.get_traceback(with_context=True)`)."""

	def new_logs(self, before: int) -> list[str]:
		count = frappe.db.count("Error Log", {"method": alerts.LOG_TITLE}) - before
		self.assertGreater(count, 0, "el fallo forzado no dejó ningún Error Log 'Contingencia'")
		return frappe.get_all("Error Log", filters={"method": alerts.LOG_TITLE}, pluck="error",
		                      order_by="creation desc", limit=count)

	def before(self) -> int:
		return frappe.db.count("Error Log", {"method": alerts.LOG_TITLE})

	def test_a_failing_reservation_change_logs_no_guest_and_no_locals(self):
		guest = frappe.get_doc({"doctype": "Guest", **GUEST_DATA}).insert(ignore_permissions=True)
		before = self.before()
		with patch.dict(frappe.conf, {"developer_mode": 1}), \
		     patch.object(alerts, "_stays", side_effect=RuntimeError("caído")):
			booked = make_reservation(PROP, self.room_type, str(self.night), str(add_days(self.night, 1)),
			                          guest=guest.name)
		self.assertTrue(frappe.db.exists("Reservation", booked.name))
		for error in self.new_logs(before):
			self.assertIn("RuntimeError: caído", error)
			for secret in (*GUEST_DATA.values(), "99 123 456", guest.name, booked.name, "<Reservation"):
				self.assertNotIn(secret, error)

	def test_a_failing_recompute_logs_no_locals(self):
		before = self.before()
		with patch.dict(frappe.conf, {"developer_mode": 1}), \
		     patch.object(report, "settings_for", side_effect=RuntimeError("caído")):
			self.assertEqual(alerts.recompute(PROP, str(self.night), str(add_days(self.night, 1))), 0)
		for error in self.new_logs(before):
			self.assertIn("RuntimeError: caído", error)
			self.assertNotIn(f"'{PROP}'", error)
			self.assertNotIn(f"'{self.night}'", error)

	def test_a_failing_daily_report_logs_no_locals(self):
		before = self.before()
		with patch.dict(frappe.conf, {"developer_mode": 1}), \
		     patch.object(alerts, "is_daily_report_hour", side_effect=RuntimeError("caído")):
			self.assertEqual(alerts.daily_report(), 0)
		for error in self.new_logs(before):
			self.assertIn("RuntimeError: caído", error)
			self.assertNotIn(f"'{PROP}'", error)


class TestContingencyPropertyScope(ContingencyCase):
	"""Recepción limitada por User Permission a otra propiedad: ni la página ni el botón le sirven la de otro hotel, y
	la lista para elegir solo trae las suyas."""

	properties = (PROP, OTHER, THIRD)

	def setUp(self):
		super().setUp()
		make_property(OTHER)
		make_property(THIRD)
		frappe.get_doc({"doctype": report.SETTINGS, "property": OTHER, "enabled": 1}).insert(ignore_permissions=True)
		ensure_user(SCOPED, ["Front Desk"])

	def tearDown(self):
		super().tearDown()
		frappe.clear_cache(user=SCOPED)  # la caché de User Permission vive fuera de la transacción deshecha

	def limit_to(self, *properties: str):
		for p in properties:
			frappe.get_doc({"doctype": "User Permission", "user": SCOPED, "allow": "Property",
			                "for_value": p}).insert(ignore_permissions=True)
		frappe.clear_cache(user=SCOPED)
		frappe.set_user(SCOPED)  # nosemgrep: frappe-setuser -- the restricted persona

	def test_the_page_of_another_property_is_refused(self):
		self.limit_to(OTHER)
		with patch.dict(frappe.form_dict, {"property": PROP}, clear=True), self.assertRaises(frappe.PermissionError):
			page.get_context(frappe._dict())

	def test_mark_applied_on_another_property_is_refused(self):
		self.limit_to(OTHER)
		with self.assertRaises(frappe.PermissionError):
			api.mark_applied(PROP)
		self.assertFalse(frappe.db.exists("UY Contingency Snapshot", {"property": PROP}))

	def test_without_a_property_the_page_opens_the_users_own(self):
		self.limit_to(OTHER)
		with patch.dict(frappe.form_dict, {}, clear=True):
			context = frappe._dict()
			page.get_context(context)
		self.assertEqual(context.property, OTHER)

	def test_the_choices_list_only_the_users_properties(self):
		self.limit_to(OTHER, THIRD)
		with patch.dict(frappe.form_dict, {}, clear=True):
			context = frappe._dict()
			page.get_context(context)
		self.assertIsNone(context.property)
		self.assertEqual(context.choices, [OTHER])

	def test_strict_scope_without_permissions_lists_nothing(self):
		frappe.set_user(SCOPED)  # nosemgrep: frappe-setuser -- the unscoped persona under strict scope
		with patch.dict(frappe.conf, {"kamra_strict_property_scope": 1}), \
		     patch.dict(frappe.form_dict, {}, clear=True):
			context = frappe._dict()
			page.get_context(context)
		self.assertIsNone(context.property)
		self.assertEqual(context.choices, [])

	def test_the_rendered_page_of_another_property_is_403(self):
		from frappe.utils import set_request
		from frappe.website.serve import get_response

		self.limit_to(OTHER)
		saved = getattr(frappe.local, "request", None)
		try:
			set_request(method="GET", path="/contingencia", query_string=f"property={PROP}")
			with patch.dict(frappe.form_dict, {"property": PROP}, clear=True):
				response = get_response("contingencia")
		finally:
			frappe.local.request = saved
		self.assertEqual(response.status_code, 403)
		self.assertNotIn("Ya lo hice en las extranets", response.get_data(as_text=True))
