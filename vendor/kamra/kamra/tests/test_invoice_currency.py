"""A country pack may let the desk pick the currency a stay is invoiced in.

Prices, the folio and every payment stay in the property's currency. In some
countries the tax invoice may still go out in another one: in Uruguay the
hotel invoices in dollars or in pesos, and the CFE goes out at check-in, so
the desk picks it before the guest is checked in. A pack like that implements
invoice_currency and set_invoice_currency (kamra.localization): the guided
check-in carries the offer for its last screen, and kamra.api.check_in hands
the desk's pick to the pack before the reservation is saved as checked in. A
pack without them offers nothing and takes no pick; a pick the pack did not
offer is refused and the guest is not checked in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

from kamra import api, checkin
from kamra import localization as loc
from kamra.localization import generic
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

OFFER = {
	"title": "Invoice currency",
	"text": "Dollars proposed: high season.",
	"options": [{"code": "USD", "label": "Dollars (USD)", "note": "The invoice carries the rate."},
	            {"code": "UYU", "label": "Pesos (UYU)", "note": "Converted at the central bank rate."}],
	"currency": "USD",
}


class TestTheOffer(AuditTestCase):
	def test_a_pack_without_it_offers_nothing_and_takes_no_pick(self):
		self.assertIsNone(loc.invoice_currency(generic, "RES-X"))
		doc = frappe._dict(name="RES-X")
		with self.assertRaises(frappe.ValidationError):
			loc.set_invoice_currency(generic, doc, "USD")

	def test_what_the_pack_offers_is_what_the_desk_sees(self):
		pack = MagicMock(spec=["invoice_currency"])
		pack.invoice_currency.return_value = OFFER
		self.assertEqual(loc.invoice_currency(pack, "RES-X"), OFFER)
		pack.invoice_currency.assert_called_once_with("RES-X")
		pack.invoice_currency.return_value = None
		self.assertIsNone(loc.invoice_currency(pack, "RES-X"))

	def test_one_currency_is_no_choice_and_a_bad_code_is_dropped(self):
		pack = MagicMock(spec=["invoice_currency"])
		pack.invoice_currency.return_value = {"options": [{"code": "usd"}, {"code": "dollars"}, {"code": "USD"}]}
		self.assertIsNone(loc.invoice_currency(pack, "RES-X"))
		pack.invoice_currency.return_value = {"options": [{"code": "usd"}, {"code": " uyu "}], "currency": "EUR"}
		self.assertEqual(loc.invoice_currency(pack, "RES-X"), {
			"title": "", "text": "", "currency": "USD",
			"options": [{"code": "USD", "label": "USD", "note": ""}, {"code": "UYU", "label": "UYU", "note": ""}]})

	def test_a_pack_that_fails_never_stops_the_check_in_screen(self):
		pack = MagicMock(spec=["invoice_currency"])
		pack.invoice_currency.side_effect = RuntimeError("boom")
		self.assertIsNone(loc.invoice_currency(pack, "RES-X"))


class TestThePickAtCheckIn(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		frappe.db.set_value("Room", self.room, "housekeeping_status", "Clean")
		self.res = make_reservation(PROP_A, self.rt, TODAY, d(2), room=self.room)
		pack = loc.pack_for(PROP_A)
		self.offer = self._stack.enter_context(
			patch.object(pack, "invoice_currency", create=True, new=MagicMock(return_value=OFFER)))
		self.picked: list[tuple[str, str, str]] = []

		def pick(doc, code):
			self.picked.append((doc.name, doc.status, code))
			doc.flags.invoice_currency = code

		self.pick = self._stack.enter_context(
			patch.object(pack, "set_invoice_currency", create=True, new=MagicMock(side_effect=pick)))

	def status(self):
		return frappe.db.get_value("Reservation", self.res.name, "status")

	def test_the_check_in_screen_carries_the_offer(self):
		self.assertEqual(checkin.checkin_flow(self.res.name)["invoice_currency"], OFFER)
		self.offer.return_value = None
		self.assertIsNone(checkin.checkin_flow(self.res.name)["invoice_currency"])

	def test_check_in_hands_the_pick_to_the_pack_before_the_guest_is_in(self):
		api.check_in(self.res.name, invoice_currency="uyu")
		self.assertEqual(self.picked, [(self.res.name, "Confirmed", "UYU")])
		# the pack got the very reservation that was then saved as checked in: its hooks see the pick
		[doc] = [c.args[0] for c in self.pick.call_args_list]
		self.assertEqual((doc.status, doc.flags.invoice_currency), ("Checked In", "UYU"))
		self.assertEqual(self.status(), "Checked In")

	def test_a_currency_not_offered_is_refused_and_the_guest_is_not_checked_in(self):
		with self.assertRaises(frappe.ValidationError):
			api.check_in(self.res.name, invoice_currency="EUR")
		self.assertEqual(self.picked, [])
		self.assertEqual(self.status(), "Confirmed")

	def test_without_a_pick_the_check_in_is_as_before(self):
		api.check_in(self.res.name)
		self.assertEqual(self.picked, [])
		self.assertEqual(self.status(), "Checked In")
