"""A season priced by occupancy (HR-367): an Absolute season whose value is the
room at the room type's base occupancy, the guests above it paying the season's
own extra-adult and child amounts - the same rules Room Type occupancy pricing
follows. Quote, the folio and the channel manager's ARI all price the party
with it; without the flag the season keeps replacing the rate, as it always has."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate, nowdate

from kamra import folio, pricing
from kamra.tests.audit_fixtures import PROP_A, AuditTestCase, make_property


class SeasonByOccupancyTestCase(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		make_property(PROP_A)
		# a triple sold per person: 100 for two, 30 a head above (children included)
		self.rt = frappe.get_doc({
			"doctype": "Room Type", "property": PROP_A, "room_type_code": "UT3P",
			"room_type_name": "UT Triple", "base_occupancy": 2, "adults_capacity": 3,
			"max_total_occupants": 3, "base_price": 100, "extra_adult_price": 30,
			"child_price": 30, "children_within_base_occupancy": 1,
			"single_occupancy_price": 0,
		}).insert(ignore_permissions=True).name
		frappe.clear_document_cache("Room Type", self.rt)
		self.day = str(getdate(add_days(nowdate(), 10)))
		self.off = str(add_days(self.day, 1))

	def tearDown(self):
		super().tearDown()
		frappe.clear_document_cache("Room Type", self.rt)

	def season(self, by_occupancy: int = 1):
		return frappe.get_doc({
			"doctype": "Season", "property": PROP_A, "season_name": "UT High", "room_type": self.rt,
			"start_date": self.day, "end_date": self.day, "adjustment_type": "Absolute",
			"adjustment_value": 200, "priced_by_occupancy": by_occupancy,
			"extra_adult_price": 50, "child_price": 50, "priority": 10,
		}).insert(ignore_permissions=True)

	def night(self, date, adults: int, children: int = 0) -> Decimal:
		q = pricing.quote(PROP_A, self.rt, date, str(add_days(date, 1)), adults=adults,
		                  children=children)
		return Decimal(str(q["room_total"]))

	def test_in_season_the_party_pays_the_seasons_price_for_its_size(self):
		self.season()
		self.assertEqual([self.night(self.day, 1), self.night(self.day, 2), self.night(self.day, 3),
		                  self.night(self.day, 2, 1)], [200, 200, 250, 250])

	def test_off_season_the_room_type_prices_the_party(self):
		self.season()
		self.assertEqual([self.night(self.off, 1), self.night(self.off, 2), self.night(self.off, 3)],
		                 [100, 100, 130])

	def test_without_the_flag_the_season_still_replaces_the_rate(self):
		self.season(by_occupancy=0)
		self.assertEqual(self.night(self.day, 3), 200)
		self.assertEqual(pricing.season_adjust(PROP_A, self.day, Decimal(130), self.rt), Decimal(200))

	def test_the_price_explanation_shows_the_seasons_price_for_the_party(self):
		from kamra import rates
		self.season()
		rt = frappe.get_doc("Room Type", self.rt)
		self.assertEqual([rates.explain_night(PROP_A, rt, self.day, adults=a)["after_season"] for a in (2, 3)], [200, 250])

	def test_the_folio_bills_what_the_quote_charged(self):
		self.season()
		stay = frappe._dict(property=PROP_A, room_type=self.rt, adults=3, children=0,
		                    check_in_date=self.day, check_out_date=self.off)
		self.assertEqual(folio._plan_free_night_rate(stay, self.day), self.night(self.day, 3))
		self.assertEqual(folio._plan_free_night_rate(stay, self.day), 250)

	def test_the_channel_manager_gets_each_occupancy_and_the_full_room_per_room(self):
		from kamra import channel_ari
		self.season()
		conn = frappe.get_doc({
			"doctype": "Channel Manager Connection", "property": PROP_A, "provider": "Channex",
			"active": 0, "external_property_id": "cx-ut-3p", "webhook_secret": "ut-hook",
			"ari_batch_seconds": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name,
		                "room_type": self.rt, "external_room_id": "rt-triple",
		                "external_rate_id": "rp-triple"}).insert(ignore_permissions=True)
		per_person = channel_ari.restriction_cells(conn, self.day, self.day, {"rp-triple": [1, 2, 3]})
		self.assertEqual([(r["occupancy"], r["rate"]) for r in per_person["rp-triple"][self.day]["rates"]],
		                 [(1, 200), (2, 200), (3, 250)])
		# a plan sold per room is priced for the full room (D-S367-5)
		per_room = channel_ari.restriction_cells(conn, self.day, self.day)
		self.assertEqual(per_room["rp-triple"][self.day]["rate"], Decimal("250.00"))
