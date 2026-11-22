"""El cableado del monitoreo (HR-21) en hooks.py, como lo ve Frappe: quién arranca el latido, quién manda los errores a
Sentry, quién filtra el cliente de Frappe y quién quita el DSN del escritorio; y que el endpoint existe y es GET."""

import frappe
from kamra.tests.audit_fixtures import AuditTestCase

from kamra_uy import health


class WiringTestCase(AuditTestCase):
	properties = ()

	def hooks(self, name):
		return frappe.get_hooks(name, app_name="kamra_uy")

	def test_the_scheduler_beats_every_minute(self):
		self.assertIn("kamra_uy.health.beat.heartbeat", self.hooks("scheduler_events")["cron"]["* * * * *"])

	def test_every_error_log_goes_to_sentry(self):
		handlers = self.hooks("doc_events")["Error Log"]["after_insert"]
		self.assertIn("kamra_uy.health.sentry.on_error_log", handlers if isinstance(handlers, list) else [handlers])

	def test_the_client_is_scrubbed_in_requests_and_jobs(self):
		self.assertIn("kamra_uy.health.sentry.setup", self.hooks("before_request"))
		self.assertIn("kamra_uy.health.sentry.setup", self.hooks("before_job"))

	def test_the_desk_never_gets_the_dsn(self):
		self.assertIn("kamra_uy.health.sentry.extend_bootinfo", self.hooks("extend_bootinfo"))

	def test_the_endpoint_resolves_and_is_a_guest_get(self):
		method = frappe.get_attr("kamra_uy.health.status")
		self.assertIs(method, health.status)
		self.assertIn(method, frappe.whitelisted)
		self.assertIn(method, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["GET"])
