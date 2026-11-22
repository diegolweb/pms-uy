"""A property outside India never reads as Indian.

Kamra started in India, and a few of its fallbacks still assumed it: a
property with no country took the India pack, the bill borrowed GST's "SAC"
column and "Place of supply" line when the country had neither, and the
amount in words came out in Indian-style English ("... Only") whatever the
property's language. Here a property in a country with no pack of its own
(Chile, on the generic pack) is billed and booked end to end, and nothing
on its bill or its booking page may carry an Indian term. An India property
is the control: it reads exactly as before.
"""

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from kamra import api, localization, public_api
from kamra import folio as folio_mod
from kamra.localization import generic, india, thailand
from kamra.localization import words
from kamra.tests.audit_fixtures import (
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_reservation,
	make_room,
	make_room_type,
)

CHILE = "UT No India Chile"
INDIA = "UT No India Control"

BANNED = ("GST", "CGST", "SGST", "SAC", "Place of supply", "Rupee", "Paise", "₹",
          "INR", "Indian", "lakh", "crore", "Pincode", "Aadhaar", "Karnataka")


def _values(value):
	"""Every leaf VALUE of a payload as text - keys are field names, not
	words a guest reads."""
	if isinstance(value, dict):
		for v in value.values():
			yield from _values(v)
	elif isinstance(value, (list, tuple, set)):
		for v in value:
			yield from _values(v)
	elif value is not None:
		yield str(value)


def _single(country="", language=""):
	"""A stand-in for frappe.db.get_single_value with these System Settings."""
	real = frappe.db.get_single_value
	values = {"country": country, "language": language}

	def get(doctype, field, *args, **kwargs):
		if doctype == "System Settings" and field in values:
			return values[field]
		return real(doctype, field, *args, **kwargs)
	return get


class NoIndiaDefaultTestCase(AuditTestCase):
	properties = (CHILE, INDIA)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		frappe.local.lang = "en"
		self.chile = self._property(CHILE, country="Chile", state="Region Metropolitana",
		                            currency="EUR", booking_language="es")
		self.india = self._property(INDIA, country="India", state="Karnataka",
		                            currency="INR")

	def _property(self, name, **fields):
		doc = frappe.get_doc({
			"doctype": "Property", "property_name": name, "city": "Testville",
			"business_date": TODAY, "booking_engine_enabled": 1,
			"gst_mode": "Fixed", "no_show_charge": "None", "rates_include_tax": 0,
			"room_levy_percent": 0, "require_cashier_pin": 0, **fields,
		}).insert(ignore_permissions=True)
		frappe.clear_document_cache("Property", doc.name)
		return doc.name

	def _blank_country(self, prop):
		frappe.db.set_value("Property", prop, "country", "")
		frappe.clear_document_cache("Property", prop)

	def _settled_folio(self, prop, amount):
		rt = make_room_type(prop, "STD", base_price=1000)
		room = make_room(prop, rt, "101")
		res = make_reservation(prop, rt, d(-1), d(1), status="Checked In", room=room)
		folio = guest_folio(res.name)
		api.add_folio_charge(folio, "Misc", "UT Misc", amount, gst_rate=0)
		total = float(frappe.db.get_value("Folio", folio, "grand_total"))
		api.add_folio_payment(folio, "Cash", total)
		folio_mod.close_folio(folio)
		return folio

	# 1. the country -------------------------------------------------------

	def test_no_country_is_the_generic_pack_never_india(self):
		self.assertIs(localization.pack_for_country(""), generic)
		with patch.object(localization, "default_country", return_value=""):
			self.assertIs(localization.pack_for(None), generic)
			self._blank_country(INDIA)
			self.assertIs(localization.pack_for(INDIA), generic)

	def test_no_country_takes_the_install_country(self):
		self._blank_country(INDIA)
		with patch.object(localization, "default_country", return_value="Thailand"):
			self.assertIs(localization.pack_for(None), thailand)
			self.assertIs(localization.pack_for(INDIA), thailand)
			self.assertEqual(localization.property_country(INDIA), "Thailand")

	def test_a_property_keeps_its_own_country(self):
		self.assertIs(localization.pack_for(self.india), india)
		self.assertIs(localization.pack_for(self.chile), generic)
		with patch.object(localization, "default_country", return_value="Thailand"):
			self.assertEqual(localization.property_country(self.chile), "Chile")

	def test_default_country_reads_system_settings(self):
		with patch.object(frappe.db, "get_single_value", side_effect=_single(" Thailand ")):
			self.assertEqual(localization.default_country(), "Thailand")
		with patch.object(frappe.db, "get_single_value", side_effect=_single(None)):
			self.assertEqual(localization.default_country(), "")

	# 2. the country /book and the desk read -------------------------------

	def test_booking_payloads_never_say_india(self):
		payload = public_api._property_payload(frappe.get_doc("Property", CHILE))
		self.assertEqual(payload["country"], "Chile")
		self.assertEqual(api._booking_property_policy(CHILE)["country"], "Chile")
		self._blank_country(CHILE)
		with patch.object(frappe.db, "get_single_value", side_effect=_single("")):
			payload = public_api._property_payload(frappe.get_doc("Property", CHILE))
			self.assertEqual(payload["country"], "")
			self.assertEqual(api._booking_property_policy(CHILE)["country"], "")
		with patch.object(frappe.db, "get_single_value", side_effect=_single("Thailand")):
			payload = public_api._property_payload(frappe.get_doc("Property", CHILE))
			self.assertEqual(payload["country"], "Thailand")

	# 3-4. the bill --------------------------------------------------------

	def test_a_generic_bill_borrows_nothing_from_gst(self):
		bill = api.folio_invoice(self._settled_folio(CHILE, 1250.50))
		self.assertTrue(bill["document"]["is_final"])
		self.assertIsNone(bill["document"]["service_code_label"])
		self.assertIsNone(bill["property"]["place_of_supply"])
		self.assertEqual(bill["document"]["amount_in_words"],
		                 "Mil doscientos cincuenta euros con cincuenta céntimos")

	def test_an_india_bill_reads_as_before(self):
		bill = api.folio_invoice(self._settled_folio(INDIA, 1250.50))
		self.assertEqual(bill["document"]["service_code_label"], "SAC")
		self.assertEqual(bill["property"]["place_of_supply"], "Karnataka")
		self.assertTrue(bill["document"]["amount_in_words"].endswith(" Only"))
		self.assertTrue(bill["document"]["amount_in_words"].startswith("Rupees "))

	def test_a_banquet_document_has_no_sac_fallback(self):
		from kamra import banquet
		from kamra.tests.fixtures import PROPERTY, build, enquiry

		fn = enquiry(build())
		# the banquet fixture's hotel is in India (the field's default)
		header = banquet.banquet_document(fn, "quote")["header"]
		self.assertEqual(header["service_code_label"], "SAC")
		frappe.db.set_value("Property", PROPERTY, "country", "Chile")
		frappe.clear_document_cache("Property", PROPERTY)
		try:
			header = banquet.banquet_document(fn, "quote")["header"]
		finally:
			frappe.clear_document_cache("Property", PROPERTY)
		self.assertIsNone(header["service_code_label"])
		self.assertIsNone(header["place_of_supply"])

	# 5. amounts in words --------------------------------------------------

	def test_amount_in_words_in_a_language(self):
		self.assertEqual(words.amount_in_words_in(Decimal("1.00"), "USD", "es"),
		                 "Un dólar con cero centavos")
		# a whole number is a whole number, never read as cents
		self.assertEqual(words.amount_in_words_in(1, "USD", "es"), "Un dólar con cero centavos")
		self.assertEqual(words.amount_in_words_in(Decimal("21000.00"), "UYU", "es"),
		                 "Veintiún mil pesos con cero centésimos")
		self.assertEqual(words.amount_in_words_in("1250.50", "EUR", "ES"),
		                 "Mil doscientos cincuenta euros con cincuenta céntimos")

	def test_amount_in_words_in_gives_up_cleanly(self):
		self.assertIsNone(words.amount_in_words_in(10, "XYZ", "es"))
		self.assertIsNone(words.amount_in_words_in(100, "USD", "en"))
		self.assertIsNone(words.amount_in_words_in(100, "USD", ""))
		self.assertIsNone(words.amount_in_words_in(100, "USD", None))
		self.assertIsNone(words.amount_in_words_in(100, "USD", "xx"))
		# num2words' Arabic speller names every currency as Saudi riyals:
		# a dollar bill must not read as riyals
		self.assertIsNone(words.amount_in_words_in(100, "USD", "ar"))
		self.assertIsNone(words.amount_in_words_in(100, "AED", "ar"))
		self.assertTrue(words.amount_in_words_in(100, "SAR", "ar"))

	def test_invoice_language(self):
		prop = frappe._dict(booking_language="es")
		self.assertEqual(localization.invoice_language(prop), "es")
		with patch.object(frappe.db, "get_single_value", side_effect=_single(language="pt-BR")):
			self.assertEqual(localization.invoice_language(frappe._dict(booking_language="")), "pt")
		with patch.object(frappe.db, "get_single_value", side_effect=_single(language="")):
			self.assertEqual(localization.invoice_language(frappe._dict()), "en")

	def test_amount_in_words_falls_back_to_english(self):
		prop = frappe._dict(name=None, currency="EUR", booking_language="en")
		self.assertEqual(localization.amount_in_words(generic, prop, 12),
		                 "Euros Twelve Only")
		prop.booking_language = "pt"
		self.assertEqual(localization.amount_in_words(generic, prop, 12), "Doze euros")
		# a currency the language's speller does not know: English, not empty
		prop.update(currency="XYZ", booking_language="es")
		self.assertEqual(localization.amount_in_words(generic, prop, 12), "XYZ Twelve Only")

	# 6. the sweep ---------------------------------------------------------

	def test_nothing_indian_on_the_bill_or_the_booking_page(self):
		bill = api.folio_invoice(self._settled_folio(CHILE, 1250.50))
		ci, co = add_days(nowdate(), 5), add_days(nowdate(), 7)
		surfaces = {
			"folio_invoice": bill,
			"showcase": public_api.showcase(CHILE),
			"search_stay": public_api.search_stay(CHILE, ci, co, 2, 0),
		}
		self.assertTrue(surfaces["search_stay"], "the stay search found no room to sell")
		for surface, payload in surfaces.items():
			for text in _values(payload):
				for word in BANNED:
					with self.subTest(surface=surface, word=word):
						self.assertNotIn(word, text)
				self.assertFalse(text.endswith(" Only"), f"{surface}: {text!r}")
