"""The site root when Kamra is the home page (HR-138).

install.py and scripts/first_boot.py set Website Settings.home_page = "kamra",
so Frappe serves www/kamra.py in place at "/" and "/index". The SPA's router
has the /kamra basename and draws nothing there: a blank page with only its
<title>. So the page sends those requests on to /kamra, keeping the query,
with a temporary redirect - a hotel may still pick its own home page later,
and browsers keep a 301 for good. /kamra and its routes still serve the app.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import set_request
from frappe.website.serve import get_response


class HomePageTestCase(IntegrationTestCase):
	def setUp(self):
		before = frappe.db.get_single_value("Website Settings", "home_page")
		self.addCleanup(self.home, before)
		self.addCleanup(frappe.set_user, "Administrator")  # nosemgrep: frappe-setuser -- test fixture
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- a visitor of the site
		self.home("kamra")

	def home(self, route: str | None):
		frappe.db.set_single_value("Website Settings", "home_page", route or "")
		frappe.cache.delete_key("home_page")

	def get(self, path: str, query: str = ""):
		frappe.flags.redirect_location = None
		set_request(method="GET", path=path, query_string=query)
		return get_response()

	def test_the_root_is_a_temporary_redirect_to_kamra(self):
		for path in ("/", "/index"):
			with self.subTest(path):
				response = self.get(path)
				self.assertEqual(response.status_code, 302)
				self.assertEqual(response.headers.get("Location"), "/kamra")

	def test_the_redirect_keeps_the_query(self):
		response = self.get("/", "utm_source=qr&lang=pt")
		self.assertEqual(response.status_code, 302)
		self.assertEqual(response.headers.get("Location"), "/kamra?utm_source=qr&lang=pt")

	def test_kamra_and_its_routes_still_serve_the_app(self):
		for path in ("/kamra", "/kamra/login", "/kamra/setup"):
			with self.subTest(path):
				response = self.get(path)
				self.assertEqual(response.status_code, 200)
				self.assertIsNone(response.headers.get("Location"))
				body = response.get_data(as_text=True)
				self.assertIn('id="root"', body)
				self.assertIn('type="module"', body)

	def test_a_home_page_of_the_hotel_is_not_sent_to_kamra(self):
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		route = "test-hotel-home-" + frappe.generate_hash(length=6)
		frappe.get_doc({"doctype": "Web Page", "title": "Hotel home", "route": route,
			"published": 1, "content_type": "Rich Text",
			"main_section": "<p>Bienvenidos al hotel</p>"}).insert(ignore_permissions=True)
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- a visitor of the site
		self.home(route)
		response = self.get("/")
		self.assertEqual(response.status_code, 200)
		self.assertIsNone(response.headers.get("Location"))
		self.assertIn("Bienvenidos al hotel", response.get_data(as_text=True))
