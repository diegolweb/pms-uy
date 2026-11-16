"""AGPL §13: every network-facing screen offers the corresponding source.

site_info() is the one public call every screen already makes, so it carries
the links. A site points them at its own published fork with
`bench --site <site> set-config -p source_code_urls '{"kamra": "<url>", ...}'`.
Without a usable URL of Kamra's own the offer is empty and no screen paints the
link: it stays off until the deployment sets the key (decision D-HR-25).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from kamra.public_api import _source_code_links, site_info

FORK = "https://example.test/f/tree/hr-abc"
LOCAL_APP = "https://example.test/u/tree/hr-abc"


class SourceCodeOfferTestCase(IntegrationTestCase):
	def offer(self, conf_value, installed_apps=("frappe", "kamra", "kamra_uy")):
		"""site_info()["source_code"] with the site config key set (or unset), never
		touching site_config.json."""
		with patch.dict(frappe.conf), patch(
			"frappe.get_installed_apps", return_value=list(installed_apps)):
			frappe.conf.pop("source_code_urls", None)
			if conf_value is not None:
				frappe.conf["source_code_urls"] = conf_value
			return site_info()["source_code"]

	def test_an_unconfigured_site_offers_nothing(self):
		self.assertEqual(self.offer(None), [])

	def test_a_configured_site_offers_kamra_first_then_the_installed_apps_in_order(self):
		links = self.offer({"kamra_uy": LOCAL_APP, "kamra": FORK},
			installed_apps=("frappe", "kamra_uy", "kamra"))
		self.assertEqual(links, [{"app": "kamra", "url": FORK},
			{"app": "kamra_uy", "url": LOCAL_APP}])

	def test_a_url_that_is_not_http_is_dropped(self):
		links = self.offer({"kamra": FORK, "kamra_uy": "javascript:alert(1)"})
		self.assertEqual(links, [{"app": "kamra", "url": FORK}])
		# Without a usable URL of Kamra's own nothing is offered, not even the other apps.
		self.assertEqual(self.offer({"kamra": "javascript:alert(1)", "kamra_uy": LOCAL_APP}), [])
		self.assertEqual(self.offer({"kamra": "ftp://example.test/f"}), [])
		self.assertEqual(self.offer({"kamra_uy": LOCAL_APP}), [])

	def test_a_value_that_is_not_an_object_is_ignored(self):
		for bad in ('{"kamra": "https://example.test/f"}', [FORK], 42, ""):
			with self.subTest(bad=bad):
				self.assertEqual(self.offer(bad), [])

	def test_an_app_that_is_not_installed_is_not_offered(self):
		links = self.offer({"kamra": FORK, "kamra_uy": LOCAL_APP, "other_app": LOCAL_APP},
			installed_apps=("frappe", "kamra"))
		self.assertEqual(links, [{"app": "kamra", "url": FORK}])

	def test_the_helper_is_pure_and_never_raises(self):
		self.assertEqual(_source_code_links(None, ["frappe", "kamra"]), [])
		self.assertEqual(_source_code_links({"kamra_uy": 7, "kamra": None}, ["kamra", "kamra_uy"]), [])
		self.assertEqual(_source_code_links({"kamra": " " + FORK + " "}, ["kamra"]),
			[{"app": "kamra", "url": FORK}])
