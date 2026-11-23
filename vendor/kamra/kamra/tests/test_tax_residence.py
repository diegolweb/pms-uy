"""A country pack may tax a stay by where the guest lives.

Uruguay charges no VAT on a non-resident's room nights and does on a
resident's in high season, so the desk has to ask. A pack that implements
guest_residence (and the four that go with it, kamra.localization) makes
the desk ask at booking, on an existing booking and at check-in; a pack
without them never asks, and nothing in the quote, the booking or the
check-in flow changes.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import frappe

from kamra import localization as loc
from kamra.localization import generic
from kamra.tests.audit_fixtures import AuditTestCase


def _pack():
	return types.SimpleNamespace(
		guest_residence=MagicMock(return_value={"home_country": "Uruguay", "declared": None, "country": None,
		                                         "can_declare": True, "missing": True}),
		declare_residence=MagicMock(), residence_on_new_booking=MagicMock(),
		quote_residence=MagicMock(side_effect=lambda q, resident, inputs: {**q, "taxed": resident}),
		save_residence_country=MagicMock())


class TestTaxResidence(AuditTestCase):
	def test_a_pack_without_it_never_asks(self):
		self.assertFalse(loc.asks_residence(generic))
		self.assertFalse(loc.front_desk_vocabulary(generic)["asks_residence"])
		self.assertIsNone(loc.guest_residence(generic, "RES-X"))
		quote = {"amount_after_tax": 100}
		self.assertIs(loc.quote_residence(generic, quote, "1", {}), quote)
		loc.residence_on_new_booking(generic, object(), "1")  # nothing to do, nothing raised
		loc.save_residence_country(generic, "G-X", "Uruguay")
		with self.assertRaises(frappe.ValidationError):
			loc.declare_residence(generic, "RES-X", "1")

	def test_a_pack_that_taxes_by_residence_is_asked_with_a_clear_yes_or_no(self):
		pack = _pack()
		self.assertTrue(loc.front_desk_vocabulary(pack)["asks_residence"])
		self.assertTrue(loc.guest_residence(pack, "RES-X")["missing"])
		self.assertEqual(loc.quote_residence(pack, {"a": 1}, "0", {"property": "P"}), {"a": 1, "taxed": False})
		self.assertEqual(loc.quote_residence(pack, {"a": 1}, True, {}), {"a": 1, "taxed": True})
		# no answer, or anything but "1" / "0", is no answer: nothing is guessed for the guest
		for value in (None, "", "yes", "2"):
			self.assertEqual(loc.quote_residence(pack, {"a": 1}, value, {}), {"a": 1})
		doc = object()
		loc.residence_on_new_booking(pack, doc, "1")
		pack.residence_on_new_booking.assert_called_once_with(doc, True)
		loc.residence_on_new_booking(pack, doc, None)
		self.assertEqual(pack.residence_on_new_booking.call_count, 1)
		loc.declare_residence(pack, "RES-X", "0")
		pack.declare_residence.assert_called_once_with("RES-X", False)
		with self.assertRaises(frappe.ValidationError):
			loc.declare_residence(pack, "RES-X", "maybe")
		loc.save_residence_country(pack, "G-X", "  ")
		pack.save_residence_country.assert_not_called()
		loc.save_residence_country(pack, "G-X", "Argentina")
		pack.save_residence_country.assert_called_once_with("G-X", "Argentina")
