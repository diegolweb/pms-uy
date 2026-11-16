"""kamra_uy queda instalada sobre kamra en el sitio (lo corre `bench run-tests --app kamra_uy`)."""

import frappe
from frappe.tests import IntegrationTestCase


class TestInstallation(IntegrationTestCase):
	def test_installed_after_kamra(self):
		apps = frappe.get_installed_apps()
		self.assertIn("kamra", apps)
		self.assertIn("kamra_uy", apps)
		self.assertLess(apps.index("kamra"), apps.index("kamra_uy"))

	def test_module_is_registered(self):
		self.assertEqual(frappe.db.get_value("Module Def", "Kamra UY", "app_name"), "kamra_uy")
