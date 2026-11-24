"""The public page quotes and books a long stay: with no `maximum_nights` on
the property a 37-night stay that crosses a season with its own minimum stay
is priced night by night and booked; with one, longer stays are refused.

Runs with `bench run-tests --module kamra.tests.test_long_online_stay`.
"""

import frappe

from kamra import public_api
from kamra.tests.audit_fixtures import AuditTestCase, d, make_property, make_room, make_room_type

PROP = "UT Long Online Stay"
PHONE = "919800032237"


class TestLongOnlineStay(AuditTestCase):
	properties = (PROP,)

	def test_a_37_night_stay_is_quoted_and_booked_unless_the_property_caps_it(self):
		frappe.local.lang = "en"
		make_property(PROP, booking_engine_enabled=1, booking_payment_mode="Pay at hotel",
		              booking_mode="Instant", minimum_nights=1, maximum_nights=0,
		              cleaning_fee=0)
		rt = make_room_type(PROP, "LNG", base_price=1000, adults_capacity=2)
		make_room(PROP, rt, "L101")
		# a high season with a 7-night minimum over the first 16 nights
		frappe.get_doc({"doctype": "Season", "property": PROP, "season_name": "UT Long High",
			"start_date": d(5), "end_date": d(20), "adjustment_type": "Percent",
			"adjustment_value": 50, "minimum_nights": 7, "priority": 10}).insert(
			ignore_permissions=True)
		ci, co = d(5), d(42)  # 37 nights

		row = public_api.search_stay(PROP, ci, co, 2, 0)[0]
		self.assertTrue(row["available"], row.get("reason"))
		self.assertNotIn("reason", row)
		high = public_api.search_stay(PROP, ci, d(21), 2, 0)[0]["quote"]
		low = public_api.search_stay(PROP, d(21), co, 2, 0)[0]["quote"]
		self.assertGreater(high["amount_before_tax"], 16 * 1000)  # the season is charged
		self.assertAlmostEqual(row["quote"]["amount_before_tax"],
		                       high["amount_before_tax"] + low["amount_before_tax"], places=2)
		self.assertEqual(public_api.showcase(PROP)["property"]["maximum_nights"], 0)

		out = public_api.book(property=PROP, room_type=rt, check_in_date=ci, check_out_date=co,
		                      guest_name="UT Long Guest", phone=PHONE,
		                      email="long-guest@example.com", adults=2)
		self.assertEqual(out["status"], "Confirmed")
		self.assertAlmostEqual(out["amount_after_tax"], row["quote"]["amount_after_tax"], places=2)
		res = frappe.db.get_value("Reservation", out["reservation"],
		                          ["check_in_date", "check_out_date"], as_dict=True)
		self.assertEqual((str(res.check_in_date), str(res.check_out_date)), (ci, co))

		frappe.db.set_value("Property", PROP, "maximum_nights", 30)
		frappe.clear_document_cache("Property", PROP)
		for call in (lambda: public_api.search_stay(PROP, d(50), d(87), 2, 0),
		             lambda: public_api.book(property=PROP, room_type=rt, check_in_date=d(50),
		                                     check_out_date=d(87), guest_name="UT Long Guest",
		                                     phone=PHONE, adults=2)):
			with self.assertRaisesRegex(frappe.ValidationError, "longer than 30 nights"):
				call()
		self.assertTrue(public_api.search_stay(PROP, d(50), d(80), 2, 0)[0]["available"])
