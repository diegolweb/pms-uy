"""Sentry sin datos personales (HR-21): todo Error Log sale hacia Sentry pasado por el filtro de kamra_uy/health/scrub.py,
y un cliente que haya creado Frappe (FRAPPE_SENTRY_DSN) también queda filtrado. Sin red: el transporte de sentry-sdk es
una lista. Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.health.tests.test_sentry`)."""

import json
import os
from unittest.mock import patch

import frappe
import sentry_sdk
from kamra.tests.audit_fixtures import AuditTestCase

from kamra_uy.health import sentry

DSN = "https://clave-de-prueba@sentry.example.com/1"
EMAIL = "juana.perez@correo.test"
PHONE = "+598 99 123 456"
NAME = "Juana Pérez"
PII = (EMAIL, "juana.perez", "99 123 456", "Juana", "Pérez")


def _leaks(events) -> list[str]:
	blob = json.dumps(events, ensure_ascii=False, default=str)
	return [p for p in PII if p in blob]


class SentryCase(AuditTestCase):
	properties = ()

	def setUp(self):
		super().setUp()
		self.previous = sentry_sdk.Hub.current.client
		sentry_sdk.Hub.current.bind_client(None)
		self.events = []
		self._stack.enter_context(patch.dict(os.environ, {}, clear=False))
		os.environ.pop(sentry.ENV_DSN, None)
		self.conf = self._stack.enter_context(patch.dict(frappe.local.conf, {sentry.CONF_DSN: DSN}))
		self._stack.enter_context(patch.object(sentry, "_transport", return_value=self.events.append))

	def tearDown(self):
		client = sentry_sdk.Hub.current.client
		if client is not None and client is not self.previous:
			client.close(timeout=0)
		sentry_sdk.Hub.current.bind_client(self.previous)
		super().tearDown()

	def fail_with_pii(self):
		guest = {"email": EMAIL, "phone": PHONE, "name": NAME}  # noqa: F841 — una variable local con datos
		raise frappe.DoesNotExistError(f"Guest {EMAIL} ({PHONE}) not found: <strong>{NAME}</strong>")


class ErrorLogToSentryTestCase(SentryCase):
	def test_an_error_log_goes_to_sentry_without_personal_data(self):
		try:
			self.fail_with_pii()
		except frappe.DoesNotExistError:
			log = frappe.log_error(title=f"Reserva de {EMAIL}")
		self.assertEqual(len(self.events), 1)
		event = self.events[0]
		self.assertEqual(_leaks(event), [])
		exc = event["exception"]["values"][-1]
		self.assertEqual(exc["type"], "DoesNotExistError")
		self.assertIn("fail_with_pii", [f["function"] for f in exc["stacktrace"]["frames"]])
		self.assertTrue(all("vars" not in f for f in exc["stacktrace"]["frames"]))
		self.assertEqual(event["tags"]["error_log"], log.name)
		self.assertIn("[correo]", event["tags"]["titulo"])

	def test_an_error_log_without_an_exception_is_a_scrubbed_message(self):
		frappe.get_doc({"doctype": "Error Log", "method": f"Reserva de {NAME!r}",
		                "error": f"Traceback (most recent call last):\nValueError: {EMAIL} {PHONE}"}).insert(
			ignore_permissions=True)
		self.assertEqual(len(self.events), 1)
		self.assertEqual(_leaks(self.events[0]), [])
		self.assertIn("ValueError", json.dumps(self.events[0]))

	def test_a_request_error_titled_with_its_message_is_sent_by_type(self):
		# lo que deja log_error_snapshot en un 500 de una petición (inserción diferida, sin la excepción a mano)
		trace = ("Traceback (most recent call last):\n  File \"apps/kamra/kamra/api.py\", line 9, in book\n"
		         "    raise frappe.DoesNotExistError\n"
		         f"frappe.exceptions.DoesNotExistError: Guest {NAME} not found")
		frappe.get_doc({"doctype": "Error Log", "method": f"Guest {NAME} not found", "error": trace}).insert(
			ignore_permissions=True)
		self.assertEqual(len(self.events), 1)
		self.assertEqual(_leaks(self.events[0]), [])
		self.assertEqual(self.events[0]["tags"]["titulo"], "frappe.exceptions.DoesNotExistError")
		self.assertIn("api.py", json.dumps(self.events[0]))

	def test_without_a_dsn_nothing_is_sent_and_no_client_is_made(self):
		self.conf.pop(sentry.CONF_DSN)
		frappe.log_error(title="UT sin Sentry", message="x")
		self.assertEqual(self.events, [])
		self.assertIsNone(sentry_sdk.Hub.current.client)
		self.assertFalse(sentry.setup())

	def test_when_frappe_sends_it_kamra_uy_does_not_send_it_again(self):
		os.environ[sentry.ENV_DSN] = DSN
		frappe.log_error(title="UT dos veces", message="x")
		self.assertEqual(self.events, [])

	def test_breadcrumbs_and_local_variables_never_leave(self):
		self.assertTrue(sentry.setup())
		sentry_sdk.add_breadcrumb(category="query", message=f"select * from tabGuest where email='{EMAIL}'")
		try:
			self.fail_with_pii()
		except frappe.DoesNotExistError:
			sentry_sdk.capture_exception()
		self.assertEqual(len(self.events), 1)
		self.assertEqual(_leaks(self.events[0]), [])
		self.assertNotIn("breadcrumbs", self.events[0])

	def test_a_broken_sentry_never_breaks_the_error_log(self):
		with patch.object(sentry_sdk, "capture_message", side_effect=RuntimeError("sentry roto")):
			doc = frappe.get_doc({"doctype": "Error Log", "method": "UT", "error": "x"}).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Error Log", doc.name))


class FrappeClientTestCase(SentryCase):
	def test_the_client_frappe_makes_is_scrubbed_too(self):
		# lo que hace frappe/app.py con FRAPPE_SENTRY_DSN: un cliente sin filtro y con las integraciones por defecto
		sentry_sdk.init(dsn=DSN, transport=self.events.append)
		self.assertTrue(sentry.setup())
		with sentry_sdk.configure_scope() as scope:
			scope.set_user({"id": "reservas.example.com", "email": EMAIL})
			scope.set_tag("frappe_user", EMAIL)
			scope.set_context("JSON Body", {"guest_name": NAME, "phone": PHONE})
		try:
			self.fail_with_pii()
		except frappe.DoesNotExistError:
			sentry_sdk.capture_exception()
		self.assertEqual(len(self.events), 1)
		self.assertEqual(_leaks(self.events[0]), [])
		with sentry_sdk.configure_scope() as scope:
			scope.clear()

	def test_the_frappe_client_stops_keeping_breadcrumbs_locals_and_pii(self):
		sentry_sdk.init(dsn=DSN, transport=self.events.append)
		sentry.setup()
		options = sentry_sdk.Hub.current.client.options
		self.assertIsNone(options["before_breadcrumb"]({"message": EMAIL}, {}))
		self.assertFalse(options["include_local_variables"])
		self.assertFalse(options["send_default_pii"])
		self.assertEqual(options["max_request_body_size"], "never")

	def test_setup_twice_does_not_wrap_twice(self):
		sentry_sdk.init(dsn=DSN, transport=self.events.append)
		sentry.setup()
		first = sentry_sdk.Hub.current.client.options["before_send"]
		sentry.setup()
		self.assertIs(sentry_sdk.Hub.current.client.options["before_send"], first)

	def test_a_filter_frappe_had_still_runs_first(self):
		sentry_sdk.init(dsn=DSN, transport=self.events.append, before_send=lambda event, hint: None)
		sentry.setup()
		sentry_sdk.capture_message("descartado por el filtro de antes")
		self.assertEqual(self.events, [])


class BrowserTestCase(SentryCase):
	def test_the_desk_never_gets_the_dsn(self):
		bootinfo = frappe._dict(sentry_dsn=DSN, other=1)
		sentry.extend_bootinfo(bootinfo=bootinfo)
		self.assertNotIn("sentry_dsn", bootinfo)
		self.assertEqual(bootinfo.other, 1)
